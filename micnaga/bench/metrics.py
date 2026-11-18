"""Metricas de calidad del banco de pruebas (spec 12): WER, DER y eco atribuido a «Yo».

Las referencias estan en el esquema de ``transcripcion.json`` (``reference.json``).

* **WER** (``jiwer``): texto de todos los segmentos ordenados por tiempo, en minusculas y sin
  puntuacion (se conservan los acentos).
* **DER** (``pyannote.metrics``): solo la pista remota (los hablantes distintos de ``me``), con
  ``collar = 0,25 s`` y el solapado puntuado; la correspondencia de etiquetas es la optima.
* **Eco como «Yo»**: fraccion de frases de otros (segmentos de referencia que no son ``me``) que
  aparecen en la hipotesis atribuidas a ``me`` (solape temporal >= 50 % de la frase y similitud de
  texto >= 0,6).
"""

from __future__ import annotations

import re

from micnaga.pipeline.stages.phrases import text_similarity
from micnaga.types import SPEAKER_ME, Segment, Transcript

DER_COLLAR_S = 0.25
ECHO_MIN_OVERLAP = 0.5
ECHO_MIN_SIMILARITY = 0.6


def normalize_words(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def transcript_text(t: Transcript) -> str:
    segments = sorted(t.segments, key=lambda s: (s.start, s.end))
    return normalize_words(" ".join(s.text for s in segments))


def wer(reference: Transcript, hypothesis: Transcript) -> float | None:
    """Tasa de error de palabras (0..1+); ``None`` si la referencia esta vacia."""
    import jiwer

    ref = transcript_text(reference)
    if not ref:
        return None
    return float(jiwer.wer(ref, transcript_text(hypothesis)))


def _annotation(segments: list[Segment]) -> object:
    from pyannote.core import Annotation
    from pyannote.core import Segment as PSegment

    ann = Annotation()
    for s in segments:
        if s.speaker != SPEAKER_ME and s.end > s.start:
            ann[PSegment(s.start, s.end)] = s.speaker
    return ann


def der(reference: Transcript, hypothesis: Transcript) -> float | None:
    """DER de la pista remota; ``None`` si la referencia no tiene voz remota."""
    from pyannote.core import Annotation
    from pyannote.core import Segment as PSegment
    from pyannote.metrics.diarization import DiarizationErrorRate

    ref = _annotation(reference.segments)
    hyp = _annotation(hypothesis.segments)
    assert isinstance(ref, Annotation) and isinstance(hyp, Annotation)
    if len(ref) == 0:
        return None
    end = max(
        reference.meeting.duration_s,
        max((s.end for s in [*reference.segments, *hypothesis.segments]), default=0.0),
    )
    metric = DiarizationErrorRate(collar=DER_COLLAR_S, skip_overlap=False)
    return float(metric(ref, hyp, uem=PSegment(0.0, end)))


def echo_as_me_rate(reference: Transcript, hypothesis: Transcript) -> float | None:
    """Fraccion de frases de otros que salen como ``me`` en la hipotesis (ver el modulo)."""
    others = [s for s in reference.segments if s.speaker != SPEAKER_ME]
    if not others:
        return None
    mine = [s for s in hypothesis.segments if s.speaker == SPEAKER_ME]
    hits = 0
    for ref in others:
        length = max(ref.end - ref.start, 1e-6)
        for hyp in mine:
            overlap = min(ref.end, hyp.end) - max(ref.start, hyp.start)
            if overlap / length >= ECHO_MIN_OVERLAP and (
                text_similarity(ref.text, hyp.text) >= ECHO_MIN_SIMILARITY
            ):
                hits += 1
                break
    return hits / len(others)
