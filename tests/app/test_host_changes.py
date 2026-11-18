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


def test_expire_falls_back_to_a_python_morph_when_the_js_does_not_react() -> None:
    rig = Rig()
    rig.ctl.show_popup(title="x", code=None, monitor_hint=None, timeout_s=15.0)
    rig.ctl.start_recording(provisional=True, meeting=MeetingInfo())
    host = rig.host
    assert host is not None
    host.expire_popup()
    assert any("micnaga.popup.expire" in js for js in rig.js(rig.popup))
    host._force_expire(True)  # pasan 0,8 s y el popup sigue "showing"
    assert host._popup_state in ("morphing", "idle")


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


def test_pending_pill_is_flushed_by_the_safety_timer(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(host_mod, "PILL_FLUSH_S", 0.05)
    rig = Rig()
    host = rig.host
    assert host is not None
    host.show_popup("x", None, None)
    host.show_pill(elapsed_s=3.0, provisional=False)
    assert not rig.pill.visible
    import time

    deadline = time.monotonic() + 3
    while not rig.pill.visible and time.monotonic() < deadline:
        time.sleep(0.02)
    assert rig.pill.visible


# ------------------------------------------------------------------ cristal real y transparencia


def _boot(rig: Rig) -> None:
    host = rig.host
    assert host is not None
    host._want_tray = False
    host._factory.start(lambda: host._bootstrap(None), icon=None)  # type: ignore[arg-type]


def test_overlays_get_real_transparency_and_acrylic_on_windows_11() -> None:
    rig = Rig()
    _boot(rig)
    host = rig.host
    assert host is not None and host.acrylic
    for key in ("popup", "pill"):
        win = rig.factory.windows[key]
        assert ("make_transparent", None) in win.log
        assert any("mic.setBackdrop('acrylic')" in js for js in rig.js(win))
        assert (win.hwnd, 38, 3) in rig.w32.dwm  # DWMWA_SYSTEMBACKDROP_TYPE = acrilico
    pill_dark = [v for (h, a, v) in rig.w32.dwm if h == rig.pill.hwnd and a == 20]
    assert pill_dark == [1]  # la pastilla siempre oscura


def test_acrylic_falls_back_on_windows_10() -> None:
    rig = Rig()
    rig.w32.build = 19045
    _boot(rig)
    host = rig.host
    assert host is not None and not host.acrylic
    assert not any("setBackdrop" in js for js in rig.js(rig.popup) + rig.js(rig.pill))
    assert not [c for c in rig.w32.dwm if c[1] == 38]


def test_acrylic_can_be_disabled_by_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MICNAGA_UI_ACRYLIC", "0")
    rig = Rig()
    _boot(rig)
    assert rig.host is not None and not rig.host.acrylic


def test_acrylic_fly_animates_size_to_the_exact_pill_rect() -> None:
    rig = Rig()
    _boot(rig)
    host = rig.host
    assert host is not None
    host.show_popup("x")
    host.handle_popup_decide("record")
    host.handle_popup_morph(False)
    final = rig.w32.places(rig.popup.hwnd)[-1]
    assert final == host._pill_target(host._popup_mon)  # type: ignore[arg-type]
    assert final[2:] != host._popup_rect[2:]  # type: ignore[index]  # cambia el tamano


def test_popup_state_is_not_authoritative_without_a_provider() -> None:
    from tests.ui.test_host import Rig as UiRig

    ui = UiRig()
    assert ui.host.popup_state()["authoritative"] is False
    rig = Rig()
    assert rig.app.ui.popup_state()["authoritative"] is True  # type: ignore[attr-defined]
