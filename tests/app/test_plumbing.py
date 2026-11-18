"""Supervisor del worker, tuberia de eventos, biblioteca, modelos, higiene y arranque."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from micnaga import models, paths
from micnaga.config import Config
from micnaga.events import JobDiscarded, JobDone, JobFailed, JobProgress, JobStarted, encode_line
from micnaga.integration.events_server import EventsServer
from micnaga.integration.jobs import Finisher, WorkerSupervisor
from micnaga.integration.library_ops import AppLibraryOps, VoiceprintsDisabledError
from micnaga.pipeline.worker import PipeEventSink
from micnaga.store.db import Database, JobState, open_database
from micnaga.ui import fake
from micnaga.ui.contracts import ModelProgress
from tests.app.fakes import FakeProc, Rig

pytestmark = pytest.mark.timeout(60)


# ------------------------------------------------------------------------------ supervisor


class SupRig:
    def __init__(self, tmp: Path) -> None:
        self.db = Database(tmp / "jobs.db")
        self.now = 100.0
        self.running = False
        self.models = True
        self.spawned: list[FakeProc] = []
        self.busy: list[bool] = []

        def spawn() -> FakeProc | None:
            if self.running:
                return None
            p = FakeProc()
            self.spawned.append(p)
            return p

        self.sup = WorkerSupervisor(
            Config(),
            db_factory=lambda: self.db,
            spawn=spawn,  # type: ignore[arg-type]
            is_running=lambda: self.running,
            models_ready=lambda: self.models,
            on_busy=self.busy.append,
            clock=lambda: self.now,
            min_respawn_s=5.0,
        )

    def tick(self, advance: float = 10.0) -> None:
        self.now += advance
        self.sup.tick(self.db)


def test_supervisor_starts_the_worker_only_with_pending_jobs(tmp_path: Path) -> None:
    r = SupRig(tmp_path)
    r.tick()
    assert r.spawned == [] and r.busy[-1] is False
    r.db.enqueue_job(tmp_path / "a")
    r.tick()
    assert len(r.spawned) == 1 and r.busy[-1] is True


def test_supervisor_waits_for_models(tmp_path: Path) -> None:
    r = SupRig(tmp_path)
    r.models = False
    r.db.enqueue_job(tmp_path / "a")
    r.tick()
    assert r.spawned == []
    r.models = True
    r.tick()
    assert len(r.spawned) == 1


def test_supervisor_does_not_spawn_while_a_worker_runs(tmp_path: Path) -> None:
    r = SupRig(tmp_path)
    r.running = True
    r.db.enqueue_job(tmp_path / "a")
    r.tick()
    assert r.spawned == []


def test_supervisor_respawns_when_a_job_arrives_as_the_worker_exits(tmp_path: Path) -> None:
    """Carrera de ``--once``: el job se encola cuando el worker esta saliendo (spawn -> None)."""
    r = SupRig(tmp_path)
    r.db.enqueue_job(tmp_path / "a")
    r.tick()
    first = r.spawned[0]
    r.db.enqueue_job(tmp_path / "b")  # llega con el worker saliendo
    r.running = True
    r.tick()
    assert len(r.spawned) == 1  # no se puede lanzar otro: el mutex sigue cogido
    first.code = 0  # el worker termina
    r.running = False
    r.tick()
    assert len(r.spawned) == 2  # tras salir, se relanza para el job restante


def test_supervisor_backs_off_when_the_worker_crashes_at_start(tmp_path: Path) -> None:
    r = SupRig(tmp_path)
    r.db.enqueue_job(tmp_path / "a")
    r.tick()
    r.spawned[0].code = 1
    r.tick(advance=6.0)  # salio rapido con error: espera exponencial, no bucle
    assert len(r.spawned) == 1
    r.tick(advance=100.0)
    assert len(r.spawned) == 2


def test_supervisor_thread_kick_spawns(tmp_path: Path) -> None:
    r = SupRig(tmp_path)
    r.sup._interval_s = 30.0
    r.sup._db_factory = lambda: Database(tmp_path / "jobs.db")
    r.db.enqueue_job(tmp_path / "a")
    r.sup.start()
    try:
        deadline = time.monotonic() + 10
        while not r.spawned and time.monotonic() < deadline:
            r.sup.kick()
            time.sleep(0.05)
        assert r.spawned
    finally:
        r.sup.stop()


def test_worker_marks_in_the_db_are_seen_as_busy(tmp_path: Path) -> None:
    r = SupRig(tmp_path)
    job = r.db.enqueue_job(tmp_path / "a")
    r.db.claim_next_job()
    r.tick()
    assert r.busy[-1] is True
    r.db.finish_job(job.id)
    r.tick()
    assert r.busy[-1] is False
    assert r.db.count_jobs()[JobState.DONE] == 1


def test_finisher_runs_tasks_in_order_on_one_thread_and_reports_idle(tmp_path: Path) -> None:
    seen: list[tuple[str, int]] = []
    fin = Finisher(lambda: Database(tmp_path / "f.db"))
    for name in "abc":
        fin.submit(name, lambda _db, n=name: seen.append((n, threading.get_ident())))
    assert fin.wait_idle(10)
    assert [n for n, _ in seen] == ["a", "b", "c"]
    assert len({t for _, t in seen}) == 1
    fin.submit("boom", lambda _db: 1 / 0)  # un fallo no mata el hilo
    fin.submit("after", lambda _db: seen.append(("after", 0)))
    assert fin.stop(10)
    assert seen[-1][0] == "after"


# ------------------------------------------------------------------------------ tuberia de eventos


def _wait(cond: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not cond():
        time.sleep(0.02)
    assert cond()


def test_events_server_receives_worker_events_and_survives_reconnects() -> None:
    got: list[Any] = []
    name = rf"\\.\pipe\micnaga-test-{uuid.uuid4().hex}"
    srv = EventsServer(got.append, name)
    srv.start()
    try:
        for round_ in range(2):  # el worker se conecta, se va y vuelve
            sink = PipeEventSink(name)
            sink(JobStarted(round_, "x"))
            sink(JobProgress(round_, 3, "aec", 0.5))
            sink(JobDone(round_, "C:/m/carpeta", "Titulo", 61.0, 2))
            _wait(lambda n=round_: len(got) >= 3 * (n + 1))
            sink.close()
    finally:
        srv.stop()
    assert [type(e).__name__ for e in got[:3]] == ["JobStarted", "JobProgress", "JobDone"]


def test_events_server_drops_garbage_and_oversized_lines() -> None:
    import win32file

    got: list[Any] = []
    name = rf"\\.\pipe\micnaga-test-{uuid.uuid4().hex}"
    srv = EventsServer(got.append, name)
    srv.start()
    try:
        h = win32file.CreateFile(
            name, win32file.GENERIC_WRITE, 0, None, win32file.OPEN_EXISTING, 0, None
        )
        win32file.WriteFile(h, b"no es json\n")
        win32file.WriteFile(h, b'{"type":"nope"}\n')
        win32file.WriteFile(h, b"x" * 100_000 + b"\n")
        win32file.WriteFile(h, encode_line(JobFailed(1, "c", "boom")))
        win32file.CloseHandle(h)
        _wait(lambda: len(got) == 1)
    finally:
        srv.stop()
    assert isinstance(got[0], JobFailed)


def test_events_server_stops_promptly_while_idle_and_while_connected() -> None:
    import win32file

    name = rf"\\.\pipe\micnaga-test-{uuid.uuid4().hex}"
    idle = EventsServer(lambda _e: None, name)
    idle.start()
    t0 = time.monotonic()
    idle.stop()
    assert time.monotonic() - t0 < 2.0

    srv = EventsServer(lambda _e: None, name)
    srv.start()
    h = win32file.CreateFile(
        name, win32file.GENERIC_WRITE, 0, None, win32file.OPEN_EXISTING, 0, None
    )
    time.sleep(0.2)
    t0 = time.monotonic()
    srv.stop()  # con un cliente conectado y sin escribir
    assert time.monotonic() - t0 < 2.0
    win32file.CloseHandle(h)


def test_job_done_event_shows_the_ready_toast_and_job_failed_is_logged() -> None:
    rig = Rig()
    name = rf"\\.\pipe\micnaga-test-{uuid.uuid4().hex}"
    srv = EventsServer(rig.app.handle_event, name)
    srv.start()
    try:
        sink = PipeEventSink(name)
        sink(JobStarted(7, "C:/m/x"))
        _wait(lambda: rig.app._active_jobs == {7})
        sink(
            JobDone(
                7,
                str(paths.meetings_root() / "2026-11-18_1030_revision"),
                "Revisión semanal",
                2820.0,
                3,
            )
        )
        _wait(lambda: len(rig.toaster.toasts) == 1)
        sink(JobFailed(8, "C:/m/y", "kaput"))
        sink(JobDiscarded(9, "discarded:9", "gate duration"))
        _wait(lambda: rig.app.last_job_error == "kaput")
        sink.close()
    finally:
        srv.stop()
    toast = rig.toaster.toasts[0]
    assert "Revisión semanal" in " ".join(map(str, toast.text_fields))
    assert rig.app._active_jobs == set()
    assert rig.app.ui.tray_state() == "watching"  # type: ignore[attr-defined]


def test_processing_colour_follows_job_events() -> None:
    rig = Rig()
    rig.app.handle_event(JobStarted(1, "x"))
    assert rig.app.ui.tray_state() == "processing"  # type: ignore[attr-defined]
    rig.app.handle_event(JobDone(1, "x/y", "t", 1.0, 1))
    assert rig.app.ui.tray_state() == "watching"  # type: ignore[attr-defined]


# ------------------------------------------------------------------------------ biblioteca


@pytest.fixture
def library(tmp_storage: Path) -> list[str]:
    cfg = Config()
    cfg.privacy.voiceprints_enabled = True
    from micnaga.config import save_config

    save_config(cfg)
    return fake.build_demo_library(tmp_storage)


def test_library_ops_rename_merge_regenerate_and_viewer_audio(library: list[str]) -> None:
    from micnaga.pipeline import output

    ops = AppLibraryOps()
    folder = library[0]
    ops.rename_speaker(folder, "S2", "Marcos")
    root = paths.meetings_root()
    tr = output.read_transcript(root / folder)
    assert {s.id: s.name for s in tr.speakers}["S2"] == "Marcos"
    assert "Marcos" in (root / folder / paths.TRANSCRIPT_MD).read_text(encoding="utf-8")
    ops.merge_speakers(folder, "S1", "S2")
    assert {s.id for s in output.read_transcript(root / folder).speakers} == {"me", "S1"}
    (root / folder / paths.TRANSCRIPT_MD).unlink()
    ops.regenerate_from_json(folder)
    assert (root / folder / paths.TRANSCRIPT_MD).is_file()
    with open_database() as db:  # reindexado
        assert db.search("proveedor")
    # el visor sirve los FLAC por nombre canonico, no por capture.json
    from micnaga.ui.bridge import LibraryApi

    api = LibraryApi(
        db_factory=open_database,
        meetings_root=paths.meetings_root,
        audio_url=lambda f, t: f"/{f}/{t}",
        config=lambda: Config(),
        ops=ops,
    )
    res = api.get_meeting(folder)
    assert res["ok"] and res["audio"]["yo"] and res["audio"]["remoto"]


def test_library_ops_reject_paths_outside_the_root(library: list[str]) -> None:
    ops = AppLibraryOps()
    for bad in ("..", "../x", "does-not-exist", ""):
        with pytest.raises(FileNotFoundError):
            ops.rename_speaker(bad, "S1", "X")


def test_voiceprints_need_the_opt_in_and_never_save_me(library: list[str], tmp_home: Path) -> None:
    ops = AppLibraryOps()
    with pytest.raises(ValueError):
        ops.save_voiceprint(library[0], "me")
    with pytest.raises(LookupError):  # activadas, pero la reunion no guardo embeddings
        ops.save_voiceprint(library[0], "S1")
    off = AppLibraryOps(lambda: Config())
    with pytest.raises(VoiceprintsDisabledError):
        off.save_voiceprint(library[0], "S1")
    assert ops.list_voiceprints() == []


def test_voiceprint_roundtrip_list_and_delete(library: list[str]) -> None:
    import numpy as np

    from micnaga.store.voiceprints import VoiceprintStore, save_meeting_embeddings

    root = paths.meetings_root()
    save_meeting_embeddings(root / library[0], {"S1": np.ones(8, dtype=np.float32)}, engine="t")
    ops = AppLibraryOps()
    ops.rename_speaker(library[0], "S1", "Laura")
    ops.save_voiceprint(library[0], "S1")
    assert [v.name for v in ops.list_voiceprints()] == ["Laura"]
    ops.delete_voiceprint("Laura")
    assert VoiceprintStore().list() == []


# ------------------------------------------------------------------------------ modelos


def test_model_downloader_adapter_reports_per_model_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from micnaga.integration.application import make_model_downloader

    cfg = Config()

    def fake_download(config: Config, cb: Any, **_: Any) -> list[Path]:
        for gid in models.required_groups(config):
            files = models.MANIFEST[gid].files
            for f in files:
                cb(models.DownloadProgress(f.rel_path, 0, f.size, 0, 1, "download"))
                cb(models.DownloadProgress(f.rel_path, f.size, f.size, 1, 1, "done"))
        return []

    monkeypatch.setattr(models, "download_models", fake_download)
    events: list[ModelProgress] = []
    make_model_downloader(lambda: cfg)(events.append)
    required = {models.MANIFEST[g].title for g in models.required_groups(cfg)}
    assert {e.name for e in events} == required
    assert {e.name for e in events if e.state == "verified"} == required
    assert all(e.state != "verified" or e.done == e.total for e in events)
    assert events[0].state == "pending"


# ------------------------------------------------------------------------------ arranque / higiene


def test_import_hygiene_of_the_app_entry() -> None:
    """numpy entra (~90 ms) via micnaga.types/store.db; nada mas pesado."""
    code = (
        "import sys\n"
        "import micnaga.app\n"
        "import micnaga.integration.application, micnaga.integration.controller\n"
        "import micnaga.integration.jobs, micnaga.integration.events_server\n"
        "bad = [m for m in ('onnxruntime','torch','torchaudio','diarize','faster_whisper',"
        "'soundfile','scipy','sherpa_onnx','webview','sklearn') if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


def test_app_run_sets_blas_threads_before_anything_imports_numpy() -> None:
    code = (
        "import os, sys\n"
        "from micnaga import app\n"
        "app._limit_blas_threads()\n"
        "e = os.environ; print(e['OPENBLAS_NUM_THREADS'], e['OMP_NUM_THREADS'], "
        "'numpy' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.split() == ["1", "1", "False"]


def test_single_instance_guard() -> None:
    from micnaga.app import SingleInstance

    a, b = SingleInstance(), SingleInstance()
    assert a.acquire()
    try:
        assert not b.acquire()
    finally:
        a.release()
    assert b.acquire()
    b.release()


def test_headless_smoke_starts_and_stops_cleanly() -> None:
    before = {t.name for t in threading.enumerate()}
    rig = Rig()
    rig.app._start_events_server = True
    rig.app._recover_at_boot = True
    rig.app._warmup = True
    rig.app._start_watcher = True
    rig.app.start()
    assert rig.hotkeys is not None and rig.hotkeys.started
    assert rig.watcher is not None and rig.watcher.running
    time.sleep(0.3)
    rig.app.shutdown(wait_s=10)
    assert rig.hotkeys.stopped
    assert not rig.watcher.running
    rig.app.shutdown()  # idempotente
    deadline = time.monotonic() + 5
    leftover: set[str] = set()
    while time.monotonic() < deadline:
        leftover = {
            t.name
            for t in threading.enumerate()
            if t.name.startswith(("micnaga-worker-sup", "micnaga-events-srv", "micnaga-watcher"))
            and t.name not in before
        }
        if not leftover:
            break
        time.sleep(0.05)
    assert not leftover


def test_models_missing_opens_the_wizard_and_blocks_the_worker() -> None:
    rig = Rig()
    rig.models_ok = False
    rig.app.start()
    try:
        assert rig.host is not None and rig.host.has_window("wizard")
    finally:
        rig.app.shutdown(wait_s=5)


def test_first_run_done_never_touches_the_run_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """El instalador deja el arranque activo y el asistente no lo pregunta ni lo pisa."""
    from micnaga.ui import startup

    mem = startup.MemoryRegistry()
    monkeypatch.setattr(startup, "default_backend", lambda: mem)
    rig = Rig()
    rig.app.callbacks.on_first_run_done()
    assert mem.get("Micnaga") is None


def test_ensure_autostart_rewrites_missing_value_only_when_frozen_and_wanted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from micnaga.ui import startup

    mem = startup.MemoryRegistry()
    monkeypatch.setattr(startup, "default_backend", lambda: mem)
    monkeypatch.setattr(startup, "default_command", lambda **_: r"C:/M/micnaga.exe app")
    rig = Rig()
    rig.app._ensure_autostart()  # no congelada: no escribe
    assert mem.get("Micnaga") is None
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    rig.app._ensure_autostart()
    assert mem.get("Micnaga") == r"C:/M/micnaga.exe app"
    mem.set("Micnaga", "old")  # ruta obsoleta tras mover la instalacion
    rig.app._ensure_autostart()
    assert mem.get("Micnaga") == r"C:/M/micnaga.exe app"
    mem.delete("Micnaga")  # el usuario lo apago en Ajustes: config.run_at_login=False
    rig.app.config.startup.run_at_login = False
    rig.app._ensure_autostart()
    assert mem.get("Micnaga") is None
    monkeypatch.setenv("MICNAGA_NO_AUTOSTART", "1")
    rig.app.config.startup.run_at_login = True
    rig.app._ensure_autostart()
    assert mem.get("Micnaga") is None


def test_level_monitor_reads_synthetic_sources() -> None:
    from micnaga.capture.synthetic import demo_backend
    from micnaga.integration.levels import LevelMonitor

    mon = LevelMonitor(demo_backend)
    mon.start()
    try:
        _wait(lambda: max(mon.levels()) > 0.0)
    finally:
        mon.stop()
    assert mon.levels() == (0.0, 0.0) and not mon.active


def test_wizard_level_monitor_is_not_started_while_recording() -> None:
    rig = Rig()
    started: list[bool] = []
    rig.app._monitor.start = lambda: started.append(True)  # type: ignore[method-assign]
    rig.app.callbacks.on_levels_monitor(True)
    assert started == [True]
    from micnaga.watcher import MeetingInfo

    rig.ctl.start_recording(provisional=False, meeting=MeetingInfo(manual=True))
    rig.app.callbacks.on_levels_monitor(True)
    assert started == [True]
