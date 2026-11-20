"""UiHost con ventanas y Win32 falsos: orquestacion de popup, pastilla, bandeja y ventanas."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest

from micnaga.config import Config
from micnaga.ui import winapi
from micnaga.ui.contracts import UiCallbacks
from micnaga.ui.host import UiHost
from micnaga.ui.windows import FakeFactory, FakeWindow
from tests.ui.fakes import FakeWin32


class MemStore:
    def __init__(self) -> None:
        self.cfg = Config()

    def load(self) -> Config:
        return copy.deepcopy(self.cfg)

    def save(self, config: Config) -> None:
        self.cfg = config


class Rig:
    def __init__(self, cfg: Config | None = None, **cb: Any) -> None:
        self.events: list[Any] = []
        callbacks = UiCallbacks(
            on_popup_decision=lambda d: self.events.append(("decision", d)),
            on_popup_hover=lambda h: self.events.append(("hover", h)),
            on_popup_closed=lambda k: self.events.append(("closed", k)),
            on_marker=lambda: self.events.append("marker"),
            on_pause_toggle=lambda p: self.events.append(("pause", p)),
            on_stop=lambda: self.events.append("stop"),
            on_pill_moved=lambda x, y: self.events.append(("moved", x, y)),
            on_open_meeting=lambda f: self.events.append(("meeting", f)),
        )
        for k, v in cb.items():
            setattr(callbacks, k, v)
        self.w32 = FakeWin32()
        self.w32.animation = False
        self.fac = FakeFactory()
        self.store = MemStore()
        if cfg is not None:
            self.store.cfg = cfg
        self.played: list[bytes] = []
        self.host = UiHost(
            config=copy.deepcopy(self.store.cfg),
            callbacks=callbacks,
            factory=self.fac,
            win32=self.w32,
            config_store=self.store,
            spawn=lambda fn: fn(),
            tray=False,
            play=self.played.append,
        )
        self.host._create_overlays()

    @property
    def popup(self) -> FakeWindow:
        """La tarjeta vive en la misma ventana que la pastilla (superficie unica)."""
        return self.fac.windows["pill"]

    @property
    def pill(self) -> FakeWindow:
        return self.fac.windows["pill"]


def payload(win: FakeWindow, fn: str) -> Any:
    for js in win.js_calls():
        m = re.match(rf"{re.escape(fn)}\((.*)\)$", js)
        if m:
            return json.loads(m.group(1))
    raise AssertionError(f"{fn} not called: {win.js_calls()}")


def test_run_creates_one_hidden_surface_applies_noactivate_and_calls_on_ready() -> None:
    r = Rig()
    ready: list[UiHost] = []
    r.host._want_tray = False
    r.host._factory.start(lambda: r.host._bootstrap(ready.append), icon=None)  # type: ignore[arg-type]
    assert ready == [r.host]
    assert set(r.fac.windows) == {"pill"}, "una sola superficie: la tarjeta nace de la pastilla"
    spec = r.pill.spec
    assert spec.transparent and spec.frameless and spec.on_top and spec.hidden and not spec.focus
    assert spec.min_size[1] <= 40, "min_size por defecto (200x100) deformaria la pastilla"
    style = r.w32.styles[r.pill.hwnd]
    assert style & winapi.WS_EX_NOACTIVATE and style & winapi.WS_EX_TOOLWINDOW
    assert (spec.width, spec.height) == winapi.STAGE_SIZE == (420, 112)
    assert r.host._popup is r.host._pillwin
    assert not r.host.acrylic, "el acrilico pintaria el lienzo entero, no la forma"
    r.host._stopping.set()


# geometria del monitor falso (1920 x 1040 a 125 %): pastilla 250 x 50 centrada arriba
PILL = ((1920 - 250) // 2, 15, 250, 50)
STAGE = (PILL[0] + 125 - 262, 15, 525, 140)  # 420 x 112 centrado sobre la pastilla
PILL_REGION = (137, 0, 250, 50)
CARD_REGION = (29, 0, 466, 140)


def test_stage_geometry_and_regions_follow_the_pill() -> None:
    mon = winapi.MonitorInfo((0, 0, 1920, 1040), 1.25)
    pill = winapi.pill_rect(mon)
    assert pill == PILL
    stage = winapi.stage_rect(pill, mon)
    assert stage == STAGE
    lay = winapi.stage_layout(pill, stage, mon.scale)
    assert lay == {"px": 209.6, "py": 0.0, "cardL": 23.6}  # tarjeta centrada en la pastilla
    assert winapi.css_region((lay["px"] - 100, 0, 200, 40), 1.25) == PILL_REGION
    assert winapi.css_region((lay["cardL"], 0, 372, 112), 1.25) == CARD_REGION
    # pastilla arrastrada al borde izquierdo: el lienzo no sale de la pantalla y la tarjeta
    # sigue conteniendo la pastilla
    edge = (0, 300, 250, 50)
    stage = winapi.stage_rect(edge, mon)
    lay = winapi.stage_layout(edge, stage, mon.scale)
    assert stage[0] == 0 and lay["px"] == 100 and lay["cardL"] == 0


def test_show_popup_grows_the_card_out_of_the_visible_pill() -> None:
    r = Rig()
    r.host.show_pill(elapsed_s=21.0, provisional=True)
    assert r.pill.current_rect == STAGE and r.pill.region == PILL_REGION
    assert r.host.surface_phase == "pill"
    r.w32.calls.clear()
    r.pill.log.clear()
    r.host.show_popup("Revisión semanal", "abc-defg-hij", monitor_hint=(10, 10, 900, 700))
    # nace donde esta la pastilla: ni otro monitor ni otra ventana ni otro tamano
    assert not any(c[0] == "monitor_info" for c in r.w32.calls)
    assert not any(k == "place" for k, _ in r.pill.log), "la ventana no se mueve ni redimensiona"
    assert r.pill.region == CARD_REGION, "la region se abre a la tarjeta ANTES de crecer"
    p = payload(r.popup, "micnaga.popup.show")
    assert p == {
        "title": "Revisión semanal",
        "code": "abc-defg-hij",
        "timeoutS": 15.0,
        "onTimeout": "provisional",
        "remindNotice": True,
    }
    kinds = [k for k, _ in r.pill.log]
    assert kinds.index("region") < kinds.index("show")
    assert not any(js.startswith("micnaga.pill.show(") for js in r.pill.js_calls())
    assert ("show", False) in r.pill.log  # activate=False
    assert r.host.popup_visible and r.host.surface_phase == "expanding"
    r.host.handle_surface_phase("card")
    assert r.host.surface_phase == "card"


def test_show_popup_without_a_pill_materialises_it_first_on_the_browser_monitor() -> None:
    """Ruta rapida de Meet: aun no hay pastilla; aparece en su sitio y de ella crece la
    tarjeta."""
    r = Rig()
    r.host.show_popup("Revisión semanal", "abc-defg-hij", monitor_hint=(10, 10, 900, 700))
    assert ("monitor_info", ((10, 10, 900, 700),)) in r.w32.calls
    assert r.pill.current_rect == STAGE and r.pill.region == CARD_REGION
    born = payload(r.pill, "micnaga.pill.show")
    assert born["enter"] is True and born["provisional"] is True
    assert born["layout"] == {"px": 209.6, "py": 0.0, "cardL": 23.6}
    calls = r.pill.js_calls()
    first_show = next(i for i, js in enumerate(calls) if js.startswith("micnaga.pill.show("))
    first_popup = next(i for i, js in enumerate(calls) if js.startswith("micnaga.popup.show("))
    assert first_show < first_popup
    kinds = [k for k, _ in r.pill.log]
    assert kinds.index("js") < kinds.index("show")  # estado pintado antes de mostrarla
    assert not r.host.pill_visible  # aun no es la pastilla de grabacion del controlador


def test_show_popup_honours_config_and_overrides() -> None:
    cfg = Config()
    cfg.detection.popup_timeout_s = 9
    cfg.detection.on_timeout = "discard"
    cfg.privacy.remind_notice = False
    r = Rig(cfg)
    r.host.show_popup("x")
    p = payload(r.popup, "micnaga.popup.show")
    assert (p["timeoutS"], p["onTimeout"], p["remindNotice"]) == (9, "discard", False)
    r.host.show_popup("x", timeout_s=3, on_timeout="provisional")
    p = json.loads(re.findall(r"micnaga\.popup\.show\((.*)\)", "\n".join(r.popup.js_calls()))[-1])
    assert (p["timeoutS"], p["onTimeout"]) == (3, "provisional")


def test_motion_off_is_pushed_when_windows_animations_are_disabled() -> None:
    r = Rig()
    r.host.show_popup("x")
    assert "mic.setMotion(false);" in r.popup.js_calls()
    r.w32.animation = True
    r.host.show_popup("x")
    assert "mic.setMotion(true);" in r.popup.js_calls()


def test_sound_is_opt_in_and_silent_under_dnd() -> None:
    r = Rig()
    r.host.show_popup("x")
    assert r.played == []  # desactivado por defecto
    cfg = Config()
    cfg.ui.sound = True
    r = Rig(cfg)
    r.host.show_popup("x")
    assert len(r.played) == 1
    r.w32.qstate = winapi.QUNS_QUIET_TIME
    r.host.show_popup("x")  # aparece igual, en silencio
    assert len(r.played) == 1 and r.host.popup_visible


def test_record_collapses_the_card_back_into_the_pill() -> None:
    """pill -> expanding -> card -> collapsing -> pill, en la misma ventana."""
    r = Rig()
    r.host.show_pill(elapsed_s=21.0, provisional=True)
    r.host.show_popup("x")
    r.host.handle_surface_phase("card")
    r.host.handle_popup_decide("record")
    r.host.handle_popup_morph(False)
    assert r.host.surface_phase == "collapsing"
    assert "micnaga.pill.setProvisional(false)" in r.pill.js_calls()
    shows = sum(1 for js in r.pill.js_calls() if js.startswith("micnaga.pill.show("))
    r.host.show_pill(elapsed_s=42.0, provisional=False)  # el controlador, durante el encogido
    assert sum(1 for js in r.pill.js_calls() if js.startswith("micnaga.pill.show(")) == shows
    r.host.handle_popup_closed("morphed")
    assert r.host.surface_phase == "pill" and r.pill.region == PILL_REGION
    assert r.pill.visible and ("hide", None) not in r.pill.log
    assert not r.host.popup_visible
    assert ("decision", "record") in r.events and ("closed", "morphed") in r.events
    assert r.host.tray_state() == "recording"


def test_meet_record_keeps_the_born_pill_as_the_recording_pill() -> None:
    r = Rig()
    r.host.show_popup("x", "abc-defg-hij")
    r.host.handle_popup_decide("record")
    r.host.handle_popup_morph(False)
    r.host.show_pill(elapsed_s=7.5)  # el controlador la pide mientras la tarjeta encoge
    assert "micnaga.pill.sync(7.50)" in r.pill.js_calls()  # manda el valor del vigilante
    r.host.handle_popup_closed("morphed")
    assert r.host.pill_visible and r.host.surface_phase == "pill" and r.pill.visible


def test_timeout_becomes_a_provisional_pill() -> None:
    r = Rig()
    r.host.show_pill(provisional=True)
    r.host.show_popup("x")
    r.host.handle_popup_decide("timeout")
    r.host.handle_popup_morph(True)
    assert "micnaga.pill.setProvisional(true)" in r.pill.js_calls()
    r.host.handle_popup_closed("morphed")
    r.host.set_pill_provisional(False)  # el usuario confirma luego
    assert "micnaga.pill.setProvisional(false)" in r.pill.js_calls()


def test_timeout_with_discard_policy_does_not_mark_provisional() -> None:
    cfg = Config()
    cfg.detection.on_timeout = "discard"
    r = Rig(cfg)
    r.host.show_popup("x")
    r.host.handle_popup_decide("timeout")
    r.host.handle_popup_closed("leaving")
    assert not r.host.pill_visible and ("closed", "leaving") in r.events
    assert "micnaga.pill.vanish()" in r.pill.js_calls() and not r.pill.visible


def test_no_collapses_into_the_pill_and_then_the_undo_chip() -> None:
    """«No»: el controlador oculta la pastilla y pide «Deshacer» mientras la tarjeta encoge;
    nada parpadea: al terminar, la pastilla se ensancha hasta el aviso."""
    r = Rig()
    r.host.show_pill(provisional=True)
    r.host.show_popup("x")
    r.host.handle_popup_decide("dismiss")
    r.host.handle_surface_phase("collapsing")
    r.host.hide_pill()
    r.host.show_undo(8.0)
    assert r.pill.visible and ("hide", None) not in r.pill.log
    assert not any(js.startswith("micnaga.pill.undo(") for js in r.pill.js_calls())
    r.host.handle_popup_closed("leaving")
    assert payload(r.pill, "micnaga.pill.undo") == {"seconds": 8.0}
    assert r.host.surface_phase == "undo" and r.pill.region == (114, 0, 296, 50)
    r.host.hide_undo()
    assert not r.pill.visible and r.host.surface_phase == "hidden"


def test_end_card_finish_shrinks_then_the_pill_fades_away() -> None:
    r = Rig()
    r.host.show_pill(elapsed_s=600.0)
    r.host.show_end_popup("Reunión", duration_s=600.0, confirmed=True)
    assert r.host.surface_phase == "expanding" and r.pill.region == CARD_REGION
    assert payload(r.pill, "micnaga.popup.showEnd")["confirmed"] is True
    r.host.handle_end_decide("finish")
    r.host.hide_pill()  # la grabacion termina mientras la tarjeta encoge
    assert r.pill.visible
    r.host.hide_end_popup("answered")
    assert 'micnaga.popup.endClose("answered")' in r.pill.js_calls()
    r.host.handle_popup_closed("leaving")
    assert "micnaga.pill.vanish()" in r.pill.js_calls() and not r.pill.visible


def test_end_card_keep_recording_returns_to_the_pill() -> None:
    r = Rig()
    r.host.show_pill(elapsed_s=600.0)
    r.host.show_end_popup("Reunión", duration_s=600.0, confirmed=True)
    r.host.handle_end_decide("keep")
    r.host.handle_popup_closed("leaving")
    assert r.host.surface_phase == "pill" and r.pill.visible and r.pill.region == PILL_REGION


def test_pill_standalone_show_hide_and_pause_state() -> None:
    r = Rig()
    r.host.show_pill(elapsed_s=12.0)
    assert r.pill.current_rect == STAGE and r.pill.region == PILL_REGION and r.host.pill_visible
    r.host.set_pill_paused(True)
    assert "micnaga.pill.setPaused(true)" in r.pill.js_calls()
    assert r.host.tray_state() == "paused"
    r.host.sync_pill(30.5)
    assert "micnaga.pill.sync(30.50)" in r.pill.js_calls()
    r.host.hide_pill()
    assert not r.host.pill_visible and r.host.tray_state() == "watching"
    assert "micnaga.pill.stop()" in r.pill.js_calls() and ("hide", None) in r.pill.log


def test_pill_hover_widens_the_region_symmetrically_and_restores() -> None:
    """La ventana no se redimensiona al pasar el raton: solo su region (sin tirones)."""
    r = Rig()
    r.host.show_pill()
    places = len(r.w32.places(r.pill.hwnd))
    r.host.handle_pill_hover(True)
    wide = round(winapi.PILL_HOVER_WIDTH * 1.25)
    x, y, w, h = PILL_REGION
    assert r.pill.region == (x - (wide - w) // 2, y, wide, h)
    r.host.handle_pill_hover(True)  # idempotente
    r.host.handle_pill_hover(False)
    assert r.pill.region == PILL_REGION
    assert len(r.w32.places(r.pill.hwnd)) == places


def test_hover_is_ignored_while_the_card_is_open() -> None:
    r = Rig()
    r.host.show_pill()
    r.host.show_popup("x")
    r.host.handle_pill_hover(True)
    assert r.pill.region == CARD_REGION


def test_pill_buttons_route_to_callbacks_and_pause_is_confirmed_in_the_ui() -> None:
    r = Rig()
    r.host.show_pill()
    r.host.handle_pill_marker()
    r.host.handle_pill_toggle_pause()
    r.host.handle_pill_toggle_pause()
    r.host.handle_pill_stop()
    assert r.events == ["marker", ("pause", True), ("pause", False), "stop"]
    assert r.pill.js_calls().count("micnaga.pill.setPaused(true)") == 1


def test_failed_pause_callback_leaves_the_pill_unpaused() -> None:
    def boom(_p: bool) -> None:
        raise RuntimeError("recorder fail")

    r = Rig(on_pause_toggle=boom)
    r.host.show_pill()
    r.host.handle_pill_toggle_pause()
    assert "micnaga.pill.setPaused(true)" not in r.pill.js_calls()
    assert r.host.tray_state() == "recording"


def test_dragged_pill_position_is_remembered_and_programmatic_moves_ignored() -> None:
    r = Rig()
    r.host.show_pill()
    r.pill.fire_moved(5, 5)  # eco de nuestro propio place(): ignorado
    assert not any(e[0] == "moved" for e in r.events if isinstance(e, tuple))
    r.host._pill.ignore_moves_until = 0.0
    r.pill.fire_moved(300, 200)  # esquina del lienzo; la pastilla esta desplazada dentro
    dx = PILL[0] - STAGE[0]
    assert ("moved", 300 + dx, 200) in r.events
    r.host.hide_pill()
    r.host.show_pill()
    assert r.pill.current_rect[:2] == (300, 200)  # vuelve donde el usuario la dejo


def test_remembered_pill_position_is_clamped_to_the_visible_area() -> None:
    r = Rig()
    r.host.show_pill()
    r.host._pill.ignore_moves_until = 0.0
    r.pill.fire_moved(9999, -400)
    r.host.show_pill()
    x, y, w, _h = r.pill.current_rect
    assert x + w <= 1920 and y >= 0


def test_popup_remembers_remind_notice_dismissal() -> None:
    r = Rig()
    r.host.handle_notice_dismissed()
    assert r.store.cfg.privacy.remind_notice is False
    assert r.host.config.privacy.remind_notice is False
    r.host.show_popup("x")
    assert payload(r.popup, "micnaga.popup.show")["remindNotice"] is False


def test_accept_dismiss_and_end_drive_the_popup_js_only_while_showing() -> None:
    r = Rig()
    r.host.accept_popup()
    assert r.popup.js_calls() == []  # no hay popup
    r.host.show_popup("x")
    r.host.accept_popup()
    r.host.dismiss_popup()
    assert (
        "micnaga.popup.accept()" in r.popup.js_calls()
        and "micnaga.popup.dismiss()" in r.popup.js_calls()
    )
    r.host._popup_state = "idle"
    r.host.hide_popup()
    assert "micnaga.popup.end()" not in r.popup.js_calls()


def test_hide_popup_fades_via_js_and_hides_after_close_event() -> None:
    r = Rig()
    r.host.show_popup("x")
    r.host.hide_popup("ended")
    assert "micnaga.popup.end()" in r.popup.js_calls()
    r.host.handle_popup_closed("fading")
    assert not r.host.popup_visible and ("closed", "fading") in r.events
    r.host.handle_popup_closed(
        "fading"
    )  # el temporizador de seguridad llega tarde: sin doble aviso
    assert r.events.count(("closed", "fading")) == 1


def test_clicking_the_card_activates_it_for_keyboard_and_hiding_restores_noactivate() -> None:
    r = Rig()
    r.host.show_popup("x")
    hwnd = r.popup.hwnd
    r.w32.styles[hwnd] = winapi.WS_EX_NOACTIVATE | winapi.WS_EX_TOOLWINDOW
    r.host.handle_popup_activate()
    assert not r.w32.styles[hwnd] & winapi.WS_EX_NOACTIVATE and r.w32.foreground == [hwnd]
    r.host.handle_popup_closed("leaving")
    assert r.w32.styles[hwnd] & winapi.WS_EX_NOACTIVATE


def test_tray_state_priorities() -> None:
    r = Rig()
    assert r.host.tray_state() == "watching"
    r.host.set_processing(True)
    assert r.host.tray_state() == "processing"
    r.host.show_pill()
    assert r.host.tray_state() == "recording"  # grabar manda sobre procesar
    r.host.set_pill_paused(True)
    assert r.host.tray_state() == "paused"
    r.host.hide_pill()
    assert r.host.tray_state() == "processing"


def test_levels_provider_shapes_and_failures() -> None:
    r = Rig()
    assert r.host.levels() == (0.0, 0.0)
    r.host.set_levels_provider(lambda: {"mic": 0.2, "system": 0.4})
    assert r.host.levels() == (0.2, 0.4)

    def boom() -> object:
        raise RuntimeError("device lost")

    r.host.set_levels_provider(boom)
    assert r.host.levels() == (0.0, 0.0)


def test_library_window_is_reused_and_navigates_to_a_meeting() -> None:
    r = Rig()
    r.host.open_library()
    spec = r.fac.created[-1]
    assert (
        spec.key == "library"
        and spec.page == "library.html"
        and spec.params["theme"] in ("light", "dark")
    )
    n = len(r.fac.created)
    r.host.open_library(folder="2026-11-18_1030_demo", seek_s=12.5)
    assert len(r.fac.created) == n  # misma ventana
    lib = r.fac.windows["library"]
    assert ("show", True) in lib.log
    assert 'micnaga.library.open("2026-11-18_1030_demo", 12.5)' in lib.js_calls()[-1]


def test_library_created_with_folder_param_and_closed_callback() -> None:
    r = Rig()
    closed: list[str] = []
    r.host.set_on_window_closed(closed.append)
    r.host.open_library(folder="f1", seek_s=3)
    assert r.fac.created[-1].params["folder"] == "f1" and r.fac.created[-1].params["t"] == "3.00"
    r.host.close_window("library")
    assert closed == ["library"] and not r.host.has_window("library")
    r.host.open_library()  # se puede volver a abrir
    assert r.host.has_window("library")


def test_settings_and_wizard_windows() -> None:
    r = Rig()
    r.host.open_settings()
    r.host.open_wizard()
    # Ajustes vive dentro de la biblioteca (misma ventana, vista «settings»)
    assert {s.key for s in r.fac.created} >= {"library", "wizard"}
    assert "settings" not in r.fac.windows
    assert r.fac.windows["library"].spec.params["view"] == "settings"
    assert r.fac.windows["wizard"].spec.resizable is False
    assert hasattr(r.fac.windows["library"].spec.js_api, "settings_call")


def test_open_settings_again_switches_the_open_library_to_settings() -> None:
    r = Rig()
    r.host.open_library()
    r.host.open_settings()
    assert any("showSettings()" in js for js in r.fac.windows["library"].js_calls())


def test_apply_config_pushes_theme_to_every_window() -> None:
    r = Rig()
    r.host.open_library()
    cfg = Config()
    cfg.ui.theme = "dark"
    r.host.apply_config(cfg)
    for win in (r.popup, r.pill, r.fac.windows["library"]):
        assert any('mic.setTheme("dark")' in js for js in win.js_calls())


def test_system_theme_param_follows_the_registry_value() -> None:
    r = Rig()
    r.w32.hkcu[(winapi.PERSONALIZE_KEY, "AppsUseLightTheme")] = 0
    r.host.open_settings()
    assert r.fac.windows["library"].spec.params["theme"] == "dark"
    assert r.fac.windows["library"].spec.background == "#050507"


def test_motion_param_when_windows_animations_off() -> None:
    r = Rig()
    r.host.open_settings()
    assert r.fac.windows["library"].spec.params.get("motion") == "off"


def test_toast_open_navigates_library_and_notifies_integrator() -> None:
    r = Rig()
    r.host._open_from_toast("2026-11-18_1030_demo")
    assert ("meeting", "2026-11-18_1030_demo") in r.events
    assert r.fac.created[-1].params["folder"] == "2026-11-18_1030_demo"


def test_quit_destroys_every_window() -> None:
    r = Rig()
    r.host.open_library()
    r.host.quit()
    assert all(not w.alive for w in r.fac.windows.values())


def test_show_popup_before_run_is_a_clear_error() -> None:
    host = UiHost(
        config=Config(),
        factory=FakeFactory(),
        win32=FakeWin32(),
        config_store=MemStore(),
        tray=False,
    )
    with pytest.raises(RuntimeError, match="run"):
        host.show_popup("x")


# ------------------------------------------------- regresion A: el popup no salia en uso real


class _StubbornWindow(FakeWindow):
    """El primer ``show`` no deja la ventana visible (como un WebView2 que no se pinta)."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.shows = 0

    def show(self, *, activate: bool) -> None:
        self.shows += 1
        self.log.append(("show", activate))
        self.visible = self.shows > 1


def test_popup_that_is_not_visible_after_show_is_presented_again() -> None:
    r = Rig()
    pop = _StubbornWindow(r.popup.spec, r.popup.hwnd)
    r.host._popup = pop
    r.host.show_popup("Reunión", "abc-defg-hij")
    assert not pop.visible
    shown_at = r.host._popup_shown_at
    assert r.host._verify_popup(shown_at) is False  # lo detecta y lo vuelve a presentar
    assert pop.visible and pop.shows == 2
    assert sum(1 for js in pop.js_calls() if js.startswith("micnaga.popup.show(")) == 2
    assert r.host._verify_popup(shown_at) is True


def test_verify_ignores_a_popup_that_was_already_answered() -> None:
    r = Rig()
    pop = _StubbornWindow(r.popup.spec, r.popup.hwnd)
    r.host._popup = pop
    r.host.show_popup("Reunión")
    shown_at = r.host._popup_shown_at
    r.host.handle_popup_closed("leaving")  # el JS avisa de que la tarjeta se fue
    assert r.host._verify_popup(shown_at) is True and pop.shows == 1


def test_bootstrap_hides_overlays_that_navigation_made_visible() -> None:
    """pywebview (edgechromium) hace ``form.Show()`` al navegar una ventana transparente aunque
    se creara oculta: al arrancar quedaban una tarjeta vacia y una pastilla visibles."""
    r = Rig()
    r.host._want_tray = False
    r.popup.visible = r.pill.visible = True
    r.host._bootstrap(None)
    assert not r.popup.visible and not r.pill.visible
    r.host._stopping.set()


def test_bootstrap_keeps_an_overlay_that_is_in_use() -> None:
    r = Rig()
    r.host._want_tray = False
    r.host.show_pill(elapsed_s=5.0)
    assert r.pill.visible
    r.host._bootstrap(None)
    assert r.pill.visible
    r.host._stopping.set()


def test_verify_does_not_re_present_an_answered_or_quitting_popup() -> None:
    for finish in ("answer", "end_answer", "quit"):
        r = Rig()
        pop = _StubbornWindow(r.popup.spec, r.popup.hwnd)
        r.host._popup = pop
        if finish == "end_answer":
            r.host.show_end_popup("Reunión", duration_s=60.0, confirmed=True)
            r.host.handle_end_decide("finish")
        else:
            r.host.show_popup("Reunión")
            if finish == "answer":
                r.host.handle_popup_decide("record")
            else:
                r.host._stopping.set()
        # la tarjeta se esta yendo (o la app sale): la comprobacion no la vuelve a mostrar
        assert r.host._verify_popup(r.host._popup_shown_at) is True, finish
        assert pop.shows == 1, finish
