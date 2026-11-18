"""Etapa 1: compuertas finales sobre el audio completo (spec 8.1 y 5).

Una sesion confirmada solo exige la duracion (basta con la cabecera de los ficheros: no se carga
audio ni se ejecuta el VAD). Una provisional carga ambas pistas, detecta voz en cada una y aplica
las tres compuertas (``micnaga.gates``; la compuerta 3 se calcula con el micro **sin** cancelar
el eco, de ahi la sombra de eco de ``gates.turn_spells``). Si no pasa se lanza
``SessionDiscarded``: el ``runner`` borra la carpeta y anota solo fecha + motivo.

Salida: ``work/01_gates.json``.
"""

from __future__ import annotations

from typing import Any

from micnaga import gates
from micnaga.pipeline.stages.base import (
    SessionDiscarded,
    StageContext,
    StageResult,
    audio_frames,
)

OUTPUT = "01_gates.json"


def run(ctx: StageContext) -> StageResult:
    cfg = ctx.config.gates
    confirmed = ctx.meta.confirmed
    frames = [audio_frames(ctx.source_track_path(t)) for t in ("mic", "system")]
    duration_s = max(n / sr for n, sr in frames)
    me_regions: list[Any] = []
    system_regions: list[Any] = []
    if confirmed:
        metrics = gates.GateMetrics(duration_s, 0.0, 0)
        result = gates.evaluate_gates(duration_s, 0.0, 0, True, cfg)
    else:
        try:
            me_audio = ctx.read_source_track("mic")
            sys_audio = ctx.read_source_track("system")
            ctx.progress(0.2)
            me_regions = ctx.deps.detect_speech(me_audio, ctx.sr, ctx.config)
            ctx.progress(0.6)
            system_regions = ctx.deps.detect_speech(sys_audio, ctx.sr, ctx.config)
        finally:
            ctx.deps.release_vad()
        result, metrics = gates.evaluate_regions(duration_s, me_regions, system_regions, False, cfg)
    ctx.write_json(
        OUTPUT,
        {
            "passed": result.passed,
            "reason": result.reason,
            "gate": result.gate,
            "confirmed": confirmed,
            "metrics": metrics.to_dict(),
        },
    )
    if not result.passed:
        raise SessionDiscarded(result.reason)
    return StageResult([OUTPUT], {"confirmed": confirmed, **metrics.to_dict()})
