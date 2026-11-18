"""Etapa 7: diarizacion de la pista remota (spec 8.7). Solo la pista del sistema.

Un unico diarizador cargado (``processing.diarization``). Si es ademas un ``SpeakerEmbedder`` y
las huellas estan activadas (``privacy.voiceprints_enabled``), se calculan aqui los embeddings de
cada hablante (el modelo solo esta cargado ahora) para la etapa 10.

Degradacion: si no hay diarizador instalado (``EngineUnavailableError``) la etapa termina sin
turnos y la etapa 8 etiqueta toda la pista remota como ``S1`` («sin diarizacion», F1);
``info.diarizer`` es ``None`` y ``info.skipped`` dice por que. Sin palabras remotas no se carga
ningun modelo.

Salidas: ``work/07_turns.json`` y, si procede, ``work/07_embeddings.json``.
"""

from __future__ import annotations

import logging
from typing import Any

from micnaga.pipeline.engines.diar.base import SpeakerEmbedder
from micnaga.pipeline.stages.base import StageContext, StageResult, engine_scope
from micnaga.registry import EngineUnavailableError

log = logging.getLogger(__name__)

TURNS = "07_turns.json"
EMBEDDINGS = "07_embeddings.json"


def run(ctx: StageContext) -> StageResult:
    words: list[Any] = ctx.read_json("06_words_remote.json")
    if not words:
        ctx.write_json(TURNS, [])
        return StageResult([TURNS], {"diarizer": None, "skipped": "no remote speech"})
    try:
        diarizer = ctx.deps.create_diarizer(ctx.config)
    except EngineUnavailableError as exc:
        log.warning("diarizer unavailable, labelling the remote track as one speaker: %s", exc)
        ctx.write_json(TURNS, [])
        return StageResult(
            [TURNS], {"diarizer": None, "skipped": "unavailable", "detail": str(exc)}
        )
    name = diarizer.name
    outputs = [TURNS]
    embeddings_out: dict[str, list[float]] = {}
    with engine_scope(diarizer):
        audio = ctx.conditioned("system")
        ctx.progress(0.1)
        turns = diarizer.diarize(audio, ctx.sr, None)
        ctx.progress(0.8)
        if (
            ctx.config.privacy.voiceprints_enabled
            and turns
            and isinstance(diarizer, SpeakerEmbedder)
        ):
            for sid, vec in diarizer.embed(audio, ctx.sr, turns).items():
                embeddings_out[sid] = [float(x) for x in vec]
        del audio
    del diarizer
    ctx.write_json(TURNS, [{"speaker": t.speaker, "start": t.start, "end": t.end} for t in turns])
    if embeddings_out:
        ctx.write_json(EMBEDDINGS, embeddings_out)
        outputs.append(EMBEDDINGS)
    return StageResult(
        outputs,
        {
            "diarizer": name,
            "n_speakers": len({t.speaker for t in turns}),
            "has_embeddings": bool(embeddings_out),
        },
    )
