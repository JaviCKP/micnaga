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
        return self.fac.windows["popup"]

    @property
    def pill(self) -> FakeWindow:
        return self.fac.windows["pill"]


def payload(win: FakeWindow, fn: str) -> Any:
    for js in win.js_calls():
        m = re.match(rf"{re.escape(fn)}\((.*)\)$", js)
        if m:
            return json.loads(m.group(1))
    raise AssertionError(f"{fn} not called: {win.js_calls()}")


def test_run_creates_hidden_overlays_applies_noactivate_and_calls_on_ready() -> None:
    r = Rig()
    ready: list[UiHost] = []
    r.host._want_tray = False
    r.host._factory.start(lambda: r.host._bootstrap(ready.append), icon=None)  # type: ignore[arg-type]
    assert ready == [r.host]
    for key in ("popup", "pill"):
        spec = r.fac.windows[key].spec
        assert (
            spec.transparent and spec.frameless and spec.on_top and spec.hidden and not spec.focus
        )
        assert spec.min_size[1] <= 40, "min_size por defecto (200x100) deformaria la pastilla"
        style = r.w32.styles[r.fac.windows[key].hwnd]
        assert style & winapi.WS_EX_NOACTIVATE and style & winapi.WS_EX_TOOLWINDOW
    assert (r.fac.windows["popup"].spec.width, r.fac.windows["popup"].spec.height) == (372, 112)
    assert (r.fac.windows["pill"].spec.width, r.fac.windows["pill"].spec.height) == (200, 40)
    r.host._stopping.set()


def test_show_popup_places_top_right_in_physical_pixels_and_shows_without_activation() -> None:
    r = Rig()
    r.host.show_popup("Revisión semanal", "abc-defg-hij", monitor_hint=(10, 10, 900, 700))
    assert ("monitor_info", ((10, 10, 900, 700),)) in r.w32.calls
    assert r.popup.current_rect == (1920 - 465 - 30, 30, 465, 140)  # 372x112 a 125 %
    p = payload(r.popup, "micnaga.popup.show")
    assert p == {
        "title": "Revisión semanal",
        "code": "abc-defg-hij",
        "timeoutS": 15.0,
        "onTimeout": "provisional",
        "remindNotice": True,
    }
    kinds = [k for k, _ in r.popup.log]
    assert kinds.index("js") < kinds.index("show")  # estado oculto antes de mostrarla
    assert ("show", False) in r.popup.log  # activate=False
    assert r.host.popup_visible


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


def test_record_flies_to_pill_then_shows_it_and_hides_popup() -> None:
    r = Rig()
    r.host.show_popup("x")
    r.host.handle_popup_decide("record")
    r.host.handle_popup_morph(False)  # spawn inline: el vuelo ocurre aqui
    popup_rect = r.w32.places(r.popup.hwnd)[-1]  # el vuelo mueve el hwnd por Win32
    # el popup acaba con su tarjeta (centrada arriba) sobre el hueco de la pastilla
    pill_x = (1920 - 250) // 2
    assert popup_rect == (pill_x - (465 - 250) // 2, 15, 465, 140)
    assert r.pill.current_rect == (pill_x, 15, 250, 50)
    p = payload(r.pill, "micnaga.pill.show")
    assert p["provisional"] is False and p["paused"] is False
    assert ("show", False) in r.pill.log
    r.host.handle_popup_closed("morphed")
    assert ("hide", None) in r.popup.log and not r.host.popup_visible
    assert ("decision", "record") in r.events and ("closed", "morphed") in r.events
    assert r.host.tray_state() == "recording"


def test_timeout_becomes_a_provisional_pill() -> None:
    r = Rig()
    r.host.show_popup("x")
    r.host.handle_popup_decide("timeout")
    r.host.handle_popup_morph(True)
    assert payload(r.pill, "micnaga.pill.show")["provisional"] is True
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


def test_show_pill_during_the_flight_is_deferred_until_landing() -> None:
    r = Rig()
    r.host.show_popup("x")
    r.host._popup_state = "morphing"
    r.host.show_pill(elapsed_s=7.5)
    assert not any(k == "show" for k, _ in r.pill.log)  # aun no
    r.host._spawn(r.host._fly_to_pill)
    p = payload(r.pill, "micnaga.pill.show")
    assert p["elapsed"] == 7.5  # manda el valor del vigilante, no el del popup


def test_flight_is_animated_when_windows_animations_are_on() -> None:
    r = Rig()
    r.w32.animation = True
    r.host.show_popup("x")
    r.host.handle_popup_morph(False)
    assert len(r.w32.places(r.popup.hwnd)) > 10  # fotogramas del vuelo de 400 ms


def test_pill_standalone_show_hide_and_pause_state() -> None:
    r = Rig()
    r.host.show_pill(elapsed_s=12.0)
    assert r.pill.current_rect[2:] == (250, 50) and r.host.pill_visible
    r.host.set_pill_paused(True)
    assert "micnaga.pill.setPaused(true)" in r.pill.js_calls()
    assert r.host.tray_state() == "paused"
    r.host.sync_pill(30.5)
    assert "micnaga.pill.sync(30.50)" in r.pill.js_calls()
    r.host.hide_pill()
    assert not r.host.pill_visible and r.host.tray_state() == "watching"
    assert "micnaga.pill.stop()" in r.pill.js_calls() and ("hide", None) in r.pill.log


def test_pill_hover_widens_the_window_symmetrically_and_restores() -> None:
    r = Rig()
    r.host.show_pill()
    x, y, w, h = r.pill.current_rect
    r.host.handle_pill_hover(True)
    wide = round(winapi.PILL_HOVER_WIDTH * 1.25)
    assert r.pill.current_rect == (x - (wide - w) // 2, y, wide, h)
    r.host.handle_pill_hover(True)  # idempotente
    r.host.handle_pill_hover(False)
    assert r.pill.current_rect == (x, y, w, h)


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
    r.pill.fire_moved(300, 200)
    assert ("moved", 300, 200) in r.events
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
    assert {s.key for s in r.fac.created} >= {"settings", "wizard"}
    assert r.fac.windows["wizard"].spec.resizable is False
    assert r.fac.windows["settings"].spec.js_api is not None


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
    assert r.fac.windows["settings"].spec.params["theme"] == "dark"
    assert r.fac.windows["settings"].spec.background == "#050507"


def test_motion_param_when_windows_animations_off() -> None:
    r = Rig()
    r.host.open_settings()
    assert r.fac.windows["settings"].spec.params.get("motion") == "off"


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
