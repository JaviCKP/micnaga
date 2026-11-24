"""Procesos de transcripcion en vivo (``micnaga live <carpeta>``) de la sesion que se graba.

La app lanza uno al empezar a grabar (provisional o confirmada) y lo para al terminar: el
proceso va aparte (un fallo no toca la captura), con prioridad baja y pocos hilos, y cede si el
equipo esta ocupado (``pipeline.live.run_live``). Reglas:

* «No» / «Descartar» (``begin_discard``): se le pide parar al instante (no se espera). Si se
  deshace, se relanza y sigue donde lo dejo (lo calculado esta en ``work/live/``).
* Al parar la grabacion (``stop``): se le pide parar y se espera poco (``STOP_WAIT_S``) a que
  salga antes de convertir a FLAC; si no sale a tiempo se termina (sus escrituras son atomicas:
  nunca deja un resultado a medias). Lo que falte lo hace el worker al procesar el trabajo.
* Si se confirma la grabacion y el ASR ya esta cargado, espera al cierre de los FLAC y usa
  ese mismo presupuesto de parada para guardar bloques pendientes con el modelo caliente.
* Al apartar en «Descartadas» se borra ``work/live/`` (``holding.move_to_held``).

Todo es seguro desde cualquier hilo y ``start`` nunca bloquea mas que el ``Popen``.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from micnaga.config import Config

log = logging.getLogger(__name__)

STOP_WAIT_S = 5.0
"""El vivo solo mira la orden de parar entre bloques y un bloque de Whisper con 2 hilos tarda
20-30 s: esperarlo retrasaba cada «colgar» hasta 20 s (medido). Matarlo pierde a lo sumo ese
bloque (escrituras atomicas), que el worker rehace en pocos segundos con todos sus hilos."""


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
        return self.disabled_reason() is None

    def disabled_reason(self) -> str | None:
        """Por que no se transcribe en vivo (``None``: si se hace)."""
        try:
            config = self._config()
            if not config.processing.live:
                return "disabled in settings (processing.live = false)"
            from micnaga.pipeline.live import LIVE_SR, live_supported

            if not live_supported(config):
                return f"sample rate is not {LIVE_SR} Hz"
            if not self._models_ready():
                return "models are not downloaded yet"
        except Exception:
            log.exception("could not decide whether to transcribe live")
            return "error deciding"
        return None

    def start(self, session: Path) -> None:
        why = self.disabled_reason()
        if why is not None:
            log.info("live processing not started: %s", why)
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
            if proc is None:
                log.info("live processing not started: no process")
                return
            self._procs[session] = proc
        log.info("live processing started (pid %s)", getattr(proc, "pid", "?"))

    def signal_stop(self, session: Path) -> None:
        """Pide parar sin esperar (p. ej. al pulsar «No»)."""
        from micnaga.pipeline import live

        if session in self._procs:
            live.request_stop(session)

    def prepare_finish(self, session: Path) -> bool:
        """Retiene un ASR caliente durante el cierre del audio de una grabacion confirmada."""
        from micnaga.pipeline import live

        with self._lock:
            proc = self._procs.get(session)
            folder = live.live_dir(session)
            if (
                proc is None
                or proc.poll() is not None
                or not self._config().processing.live
                or not (folder / live.ASR_ENGINE_FILE).is_file()
            ):
                return False
            (folder / live.FINISH_REQUEST).touch()
            return True

    def finish(self, session: Path, *, audio_finalized: bool) -> None:
        """Solo entrega al vivo audio final y consistente, y lo recoge antes de encolar."""
        from micnaga.pipeline import live
        from micnaga.pipeline.stages.base import atomic_write_json

        config = self._config()
        if not audio_finalized or not config.processing.live:
            self.stop(session)
            return
        try:
            atomic_write_json(
                live.live_dir(session) / live.FINISH_READY, live.fingerprint(config)
            )
        except Exception:
            log.warning("could not publish warm ASR handoff", exc_info=True)
            self.stop(session)
            return
        self._wait(session, self._stop_wait_s)

    def stop(self, session: Path, *, wait_s: float | None = None) -> None:
        """Pide parar y espera a que salga (lo termina si no sale a tiempo)."""
        self.signal_stop(session)
        self._wait(session, self._stop_wait_s if wait_s is None else wait_s)

    def _wait(self, session: Path, timeout: float) -> None:
        with self._lock:
            proc = self._procs.get(session)
        if proc is None:
            return
        early = proc.poll()
        if early is not None:
            # salio antes de colgar: su motivo esta en live.log (codigo != 0 = fallo)
            log.info("live processing had already exited (code %s); see live.log", early)
        else:
            t0 = time.monotonic()
            try:
                code = proc.wait(timeout=timeout)
                log.info(
                    "live processing stopped in %.1f s (code %s)", time.monotonic() - t0, code
                )
            except Exception:
                # Sus escrituras son atomicas; el worker retoma los bloques completados.
                log.info("live processing did not stop in %.0f s; terminating it", timeout)
                self.signal_stop(session)
                with contextlib.suppress(Exception):
                    proc.kill()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=5.0)
        with self._lock:
            if self._procs.get(session) is proc and proc.poll() is not None:
                del self._procs[session]

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

    def prepare_finish(self, session: Path) -> bool:
        return False

    def finish(self, session: Path, *, audio_finalized: bool) -> None:
        pass

    def stop(self, session: Path, *, wait_s: float | None = None) -> None:
        pass

    def stop_all(self, *, wait_s: float | None = None) -> None:
        pass


LivePort = LiveRunner | NoLive | Any

__all__ = ["STOP_WAIT_S", "LivePort", "LiveRunner", "NoLive"]
