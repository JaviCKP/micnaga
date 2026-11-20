"""Runner de punta a punta con motores falsos (sin modelos)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from micnaga import paths
from micnaga.bench.fakes import FakeAsr, FakeDiarizer, fake_deps_for_session, load_script
from micnaga.bench.synthetic import SyntheticSpec
from micnaga.config import Config, save_config
from micnaga.events import (
    Event,
    JobDiscarded,
    JobDone,
    JobFailed,
    JobProgress,
    JobStarted,
)
from micnaga.pipeline import output
from micnaga.pipeline.runner import FINALIZE_FILE, PipelineRunner
from micnaga.pipeline.stages import base as stages_base
from micnaga.pipeline.stages import s07_diarize, s10_names
from micnaga.pipeline.stages.base import Deps, Stage, StageContext
from micnaga.registry import EngineUnavailableError
from micnaga.store import voiceprints as vp
from micnaga.store.db import Database, JobState
from micnaga.store.voiceprints import VoiceprintStore
from micnaga.types import PIPELINE_STAGES, CaptureMeta, Transcript, Turn
from tests.pipeline.helpers import (
    SimulatedCrash,
    make_session,
    open_db,
    relaxed_config,
    runner_for,
)

FINAL_NAME = "2026-11-18_1030_revision-semanal"


@pytest.fixture
def db() -> Database:
    database = open_db()
    yield database  # type: ignore[misc]
    database.close()


def claim(db: Database, session: Path):  # type: ignore[no-untyped-def]
    db.enqueue_job(session)
    job = db.claim_next_job()
    assert job is not None
    return job


# ------------------------------------------------------------------ camino feliz


def test_end_to_end_headphones_meeting(db: Database) -> None:
    session = make_session()
    events: list[Event] = []
    runner = runner_for(db=db, emit=events.append)
    job = claim(db, session)
    result = runner.run(session, job)

    assert result.status == "done", result.reason
    final = result.session_dir
    assert final == session.parent / FINAL_NAME and final.is_dir() and not session.exists()
    for name in (
        paths.TRANSCRIPT_JSON,
        paths.TRANSCRIPT_MD,
        paths.TRANSCRIPT_SRT,
        paths.AUDIO_ME_FLAC,
        paths.AUDIO_REMOTE_FLAC,
        paths.CAPTURE_JSON,
    ):
        assert (final / name).is_file(), name
    assert not (final / paths.WORK_DIRNAME).exists()

    transcript = output.read_transcript(final)
    script = load_script(final)
    me_texts = [u["text"] for u in script if u["speaker"] == "me"]
    assert [s.text for s in transcript.segments if s.speaker == "me"] == me_texts
    assert {s.id for s in transcript.speakers} == {"me", "S1", "S2"}
    assert transcript.meeting.title == "Revisión semanal"
    assert transcript.meeting.meet_code == "abc-defg-hij"
    assert transcript.meeting.output_device == "headphones"
    assert transcript.meeting.duration_s == pytest.approx(45.0, abs=0.1)
    assert [m.label for m in transcript.markers] == ["Decidir proveedor"]
    assert transcript.engines.asr == "fake-asr" and transcript.engines.aec is None
    times = [s.start for s in transcript.segments]
    assert times == sorted(times)

    md = (final / paths.TRANSCRIPT_MD).read_text(encoding="utf-8")
    assert md.startswith("# Revisión semanal — 18/11/2026 10:30 (1 min)\n")
    assert "## Marcadores" in md and "==" in md

    # SQLite: trabajo terminado, carpeta nueva y reunion indexada
    done = db.get_job(job.id)
    assert done.state is JobState.DONE and done.stage == 11
    assert Path(done.session_dir) == final
    meeting = db.get_meeting(FINAL_NAME)
    assert meeting is not None and meeting.job_id == job.id
    assert db.search("proveedor")

    # eventos
    assert isinstance(events[0], JobStarted)
    progress = [e for e in events if isinstance(e, JobProgress)]
    fractions = [e.fraction for e in progress]
    assert fractions == sorted(fractions) and 0 <= fractions[0] < fractions[-1] <= 1
    assert {e.stage_name for e in progress} == set(PIPELINE_STAGES)
    assert isinstance(events[-1], JobDone) and events[-1].n_speakers == 3
    assert events[-1].title == "Revisión semanal"


def test_capture_title_names_the_transcript_and_the_final_folder(db: Database) -> None:
    # como ``micnaga record --title Prueba-podcast``: la carpeta ya lleva el slug del titulo
    name = "2026-11-18_1030_prueba-podcast"
    session = make_session(name, SyntheticSpec(duration_s=45.0, title="Prueba-podcast"))
    result = runner_for(db=db).run(session)
    assert result.status == "done", result.reason
    assert result.session_dir == session  # sin renombrar a ``..._reunion``
    transcript = output.read_transcript(result.session_dir)
    assert transcript.meeting.title == "Prueba-podcast"
    md = (result.session_dir / paths.TRANSCRIPT_MD).read_text(encoding="utf-8")
    assert md.startswith("# Prueba-podcast — ")
    # una carpeta con otro nombre se renombra al slug del titulo, nunca al titulo por defecto
    other = make_session("tmp-other", SyntheticSpec(duration_s=45.0, title="Prueba-podcast"))
    renamed = runner_for(db=db).run(other)
    assert renamed.session_dir.name == f"{name}-2"


def test_silent_mic_does_not_list_me_as_participant(db: Database) -> None:
    # como el podcast real: solo habla la pista del sistema y el micro esta callado
    spec = SyntheticSpec(kind="youtube", duration_s=45.0, confirmed=True)
    result = runner_for(db=db).run(make_session("yt", spec))
    t = result.transcript
    assert result.status == "done" and t is not None, result.reason
    assert all(s.speaker != "me" for s in t.segments)
    assert "me" not in {s.id for s in t.speakers}
    assert {s.id for s in t.speakers} == {s.speaker for s in t.segments}
    md = (result.session_dir / paths.TRANSCRIPT_MD).read_text(encoding="utf-8")
    participants = next(line for line in md.splitlines() if line.startswith("Participantes:"))
    assert "Yo" not in participants


def test_second_meeting_with_the_same_title_gets_a_suffix(db: Database) -> None:
    first = runner_for(db=db).run(make_session("a"))
    second = runner_for(db=db).run(make_session("b"))
    assert first.session_dir.name == FINAL_NAME
    assert second.session_dir.name == f"{FINAL_NAME}-2"


def test_stage_times_and_markers_are_recorded(db: Database) -> None:
    session = make_session()
    result = runner_for(db=db, keep_work=True, rename=False).run(session)
    assert result.status == "done"
    assert list(result.stage_times) == list(PIPELINE_STAGES)
    work = session / paths.WORK_DIRNAME
    markers = sorted(work.glob("*.done.json"))
    assert [m.name for m in markers] == [
        f"{i:02d}_{n}.done.json" for i, n in enumerate(PIPELINE_STAGES, 1)
    ]
    data = json.loads(markers[5].read_text(encoding="utf-8"))
    assert data["stage"] == "asr" and data["info"]["engine"] == "fake-asr"
    assert data["outputs"] == ["06_words_me.json", "06_words_remote.json"]
    assert (work / "02_mic.wav").is_file() and (work / "04_system.wav").is_file()
    assert not (work / "03_mic_aec.wav").exists()  # auriculares: sin AEC


# ------------------------------------------------------------------ idempotencia / reanudacion


def test_rerun_skips_every_finished_stage(db: Database) -> None:
    session = make_session()
    first = runner_for(db=db, keep_work=True, rename=False).run(session)
    second = runner_for(db=db, keep_work=True, rename=False).run(session)
    assert second.status == "done"
    assert second.skipped == list(PIPELINE_STAGES)
    assert second.transcript == first.transcript


def test_removing_a_marker_reruns_that_stage_and_the_following_ones(db: Database) -> None:
    session = make_session()
    runner_for(db=db, keep_work=True, rename=False).run(session)
    (session / paths.WORK_DIRNAME / "06_asr.done.json").unlink()
    result = runner_for(db=db, keep_work=True, rename=False).run(session)
    assert result.skipped == list(PIPELINE_STAGES[:5])
    assert all(result.stage_times[n] > 0 for n in PIPELINE_STAGES[5:])


@pytest.mark.parametrize("crash_at", range(1, 12))
def test_resume_after_crash_at_stage(db: Database, crash_at: int) -> None:
    session = make_session("crash")
    fresh = runner_for(db=db, rename=False).run(make_session("fresh"))
    assert fresh.transcript is not None

    fired: list[int] = []

    def crash(stage: Stage) -> None:
        if stage.index == crash_at and not fired:
            fired.append(stage.index)
            raise SimulatedCrash

    job = claim(db, session)
    with pytest.raises(SimulatedCrash):
        runner_for(db=db, on_stage_start=crash, keep_work=True).run(session, job)
    assert db.get_job(job.id).state is JobState.PROCESSING
    assert db.get_job(job.id).stage == crash_at

    # reinicio del worker: el trabajo huerfano vuelve a la cola y se reanuda
    assert [j.id for j in db.requeue_stale()] == [job.id]
    again = db.claim_next_job()
    assert again is not None and again.id == job.id
    result = runner_for(db=db).run(Path(again.session_dir), again)
    assert result.status == "done", result.reason
    assert result.skipped == list(PIPELINE_STAGES[: crash_at - 1])
    assert result.transcript is not None
    assert result.transcript.to_dict() == fresh.transcript.to_dict()
    assert db.get_job(job.id).state is JobState.DONE


def test_crash_in_the_middle_of_asr_leaves_no_marker_and_resumes(db: Database) -> None:
    session = make_session()
    base = fake_deps_for_session(session)
    calls = {"n": 0}

    class Dying(FakeAsr):
        def transcribe(self, audio, sr):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 2:
                raise SimulatedCrash
            return super().transcribe(audio, sr)

    dying = Deps(**{**base.__dict__, "create_asr": lambda _c: Dying(load_script(session))})
    with pytest.raises(SimulatedCrash):
        runner_for(deps=dying, keep_work=True, rename=False).run(session)
    work = session / paths.WORK_DIRNAME
    assert (work / "05_vad.done.json").is_file() and not (work / "06_asr.done.json").exists()
    assert not list(work.glob("06_words*"))  # nada a medias (escritura atomica)
    result = runner_for(db=db, rename=False).run(session)
    assert result.status == "done" and result.skipped == list(PIPELINE_STAGES[:5])


# ------------------------------------------------------------------ descarte


def test_discarded_session_is_deleted_and_only_date_and_reason_are_logged(db: Database) -> None:
    session = make_session("tmp-nota", SyntheticSpec(kind="voice_note", duration_s=45.0))
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("micnaga")
    handler = Capture(level=logging.DEBUG)
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    events: list[Event] = []
    try:
        job = claim(db, session)
        result = runner_for(db=db, emit=events.append).run(session, job)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    assert result.status == "discarded" and result.reason == "voz remota 0 s < 5 s"
    assert not session.exists()
    stored = db.get_job(job.id)
    assert stored.state is JobState.DISCARDED and stored.session_dir == f"discarded:{job.id}"
    entries = db.discard_entries()
    assert len(entries) == 1  # discard_job ya anota; no se duplica
    assert entries[0][1] == "voz remota 0 s < 5 s"
    assert len(entries[0][0]) == 10 and entries[0][0].startswith("20")  # solo una fecha
    assert db.list_meetings() == []
    discarded = [e for e in events if isinstance(e, JobDiscarded)]
    assert len(discarded) == 1 and "tmp-nota" not in discarded[0].session_dir
    blob = " ".join(r.getMessage() for r in records) + " ".join(
        str(a) for r in records for a in (r.args if isinstance(r.args, tuple) else ())
    )
    assert "tmp-nota" not in blob and "Revisión" not in blob and str(session) not in blob
    assert "voz remota 0 s" in blob


def test_discard_without_a_job_logs_once_and_without_a_db_does_not_fail(db: Database) -> None:
    session = make_session("tmp-1", SyntheticSpec(kind="voice_note", duration_s=45.0))
    result = runner_for(db=db).run(session)
    assert result.status == "discarded" and not session.exists()
    assert len(db.discard_entries()) == 1
    other = make_session("tmp-2", SyntheticSpec(kind="voice_note", duration_s=45.0))
    assert runner_for(db=None).run(other).status == "discarded"
    assert not other.exists()


def test_short_confirmed_session_is_discarded_by_duration() -> None:
    session = make_session(
        "tmp-c", SyntheticSpec(kind="voice_note", duration_s=15.0, confirmed=True)
    )
    result = runner_for().run(session)
    assert result.status == "discarded" and result.reason == "duracion 15 s < 20 s"


def test_user_restored_short_session_is_transcribed_not_held_again() -> None:
    """P2-b: «Recuperar y transcribir» de una sesion corta no la devuelve a «Descartadas»."""
    from micnaga import holding

    session = make_session(
        "tmp-short", SyntheticSpec(kind="voice_note", duration_s=15.0, confirmed=True)
    )
    root = session.parent
    assert runner_for().run(session).status == "discarded"
    assert [h.name for h in holding.list_held(root)] == ["tmp-short"]
    restored = holding.restore_held(root, "tmp-short")
    result = runner_for(rename=False, keep_work=True).run(restored)
    assert result.status == "done", result.reason
    assert holding.list_held(root) == []
    gates = json.loads((restored / "work" / "01_gates.json").read_text(encoding="utf-8"))
    assert gates["passed"] and gates["reason"] == "recuperada por el usuario"


@pytest.mark.parametrize("enabled", [True, False])
def test_done_job_marks_the_acta_as_pending_only_with_secretaria(
    db: Database, enabled: bool
) -> None:
    """P2-c: la marca duradera se escribe al terminar (antes de ``finish_job``)."""
    session = make_session("tmp-mark", SyntheticSpec(kind="voice_note", confirmed=True))
    cfg = relaxed_config()
    cfg.secretaria.enabled = enabled
    job = claim(db, session)
    result = runner_for(config=cfg, db=db).run(session, job)
    assert result.status == "done"
    assert (result.session_dir / paths.ANALYSIS_PENDING).is_file() is enabled


def test_confirmed_session_skips_vad_gates() -> None:
    # solo micro (nota de voz) pero confirmada: la compuerta 1 es la unica que cuenta
    session = make_session("tmp-conf", SyntheticSpec(kind="voice_note", confirmed=True))
    result = runner_for(rename=False, keep_work=True).run(session)
    assert result.status == "done"
    gates = json.loads((session / "work" / "01_gates.json").read_text(encoding="utf-8"))
    assert gates["confirmed"] and gates["passed"]


# ------------------------------------------------------------------ fallo y reintento


def test_failure_keeps_files_and_a_retry_resumes(db: Database) -> None:
    session = make_session()
    base = fake_deps_for_session(session)

    def broken(_c: Config) -> object:
        raise RuntimeError("boom")

    bad = Deps(**{**base.__dict__, "create_asr": broken})
    events: list[Event] = []
    job = claim(db, session)
    result = runner_for(deps=bad, db=db, emit=events.append).run(session, job)
    assert result.status == "failed" and "RuntimeError: boom" in (result.reason or "")
    stored = db.get_job(job.id)
    assert stored.state is JobState.FAILED and "boom" in (stored.error or "")
    assert session.is_dir() and (session / "work" / "05_vad.done.json").is_file()
    failed = [e for e in events if isinstance(e, JobFailed)]
    assert len(failed) == 1 and "boom" in failed[0].error

    db.retry_job(job.id)
    retry = db.claim_next_job()
    assert retry is not None
    ok = runner_for(db=db).run(session, retry)
    assert ok.status == "done" and ok.skipped == list(PIPELINE_STAGES[:5])


def test_missing_capture_json_fails_cleanly(db: Database) -> None:
    session = make_session()
    (session / paths.CAPTURE_JSON).unlink()
    job = claim(db, session)
    result = runner_for(db=db).run(session, job)
    assert result.status == "failed" and "capture.json" in (result.reason or "")
    assert db.get_job(job.id).state is JobState.FAILED


def test_missing_session_folder_fails(db: Database, tmp_storage: Path) -> None:
    ghost = tmp_storage / "no-existe"
    job = claim(db, ghost)
    assert runner_for(db=db).run(ghost, job).status == "failed"


# ------------------------------------------------------------------ eco, AEC y diarizador


def test_speakers_session_runs_aec_and_the_text_layer_removes_the_echo(db: Database) -> None:
    spec = SyntheticSpec(duration_s=45.0, output_kind="speakers", echo=True, seed=1)
    session = make_session("echo", spec)
    result = runner_for(db=db, keep_work=True, rename=False).run(session)
    assert result.status == "done"
    work = session / paths.WORK_DIRNAME
    assert (work / "03_mic_aec.wav").is_file()
    marker = json.loads((work / "09_echo_dedup.done.json").read_text(encoding="utf-8"))
    assert marker["info"]["dropped"] > 0
    t = result.transcript
    assert t is not None and t.engines.aec == "fake-aec" and t.meeting.output_device == "speakers"
    me_texts = [u["text"] for u in load_script(session) if u["speaker"] == "me"]
    assert [s.text for s in t.segments if s.speaker == "me"] == me_texts


def test_no_aec_available_degrades_without_failing(db: Database) -> None:
    spec = SyntheticSpec(duration_s=45.0, output_kind="speakers", echo=True)
    session = make_session("noaec", spec)
    base = fake_deps_for_session(session)

    def unavailable(_c: Config) -> object:
        raise EngineUnavailableError("no aec installed")

    deps = Deps(**{**base.__dict__, "create_aec": unavailable})
    result = runner_for(deps=deps, db=db).run(session)
    assert result.status == "done" and result.transcript is not None
    assert result.transcript.engines.aec is None
    me_texts = [u["text"] for u in load_script(result.session_dir) if u["speaker"] == "me"]
    assert [s.text for s in result.transcript.segments if s.speaker == "me"] == me_texts


def test_no_diarizer_labels_the_remote_track_as_one_speaker(db: Database) -> None:
    session = make_session()
    base = fake_deps_for_session(session)

    def unavailable(_c: Config) -> object:
        raise EngineUnavailableError("diarize not installed")

    deps = Deps(**{**base.__dict__, "create_diarizer": unavailable})
    result = runner_for(deps=deps, db=db).run(session)
    t = result.transcript
    assert result.status == "done" and t is not None
    assert t.engines.diarization is None
    assert {s.id for s in t.speakers} == {"me", "S1"}
    assert {s.speaker for s in t.segments} == {"me", "S1"}


def test_models_are_closed_after_each_stage(db: Database) -> None:
    session = make_session()
    base = fake_deps_for_session(session)
    created: list[object] = []

    def asr(_c: Config) -> object:
        engine = FakeAsr(load_script(session))
        created.append(engine)
        return engine

    deps = Deps(**{**base.__dict__, "create_asr": asr})
    runner_for(deps=deps, db=db).run(session)
    assert len(created) == 1 and created[0].closed  # type: ignore[attr-defined]


# ------------------------------------------------------------------ glosario y huellas


def test_glossary_is_applied_to_the_final_text(db: Database, tmp_home: Path) -> None:
    paths.ensure_dir(tmp_home)
    (tmp_home / "glossary.txt").write_text("Proveedor\nkubernetes: kubernetes\n", encoding="utf-8")
    result = runner_for(db=db).run(make_session())
    assert result.transcript is not None
    texts = " ".join(s.text for s in result.transcript.segments)
    assert "Proveedor" in texts and "proveedor" not in texts


def test_voiceprint_names_speakers_when_enabled(db: Database, tmp_home: Path) -> None:
    cfg = relaxed_config()
    cfg.privacy.voiceprints_enabled = True
    save_config(cfg)
    store = VoiceprintStore(threshold=0.75)
    laura = np.full(8, 0.05, dtype=np.float32)
    laura[1] = 1.0  # el embedding que el diarizador falso da a S1
    store.save("Laura", laura, engine="fake-diarizer")
    session = make_session()
    deps = fake_deps_for_session(session)
    deps.voiceprints = store
    result = runner_for(config=cfg, deps=deps, db=db).run(session)
    t = result.transcript
    assert t is not None
    named = {s.id: s for s in t.speakers}
    assert named["S1"].name == "Laura" and named["S1"].voiceprint_match is not None
    assert named["S1"].voiceprint_match > 0.99
    assert named["S2"].name == "Hablante 2" and named["S2"].voiceprint_match is None
    assert "**[" in (result.session_dir / paths.TRANSCRIPT_MD).read_text(encoding="utf-8")
    assert (result.session_dir / "speaker_embeddings.dpapi").is_file()

    # renombrar y aprender la huella (MIC-11)
    output.rename_speaker(result.session_dir, "S2", "Marta", db=db, learn_voiceprint=True)
    assert store.list() == ["Laura", "Marta"]


class GhostDiarizer(FakeDiarizer):
    """``FakeDiarizer`` que ademas inventa un hablante con una sola palabra al final (podcast)."""

    def __init__(self, script: Any) -> None:
        super().__init__(script)
        self.embedded: list[set[str]] = []

    def diarize(self, audio, sr, n_speakers):  # type: ignore[no-untyped-def]
        turns = super().diarize(audio, sr, n_speakers)
        last = max(t.end for t in turns)
        return [*turns, Turn("S3", last + 0.3, last + 0.9)]

    def embed(self, audio, sr, turns):  # type: ignore[no-untyped-def]
        self.embedded.append({t.speaker for t in turns})
        return super().embed(audio, sr, turns)


def test_ghost_speaker_is_absorbed_before_voiceprints(db: Database) -> None:
    session = make_session()
    cfg = relaxed_config()
    cfg.privacy.voiceprints_enabled = True
    base = fake_deps_for_session(session)
    ghost = GhostDiarizer(load_script(session))
    deps = Deps(**{**base.__dict__, "create_diarizer": lambda _c: ghost})
    result = runner_for(config=cfg, deps=deps, db=db, keep_work=True, rename=False).run(session)
    t = result.transcript
    assert result.status == "done" and t is not None, result.reason
    assert {s.id for s in t.speakers} == {"me", "S1", "S2"}  # sin «Hablante 3»
    info = diarize_marker(result.session_dir)["info"]
    assert info["n_speakers_raw"] == 3 and info["n_speakers"] == 2
    assert info["absorbed"] == ["S3"] and info["embedder"] == "fake-diarizer"
    turns = json.loads((result.session_dir / "work" / "07_turns.json").read_text("utf-8"))
    assert {x["speaker"] for x in turns} == {"S1", "S2"}
    # la huella guardada se calcula con los turnos ya filtrados: nunca la de un fantasma
    assert ghost.embedded[-1] == {"S1", "S2"}


def test_voiceprints_stay_off_by_default(db: Database) -> None:
    result = runner_for(db=db).run(make_session())
    assert result.transcript is not None
    assert all(s.voiceprint_match is None for s in result.transcript.speakers)
    assert not (result.session_dir / "speaker_embeddings.dpapi").exists()


# ------------------------------------------------------------------ huellas: nada en claro en disco

EMB_BASE = 0.0371337  # valores raros: si aparecen en claro en un fichero, es una fuga
EMB_PEAK = 0.9876543
LEAK_TOKENS = (b"0.0371337", b"0.987654")


class DistinctDiarizer(FakeDiarizer):
    """Como ``FakeDiarizer`` pero con embeddings de valores reconocibles."""

    def embed(self, audio, sr, turns):  # type: ignore[no-untyped-def]
        return {
            sid: np.where(v == 1.0, EMB_PEAK, EMB_BASE).astype(np.float32)
            for sid, v in super().embed(audio, sr, turns).items()
        }


def voiceprint_setup(session: Path) -> tuple[Config, Deps]:
    cfg = relaxed_config()
    cfg.privacy.voiceprints_enabled = True
    store = VoiceprintStore(threshold=0.75)
    laura = np.full(8, EMB_BASE, dtype=np.float32)
    laura[1] = EMB_PEAK  # el embedding que ``DistinctDiarizer`` da a S1
    store.save("Laura", laura, engine="fake-diarizer")
    base = fake_deps_for_session(session)
    script = load_script(session)
    deps = Deps(
        **{
            **base.__dict__,
            "create_diarizer": lambda _c: DistinctDiarizer(script),
            "voiceprints": store,
        }
    )
    return cfg, deps


def assert_no_plaintext_vectors(root: Path) -> None:
    files = [p for p in root.rglob("*") if p.is_file()]
    assert files
    for file in files:
        data = file.read_bytes()
        for token in LEAK_TOKENS:
            assert token not in data, f"plaintext speaker vector in {file.relative_to(root)}"
    assert not list(root.rglob(s07_diarize.LEGACY_EMBEDDINGS))


def spy_disk_writes(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Captura todo lo que las etapas y el almacen de huellas mandan al disco (temporales incl.)."""
    written: list[bytes] = []
    real_bytes, real_vp = stages_base.atomic_write_bytes, vp._atomic_write

    def bytes_spy(path: Path, data: bytes) -> None:
        written.append(data)
        real_bytes(path, data)

    def vp_spy(path: Path, data: bytes) -> None:
        written.append(data)
        real_vp(path, data)

    monkeypatch.setattr(stages_base, "atomic_write_bytes", bytes_spy)
    monkeypatch.setattr(vp, "_atomic_write", vp_spy)
    return written


def diarize_marker(session: Path) -> dict:  # type: ignore[type-arg]
    marker = session / paths.WORK_DIRNAME / "07_diarize.done.json"
    return json.loads(marker.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def named_speakers(transcript: Transcript | None) -> dict[str, str]:
    assert transcript is not None
    return {s.id: s.name for s in transcript.speakers}


def test_speaker_embeddings_are_written_encrypted_from_the_first_byte(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session()
    cfg, deps = voiceprint_setup(session)
    written = spy_disk_writes(monkeypatch)
    result = runner_for(config=cfg, deps=deps, db=db, keep_work=True, rename=False).run(session)
    assert result.status == "done", result.reason
    assert written  # el espia vio las escrituras (temporales incluidos)
    for data in written:
        assert all(token not in data for token in LEAK_TOKENS), "plaintext vector written"
    assert_no_plaintext_vectors(session)
    work = session / paths.WORK_DIRNAME
    assert (work / s07_diarize.EMBEDDINGS).is_file()
    marker = diarize_marker(session)
    assert marker["info"]["has_embeddings"] is True
    assert s07_diarize.EMBEDDINGS in marker["outputs"]
    assert named_speakers(result.transcript)["S1"] == "Laura"
    # se puede descifrar y son los vectores del diarizador
    loaded = vp.read_protected_json(work / s07_diarize.EMBEDDINGS)
    assert np.isclose(loaded["S1"][1], EMB_PEAK) and np.isclose(loaded["S1"][0], EMB_BASE)


def test_failure_after_diarization_leaves_no_plaintext_vectors_and_retry_names(
    db: Database,
) -> None:
    session = make_session()
    cfg, deps = voiceprint_setup(session)

    def fail_at_assign(stage: Stage) -> None:
        if stage.index == 8:
            raise RuntimeError("boom after diarization")

    job = claim(db, session)
    failed = runner_for(config=cfg, deps=deps, db=db, on_stage_start=fail_at_assign).run(
        session, job
    )
    assert failed.status == "failed" and "boom" in (failed.reason or "")
    work = session / paths.WORK_DIRNAME
    assert (work / "07_diarize.done.json").is_file()
    assert (work / s07_diarize.EMBEDDINGS).is_file()  # se conserva para reintentar, cifrado
    assert_no_plaintext_vectors(session)

    db.retry_job(job.id)
    retry = db.claim_next_job()
    assert retry is not None
    ok = runner_for(config=cfg, deps=deps, db=db).run(session, retry)
    assert ok.status == "done", ok.reason
    assert ok.skipped == list(PIPELINE_STAGES[:7])
    assert named_speakers(ok.transcript)["S1"] == "Laura"


def test_legacy_plaintext_embeddings_are_deleted_and_diarization_reruns(db: Database) -> None:
    session = make_session()
    cfg, deps = voiceprint_setup(session)
    first = runner_for(config=cfg, deps=deps, keep_work=True, rename=False).run(session)
    assert first.status == "done", first.reason
    work = session / paths.WORK_DIRNAME
    # estado que dejaba una version antigua: embeddings en claro, listados en la marca de la 7
    (work / s07_diarize.EMBEDDINGS).unlink()
    legacy = work / s07_diarize.LEGACY_EMBEDDINGS
    legacy.write_text(json.dumps({"S1": [EMB_BASE, EMB_PEAK]}), encoding="utf-8")
    orphan = work / ".tmp-abc123.json"  # temporal huerfano de un cierre brusco
    orphan.write_text(json.dumps({"S2": [EMB_PEAK, EMB_BASE]}), encoding="utf-8")
    marker_file = work / "07_diarize.done.json"
    marker = json.loads(marker_file.read_text(encoding="utf-8"))
    marker["outputs"] = [s07_diarize.TURNS, s07_diarize.LEGACY_EMBEDDINGS]
    marker_file.write_text(json.dumps(marker), encoding="utf-8")

    result = runner_for(config=cfg, deps=deps, db=db, keep_work=True, rename=False).run(session)
    assert result.status == "done", result.reason
    assert result.skipped == list(PIPELINE_STAGES[:6])  # la diarizacion se repite
    assert not legacy.exists() and not orphan.exists()
    assert (work / s07_diarize.EMBEDDINGS).is_file()
    assert named_speakers(result.transcript)["S1"] == "Laura"
    assert_no_plaintext_vectors(session)


def test_names_stage_never_reads_legacy_plaintext_embeddings() -> None:
    session = make_session()
    cfg, deps = voiceprint_setup(session)
    runner_for(config=cfg, deps=deps, keep_work=True, rename=False).run(session)
    work = session / paths.WORK_DIRNAME
    (work / s07_diarize.EMBEDDINGS).unlink()
    legacy = work / s07_diarize.LEGACY_EMBEDDINGS
    legacy.write_text(json.dumps({"S1": [EMB_BASE, EMB_PEAK] + [EMB_BASE] * 6}), encoding="utf-8")
    meta = CaptureMeta.from_json((session / paths.CAPTURE_JSON).read_text(encoding="utf-8"))
    ctx = StageContext(session, cfg, deps, meta)
    assert s10_names.match_voiceprints(ctx, {"S1", "S2"}) == ({}, 0)
    assert not legacy.exists()


def test_dpapi_failure_skips_embeddings_and_never_writes_them_in_clear(
    db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    session = make_session()
    cfg, deps = voiceprint_setup(session)  # guarda la huella de Laura antes de romper DPAPI

    def no_dpapi(_data: bytes) -> bytes:
        raise vp.VoiceprintError("DPAPI encryption failed: test")

    monkeypatch.setattr(vp, "_protect", no_dpapi)
    with caplog.at_level(logging.WARNING):
        result = runner_for(config=cfg, deps=deps, db=db, keep_work=True, rename=False).run(session)
    assert result.status == "done", result.reason
    assert "speaker embeddings skipped" in caplog.text
    work = session / paths.WORK_DIRNAME
    assert not (work / s07_diarize.EMBEDDINGS).exists()
    marker = diarize_marker(session)
    assert marker["info"]["has_embeddings"] is False
    assert marker["outputs"] == [s07_diarize.TURNS]
    assert named_speakers(result.transcript)["S1"] == "Hablante 1"
    assert_no_plaintext_vectors(session)


def test_unreadable_encrypted_embeddings_skip_voiceprints_without_failing(db: Database) -> None:
    session = make_session()
    cfg, deps = voiceprint_setup(session)
    runner_for(config=cfg, deps=deps, keep_work=True, rename=False).run(session)
    work = session / paths.WORK_DIRNAME
    (work / s07_diarize.EMBEDDINGS).write_bytes(b"not a dpapi blob")
    (work / "10_names.done.json").unlink()
    result = runner_for(config=cfg, deps=deps, db=db, keep_work=True, rename=False).run(session)
    assert result.status == "done", result.reason
    assert named_speakers(result.transcript)["S1"] == "Hablante 1"


# ------------------------------------------------------------------ cierre


def test_crash_between_job_update_and_rename_is_recovered(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session("tmp-x")
    job = claim(db, session)

    def dying_rename(self: PipelineRunner, src: Path, dst: Path) -> None:
        raise SimulatedCrash

    with monkeypatch.context() as m:
        m.setattr(PipelineRunner, "_rename", dying_rename)
        with pytest.raises(SimulatedCrash):
            runner_for(db=db).run(session, job)
    final = session.parent / FINAL_NAME
    assert session.is_dir() and not final.exists()
    assert (session / "work" / FINALIZE_FILE).is_file()
    assert db.get_job(job.id).session_dir == str(final)  # ya apunta a la carpeta final

    db.requeue_stale()
    again = db.claim_next_job()
    assert again is not None
    result = runner_for(db=db).run(Path(again.session_dir), again)
    assert result.status == "done", result.reason
    assert final.is_dir() and not session.exists()
    assert db.get_job(job.id).state is JobState.DONE
    assert db.get_meeting(FINAL_NAME) is not None


def test_crash_after_the_rename_resumes_from_the_final_folder(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session("tmp-y")
    job = claim(db, session)

    def dying_index(*args: object, **kwargs: object) -> int:
        raise SimulatedCrash

    with monkeypatch.context() as m:
        m.setattr(output, "index_meeting", dying_index)
        with pytest.raises(SimulatedCrash):
            runner_for(db=db).run(session, job)
    final = session.parent / FINAL_NAME
    assert final.is_dir() and not session.exists()
    db.requeue_stale()
    again = db.claim_next_job()
    assert again is not None and Path(again.session_dir) == final
    result = runner_for(db=db).run(Path(again.session_dir), again)
    assert result.status == "done" and result.session_dir == final
    assert result.skipped == list(PIPELINE_STAGES)  # nada se reprocesa
    assert db.get_meeting(FINAL_NAME) is not None


def test_already_processed_folder_without_work_is_only_closed(db: Database) -> None:
    done = runner_for(db=db).run(make_session())
    assert not (done.session_dir / paths.WORK_DIRNAME).exists()
    transcript_before = (done.session_dir / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8")
    again = runner_for(db=db).run(done.session_dir)
    assert again.status == "done" and again.stage_times == {}
    assert (done.session_dir / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8") == (
        transcript_before
    )


def test_wav_leftovers_are_converted_to_flac(db: Database) -> None:
    import soundfile as sf

    session = make_session()
    for flac, wav in (
        (paths.AUDIO_ME_FLAC, paths.AUDIO_ME_WAV),
        (paths.AUDIO_REMOTE_FLAC, paths.AUDIO_REMOTE_WAV),
    ):
        data, sr = sf.read(str(session / flac), dtype="int16")
        sf.write(str(session / wav), data, sr, subtype="PCM_16")
        (session / flac).unlink()
    meta = json.loads((session / paths.CAPTURE_JSON).read_text(encoding="utf-8"))
    meta["mic"]["file"], meta["system"]["file"] = paths.AUDIO_ME_WAV, paths.AUDIO_REMOTE_WAV
    (session / paths.CAPTURE_JSON).write_text(json.dumps(meta), encoding="utf-8")
    result = runner_for(db=db).run(session)
    assert result.status == "done"
    final = result.session_dir
    assert (final / paths.AUDIO_ME_FLAC).is_file() and (final / paths.AUDIO_REMOTE_FLAC).is_file()
    assert not (final / paths.AUDIO_ME_WAV).exists()


def test_events_sink_failures_never_break_processing(db: Database) -> None:
    def broken(_e: Event) -> None:
        raise OSError("pipe closed")

    result = runner_for(db=db, emit=broken).run(claim_free(db))
    assert result.status == "done"


def claim_free(db: Database) -> Path:
    session = make_session("evt")
    db.enqueue_job(session)
    return session


def test_transcript_round_trips(db: Database) -> None:
    result = runner_for(db=db).run(make_session())
    assert result.transcript is not None
    on_disk = Transcript.from_json(
        (result.session_dir / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8")
    )
    assert on_disk == result.transcript
