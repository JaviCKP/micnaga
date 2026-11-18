"""Etapa 8: asignacion de palabras a hablantes y construccion de frases (spec 8.8).

* Toda palabra de la pista del micro es ``"me"`` (Yo).
* Cada palabra remota va al turno de diarizacion con el que **mas tiempo se solapa**; si no solapa
  con ninguno, al turno **mas cercano** en el tiempo. Sin turnos (sin diarizacion) todo es ``S1``.

Despues las palabras se agrupan en frases (``phrases.build_phrases``: corte por cambio de hablante,
hueco >= 0,8 s o fin de frase).

Salida: ``work/08_phrases.json`` (lista de ``Segment``).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from micnaga.pipeline.stages.base import StageContext, StageResult
from micnaga.pipeline.stages.phrases import build_phrases
from micnaga.types import SPEAKER_ME, Turn, Word

OUTPUT = "08_phrases.json"
DEFAULT_REMOTE = "S1"


def assign_speakers(words: Sequence[Word], turns: Sequence[Turn]) -> list[str]:
    """Hablante de cada palabra remota (misma longitud y orden que ``words``)."""
    if not turns:
        return [DEFAULT_REMOTE] * len(words)
    starts = np.array([t.start for t in turns])
    ends = np.array([t.end for t in turns])
    labels = [t.speaker for t in turns]
    out: list[str] = []
    for w in words:
        overlap = np.minimum(ends, w.e) - np.maximum(starts, w.s)
        best = int(np.argmax(overlap))
        if overlap[best] <= 0:
            distance = np.maximum(np.maximum(starts - w.e, w.s - ends), 0.0)
            best = int(np.argmin(distance))
        out.append(labels[best])
    return out


def run(ctx: StageContext) -> StageResult:
    me = [Word.from_dict(d) for d in ctx.read_json("06_words_me.json")]
    remote = [Word.from_dict(d) for d in ctx.read_json("06_words_remote.json")]
    turns = [Turn(t["speaker"], t["start"], t["end"]) for t in ctx.read_json("07_turns.json")]
    remote_speakers = assign_speakers(remote, turns)
    pairs = [(SPEAKER_ME, w) for w in me] + list(zip(remote_speakers, remote, strict=True))
    phrases = build_phrases(pairs)
    ctx.write_json(OUTPUT, [p.to_dict() for p in phrases])
    return StageResult(
        [OUTPUT],
        {"phrases": len(phrases), "remote_speakers": sorted(set(remote_speakers))},
    )
