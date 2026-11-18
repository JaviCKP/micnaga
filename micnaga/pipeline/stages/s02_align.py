"""Etapa 2: alineacion de las dos pistas (spec 8.2).

Aplica la correccion de deriva registrada en ``capture.json`` con
``micnaga.capture.sync.align_tracks(mic, system, meta)`` y verifica que ambas pistas duran lo
mismo (+/- 50 ms). Si no, no se pierde la reunion: se rellena con silencio la mas corta hasta la
longitud de la mas larga y se anota ``length_mismatch_s`` en la marca de la etapa (y un aviso).

Salidas: ``work/02_mic.wav`` y ``work/02_system.wav`` (WAV PCM 16 bits).
"""

from __future__ import annotations

import logging

import numpy as np

from micnaga.pipeline.stages.base import ALIGN_TOLERANCE_S, StageContext, StageResult
from micnaga.types import AudioArray

log = logging.getLogger(__name__)

MIC = "02_mic.wav"
SYSTEM = "02_system.wav"


def equalize(mic: AudioArray, system: AudioArray) -> tuple[AudioArray, AudioArray]:
    """Rellena con ceros la pista mas corta hasta igualar longitudes."""
    n = max(len(mic), len(system))

    def pad(x: AudioArray) -> AudioArray:
        if len(x) == n:
            return x
        out = np.zeros(n, dtype=np.float32)
        out[: len(x)] = x
        return out

    return pad(mic), pad(system)


def run(ctx: StageContext) -> StageResult:
    mic = ctx.read_source_track("mic")
    system = ctx.read_source_track("system")
    ctx.progress(0.3)
    mic, system = ctx.deps.align_tracks(mic, system, ctx.meta)
    mic = np.asarray(mic, dtype=np.float32)
    system = np.asarray(system, dtype=np.float32)
    mismatch_s = abs(len(mic) - len(system)) / ctx.sr
    if mismatch_s > ALIGN_TOLERANCE_S:
        log.warning("aligned tracks differ by %.3f s; padding the shorter one", mismatch_s)
    mic, system = equalize(mic, system)
    ctx.write_audio(MIC, mic)
    ctx.write_audio(SYSTEM, system)
    return StageResult(
        [MIC, SYSTEM],
        {
            "duration_s": len(mic) / ctx.sr,
            "length_mismatch_s": round(mismatch_s, 4),
            "within_tolerance": mismatch_s <= ALIGN_TOLERANCE_S,
        },
    )
