"""Linea de tiempo comun de una sesion: reloj QPC, pausas privadas y conversion a tiempo de audio.

Todo el grabador habla en dos tiempos:

* **reloj comun** (``ns``): ``time.perf_counter_ns`` (QueryPerformanceCounter en Windows).
* **tiempo de grabacion** (``rt``, segundos): reloj comun desde el inicio **sin contar las pausas
  privadas**. Es la linea de tiempo del audio en disco, de ``Marker.t`` y de
  ``PrivatePause.at_s``.

Una pausa privada ``[p, r)`` descarta el audio de ese intervalo. Para que no se cuele nada que
se capturo durante la pausa pero llega despues (latencia del dispositivo), al reanudar se
descarta ademas ``guard_s`` (por defecto 0,3 s): la pausa efectiva termina en ``r + guard``.
Es seguro llamar a todo desde cualquier hilo.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

NS = 1_000_000_000
DEFAULT_PAUSE_GUARD_S = 0.3


@dataclass
class _Pause:
    start_ns: int
    end_ns: int | None = None
    """``None`` mientras la pausa sigue abierta."""


class Timeline:
    """Reloj comun + intervalos de pausa. Los tiempos de entrada son ``ns`` del reloj comun."""

    def __init__(
        self,
        clock_ns: Callable[[], int],
        *,
        t0_ns: int | None = None,
        pause_guard_s: float = DEFAULT_PAUSE_GUARD_S,
    ) -> None:
        self._clock = clock_ns
        self.t0_ns = clock_ns() if t0_ns is None else t0_ns
        self._guard_ns = int(pause_guard_s * NS)
        self._pauses: list[_Pause] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ reloj

    def now_ns(self) -> int:
        return self._clock()

    def rt(self, ns: int) -> float:
        """Tiempo de grabacion (s) en el instante ``ns``; se congela durante una pausa."""
        with self._lock:
            return self._rt_locked(ns)

    def _rt_locked(self, ns: int) -> float:
        paused = 0
        for p in self._pauses:
            if ns <= p.start_ns:
                break
            end = ns if p.end_ns is None else min(p.end_ns, ns)
            paused += max(end - p.start_ns, 0)
        return (ns - self.t0_ns - paused) / NS

    def elapsed_s(self) -> float:
        return self.rt(self.now_ns())

    # ----------------------------------------------------------------- pausas

    @property
    def paused(self) -> bool:
        with self._lock:
            return bool(self._pauses) and self._pauses[-1].end_ns is None

    def pause(self, ns: int | None = None) -> bool:
        """Abre una pausa. ``False`` si ya estaba en pausa."""
        ns = self.now_ns() if ns is None else ns
        with self._lock:
            if self._pauses:
                last = self._pauses[-1]
                if last.end_ns is None:
                    return False
                if ns < last.end_ns:  # re-pausa dentro del margen de guarda: se fusionan
                    last.end_ns = None
                    return True
            self._pauses.append(_Pause(start_ns=ns))
            return True

    def resume(self, ns: int | None = None) -> tuple[float, float] | None:
        """Cierra la pausa; devuelve ``(at_s, wall_s)`` de la pausa efectiva o ``None``."""
        ns = self.now_ns() if ns is None else ns
        with self._lock:
            if not self._pauses or self._pauses[-1].end_ns is not None:
                return None
            p = self._pauses[-1]
            p.end_ns = max(ns, p.start_ns) + self._guard_ns
            at_s = self._rt_locked(p.start_ns)
            return at_s, (p.end_ns - p.start_ns) / NS

    def close(self, ns: int | None = None) -> tuple[float, float] | None:
        """Cierra una pausa abierta al parar (sin margen de guarda)."""
        ns = self.now_ns() if ns is None else ns
        with self._lock:
            if not self._pauses or self._pauses[-1].end_ns is not None:
                return None
            p = self._pauses[-1]
            p.end_ns = max(ns, p.start_ns)
            return self._rt_locked(p.start_ns), (p.end_ns - p.start_ns) / NS

    def pause_records(self) -> list[tuple[float, float]]:
        """``(at_s, wall_s)`` de cada pausa (la abierta cuenta hasta ahora)."""
        now = self.now_ns()
        with self._lock:
            return [
                (
                    self._rt_locked(p.start_ns),
                    ((now if p.end_ns is None else p.end_ns) - p.start_ns) / NS,
                )
                for p in self._pauses
            ]

    def open_pause_start_rt(self) -> float | None:
        with self._lock:
            if self._pauses and self._pauses[-1].end_ns is None:
                return self._rt_locked(self._pauses[-1].start_ns)
        return None

    # --------------------------------------------------------------- segmentos

    def segment_at(self, ns: int) -> int | None:
        """Indice del tramo activo en ``ns`` (0 = antes de la 1.ª pausa) o ``None`` si en pausa."""
        with self._lock:
            return self._segment_locked(ns)

    def _segment_locked(self, ns: int) -> int | None:
        seg = 0
        for p in self._pauses:
            if ns < p.start_ns:
                break
            if p.end_ns is None or ns < p.end_ns:
                return None
            seg += 1
        return seg

    def segment_end_rt(self, seg: int) -> float | None:
        """``rt`` en que termino el tramo ``seg`` (inicio de su pausa); ``None`` si abierto."""
        with self._lock:
            if seg < len(self._pauses):
                return self._rt_locked(self._pauses[seg].start_ns)
        return None

    def slices(self, start_ns: int, end_ns: int) -> list[tuple[int, int, int]]:
        """Trozos activos de ``[start_ns, end_ns]`` como ``(ini, fin, tramo)`` (sin pausas)."""
        with self._lock:
            out: list[tuple[int, int, int]] = []
            cur = start_ns
            seg = 0
            for p in self._pauses:
                p_end = p.end_ns
                if p_end is not None and p_end <= cur:
                    seg += 1
                    continue
                if p.start_ns >= end_ns:
                    break
                if p.start_ns > cur:
                    out.append((cur, p.start_ns, seg))
                if p_end is None:
                    return out
                cur = max(cur, p_end)
                seg += 1
                if cur >= end_ns:
                    return out
            if cur < end_ns:
                out.append((cur, end_ns, seg))
            return out
