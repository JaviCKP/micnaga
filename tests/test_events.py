from __future__ import annotations

import json
from pathlib import Path

import pytest

from micnaga import events, paths
from micnaga.events import (
    EVENT_TYPES,
    Event,
    JobDiscarded,
    JobDone,
    JobFailed,
    JobProgress,
    JobStarted,
    UnknownEventError,
    decode_line,
    encode_line,
)
from micnaga.types import PIPELINE_STAGES, stage_name

SAMPLES: list[Event] = [
    JobStarted(job_id=1, session_dir="C:/m/a"),
    JobProgress(job_id=1, stage=6, stage_name="asr", fraction=0.5),
    JobDone(
        job_id=1, session_dir="C:/m/a", title="Revisión semanal", duration_s=2820.0, n_speakers=3
    ),
    JobFailed(job_id=2, session_dir="C:/m/b", error="boom"),
    JobDiscarded(job_id=3, session_dir="C:/m/c", reason="voz remota < 30 s"),
]


@pytest.mark.parametrize("event", SAMPLES, ids=lambda e: e.type)
def test_roundtrip(event: Event) -> None:
    assert Event.from_json(event.to_json()) == event
    assert decode_line(encode_line(event)) == event
    assert Event.from_dict(event.to_dict()) == event


def test_spec_event_names_present() -> None:
    assert {"job_progress", "job_done", "job_failed"} <= set(EVENT_TYPES)


def test_wire_format_is_one_compact_utf8_line() -> None:
    line = encode_line(SAMPLES[2])
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    assert "Revisión".encode() in line  # sin escapes \u
    data = json.loads(line)
    assert data["type"] == "job_done"
    assert data["n_speakers"] == 3


def test_unknown_and_malformed() -> None:
    with pytest.raises(UnknownEventError):
        Event.from_json('{"type": "nope"}')
    with pytest.raises(UnknownEventError):
        Event.from_json("[1, 2]")
    with pytest.raises(UnknownEventError):
        Event.from_json("not json")
    with pytest.raises(UnknownEventError):
        Event.from_json('{"type": "job_done", "job_id": 1}')  # faltan campos


def test_extra_fields_are_ignored() -> None:
    e = Event.from_dict(
        {"type": "job_failed", "job_id": 1, "session_dir": "x", "error": "e", "new": 1}
    )
    assert e == JobFailed(job_id=1, session_dir="x", error="e")


def test_stage_names() -> None:
    assert len(PIPELINE_STAGES) == 11
    assert stage_name(0) == "queued"
    assert stage_name(1) == "gates"
    assert stage_name(6) == "asr"
    assert stage_name(11) == "assemble"
    assert stage_name(99) == "assemble"


def test_pipe_name_depends_on_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "a"))
    a = events.pipe_name()
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "b"))
    b = events.pipe_name()
    assert a != b
    assert a.startswith("\\\\.\\pipe\\micnaga-events-")
