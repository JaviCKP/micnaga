"""Etapa 1: compuertas finales sobre el audio completo (spec 8.1 y 5).

Una sesion confirmada solo exige la duracion (basta con la cabecera de los ficheros: no se carga
audio ni se ejecuta el VAD). Una provisional carga ambas pistas, detecta voz en cada una y aplica
las tres compuertas (``micnaga.gates``; la compuerta 3 se calcula con el micro **sin** cancelar
el eco, de ahi la sombra de eco de ``gates.turn_spells``). Si no pasa se lanza
``SessionDiscarded``: el ``runner`` aparta la carpeta en «Descartadas» (``micnaga.holding``) y
anota solo fecha + motivo. Con ``gates.require_confirmed`` (defecto) una sesion sin confirmar
no se transcribe nunca. Una reunion presencial (``extra.in_person`` o su marca en la carpeta,
``capture.session.is_in_person``) cuenta como confirmada: la empezo el usuario a proposito y no
hay voz remota ni turnos que medir; solo se exige la duracion.

Salida: ``work/01_gates.json``.
"""

from __future__ import annotations

from typing import Any

from micnaga import gates
from micnaga.capture.session import is_in_person
from micnaga.pipeline.stages.base import (
    SessionDiscarded,
    StageContext,
    StageResult,
    audio_frames,
)

OUTPUT = "01_gates.json"
UNCONFIRMED = "sin confirmar"
USER_RESTORED = "recuperada por el usuario"


def run(ctx: StageContext) -> StageResult:
    cfg = ctx.config.gates
    in_person = is_in_person(ctx.session, ctx.meta.extra)
    confirmed = ctx.meta.confirmed or in_person
    if cfg.require_confirmed and not confirmed:
        # Red de seguridad: sin «Si» del usuario nunca se transcribe (se aparta, DECISIONS §18).
        ctx.write_json(OUTPUT, {"passed": False, "reason": UNCONFIRMED, "confirmed": False})
        raise SessionDiscarded(UNCONFIRMED)
    frames = [audio_frames(ctx.source_track_path(t)) for t in ("mic", "system")]
    duration_s = max(n / sr for n, sr in frames)
    if gates.user_restored(ctx.meta.extra):  # «Recuperar y transcribir»: decision del usuario
        metrics = gates.GateMetrics(duration_s, 0.0, 0)
        ctx.write_json(
            OUTPUT,
            {
                "passed": True,
                "reason": USER_RESTORED,
                "confirmed": True,
                "metrics": metrics.to_dict(),
            },
        )
        return StageResult([OUTPUT], {"confirmed": True, **metrics.to_dict()})
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
            "in_person": in_person,
            "metrics": metrics.to_dict(),
        },
    )
    if not result.passed:
        raise SessionDiscarded(result.reason)
    return StageResult([OUTPUT], {"confirmed": confirmed, **metrics.to_dict()})
