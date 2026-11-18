"""Etapa 5: deteccion de voz por pista y agrupacion en bloques (spec 8.5).

Sobre el audio acondicionado de cada pista: ``detect_speech`` (Silero VAD ONNX; 250 ms de voz
minima, 500 ms de silencio para cortar, 200 ms de margen, valores de ``[vad]``) y
``group_regions`` (bloques de hasta ``vad.max_block_s`` = 30 s). Un bloque va del inicio del
primer tramo al fin del ultimo; la transcripcion (etapa 6) trabaja bloque a bloque.

Salida: ``work/05_vad.json``::

    {"mic": {"regions": [[s, e], ...], "chunks": [[s, e], ...]}, "system": {...}}
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from micnaga.pipeline.stages.base import StageContext, StageResult
from micnaga.types import SpeechRegion

OUTPUT = "05_vad.json"

Span = tuple[float, float]


def as_spans(blocks: Iterable[Any]) -> list[Span]:
    """Normaliza lo que devuelva ``group_regions``: ``(ini, fin)``, ``SpeechRegion`` o listas."""
    spans: list[Span] = []
    for b in blocks:
        if isinstance(b, SpeechRegion):
            spans.append((b.start, b.end))
        elif (
            isinstance(b, tuple | list)
            and len(b) == 2
            and all(isinstance(x, int | float) for x in b)
        ):
            spans.append((float(b[0]), float(b[1])))
        else:
            regions = list(b)
            if regions:
                spans.append((float(regions[0].start), float(regions[-1].end)))
    return spans


def run(ctx: StageContext) -> StageResult:
    out: dict[str, dict[str, list[list[float]]]] = {}
    n_regions: dict[str, int] = {}
    max_block = ctx.config.vad.max_block_s
    try:
        for i, track in enumerate(("mic", "system")):
            audio = ctx.conditioned(track)
            regions = ctx.deps.detect_speech(audio, ctx.sr, ctx.config)
            chunks = as_spans(ctx.deps.group_regions(regions, max_block))
            out[track] = {
                "regions": [[r.start, r.end] for r in regions],
                "chunks": [[a, b] for a, b in chunks],
            }
            n_regions[track] = len(regions)
            ctx.progress((i + 1) / 2)
    finally:
        ctx.deps.release_vad()
    ctx.write_json(OUTPUT, out)
    return StageResult([OUTPUT], {"regions": n_regions})
