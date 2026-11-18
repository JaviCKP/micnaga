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
import ctypes
import hashlib
import logging
import os
import time

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


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="micnaga app", description="Bandeja, vigilante y ventanas.")
    p.add_argument("--exit-after", type=float, default=0.0, metavar="S", help="salir tras S s")
    p.add_argument("--no-single-instance", action="store_true", help="no usar el mutex")
    return p


def run(argv: list[str]) -> int:
    """Punto de entrada de ``micnaga app``."""
    started = _process_start_time()
    _limit_blas_threads()
    args = _parser().parse_args(argv)

    from micnaga.config import ConfigError, ensure_config
    from micnaga.logging_setup import setup_logging

    setup_logging("app")
    guard = SingleInstance()
    if not args.no_single_instance and not guard.acquire():
        log.info("another instance is already running; exiting")
        return EXIT_ALREADY_RUNNING
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
        try:
            app.ui.run(app.start)
        finally:
            app.shutdown()
        log.info("exited cleanly after %.1f s", time.time() - (started or time.time()))
        return 0
    finally:
        guard.release()
