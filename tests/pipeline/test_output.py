"""Salidas (spec seccion 9): pruebas «golden» de JSON, Markdown y SRT."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from micnaga import paths
from micnaga.pipeline import output
from micnaga.store.db import Database
from micnaga.types import (
    EnginesInfo,
    Marker,
    MeetingInfo,
    Segment,
    Speaker,
    Transcript,
    Word,
)


def spec_transcript() -> Transcript:
    """El ejemplo de la seccion 9 de la spec, mas un pasaje en el minuto 12 con el marcador."""
    return Transcript(
        meeting=MeetingInfo(
            title="Revisión semanal",
            start="2026-11-18T10:30:00+01:00",
            duration_s=2820,
            source="meet",
            meet_code="abc-defg-hij",
            output_device="headphones",
        ),
        speakers=[
            Speaker("me", "Yo"),
            Speaker("S1", "Laura", 0.84),
            Speaker("S2", "Hablante 2"),
        ],
        segments=[
            Segment(
                12.3,
                14.1,
                "me",
                "Buenos días, ¿me oís bien?",
                [Word("Buenos", 12.3, 12.6, 0.97)],
            ),
            Segment(15.0, 18.0, "S1", "Perfecto. Empezamos por lo del despliegue...", []),
            Segment(722.0, 727.5, "S2", "Decidir proveedor antes del viernes, por favor.", []),
        ],
        markers=[Marker(724.0, "Decidir proveedor antes del viernes")],
        engines=EnginesInfo(asr="parakeet-tdt-0.6b-v3-int8", diarization="diarize-x.y"),
    )


SPEC_MD_HEAD = [
    "# Revisión semanal — 18/11/2026 10:30 (47 min)",
    "",
    "Participantes: Yo, Laura, Hablante 2",
    "",
    "## Marcadores",
    "- [12:04] Decidir proveedor antes del viernes",
    "",
    "## Transcripción",
    "**[00:00:12] Yo:** Buenos días, ¿me oís bien?",
    "**[00:00:15] Laura:** Perfecto. Empezamos por lo del despliegue...",
]


def test_markdown_matches_the_spec_sample() -> None:
    md = output.render_markdown(spec_transcript())
    lines = md.split("\n")
    assert lines[: len(SPEC_MD_HEAD)] == SPEC_MD_HEAD
    # el pasaje marcado sale resaltado
    assert lines[len(SPEC_MD_HEAD)] == (
        "**[00:12:02] Hablante 2:** ==Decidir proveedor antes del viernes, por favor.=="
    )
    assert md.endswith("\n") and "\r" not in md


def test_markdown_participants_skip_speakers_without_segments() -> None:
    t = spec_transcript()
    # transcripciones antiguas listaban siempre a «Yo» aunque el micro no hablara
    silent_me = Transcript(
        t.meeting, t.speakers, [s for s in t.segments if s.speaker != "me"], [], t.engines
    )
    md = output.render_markdown(silent_me)
    assert "Participantes: Laura, Hablante 2\n" in md and "Yo" not in md
    empty = Transcript(t.meeting, [], [], [], t.engines)
    assert "Participantes: —\n" in output.render_markdown(empty)


def test_json_matches_the_spec_schema() -> None:
    data = spec_transcript().to_dict()
    assert list(data) == ["version", "meeting", "speakers", "segments", "markers", "engines"]
    assert data["version"] == 1
    assert data["meeting"] == {
        "title": "Revisión semanal",
        "start": "2026-11-18T10:30:00+01:00",
        "duration_s": 2820,
        "source": "meet",
        "meet_code": "abc-defg-hij",
        "output_device": "headphones",
    }
    assert data["speakers"][:2] == [
        {"id": "me", "name": "Yo"},
        {"id": "S1", "name": "Laura", "voiceprint_match": 0.84},
    ]
    assert data["segments"][0] == {
        "start": 12.3,
        "end": 14.1,
        "speaker": "me",
        "text": "Buenos días, ¿me oís bien?",
        "words": [{"w": "Buenos", "s": 12.3, "e": 12.6, "p": 0.97}],
    }
    assert data["markers"] == [{"t": 724.0, "label": "Decidir proveedor antes del viernes"}]
    assert data["engines"] == {"asr": "parakeet-tdt-0.6b-v3-int8", "diarization": "diarize-x.y"}


def test_srt_golden() -> None:
    t = spec_transcript()
    srt = output.render_srt(t)
    assert srt == (
        "1\n00:00:12,300 --> 00:00:14,100\nYo: Buenos días, ¿me oís bien?\n"
        "\n"
        "2\n00:00:15,000 --> 00:00:18,000\nLaura: Perfecto. Empezamos por lo del despliegue...\n"
        "\n"
        "3\n00:12:02,000 --> 00:12:07,500\n"
        "Hablante 2: Decidir proveedor antes del viernes, por favor.\n"
    )


def test_long_segments_are_split_into_cues_with_word_times() -> None:
    words = [Word(f"palabra{i}", i * 0.5, i * 0.5 + 0.4) for i in range(40)]
    seg = Segment(0, 19.9, "me", " ".join(w.w for w in words), words)
    t = Transcript(
        MeetingInfo("x", "2026-11-18T10:30:00+01:00", 20),
        [Speaker("me", "Yo")],
        [seg],
        [],
        EnginesInfo("fake"),
    )
    srt = output.render_srt(t)
    blocks = srt.strip().split("\n\n")
    assert len(blocks) >= 3
    assert blocks[0].splitlines()[2].startswith("Yo: palabra0")
    assert not blocks[1].splitlines()[2].startswith("Yo:")
    for b in blocks:
        text = " ".join(b.splitlines()[2:])
        assert len(text) <= output.SRT_MAX_CUE_CHARS + 5


def test_time_formats() -> None:
    assert output.format_hms(3725.9) == "01:02:05"
    assert output.format_mmss(724) == "12:04"
    assert output.format_mmss(4325) == "72:05"  # pasada la hora sigue contando minutos
    assert output.format_srt_time(3725.9996) == "01:02:06,000"


def test_marker_edge_cases() -> None:
    t = spec_transcript()
    t2 = Transcript(
        t.meeting,
        t.speakers,
        t.segments,
        [Marker(4325.0, ""), Marker(16.0, "En medio"), Marker(500.0, "En un hueco")],
        t.engines,
    )
    md = output.render_markdown(t2)
    assert "- [72:05] Marcador" in md
    assert "- [00:16] En medio" in md
    lines = md.split("\n")
    # 16 s cae dentro de Laura; 500 s esta mas cerca del segmento de las 722 s que del de las 15 s
    assert "==Perfecto. Empezamos por lo del despliegue...==" in md
    assert next(ln for ln in lines if ln.startswith("**[00:12:02]")).endswith("por favor.==")
    # sin marcadores no hay seccion
    plain = output.render_markdown(Transcript(t.meeting, t.speakers, t.segments, [], t.engines))
    assert "## Marcadores" not in plain and "==" not in plain


def test_marker_segment_index_prefers_containing_then_nearest_previous() -> None:
    segs = [Segment(0, 10, "me", "a"), Segment(20, 30, "me", "b")]
    assert output.marker_segment_index(segs, 5) == 0
    assert output.marker_segment_index(segs, 15) == 0  # empate: el anterior
    assert output.marker_segment_index(segs, 16) == 1
    assert output.marker_segment_index([], 1) is None


def test_header_uses_the_iso_offset_not_the_machine_timezone() -> None:
    t = spec_transcript()
    other = Transcript(
        MeetingInfo("Prueba", "2026-01-05T23:45:00-08:00", 20 * 60),
        t.speakers,
        [],
        [],
        t.engines,
    )
    assert output.render_markdown(other).startswith("# Prueba — 05/01/2026 23:45 (20 min)\n")


# ------------------------------------------------------------------ E/S y ediciones


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    out = tmp_path / "2026-11-18_1030_revision-semanal"
    output.write_transcript(out, spec_transcript())
    return out


def test_write_transcript_writes_the_three_files(folder: Path) -> None:
    assert json.loads((folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8")) == (
        spec_transcript().to_dict()
    )
    assert (folder / paths.TRANSCRIPT_MD).read_text(encoding="utf-8") == output.render_markdown(
        spec_transcript()
    )
    assert (folder / paths.TRANSCRIPT_SRT).read_text(encoding="utf-8") == output.render_srt(
        spec_transcript()
    )
    assert not list(folder.glob(".tmp-*"))


def test_rename_speaker_regenerates_md_srt_and_index(folder: Path) -> None:
    db = Database(folder.parent / "test.db")
    try:
        output.index_meeting(db, folder, spec_transcript())
        updated = output.rename_speaker(folder, "S2", "Marta", db=db)
        assert [s.name for s in updated.speakers] == ["Yo", "Laura", "Marta"]
        md = (folder / paths.TRANSCRIPT_MD).read_text(encoding="utf-8")
        assert "Participantes: Yo, Laura, Marta" in md
        assert "**[00:12:02] Marta:**" in md and "Hablante 2" not in md
        assert "Marta: Decidir" in (folder / paths.TRANSCRIPT_SRT).read_text(encoding="utf-8")
        data = json.loads((folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8"))
        assert [s["name"] for s in data["speakers"]] == ["Yo", "Laura", "Marta"]
        record = db.get_meeting(folder.name)
        assert record is not None and [s.name for s in record.speakers][-1] == "Marta"
        assert [h.speaker for h in db.search("proveedor")] == ["S2"]
    finally:
        db.close()


def test_rename_speaker_validation(folder: Path) -> None:
    with pytest.raises(KeyError):
        output.rename_speaker(folder, "S9", "X", reindex=False)
    with pytest.raises(ValueError):
        output.rename_speaker(folder, "S1", "  ", reindex=False)


def test_regenerate_from_json_after_manual_text_edit(folder: Path) -> None:
    file = folder / paths.TRANSCRIPT_JSON
    data = json.loads(file.read_text(encoding="utf-8"))
    data["segments"][0]["text"] = "Buenas tardes a todos"
    data["segments"][0]["words"] = []
    file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (folder / paths.TRANSCRIPT_MD).unlink()
    (folder / paths.TRANSCRIPT_SRT).unlink()
    output.regenerate_from_json(folder)
    assert "**[00:00:12] Yo:** Buenas tardes a todos" in (folder / paths.TRANSCRIPT_MD).read_text(
        encoding="utf-8"
    )
    assert "Yo: Buenas tardes a todos" in (folder / paths.TRANSCRIPT_SRT).read_text(
        encoding="utf-8"
    )


def test_merge_speakers_relabels_and_joins_adjacent_segments(tmp_path: Path) -> None:
    def seg(sp: str, s: float, e: float, text: str) -> Segment:
        return Segment(s, e, sp, text, [Word(text, s, e)])

    t = Transcript(
        MeetingInfo("Reunión", "2026-11-18T10:30:00+01:00", 60),
        [Speaker("me", "Yo"), Speaker("S1", "Laura"), Speaker("S2", "Hablante 2")],
        [
            seg("S1", 0, 2, "Hola"),
            seg("S2", 2.5, 4, "qué tal"),  # la diarizacion partio a una persona en dos
            seg("me", 5, 6, "Bien"),
            seg("S2", 20, 22, "Adiós"),
        ],
        [],
        EnginesInfo("fake"),
    )
    out = tmp_path / "m"
    output.write_transcript(out, t)
    merged = output.merge_speakers(out, "S1", "S2", reindex=False)
    assert [s.id for s in merged.speakers] == ["me", "S1"]
    assert [(s.speaker, s.text) for s in merged.segments] == [
        ("S1", "Hola qué tal"),
        ("me", "Bien"),
        ("S1", "Adiós"),
    ]
    md = (out / paths.TRANSCRIPT_MD).read_text(encoding="utf-8")
    assert "Participantes: Yo, Laura" in md and "Hablante 2" not in md
    with pytest.raises(ValueError):
        output.merge_speakers(out, "S1", "me", reindex=False)
    with pytest.raises(ValueError):
        output.merge_speakers(out, "S1", "S1", reindex=False)
    with pytest.raises(KeyError):
        output.merge_speakers(out, "S1", "S7", reindex=False)
