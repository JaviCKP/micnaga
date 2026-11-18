"""Etapa 4: acondicionado (spec 8.4).

Filtro paso alto de 80 Hz (Butterworth de 4.o orden, fase cero) y normalizacion de volumen **por
pista**. Sin supresion de ruido: los modelos de transcripcion rinden peor con audio sobreprocesado.

Normalizacion: la ganancia lleva el RMS de las tramas «activas» (por encima de -50 dBFS en
tramas de 50 ms) a ``TARGET_RMS_DBFS`` (-23 dBFS), limitada a ``MAX_GAIN_DB`` (+30 dB) y de modo
que el pico no pase de ``PEAK_LIMIT`` (0,98). Una pista sin tramas activas (silencio) no se toca.

Entradas: ``03_mic_aec.wav`` si existe (si no ``02_mic.wav``) y ``02_system.wav``.
Salidas: ``work/04_mic.wav`` y ``work/04_system.wav``.
"""

from __future__ import annotations

import numpy as np

from micnaga.pipeline.stages.base import StageContext, StageResult
from micnaga.types import AudioArray

HIGHPASS_HZ = 80.0
TARGET_RMS_DBFS = -23.0
ACTIVE_THRESHOLD_DBFS = -50.0
MAX_GAIN_DB = 30.0
PEAK_LIMIT = 0.98
FRAME_S = 0.05
BLOCK_S = 60.0
MARGIN_S = 2.0

MIC = "04_mic.wav"
SYSTEM = "04_system.wav"


def highpass(audio: AudioArray, sr: int, cutoff_hz: float = HIGHPASS_HZ) -> AudioArray:
    """Paso alto de fase cero por bloques de ``BLOCK_S`` con margen ``MARGIN_S`` a cada lado.

    Por bloques el pico de RAM no depende de la duracion (una hora entera en float64 con
    ``sosfiltfilt`` llegaba a ~2 GB); la respuesta al impulso del filtro dura unas decimas de
    segundo, asi que los margenes absorben los transitorios de los bordes de cada bloque.
    """
    from scipy import signal

    if len(audio) < 64:
        return audio
    sos = signal.butter(4, cutoff_hz, btype="highpass", fs=sr, output="sos")
    block, margin = int(BLOCK_S * sr), int(MARGIN_S * sr)
    if len(audio) <= block + 2 * margin:
        return np.asarray(signal.sosfiltfilt(sos, audio.astype(np.float64)), dtype=np.float32)
    out = np.empty(len(audio), dtype=np.float32)
    for start in range(0, len(audio), block):
        end = min(start + block, len(audio))
        lo, hi = max(0, start - margin), min(len(audio), end + margin)
        filtered = signal.sosfiltfilt(sos, audio[lo:hi].astype(np.float64))
        out[start:end] = filtered[start - lo : start - lo + (end - start)]
    return out


def _active_rms(audio: AudioArray, frame: int) -> tuple[float, bool]:
    """RMS global de las tramas activas (calculado por trozos); ``(0, False)`` si no hay."""
    n = len(audio) // frame
    chunk_frames = max(1, int(600 * 16000 / frame))
    threshold = 10 ** (ACTIVE_THRESHOLD_DBFS / 20)
    energy, count = 0.0, 0
    for f0 in range(0, n, chunk_frames):
        f1 = min(n, f0 + chunk_frames)
        frames = audio[f0 * frame : f1 * frame].reshape(f1 - f0, frame).astype(np.float64)
        rms = np.sqrt(np.mean(frames**2, axis=1))
        active = rms > threshold
        energy += float(np.sum(rms[active] ** 2))
        count += int(np.count_nonzero(active))
    if count == 0:
        return 0.0, False
    return float(np.sqrt(energy / count)), True


def normalize_loudness(audio: AudioArray, sr: int) -> tuple[AudioArray, float]:
    """Devuelve ``(audio, ganancia_dB)``."""
    frame = max(1, int(FRAME_S * sr))
    if len(audio) // frame == 0:
        return audio, 0.0
    level, found = _active_rms(audio, frame)
    if not found:
        return audio, 0.0
    gain_db = min(TARGET_RMS_DBFS - 20 * float(np.log10(level)), MAX_GAIN_DB)
    gain = 10 ** (gain_db / 20)
    peak = float(np.max(np.abs(audio)))
    if peak * gain > PEAK_LIMIT:
        gain = PEAK_LIMIT / peak
    return audio * np.float32(gain), float(20 * np.log10(gain))


def condition(audio: AudioArray, sr: int) -> tuple[AudioArray, float]:
    return normalize_loudness(highpass(audio, sr), sr)


def run(ctx: StageContext) -> StageResult:
    gains: dict[str, float] = {}
    sources = {
        "mic": "03_mic_aec.wav" if ctx.exists("03_mic_aec.wav") else "02_mic.wav",
        "system": "02_system.wav",
    }
    for i, (track, out) in enumerate((("mic", MIC), ("system", SYSTEM))):
        processed, gain_db = condition(ctx.read_work_audio(sources[track]), ctx.sr)
        ctx.write_audio(out, processed)
        gains[track] = round(gain_db, 2)
        ctx.progress((i + 1) / 2)
    return StageResult([MIC, SYSTEM], {"highpass_hz": HIGHPASS_HZ, "gain_db": gains})
