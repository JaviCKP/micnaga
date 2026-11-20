"""Cambios de ``UiHost`` para la integracion: cuenta atras unica, pastilla sin duplicar."""

from __future__ import annotations

from micnaga.config import Config
from micnaga.ui import host as host_mod
from micnaga.watcher import MeetingInfo
from tests.app.fakes import Rig


def test_popup_state_default_without_provider() -> None:
    from tests.ui.test_host import Rig as UiRig

    ui = UiRig()
    st = ui.host.popup_state()
    assert set(st) == {"remaining_s", "total_s", "paused", "authoritative"}


def test_popup_api_exposes_popup_state() -> None:
    from micnaga.ui.bridge import PopupApi

    rig = Rig()
    api = PopupApi(rig.app.ui)  # type: ignore[arg-type]
    assert set(api.popup_state()) == {"remaining_s", "total_s", "paused", "authoritative"}


def test_expire_falls_back_to_a_python_collapse_when_the_js_does_not_react() -> None:
    rig = Rig()
    rig.ctl.start_recording(provisional=True, meeting=MeetingInfo())
    rig.ctl.show_popup(title="x", code=None, monitor_hint=None, timeout_s=15.0)
    host = rig.host
    assert host is not None
    host.expire_popup()
    assert any("micnaga.popup.expire" in js for js in rig.js(rig.popup))
    host._force_expire(True)  # pasan 0,8 s y el popup sigue "showing"
    assert host._popup_state == "idle"
    assert "micnaga.pill.settle()" in rig.js(rig.pill)  # el JS encaja ya en pastilla


def test_expire_with_discard_policy_just_hides() -> None:
    cfg = Config()
    cfg.detection.on_timeout = "discard"
    rig = Rig(cfg)
    rig.ctl.show_popup(title="x", code=None, monitor_hint=None, timeout_s=15.0)
    host = rig.host
    assert host is not None
    host._force_expire(False)
    assert host._popup_state == "idle" and not rig.popup.visible


def test_show_pill_twice_does_not_restart_the_clock() -> None:
    rig = Rig()
    host = rig.host
    assert host is not None
    host.show_pill(elapsed_s=5.0, provisional=True)
    host.show_pill(elapsed_s=0.0, provisional=False)
    shows = [js for js in rig.js(rig.pill) if js.startswith("micnaga.pill.show(")]
    assert len(shows) == 1
    assert any("setProvisional(false)" in js for js in rig.js(rig.pill))


def test_show_pill_while_the_card_is_open_is_never_hidden() -> None:
    """Nunca se graba sin pastilla: con la tarjeta abierta la pastilla ya esta en pantalla
    (es la propia tarjeta) y al cerrarse queda."""
    rig = Rig()
    host = rig.host
    assert host is not None
    host.show_popup("x", None, None)
    host.show_pill(elapsed_s=3.0, provisional=False)
    assert rig.pill.visible and host.pill_visible
    host.handle_popup_closed("morphed")
    assert rig.pill.visible and host.surface_phase == "pill"


# ------------------------------------------------------------------ transparencia


def _boot(rig: Rig) -> None:
    host = rig.host
    assert host is not None
    host._want_tray = False
    host._factory.start(lambda: host._bootstrap(None), icon=None)  # type: ignore[arg-type]


def test_surface_gets_real_transparency_and_no_acrylic() -> None:
    """La superficie es un lienzo mayor que la forma visible: el acrilico de DWM pintaria el
    lienzo entero, asi que la forma la dibuja el CSS y la recorta la region."""
    rig = Rig()
    _boot(rig)
    host = rig.host
    assert host is not None and not host.acrylic
    assert ("make_transparent", None) in rig.pill.log
    assert not any("setBackdrop" in js for js in rig.js(rig.pill))
    assert not [c for c in rig.w32.dwm if c[1] == 38]


def test_collapse_guard_closes_the_card_if_the_js_never_answers(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(host_mod, "COLLAPSE_GUARD_S", 0.05)
    rig = Rig()
    host = rig.host
    assert host is not None
    host.show_pill(elapsed_s=3.0)
    host.show_popup("x", None, None)
    host.hide_popup("ended")
    import time

    deadline = time.monotonic() + 3
    while host.popup_visible and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not host.popup_visible and host.surface_phase == "pill"


def test_acrylic_can_be_disabled_by_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MICNAGA_UI_ACRYLIC", "0")
    rig = Rig()
    _boot(rig)
    assert rig.host is not None and not rig.host.acrylic


def test_the_morph_never_moves_or_resizes_the_window() -> None:
    """Crecer y encoger es CSS dentro de un lienzo fijo: Win32 solo cambia la region."""
    rig = Rig()
    _boot(rig)
    host = rig.host
    assert host is not None
    host.show_pill(elapsed_s=21.0, provisional=True)
    places = list(rig.w32.places(rig.pill.hwnd))
    host.show_popup("x")
    host.handle_surface_phase("card")
    host.handle_popup_decide("record")
    host.handle_popup_morph(False)
    host.handle_popup_closed("morphed")
    assert rig.w32.places(rig.pill.hwnd) == places
    regions = [a for k, a in rig.pill.log if k == "region"]
    assert len(regions) >= 3 and regions[-1] == regions[0] != regions[-2]  # pill, card, pill


def test_popup_state_is_not_authoritative_without_a_provider() -> None:
    from tests.ui.test_host import Rig as UiRig

    ui = UiRig()
    assert ui.host.popup_state()["authoritative"] is False
    rig = Rig()
    assert rig.app.ui.popup_state()["authoritative"] is True  # type: ignore[attr-defined]
