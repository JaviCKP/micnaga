"""Utilidades comunes de los motores ASR (remuestreo y troceado defensivo)."""

from __future__ import annotations

import numpy as np

from micnaga.types import AudioArray

MODEL_SR = 16000
MIN_INPUT_S = 0.3
"""Duracion minima que se entrega a un modelo (se rellena con ceros por debajo)."""
MAX_INPUT_S = 40.0
"""Por encima de esto se trocea (el pipeline normalmente entrega bloques de <= 30 s)."""
TARGET_CHUNK_S = 30.0


def to_mono_16k(audio: AudioArray, sr: int) -> AudioArray:
    """Audio float32 mono a 16 kHz (acepta (n,) o (n, canales))."""
    x = np.asarray(audio, dtype=np.float32)
    if x.ndim == 2:
        x = x.mean(axis=1, dtype=np.float32)
    elif x.ndim != 1:
        raise ValueError("audio must be 1-D (mono) or 2-D (samples, channels)")
    if sr != MODEL_SR:
        import soxr

        x = np.asarray(soxr.resample(x, sr, MODEL_SR), dtype=np.float32)
    return np.ascontiguousarray(x)


def split_points(
    audio: AudioArray,
    *,
    max_s: float = MAX_INPUT_S,
    target_s: float = TARGET_CHUNK_S,
    sr: int = MODEL_SR,
    search_s: float = 5.0,
) -> list[tuple[int, int]]:
    """Trozos ``(inicio, fin)`` en muestras de <= ``target_s``, cortados en el punto mas silencioso.

    Si el audio dura <= ``max_s`` se devuelve un solo trozo.
    """
    n = len(audio)
    if n <= max_s * sr:
        return [(0, n)]
    win = int(0.05 * sr)
    out: list[tuple[int, int]] = []
    start = 0
    while n - start > target_s * sr:
        lo = start + int((target_s - search_s) * sr)
        hi = start + int(target_s * sr)
        seg = audio[lo:hi]
        m = len(seg) // win
        if m > 0:
            energy = np.square(seg[: m * win]).reshape(m, win).mean(axis=1)
            cut = lo + int(np.argmin(energy)) * win + win // 2
        else:
            cut = hi
        out.append((start, cut))
        start = cut
    out.append((start, n))
    return out


def pad_min(audio: AudioArray, sr: int = MODEL_SR, min_s: float = MIN_INPUT_S) -> AudioArray:
    """Rellena con ceros hasta ``min_s`` (los modelos fallan con audio de pocos ms)."""
    need = int(min_s * sr)
    if len(audio) >= need:
        return audio
    out = np.zeros(need, dtype=np.float32)
    out[: len(audio)] = audio
    return out
