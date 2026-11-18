"""Etapa 3: eliminacion de eco, solo con altavoces (spec 8.3).

Se aplica si ``output_kind != "headphones"`` (altavoces o desconocido) **o** si algun cambio de
dispositivo de la pista del sistema paso a algo que no son auriculares (caso trampa «cambio de
auriculares a altavoces a mitad»). El cancelador (``registry``/``processing.aec``) limpia la pista
del micro usando la del sistema como referencia; la del sistema no cambia.

Degradacion: con ``aec = "off"`` o si no hay ninguna implementacion instalada
(``EngineUnavailableError``) la etapa termina sin salida y deja constancia en su marca; la segunda
capa (etapa 9) es la red de seguridad.

Salida: ``work/03_mic_aec.wav`` (si se aplico).
"""

from __future__ import annotations

import gc
import logging

import numpy as np

from micnaga.pipeline.stages.base import StageContext, StageResult
from micnaga.registry import EngineUnavailableError
from micnaga.types import CaptureMeta

log = logging.getLogger(__name__)

OUTPUT = "03_mic_aec.wav"


def needs_aec(meta: CaptureMeta) -> bool:
    if meta.output_kind != "headphones":
        return True
    return any(c.track == "system" and c.output_kind != "headphones" for c in meta.device_changes)


def run(ctx: StageContext) -> StageResult:
    if not needs_aec(ctx.meta):
        return StageResult([], {"skipped": "headphones"})
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
        ctx.progress(0.3)
        cleaned = np.asarray(canceller.process(mic, ref, ctx.sr), dtype=np.float32)
        if len(cleaned) != len(mic):
            raise ValueError(f"echo canceller returned {len(cleaned)} samples, expected {len(mic)}")
        engine_name = canceller.name
    finally:
        close = getattr(canceller, "close", None)
        if callable(close):
            close()
        del canceller
        gc.collect()
    ctx.write_audio(OUTPUT, cleaned)
    return StageResult([OUTPUT], {"aec": engine_name})
