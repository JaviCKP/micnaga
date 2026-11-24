"""Etapa 3: eliminacion de eco, solo donde hace falta (spec 8.3, DECISIONS §35).

La regla esta en ``aec.policy`` y es la misma que usa el vivo: segun el modo de
``processing.aec`` («Automatica» / «Siempre» / «Desactivada») y, en «Automatica», segun el eco
**medido** (``aec.echo_check``) en cada trozo de 60 s (mirando los 5 min anteriores). Con
auriculares en «Automatica» no se mira el audio; una salida desconocida no se trata como
altavoces: decide la medida. El cancelador (``registry``/``processing.aec``) limpia la pista
del micro usando la del sistema como referencia; la del sistema no cambia. La decision de cada
trozo queda en el log y en la marca (``info.applied_slices``, ``info.reasons``).

Degradacion: con ``aec = "off"`` o si no hay ninguna implementacion instalada
(``EngineUnavailableError``) la etapa termina sin salida y deja constancia en su marca; la segunda
capa (etapa 9) es la red de seguridad.

Salida: ``work/03_mic_aec.wav`` (si se cancelo algun trozo).
"""

from __future__ import annotations

import gc
import logging
from typing import Any

import numpy as np

from micnaga.pipeline.stages.base import StageContext, StageResult
from micnaga.registry import EngineUnavailableError
from micnaga.types import CaptureMeta

log = logging.getLogger(__name__)

OUTPUT = "03_mic_aec.wav"


def needs_aec(meta: CaptureMeta) -> bool:
    """``False`` solo con auriculares toda la reunion (en «Automatica» no se mira el audio)."""
    from micnaga.pipeline.aec.policy import headphones_only

    return not headphones_only(meta)


def _close(canceller: Any) -> None:
    close = getattr(canceller, "close", None)
    if callable(close):
        close()


def run(ctx: StageContext) -> StageResult:
    from micnaga.pipeline.aec.policy import aec_mode, cancel_track, precheck

    mode = aec_mode(ctx.config.processing.aec)
    skip = precheck(mode, ctx.meta)
    if skip is not None:
        log.info("echo canceller (mode %s): not applied (%s)", mode, skip)
        return StageResult([], {"skipped": skip})
    try:
        canceller = ctx.deps.create_aec(ctx.config)
    except EngineUnavailableError as exc:
        log.warning("no echo canceller available, relying on text de-duplication: %s", exc)
        return StageResult([], {"skipped": "unavailable", "detail": str(exc)})
    if canceller is None:
        return StageResult([], {"skipped": "off"})
    try:
        mic = ctx.read_work_audio("02_mic.wav")
        ref = ctx.read_work_audio("02_system.wav")
        cleaned, summary = cancel_track(
            mode, ctx.meta, mic, ref, ctx.sr, canceller, progress=ctx.progress
        )
    finally:
        _close(canceller)
        del canceller
        gc.collect()
    log.info("%s", summary.describe())
    info = summary.info()
    if cleaned is None:
        return StageResult([], info)
    out = np.zeros(len(mic), dtype=np.float32)
    out[: len(cleaned)] = cleaned
    if len(mic) > len(cleaned):
        out[len(cleaned) :] = mic[len(cleaned) :]
    ctx.write_audio(OUTPUT, out)
    return StageResult([OUTPUT], info)
