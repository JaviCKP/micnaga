"""Backends falsos (audio sintetico) para los tests del ``Recorder``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf
from numpy.typing import NDArray

from micnaga.capture.sources import EndpointInfo, EndpointSnapshot
from micnaga.capture.synthetic import SignalFn, SyntheticBackend, SyntheticSource
from micnaga.config import CaptureConfig
from micnaga.types import CaptureMeta

F32 = NDArray[np.float32]


def tone(freq: float, rate: int, amp: float = 0.3) -> SignalFn:
    def fn(start: int, n: int) -> F32:
        t = (np.arange(start, start + n) / rate).astype(np.float64)
        return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)

    return fn


def tone_with_sentinel(
    freq: float,
    rate: int,
    window_s: tuple[float, float],
    *,
    amp: float = 0.3,
    sentinel: float = 0.9,
) -> SignalFn:
    """Tono que vale ``sentinel`` (constante) en ``window_s`` (tiempo de la fuente)."""
    base = tone(freq, rate, amp)

    def fn(start: int, n: int) -> F32:
        out = base(start, n)
        t = np.arange(start, start + n) / rate
        out[(t >= window_s[0]) & (t < window_s[1])] = sentinel
        return out

    return fn


def bursts(
    times_s: list[float], rate: int, *, drift_ppm: float = 0.0, freq: float = 2000.0
) -> SignalFn:
    """Ráfagas gaussianas de ``freq`` Hz centradas en ``times_s`` (tiempo de reloj comun).

    La tarjeta con ``drift_ppm`` > 0 va lenta: la muestra ``i`` esta en ``i / rate * (1 + ppm)``.
    """
    scale = 1.0 + drift_ppm * 1e-6

    def fn(start: int, n: int) -> F32:
        t = np.arange(start, start + n) / rate * scale
        out = np.zeros(n, dtype=np.float64)
        for c in times_s:
            env = np.exp(-0.5 * ((t - c) / 0.004) ** 2)
            out += env * np.sin(2 * np.pi * freq * (t - c))
        return (0.6 * out).astype(np.float32)

    return fn


class FakeDevices:
    """Estado mutable de los dispositivos por defecto, para simular cambios."""

    def __init__(
        self,
        render: EndpointInfo | None = None,
        capture: EndpointInfo | None = None,
    ) -> None:
        self.render = render or EndpointInfo("render-A", "Speakers (fake)", "speakers")
        self.capture: EndpointInfo | None = capture or EndpointInfo("cap-A", "Mic (fake)")

    def snapshot(self) -> EndpointSnapshot:
        return EndpointSnapshot(render=self.render, capture=self.capture)


def make_backend(
    devices: FakeDevices,
    mic: Callable[[EndpointInfo], SyntheticSource],
    system: Callable[[EndpointInfo], SyntheticSource],
) -> SyntheticBackend:
    def mic_factory() -> SyntheticSource:
        assert devices.capture is not None
        return mic(devices.capture)

    return SyntheticBackend(mic_factory, lambda: system(devices.render), devices.snapshot)


def cfg(**kw: float) -> CaptureConfig:
    base = {"sample_rate": 16000, "chunk_flush_s": 0.5, "sync_log_interval_s": 1.0}
    base.update(kw)
    return CaptureConfig(**base)  # type: ignore[arg-type]


def read_track(path: Path) -> F32:
    data, rate = sf.read(str(path), dtype="float32")
    assert rate == 16000
    return np.asarray(data, dtype=np.float32)


def meta_of(session: Path) -> CaptureMeta:
    return CaptureMeta.from_json((session / "capture.json").read_text(encoding="utf-8"))
