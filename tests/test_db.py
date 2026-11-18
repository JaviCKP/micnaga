from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from micnaga.store.db import (
    Database,
    InvalidTransition,
    JobNotFoundError,
    JobState,
    MeetingRecord,
    fts_query,
    open_database,
)
from micnaga.types import (
    EnginesInfo,
    MeetingInfo,
    Segment,
    Speaker,
    Transcript,
    Word,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    d = Database(tmp_path / "t.db", clock=FakeClock())
    yield d
    d.close()


def test_fts5_available() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
    conn.close()


def test_wal_and_foreign_keys(db: Database) -> None:
    assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_open_database_default_path(tmp_storage: Path) -> None:
    with open_database() as d:
        assert d.path == str(tmp_storage / "micnaga.db")
    assert (tmp_storage / "micnaga.db").exists()


def test_reopen_is_idempotent(tmp_path: Path) -> None:
    Database(tmp_path / "x.db").close()
    with Database(tmp_path / "x.db") as d:
        assert d.list_jobs() == []


# ----------------------------------------------------------------------------- jobs


def test_enqueue_is_idempotent(db: Database) -> None:
    a = db.enqueue_job("C:/m/a")
    b = db.enqueue_job("C:/m/a")
    assert a.id == b.id
    assert a.state is JobState.PENDING
    assert (a.stage, a.attempts, a.error) == (0, 0, None)
    assert len(db.list_jobs()) == 1


def test_claim_is_fifo_and_exclusive(db: Database) -> None:
    j1 = db.enqueue_job("a")
    j2 = db.enqueue_job("b")
    c1 = db.claim_next_job()
    c2 = db.claim_next_job()
    assert c1 is not None
    assert c2 is not None
    assert (c1.id, c2.id) == (j1.id, j2.id)
    assert c1.state is JobState.PROCESSING
    assert c1.attempts == 1
    assert db.claim_next_job() is None


def test_happy_path_done(db: Database) -> None:
    j = db.enqueue_job("a")
    db.claim_next_job()
    assert db.set_stage(j.id, 3).stage == 3
    assert db.set_stage(j.id, 6).stage == 6
    done = db.finish_job(j.id)
    assert done.state is JobState.DONE
    assert done.stage == 6


def test_fail_and_retry(db: Database) -> None:
    j = db.enqueue_job("a")
    db.claim_next_job()
    failed = db.fail_job(j.id, "boom")
    assert (failed.state, failed.error) == (JobState.FAILED, "boom")
    retried = db.retry_job(j.id)
    assert (retried.state, retried.error) == (JobState.PENDING, None)
    again = db.claim_next_job()
    assert again is not None
    assert again.attempts == 2


def test_discard_logs_only_date_and_reason(db: Database) -> None:
    j = db.enqueue_job("C:/secret/reunion-x")
    db.claim_next_job()
    d = db.discard_job(j.id, "voz remota < 30 s")
    assert d.state is JobState.DISCARDED
    entries = db.discard_entries()
    assert len(entries) == 1
    day, reason = entries[0]
    assert reason == "voz remota < 30 s"
    date.fromisoformat(day)
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(discard_log)")]
    assert cols == ["id", "date", "reason"]
    dump = " ".join(str(tuple(r)) for r in db.conn.execute("SELECT * FROM discard_log"))
    assert "secret" not in dump


def test_discard_from_pending(db: Database) -> None:
    j = db.enqueue_job("a")
    assert db.discard_job(j.id, "too short").state is JobState.DISCARDED


@pytest.mark.parametrize(
    "setup, op",
    [
        ("pending", "finish"),
        ("pending", "fail"),
        ("pending", "stage"),
        ("pending", "retry"),
        ("processing", "retry"),
        ("done", "finish"),
        ("done", "fail"),
        ("done", "discard"),
        ("done", "retry"),
        ("failed", "finish"),
        ("failed", "stage"),
        ("discarded", "retry"),
        ("discarded", "finish"),
    ],
)
def test_invalid_transitions(db: Database, setup: str, op: str) -> None:
    j = db.enqueue_job("a")
    if setup in ("processing", "done", "failed", "discarded"):
        db.claim_next_job()
    if setup == "done":
        db.finish_job(j.id)
    elif setup == "failed":
        db.fail_job(j.id, "x")
    elif setup == "discarded":
        db.discard_job(j.id, "x")
    calls = {
        "finish": lambda: db.finish_job(j.id),
        "fail": lambda: db.fail_job(j.id, "e"),
        "stage": lambda: db.set_stage(j.id, 2),
        "retry": lambda: db.retry_job(j.id),
        "discard": lambda: db.discard_job(j.id, "r"),
    }
    with pytest.raises(InvalidTransition):
        calls[op]()
    assert db.get_job(j.id).state is JobState(setup)


def test_invalid_discard_does_not_log(db: Database) -> None:
    j = db.enqueue_job("a")
    db.claim_next_job()
    db.finish_job(j.id)
    with pytest.raises(InvalidTransition):
        db.discard_job(j.id, "late")
    assert db.discard_entries() == []


def test_unknown_job(db: Database) -> None:
    with pytest.raises(JobNotFoundError):
        db.get_job(999)
    with pytest.raises(JobNotFoundError):
        db.finish_job(999)


def test_requeue_stale_after_crash(db: Database) -> None:
    j = db.enqueue_job("a")
    db.claim_next_job()
    db.set_stage(j.id, 6)
    changed = db.requeue_stale()
    assert [c.id for c in changed] == [j.id]
    back = db.get_job(j.id)
    assert back.state is JobState.PENDING
    assert back.stage == 6  # conserva la etapa: la reanudacion usa work/
    again = db.claim_next_job()
    assert again is not None
    assert again.attempts == 2


def test_requeue_stale_gives_up_after_max_attempts(db: Database) -> None:
    j = db.enqueue_job("a")
    for _ in range(3):
        db.claim_next_job()
        db.requeue_stale(max_attempts=3)
    assert db.get_job(j.id).state is JobState.FAILED
    assert db.claim_next_job() is None


def test_requeue_ignores_other_states(db: Database) -> None:
    j = db.enqueue_job("a")
    db.enqueue_job("b")
    assert db.requeue_stale() == []
    assert db.get_job(j.id).state is JobState.PENDING


def test_count_jobs(db: Database) -> None:
    db.enqueue_job("a")
    db.enqueue_job("b")
    db.claim_next_job()
    counts = db.count_jobs()
    assert counts[JobState.PENDING] == 1
    assert counts[JobState.PROCESSING] == 1
    assert counts[JobState.DONE] == 0


def test_concurrent_claims_never_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "c.db"
    with Database(path) as seed:
        for i in range(30):
            seed.enqueue_job(f"s{i}")
    claimed: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        with Database(path) as d:
            while (job := d.claim_next_job()) is not None:
                with lock:
                    claimed.append(job.id)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == list(range(1, 31))


# ----------------------------------------------------------------------------- FTS


def _transcript(
    title: str, texts: list[str], start: str = "2026-11-18T10:30:00+01:00"
) -> Transcript:
    segs = [
        Segment(
            start=float(i * 5),
            end=float(i * 5 + 4),
            speaker="me" if i % 2 == 0 else "S1",
            text=t,
            words=[Word("x", 0.0, 1.0, 0.9)],
        )
        for i, t in enumerate(texts)
    ]
    return Transcript(
        meeting=MeetingInfo(title=title, start=start, duration_s=60.0),
        speakers=[Speaker("me", "Yo"), Speaker("S1", "Laura", 0.84)],
        segments=segs,
        markers=[],
        engines=EnginesInfo(asr="fake"),
    )


def test_fts_query_builder() -> None:
    assert fts_query("revision sema") == '"revision"* "sema"*'
    assert fts_query('  "weird" AND (x) ') == '"weird"* "AND"* "x"*'
    assert fts_query("   ") == ""
    assert fts_query("a-b") == '"a"* "b"*'


def test_search_finds_text_and_ignores_accents(db: Database) -> None:
    db.index_transcript(
        "2026-11-18_1030_semanal",
        _transcript(
            "Semanal", ["Buenos días, ¿me oís bien?", "Empezamos por la reunión de despliegue"]
        ),
    )
    hits = db.search("reunion")
    assert len(hits) == 1
    assert hits[0].speaker == "S1"
    assert "<mark>reunión</mark>" in hits[0].snippet
    assert hits[0].folder == "2026-11-18_1030_semanal"
    assert db.search("REUNIÓN")[0].segment_id == hits[0].segment_id
    assert [h.speaker for h in db.search("buenos dias")] == ["me"]
    assert [h.speaker for h in db.search("despl")] == ["S1"]  # prefijo
    assert db.search("inexistente") == []
    assert db.search("") == []
    assert db.search("^^ ** ((") == []  # no rompe la sintaxis FTS


def test_search_limit_and_folder_filter(db: Database) -> None:
    db.index_transcript("a", _transcript("A", ["presupuesto uno", "presupuesto dos"]))
    db.index_transcript("b", _transcript("B", ["presupuesto tres"]))
    assert len(db.search("presupuesto")) == 3
    assert len(db.search("presupuesto", limit=2)) == 2
    assert [h.folder for h in db.search("presupuesto", folder="b")] == ["b"]


def test_reindex_replaces_segments(db: Database) -> None:
    db.index_transcript("a", _transcript("A", ["texto viejo"]))
    db.index_transcript("a", _transcript("A", ["texto nuevo"]))
    assert db.search("viejo") == []
    assert len(db.search("nuevo")) == 1
    count = db.conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    assert count == 1
    assert len(db.list_meetings()) == 1


def test_delete_meeting_cascades_to_fts(db: Database) -> None:
    db.index_transcript("a", _transcript("A", ["palabra unica"]))
    db.index_transcript("b", _transcript("B", ["palabra comun"]))
    assert db.delete_meeting("a") is True
    assert db.delete_meeting("a") is False
    assert db.search("unica") == []
    assert len(db.search("comun")) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 1
    # el indice FTS sigue sano
    db.conn.execute("INSERT INTO segments_fts (segments_fts) VALUES ('integrity-check')")


def test_meeting_roundtrip_and_ordering(db: Database) -> None:
    db.index_transcript("old", _transcript("Vieja", ["x"], start="2026-01-01T09:00:00+01:00"))
    db.index_transcript(
        "new", _transcript("Nueva", ["y"], start="2026-11-18T10:30:00+01:00"), job_id=None
    )
    assert [m.folder for m in db.list_meetings()] == ["new", "old"]
    m = db.get_meeting("new")
    assert m is not None
    assert m.title == "Nueva"
    assert m.speakers == [Speaker("me", "Yo"), Speaker("S1", "Laura", 0.84)]
    assert db.get_meeting("nope") is None


def test_meeting_links_to_job(db: Database) -> None:
    job = db.enqueue_job("a")
    mid = db.upsert_meeting(
        MeetingRecord(
            folder="a", title="A", start="2026-11-18T10:30:00+01:00", duration_s=10, job_id=job.id
        )
    )
    assert mid >= 1
    # upsert sin job_id conserva el vinculo
    db.upsert_meeting(
        MeetingRecord(folder="a", title="A2", start="2026-11-18T10:30:00+01:00", duration_s=11)
    )
    m = db.get_meeting("a")
    assert m is not None
    assert (m.title, m.job_id) == ("A2", job.id)


def test_search_speed_smoke(db: Database) -> None:
    import time

    texts = [f"frase numero {i} sobre presupuesto{i % 7} y despliegue" for i in range(5000)]
    db.index_transcript("big", _transcript("Big", texts))
    t0 = time.perf_counter()
    hits = db.search("despliegue presupuesto3", limit=20)
    dt = time.perf_counter() - t0
    assert hits
    assert dt < 0.2


def test_discard_scrubs_session_dir(db: Database) -> None:
    j = db.enqueue_job("C:/Meetings/2026-11-18_1030_secret-project")
    db.claim_next_job()
    d = db.discard_job(j.id, "nota de voz")
    assert d.session_dir == f"discarded:{j.id}"
    dump = " ".join(str(tuple(r)) for r in db.conn.execute("SELECT * FROM jobs"))
    assert "secret" not in dump
    assert db.find_job("C:/Meetings/2026-11-18_1030_secret-project") is None
