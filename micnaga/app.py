"""Entrada de la aplicacion: ``micnaga app`` (bandeja, vigilante, captura, cola y ventanas).

Este modulo es deliberadamente ligero: no importa numpy, onnxruntime ni torch (el camino hasta la
bandeja debe tardar < 2 s). Las variables de hilos de BLAS se fijan **antes** de que nadie pueda
importar numpy en este proceso.

Opciones (para desarrollo y pruebas)::

    micnaga app [--exit-after SEGUNDOS] [--no-single-instance]

``--exit-after`` sale por el mismo camino que «Salir» de la bandeja.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import faulthandler
import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import IO, Any

log = logging.getLogger("micnaga.app")

ERROR_ALREADY_EXISTS = 183
EXIT_ALREADY_RUNNING = 0


def _limit_blas_threads() -> None:
    # La UI/captura no calcula con BLAS: un hilo basta y evita decenas de hilos ociosos de OpenBLAS.
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[var] = "1"


def _process_start_time() -> float | None:
    try:
        import psutil

        return float(psutil.Process().create_time())
    except Exception:
        return None


class SingleInstance:
    """Mutex con nombre (derivado de ``MICNAGA_HOME``, como el del worker y la tuberia)."""

    def __init__(self) -> None:
        from micnaga import paths

        digest = hashlib.sha1(str(paths.app_data_dir()).lower().encode("utf-8")).hexdigest()[:10]
        self.name = f"Local\\micnaga-app-{digest}"
        self._handle: int | None = None

    def acquire(self) -> bool:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, True, self.name)
        if not handle:
            return True  # sin mutex no se puede garantizar; mejor arrancar que no grabar
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS or kernel32.GetLastError() == (
            ERROR_ALREADY_EXISTS
        ):
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        self._handle = int(handle)
        return True

    def release(self) -> None:
        if self._handle is not None:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None


CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT = 0, 1, 2
CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT = 5, 6
CONSOLE_CLOSE_WAIT_S = 4.0
"""Windows mata el proceso ~5 s tras CTRL_CLOSE: se espera hasta aqui a cerrar la grabacion."""
_CONSOLE_EVENTS = {
    CTRL_C_EVENT: "console_ctrl_c",
    CTRL_BREAK_EVENT: "console_ctrl_break",
    CTRL_CLOSE_EVENT: "console_close",
    CTRL_LOGOFF_EVENT: "session_logoff",
    CTRL_SHUTDOWN_EVENT: "system_shutdown",
}


class ConsoleGuard:
    """Ctrl+C / Ctrl+Break en la consola desde la que se lanzo la app (``micnaga app`` en un
    terminal, o un Ctrl+C dirigido a OTRO programa de la misma consola, que Windows reparte a
    todos sus procesos) ya no la cierran: llegaban al manejador de SIGINT de pywebview, que
    terminaba el bucle de la GUI y la app salia «limpiamente» sin que nadie pulsara «Salir».
    Se registran y se ignoran. Cerrar la consola, cerrar sesion o apagar si salen, por el camino
    de «Salir» y con el motivo en el log."""

    def __init__(self, on_exit: Callable[[str], None]) -> None:
        self._on_exit = on_exit
        self._handler: Any = None

    def handle(self, event: int) -> bool:
        reason = _CONSOLE_EVENTS.get(event, f"console_event_{event}")
        if event in (CTRL_C_EVENT, CTRL_BREAK_EVENT):
            log.warning("ignored %s (use «Salir» in the tray to exit)", reason)
            return True
        log.info("console event %s: exiting", reason)
        done = threading.Event()

        def _quit() -> None:
            try:
                self._on_exit(reason)
            finally:
                done.set()

        threading.Thread(target=_quit, name="console-exit", daemon=True).start()
        done.wait(CONSOLE_CLOSE_WAIT_S)
        return True

    def install(self) -> bool:
        try:
            proto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
            self._handler = proto(lambda ev: 1 if self.handle(int(ev)) else 0)
            return bool(ctypes.windll.kernel32.SetConsoleCtrlHandler(self._handler, True))
        except Exception:
            log.exception("could not install the console control handler")
            return False

    def uninstall(self) -> None:
        if self._handler is not None:
            with contextlib.suppress(Exception):
                ctypes.windll.kernel32.SetConsoleCtrlHandler(self._handler, False)
            self._handler = None


def _enable_crash_traces() -> IO[str] | None:
    """Un cierre nativo (access violation en WebView2/PortAudio...) mata el proceso sin pasar
    por ``logging``: ``faulthandler`` deja la pila de cada hilo en ``logs/crash.log``."""
    try:
        from micnaga import paths

        f = open(paths.logs_dir() / "crash.log", "a", encoding="utf-8")  # noqa: SIM115
        f.write(f"--- app start {time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()}\n")
        f.flush()
        faulthandler.enable(file=f, all_threads=True)
        return f
    except Exception:
        log.exception("could not enable crash traces")
        return None


EXIT_GRACE_S = 8.0


def _exit_watchdog(grace_s: float = EXIT_GRACE_S) -> None:
    """Todo esta cerrado y guardado: si algun hilo no-daemon (una libreria, un proceso hijo
    colgado...) mantiene vivo el proceso, se sale igualmente pasados ``grace_s``. Sin esto el
    proceso podia quedarse en segundo plano tras «Salir»."""

    def _force() -> None:
        lingering = [
            t.name
            for t in threading.enumerate()
            if t is not threading.current_thread() and not t.daemon and t.is_alive()
        ]
        log.warning(
            "process still alive %.0f s after exiting (%s); forcing exit",
            grace_s,
            ", ".join(lingering) or "no threads",
        )
        logging.shutdown()
        os._exit(0)

    timer = threading.Timer(grace_s, _force)
    timer.daemon = True
    timer.start()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="micnaga app", description="Bandeja, vigilante y ventanas.")
    p.add_argument("--exit-after", type=float, default=0.0, metavar="S", help="salir tras S s")
    p.add_argument("--no-single-instance", action="store_true", help="no usar el mutex")
    p.add_argument("--open-url", metavar="URL", help="abrir una cita de Micnaga")
    return p


def run(argv: list[str]) -> int:
    """Punto de entrada de ``micnaga app``."""
    started = _process_start_time()
    _limit_blas_threads()
    parser = _parser()
    args = parser.parse_args(argv)

    from micnaga.config import ConfigError, ensure_config
    from micnaga.logging_setup import setup_logging
    from micnaga.meeting_links import parse_meeting_link, send_meeting_link

    link = parse_meeting_link(args.open_url) if args.open_url is not None else None
    if args.open_url is not None and (link is None or args.no_single_instance):
        parser.error("la cita de Micnaga no es válida")

    setup_logging("app")
    guard = SingleInstance()
    if not args.no_single_instance and not guard.acquire():
        if link is not None and send_meeting_link(link):
            return 0
        log.info("another instance is already running; exiting")
        return EXIT_ALREADY_RUNNING
    crash_log = _enable_crash_traces()
    try:
        try:
            config = ensure_config()
        except ConfigError:
            log.exception("invalid config.toml; running with defaults (file left untouched)")
            from micnaga.config import Config

            config = Config()

        from micnaga.integration.application import Application, default_ui_factory

        app = Application(
            config,
            ui_factory=default_ui_factory(config),
            process_start=started,
            exit_after_s=args.exit_after,
        )
        console = ConsoleGuard(app.request_quit)
        console.install()
        try:

            def ready(host: object) -> None:
                app.start(host)
                if link is not None:
                    app.open_meeting_link(link)

            app.ui.run(ready)
        finally:
            reason = getattr(app.ui, "exit_reason", "") or "gui_loop_ended"
            app.shutdown(reason=reason)
            console.uninstall()
        log.info(
            "exited cleanly after %.1f s (reason: %s)",
            time.time() - (started or time.time()),
            reason,
        )
        _exit_watchdog()
        return 0
    finally:
        guard.release()
        if crash_log is not None:
            with contextlib.suppress(Exception):
                faulthandler.disable()
                crash_log.close()
