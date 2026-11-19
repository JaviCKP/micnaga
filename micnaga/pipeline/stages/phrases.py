"""Logica pura de frases y hablantes compartida por las etapas 8-11 y por ``output``.

Sin dependencias pesadas (solo ``micnaga.types`` y ``rapidfuzz``).

Definiciones
------------
* **Frase** (etapa 8): palabras consecutivas del mismo hablante; se corta cuando cambia el
  hablante, cuando el hueco entre palabras es >= ``PHRASE_GAP_S`` (0,8 s) o cuando la palabra
  anterior termina en puntuacion de fin de frase (``. ? ! ...``).
* **Segmento / parrafo** (etapa 11): frases consecutivas del mismo hablante con hueco
  < ``merge_gap_s`` (1,5 s) se unen; un parrafo se parte, en un fin de frase, cuando ya dura
  ``MAX_PARAGRAPH_S`` (45 s) o ``MAX_PARAGRAPH_CHARS`` (700) caracteres.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

from rapidfuzz import fuzz

from micnaga.types import SPEAKER_ME, SPEAKER_ME_NAME, Segment, Speaker, Word

PHRASE_GAP_S = 0.8
MAX_PARAGRAPH_S = 45.0
MAX_PARAGRAPH_CHARS = 700
ECHO_TIME_TOLERANCE_S = 0.5
ECHO_PARTIAL_MIN_CHARS = 12
_SENTENCE_END = re.compile(r"[.?!…]['\")\]»]*$")


def ends_sentence(word: str) -> bool:
    return bool(_SENTENCE_END.search(word.strip()))


def words_text(words: Iterable[Word]) -> str:
    return " ".join(w.w for w in words)


def make_segment(speaker: str, words: Sequence[Word]) -> Segment:
    return Segment(
        start=words[0].s,
        end=max(w.e for w in words),
        speaker=speaker,
        text=words_text(words),
        words=list(words),
    )


def build_phrases(
    words_by_speaker: Iterable[tuple[str, Word]], *, gap_s: float = PHRASE_GAP_S
) -> list[Segment]:
    """Agrupa palabras (``(hablante, palabra)``) en frases; el resultado va ordenado por inicio.

    Las palabras de cada hablante se tratan por separado (los hablantes pueden solaparse en el
    tiempo: micro y sistema son pistas independientes).
    """
    per_speaker: dict[str, list[Word]] = {}
    for speaker, word in words_by_speaker:
        per_speaker.setdefault(speaker, []).append(word)
    phrases: list[Segment] = []
    for speaker, words in per_speaker.items():
        words.sort(key=lambda w: (w.s, w.e))
        current: list[Word] = []
        for w in words:
            if current and (w.s - current[-1].e >= gap_s or ends_sentence(current[-1].w)):
                phrases.append(make_segment(speaker, current))
                current = []
            current.append(w)
        if current:
            phrases.append(make_segment(speaker, current))
    phrases.sort(key=lambda s: (s.start, s.end))
    return phrases


def merge_segments(
    phrases: Sequence[Segment],
    *,
    merge_gap_s: float = 1.5,
    max_paragraph_s: float = MAX_PARAGRAPH_S,
    max_paragraph_chars: int = MAX_PARAGRAPH_CHARS,
) -> list[Segment]:
    """Une frases consecutivas del mismo hablante con hueco < ``merge_gap_s`` (ver modulo)."""
    out: list[Segment] = []
    for seg in sorted(phrases, key=lambda s: (s.start, s.end)):
        if out:
            prev = out[-1]
            gap = seg.start - prev.end
            long_enough = (
                prev.end - prev.start >= max_paragraph_s or len(prev.text) >= max_paragraph_chars
            )
            sentence_done = bool(prev.words) and ends_sentence(prev.words[-1].w)
            if (
                prev.speaker == seg.speaker
                and gap < merge_gap_s
                and not (long_enough and sentence_done)
            ):
                words = [*prev.words, *seg.words]
                text = words_text(words) if words else f"{prev.text} {seg.text}".strip()
                out[-1] = Segment(prev.start, max(prev.end, seg.end), prev.speaker, text, words)
                continue
        out.append(seg)
    return out


# ----------------------------------------------------------------------------- hablantes


def default_speaker_name(speaker_id: str) -> str:
    """``me`` -> ``Yo``; ``S2`` -> ``Hablante 2``; otro id -> ``Hablante <id>``."""
    if speaker_id == SPEAKER_ME:
        return SPEAKER_ME_NAME
    digits = re.sub(r"\D", "", speaker_id)
    return f"Hablante {digits or speaker_id}"


def speaker_sort_key(speaker_id: str) -> tuple[int, int, str]:
    if speaker_id == SPEAKER_ME:
        return (0, 0, "")
    digits = re.sub(r"\D", "", speaker_id)
    return (1, int(digits) if digits else 10**6, speaker_id)


def build_speakers(
    segments: Sequence[Segment], names: dict[str, tuple[str, float | None]] | None = None
) -> list[Speaker]:
    """Hablantes con al menos un segmento: ``me`` primero y despues ``S<n>`` por numero.

    ``me`` (Yo) solo aparece si habla: un micro callado (YouTube, reunion solo escuchada) no
    lo lista como participante.
    """
    names = names or {}
    ids = {s.speaker for s in segments}
    speakers: list[Speaker] = []
    for sid in sorted(ids, key=speaker_sort_key):
        if sid in names:
            name, match = names[sid]
            speakers.append(Speaker(sid, name, match))
        else:
            speakers.append(Speaker(sid, default_speaker_name(sid)))
    return speakers


# ----------------------------------------------------------------------------- eco (etapa 9)


def normalize_text(text: str) -> str:
    """Minusculas, sin acentos ni puntuacion, espacios simples."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", stripped).split())


def text_similarity(a: str, b: str) -> float:
    """Similitud 0..1 entre dos textos normalizados.

    ``ratio`` de rapidfuzz y, si el mas corto tiene >= ``ECHO_PARTIAL_MIN_CHARS`` caracteres,
    tambien ``partial_ratio`` (un eco puede ser un trozo de la frase remota).
    """
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    score = float(fuzz.ratio(na, nb))
    if min(len(na), len(nb)) >= ECHO_PARTIAL_MIN_CHARS:
        score = max(score, float(fuzz.partial_ratio(na, nb)))
    return score / 100.0


def drop_echo(
    phrases: Sequence[Segment],
    *,
    similarity: float = 0.8,
    tolerance_s: float = ECHO_TIME_TOLERANCE_S,
) -> tuple[list[Segment], list[Segment]]:
    """Etapa 9: descarta las frases de ``me`` que coinciden en el tiempo con voz remota y se le
    parecen >= ``similarity``. Devuelve ``(conservadas, descartadas)``.

    Cada frase de ``me`` se compara con (a) cada frase remota que solape su ventana
    ``[inicio - tolerancia, fin + tolerancia]`` y (b) el texto de las palabras remotas dentro de
    esa ventana (el eco puede trocearse distinto que el original).
    """
    remote = [p for p in phrases if p.speaker != SPEAKER_ME]
    kept: list[Segment] = []
    dropped: list[Segment] = []
    for p in phrases:
        if p.speaker != SPEAKER_ME:
            kept.append(p)
            continue
        lo, hi = p.start - tolerance_s, p.end + tolerance_s
        near = [r for r in remote if r.start < hi and r.end > lo]
        is_echo = False
        if near:
            if any(text_similarity(p.text, r.text) >= similarity for r in near):
                is_echo = True
            else:
                window_words = [w.w for r in near for w in r.words if w.e > lo and w.s < hi]
                if window_words and text_similarity(p.text, " ".join(window_words)) >= similarity:
                    is_echo = True
        (dropped if is_echo else kept).append(p)
    return kept, dropped
