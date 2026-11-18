"""Etapa 6: transcripcion por bloque (spec 8.6).

Se crea **un** motor ASR (``processing.asr_engine``; los hilos los toma el motor de
``processing.threads``), se transcribe cada bloque de las dos pistas (los bloques de la etapa 5) y
los tiempos de cada palabra, relativos al bloque, se desplazan a la linea de tiempo global. Despues
se cierra el motor, ``del`` y ``gc.collect()``.

Salidas: ``work/06_words_me.json`` y ``work/06_words_remote.json`` (listas de palabras
``{"w","s","e","p"}``); el nombre del motor queda en la marca (``info.engine``).
"""

from __future__ import annotations

from typing import Any

from micnaga.pipeline.stages.base import StageContext, StageResult, engine_scope
from micnaga.types import Word

WORDS_ME = "06_words_me.json"
WORDS_REMOTE = "06_words_remote.json"
MIN_CHUNK_S = 0.1


def run(ctx: StageContext) -> StageResult:
    vad: dict[str, Any] = ctx.read_json("05_vad.json")
    sr = ctx.sr
    total = sum(b - a for t in ("mic", "system") for a, b in vad[t]["chunks"]) or 1.0
    done = 0.0
    counts: dict[str, int] = {}
    outputs = {"mic": WORDS_ME, "system": WORDS_REMOTE}
    engine = ctx.deps.create_asr(ctx.config)
    name = engine.name
    with engine_scope(engine):
        for track in ("mic", "system"):
            chunks: list[list[float]] = vad[track]["chunks"]
            words: list[Word] = []
            audio = ctx.conditioned(track) if chunks else None
            for a, b in chunks:
                if audio is not None and b - a >= MIN_CHUNK_S:
                    i0, i1 = max(0, int(a * sr)), min(len(audio), int(b * sr))
                    for w in engine.transcribe(audio[i0:i1], sr):
                        if not w.w.strip():
                            continue
                        words.append(Word(w.w.strip(), w.s + a, max(w.e, w.s) + a, w.p))
                done += b - a
                ctx.progress(done / total)
            words.sort(key=lambda w: (w.s, w.e))
            ctx.write_json(outputs[track], [w.to_dict() for w in words])
            counts[track] = len(words)
            del audio
    del engine
    return StageResult([WORDS_ME, WORDS_REMOTE], {"engine": name, "words": counts})
