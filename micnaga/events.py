"""Eventos JSON pequenos entre el proceso de trabajo (worker) y la UI (seccion 4).

Cada evento es un dataclass inmutable con un campo ``type`` fijo. En el cable viajan como
una linea de JSON (``encode_line`` / ``decode_line``); el transporte (tuberia con nombre,
ver ``pipe_name``) lo implementan ``pipeline/`` y ``app``. ``fraction`` es el avance global
del trabajo en [0, 1].

Roles del canal (convencion): la **UI/app es el servidor** de la tuberia y el **worker es el
cliente**; el worker conecta de forma perezosa, escribe un evento por linea y, si no hay
nadie escuchando (UI cerrada) o la escritura falla, **descarta el evento sin error**: la
fuente de verdad es la tabla ``jobs`` de SQLite, que la UI consulta al arrancar y puede
sondear. Los eventos son solo un aviso para refrescar la interfaz y mostrar el toast.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from typing import Any, ClassVar

from micnaga import paths


class UnknownEventError(ValueError):
    """Evento con un ``type`` desconocido o JSON mal formado."""


@dataclass(frozen=True)
class Event:
    type: ClassVar[str]

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, **dataclasses.asdict(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        kind = data.get("type")
        target = _REGISTRY.get(kind) if isinstance(kind, str) else None
        if target is None:
            raise UnknownEventError(f"unknown event type: {kind!r}")
        names = {f.name for f in dataclasses.fields(target)}
        try:
            return target(**{k: v for k, v in data.items() if k in names})
        except TypeError as exc:
            raise UnknownEventError(f"bad {kind} event: {exc}") from exc

    @classmethod
    def from_json(cls, text: str) -> Event:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UnknownEventError(f"invalid event JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise UnknownEventError("event JSON must be an object")
        return cls.from_dict(data)


@dataclass(frozen=True)
class JobStarted(Event):
    type: ClassVar[str] = "job_started"
    job_id: int
    session_dir: str


@dataclass(frozen=True)
class JobProgress(Event):
    type: ClassVar[str] = "job_progress"
    job_id: int
    stage: int
    """Indice 1-based en ``types.PIPELINE_STAGES``."""
    stage_name: str
    fraction: float


@dataclass(frozen=True)
class JobDone(Event):
    type: ClassVar[str] = "job_done"
    job_id: int
    session_dir: str
    title: str
    duration_s: float
    n_speakers: int


@dataclass(frozen=True)
class JobFailed(Event):
    type: ClassVar[str] = "job_failed"
    job_id: int
    session_dir: str
    error: str


@dataclass(frozen=True)
class JobDiscarded(Event):
    type: ClassVar[str] = "job_discarded"
    job_id: int
    session_dir: str
    reason: str


@dataclass(frozen=True)
class OpenMeeting(Event):
    type: ClassVar[str] = "open_meeting"
    meeting_id: int
    seconds: float
    library_id: str
    device_id: str | None = None


_REGISTRY: dict[str, type[Event]] = {
    cls.type: cls
    for cls in (JobStarted, JobProgress, JobDone, JobFailed, JobDiscarded, OpenMeeting)
}

EVENT_TYPES: tuple[str, ...] = tuple(_REGISTRY)


def encode_line(event: Event) -> bytes:
    """Evento -> una linea JSON UTF-8 terminada en ``\\n``."""
    return event.to_json().encode("utf-8") + b"\n"


def decode_line(line: bytes | str) -> Event:
    text = line.decode("utf-8") if isinstance(line, bytes) else line
    return Event.from_json(text.strip())


def pipe_name() -> str:
    """Nombre de la tuberia worker -> UI, distinto por carpeta de datos (``MICNAGA_HOME``).

    Asi dos instancias con ``MICNAGA_HOME`` distinto (tests, desarrollo) no se pisan.
    """
    digest = hashlib.sha1(str(paths.app_data_dir()).lower().encode("utf-8")).hexdigest()[:10]
    return rf"\\.\pipe\micnaga-events-{digest}"
