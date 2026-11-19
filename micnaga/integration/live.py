"""Procesos de transcripcion en vivo (``micnaga live <carpeta>``) de la sesion que se graba.

La app lanza uno al empezar a grabar (provisional o confirmada) y lo para al terminar: el
proceso va aparte (un fallo no toca la captura), con prioridad baja y pocos hilos, y cede si el
equipo esta ocupado (``pipeline.live.run_live``). Reglas:

* «No» / «Descartar» (``begin_discard``): se le pide parar al instante (no se espera). Si se
  deshace, se relanza y sigue donde lo dejo (lo calculado esta en ``work/live/``).
* Al parar la grabacion (``stop``): se le pide parar y se espera (``STOP_WAIT_S``) a que salga
  antes de convertir a FLAC; si no sale a tiempo se termina (sus escrituras son atomicas: nunca
  deja un resultado a medias). Lo que falte lo hace el worker al procesar el trabajo.
* Al apartar en «Descartadas» se borra ``work/live/`` (``holding.move_to_held``).

Todo es seguro desde cualquier hilo y ``start`` nunca bloquea mas que el ``Popen``.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from micnaga.config import Config

log = logging.getLogger(__name__)

STOP_WAIT_S = 20.0


class LiveProc(Protocol):
    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


def _default_spawn(session: Path) -> LiveProc:
    from micnaga.pipeline.worker import spawn_live

    return spawn_live(session)


class LiveRunner:
    def __init__(
        self,
        config: Callable[[], Config],
        *,
        spawn: Callable[[Path], LiveProc | None] | None = None,
        models_ready: Callable[[], bool] = lambda: True,
        stop_wait_s: float = STOP_WAIT_S,
    ) -> None:
        self._config = config
        self._spawn = spawn or _default_spawn
        self._models_ready = models_ready
        self._stop_wait_s = stop_wait_s
        self._lock = threading.Lock()
        self._procs: dict[Path, LiveProc] = {}

    def enabled(self) -> bool:
        try:
            return bool(self._config().processing.live) and self._models_ready()
        except Exception:
            log.exception("could not decide whether to transcribe live")
            return False

    def start(self, session: Path) -> None:
        if not self.enabled():
            return
        from micnaga.pipeline import live

        live.clear_stop(session)
        with self._lock:
            old = self._procs.get(session)
            if old is not None and old.poll() is None:
                return
            try:
                proc = self._spawn(session)
            except Exception:
                log.exception("could not start live processing (the worker will do it all)")
                return
            if proc is not None:
                self._procs[session] = proc
        log.info("live processing started")

    def signal_stop(self, session: Path) -> None:
        """Pide parar sin esperar (p. ej. al pulsar «No»)."""
        from micnaga.pipeline import live

        if session in self._procs:
            live.request_stop(session)

    def stop(self, session: Path, *, wait_s: float | None = None) -> None:
        """Pide parar y espera a que salga (lo termina si no sale a tiempo)."""
        from micnaga.pipeline import live

        with self._lock:
            proc = self._procs.pop(session, None)
        if proc is None:
            return
        live.request_stop(session)
        timeout = self._stop_wait_s if wait_s is None else wait_s
        try:
            proc.wait(timeout=timeout)
        except Exception:
            log.warning("live processing did not stop in %.0f s; terminating it", timeout)
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5.0)

    def stop_all(self, *, wait_s: float | None = None) -> None:
        with self._lock:
            sessions = list(self._procs)
        for s in sessions:
            self.stop(s, wait_s=wait_s)

    @property
    def running(self) -> list[Path]:
        with self._lock:
            return [s for s, p in self._procs.items() if p.poll() is None]


class NoLive:
    """Sin procesado en vivo (tests y valor por defecto del controlador)."""

    def start(self, session: Path) -> None:
        pass

    def signal_stop(self, session: Path) -> None:
        pass

    def stop(self, session: Path, *, wait_s: float | None = None) -> None:
        pass

    def stop_all(self, *, wait_s: float | None = None) -> None:
        pass


LivePort = LiveRunner | NoLive | Any

__all__ = ["STOP_WAIT_S", "LivePort", "LiveRunner", "NoLive"]
