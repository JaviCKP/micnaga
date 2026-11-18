"""Utilidades comunes de los cancelacores de eco: conversion, ajuste de longitudes y retardo."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from micnaga.types import AudioArray


def float_to_int16(x: AudioArray) -> NDArray[np.int16]:
    """float32 [-1, 1] -> int16 con redondeo y recorte."""
    return np.asarray(np.clip(np.rint(x * 32768.0), -32768, 32767), dtype=np.int16)


def int16_to_float(x: NDArray[np.int16]) -> AudioArray:
    return np.asarray(x, dtype=np.float32) / 32768.0


def match_length(ref: AudioArray, n: int) -> AudioArray:
    """Recorta o rellena con ceros ``ref`` hasta ``n`` muestras (misma longitud que el micro)."""
    if len(ref) == n:
        return ref
    if len(ref) > n:
        return ref[:n]
    out = np.zeros(n, dtype=np.float32)
    out[: len(ref)] = ref
    return out


def pad_to_multiple(x: NDArray[np.int16], frame: int) -> NDArray[np.int16]:
    """Rellena con ceros hasta un multiplo de ``frame`` (la ultima trama parcial)."""
    rem = len(x) % frame
    if rem == 0:
        return x
    return np.concatenate([x, np.zeros(frame - rem, dtype=np.int16)])


def delay_ref(ref: AudioArray, delay: int) -> AudioArray:
    """Retrasa ``ref`` ``delay`` muestras (misma longitud).

    Si el eco llega ``D`` muestras despues de la referencia, retrasar la referencia ``D - margen``
    acerca ambas y deja al filtro adaptativo solo ``margen`` + la reverberacion que cubrir.
    """
    if delay <= 0:
        return ref
    out = np.zeros_like(ref)
    if delay < len(ref):
        out[delay:] = ref[: len(ref) - delay]
    return out


MIN_PEAK_Z = 7.0
"""Confianza minima del pico de GCC-PHAT (desviaciones tipicas sobre la media de la correlacion).
Ruido blanco da z ~ 4-5 en 8000 retardos; un eco real da > 20."""


def estimate_bulk_delay(
    mic: AudioArray,
    ref: AudioArray,
    sr: int,
    *,
    max_delay_s: float = 0.5,
    window_s: float = 20.0,
    windows: int = 3,
    min_z: float = MIN_PEAK_Z,
) -> int:
    """Retardo bruto (muestras) con el que el eco de ``ref`` llega a ``mic`` (GCC-PHAT).

    Mira hasta ``windows`` ventanas de ``window_s`` segundos con mas energia en ``ref`` y
    promedia sus correlaciones. Devuelve 0 si no hay senal o si el pico no destaca
    (``min_z`` desviaciones tipicas): con un eco debil o sin eco, GCC-PHAT siempre devuelve
    *algun* maximo y retrasar la referencia a ciegas empeoraria el resultado.
    Solo detecta retardos >= 0.
    """
    n = min(len(mic), len(ref))
    win = int(window_s * sr)
    if n < sr:
        return 0
    starts = list(range(0, max(1, n - win + 1), max(1, win)))
    energies = [float(np.sum(np.square(ref[s : s + win]))) for s in starts]
    order = np.argsort(energies)[::-1][:windows]
    max_lag = int(max_delay_s * sr)
    acc: NDArray[np.float64] | None = None
    for i in order:
        s = starts[int(i)]
        m = np.asarray(mic[s : s + win], dtype=np.float64)
        r = np.asarray(ref[s : s + win], dtype=np.float64)
        if np.sum(r * r) < 1e-9 or np.sum(m * m) < 1e-9:
            continue
        nfft = 1 << int(np.ceil(np.log2(len(m) + len(r))))
        spec = np.fft.rfft(m, nfft) * np.conj(np.fft.rfft(r, nfft))
        spec /= np.maximum(np.abs(spec), 1e-12)  # PHAT
        cc = np.fft.irfft(spec, nfft)
        seg = cc[: max_lag + 1]  # retardos positivos: mic va por detras de ref
        acc = seg if acc is None else acc + seg
    if acc is None:
        return 0
    peak = int(np.argmax(acc))
    z = (float(acc[peak]) - float(np.mean(acc))) / (float(np.std(acc)) + 1e-12)
    return peak if z >= min_z else 0
