"""«Grabar reunión presencial» en el pipeline: compuertas, umbral del micro (etapa 7) y origen."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from micnaga import gates
from micnaga.bench.fakes import FakeDiarizer, fake_deps_for_session, load_script
from micnaga.bench.synthetic import SyntheticSpec
from micnaga.capture import session as capture_session
from micnaga.config import GatesConfig
from micnaga.pipeline.engines.diar import room
from micnaga.pipeline.stages import s07_diarize
from micnaga.pipeline.stages.base import Deps
from micnaga.store.db import Database
from tests.pipeline.helpers import make_session, open_db, runner_for

STRICT = GatesConfig(min_duration_s=20.0, min_remote_speech_s=30.0, min_turns=6)


@pytest.fixture
def db() -> Database:
    database = open_db()
    yield database  # type: ignore[misc]
    database.close()


class CountingDiarizer(FakeDiarizer):
    def __init__(self, script: Any) -> None:
        super().__init__(script)
        self.calls = 0

    def diarize(self, audio, sr, n_speakers):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().diarize(audio, sr, n_speakers)


def counting_deps(session: Path) -> tuple[Deps, CountingDiarizer]:
    base = fake_deps_for_session(session)
    diar = CountingDiarizer(load_script(session))
    return Deps(**{**base.__dict__, "create_diarizer": lambda _c: diar}), diar


def voice_note(name: str) -> Path:
    """Solo habla el micro (sistema en silencio), sin confirmar: lo de una sala sin llamada."""
    return make_session(name, SyntheticSpec(kind="voice_note", duration_s=45.0))


# ------------------------------------------------------------------ marca y compuertas (puro)


def test_in_person_flag_comes_from_extra_or_the_folder_marker(tmp_path: Path) -> None:
    assert not capture_session.is_in_person(tmp_path, {})
    assert capture_session.is_in_person(tmp_path, {gates.IN_PERSON_KEY: True})
    assert not capture_session.is_in_person(tmp_path, {gates.IN_PERSON_KEY: "yes"})
    assert capture_session.mark_in_person(tmp_path / "s")
    assert capture_session.is_in_person(tmp_path / "s", None)  # cierre brusco: solo la marca


def test_apply_in_person_needs_a_capture_json_and_keeps_the_rest(tmp_path: Path) -> None:
    assert capture_session.apply_in_person(tmp_path) is False  # sin capture.json: no lanza
    session = voice_note("tmp-apply")
    before = capture_session.read_capture_json(session)
    assert capture_session.apply_in_person(session)
    after = capture_session.read_capture_json(session)
    assert after.extra[gates.IN_PERSON_KEY] is True
    assert {k: v for k, v in after.extra.items() if k != gates.IN_PERSON_KEY} == before.extra
    assert after.confirmed == before.confirmed and after.mic == before.mic


def test_in_person_only_needs_the_duration_gate() -> None:
    assert not gates.evaluate_gates(200, 0, 0, False, STRICT).passed
    assert gates.evaluate_gates(200, 0, 0, False, STRICT, in_person=True).passed
    r = gates.evaluate_gates(10, 0, 0, False, STRICT, in_person=True)
    assert not r.passed and r.gate == "duration"


# ------------------------------------------------------------------ pipeline completo


def test_in_person_session_without_remote_speech_is_transcribed(db: Database) -> None:
    session = voice_note("tmp-sala")
    assert capture_session.apply_in_person(session)
    result = runner_for(db=db, keep_work=True, rename=False).run(session)
    t = result.transcript
    assert result.status == "done" and t is not None, result.reason
    assert t.meeting.source == gates.SOURCE_IN_PERSON
    work = result.session_dir / "work"
    gate = json.loads((work / "01_gates.json").read_text("utf-8"))
    assert gate["passed"] and gate["in_person"] is True and gate["confirmed"] is True
    on_disk = json.loads((result.session_dir / "transcripcion.json").read_text("utf-8"))
    assert on_disk["meeting"]["source"] == "presencial"


def test_the_same_audio_without_the_flag_is_held_by_the_gates(db: Database) -> None:
    session = voice_note("tmp-nota-normal")
    result = runner_for(db=db, keep_work=True, rename=False).run(session)
    assert result.status == "discarded"


def test_marker_alone_is_enough_after_a_crash(db: Database) -> None:
    session = voice_note("tmp-sala-crash")
    assert capture_session.mark_in_person(session)  # capture.json sin extra.in_person
    result = runner_for(db=db, keep_work=True, rename=False).run(session)
    assert result.status == "done" and result.transcript is not None, result.reason
    assert result.transcript.meeting.source == gates.SOURCE_IN_PERSON


def test_in_person_always_tries_the_mic_diarization(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # el umbral normal, inalcanzable: solo el de presencial (5 s) deja diarizar el micro
    monkeypatch.setattr(room, "MIN_MIC_SPEECH_S", 1e9)
    assert s07_diarize.IN_PERSON_MIN_MIC_SPEECH_S == 5.0

    plain = make_session(
        "tmp-diar-normal", SyntheticSpec(kind="voice_note", duration_s=45.0, confirmed=True)
    )
    plain_deps, plain_diar = counting_deps(plain)
    result = runner_for(deps=plain_deps, db=db, keep_work=True, rename=False).run(plain)
    assert result.status == "done", result.reason
    info = json.loads((result.session_dir / "work" / "07_diarize.done.json").read_text("utf-8"))
    assert plain_diar.calls == 0 and info["info"]["diarizer"] is None  # micro sin diarizar

    sala = voice_note("tmp-diar-sala")
    capture_session.apply_in_person(sala)
    deps, diar = counting_deps(sala)
    result = runner_for(deps=deps, db=db, keep_work=True, rename=False).run(sala)
    assert result.status == "done", result.reason
    info = json.loads((result.session_dir / "work" / "07_diarize.done.json").read_text("utf-8"))
    assert diar.calls == 1  # el micro (no hay voz remota)
    assert info["info"]["in_person"] is True
    assert "skipped" not in info["info"]["mic"]
