"""Escrituras de Google en ``micnaga.db`` (``IF NOT EXISTS``, sin tocar ``SCHEMA_VERSION``).

* ``google_writes``: una fila por escritura aprobada (clave = reunion + propuesta + operacion).
  ``intent`` se graba ANTES de lanzar ``claude -p``; ``done`` con los ids y enlaces; ``unknown`` si
  no se sabe si se ejecuto (tiempo agotado tras la llamada): antes de reintentar se busca la marca
  de Micnaga en Google. Asi un reintento nunca crea dos veces.
* ``google_odoo``: evento de Google -> evento espejo de Odoo (``calendar.event``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS google_writes (
        key         TEXT PRIMARY KEY,
        folder      TEXT NOT NULL,
        proposal    TEXT NOT NULL,
        op          TEXT NOT NULL,
        tool        TEXT NOT NULL,
        state       TEXT NOT NULL,
        result      TEXT,
        updated_at  REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS google_odoo (
        google_event_id TEXT PRIMARY KEY,
        odoo_id         INTEGER NOT NULL,
        updated_at      REAL NOT NULL
    )""",
)


@dataclass(frozen=True)
class Write:
    key: str
    folder: str
    proposal: str
    op: str
    tool: str
    state: str
    result: dict[str, Any]


def write_key(folder: str, proposal_id: str, op: str, revision: int = 0) -> str:
    """Clave estable: la misma propuesta (y revision) da la misma clave en cada reintento."""
    raw = f"{folder}|{proposal_id}|{op}|{revision}"
    return "w" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def ensure(db: Any) -> None:
    for attempt in range(5):
        try:
            for stmt in _SCHEMA:
                db.conn.execute(stmt)
            return
        except sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))


def get(db: Any, key: str) -> Write | None:
    row = db.conn.execute("SELECT * FROM google_writes WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        result = json.loads(row["result"] or "{}")
    except ValueError:
        result = {}
    return Write(
        row["key"], row["folder"], row["proposal"], row["op"], row["tool"], row["state"], result
    )


def put(
    db: Any,
    key: str,
    *,
    folder: str,
    proposal: str,
    op: str,
    tool: str,
    state: str,
    result: dict[str, Any] | None = None,
) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO google_writes (key, folder, proposal, op, tool, state, result, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET state = excluded.state, result = excluded.result,"
            " tool = excluded.tool, updated_at = excluded.updated_at",
            (
                key,
                folder,
                proposal,
                op,
                tool,
                state,
                json.dumps(result or {}, ensure_ascii=False),
                time.time(),
            ),
        )


def mirror_get(db: Any, google_event_id: str) -> int | None:
    row = db.conn.execute(
        "SELECT odoo_id FROM google_odoo WHERE google_event_id = ?", (google_event_id,)
    ).fetchone()
    return None if row is None else int(row["odoo_id"])


def mirror_put(db: Any, google_event_id: str, odoo_id: int) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO google_odoo (google_event_id, odoo_id, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(google_event_id) DO UPDATE SET odoo_id = excluded.odoo_id,"
            " updated_at = excluded.updated_at",
            (google_event_id, odoo_id, time.time()),
        )


def mirror_forget(db: Any, google_event_id: str) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM google_odoo WHERE google_event_id = ?", (google_event_id,))
