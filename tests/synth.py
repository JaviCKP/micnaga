"""Audio sintetico para tests. NUNCA grabaciones reales.

Todo devuelve mono ``float32`` en [-1, 1] (la convencion de ``micnaga.types.AudioArray``).
Uso: ``from tests.synth import sine, noise, speech_like, add_echo``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import soundfile as sf

from micnaga.types import AudioArray

SR = 16000


def _n(duration: float, sr: int) -> int:
    return round(duration * sr)


def silence(duration: float, sr: int = SR) -> AudioArray:
    return np.zeros(_n(duration, sr), dtype=np.float32)


def sine(freq: float, duration: float, sr: int = SR, amp: float = 0.5) -> AudioArray:
    t = np.arange(_n(duration, sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def noise(duration: float, sr: int = SR, amp: float = 0.1, seed: int = 0) -> AudioArray:
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal(_n(duration, sr))).clip(-1, 1).astype(np.float32)


def speech_like(
    duration: float,
    sr: int = SR,
    *,
    f0: float = 120.0,
    amp: float = 0.3,
    syllable_hz: float = 4.0,
    seed: int = 0,
) -> AudioArray:
    """Pila de armonicos con envolvente silabica (~4 Hz): "suena" a voz para un VAD simple.

    Distintos ``f0``/``seed`` dan "hablantes" distintos (espectro y fase distintos).
    """
    rng = np.random.default_rng(seed)
    t = np.arange(_n(duration, sr)) / sr
    sig = np.zeros_like(t)
    for k in range(1, 15):
        sig += (1.0 / k) * np.sin(2 * np.pi * f0 * k * t + rng.uniform(0, 2 * np.pi))
    env = 0.5 * (1 + np.sin(2 * np.pi * syllable_hz * t + rng.uniform(0, 2 * np.pi))) ** 0.7
    sig *= env
    peak = float(np.max(np.abs(sig))) or 1.0
    return (amp * sig / peak).astype(np.float32)


def speech_bursts(
    spans: Sequence[tuple[float, float]],
    total: float,
    sr: int = SR,
    *,
    f0: float = 120.0,
    amp: float = 0.3,
    seed: int = 0,
) -> AudioArray:
    """Silencio de ``total`` s con "voz" solo en ``spans`` (inicio, fin) en segundos."""
    out = silence(total, sr)
    for i, (a, b) in enumerate(spans):
        i0, i1 = _n(a, sr), min(_n(b, sr), len(out))
        out[i0:i1] = speech_like((i1 - i0) / sr, sr, f0=f0, amp=amp, seed=seed + i)[: i1 - i0]
    return out


def mix(*tracks: AudioArray) -> AudioArray:
    n = max(len(t) for t in tracks)
    out = np.zeros(n, dtype=np.float32)
    for t in tracks:
        out[: len(t)] += t
    return out.clip(-1, 1)


def delay(sig: AudioArray, seconds: float, sr: int = SR) -> AudioArray:
    """Retrasa ``sig`` manteniendo la longitud."""
    k = _n(seconds, sr)
    if k <= 0:
        return sig.copy()
    out = np.zeros_like(sig)
    out[k:] = sig[: len(sig) - k]
    return out


def add_echo(
    mic: AudioArray, ref: AudioArray, *, delay_s: float = 0.04, gain: float = 0.5, sr: int = SR
) -> AudioArray:
    """Mic con el eco de ``ref`` (lo que sale por los altavoces) filtrado y retrasado."""
    echo = gain * delay(ref[: len(mic)], delay_s, sr)
    return mix(mic, echo.astype(np.float32))


def to_int16(sig: AudioArray) -> np.ndarray:
    return (sig.clip(-1, 1) * 32767).astype(np.int16)


def write_wav(path: Path, sig: AudioArray, sr: int = SR) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), sig, sr, subtype="PCM_16")
    return path
