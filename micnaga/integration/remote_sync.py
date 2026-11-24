"""Publica únicamente texto de reuniones completas desde un hilo de bajo impacto."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from micnaga import paths
from micnaga.brain import secrets
from micnaga.brain.acta import ACTA_JSON
from micnaga.brain.library_mcp import (
    MAX_ACTA_BYTES,
    MAX_TRANSCRIPT_BYTES,
    _completed_folder,
    _index,
    _local_file,
    _read_json,
    _safe_text,
    export_minutes,
)
from micnaga.holding import DISCARD_MARKER
from micnaga.meeting_links import library_id
from micnaga.remote_protocol import DEVICE_ID, DEVICE_KEY, MAX_DOCUMENT_BYTES, https_url

log = logging.getLogger(__name__)
_CREDENTIAL = "Micnaga/remote-library"
_SCAN_S = 90.0
_MAX_PER_PASS = 24
_SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
    scope TEXT PRIMARY KEY,
    catalog TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sent (
    scope TEXT NOT NULL,
    id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    revision INTEGER NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, id)
)
"""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _request(url: str, key: str, data: bytes | None = None) -> tuple[int, bytes]:
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data, headers, method="PUT" if data is not None else "GET")
    try:
        with build_opener(_NoRedirect).open(req, timeout=8) as response:
            return response.status, response.read(4096)
    except HTTPError as exc:
        exc.close()
        return exc.code, b""


def _catalog(root: Path) -> str:
    st = (root / paths.DB_FILENAME).stat()
    return f"{st.st_dev}:{st.st_ino}"


@dataclass(frozen=True)
class Connection:
    url: str
    device: str
    workspace: str
    key: str = field(repr=False)
    library: str
    catalog: str

    @property
    def origin(self) -> str:
        return self.url.removesuffix("/mcp")

    @property
    def scope(self) -> str:
        return f"{self.url} {self.device} {self.library}"


def _load_connection() -> Connection | None:
    raw = secrets.get(_CREDENTIAL)
    if not raw:
        return None
    try:
        return Connection(**json.loads(raw))
    except (TypeError, ValueError):
        return None


def _stamp(file: Path | None) -> tuple[int, int, int] | None:
    if file is None:
        return None
    st = file.stat()
    return st.st_mtime_ns, st.st_size, st.st_ino


def _ledger() -> sqlite3.Connection:
    base = paths.ensure_dir(paths.app_data_dir())
    db = sqlite3.connect(base / "remote-sync.sqlite3")
    try:
        db.executescript(_SCHEMA)
    except BaseException:
        db.close()
        raise
    return db


def _document(folder: Path, row: sqlite3.Row) -> dict[str, Any]:
    data = _read_json(folder, paths.TRANSCRIPT_JSON, MAX_TRANSCRIPT_BYTES)
    if data is None:
        raise ValueError("transcripción no disponible")
    acta_file = _local_file(folder, ACTA_JSON)
    acta = _read_json(folder, ACTA_JSON, MAX_ACTA_BYTES)
    if acta_file is not None and acta is None:
        raise ValueError("acta no legible")
    speakers = {s["id"]: _safe_text(s["name"][:100]) for s in data.get("speakers", [])}
    segments = []
    for segment in data["segments"]:
        speaker = segment["speaker"]
        original = segment.get("text_original")
        item = {
            "start": segment["start"],
            "end": segment["end"],
            "speaker": speakers.get(speaker, _safe_text(speaker[:100])),
            "text": _safe_text(segment["text"]),
        }
        if isinstance(original, str) and original != segment["text"]:
            item["text_original"] = _safe_text(original)
        segments.append(item)
    return {
        "titulo": _safe_text(row["title"][:180]),
        "fecha": row["start"],
        "duracion_s": row["duration_s"],
        "acta": export_minutes(acta) if acta is not None else None,
        "segments": segments,
    }


class RemoteSync:
    def __init__(self, root: Callable[[], Path], recording: Callable[[], bool]) -> None:
        self.root = root
        self.recording = recording
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._status: dict[str, Any] = {"state": "pending", "sent": 0}
        self._thread = threading.Thread(target=self._run, name="micnaga-remote-sync", daemon=True)
        self._connection = _load_connection()
        self._configure = threading.Lock()

    def connect(self, value: str, key: str) -> dict[str, Any]:
        url = https_url(value.strip(), path="/mcp")
        key = key.strip()
        if DEVICE_KEY.fullmatch(key) is None:
            raise ValueError("clave no válida")
        root = self.root().resolve()
        identity = _catalog(root)
        code, body = _request(url.removesuffix("/mcp") + "/api/v1/connection", key)
        if code in (401, 403):
            raise ValueError("clave no válida")
        if code != 200:
            raise OSError("no se pudo verificar el servicio")
        reply = json.loads(body)
        if not isinstance(reply, dict):
            raise ValueError("respuesta del servicio no válida")
        device, workspace = reply.get("device"), reply.get("workspace")
        if (
            not isinstance(device, str)
            or DEVICE_ID.fullmatch(device) is None
            or not isinstance(workspace, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", workspace) is None
        ):
            raise ValueError("respuesta del servicio no válida")
        connection = Connection(url, device, workspace, key, library_id(root), identity)
        with self._configure:
            with closing(_ledger()) as db, db:
                db.execute(
                    "INSERT OR IGNORE INTO bindings VALUES (?,?)", (connection.scope, identity)
                )
                saved = db.execute(
                    "SELECT catalog FROM bindings WHERE scope=?", (connection.scope,)
                ).fetchone()
                if saved[0] != identity:
                    raise ValueError("el índice se reemplazó, usa una clave de equipo nueva")
            secrets.put(_CREDENTIAL, json.dumps(asdict(connection)))
            self._connection = connection
            self._status = {"state": "pending", "sent": 0}
        self.kick()
        return self.status()

    def disconnect(self) -> None:
        with self._configure:
            secrets.put(_CREDENTIAL, None)
            self._connection = None
        self.kick()

    def status(self) -> dict[str, Any]:
        connection = self._connection
        if connection is None:
            return {"state": "off"}
        return {**self._status, "url": connection.url, "workspace": connection.workspace}

    def owns_citation(self, root: Path, library: str, device: str) -> bool:
        connection = self._connection
        try:
            return (
                connection is not None and connection.device == device
                and connection.library == library and connection.catalog == _catalog(root)
            )
        except OSError:
            return False

    def kick(self) -> None:
        self._wake.set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.kick()
        if self._thread.is_alive():
            self._thread.join(timeout=0.2)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            connection = self._connection
            delay: float | None = None
            if connection is not None:
                delay = _SCAN_S
                try:
                    state, sent = self.scan(connection)
                    if state == "pending":
                        delay = 2.0
                except (OSError, URLError, sqlite3.Error):
                    state, sent = "offline", 0
                except Exception:
                    log.exception("remote library sync failed")
                    state, sent = "error", 0
                if self._connection is connection:
                    self._status = {"state": state, "sent": sent, "at": time.time()}
            self._wake.wait(delay)

    def scan(self, connection: Connection) -> tuple[str, int]:
        root = self.root().resolve()
        if library_id(root) != connection.library or _catalog(root) != connection.catalog:
            return "library_changed", 0
        if self.recording():
            return "paused", 0
        # Solo la tabla de índice es autoritativa para bajas. Si un fichero falta durante un
        # renombrado o una unidad está desconectada, se conserva la copia anterior.
        with _index(root) as local:
            if local is None:
                return "offline", 0
            rows = local.execute(
                "SELECT m.id, m.folder, m.title, m.start, m.duration_s, j.state FROM meetings m"
                " LEFT JOIN jobs j ON j.id=m.job_id ORDER BY m.start DESC"
            ).fetchall()
        scope = connection.scope
        with closing(_ledger()) as db, db:
            existing = {
                r[0]: r[1:]
                for r in db.execute(
                    "SELECT id, fingerprint, revision, acknowledged FROM sent WHERE scope=?",
                    (scope,),
                )
            }
            known_ids = {row["id"] for row in rows}
            deleted = sorted(existing.keys() - known_ids)
            work: list[tuple[int, sqlite3.Row | None]] = [(i, None) for i in deleted]
            work.extend((r["id"], r) for r in rows)
            sent, failures, tries = 0, 0, 0
            for meeting, row in work:
                if self._stop.is_set() or self._connection is not connection or self.recording():
                    return "paused", sent
                try:
                    change = self._change(root, row)
                except (OSError, ValueError):
                    failures += 1
                    continue
                if change is None:
                    continue
                stamp, folder = change
                old, rev, ack = existing.get(meeting, (None, 0, 0))
                if stamp == old and ack == -1:
                    failures += 1
                    continue
                if (stamp == old and ack == 1) or (folder is None and old is None):
                    continue
                if tries >= _MAX_PER_PASS:
                    return "pending", sent
                try:
                    doc = _document(folder, row) if folder is not None and row is not None else None
                    if row is not None and self._change(root, row) != change:
                        continue  # Los ficheros cambiaron mientras se leían.
                    revision = rev if stamp == old else rev + 1
                    data = json.dumps(
                        {"revision": revision, "document": doc},
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                except (OSError, ValueError, TypeError, KeyError):
                    failures += 1
                    continue
                if len(data) > MAX_DOCUMENT_BYTES:
                    failures += 1
                    continue
                if self._stop.is_set() or self._connection is not connection or self.recording():
                    return "paused", sent
                db.execute(
                    "INSERT OR REPLACE INTO sent VALUES (?,?,?,?,0)",
                    (scope, meeting, stamp, revision),
                )
                db.commit()  # Reintentar la misma revisión después de un cierre o un 204 perdido.
                tries += 1
                url = f"{connection.origin}/api/v1/meetings/{connection.library}/{meeting}"
                code, _ = _request(url, connection.key, data)
                if code == 204:
                    db.execute(
                        "UPDATE sent SET acknowledged=1 WHERE scope=? AND id=?", (scope, meeting)
                    )
                    db.commit()
                    sent += 1
                elif code in (401, 403):
                    return "credentials", sent
                elif code in (400, 409, 413):
                    db.execute(
                        "UPDATE sent SET acknowledged=-1 WHERE scope=? AND id=?", (scope, meeting)
                    )
                    db.commit()
                    failures += 1
                else:
                    return "offline", sent
            return ("error" if failures else "ready"), sent

    @staticmethod
    def _change(root: Path, row: sqlite3.Row | None) -> tuple[str, Path | None] | None:
        if row is None or row["state"] == "discarded":
            return "deleted", None
        if row["state"] not in ("done", None):
            return None
        folder = _completed_folder(root, row["folder"])
        if folder is None:
            candidate = root / row["folder"]
            if candidate.parent == root and _local_file(candidate, DISCARD_MARKER) is not None:
                return "deleted", None
            return None
        stamp = (
            row["title"],
            row["start"],
            row["duration_s"],
            _stamp(_local_file(folder, paths.TRANSCRIPT_JSON)),
            _stamp(_local_file(folder, ACTA_JSON)),
        )
        return hashlib.sha256(repr(stamp).encode("utf-8")).hexdigest(), folder
