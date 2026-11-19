"""Con el ``Recorder`` real sobre el backend sintetico: FLAC, capture.json y recuperacion."""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any

import pytest

from micnaga import paths
from micnaga.capture import Recorder
from micnaga.capture.session import read_capture_json, write_capture_json
from micnaga.capture.synthetic import demo_backend
from micnaga.integration.recovery import recover_and_enqueue
from micnaga.store.db import Database, open_database
from micnaga.watcher import MeetingInfo
from tests.app.fakes import Rig
from tests.watcher.helpers import CODE_A

pytestmark = pytest.mark.timeout(60)


def real_recorder(capture: Any) -> Recorder:
    return Recorder(capture, backend=demo_backend(), fsync=False)


def make_rig() -> Rig:
    rig = Rig()
    rig.app.controller._factory = real_recorder
    return rig


def test_confirmed_session_ends_as_flac_with_final_metadata_and_a_job() -> None:
    rig = make_rig()
    ctl = rig.ctl
    ctl.start_recording(provisional=True, meeting=MeetingInfo(title="Teams"))
    session = ctl.session_dir
    assert session is not None
    time.sleep(1.2)
    final = MeetingInfo(title="Revisión semanal", code=CODE_A)
    ctl.stop_and_finalize(confirmed=True, meeting=final)
    rig.settle()
    assert (session / paths.AUDIO_ME_FLAC).is_file()
    assert (session / paths.AUDIO_REMOTE_FLAC).is_file()
    assert not (session / paths.AUDIO_ME_WAV).exists()  # los WAV se borran tras verificar
    meta = read_capture_json(session)
    assert meta.confirmed is True  # el flag y el titulo se fijaron ANTES de parar
    assert meta.meet_title == "Revisión semanal" and meta.meet_code == CODE_A
    assert meta.extra["state"] == "finalized"
    assert meta.mic.file == paths.AUDIO_ME_FLAC
    jobs = rig.jobs()
    assert len(jobs) == 1 and Path(jobs[0].session_dir) == session


def test_provisional_session_is_queued_unconfirmed() -> None:
    rig = make_rig()
    rig.ctl.start_recording(provisional=True, meeting=MeetingInfo(title="Teams"))
    session = rig.ctl.session_dir
    assert session is not None
    time.sleep(0.8)
    rig.ctl.stop_and_finalize(confirmed=False, meeting=MeetingInfo(title="Teams"))
    rig.settle()
    assert read_capture_json(session).confirmed is False
    assert len(rig.jobs()) == 1


def test_no_with_the_real_recorder_is_held_and_the_mark_survives_stop() -> None:
    """Hallazgo 1 con el grabador real: la marca sobrevive a ``stop()`` (que reescribe
    ``capture.json``) y la sesion acaba apartada (FLAC), nunca en la cola."""
    from micnaga import holding

    rig = make_rig()
    rig.ctl.start_recording(provisional=True, meeting=MeetingInfo(title="Teams"))
    session = rig.ctl.session_dir
    assert session is not None
    time.sleep(0.5)
    rig.ctl.begin_discard(undo_s=8)
    assert holding.discard_reason(session) == holding.REASON_DECLINED
    rec = rig.ctl._session.recorder  # type: ignore[union-attr]
    rec.stop()  # p. ej. la app se cierra justo aqui: stop() reescribe capture.json
    assert holding.discard_reason(session) == holding.REASON_DECLINED
    db = open_database()
    try:
        assert recover_and_enqueue(rig.cfg, db) == []  # el arranque NO la encola...
    finally:
        db.close()
    held = holding.held_root(session.parent) / session.name
    assert not session.exists() and (held / paths.AUDIO_ME_FLAC).is_file()  # ...la aparta
    assert rig.jobs() == []


def test_private_pause_and_markers_reach_capture_json() -> None:
    rig = make_rig()
    rig.ctl.start_recording(provisional=False, meeting=MeetingInfo(title="Teams"))
    session = rig.ctl.session_dir
    assert session is not None
    time.sleep(0.4)
    assert rig.ctl.add_marker("importante")
    rig.ctl.pause_recording()
    time.sleep(0.4)
    rig.ctl.resume_recording()
    time.sleep(0.4)
    rig.ctl.stop_and_finalize(confirmed=True, meeting=MeetingInfo(title="Teams"))
    rig.settle()
    meta = read_capture_json(session)
    assert [m.label for m in meta.markers] == ["importante"]
    assert len(meta.pauses) == 1


# ------------------------------------------------------------------ recuperacion al arrancar


def _record(folder: Path, seconds: float = 0.8, *, confirmed: bool = False) -> Path:
    rec = Recorder(backend=demo_backend(), fsync=False)
    rec.start(folder, confirmed=confirmed, meet_title="Reunion rota")
    time.sleep(seconds)
    rec.stop()
    return folder


def _make_it_look_crashed(folder: Path) -> None:
    meta = read_capture_json(folder)
    extra = dict(meta.extra, state="recording", pid=2_000_000_000)  # un proceso que no existe
    write_capture_json(folder, dataclasses.replace(meta, extra=extra))


def test_boot_recovery_holds_an_unconfirmed_crashed_session(tmp_storage: Path) -> None:
    from micnaga import holding

    folder = _record(tmp_storage / "2026-11-18_1000_sin-si")
    _make_it_look_crashed(folder)
    db = open_database()
    try:
        assert recover_and_enqueue(Rig().cfg, db) == []
        assert db.list_jobs() == []
    finally:
        db.close()
    held = holding.held_root(tmp_storage) / folder.name
    assert (held / paths.AUDIO_ME_FLAC).is_file()
    assert holding.list_held(tmp_storage)[0].reason == holding.REASON_INTERRUPTED


def test_boot_recovery_enqueues_crashed_session(tmp_storage: Path) -> None:
    folder = _record(tmp_storage / "2026-11-18_1000_rota", confirmed=True)
    _make_it_look_crashed(folder)
    db = open_database()
    try:
        queued = recover_and_enqueue(Rig().cfg, db)
        assert queued == [folder]
        job = db.find_job(folder)
        assert job is not None
    finally:
        db.close()
    assert read_capture_json(folder).extra["state"] == "finalized"
    assert (folder / paths.AUDIO_ME_FLAC).is_file()


def test_boot_sweep_picks_up_stopped_orphans_but_not_known_or_done(tmp_storage: Path) -> None:
    orphan = _record(tmp_storage / "orphan", confirmed=True)
    known = _record(tmp_storage / "known", confirmed=True)
    done = _record(tmp_storage / "done", confirmed=True)
    (done / paths.TRANSCRIPT_JSON).write_text("{}", encoding="utf-8")
    db = open_database()
    try:
        db.enqueue_job(known)
        queued = recover_and_enqueue(Rig().cfg, db)
        assert queued == [orphan]
        assert db.find_job(orphan) is not None and db.find_job(done) is None
        assert len(db.list_jobs()) == 2
    finally:
        db.close()


def test_application_boot_recovers_and_kicks_the_worker(tmp_storage: Path) -> None:
    folder = _record(tmp_storage / "boot", confirmed=True)
    _make_it_look_crashed(folder)
    rig = Rig()
    rig.app._recover_at_boot = True
    rig.app.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (rig.spawned and rig.app.boot_queued):
            time.sleep(0.05)
        assert rig.app.boot_queued == [folder]
        assert rig.spawned
    finally:
        rig.app.shutdown(wait_s=10)
    db = Database(paths.db_path())
    try:
        assert db.find_job(folder) is not None
    finally:
        db.close()
