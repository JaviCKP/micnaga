"""SQLite compartido entre procesos (app/captura y worker): cola de trabajos + indice FTS5.

Reglas de uso
-------------
* Una instancia de ``Database`` por hilo/proceso (``sqlite3`` por defecto no comparte
  conexiones entre hilos). WAL + ``busy_timeout`` permiten varios procesos a la vez.
* Las transiciones de estado son atomicas (``BEGIN IMMEDIATE`` + ``UPDATE ... WHERE state``)
  y lanzan ``InvalidTransition`` si el trabajo no esta en un estado de origen valido.
* ``jobs.stage`` es el indice 1-based de la **ultima etapa iniciada** (``types.PIPELINE_STAGES``);
  la reanudacion real se basa en los ficheros de ``work/`` (cada etapa es idempotente).
* ``discard_log`` solo guarda fecha y motivo (spec, seccion 5): nada que identifique la
  reunion. Al descartar, ademas, ``jobs.session_dir`` se sustituye por ``"discarded:<id>"``
  (la carpeta lleva fecha y titulo); el llamador debe conservar la ruta original para
  borrar la carpeta, que ``discard_job`` NO borra.

Maquina de estados::

    pending --claim--> processing --finish--> done
       |                  | |--fail-------> failed --retry--> pending
       |                  | |--discard----> discarded
       |                  '--requeue_stale (arranque tras cierre inesperado)--> pending
       '--discard------------------------------------------------------------> discarded
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

from micnaga.types import Segment, Speaker, Transcript

if TYPE_CHECKING:
    from micnaga.config import Config

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 10_000
DEFAULT_MAX_ATTEMPTS = 3


class JobState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    DISCARDED = "discarded"


class JobNotFoundError(LookupError):
    """No existe un trabajo con ese id."""


class InvalidTransition(RuntimeError):
    """El trabajo no esta en un estado desde el que se permita la operacion."""


@dataclass(frozen=True)
class Job:
    id: int
    session_dir: str
    state: JobState
    stage: int
    error: str | None
    attempts: int
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class MeetingRecord:
    folder: str
    """Nombre de la carpeta de la reunion (relativo a la raiz de reuniones)."""
    title: str
    start: str
    """ISO 8601."""
    duration_s: float
    meet_code: str | None = None
    output_device: str | None = None
    speakers: list[Speaker] = field(default_factory=list)
    job_id: int | None = None
    id: int | None = None


@dataclass(frozen=True)
class SearchHit:
    meeting_id: int
    folder: str
    title: str
    segment_id: int
    start: float
    speaker: str
    text: str
    snippet: str
    """Texto con las coincidencias entre ``<mark>`` y ``</mark>``."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_dir TEXT    NOT NULL UNIQUE,
    state       TEXT    NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','processing','done','failed','discarded')),
    stage       INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs (state, id);

CREATE TABLE IF NOT EXISTS meetings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    folder        TEXT    NOT NULL UNIQUE,
    title         TEXT    NOT NULL,
    start         TEXT    NOT NULL,
    duration_s    REAL    NOT NULL,
    meet_code     TEXT,
    output_device TEXT,
    speakers      TEXT    NOT NULL DEFAULT '[]',
    job_id        INTEGER REFERENCES jobs (id) ON DELETE SET NULL,
    created_at    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS meetings_start_idx ON meetings (start);

CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings (id) ON DELETE CASCADE,
    start      REAL    NOT NULL,
    "end"      REAL    NOT NULL,
    speaker    TEXT    NOT NULL,
    text       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS segments_meeting_idx ON segments (meeting_id, start);

-- Texto completo (MIC-14). Contenido externo: el texto vive solo en `segments`.
-- remove_diacritics 2: "reunion" encuentra "reunión".
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5 (
    text,
    content = 'segments',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts (segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE OF text ON segments BEGIN
    INSERT INTO segments_fts (segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO segments_fts (rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS discard_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    date   TEXT NOT NULL,
    reason TEXT NOT NULL
);
"""


def fts_query(text: str) -> str:
    """Convierte texto libre en una consulta FTS5 segura (AND de prefijos entre comillas).

    ``"revision sema"`` -> ``'"revision"* "sema"*'``. Devuelve ``""`` si no hay palabras.
    """
    tokens = re.findall(r"\w+", text, flags=re.UNICODE)
    return " ".join(f'"{t}"*' for t in tokens)


class Database:
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self.path = str(path)
        self._clock = clock
        self._conn = sqlite3.connect(
            self.path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    # ---------------------------------------------------------------- ciclo de vida

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        """Conexion cruda (para consultas puntuales de otros modulos)."""
        return self._conn

    def _migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version < SCHEMA_VERSION:
            with self.transaction() as conn:
                for statement in _split_script(_SCHEMA):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT`` (``ROLLBACK`` si hay excepcion)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ---------------------------------------------------------------- jobs

    def enqueue_job(self, session_dir: Path | str) -> Job:
        """Crea un trabajo ``pending`` para la sesion; si ya existe devuelve el existente."""
        key = str(session_dir)
        now = self._clock()
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO jobs"
                " (session_dir, state, stage, attempts, created_at, updated_at)"
                " VALUES (?, 'pending', 0, 0, ?, ?)",
                (key, now, now),
            )
            row = conn.execute("SELECT * FROM jobs WHERE session_dir = ?", (key,)).fetchone()
        return _job(row)

    def get_job(self, job_id: int) -> Job:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return _job(row)

    def find_job(self, session_dir: Path | str) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE session_dir = ?", (str(session_dir),)
        ).fetchone()
        return None if row is None else _job(row)

    def list_jobs(self, state: JobState | None = None) -> list[Job]:
        if state is None:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE state = ? ORDER BY id", (state.value,)
            ).fetchall()
        return [_job(r) for r in rows]

    def count_jobs(self) -> dict[JobState, int]:
        counts = dict.fromkeys(JobState, 0)
        for row in self._conn.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"):
            counts[JobState(row["state"])] = int(row["n"])
        return counts

    def list_open_jobs(self) -> list[Job]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE state IN ('pending', 'processing') ORDER BY id"
        )
        return [_job(row) for row in rows]

    def claim_next_job(self) -> Job | None:
        """Toma atomicamente el trabajo ``pending`` mas antiguo y lo pasa a ``processing``."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE state = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET state = 'processing', attempts = attempts + 1, error = NULL,"
                " updated_at = ? WHERE id = ?",
                (self._clock(), row["id"]),
            )
            claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        return _job(claimed)

    def set_stage(self, job_id: int, stage: int) -> Job:
        """Anota la etapa en curso (solo mientras esta ``processing``)."""
        return self._transition(
            job_id, None, (JobState.PROCESSING,), "stage = ?", (stage,), "set_stage"
        )

    def touch_job_progress(self, job_id: int) -> None:
        self._conn.execute(
            "UPDATE jobs SET updated_at = ? WHERE id = ? AND state = 'processing'",
            (self._clock(), job_id),
        )

    def finish_job(self, job_id: int) -> Job:
        return self._transition(job_id, JobState.DONE, (JobState.PROCESSING,), op="finish")

    def fail_job(self, job_id: int, error: str) -> Job:
        return self._transition(
            job_id, JobState.FAILED, (JobState.PROCESSING,), "error = ?", (error,), "fail"
        )

    def discard_job(self, job_id: int, reason: str) -> Job:
        """``pending|processing`` -> ``discarded`` y anota fecha + motivo en ``discard_log``."""
        with self.transaction() as conn:
            job = self._transition_in(
                conn,
                job_id,
                JobState.DISCARDED,
                (JobState.PENDING, JobState.PROCESSING),
                "",
                (),
                "discard",
            )
            conn.execute(
                "INSERT INTO discard_log (date, reason) VALUES (?, ?)",
                (date.fromtimestamp(self._clock()).isoformat(), reason),
            )
            # Privacidad (spec 5): no queda rastro de la carpeta (fecha + titulo) en `jobs`.
            conn.execute(
                "UPDATE jobs SET session_dir = ? WHERE id = ?", (f"discarded:{job_id}", job_id)
            )
            job = replace(job, session_dir=f"discarded:{job_id}")
        return job

    def set_session_dir(self, job_id: int, session_dir: Path | str) -> None:
        """Cambia la carpeta del trabajo (el pipeline la renombra al terminar)."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET session_dir = ?, updated_at = ? WHERE id = ?",
                (str(session_dir), self._clock(), job_id),
            )

    def retry_job(self, job_id: int, *, reset_attempts: bool = False) -> Job:
        """``failed`` -> ``pending`` (accion manual del usuario). ``reset_attempts`` vuelve a
        contar los intentos desde cero (un reintento explicito no hereda el limite agotado)."""
        extra = "error = NULL, attempts = 0" if reset_attempts else "error = NULL"
        return self._transition(job_id, JobState.PENDING, (JobState.FAILED,), extra, (), "retry")

    def requeue_stale(self, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> list[Job]:
        """Recupera trabajos que quedaron ``processing`` por un cierre inesperado.

        Llamar al arrancar el worker (solo hay un worker, asi que todo ``processing`` es
        huerfano). Si ya se intento ``max_attempts`` veces pasa a ``failed``; si no, vuelve
        a ``pending`` conservando ``stage`` y los ficheros de ``work/``.
        """
        changed: list[int] = []
        now = self._clock()
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id, attempts FROM jobs WHERE state = 'processing'"
            ).fetchall()
            for row in rows:
                if row["attempts"] >= max_attempts:
                    conn.execute(
                        "UPDATE jobs SET state = 'failed', error = ?, updated_at = ? WHERE id = ?",
                        ("interrupted too many times", now, row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET state = 'pending', updated_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
                changed.append(int(row["id"]))
        return [self.get_job(i) for i in changed]

    def _transition(
        self,
        job_id: int,
        to: JobState | None,
        allowed_from: tuple[JobState, ...],
        extra_set: str = "",
        extra_args: tuple[object, ...] = (),
        op: str = "transition",
    ) -> Job:
        with self.transaction() as conn:
            return self._transition_in(conn, job_id, to, allowed_from, extra_set, extra_args, op)

    def _transition_in(
        self,
        conn: sqlite3.Connection,
        job_id: int,
        to: JobState | None,
        allowed_from: tuple[JobState, ...],
        extra_set: str,
        extra_args: tuple[object, ...],
        op: str,
    ) -> Job:
        sets = ["updated_at = ?"]
        args: list[object] = [self._clock()]
        if to is not None:
            sets.append("state = ?")
            args.append(to.value)
        if extra_set:
            sets.append(extra_set)
            args.extend(extra_args)
        placeholders = ",".join("?" for _ in allowed_from)
        args.extend([job_id, *[s.value for s in allowed_from]])
        cur = conn.execute(
            f"UPDATE jobs SET {', '.join(sets)} WHERE id = ? AND state IN ({placeholders})",
            args,
        )
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        if cur.rowcount == 0:
            raise InvalidTransition(f"cannot {op} job {job_id} in state {row['state']!r}")
        return _job(row)

    # ---------------------------------------------------------------- reuniones + FTS

    def upsert_meeting(self, record: MeetingRecord, segments: Sequence[Segment] = ()) -> int:
        """Inserta/actualiza la reunion (clave: ``folder``) y reemplaza sus segmentos."""
        speakers = json.dumps([s.to_dict() for s in record.speakers], ensure_ascii=False)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meetings (folder, title, start, duration_s, meet_code, output_device,"
                " speakers, job_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (folder) DO UPDATE SET title = excluded.title,"
                " start = excluded.start,"
                " duration_s = excluded.duration_s, meet_code = excluded.meet_code,"
                " output_device = excluded.output_device, speakers = excluded.speakers,"
                " job_id = COALESCE(excluded.job_id, meetings.job_id)",
                (
                    record.folder,
                    record.title,
                    record.start,
                    record.duration_s,
                    record.meet_code,
                    record.output_device,
                    speakers,
                    record.job_id,
                    self._clock(),
                ),
            )
            meeting_id = int(
                conn.execute(
                    "SELECT id FROM meetings WHERE folder = ?", (record.folder,)
                ).fetchone()[0]
            )
            self._replace_segments(conn, meeting_id, segments)
        return meeting_id

    def index_transcript(
        self, folder: str, transcript: Transcript, job_id: int | None = None
    ) -> int:
        """Indexa un ``Transcript`` completo (etapa 11 y renombrado de hablantes)."""
        m = transcript.meeting
        record = MeetingRecord(
            folder=folder,
            title=m.title,
            start=m.start,
            duration_s=m.duration_s,
            meet_code=m.meet_code,
            output_device=m.output_device,
            speakers=list(transcript.speakers),
            job_id=job_id,
        )
        return self.upsert_meeting(record, transcript.segments)

    def replace_segments(self, meeting_id: int, segments: Sequence[Segment]) -> None:
        with self.transaction() as conn:
            self._replace_segments(conn, meeting_id, segments)

    @staticmethod
    def _replace_segments(
        conn: sqlite3.Connection, meeting_id: int, segments: Sequence[Segment]
    ) -> None:
        conn.execute("DELETE FROM segments WHERE meeting_id = ?", (meeting_id,))
        conn.executemany(
            'INSERT INTO segments (meeting_id, start, "end", speaker, text) VALUES (?,?,?,?,?)',
            [(meeting_id, s.start, s.end, s.speaker, s.text) for s in segments],
        )

    def get_meeting(self, folder: str) -> MeetingRecord | None:
        row = self._conn.execute("SELECT * FROM meetings WHERE folder = ?", (folder,)).fetchone()
        return None if row is None else _meeting(row)

    def get_meeting_by_id(self, meeting_id: int) -> MeetingRecord | None:
        row = self._conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return None if row is None else _meeting(row)

    def list_meetings(self, limit: int = 200, offset: int = 0) -> list[MeetingRecord]:
        rows = self._conn.execute(
            "SELECT * FROM meetings ORDER BY start DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return [_meeting(r) for r in rows]

    def delete_meeting(self, folder: str) -> bool:
        """Borra la reunion y (en cascada) sus segmentos e indice FTS."""
        with self.transaction() as conn:
            return conn.execute("DELETE FROM meetings WHERE folder = ?", (folder,)).rowcount > 0

    def search(self, query: str, limit: int = 50, *, folder: str | None = None) -> list[SearchHit]:
        """Busqueda de texto completo por relevancia; ignora acentos y mayusculas."""
        match = fts_query(query)
        if not match:
            return []
        sql = (
            "SELECT s.id AS segment_id, s.meeting_id, m.folder, m.title, s.start, s.speaker,"
            " s.text,"
            " snippet(segments_fts, 0, '<mark>', '</mark>', '…', 16) AS snippet"
            " FROM segments_fts JOIN segments s ON s.id = segments_fts.rowid"
            " JOIN meetings m ON m.id = s.meeting_id WHERE segments_fts MATCH ?"
        )
        args: list[object] = [match]
        if folder is not None:
            sql += " AND m.folder = ?"
            args.append(folder)
        sql += " ORDER BY bm25(segments_fts) LIMIT ?"
        args.append(limit)
        return [
            SearchHit(
                meeting_id=int(r["meeting_id"]),
                folder=r["folder"],
                title=r["title"],
                segment_id=int(r["segment_id"]),
                start=float(r["start"]),
                speaker=r["speaker"],
                text=r["text"],
                snippet=r["snippet"],
            )
            for r in self._conn.execute(sql, args)
        ]

    # ---------------------------------------------------------------- discard_log

    def log_discard(self, reason: str, when: date | None = None) -> None:
        """Anota una descarga (solo fecha y motivo)."""
        day = (when or date.fromtimestamp(self._clock())).isoformat()
        with self.transaction() as conn:
            conn.execute("INSERT INTO discard_log (date, reason) VALUES (?, ?)", (day, reason))

    def discard_entries(self) -> list[tuple[str, str]]:
        rows = self._conn.execute("SELECT date, reason FROM discard_log ORDER BY id").fetchall()
        return [(r["date"], r["reason"]) for r in rows]


def open_database(config: Config | None = None) -> Database:
    """Abre (creando carpetas y esquema) el indice por defecto: ``paths.db_path()``."""
    from micnaga import paths

    path = paths.db_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    return Database(path)


def _split_script(script: str) -> list[str]:
    """Divide el script de esquema en sentencias (los triggers contienen ``;`` internos)."""
    statements: list[str] = []
    buf: list[str] = []
    in_trigger = False
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.upper().startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if stripped.upper() == "END;":
                statements.append("\n".join(buf))
                buf.clear()
                in_trigger = False
        elif stripped.endswith(";"):
            statements.append("\n".join(buf))
            buf.clear()
    return statements


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=int(row["id"]),
        session_dir=row["session_dir"],
        state=JobState(row["state"]),
        stage=int(row["stage"]),
        error=row["error"],
        attempts=int(row["attempts"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _meeting(row: sqlite3.Row) -> MeetingRecord:
    return MeetingRecord(
        id=int(row["id"]),
        folder=row["folder"],
        title=row["title"],
        start=row["start"],
        duration_s=float(row["duration_s"]),
        meet_code=row["meet_code"],
        output_device=row["output_device"],
        speakers=[Speaker.from_dict(s) for s in json.loads(row["speakers"])],
        job_id=row["job_id"],
    )
