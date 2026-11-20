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
