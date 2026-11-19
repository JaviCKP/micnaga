"""Hallazgo 3: cambiar la carpeta de reuniones no deja al Finisher ni al supervisor con el
indice viejo (antes seguian encolando en la base de datos de la carpeta anterior)."""

from __future__ import annotations

import threading
from typing import Any

from micnaga.config import Config
from micnaga.integration.jobs import Finisher, WorkerSupervisor


class FakeDb:
    def __init__(self, key: str) -> None:
        self.key = key
        self.closed = False
        self.used: list[str] = []

    def close(self) -> None:
        self.closed = True

    def count_jobs(self) -> dict[Any, int]:
        from micnaga.store.db import JobState

        self.used.append("count")
        return dict.fromkeys(JobState, 0)


def test_finisher_reopens_the_index_when_the_folder_changes() -> None:
    current = {"key": "A"}
    opened: list[FakeDb] = []

    def factory() -> FakeDb:
        db = FakeDb(current["key"])
        opened.append(db)
        return db

    seen: list[str] = []
    fin = Finisher(factory, db_key=lambda: current["key"])  # type: ignore[arg-type]
    fin.submit("t1", lambda db: seen.append(db.key))  # type: ignore[attr-defined]
    fin.submit("t2", lambda db: seen.append(db.key))  # type: ignore[attr-defined]
    assert fin.wait_idle(5)
    current["key"] = "B"
    fin.submit("t3", lambda db: seen.append(db.key))  # type: ignore[attr-defined]
    assert fin.wait_idle(5)
    fin.stop(5)
    assert seen == ["A", "A", "B"]
    assert [d.key for d in opened] == ["A", "B"], "una conexion por carpeta, no una por tarea"
    assert opened[0].closed and opened[1].closed


def test_supervisor_reopens_the_index_when_the_folder_changes() -> None:
    current = {"key": "A"}
    opened: list[FakeDb] = []
    ticks = threading.Event()

    def factory() -> FakeDb:
        db = FakeDb(current["key"])
        opened.append(db)
        return db

    sup = WorkerSupervisor(
        Config(),
        db_factory=factory,  # type: ignore[arg-type]
        db_key=lambda: current["key"],
        spawn=lambda: None,
        is_running=lambda: False,
        models_ready=lambda: True,
        on_busy=lambda _b: ticks.set(),
        interval_s=0.05,
    )
    sup.start()
    try:
        assert ticks.wait(5)
        current["key"] = "B"
        ticks.clear()
        sup.kick()
        assert ticks.wait(5)
        for _ in range(50):
            if any(d.key == "B" for d in opened):
                break
            sup.kick()
            ticks.wait(0.1)
    finally:
        sup.stop()
    assert [d.key for d in opened][:2] == ["A", "B"]
    assert all(d.closed for d in opened)
