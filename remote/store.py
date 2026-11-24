"""Almacén de las reuniones publicadas y sus permisos."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from micnaga.brain.library_mcp import (
    TOOL_SPECS,
    LibraryError,
    LibraryTools,
    _day,
    _integer,
)
from micnaga.meeting_links import indexed_meeting_link
from micnaga.remote_protocol import DEVICE_KEY
from micnaga.text import fts_query

READ_TOOLS = {
    name: TOOL_SPECS[name]
    for name in (
        "buscar_reuniones",
        "leer_acta",
        "leer_transcripcion",
    )
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    workspace TEXT NOT NULL,
    subject TEXT NOT NULL,
    PRIMARY KEY (subject, workspace)
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device TEXT NOT NULL,
    library TEXT NOT NULL,
    local_id INTEGER NOT NULL,
    workspace TEXT NOT NULL,
    revision INTEGER NOT NULL,
    title TEXT,
    start TEXT,
    duration REAL,
    payload TEXT,
    UNIQUE (device, library, local_id)
);
CREATE INDEX IF NOT EXISTS documents_recent ON documents (workspace, start DESC, id DESC)
    WHERE payload IS NOT NULL;
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5 (
    document UNINDEXED, at UNINDEXED, version UNINDEXED, text,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS search_rows (
    document INTEGER NOT NULL,
    row_id INTEGER PRIMARY KEY
);
CREATE INDEX IF NOT EXISTS search_rows_document ON search_rows(document);
"""

_VISIBLE = """
    d.payload IS NOT NULL AND EXISTS (
        SELECT 1 FROM members WHERE subject = ? AND workspace = d.workspace
    )
"""


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("ascii")).hexdigest()


class Store:
    def __init__(self, file: Path) -> None:
        self.file = file
        file.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.file, timeout=10.0)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def add_device(self, workspace: str) -> tuple[str, str]:
        device, key = secrets.token_hex(16), "mn_" + secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("INSERT INTO devices VALUES (?,?,?)", (device, workspace, _hash(key)))
        return device, key

    def revoke_device(self, device: str, *, purge: bool = False) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM devices WHERE id = ?", (device,))
            if purge:
                docs = "SELECT id FROM documents WHERE device=?"
                rows = f"SELECT row_id FROM search_rows WHERE document IN ({docs})"
                db.execute(f"DELETE FROM search WHERE rowid IN ({rows})", (device,))
                db.execute(f"DELETE FROM search_rows WHERE document IN ({docs})", (device,))
                db.execute("DELETE FROM documents WHERE device=?", (device,))

    def grant(self, workspace: str, subject: str, *, allow: bool) -> None:
        with self.connect() as db:
            if allow:
                db.execute("INSERT OR IGNORE INTO members VALUES (?,?)", (workspace, subject))
            else:
                db.execute(
                    "DELETE FROM members WHERE workspace = ? AND subject = ?",
                    (
                        workspace,
                        subject,
                    ),
                )

    def has_access(self, subject: str) -> bool:
        with self.connect() as db:
            return (
                db.execute("SELECT 1 FROM members WHERE subject = ?", (subject,)).fetchone()
                is not None
            )

    @staticmethod
    def _device(db: sqlite3.Connection, key: str) -> sqlite3.Row | None:
        if DEVICE_KEY.fullmatch(key) is None:
            return None
        row: sqlite3.Row | None = db.execute(
            "SELECT id, workspace FROM devices WHERE key_hash = ?",
            (_hash(key),),
        ).fetchone()
        return row

    def device(self, key: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return self._device(db, key)

    def put(
        self,
        key: str,
        library: str,
        local_id: int,
        revision: int,
        data: dict[str, Any] | None,
    ) -> int:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            device = self._device(db, key)
            if device is None:
                return 401
            identity = (device["id"], library, local_id)
            current = db.execute(
                "SELECT revision, payload FROM documents"
                " WHERE device=? AND library=? AND local_id=?",
                identity,
            ).fetchone()
            payload = (
                json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                if data is not None
                else None
            )
            if current is not None and revision <= current["revision"]:
                if revision == current["revision"] and payload == current["payload"]:
                    return 204
                return 409
            document = db.execute(
                "INSERT INTO documents"
                " (device, library, local_id, workspace, revision, title, start, duration, payload)"
                " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(device, library, local_id) DO UPDATE SET"
                " revision=excluded.revision, title=excluded.title, start=excluded.start,"
                " duration=excluded.duration, payload=excluded.payload RETURNING id",
                (
                    *identity,
                    device["workspace"],
                    revision,
                    data["titulo"] if data else None,
                    data["fecha"] if data else None,
                    data["duracion_s"] if data else None,
                    payload,
                ),
            ).fetchone()[0]
            db.execute(
                "DELETE FROM search WHERE rowid IN"
                " (SELECT row_id FROM search_rows WHERE document=?)",
                (document,),
            )
            db.execute("DELETE FROM search_rows WHERE document=?", (document,))
            if data is not None:
                hits: list[tuple[float | None, str | None, str]] = [
                    (None, None, data["titulo"] + "\n" + (data["acta"] or "")),
                ]
                for s in data["segments"]:
                    original = s.get("text_original") or ""
                    text = s["text"]
                    hits.append((s["start"], "limpia", text))
                    if original and original != text:
                        hits.append((s["start"], "original", original))
                for at, version, text in hits:
                    row = db.execute(
                        "INSERT INTO search VALUES (?,?,?,?)", (document, at, version, text)
                    )
                    db.execute("INSERT INTO search_rows VALUES (?,?)", (document, row.lastrowid))
            return 204

    @staticmethod
    def _cite(row: sqlite3.Row, at: float = 0) -> str:
        return indexed_meeting_link(row["library"], row["local_id"], at, row["device"])

    @classmethod
    def _metadata(cls, row: sqlite3.Row, at: float = 0) -> dict[str, Any]:
        return {
            "reunion_id": row["id"],
            "biblioteca": row["workspace"],
            "titulo": row["title"],
            "fecha": row["start"],
            "duracion_s": row["duration"],
            "enlace": cls._cite(row, at),
        }

    def call(self, subject: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if (
            name not in READ_TOOLS
            or args.keys() - READ_TOOLS[name]["inputSchema"]["properties"].keys()
        ):
            raise LibraryError("Herramienta o argumentos desconocidos.")
        with self.connect() as db:
            if name == "buscar_reuniones":
                return self._search(db, subject, args)
            meeting = _integer(args, "reunion_id", 1, 2**63 - 1)
            offset = _integer(args, "desplazamiento", 0, 10_000_000)
            row = db.execute(
                f"SELECT d.* FROM documents d WHERE {_VISIBLE} AND d.id = ?",
                (subject, meeting),
            ).fetchone()
        if row is None:
            raise LibraryError("Reunión no disponible.")
        data, metadata = json.loads(row["payload"]), self._metadata(row)
        if name == "leer_acta":
            if data["acta"] is None:
                raise LibraryError("La reunión aún no tiene acta.")
            return LibraryTools.minutes_page(data["acta"], metadata, offset)
        return LibraryTools.transcript_page(
            data, metadata, args, offset, lambda at: self._cite(row, at)
        )

    @classmethod
    def _search(cls, db: sqlite3.Connection, subject: str, args: dict[str, Any]) -> dict[str, Any]:
        text = args.get("texto", "")
        if not isinstance(text, str) or len(text) > 120:
            raise LibraryError("texto debe ser una cadena de hasta 120 caracteres.")
        since, until = _day(args, "desde"), _day(args, "hasta")
        if since and until and since > until:
            raise LibraryError("desde debe ser anterior o igual a hasta.")
        limit = _integer(args, "limite", 1, 10, 8)
        page = _integer(args, "pagina", 1, 1_000_000, 1)
        match = fts_query(text)
        bounds = " AND (? IS NULL OR d.start >= ?) AND (? IS NULL OR substr(d.start, 1, 10) <= ?)"
        order = " ORDER BY d.start DESC, d.id DESC LIMIT ? OFFSET ?"
        fields = "d.id, d.device, d.local_id, d.library, d.workspace, d.start, d.title, d.duration"
        params = (subject, since, since, until, until, limit + 1, (page - 1) * limit)
        if match:
            rows = db.execute(
                "WITH hits AS (SELECT document, min(search.rowid) AS any_hit,"
                " min(CASE WHEN at IS NOT NULL THEN search.rowid END) AS quote"
                " FROM search JOIN documents d ON d.id = search.document"
                f" WHERE search MATCH ? AND {_VISIBLE}{bounds} GROUP BY document)"
                f" SELECT {fields}, s.at, s.version, substr(s.text, 1, 240) AS excerpt"
                " FROM hits JOIN documents d ON d.id = hits.document"
                " JOIN search s ON s.rowid = coalesce(hits.quote, hits.any_hit)"
                f" {order}",
                (match, *params),
            ).fetchall()
        else:
            rows = db.execute(
                f"SELECT {fields}, NULL AS at, NULL AS excerpt FROM documents d"
                f" WHERE {_VISIBLE}{bounds}{order}",
                params,
            ).fetchall()
        meetings = []
        for row in rows[:limit]:
            at = row["at"]
            result = cls._metadata(row, at or 0)
            if at is not None:
                result.update(fragmento=row["excerpt"], inicio_s=at, version=row["version"])
            meetings.append(result)
        return {"reuniones": meetings, "siguiente_pagina": page + 1 if len(rows) > limit else None}
