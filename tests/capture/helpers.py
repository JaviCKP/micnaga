"""Utilidades comunes de los tests de captura."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
from numpy.typing import NDArray

from micnaga.capture.timeline import NS, Timeline
from micnaga.capture.track import Track
from micnaga.capture.wavio import ChunkedWavWriter


class ManualClock:
    """Reloj en ns controlado por el test."""

    def __init__(self, t_ns: int = 0) -> None:
        self.t = t_ns

    def __call__(self) -> int:
        return self.t

    def set_s(self, seconds: float) -> None:
        self.t = round(seconds * NS)


def s_to_ns(seconds: float) -> int:
    return round(seconds * NS)


def make_track(
    tmp: Path,
    name: str = "t",
    *,
    clock: ManualClock | None = None,
    timeline: Timeline | None = None,
    in_rate: int = 16000,
    **kwargs: float,
) -> tuple[Track, ChunkedWavWriter, Timeline, ManualClock]:
    clock = clock or ManualClock()
    tl = timeline or Timeline(clock, t0_ns=0)
    writer = ChunkedWavWriter(tmp / f"{name}.wav", 16000, fsync=False)
    track = Track(name, tl, writer, **kwargs)  # type: ignore[arg-type]
    track.set_input_format(in_rate)
    return track, writer, tl, clock


def feed_stream(
    track: Track,
    signal: NDArray[np.float32],
    rate: int,
    *,
    packet_s: float = 0.1,
    t0_s: float = 0.0,
    slow: float = 1.0,
    skip: tuple[float, float] | None = None,
    channels: int = 1,
    until_s: float | None = None,
) -> int:
    """Alimenta ``signal`` (a ``rate``) en paquetes que llegan a ``t0 + (k+1) * packet * slow``.

    ``slow > 1`` simula un reloj de tarjeta lento (mas tiempo real por paquete); ``skip`` omite los
    paquetes cuyo instante cae en ``[a, b)`` (silencio del loopback). Devuelve el ``arrival_ns``
    del ultimo paquete entregado.
    """
    n = round(packet_s * rate)
    last = 0
    k = 0
    while (k + 1) * n <= len(signal):
        arrival_s = t0_s + (k + 1) * packet_s * slow
        if until_s is not None and arrival_s > until_s:
            break
        chunk = signal[k * n : (k + 1) * n]
        k += 1
        if skip is not None and skip[0] <= arrival_s - packet_s / 2 < skip[1]:
            continue
        frames = chunk if channels == 1 else np.repeat(chunk[:, None], channels, axis=1)
        last = s_to_ns(arrival_s)
        track.feed(frames, last)
    return last


def read_wav(path: Path) -> NDArray[np.float32]:
    data, rate = sf.read(str(path), dtype="float32")
    assert rate == 16000
    return np.asarray(data, dtype=np.float32)
