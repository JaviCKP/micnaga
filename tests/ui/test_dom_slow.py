"""Pruebas DOM en Chromium headless por CDP (sin Playwright). ``MICNAGA_RUN_SLOW=1``.

Las paginas hablan con los puentes Python reales mediante ``dev_api`` (solo pruebas).
"""

from __future__ import annotations

import functools
import time
from collections.abc import Iterator
from typing import Any

import pytest

from micnaga.config import Config
from micnaga.store.db import Database
from micnaga.ui import fake, startup
from micnaga.ui.bridge import (
    LibraryApi,
    PillApi,
    PopupApi,
    SettingsApi,
    SurfaceApi,
    WizardApi,
)
from micnaga.ui.contracts import UiCallbacks
from micnaga.ui.media import LocalServer
from tests.ui.cdp import Browser, Page, find_browser

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(find_browser() is None, reason="no Chromium/Edge"),
]


class Spy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def levels(self) -> tuple[float, float]:
        return (0.3, 0.1)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("handle_"):
            return lambda *a: self.calls.append((name, a))
        raise AttributeError(name)

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


class AuthoritativeSpy(Spy):
    """Un controlador con la cuenta atras del vigilante (``popup_state``) ya agotada."""

    def __init__(self) -> None:
        super().__init__()
        self.remaining = 0.0

    def popup_state(self) -> dict[str, Any]:
        return {"remaining_s": self.remaining, "total_s": 15.0, "paused": False}


class MemStore:
    def __init__(self) -> None:
        self.cfg = Config()

    def load(self) -> Config:
        return self.cfg

    def save(self, c: Config) -> None:
        self.cfg = c


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    root = tmp_path_factory.mktemp("dom") / "m"
    folders = fake.build_demo_library(root, audio=False)
    spy = Spy()
    auth = AuthoritativeSpy()
    cfg = Config()
    ops = fake.FakeLibraryOps(lambda: root)
    store = MemStore()
    finished: list[bool] = []
    api: dict[str, object] = {
        "popup": PopupApi(spy),
        "popup_auth": PopupApi(auth),
        "pill": PillApi(spy),
        "surface": SurfaceApi(spy),
        "library": LibraryApi(
            db_factory=lambda: Database(root / "micnaga.db"),
            meetings_root=lambda: root,
            audio_url=lambda f, t: "",
            config=lambda: cfg,
            ops=ops,
        ),
        "wizard": WizardApi(
            download_models=functools.partial(fake.fake_download_models, speed=4.0),
            levels=lambda: (0.5, 0.01),
            callbacks=UiCallbacks(on_first_run_done=lambda: finished.append(True)),
            play_test=lambda: None,
        ),
        "settings": SettingsApi(
            store=store,
            callbacks=UiCallbacks(),
            registry=startup.MemoryRegistry(),
            diagnostics=fake.fake_diagnostics,
        ),
    }
    with LocalServer(meetings_root=lambda: root, dev_api=api) as srv, Browser() as b:
        yield {
            "srv": srv,
            "b": b,
            "spy": spy,
            "auth": auth,
            "folders": folders,
            "store": store,
            "finished": finished,
        }


def page(
    world: dict[str, Any], name: str, surface: str, w: int, h: int, *, wait: float = 0.8, **q: Any
) -> Page:
    p: Page = world["b"].new_page(w, h)
    p.emulate(dark=False)
    p.goto(world["srv"].page_url(name, bridge="http", surface=surface, **q), wait=wait)
    return p


def test_popup_is_an_alertdialog_with_accessible_name_and_visible_default_focus(
    world: dict[str, Any],
) -> None:
    p = page(world, "popup.html", "popup", 372, 112)
    assert p.eval("document.querySelector('[role=alertdialog]') !== null")
    assert p.eval("document.getElementById('p-title').textContent") == "¿Estás en una reu?"
    assert (
        p.eval("document.querySelector('[role=alertdialog]').getAttribute('aria-labelledby')")
        == "p-title"
    )
    assert p.eval("document.activeElement.id") == "b-rec"
    assert p.eval("getComputedStyle(document.getElementById('lift')).opacity") == "1"
    assert p.eval(
        "[document.querySelector('.card').offsetWidth, document.querySelector('.card').offsetHeight]"  # noqa: E501
    ) == [372, 112]  # compact glass card (spec: 400 x 232; resized at the user's request)
    # 6 px: the Bitnaga promo's flat card (spec: 12; the glass card was 16)
    assert p.eval("getComputedStyle(document.querySelector('.card')).borderRadius") == "6px"
    # both answers keep their keyboard shortcut, also for assistive tech
    assert p.eval("document.getElementById('b-rec').getAttribute('aria-keyshortcuts')") == "Enter"
    assert p.eval("document.getElementById('b-no').getAttribute('aria-keyshortcuts')") == "Escape"


def test_popup_enter_means_grabar(world: dict[str, Any]) -> None:
    spy: Spy = world["spy"]
    spy.calls.clear()
    p = page(world, "popup.html", "popup", 372, 112)
    p.key("Enter", "Enter", 13)
    assert p.wait_for("micnaga.popup.phase === 'confirming' || micnaga.popup.phase === 'morphing'")
    assert p.wait_for("micnaga.popup.phase === 'done'", 3)
    assert ("handle_popup_decide", ("record",)) in spy.calls
    assert ("handle_popup_morph", (False,)) in spy.calls
    assert ("handle_popup_closed", ("morphed",)) in spy.calls
    assert p.eval("document.getElementById('card').dataset.morph") == "true"


def test_popup_reshown_after_a_morph_is_whole_again(world: dict[str, Any]) -> None:
    p = page(world, "popup.html", "popup", 372, 112)
    p.key("Enter", "Enter", 13)
    assert p.wait_for("micnaga.popup.phase === 'done'", 3)
    p.eval("micnaga.popup.show({title: 'Otra', code: 'abc-defg-hij', timeoutS: 30})")
    assert p.wait_for("micnaga.popup.phase === 'shown'", 2)
    assert p.eval(
        "[document.getElementById('card').offsetWidth, document.getElementById('card').offsetHeight]"  # noqa: E501
    ) == [372, 112]
    assert p.eval("getComputedStyle(document.getElementById('card'), '::after').opacity") == "0"


def test_popup_escape_means_ahora_no(world: dict[str, Any]) -> None:
    spy: Spy = world["spy"]
    spy.calls.clear()
    p = page(world, "popup.html", "popup", 372, 112)
    p.key("Escape", "Escape", 27)
    assert p.wait_for("micnaga.popup.phase === 'leaving' || micnaga.popup.phase === 'done'")
    assert p.wait_for("micnaga.popup.phase === 'done'", 2)
    assert ("handle_popup_decide", ("dismiss",)) in spy.calls
    assert not any(n == "handle_popup_morph" for n, _ in spy.calls)
    assert ("handle_popup_closed", ("leaving",)) in spy.calls


def test_popup_buttons_work_with_the_mouse(world: dict[str, Any]) -> None:
    spy: Spy = world["spy"]
    spy.calls.clear()
    p = page(world, "popup.html", "popup", 372, 112)
    p.click(*p.center("#b-no"))
    assert p.wait_for("micnaga.popup.phase === 'done'", 2)
    assert ("handle_popup_decide", ("dismiss",)) in spy.calls


def test_popup_countdown_pauses_on_hover_and_resumes(world: dict[str, Any]) -> None:
    spy: Spy = world["spy"]
    spy.calls.clear()
    p = page(world, "popup.html", "popup", 372, 112, timeout=40)
    time.sleep(0.5)
    p.mouse_move(150, 60)
    assert p.wait_for("document.getElementById('card').dataset.hover === 'true'")
    a = float(
        p.eval("getComputedStyle(document.querySelector('#countdown > i')).getPropertyValue('--p')")
    )
    time.sleep(1.2)
    b = float(
        p.eval("getComputedStyle(document.querySelector('#countdown > i')).getPropertyValue('--p')")
    )
    assert abs(a - b) < 1e-6, "la barra debe quedarse quieta con el raton encima"
    assert ("handle_popup_hover", (True,)) in spy.calls
    p.mouse_move(900, 900)
    time.sleep(1.2)
    c = float(
        p.eval("getComputedStyle(document.querySelector('#countdown > i')).getPropertyValue('--p')")
    )
    assert c < b, "la cuenta atras reanuda al salir"
    assert ("handle_popup_hover", (False,)) in spy.calls


def test_popup_timeout_becomes_provisional_pill(world: dict[str, Any]) -> None:
    spy: Spy = world["spy"]
    spy.calls.clear()
    p = page(world, "popup.html", "popup", 372, 112, timeout=1)
    assert p.wait_for("micnaga.popup.phase === 'done'", 4)
    assert ("handle_popup_decide", ("timeout",)) in spy.calls
    assert ("handle_popup_morph", (True,)) in spy.calls
    assert p.eval("getComputedStyle(document.getElementById('echo-tag')).display") != "none"


def test_popup_never_decides_the_timeout_when_python_owns_the_countdown(
    world: dict[str, Any],
) -> None:
    auth: AuthoritativeSpy = world["auth"]
    auth.calls.clear()
    auth.remaining = 6.0
    p = page(world, "popup.html", "popup_auth", 372, 112, timeout=1)  # local 1 s would expire
    assert p.wait_for("micnaga.popup.authority === 'python'", 3)
    frac = float(
        p.eval("getComputedStyle(document.querySelector('#countdown > i')).getPropertyValue('--p')")
    )
    assert 0.3 < frac < 0.45, frac  # the bar draws Python's 6 s of 15 s, not the local 1 s
    auth.remaining = 0.0
    time.sleep(1.6)
    assert p.eval("micnaga.popup.phase") == "shown", "the JS must wait for expire()"
    assert not any(n == "handle_popup_decide" for n in auth.names())
    p.eval("micnaga.popup.expire()")
    assert p.wait_for("micnaga.popup.phase === 'done'", 3)
    assert ("handle_popup_morph", (True,)) in auth.calls
    assert not any(n == "handle_popup_decide" for n in auth.names())


def test_popup_discard_policy_leaves_instead_of_morphing(world: dict[str, Any]) -> None:
    spy: Spy = world["spy"]
    spy.calls.clear()
    p = page(world, "popup.html", "popup", 372, 112, timeout=1, on_timeout="discard")
    assert p.wait_for("micnaga.popup.phase === 'done'", 4)
    assert ("handle_popup_closed", ("leaving",)) in spy.calls
    assert not any(n == "handle_popup_morph" for n, _ in spy.calls)


def test_popup_end_before_answer_fades(world: dict[str, Any]) -> None:
    spy: Spy = world["spy"]
    spy.calls.clear()
    p = page(world, "popup.html", "popup", 372, 112, timeout=40)
    p.eval("micnaga.popup.end()")
    assert p.wait_for("micnaga.popup.phase === 'done'", 2)
    assert ("handle_popup_closed", ("fading",)) in spy.calls
    assert not any(n == "handle_popup_decide" for n, _ in spy.calls)


def test_popup_reminder_can_be_dismissed_and_is_optional(world: dict[str, Any]) -> None:
    p = page(world, "popup.html", "popup", 372, 112, notice=0)
    assert p.eval("document.getElementById('notice').classList.contains('gone')")
    p = page(world, "popup.html", "popup", 372, 112)
    assert not p.eval("document.getElementById('notice').classList.contains('gone')")
    # a tiny mark; hovering it reveals the words and the × that hides it for good
    p.mouse_move(*p.center("#notice"))
    assert p.wait_for("getComputedStyle(document.getElementById('notice-x')).opacity === '1'", 2)
    time.sleep(0.3)
    p.click(*p.center("#notice-x"))
    assert p.wait_for("document.getElementById('notice').classList.contains('gone')")
    assert ("handle_notice_dismissed", ()) in world["spy"].calls


def test_popup_respects_reduced_motion(world: dict[str, Any]) -> None:
    p = world["b"].new_page(372, 112)
    p.emulate(dark=False, reduced_motion=True)
    p.goto(world["srv"].page_url("popup.html", bridge="http", surface="popup"), wait=0.6)
    assert p.eval("mic.motion") is False
    assert (
        p.eval("getComputedStyle(document.documentElement).getPropertyValue('--dur').trim()")
        == "0.01ms"
    )
    p2 = page(world, "popup.html", "popup", 372, 112, motion="off")
    assert p2.eval("mic.motion") is False and p2.eval("mic.dur(220)") == 0


def test_popup_and_pill_use_the_spec_easing(world: dict[str, Any]) -> None:
    p = page(world, "popup.html", "popup", 372, 112)
    assert (
        p.eval("getComputedStyle(document.documentElement).getPropertyValue('--ease').trim()")
        == "cubic-bezier(0.2, 0.8, 0.2, 1)"
    )
    assert (
        p.eval("getComputedStyle(document.getElementById('lift')).transitionDuration")
        == "0.22s, 0.22s"
    )


def test_pill_hover_expands_and_buttons_route(world: dict[str, Any]) -> None:
    spy: Spy = world["spy"]
    spy.calls.clear()
    p = page(world, "pill.html", "pill", 320, 48, elapsed=65)
    assert p.eval("document.getElementById('pill').getBoundingClientRect().width") == 200
    assert p.eval("getComputedStyle(document.getElementById('pill')).opacity") == "0.6"
    assert p.eval("document.getElementById('time').textContent") in ("01:05", "01:06")
    p.mouse_move(150, 20)
    assert p.wait_for("document.getElementById('pill').getBoundingClientRect().width > 290", 2)
    assert p.wait_for(
        "parseFloat(getComputedStyle(document.getElementById('pill')).opacity) > 0.95", 2
    )
    assert ("handle_pill_hover", (True,)) in spy.calls
    p.click(*p.center("#b-marker"))
    p.click(*p.center("#b-stop"))
    deadline = time.time() + 2  # the bridge calls are async HTTP posts in this harness
    while time.time() < deadline and not {"handle_pill_marker", "handle_pill_stop"} <= set(
        spy.names()
    ):
        time.sleep(0.05)
    assert {"handle_pill_marker", "handle_pill_stop"} <= set(spy.names())
    time.sleep(0.3)
    p.click(*p.center("#b-pause"))
    time.sleep(0.4)
    assert "handle_pill_toggle_pause" in spy.names()


def test_pill_pause_freezes_timer_and_turns_amber(world: dict[str, Any]) -> None:
    p = page(world, "pill.html", "pill", 320, 48, elapsed=100)
    p.eval("micnaga.pill.setPaused(true)")
    t1 = p.eval("document.getElementById('time').textContent")
    time.sleep(1.4)
    assert p.eval("document.getElementById('time').textContent") == t1
    assert p.eval("document.getElementById('pill').dataset.state") == "paused"
    assert p.eval("document.getElementById('b-pause').getAttribute('aria-pressed')") == "true"
    p.eval("micnaga.pill.setPaused(false)")
    time.sleep(1.4)
    assert p.eval("document.getElementById('time').textContent") != t1


def test_pill_provisional_variant_shows_label(world: dict[str, Any]) -> None:
    # provisional: the «¿Guardar?» decision replaces the plain label (§18)
    p = page(world, "pill.html", "pill", 320, 48, provisional=1)
    assert p.eval("getComputedStyle(document.getElementById('b-save')).display") != "none"
    assert p.eval("document.getElementById('tag').textContent") == "provisional"
    p2 = page(world, "pill.html", "pill", 320, 48)
    assert p2.eval("getComputedStyle(document.getElementById('b-save')).display") == "none"
    assert p2.eval("getComputedStyle(document.getElementById('tag')).display") == "none"


def test_pill_is_200_by_40_with_pulsing_dot(world: dict[str, Any]) -> None:
    p = page(world, "pill.html", "pill", 320, 48)
    assert p.eval(
        "(() => { const r = document.getElementById('pill').getBoundingClientRect(); return [r.width, r.height]; })()"  # noqa: E501
    ) == [200, 40]
    assert (
        p.eval("getComputedStyle(document.querySelector('.rec-dot'), '::after').animationDuration")
        == "1.2s"
    )


def test_library_search_is_debounced_highlighted_and_opens_the_phrase(
    world: dict[str, Any],
) -> None:
    p = page(world, "library.html", "library", 1180, 760)
    assert p.eval("document.querySelectorAll('.row[data-folder]').length") == 5
    p.eval(
        "(() => { const q = document.getElementById('q'); q.value = 'proveedor'; q.dispatchEvent(new Event('input')); })()"  # noqa: E501
    )
    assert p.wait_for("document.querySelectorAll('.hit').length > 0", 3)
    assert p.eval("document.querySelector('.hit mark').textContent.toLowerCase()") == "proveedor"
    p.click(*p.center(".hit"))
    assert p.wait_for("!document.getElementById('viewer').hidden", 3)
    assert p.eval("document.querySelectorAll('#tr .ph').length") > 5
    assert (
        p.eval("document.querySelectorAll('#tr .ph mark').length") > 0
    )  # termino resaltado en la transcripcion
    # nada de HTML inyectado desde el texto
    assert p.eval("document.querySelectorAll('#side script, #tr script').length") == 0


def test_library_phrases_are_buttons_and_rename_is_inline(world: dict[str, Any]) -> None:
    p = page(world, "library.html", "library", 1180, 760, select="first")
    assert p.wait_for("document.querySelectorAll('#tr .ph').length > 0", 3)
    assert p.eval("document.querySelector('#tr .ph').getAttribute('role')") == "button"
    assert p.eval("document.querySelector('#tr .ph').tabIndex") == 0
    assert p.eval("document.querySelectorAll('#markers .mk').length") == 2
    p.eval("document.querySelectorAll('.spk .name')[2].click()")
    assert p.wait_for("document.querySelector('.spk input') !== null")
    p.eval("(() => { const i = document.querySelector('.spk input'); i.value = 'Marcos'; })()")
    p.key("Enter", "Enter", 13)
    assert p.wait_for(
        "[...document.querySelectorAll('.spk .name')].some(b => b.textContent === 'Marcos')", 3
    )
    assert p.eval(
        "[...document.querySelectorAll('#tr .ph .who span')].some(s => s.textContent === 'Marcos')"
    )


def test_library_voiceprint_consent_dialog_gates_the_save(world: dict[str, Any]) -> None:
    p = page(world, "library.html", "library", 1180, 760, select="first")
    assert p.wait_for("document.querySelectorAll('.spk').length > 0", 3)
    # la huella esta desactivada en Ajustes por defecto: el menu no ofrece guardarla
    p.eval("document.querySelectorAll('.spk .more')[0].click()")
    assert p.wait_for("!document.getElementById('menu').hidden")
    labels = p.eval("[...document.querySelectorAll('#menu button')].map(b => b.textContent)")
    assert not any("huella" in x.lower() for x in labels)


def test_settings_form_dirty_state_and_save(world: dict[str, Any]) -> None:
    p = page(world, "settings.html", "settings", 880, 760, tab="detection")
    assert p.eval("document.getElementById('savebar').dataset.dirty") == "false"
    assert p.eval("document.querySelectorAll('.frow').length") > 8
    p.eval(
        "(() => { const i = document.querySelector('[data-path=\"detection.popup_timeout_s\"] input'); i.value = '25'; i.dispatchEvent(new Event('input')); })()"  # noqa: E501
    )
    assert p.eval("document.getElementById('savebar').dataset.dirty") == "true"
    time.sleep(0.5)  # la barra de guardado entra con una transicion de 220 ms
    p.click(*p.center("#save"))
    assert p.wait_for("document.getElementById('save-msg').textContent === 'Cambios guardados'", 3)
    assert world["store"].cfg.detection.popup_timeout_s == 25
    # fuera de rango: no se puede guardar
    p.eval(
        "(() => { const i = document.querySelector('[data-path=\"detection.popup_timeout_s\"] input'); i.value = '1'; i.dispatchEvent(new Event('input')); })()"  # noqa: E501
    )
    assert p.eval("document.getElementById('save').disabled") is True


def test_settings_has_diagnostics_panel_polling_signals(world: dict[str, Any]) -> None:
    p = page(world, "settings.html", "settings", 880, 760, tab="diagnostics", wait=1.4)
    assert p.eval("document.querySelectorAll('#diag-list li').length") == 5
    assert p.eval("document.querySelector('#diag-list .sig').getAttribute('role')") == "img"


def test_settings_tabs_are_keyboard_navigable(world: dict[str, Any]) -> None:
    p = page(world, "settings.html", "settings", 880, 760, tab="general")
    p.eval("document.getElementById('tab-general').focus()")
    p.key("ArrowDown", "ArrowDown", 40)
    assert p.eval("document.querySelector('.tab[aria-selected=true]').dataset.id") == "detection"
    assert p.eval("document.getElementById('pane-detection').hidden") is False


def test_wizard_is_one_click_downloads_by_itself_and_closes(world: dict[str, Any]) -> None:
    finished: list[bool] = world["finished"]
    finished.clear()
    p = page(world, "wizard.html", "wizard", 600, 680)
    # no «Descargar» / «Continuar»: the download starts on its own
    assert p.eval("document.querySelectorAll('button:not([hidden])').length") == 1  # retry only
    assert p.eval("document.getElementById('fail').hidden") is True
    assert p.wait_for(
        "+document.getElementById('overall-bar').getAttribute('aria-valuenow') > 0", 4
    )
    assert p.wait_for("micnaga.wizard.state === 'done'", 8)
    deadline = time.time() + 4  # «Listo» for a breath, then finish() closes the window
    while time.time() < deadline and not finished:
        time.sleep(0.05)
    assert finished == [True]


def _rect(p: Page) -> list[float]:
    return p.eval(  # type: ignore[no-any-return]
        "(() => { const r = document.getElementById('pill').getBoundingClientRect();"
        " return [r.left, r.top, r.width, r.height].map(Math.round); })()"
    )


def test_pill_becomes_the_card_with_a_fade_and_no_morph(world: dict[str, Any]) -> None:
    """Dos estados, sin morfosis (DECISIONS §29): la forma salta de pastilla a tarjeta con el
    contenido oculto y el contenido entra con un fundido; nada anima ancho/alto/radio. El punto
    y el cronometro guardan la distancia a la esquina (son la cabecera de la tarjeta)."""
    spy = world["spy"]
    spy.calls.clear()
    p = page(world, "pill.html", "surface", 420, 112, levels="0.3,0.1")
    p.eval("micnaga.pill.layout({px: 210, py: 0, cardL: 24})")
    assert _rect(p) == [110, 0, 200, 40] and p.eval("micnaga.surface.phase") == "pill"
    dot0 = p.eval("document.querySelector('.state-ico').getBoundingClientRect().left")
    p.eval("micnaga.popup.show({title: 'Revisión semanal', code: 'abc-defg-hij', timeoutS: 40})")
    # la forma ya es la tarjeta al instante (sin pasos intermedios), con el contenido velado
    assert _rect(p) == [24, 0, 372, 112]
    props = str(p.eval("getComputedStyle(document.getElementById('pill')).transitionProperty"))
    assert "width" not in props and "height" not in props and "left" not in props
    assert p.wait_for("micnaga.surface.phase === 'card'", 1)
    assert p.wait_for("document.getElementById('pill').dataset.veil === 'off'", 1)
    assert p.wait_for("getComputedStyle(document.getElementById('card')).opacity === '1'", 1)
    dot1 = p.eval("document.querySelector('.state-ico').getBoundingClientRect().left")
    assert dot0 - 110 == dot1 - 24
    assert ("handle_surface_phase", ("card",)) in spy.calls
    assert not p.eval("document.getElementById('pill').classList.contains('pywebview-drag-region')")
    p.eval("micnaga.popup.dismiss()")
    assert p.eval("document.getElementById('pill').dataset.veil") == "on"  # se funde fuera
    assert _rect(p) == [24, 0, 372, 112]  # ...con la talla de tarjeta mientras se va
    assert p.wait_for("micnaga.surface.phase === 'pill' && micnaga.popup.phase === 'done'", 2)
    assert _rect(p) == [110, 0, 200, 40]
    assert p.wait_for("document.getElementById('pill').dataset.veil === 'off'", 1)
    assert ("handle_surface_phase", ("collapsing",)) in spy.calls
    assert ("handle_popup_closed", ("leaving",)) in spy.calls
    assert p.eval("document.getElementById('pill').classList.contains('pywebview-drag-region')")


def test_reduced_motion_swaps_instantly(world: dict[str, Any]) -> None:
    p = page(world, "pill.html", "surface", 420, 112, motion="off")
    p.eval("micnaga.pill.layout({px: 210, py: 0, cardL: 24})")
    p.eval("micnaga.popup.show({title: 'x', code: '', timeoutS: 40})")
    assert p.wait_for("micnaga.surface.phase === 'card'", 1)
    for sel in ("pill", "card"):
        duration = p.eval(f"getComputedStyle(document.getElementById('{sel}')).transitionDuration")
        assert set(str(duration).replace(" ", "").split(",")) == {"0s"}, (sel, duration)


def test_in_the_app_the_window_is_resized_while_the_content_is_hidden(
    world: dict[str, Any],
) -> None:
    """``?fit=1`` (la app): ventana opaca que Python redimensiona a la forma; la pagina la llena
    y pinta el fondo de noche. La talla cambia SOLO con el contenido oculto: el JS funde la
    pastilla, pide la talla (``surface_resize``) y no muestra la tarjeta hasta ``resized()``."""
    spy = world["spy"]
    spy.calls.clear()
    p = page(world, "pill.html", "surface", 200, 40, fit=1)
    assert p.eval("document.documentElement.dataset.fit") == "window"
    assert _rect(p) == [0, 0, 200, 40]
    assert p.eval("getComputedStyle(document.body).backgroundColor") == "rgb(10, 9, 15)"
    p.eval("micnaga.popup.show({title: 'Revisión semanal', code: 'abc-defg-hij', timeoutS: 40})")
    assert p.eval("document.getElementById('pill').dataset.veil") == "on"
    assert p.wait_for("micnaga.surface.phase === 'expanding'", 1)
    deadline = time.time() + 2
    while time.time() < deadline and ("handle_surface_resize", ("card",)) not in spy.calls:
        time.sleep(0.05)
    assert ("handle_surface_resize", ("card",)) in spy.calls
    assert p.eval("micnaga.surface.phase") == "expanding"  # espera a Python: sigue oculta
    p.eval("micnaga.surface.resized()")  # Python: la ventana ya mide la tarjeta
    assert p.wait_for("micnaga.surface.phase === 'card'", 1)
    assert p.wait_for("document.getElementById('pill').dataset.veil === 'off'", 1)
    assert _rect(p) == [0, 0, 200, 40]  # la forma llena la ventana, sea cual sea su talla
    p.eval("micnaga.popup.dismiss()")
    assert p.wait_for("micnaga.surface.phase === 'pill' && micnaga.popup.phase === 'done'", 1)
    assert p.eval("document.getElementById('pill').dataset.veil") == "on"  # hasta encoger
    p.eval("micnaga.surface.resized()")
    assert p.wait_for("document.getElementById('pill').dataset.veil === 'off'", 1)
    # una ventana que estaba oculta ya tiene la talla: no pide otra
    p2 = page(world, "pill.html", "surface", 372, 112, fit=1)
    spy.calls.clear()
    p2.eval("micnaga.surface.prepare(true)")
    p2.eval("micnaga.popup.show({title: 'x', code: '', timeoutS: 40})")
    assert p2.wait_for("micnaga.surface.phase === 'card'", 1)
    assert p2.wait_for("document.getElementById('pill').dataset.veil === 'off'", 1)
    assert ("handle_surface_resize", ("card",)) not in spy.calls
