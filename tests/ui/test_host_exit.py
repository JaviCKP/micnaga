"""La app solo sale por «Salir», el fin de sesion de Windows o el instalador.

Si el bucle de la GUI termina por otra cosa (ventanas cerradas, Ctrl+C de la consola que llega al
manejador de SIGINT de pywebview...) se registra el motivo y se recrean las ventanas.
"""

from __future__ import annotations

import logging
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from micnaga import app as app_mod
from micnaga.ui import host as host_mod
from micnaga.ui import winapi
from micnaga.ui.windows import FakeFactory
from tests.ui.test_host import Rig


class LoopFactory(FakeFactory):
    """``start`` ejecuta el arranque y luego ``script[n]`` (lo que pasa en esa vuelta del bucle);
    al volver, el bucle de la GUI ha terminado."""

    def __init__(self, script: list[Callable[[], None]]) -> None:
        super().__init__()
        self.script = script
        self.loops = 0

    def start(self, on_started: Callable[[], None], *, icon: Path | None) -> None:
        on_started()
        step = self.script[min(self.loops, len(self.script) - 1)]
        self.loops += 1
        step()


def _rig(
    monkeypatch: pytest.MonkeyPatch, script: list[Callable[[], None]]
) -> tuple[Rig, LoopFactory]:
    monkeypatch.setattr(winapi, "ensure_dpi_aware", lambda: None)
    r = Rig()
    fac = LoopFactory(script)
    r.fac = fac
    r.host._factory = fac
    r.host._want_tray = False
    monkeypatch.setattr(r.host, "_session_ending", lambda: False)
    return r, fac


def test_loop_ending_without_quit_recreates_the_windows_and_logs_why(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    holder: list[Rig] = []
    script: list[Callable[[], None]] = [
        lambda: None,  # el bucle termina solo (p. ej. se cerraron las ventanas ocultas)
        lambda: holder[0].host.quit("tray_quit"),
    ]
    r, fac = _rig(monkeypatch, script)
    holder.append(r)
    ready: list[object] = []
    with caplog.at_level(logging.INFO, logger="micnaga.ui.host"):
        r.host.run(ready.append)
    assert fac.loops == 2
    assert ready == [r.host], "on_ready (arrancar la app) solo una vez"
    assert [s.key for s in fac.created].count("pill") == 2
    assert r.host.exit_reason == "tray_quit"
    assert "recreating the windows" in caplog.text
    assert "GUI loop ended: tray_quit" in caplog.text


def test_quit_exits_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    holder: list[Rig] = []
    r, fac = _rig(monkeypatch, [lambda: holder[0].host.quit("exit_after")])
    holder.append(r)
    r.host.run()
    assert fac.loops == 1 and r.host.exit_reason == "exit_after"


def test_console_sigint_flag_is_reported_and_reset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    winforms = types.ModuleType("webview.platforms.winforms")
    winforms._sigint_received = False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webview.platforms.winforms", winforms)
    holder: list[Rig] = []

    def sigint() -> None:
        winforms._sigint_received = True  # type: ignore[attr-defined]

    r, fac = _rig(monkeypatch, [sigint, lambda: holder[0].host.quit("tray_quit")])
    holder.append(r)
    with caplog.at_level(logging.INFO, logger="micnaga.ui.host"):
        r.host.run()
    assert fac.loops == 2
    assert "SIGINT" in caplog.text
    assert winforms._sigint_received is False, "si no, el bucle nuevo saldria al instante"


def test_closed_overlay_is_named_in_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    holder: list[Rig] = []
    r, _fac = _rig(
        monkeypatch,
        [lambda: holder[0].fac.windows["pill"].destroy(), lambda: holder[0].host.quit()],
    )
    holder.append(r)
    with caplog.at_level(logging.INFO, logger="micnaga.ui.host"):
        r.host.run()
    assert "overlay window 'pill' was closed" in caplog.text


def test_end_of_windows_session_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    r, fac = _rig(monkeypatch, [lambda: None])
    monkeypatch.setattr(r.host, "_session_ending", lambda: True)
    r.host.run()
    assert fac.loops == 1 and r.host.exit_reason.startswith("session_end")


def test_gives_up_after_repeated_loop_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    r, fac = _rig(monkeypatch, [lambda: None])
    r.host.run()
    assert fac.loops == host_mod.MAX_GUI_RESTARTS + 1
    assert r.host.exit_reason.startswith("gui_loop_failed")


def test_recording_pill_and_open_popup_survive_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    holder: list[Rig] = []

    def first_loop() -> None:
        h = holder[0].host
        h.show_pill(elapsed_s=42.0)
        h.show_popup("Reunión", "abc-defg-hij")

    r, _fac = _rig(monkeypatch, [first_loop, lambda: holder[0].host.quit()])
    holder.append(r)
    seen: dict[str, Any] = {}
    real_restore = r.host._restore_surfaces

    def spy() -> None:
        real_restore()
        seen["pill"] = r.fac.windows["pill"].visible
        seen["popup"] = r.host.popup_visible
        seen["popup_js"] = r.fac.windows["pill"].js_calls()

    monkeypatch.setattr(r.host, "_restore_surfaces", spy)
    r.host.run()
    assert seen["pill"] is True
    assert seen["popup"] is True
    assert any(js.startswith("micnaga.popup.show(") for js in seen["popup_js"])


# ------------------------------------------------------------------- consola (app.py)


def test_console_ctrl_c_is_ignored_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    exits: list[str] = []
    guard = app_mod.ConsoleGuard(exits.append)
    with caplog.at_level(logging.INFO, logger="micnaga.app"):
        assert guard.handle(app_mod.CTRL_C_EVENT) is True
        assert guard.handle(app_mod.CTRL_BREAK_EVENT) is True
    assert exits == []
    assert "ignored console_ctrl_c" in caplog.text


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (app_mod.CTRL_CLOSE_EVENT, "console_close"),
        (app_mod.CTRL_LOGOFF_EVENT, "session_logoff"),
        (app_mod.CTRL_SHUTDOWN_EVENT, "system_shutdown"),
    ],
)
def test_console_close_logoff_shutdown_quit_with_reason(event: int, reason: str) -> None:
    exits: list[str] = []
    guard = app_mod.ConsoleGuard(exits.append)
    assert guard.handle(event) is True
    assert exits == [reason]


class _TrayStub:
    def __init__(self) -> None:
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1


def test_tray_quit_hides_everything_before_the_slow_shutdown() -> None:
    """«Salir» con una parada lenta (guardar la grabacion, esperar al worker): el icono y las
    ventanas desaparecen al momento; la GUI se cierra cuando termina la parada."""
    import threading

    release = threading.Event()
    entered = threading.Event()

    def slow_quit() -> None:
        entered.set()
        release.wait(5)

    r = Rig(on_quit=slow_quit)
    tray = _TrayStub()
    r.host._tray = tray  # type: ignore[assignment]
    r.host.open_library()
    lib = r.fac.windows["library"]
    lib.visible = True
    r.host._tray_quit()  # vuelve al momento: no bloquea el hilo de pystray
    assert tray.stopped == 1
    assert not lib.visible
    assert entered.wait(2)
    assert not r.host._stopping.is_set(), "la GUI sigue hasta que el integrador termine"
    r.host._tray_quit()  # un segundo clic no lanza otra parada
    release.set()
    for _ in range(100):
        if r.host._stopping.is_set():
            break
        threading.Event().wait(0.02)
    assert r.host._stopping.is_set()
    assert r.host._quit_reason == "tray_quit"


def test_tray_quit_still_exits_when_the_shutdown_fails() -> None:
    import threading

    def broken() -> None:
        raise RuntimeError("boom")

    r = Rig(on_quit=broken)
    r.host._tray_quit()
    for _ in range(100):
        if r.host._stopping.is_set():
            break
        threading.Event().wait(0.02)
    assert r.host._stopping.is_set()


def test_exit_watchdog_forces_the_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    exited: list[int] = []
    done = threading.Event()

    def fake_exit(code: int) -> None:
        exited.append(code)
        done.set()

    monkeypatch.setattr(app_mod.os, "_exit", fake_exit)
    monkeypatch.setattr(app_mod.logging, "shutdown", lambda: None)
    app_mod._exit_watchdog(0.05)
    assert done.wait(2) and exited == [0]
