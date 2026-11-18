"""Mezcla a mono y remuestreo en streaming a 16 kHz con soxr (seccion 7, trampa 3)."""

from __future__ import annotations

import numpy as np
import soxr
from numpy.typing import NDArray

F32 = NDArray[np.float32]
I16 = NDArray[np.int16]

_EMPTY: F32 = np.empty(0, dtype=np.float32)


def to_mono_float32(frames: NDArray[np.generic]) -> F32:
    """``(n,)`` o ``(n, canales)`` float32/int16 -> ``(n,)`` float32 [-1, 1] (media de canales)."""
    arr = np.asarray(frames)
    if arr.dtype == np.int16:
        arr = arr.astype(np.float32) * np.float32(1.0 / 32768.0)
    elif arr.dtype != np.float32:
        arr = arr.astype(np.float32)
    if arr.ndim == 2:
        arr = arr[:, 0] if arr.shape[1] == 1 else arr.mean(axis=1, dtype=np.float32)
    return np.ascontiguousarray(arr, dtype=np.float32)


def float_to_int16(x: F32) -> I16:
    """Cuantiza ``float32`` [-1, 1] a PCM16 con saturacion."""
    out: I16 = np.clip(np.rint(x * 32768.0), -32768, 32767).astype(np.int16)
    return out


class StreamResampler:
    """Mono + remuestreo en streaming (``soxr.ResampleStream``, calidad HQ).

    ``process`` recibe paquetes de cualquier tamano. ``flush`` emite la cola que soxr retiene
    (≈ 12 ms) y deja el remuestreador limpio para continuar tras un hueco o una pausa.
    ``pending`` es el retardo en muestras de salida: ``emitidas + pending`` = entrada * razon.
    """

    def __init__(self, in_rate: int, out_rate: int, *, quality: str = "HQ") -> None:
        self.in_rate = in_rate
        self.out_rate = out_rate
        self._stream: soxr.ResampleStream | None = None
        if in_rate != out_rate:
            self._stream = soxr.ResampleStream(
                float(in_rate), float(out_rate), 1, dtype="float32", quality=quality
            )

    @property
    def passthrough(self) -> bool:
        return self._stream is None

    @property
    def pending(self) -> int:
        """Muestras de salida retenidas en el filtro (0 si no se remuestrea)."""
        if self._stream is None:
            return 0
        return int(round(float(self._stream.delay())))  # noqa: RUF046

    def process(self, mono: F32) -> F32:
        if self._stream is None or len(mono) == 0:
            return mono if self._stream is None else _EMPTY
        return np.asarray(self._stream.resample_chunk(mono), dtype=np.float32)

    def flush(self) -> F32:
        """Emite la cola y reinicia el estado (el siguiente paquete empieza de cero)."""
        if self._stream is None:
            return _EMPTY
        tail = np.asarray(self._stream.resample_chunk(_EMPTY, last=True), dtype=np.float32)
        self._stream.clear()
        return tail
