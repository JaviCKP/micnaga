"""Hallazgo P1-4: cambiar la carpeta de reuniones durante una grabacion no deja la reunion
inaccesible. La sesion conserva su carpeta y su indice (los del arranque) hasta terminar el
procesado, y la biblioteca abre una reunion indexada por la ruta que guarda el indice."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from micnaga import paths
from micnaga.config import Config
from micnaga.integration.jobs import Finisher
from micnaga.pipeline import output
from micnaga.store.db import Database
from micnaga.watcher import MeetingInfo
from tests.app.fakes import Rig
from tests.brain.conftest import business_transcript


def _cfg(root: Path) -> Config:
    cfg = Config()
    cfg.storage.root = str(root)
    return cfg


def _jobs(db_file: Path) -> list[Any]:
    if not db_file.is_file():
        return []
    with Database(db_file) as db:
        return db.list_jobs()


def test_switching_folder_mid_recording_keeps_job_and_index_in_the_session_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(paths.ENV_STORAGE, raising=False)
    a, b = tmp_path / "A", tmp_path / "B"
    rig = Rig(_cfg(a))
    spawned_for: list[Path] = []
    rig.app.supervisor._spawn_for = lambda root: spawned_for.append(root)
    rig.app.start()
    try:
        rig.ctl.start_recording(provisional=False, meeting=MeetingInfo(title="Reunion"))
        session = rig.ctl.session_dir
        assert session is not None and session.parent == a
        cfg_b = _cfg(b)  # Ajustes: nueva carpeta mientras se graba
        rig.app.config = cfg_b
        rig.ctl.set_config(copy.deepcopy(cfg_b))
        rig.ctl.stop_and_finalize(confirmed=True, meeting=MeetingInfo(title="Reunion"))
        rig.settle()
        jobs_a = _jobs(a / "micnaga.db")
        assert [Path(j.session_dir) for j in jobs_a] == [session]
        assert _jobs(b / "micnaga.db") == []
        # el supervisor lanza el worker sobre la carpeta A (donde estan audio e indice)
        for _ in range(100):
            if spawned_for:
                break
            rig.app.supervisor.kick()
            import time

            time.sleep(0.05)
        assert spawned_for and spawned_for[0] == a
    finally:
        rig.app.shutdown(wait_s=5)


def test_finisher_task_with_its_own_db_path_ignores_the_current_index(tmp_path: Path) -> None:
    opened: list[Path] = []

    class Db:
        def __init__(self, key: object) -> None:
            self.key = key

        def close(self) -> None:
            pass

    def at(path: Path) -> Any:
        opened.append(path)
        return Db(path)

    seen: list[object] = []
    fin = Finisher(lambda: Db("B"), db_key=lambda: "B", path_factory=at)  # type: ignore[arg-type,return-value]
    fin.submit("own", lambda db: seen.append(db.key), db_path=tmp_path / "A.db")  # type: ignore[attr-defined]
    fin.submit("same", lambda db: seen.append(db.key), db_path=Path("B"))  # type: ignore[attr-defined]
    fin.submit("default", lambda db: seen.append(db.key))  # type: ignore[attr-defined]
    assert fin.wait_idle(5)
    fin.stop(5)
    assert seen == [tmp_path / "A.db", "B", "B"]
    assert opened == [tmp_path / "A.db"]


def test_library_opens_an_indexed_meeting_outside_the_current_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El indice de B guarda una reunion cuya carpeta esta en A: se abre por su ruta guardada."""
    from micnaga.ui.bridge import LibraryApi

    a, b = tmp_path / "A", tmp_path / "B"
    folder = a / "2026-11-19_1030_reunion"
    folder.mkdir(parents=True)
    transcript = business_transcript()
    output.write_transcript(folder, transcript)
    b.mkdir()
    db_file = b / "micnaga.db"
    with Database(db_file) as db:
        job = db.enqueue_job(folder)
        output.index_meeting(db, folder, transcript, job.id)
    opened: list[Path] = []
    api = LibraryApi(
        db_factory=lambda: Database(db_file),
        meetings_root=lambda: b,
        audio_url=lambda f, t: f"/{f}/{t}",
        config=lambda: Config(),
        opener=opened.append,
    )
    assert [m["folder"] for m in api.list_meetings()] == [folder.name]
    res = api.get_meeting(folder.name)
    assert res["ok"], res
    assert api.open_folder(folder.name)["ok"] and opened == [folder]
    # nunca una carpeta que no venga del indice
    assert not api.get_meeting("otra")["ok"]
    assert not api.get_meeting("..")["ok"]


def _meeting_in_a_indexed_in_b(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Reunion en la carpeta A, indexada (con su trabajo) en el indice de B."""
    a, b = tmp_path / "A", tmp_path / "B"
    folder = a / "2026-11-19_1030_reunion"
    folder.mkdir(parents=True)
    transcript = business_transcript()
    output.write_transcript(folder, transcript)
    (folder / paths.AUDIO_ME_FLAC).write_bytes(b"fLaC")
    b.mkdir()
    db_file = b / "micnaga.db"
    with Database(db_file) as db:
        job = db.enqueue_job(folder)
        output.index_meeting(db, folder, transcript, job.id)
    return folder, b, db_file


def test_audio_of_an_indexed_meeting_outside_the_root_is_served(tmp_path: Path) -> None:
    from micnaga.ui.media import LocalServer

    folder, b, db_file = _meeting_in_a_indexed_in_b(tmp_path)
    server = LocalServer(meetings_root=lambda: b, db_factory=lambda: Database(db_file))
    assert server.resolve_audio(folder.name, "yo") == folder / paths.AUDIO_ME_FLAC
    assert server.resolve_audio(folder.name, "remoto") is None  # no hay FLAC remoto
    assert server.resolve_audio("otra", "yo") is None
    assert server.resolve_audio("..", "yo") is None


def test_speaker_edits_reach_an_indexed_meeting_outside_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from micnaga.integration.library_ops import AppLibraryOps

    monkeypatch.delenv(paths.ENV_STORAGE, raising=False)
    folder, b, _db = _meeting_in_a_indexed_in_b(tmp_path)
    ops = AppLibraryOps(lambda: _cfg(b))
    ops.rename_speaker(folder.name, "S1", "Laura")
    names = {s.id: s.name for s in output.read_transcript(folder).speakers}
    assert names["S1"] == "Laura"
    with pytest.raises(FileNotFoundError):
        ops.rename_speaker("otra", "S1", "X")


def test_supervisor_remembers_old_folders_with_pending_jobs_across_restarts(
    tmp_path: Path,
) -> None:
    from micnaga.integration.jobs import WorkerSupervisor
    from micnaga.store.db import JobState

    a = tmp_path / "A"
    a.mkdir()
    db_a = a / "micnaga.db"
    with Database(db_a) as db:
        db.enqueue_job(a / "sesion")
    state = tmp_path / "home" / "pending_roots.json"

    class Idle:
        def count_jobs(self) -> dict[Any, int]:
            return dict.fromkeys(JobState, 0)

        def close(self) -> None:
            pass

    def make(spawned: list[Path]) -> WorkerSupervisor:
        return WorkerSupervisor(
            Config(),
            db_factory=Idle,  # type: ignore[arg-type]
            db_key=lambda: str(tmp_path / "B" / "micnaga.db"),
            spawn=lambda: None,
            spawn_for=lambda root: spawned.append(root),  # type: ignore[arg-type,func-returns-value]
            is_running=lambda: False,
            models_ready=lambda: True,
            watch_file=state,
        )

    first = make([])
    first.watch(db_a)
    assert state.is_file()
    spawned: list[Path] = []
    second = make(spawned)  # la app se reinicia con la config apuntando a B
    second.tick(Idle())  # type: ignore[arg-type]
    assert spawned == [a]
    with Database(db_a) as db:  # el worker de A vacia su cola
        job = db.claim_next_job()
        assert job is not None
        db.finish_job(job.id)
    second.tick(Idle())  # type: ignore[arg-type]
    third: list[Path] = []
    make(third).tick(Idle())  # type: ignore[arg-type]
    assert third == [], "una carpeta ya sin trabajos se olvida (tambien en disco)"
