"""Fuentes sinteticas: audio generado, sin dispositivos reales.

Sirven para los tests (``tests/capture``), para ``micnaga record --synthetic`` y para medir el
coste de CPU/RAM del grabador sin depender de WASAPI (``measure.py``). Las fuentes se pacen en
tiempo real y sellan cada paquete con su instante **programado** (sin jitter), como un
dispositivo ideal; ``drift_ppm`` simula un reloj de tarjeta de sonido lento (> 0) o rapido (< 0)
y ``gaps`` simula el silencio del loopback (no se entrega ningun paquete).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence

import numpy as np
from numpy.typing import NDArray

from micnaga.capture.sources import (
    AudioSource,
    CaptureBackend,
    EndpointInfo,
    EndpointSnapshot,
    Packet,
)

F32 = NDArray[np.float32]
SignalFn = Callable[[int, int], F32]
"""``(muestra_inicial, n) -> mono float32`` a la frecuencia nativa de la fuente."""

NS = 1_000_000_000


def demo_signal(rate: int, *, f0: float = 120.0, seed: int = 0, seconds: float = 8.0) -> SignalFn:
    """Senal tipo voz (armonicos con envolvente silabica) que se repite cada ``seconds``."""
    rng = np.random.default_rng(seed)
    t = np.arange(round(rate * seconds)) / rate
    sig = np.zeros_like(t)
    for k in range(1, 12):
        sig += (1.0 / k) * np.sin(2 * np.pi * f0 * k * t + rng.uniform(0, 2 * np.pi))
    env = 0.5 * (1 + np.sin(2 * np.pi * 3.0 * t + rng.uniform(0, 2 * np.pi))) ** 0.7
    sig *= env
    sig *= 0.3 / max(float(np.max(np.abs(sig))), 1e-9)
    base = sig.astype(np.float32)

    def fn(start: int, n: int) -> F32:
        idx = (np.arange(start, start + n) % len(base)).astype(np.int64)
        return base[idx]

    return fn


class SyntheticSource:
    """Dispositivo ideal pacido en tiempo real (implementa ``AudioSource``)."""

    def __init__(
        self,
        info: EndpointInfo,
        signal: SignalFn,
        *,
        sample_rate: int = 48000,
        channels: int = 2,
        packet_s: float = 0.1,
        drift_ppm: float = 0.0,
        gaps: Sequence[tuple[float, float]] = (),
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self.info = info
        self.sample_rate = sample_rate
        self.channels = channels
        self._signal = signal
        self._n = max(1, round(packet_s * sample_rate))
        self._period_ns = self._n * NS / sample_rate * (1 + drift_ppm * 1e-6)
        self._gaps = list(gaps)
        self._clock = clock_ns
        self._t0 = 0
        self._k = 0
        self._closed = threading.Event()
        self.opened_at_ns = 0
        self.delivered = 0
        """Muestras entregadas (para que los tests sepan cuanto se alimento)."""

    def open(self) -> None:
        self._t0 = self.opened_at_ns = self._clock()
        self._k = 0

    def _in_gap(self, arrival_ns: int) -> bool:
        mid = (arrival_ns - self._t0 - self._period_ns / 2) / NS
        return any(a <= mid < b for a, b in self._gaps)

    def read(self, timeout: float) -> Packet | None:
        if self._closed.is_set():
            return None
        deadline = self._clock() + int(timeout * NS)
        while True:
            arrival = self._t0 + round((self._k + 1) * self._period_ns)
            if self._in_gap(arrival):
                self._k += 1
                continue
            if arrival > deadline:
                wait = max(deadline - self._clock(), 0) / NS
                if wait > 0:
                    self._closed.wait(wait)
                return None
            wait = (arrival - self._clock()) / NS
            if wait > 0 and self._closed.wait(wait):
                return None
            mono = self._signal(self._k * self._n, self._n)
            frames = mono if self.channels == 1 else np.repeat(mono[:, None], self.channels, 1)
            self._k += 1
            self.delivered += self._n
            return Packet(frames=np.ascontiguousarray(frames), arrival_ns=arrival)

    def close(self) -> None:
        self._closed.set()


class SyntheticBackend:
    """``CaptureBackend`` con fuentes fabricadas por funciones; ``probe`` configurable."""

    def __init__(
        self,
        mic: Callable[[], AudioSource],
        system: Callable[[], AudioSource],
        probe: Callable[[], EndpointSnapshot | None] | None = None,
    ) -> None:
        self._mic = mic
        self._system = system
        self._probe = probe
        self.sources: list[AudioSource] = []
        self.closed = False

    def open_mic(self) -> AudioSource:
        src = self._mic()
        src.open()
        self.sources.append(src)
        return src

    def open_system(self) -> AudioSource:
        src = self._system()
        src.open()
        self.sources.append(src)
        return src

    def probe(self) -> EndpointSnapshot | None:
        return self._probe() if self._probe is not None else None

    def close(self) -> None:
        self.closed = True


def demo_backend(*, system_gap_s: float = 0.0) -> CaptureBackend:
    """Micro y sistema sinteticos (48 kHz estereo float32, voces distintas, turnos alternos)."""
    mic = SyntheticSource(
        EndpointInfo("synthetic-mic", "Synthetic microphone"),
        demo_signal(48000, f0=110.0, seed=1),
    )
    system = SyntheticSource(
        EndpointInfo("synthetic-out", "Synthetic speakers", "speakers"),
        demo_signal(48000, f0=190.0, seed=2),
        gaps=[(5.0, 5.0 + system_gap_s)] if system_gap_s > 0 else (),
    )
    return SyntheticBackend(lambda: mic, lambda: system)
