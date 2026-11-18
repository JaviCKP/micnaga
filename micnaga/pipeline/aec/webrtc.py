"""Eliminacion de eco con WebRTC AEC3 (paquete ``aec-audio-processing``, BSD-3-Clause).

API verificada (aec-audio-processing 1.0.1): ``AudioProcessor(enable_aec, enable_ns, ns_level,
enable_agc, agc_mode, enable_vad)``, ``set_stream_format(sr, canales)``,
``set_reverse_stream_format(sr, canales)``, ``process_reverse_stream(bytes_int16)`` y
``process_stream(bytes_int16) -> bytes``, siempre con **tramas de 10 ms** (160 muestras a 16 kHz;
80/160/320/480 para 8/16/32/48 kHz). Por cada trama: primero la de referencia (altavoces) y luego
la del micro. NS, AGC y VAD se desactivan (spec 8.4: sin supresion agresiva).

Siempre se trabaja a 16 kHz (otras frecuencias se remuestrean con ``soxr`` y se devuelven a la
original): con 32/48 kHz AEC3 usa bancos de filtros con mas retardo y menos predecible.
La salida de AEC3 llega con 128 muestras (8 ms) de retraso: se compensa para que ``out[i]``
corresponda a ``mic[i]`` (importa para los tiempos de palabra).
La longitud de salida es siempre la del micro (la ultima trama parcial se rellena con ceros y se
recorta). ``process`` crea un procesador nuevo en cada llamada (estado adaptativo limpio).

``warmup_s`` > 0 ejecuta antes una pasada de calentamiento sobre los primeros segundos para que
el filtro adaptativo ya haya convergido cuando empieza la pasada real (ver docs/aec_comparison.md
para el efecto medido).
"""

from __future__ import annotations

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

NAME = "webrtc-aec3"
INTERNAL_SR = 16000
FRAME = INTERNAL_SR // 100  # 10 ms
LATENCY_SAMPLES = 128  # retardo medido de la salida de AEC3 a 16 kHz (8 ms)


class WebRtcEchoCanceller:
    name = NAME

    def __init__(
        self,
        *,
        warmup_s: float = 0.0,
        precompensate_delay: int = 0,
        auto_align: bool = True,
        margin_ms: float = 20.0,
    ) -> None:
        try:
            from aec_audio_processing import AudioProcessor
        except ImportError as exc:
            raise EngineUnavailableError(
                f"aec-audio-processing is not installed: {exc} (pip install micnaga[aec])"
            ) from exc
        self._ap_cls: Any = AudioProcessor
        self.warmup_s = warmup_s
        self.precompensate_delay = precompensate_delay
        self.auto_align = auto_align
        self.margin_ms = margin_ms

    def _new_processor(self, sr: int) -> Any:
        ap = self._ap_cls(enable_aec=True, enable_ns=False, enable_agc=False, enable_vad=False)
        ap.set_stream_format(sr, 1)
        ap.set_reverse_stream_format(sr, 1)
        return ap

    @staticmethod
    def _run(ap: Any, mic16: Any, ref16: Any, frame: int) -> Any:
        out = np.empty_like(mic16)
        mic_b, ref_b = mic16.tobytes(), ref16.tobytes()
        step = frame * 2
        pieces: list[bytes] = []
        for off in range(0, len(mic_b), step):
            ap.process_reverse_stream(ref_b[off : off + step])
            pieces.append(ap.process_stream(mic_b[off : off + step]))
        out[:] = np.frombuffer(b"".join(pieces), dtype=np.int16)
        return out

    def process(self, mic: AudioArray, ref: AudioArray, sr: int) -> AudioArray:
        n = len(mic)
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        m = np.asarray(mic, dtype=np.float32)
        r = match_length(np.asarray(ref, dtype=np.float32), n)
        if sr != INTERNAL_SR:
            import soxr

            m = np.asarray(soxr.resample(m, sr, INTERNAL_SR), dtype=np.float32)
            r = np.asarray(soxr.resample(r, sr, INTERNAL_SR), dtype=np.float32)
        n16 = len(m)
        r = match_length(r, n16)
        delay = self.precompensate_delay
        if self.auto_align:
            delay = max(0, estimate_bulk_delay(m, r, INTERNAL_SR) - int(self.margin_ms * 16))
        r = delay_ref(r, delay)
        # AEC3 devuelve la salida con LATENCY_SAMPLES de retraso: se alimentan ceros al final
        # para vaciar el pipeline y se descartan las primeras LATENCY_SAMPLES de la salida.
        tail = np.zeros(LATENCY_SAMPLES, dtype=np.int16)
        mic16 = pad_to_multiple(np.concatenate([float_to_int16(m), tail]), FRAME)
        ref16 = pad_to_multiple(np.concatenate([float_to_int16(r), tail]), FRAME)
        ap = self._new_processor(INTERNAL_SR)
        warm = int(self.warmup_s * INTERNAL_SR) // FRAME * FRAME
        if warm > 0:
            self._run(ap, mic16[:warm], ref16[:warm], FRAME)
        out16 = self._run(ap, mic16, ref16, FRAME)
        out = int16_to_float(out16)[LATENCY_SAMPLES : LATENCY_SAMPLES + n16]
        if sr != INTERNAL_SR:
            import soxr

            out = np.asarray(soxr.resample(out, INTERNAL_SR, sr), dtype=np.float32)
        return match_length(out, n)


def create(config: Config) -> WebRtcEchoCanceller:
    return WebRtcEchoCanceller()
