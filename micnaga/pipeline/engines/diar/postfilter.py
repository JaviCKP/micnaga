"""Post-filtro de hablantes de cualquier diarizador: nunca se reporta un hablante «fantasma».

Prioridad del usuario: ni hablantes fantasma, ni hablantes que no existen, ni muchos hablantes
cuando hay pocos. Un «hablante» con muy poca voz es casi siempre un error del diarizador (una
palabra suelta con otra entonacion, un ruido, un trozo de musica, la mezcla de dos voces en un
cambio de turno), asi que no se reporta por separado:

* **Menor**: menos de ``MIN_SPEAKER_S`` (3 s) de voz en total, o un unico turno de menos de
  ``SINGLE_TURN_MAX_S`` (4 s).
* Un menor se absorbe entero en el hablante no menor **mas parecido** (coseno entre embeddings)
  si hay embeddings de los dos; si no, cada uno de sus turnos pasa al hablante no menor del turno
  **mas cercano en el tiempo** (solapado = distancia 0; en empate, el anterior).
* Si todos son menores se conserva el que mas habla (nunca se absorbe al dominante).
* Despues se funden los turnos contiguos del mismo hablante (hueco <= ``MERGE_GAP_S``) y se
  renumera ``S1..Sn`` por primera aparicion.

Coste conocido (aceptado a proposito): un participante real que solo dice una frase corta queda
atribuido a otro. El usuario puede corregirlo a mano; un hablante inventado es peor.

Funciones puras (sin modelos); la etapa 7 las aplica a la salida de todos los diarizadores.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
from numpy.typing import NDArray

from micnaga.types import Turn

MIN_SPEAKER_S = 3.0
SINGLE_TURN_MAX_S = 4.0
MERGE_GAP_S = 0.5


def speech_by_speaker(turns: Sequence[Turn]) -> dict[str, float]:
    """Segundos de voz de cada hablante (union de sus turnos: los solapes no cuentan doble)."""
    spans: dict[str, list[tuple[float, float]]] = {}
    for t in turns:
        if t.end > t.start:
            spans.setdefault(t.speaker, []).append((t.start, t.end))
    out: dict[str, float] = {}
    for sid, items in spans.items():
        items.sort()
        total = 0.0
        cur_a, cur_b = items[0]
        for a, b in items[1:]:
            if a > cur_b:
                total += cur_b - cur_a
                cur_a, cur_b = a, b
            else:
                cur_b = max(cur_b, b)
        out[sid] = total + cur_b - cur_a
    return out


def minor_speakers(
    turns: Sequence[Turn],
    *,
    min_speaker_s: float = MIN_SPEAKER_S,
    single_turn_max_s: float = SINGLE_TURN_MAX_S,
) -> set[str]:
    """Hablantes que no se reportan por separado (ver modulo)."""
    totals = speech_by_speaker(turns)
    counts: dict[str, int] = {}
    for t in turns:
        if t.end > t.start:
            counts[t.speaker] = counts.get(t.speaker, 0) + 1
    return {
        sid
        for sid, total in totals.items()
        if total < min_speaker_s or (counts[sid] == 1 and total < single_turn_max_s)
    }


def speakers_to_absorb(
    turns: Sequence[Turn],
    *,
    min_speaker_s: float = MIN_SPEAKER_S,
    single_turn_max_s: float = SINGLE_TURN_MAX_S,
) -> set[str]:
    """Menores que se absorberan (si todos son menores, todos menos el que mas habla)."""
    totals = speech_by_speaker(turns)
    minor = minor_speakers(turns, min_speaker_s=min_speaker_s, single_turn_max_s=single_turn_max_s)
    if totals and minor >= set(totals):
        keep = max(sorted(totals), key=lambda sid: totals[sid])
        minor = minor - {keep}
    return minor


def _unit(v: NDArray[np.float32]) -> NDArray[np.float32]:
    a = np.asarray(v, dtype=np.float32).ravel()
    return np.asarray(a / max(float(np.linalg.norm(a)), 1e-12), dtype=np.float32)


def _nearest_turn_speaker(turn: Turn, others: Sequence[Turn]) -> str:
    best: tuple[float, int, str] | None = None
    for o in others:
        distance = max(0.0, o.start - turn.end, turn.start - o.end)
        after = 1 if o.start >= turn.end and distance > 0 else 0  # empate: el anterior
        key = (distance, after, o.speaker)
        if best is None or key[:2] < best[:2]:
            best = key
    assert best is not None
    return best[2]


def merge_and_renumber(turns: Sequence[Turn], merge_gap_s: float = MERGE_GAP_S) -> list[Turn]:
    """Funde turnos contiguos (o solapados) del mismo hablante y renumera ``S1..Sn``."""
    ordered = sorted((t for t in turns if t.end > t.start), key=lambda t: (t.start, t.end))
    last: dict[str, int] = {}
    merged: list[Turn] = []
    for t in ordered:
        k = last.get(t.speaker)
        # solo se funde con el ultimo turno de ese hablante si no hay otro hablante en medio
        if k is not None and k == len(merged) - 1 and t.start - merged[k].end <= merge_gap_s:
            prev = merged[k]
            merged[k] = Turn(prev.speaker, prev.start, max(prev.end, t.end))
            continue
        if k is not None and t.start <= merged[k].end:  # solapado con su propio turno anterior
            prev = merged[k]
            merged[k] = Turn(prev.speaker, prev.start, max(prev.end, t.end))
            continue
        last[t.speaker] = len(merged)
        merged.append(t)
    mapping: dict[str, str] = {}
    out: list[Turn] = []
    for t in sorted(merged, key=lambda t: (t.start, t.end)):
        sid = mapping.setdefault(t.speaker, f"S{len(mapping) + 1}")
        out.append(Turn(sid, t.start, t.end))
    return out


def absorb_minor_speakers(
    turns: Sequence[Turn],
    embeddings: Mapping[str, NDArray[np.float32]] | None = None,
    *,
    min_speaker_s: float = MIN_SPEAKER_S,
    single_turn_max_s: float = SINGLE_TURN_MAX_S,
    merge_gap_s: float = MERGE_GAP_S,
) -> list[Turn]:
    """Turnos sin hablantes menores (absorbidos), fundidos y renumerados (ver modulo)."""
    valid = [t for t in turns if t.end > t.start]
    if not valid:
        return []
    totals = speech_by_speaker(valid)
    minor = speakers_to_absorb(
        valid, min_speaker_s=min_speaker_s, single_turn_max_s=single_turn_max_s
    )
    majors = sorted(set(totals) - minor)
    emb = embeddings or {}
    target: dict[str, str] = {}
    for sid in sorted(minor):
        if sid in emb and any(m in emb for m in majors):
            v = _unit(emb[sid])
            sims = {m: float(v @ _unit(emb[m])) for m in majors if m in emb}
            target[sid] = max(sorted(sims), key=lambda m: sims[m])
    major_turns = [t for t in valid if t.speaker not in minor]
    relabelled: list[Turn] = []
    for t in valid:
        if t.speaker not in minor:
            relabelled.append(t)
        elif t.speaker in target:
            relabelled.append(Turn(target[t.speaker], t.start, t.end))
        else:
            relabelled.append(Turn(_nearest_turn_speaker(t, major_turns), t.start, t.end))
    return merge_and_renumber(relabelled, merge_gap_s)
