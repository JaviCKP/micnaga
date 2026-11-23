"""Etapa 8: asignacion de palabras a hablantes y construccion de frases (spec 8.8).

* Cada palabra remota va al turno de diarizacion con el que **mas tiempo se solapa**; si no solapa
  con ninguno, al turno **mas cercano** en el tiempo. Sin turnos (sin diarizacion) todo es ``S1``.
* Las palabras del micro, igual con los turnos del micro (``07_turns_mic.json``: ``me`` y las
  personas de la sala ``P<n>`` de una reunion presencial). Sin turnos del micro todo es ``"me"``.

* Un cambio de hablante que cae **dentro** de una oracion (la palabra anterior no acaba en
  ``. ? !``) y sin pausa se mueve al fin de oracion mas cercano si esta a <= ``SNAP_MAX_S``
  (0,6 s) y desplaza <= ``SNAP_MAX_WORDS`` (4) palabras, siempre que el turno que las cede
  conserve al menos ``SNAP_KEEP_WORDS`` (2) palabras: la diarizacion trabaja con ventanas de
  ~1 s y su frontera puede caer unas palabras antes o despues del cambio real, mientras que la
  puntuacion del ASR marca donde termina la frase (podcast real: «... hemos | llegado a la
  vida.»).

Despues las palabras se agrupan en frases (``phrases.build_phrases``: corte por cambio de hablante,
hueco >= 0,8 s o fin de frase).

Salida: ``work/08_phrases.json`` (lista de ``Segment``).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from micnaga.pipeline.stages.base import StageContext, StageResult
from micnaga.pipeline.stages.phrases import build_phrases, ends_sentence
from micnaga.types import SPEAKER_ME, Turn, Word

MIC_TURNS = "07_turns_mic.json"

OUTPUT = "08_phrases.json"
DEFAULT_REMOTE = "S1"
SNAP_MAX_S = 0.6
SNAP_MAX_WORDS = 4
SNAP_KEEP_WORDS = 2
SNAP_PAUSE_S = 0.3


def assign_speakers(
    words: Sequence[Word], turns: Sequence[Turn], default: str = DEFAULT_REMOTE
) -> list[str]:
    """Hablante de cada palabra (misma longitud y orden que ``words``); ``default`` sin turnos."""
    if not turns:
        return [default] * len(words)
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


def _run_bounds(speakers: Sequence[str], i: int) -> tuple[int, int]:
    """``[a, b)`` del tramo de palabras consecutivas del mismo hablante que contiene ``i``."""
    a = i
    while a > 0 and speakers[a - 1] == speakers[i]:
        a -= 1
    b = i + 1
    while b < len(speakers) and speakers[b] == speakers[i]:
        b += 1
    return a, b


def snap_changes_to_sentence_ends(
    words: Sequence[Word],
    speakers: Sequence[str],
    *,
    max_shift_s: float = SNAP_MAX_S,
    max_words: int = SNAP_MAX_WORDS,
    keep_words: int = SNAP_KEEP_WORDS,
    pause_s: float = SNAP_PAUSE_S,
) -> list[str]:
    """Mueve cada cambio de hablante a mitad de oracion al fin de oracion mas cercano (modulo).

    ``words`` va en orden temporal y ``speakers`` es el hablante de cada palabra. Devuelve la
    nueva lista de hablantes (misma longitud).
    """
    out = list(speakers)
    n = len(words)
    c = 1
    while c < n:
        if (
            out[c] == out[c - 1]
            or ends_sentence(words[c - 1].w)
            or words[c].s - words[c - 1].e >= pause_s
        ):
            c += 1
            continue
        left, right = out[c - 1], out[c]
        la, _ = _run_bounds(out, c - 1)
        _, rb = _run_bounds(out, c)
        t = words[c].s
        best: tuple[float, int] | None = None
        for j in range(max(la, c - 1 - max_words), min(rb, c + max_words)):
            if not ends_sentence(words[j].w) or abs(words[j].e - t) > max_shift_s:
                continue
            if j >= c:  # adelantar: words[c..j] pasan al hablante de la izquierda
                moved, kept = j - c + 1, rb - (j + 1)
            else:  # retrasar: words[j+1..c-1] pasan al de la derecha
                moved, kept = c - 1 - j, (j + 1) - la
            if moved > max_words or kept < keep_words:
                continue
            if best is None or abs(words[j].e - t) < best[0]:
                best = (abs(words[j].e - t), j)
        if best is None:
            c += 1
            continue
        j = best[1]
        if j >= c:
            for k in range(c, j + 1):
                out[k] = left
            c = j + 2
        else:
            for k in range(j + 1, c):
                out[k] = right
            c += 1
    return out


def run(ctx: StageContext) -> StageResult:
    me = [Word.from_dict(d) for d in ctx.read_json("06_words_me.json")]
    remote = [Word.from_dict(d) for d in ctx.read_json("06_words_remote.json")]
    turns = [Turn(t["speaker"], t["start"], t["end"]) for t in ctx.read_json("07_turns.json")]
    mic_turns = (
        [Turn(t["speaker"], t["start"], t["end"]) for t in ctx.read_json(MIC_TURNS)]
        if ctx.exists(MIC_TURNS)
        else []
    )
    remote.sort(key=lambda w: (w.s, w.e))
    me.sort(key=lambda w: (w.s, w.e))
    remote_speakers = snap_changes_to_sentence_ends(remote, assign_speakers(remote, turns))
    mic_speakers = assign_speakers(me, mic_turns, SPEAKER_ME)
    if mic_turns:
        mic_speakers = snap_changes_to_sentence_ends(me, mic_speakers)
    pairs = list(zip(mic_speakers, me, strict=True)) + list(
        zip(remote_speakers, remote, strict=True)
    )
    phrases = build_phrases(pairs)
    ctx.write_json(OUTPUT, [p.to_dict() for p in phrases])
    return StageResult(
        [OUTPUT],
        {
            "phrases": len(phrases),
            "remote_speakers": sorted(set(remote_speakers)),
            "room_speakers": sorted(set(mic_speakers) - {SPEAKER_ME}),
        },
    )
