"""Etapa 5: deteccion de voz por pista y agrupacion en bloques (spec 8.5).

Sobre el audio acondicionado de cada pista: ``detect_speech`` (Silero VAD ONNX; 250 ms de voz
minima, 500 ms de silencio para cortar, 200 ms de margen, valores de ``[vad]``) y
``group_regions`` (bloques de hasta ``vad.max_block_s`` = 30 s de audio contando cada silencio
intermedio como mucho ``vad.MAX_GAP_S``: lo que queda de el tras la etapa 6). Un bloque va del
inicio del primer tramo al fin del ultimo; la transcripcion (etapa 6) trabaja bloque a bloque.

Salida: ``work/05_vad.json``::

    {"mic": {"regions": [[s, e], ...], "chunks": [[s, e], ...]}, "system": {...}}

Con ``deps.speech_probs`` (motores reales) se guardan ademas las probabilidades por trama en
``work/05_probs.npz`` (``mic``, ``system``): los tramos salen de ellas con los mismos
parametros, y la etapa 7 (diarizador ONNX) las reutiliza en vez de volver a pasar el VAD.
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from micnaga.pipeline.stages.base import StageContext, StageResult, atomic_write_bytes
from micnaga.pipeline.vad import region_params, regions_with
from micnaga.types import SpeechRegion

OUTPUT = "05_vad.json"
PROBS = "05_probs.npz"

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


def save_probs(path: Path, probs: dict[str, NDArray[np.float32]]) -> None:
    buf = io.BytesIO()
    kwargs: dict[str, Any] = dict(probs)
    np.savez(buf, **kwargs)
    atomic_write_bytes(path, buf.getvalue())


def load_probs(path: Path) -> dict[str, NDArray[np.float32]] | None:
    """Probabilidades guardadas por la etapa 5 (``None`` si no hay o no se leen)."""
    if not path.is_file():
        return None
    try:
        with np.load(path) as data:
            return {k: np.asarray(data[k], dtype=np.float32) for k in data.files}
    except (OSError, ValueError):
        return None


def run(ctx: StageContext) -> StageResult:
    out: dict[str, dict[str, list[list[float]]]] = {}
    n_regions: dict[str, int] = {}
    max_block = ctx.config.vad.max_block_s
    probs_out: dict[str, NDArray[np.float32]] = {}
    params = region_params(ctx.config)
    try:
        for i, track in enumerate(("mic", "system")):
            audio = ctx.conditioned(track)
            if ctx.deps.speech_probs is not None:
                probs = np.asarray(ctx.deps.speech_probs(audio, ctx.sr, ctx.config), np.float32)
                probs_out[track] = probs
                regions = regions_with(probs, len(audio) / ctx.sr, params)
            else:
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
    outputs = [OUTPUT]
    if probs_out:
        save_probs(ctx.path(PROBS), probs_out)
        outputs.append(PROBS)
    ctx.write_json(OUTPUT, out)
    return StageResult(outputs, {"regions": n_regions})
