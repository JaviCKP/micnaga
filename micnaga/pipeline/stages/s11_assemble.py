"""Etapa 11: montaje y salida (spec 8.11 y 9).

Une las frases consecutivas del mismo hablante con hueco < ``processing.merge_gap_s`` (1,5 s),
forma los parrafos (``phrases.merge_segments``), arma el ``Transcript`` (hablantes con nombre,
marcadores de ``capture.json``, motores usados) y escribe ``transcripcion.json`` (fuente de
verdad), ``transcripcion.md`` y ``transcripcion.srt`` en la carpeta de la sesion.

El renombrado a ``YYYY-MM-DD_HHMM_slug`` y el indexado en SQLite los hace el ``runner`` en su
cierre (son lo ultimo que se toca del sistema de ficheros).
"""

from __future__ import annotations

from typing import Any

from micnaga.pipeline import output
from micnaga.pipeline.stages.base import StageContext, StageResult
from micnaga.pipeline.stages.phrases import build_speakers, merge_segments
from micnaga.types import (
    EnginesInfo,
    MeetingInfo,
    Segment,
    Transcript,
    Word,
)

DEFAULT_TITLE = "Reunión"
TRANSCRIPT_OUTPUT = "../transcripcion.json"
"""Relativo a ``work/``: la marca solo cuenta si la transcripcion existe."""


def _round_segment(s: Segment) -> Segment:
    words = [Word(w.w, round(w.s, 3), round(w.e, 3), round(w.p, 3)) for w in s.words]
    return Segment(round(s.start, 3), round(s.end, 3), s.speaker, s.text, words)


def build_transcript(ctx: StageContext) -> Transcript:
    phrases = [Segment.from_dict(d) for d in ctx.read_json("10_phrases.json")]
    names_raw: dict[str, dict[str, Any]] = ctx.read_json("10_names.json")
    names = {
        sid: (str(v["name"]), float(v["voiceprint_match"]) if "voiceprint_match" in v else None)
        for sid, v in names_raw.items()
    }
    segments = [
        _round_segment(s)
        for s in merge_segments(phrases, merge_gap_s=ctx.config.processing.merge_gap_s)
    ]
    meta = ctx.meta
    align = ctx.stage_info("align")
    diar_name = ctx.stage_info("diarize").get("diarizer")
    aec_name = ctx.stage_info("aec").get("aec")
    return Transcript(
        meeting=MeetingInfo(
            title=(meta.meet_title or "").strip() or DEFAULT_TITLE,
            start=meta.started_at,
            duration_s=round(float(align.get("duration_s", 0.0)), 3),
            source="meet",
            meet_code=meta.meet_code,
            output_device=meta.output_kind,
        ),
        speakers=build_speakers(segments, names),
        segments=segments,
        markers=list(meta.markers),
        engines=EnginesInfo(
            asr=str(ctx.stage_info("asr").get("engine", "unknown")),
            diarization=None if diar_name is None else str(diar_name),
            aec=None if aec_name is None else str(aec_name),
        ),
    )


def run(ctx: StageContext) -> StageResult:
    transcript = build_transcript(ctx)
    output.write_transcript(ctx.session, transcript)
    return StageResult(
        [TRANSCRIPT_OUTPUT],
        {"segments": len(transcript.segments), "speakers": len(transcript.speakers)},
    )
