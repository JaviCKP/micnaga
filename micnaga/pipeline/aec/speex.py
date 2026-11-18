"""Eliminacion de eco con SpeexDSP via ``pyaec`` (DLL ``aec.dll``).

``pyaec 1.0.1`` expone la DLL por ``ctypes``: ``AecNew(frame_size, filter_length, sample_rate,
enable_preprocess)``, ``AecCancelEcho(handle, rec*, echo*, out*, size)`` y ``AecDestroy``. Su
clase ``pyaec.Aec`` convierte listas Python en cada trama (muy lenta), asi que aqui se llama a la
DLL directamente con punteros a un unico buffer ``int16`` contiguo (``AecCancelEcho`` procesa
exactamente ``frame_size`` muestras por llamada).

* ``frame_size`` = 10 ms; ``filter_length`` = cola del filtro adaptativo (256 ms por defecto).
  Speex solo cancela ecos cuyo retardo cabe en esa cola, asi que por defecto ``auto_align``
  estima el retardo bruto con GCC-PHAT (``_common.estimate_bulk_delay``, 0 si el pico no
  destaca) y retrasa la referencia ``retardo - 20 ms``; ``precompensate_delay`` lo fija a mano.
* ``enable_preprocess`` (por defecto activado, es la configuracion ganadora en
  docs/aec_comparison.md) usa el preprocesador de Speex (supresion de eco residual y ruido de la
  propia DLL): quita mas eco, a costa de ~7 dB de fidelidad de la voz cercana limpia. Su salida
  llega una trama tarde: se compensa para que ``out[i]`` corresponda a ``mic[i]``.
* LICENCIA: el wheel de ``pyaec`` no declara licencia, pero su repositorio de origen
  (github.com/thewh1teagle/aec, enlaza ``aec-rs``) es MIT y la DLL contiene SpeexDSP (BSD-3):
  compatible con MIT. Anotado en DECISIONS.md.
"""

from __future__ import annotations

import ctypes
from typing import Any

import numpy as np

from micnaga.config import Config
from micnaga.pipeline.aec._common import (
    delay_ref,
    estimate_bulk_delay,
    float_to_int16,
    int16_to_float,
    match_length,
    pad_to_multiple,
)
from micnaga.registry import EngineUnavailableError
from micnaga.types import AudioArray

NAME = "speexdsp-pyaec"
DEFAULT_FILTER_MS = 256


class SpeexEchoCanceller:
    name = NAME

    def __init__(
        self,
        *,
        filter_ms: int = DEFAULT_FILTER_MS,
        enable_preprocess: bool = True,
        precompensate_delay: int = 0,
        auto_align: bool = True,
        margin_ms: float = 20.0,
    ) -> None:
        try:
            import pyaec
        except ImportError as exc:
            raise EngineUnavailableError(
                f"pyaec is not installed: {exc} (pip install micnaga[aec])"
            ) from exc
        if getattr(pyaec, "lib", None) is None:
            raise EngineUnavailableError("pyaec could not load its native library (aec.dll)")
        self._lib: Any = pyaec.lib
        self._handle_t: Any = pyaec.AecHandle
        self.filter_ms = filter_ms
        self.enable_preprocess = enable_preprocess
        self.precompensate_delay = precompensate_delay
        self.auto_align = auto_align
        self.margin_ms = margin_ms

    def process(self, mic: AudioArray, ref: AudioArray, sr: int) -> AudioArray:
        n = len(mic)
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        r = match_length(np.asarray(ref, dtype=np.float32), n)
        delay = self.precompensate_delay
        if self.auto_align:
            m0 = np.asarray(mic, dtype=np.float32)
            delay = max(0, estimate_bulk_delay(m0, r, sr) - int(self.margin_ms * sr / 1000))
        r = delay_ref(r, delay)
        frame = max(1, sr // 100)
        # El preprocesador de Speex retrasa la salida una trama: se vacia con ceros y se descarta.
        lat = frame if self.enable_preprocess else 0
        tail = np.zeros(lat, dtype=np.int16)
        mic16 = np.ascontiguousarray(
            pad_to_multiple(np.concatenate([float_to_int16(np.asarray(mic)), tail]), frame)
        )
        ref16 = np.ascontiguousarray(
            pad_to_multiple(np.concatenate([float_to_int16(r), tail]), frame)
        )
        out16 = np.zeros_like(mic16)
        filter_len = max(frame, int(sr * self.filter_ms / 1000) // frame * frame)
        handle = self._lib.AecNew(frame, filter_len, sr, bool(self.enable_preprocess))
        if not handle:
            raise RuntimeError("pyaec: AecNew failed")
        try:
            p16 = ctypes.POINTER(ctypes.c_int16)
            mic_addr, ref_addr, out_addr = (
                mic16.ctypes.data,
                ref16.ctypes.data,
                out16.ctypes.data,
            )
            step = frame * 2  # bytes
            cancel = self._lib.AecCancelEcho
            for k in range(len(mic16) // frame):
                off = k * step
                cancel(
                    handle,
                    ctypes.cast(mic_addr + off, p16),
                    ctypes.cast(ref_addr + off, p16),
                    ctypes.cast(out_addr + off, p16),
                    frame,
                )
        finally:
            self._lib.AecDestroy(handle)
        return match_length(int16_to_float(out16)[lat : lat + n], n)


def create(config: Config) -> SpeexEchoCanceller:
    return SpeexEchoCanceller()
