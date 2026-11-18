"""Monitor de niveles para el asistente (paso 3): abre micro y sistema solo para medir.

No escribe nada a disco. ``start()`` abre las fuentes (``backend.open_mic/open_system``) y un
hilo por fuente lee paquetes y calcula el RMS; ``levels()`` da ``(micro, sistema)`` en 0..1.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from micnaga.capture.sources import CaptureBackend

log = logging.getLogger(__name__)


class LevelMonitor:
    def __init__(self, backend_factory: Callable[[], CaptureBackend] | None = None) -> None:
        self._factory = backend_factory or _wasapi
        self._lock = threading.Lock()
        self._levels = [0.0, 0.0]
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._backend: CaptureBackend | None = None
        self._sources: list[Any] = []

    @property
    def active(self) -> bool:
        return bool(self._threads)

    def levels(self) -> tuple[float, float]:
        with self._lock:
            return self._levels[0], self._levels[1]

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        try:
            backend = self._backend = self._factory()
        except Exception:
            log.exception("level monitor: backend unavailable")
            return
        for idx, opener in enumerate((backend.open_mic, backend.open_system)):
            try:
                src = opener()
            except Exception:
                log.warning("level monitor: source %d unavailable", idx, exc_info=True)
                continue
            self._sources.append(src)
            t = threading.Thread(
                target=self._loop, args=(idx, src), name=f"micnaga-levels-{idx}", daemon=True
            )
            self._threads.append(t)
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(2.0)
        self._threads.clear()
        for src in self._sources:
            with contextlib.suppress(Exception):
                src.close()
        self._sources.clear()
        if self._backend is not None:
            with contextlib.suppress(Exception):
                self._backend.close()
            self._backend = None
        with self._lock:
            self._levels = [0.0, 0.0]

    def _loop(self, idx: int, src: Any) -> None:
        import numpy as np

        while not self._stop.is_set():
            try:
                packet = src.read(0.1)
            except Exception:
                log.debug("level monitor read failed", exc_info=True)
                return
            level = 0.0
            if packet is not None:
                x = np.asarray(packet.frames, dtype=np.float32)
                if x.size:
                    level = min(1.0, float(np.sqrt(np.mean(np.square(x)))) * 3.0)
            with self._lock:
                self._levels[idx] = level


def _wasapi() -> CaptureBackend:
    from micnaga.capture.wasapi import WasapiBackend

    return WasapiBackend()
