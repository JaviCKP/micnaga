"""Tablas propias de la Secretaria en ``micnaga.db`` (sin tocar ``SCHEMA_VERSION`` del nucleo).

* ``secretary_runs``: cola/estado de la pasada por reunion (clave: nombre de carpeta).
  ``pending -> running -> done | failed | skipped``; reintentos con espera creciente
  (``next_try``). Al arrancar, lo que quedo ``running`` vuelve a ``pending``.
* ``original_fts``: el texto del ASR de los parrafos que la limpieza con IA cambio de palabra
  (``micnaga.brain.cleanup``): la busqueda encuentra tambien lo que se oyo.
* ``acta_fts``: texto del acta (titulo, resumen, decisiones, tareas...) en FTS5 propio. No usa
  ``segments``: ``Database._replace_segments`` la vacia al renombrar o fusionar hablantes.

Se crean con ``CREATE ... IF NOT EXISTS`` desde aqui (idempotente); una version antigua de
Micnaga sin Secretaria abre la misma base sin problema (no cambia ``user_version``).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from micnaga.store.db import Database, fts_query

RunState = str  # pending | running | done | failed | skipped

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS secretary_runs (
        folder      TEXT PRIMARY KEY,
        state       TEXT NOT NULL DEFAULT 'pending',
        attempts    INTEGER NOT NULL DEFAULT 0,
        error       TEXT,
        error_kind  TEXT,
        next_try    REAL NOT NULL DEFAULT 0,
        proposals   INTEGER NOT NULL DEFAULT 0,
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS secretary_runs_state ON secretary_runs (state, next_try)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS acta_fts USING fts5 (
        folder UNINDEXED, title UNINDEXED, text,
        tokenize = 'unicode61 remove_diacritics 2'
    )""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS original_fts USING fts5 (
        folder UNINDEXED, start UNINDEXED, speaker UNINDEXED, text,
        tokenize = 'unicode61 remove_diacritics 2'
    )""",
)
MAX_ATTEMPTS = 4
BACKOFF_S = (60.0, 300.0, 1800.0, 7200.0)


@dataclass(frozen=True)
class Run:
    folder: str
    state: RunState
    attempts: int
    error: str | None
    error_kind: str | None
    next_try: float
    proposals: int
    updated_at: float


@dataclass(frozen=True)
class OriginalHit:
    folder: str
    start: float
    speaker: str
    snippet: str


@dataclass(frozen=True)
class ActaHit:
    folder: str
    title: str
    snippet: str


def ensure_schema(db: Database) -> None:
    for attempt in range(5):
        try:
            for stmt in _SCHEMA:
                db.conn.execute(stmt)
            return
        except sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))


def _run(row: sqlite3.Row) -> Run:
    return Run(
        folder=row["folder"],
        state=row["state"],
        attempts=int(row["attempts"]),
        error=row["error"],
        error_kind=row["error_kind"],
        next_try=float(row["next_try"]),
        proposals=int(row["proposals"]),
        updated_at=float(row["updated_at"]),
    )


def get_run(db: Database, folder: str) -> Run | None:
    row = db.conn.execute("SELECT * FROM secretary_runs WHERE folder = ?", (folder,)).fetchone()
    return None if row is None else _run(row)


def enqueue(
    db: Database, folder: str, *, force: bool = False, failed_before: float | None = None
) -> Run:
    """Crea o reabre la pasada de una reunion (``force`` reabre aunque este hecha).

    ``failed_before``: reabre solo si fallo antes de ese instante (al arrancar la app: lo que
    fallo sin sesion o sin red vuelve a probar, sin pisar un fallo de esta misma sesion)."""
    now = time.time()
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO secretary_runs (folder, state, created_at, updated_at)"
            " VALUES (?, 'pending', ?, ?)",
            (folder, now, now),
        )
        if force:
            conn.execute(
                "UPDATE secretary_runs SET state = 'pending', attempts = 0, error = NULL,"
                " error_kind = NULL, next_try = 0, updated_at = ? WHERE folder = ?",
                (now, folder),
            )
        elif failed_before is not None:
            conn.execute(
                "UPDATE secretary_runs SET state = 'pending', attempts = 0, error = NULL,"
                " error_kind = NULL, next_try = 0, updated_at = ?"
                " WHERE folder = ? AND state = 'failed' AND updated_at < ?",
                (now, folder, failed_before),
            )
        row = conn.execute("SELECT * FROM secretary_runs WHERE folder = ?", (folder,)).fetchone()
    return _run(row)


def claim(db: Database, folder: str) -> bool:
    """``pending|failed`` -> ``running`` (si toca por ``next_try``)."""
    now = time.time()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE secretary_runs SET state = 'running', attempts = attempts + 1,"
            " updated_at = ? WHERE folder = ? AND state IN ('pending', 'failed')"
            " AND next_try <= ?",
            (now, folder, now),
        )
    return cur.rowcount > 0


def finish(db: Database, folder: str, *, proposals: int, state: RunState = "done") -> None:
    now = time.time()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE secretary_runs SET state = ?, error = NULL, error_kind = NULL,"
            " proposals = ?, updated_at = ? WHERE folder = ?",
            (state, proposals, now, folder),
        )


def set_proposals(db: Database, folder: str, proposals: int) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE secretary_runs SET proposals = ?, updated_at = ? WHERE folder = ?",
            (proposals, time.time(), folder),
        )


def fail(
    db: Database, folder: str, error: str, kind: str, *, retry_at: float | None = None
) -> Run | None:
    """Fallo: ``failed`` con espera creciente (o ``retry_at``); tras ``MAX_ATTEMPTS``, sin
    reintento automatico (``next_try`` infinito: solo «Reintentar»)."""
    now = time.time()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT attempts FROM secretary_runs WHERE folder = ?", (folder,)
        ).fetchone()
        if row is None:
            return None
        attempts = int(row["attempts"])
        if retry_at is not None:
            next_try = retry_at
        elif attempts >= MAX_ATTEMPTS or kind in ("auth", "not_found"):
            next_try = float("inf")
        else:
            next_try = now + BACKOFF_S[min(attempts, len(BACKOFF_S)) - 1]
        conn.execute(
            "UPDATE secretary_runs SET state = 'failed', error = ?, error_kind = ?,"
            " next_try = ?, updated_at = ? WHERE folder = ?",
            (error[:500], kind, next_try, now, folder),
        )
    return get_run(db, folder)


def requeue_running(db: Database) -> int:
    with db.transaction() as conn:
        return conn.execute(
            "UPDATE secretary_runs SET state = 'pending', updated_at = ? WHERE state = 'running'",
            (time.time(),),
        ).rowcount


def due(db: Database, *, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    rows = db.conn.execute(
        "SELECT folder FROM secretary_runs WHERE state IN ('pending', 'failed')"
        " AND next_try <= ? ORDER BY updated_at",
        (now,),
    ).fetchall()
    return [r["folder"] for r in rows]


def list_runs(db: Database) -> list[Run]:
    return [_run(r) for r in db.conn.execute("SELECT * FROM secretary_runs ORDER BY updated_at")]


# ============================================================================== FTS del acta


def index_acta(db: Database, folder: str, title: str, text: str) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM acta_fts WHERE folder = ?", (folder,))
        conn.execute(
            "INSERT INTO acta_fts (folder, title, text) VALUES (?, ?, ?)", (folder, title, text)
        )


def search_actas(db: Database, query: str, limit: int = 20) -> list[ActaHit]:
    match = fts_query(query)
    if not match:
        return []
    rows = db.conn.execute(
        "SELECT folder, title, snippet(acta_fts, 2, '<mark>', '</mark>', '…', 16) AS snip"
        " FROM acta_fts WHERE acta_fts MATCH ? ORDER BY bm25(acta_fts) LIMIT ?",
        (match, limit),
    ).fetchall()
    return [ActaHit(r["folder"], r["title"], r["snip"]) for r in rows]


def index_originals(db: Database, folder: str, rows: list[tuple[float, str, str]]) -> None:
    """``(inicio, hablante, texto original)`` de los parrafos corregidos de una reunion."""
    with db.transaction() as conn:
        conn.execute("DELETE FROM original_fts WHERE folder = ?", (folder,))
        conn.executemany(
            "INSERT INTO original_fts (folder, start, speaker, text) VALUES (?, ?, ?, ?)",
            [(folder, start, speaker, text) for start, speaker, text in rows if text],
        )


def search_originals(db: Database, query: str, limit: int = 20) -> list[OriginalHit]:
    match = fts_query(query)
    if not match:
        return []
    rows = db.conn.execute(
        "SELECT folder, start, speaker,"
        " snippet(original_fts, 3, '<mark>', '</mark>', '…', 16) AS snip"
        " FROM original_fts WHERE original_fts MATCH ? ORDER BY bm25(original_fts) LIMIT ?",
        (match, limit),
    ).fetchall()
    return [OriginalHit(r["folder"], float(r["start"]), r["speaker"], r["snip"]) for r in rows]


def rename_folder(db: Database, old: str, new: str, *, new_path: str, title: str) -> None:
    """Todo lo que lleva el nombre de la carpeta, en una transaccion: ``meetings`` (clave del
    upsert: si no, quedaria una fila duplicada), ``jobs.session_dir``, la pasada y el acta."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE meetings SET folder = ?, title = ? WHERE folder = ?", (new, title, old)
        )
        conn.execute(
            "UPDATE jobs SET session_dir = ? WHERE session_dir LIKE ? OR session_dir LIKE ?",
            (new_path, "%\\" + old, "%/" + old),
        )
        conn.execute("UPDATE secretary_runs SET folder = ? WHERE folder = ?", (new, old))
        conn.execute("UPDATE acta_fts SET folder = ? WHERE folder = ?", (new, old))
        conn.execute("UPDATE original_fts SET folder = ? WHERE folder = ?", (new, old))


def set_meeting_title(db: Database, folder: str, title: str) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE meetings SET title = ? WHERE folder = ?", (title, folder))
