"""Etapa 9: eco, segunda capa (spec 8.9).

Descarta toda frase de «Yo» que coincide en el tiempo con una frase remota y se le parece en
>= ``processing.echo_similarity`` (0,8; ``rapidfuzz``). Es la red de seguridad para lo que el
cancelador de eco no quito. Detalles de la comparacion en ``phrases.drop_echo``.

Salida: ``work/09_phrases.json``; ``info.dropped`` = frases descartadas.
"""

from __future__ import annotations

from micnaga.pipeline.stages.base import StageContext, StageResult
from micnaga.pipeline.stages.phrases import drop_echo
from micnaga.types import Segment

OUTPUT = "09_phrases.json"


def run(ctx: StageContext) -> StageResult:
    phrases = [Segment.from_dict(d) for d in ctx.read_json("08_phrases.json")]
    kept, dropped = drop_echo(phrases, similarity=ctx.config.processing.echo_similarity)
    ctx.write_json(OUTPUT, [p.to_dict() for p in kept])
    return StageResult(
        [OUTPUT], {"dropped": len(dropped), "kept": len(kept), "total_me": _n_me(phrases)}
    )


def _n_me(phrases: list[Segment]) -> int:
    return sum(1 for p in phrases if p.speaker == "me")
