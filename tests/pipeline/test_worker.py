"""Worker (MIC-08): cola en proceso aparte, prioridad baja, un solo worker, eventos por tuberia."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from micnaga import cli, paths
from micnaga.bench.fakes import fake_deps_for_session
from micnaga.bench.synthetic import SyntheticSpec
from micnaga.config import save_config
from micnaga.events import Event, JobProgress, JobStarted, decode_line
from micnaga.logging_setup import shutdown_logging
from micnaga.pipeline import worker
from micnaga.pipeline.runner import ENV_FAKE_ENGINES, PipelineRunner
from micnaga.store.db import Database, JobState
from tests.pipeline.helpers import make_session, open_db, relaxed_config


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    yield
    shutdown_logging()
    logging.getLogger("micnaga").propagate = True


@pytest.fixture
def db() -> Iterator[Database]:
    database = open_db()
    yield database
    database.close()


def enqueue(db: Database, session: Path) -> int:
    return db.enqueue_job(session).id


# ------------------------------------------------------------------ bucle en el proceso


def test_loop_processes_pending_jobs_and_recovers_the_orphan(db: Database) -> None:
    good1 = make_session("tmp-1")
    good2 = make_session("tmp-2")
    trap = make_session("tmp-3", SyntheticSpec(kind="voice_note", duration_s=45.0))
    orphan = make_session("tmp-4")
    ids = [enqueue(db, s) for s in (orphan, good1, good2, trap)]
    claimed = db.claim_next_job()  # el huerfano: se quedo "processing" al morir el worker
    assert claimed is not None and claimed.id == ids[0]

    events: list[Event] = []
    handled = worker.run_worker_loop(
        relaxed_config(), db, once=True, deps=fake_deps_for_session, emit=events.append
    )
    assert handled == 4
    states = [db.get_job(i).state for i in ids]
    assert states == [JobState.DONE, JobState.DONE, JobState.DONE, JobState.DISCARDED]
    assert len(db.list_meetings()) == 3
    assert len(db.discard_entries()) == 1
    assert not trap.exists()
    assert {type(e) for e in events} >= {JobStarted, JobProgress}


def test_loop_exits_immediately_when_idle_with_once(db: Database) -> None:
    assert worker.run_worker_loop(relaxed_config(), db, once=True) == 0


def test_loop_without_once_waits_and_stops_on_event(db: Database) -> None:
    stop = threading.Event()
    result: list[int] = []
    t = threading.Thread(
        target=lambda: result.append(
            worker.run_worker_loop(
                relaxed_config(), db2(), poll_interval_s=0.05, deps=fake_deps_for_session, stop=stop
            )
        )
    )
    t.start()
    time.sleep(0.2)
    session = make_session("late")
    enqueue(db, session)
    deadline = time.time() + 20
    while time.time() < deadline and db.count_jobs()[JobState.DONE] == 0:
        time.sleep(0.1)
    stop.set()
    t.join(10)
    assert not t.is_alive() and result == [1]
    assert db.count_jobs()[JobState.DONE] == 1


def db2() -> Database:
    return open_db()


def test_an_unexpected_error_fails_the_job_and_the_loop_continues(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = make_session("a"), make_session("b")
    ia, ib = enqueue(db, a), enqueue(db, b)
    real_run = PipelineRunner.run

    def flaky(self: PipelineRunner, session: Path, job=None):  # type: ignore[no-untyped-def]
        if session.name == "a":
            raise RuntimeError("bug imprevisto")
        return real_run(self, session, job)

    monkeypatch.setattr(PipelineRunner, "run", flaky)
    handled = worker.run_worker_loop(relaxed_config(), db, once=True, deps=fake_deps_for_session)
    assert handled == 2
    assert db.get_job(ia).state is JobState.FAILED and "bug imprevisto" in (
        db.get_job(ia).error or ""
    )
    assert db.get_job(ib).state is JobState.DONE


# ------------------------------------------------------------------ hilos, prioridad, mutex


def test_thread_limits_are_exported(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in worker.THREAD_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    worker.apply_thread_limits(3)
    assert all(os.environ[v] == "3" for v in worker.THREAD_ENV_VARS)
    worker.apply_thread_limits(0)
    assert os.environ["OMP_NUM_THREADS"] == "1"


def test_low_priority_is_below_normal() -> None:
    import psutil

    proc = psutil.Process()
    before = proc.nice()
    try:
        assert worker.set_low_priority()
        assert proc.nice() == psutil.BELOW_NORMAL_PRIORITY_CLASS
    finally:
        proc.nice(before)


def test_worker_modules_do_not_import_heavy_libraries() -> None:
    code = (
        "import sys, micnaga.pipeline.cli, micnaga.pipeline.worker\n"
        "bad = [m for m in ('numpy', 'scipy', 'soundfile', 'onnxruntime') if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


def test_worker_lock_is_exclusive(tmp_home: Path) -> None:
    assert not worker.is_worker_running()
    first, second = worker.WorkerLock(), worker.WorkerLock()
    assert first.acquire()
    try:
        assert worker.is_worker_running()
        assert not second.acquire()
    finally:
        first.release()
    assert not worker.is_worker_running()
    assert second.acquire()
    second.release()


def test_worker_command_and_frozen_build(monkeypatch: pytest.MonkeyPatch) -> None:
    assert worker.worker_command(("--once",))[-4:] == ["-m", "micnaga", "worker", "--once"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert worker.worker_command() == [sys.executable, "worker"]


def test_spawn_worker_does_nothing_if_one_is_running(tmp_home: Path) -> None:
    with worker.WorkerLock() as lock:
        assert lock.acquire()
        assert worker.spawn_worker() is None


# ------------------------------------------------------------------ eventos por la tuberia


def test_sink_drops_events_silently_when_nobody_listens() -> None:
    sink = worker.PipeEventSink(rf"\\.\pipe\micnaga-nadie-{uuid.uuid4().hex}")
    for i in range(500):  # mas que la cola: no debe bloquear ni lanzar
        sink(JobStarted(i, "x"))
    sink.close()


def test_sink_delivers_json_lines_to_a_listening_ui() -> None:
    import win32file
    import win32pipe

    name = rf"\\.\pipe\micnaga-test-{uuid.uuid4().hex}"
    handle = win32pipe.CreateNamedPipe(
        name,
        win32pipe.PIPE_ACCESS_INBOUND,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT,
        1,
        65536,
        65536,
        0,
        None,
    )
    received = bytearray()

    def server() -> None:
        win32pipe.ConnectNamedPipe(handle, None)
        try:
            while received.count(b"\n") < 2:
                _hr, data = win32file.ReadFile(handle, 4096)
                received.extend(data)
        except Exception:
            pass

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    sink = worker.PipeEventSink(name)
    sink(JobStarted(7, "carpeta"))
    sink(JobProgress(7, 3, "aec", 0.25))
    thread.join(10)
    sink.close()
    win32file.CloseHandle(handle)
    lines = bytes(received).splitlines()
    assert [type(decode_line(ln)) for ln in lines] == [JobStarted, JobProgress]
    assert decode_line(lines[1]) == JobProgress(7, 3, "aec", 0.25)


# ------------------------------------------------------------------ CLI y subproceso


def fast_config() -> None:
    save_config(relaxed_config())


def test_process_command_foreground(
    db: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fast_config()
    monkeypatch.setenv(ENV_FAKE_ENGINES, "1")
    session = make_session("tmp-cli")
    assert cli.main(["process", str(session)]) == 0
    assert "listo:" in capsys.readouterr().out
    assert db.get_meeting("2026-11-18_1030_revision-semanal") is not None

    trap = make_session("tmp-trap", SyntheticSpec(kind="voice_note", duration_s=45.0))
    assert cli.main(["process", str(trap)]) == 3
    assert not trap.exists()
    assert cli.main(["process", str(trap)]) == 2  # ya no existe: uso incorrecto


def test_process_enqueue_only_adds_a_pending_job(
    db: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    session = make_session("tmp-q")
    assert cli.main(["process", "--enqueue", str(session)]) == 0
    jobs = db.list_jobs()
    assert len(jobs) == 1 and jobs[0].state is JobState.PENDING
    assert session.is_dir()


def run_worker_subprocess(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, ENV_FAKE_ENGINES: "1"}
    return subprocess.run(
        [sys.executable, "-m", "micnaga", "worker", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def test_worker_subprocess_smoke_with_once(db: Database) -> None:
    fast_config()
    session = make_session("tmp-sub")
    job_id = enqueue(db, session)
    proc = run_worker_subprocess("--once")
    assert proc.returncode == 0, proc.stderr
    job = db.get_job(job_id)
    assert job.state is JobState.DONE and job.stage == 11
    assert db.get_meeting("2026-11-18_1030_revision-semanal") is not None
    assert (
        paths.meetings_root() / "2026-11-18_1030_revision-semanal" / "transcripcion.md"
    ).is_file()
    assert (paths.logs_dir() / "worker.log").is_file()


def test_second_worker_exits_without_touching_the_queue(db: Database) -> None:
    fast_config()
    session = make_session("tmp-held")
    job_id = enqueue(db, session)
    with worker.WorkerLock() as lock:
        assert lock.acquire()
        proc = run_worker_subprocess("--once")
    assert proc.returncode == 0 and "otro worker" in proc.stderr
    assert db.get_job(job_id).state is JobState.PENDING


def test_spawn_worker_launches_a_detached_process(db: Database) -> None:
    fast_config()
    session = make_session("tmp-spawn")
    job_id = enqueue(db, session)
    child = worker.spawn_worker(("--once",), env={ENV_FAKE_ENGINES: "1"})
    assert child is not None
    assert child.wait(timeout=90) == 0
    assert db.get_job(job_id).state is JobState.DONE
