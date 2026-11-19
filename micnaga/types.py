"""Tipos compartidos entre modulos (contrato estable: NO editar sin coordinar).

Convenciones
------------
* **Audio en memoria**: ``AudioArray`` = ``np.ndarray`` mono, ``float32``, rango [-1, 1].
  La frecuencia de muestreo viaja siempre aparte (``sr: int``). Las librerias que quieren
  int16 / tramas de 10 ms (AEC) convierten dentro de su adaptador, no aqui.
* **Tiempos**: segundos (``float``) desde el inicio del audio grabado (la linea de tiempo
  del fichero, *sin* los tramos de pausa privada). Los tiempos de palabras, turnos,
  segmentos y marcadores comparten esa linea de tiempo.
* **Hablantes**: ``"me"`` es el usuario (pista del micro); ``"S1"``, ``"S2"``... son los
  hablantes remotos. Los nombres visibles viven en ``Speaker.name``.
* El JSON de ``transcripcion.json`` (seccion 9 de la spec) es la fuente de verdad:
  ``Transcript.to_dict`` / ``from_dict`` lo (de)serializan con exactamente ese esquema.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

import numpy as np
from numpy.typing import NDArray

AudioArray: TypeAlias = NDArray[np.float32]
"""Audio mono float32 en [-1, 1]."""

OutputKind: TypeAlias = Literal["headphones", "speakers", "unknown"]
"""Tipo de dispositivo de salida (seccion 7). ``unknown`` se trata como altavoces."""

SPEAKER_ME = "me"
SPEAKER_ME_NAME = "Yo"
TRANSCRIPT_VERSION = 1
CAPTURE_VERSION = 1

PIPELINE_STAGES: tuple[str, ...] = (
    "gates",  # 1  compuertas finales
    "align",  # 2  alineacion / deriva
    "aec",  # 3  eliminacion de eco (solo altavoces)
    "condition",  # 4  paso alto + normalizacion
    "vad",  # 5  deteccion de voz
    "asr",  # 6  transcripcion
    "diarize",  # 7  diarizacion de la pista remota
    "assign",  # 8  asignacion palabra -> hablante
    "echo_dedup",  # 9  eco, segunda capa
    "names",  # 10 huellas de voz + glosario
    "assemble",  # 11 montaje y salida
)
"""Nombres de las 11 etapas (seccion 8). ``jobs.stage`` = indice 1-based en esta tupla."""


def stage_name(stage: int) -> str:
    """Nombre de la etapa ``stage`` (1-based); ``"queued"`` para 0."""
    if stage <= 0:
        return "queued"
    return PIPELINE_STAGES[min(stage, len(PIPELINE_STAGES)) - 1]


# ----------------------------------------------------------------------------- pipeline


@dataclass(frozen=True)
class Word:
    """Palabra con marcas de tiempo (salida de ``AsrEngine``)."""

    w: str
    s: float
    e: float
    p: float = 1.0
    """Confianza en [0, 1]."""

    def to_dict(self) -> dict[str, Any]:
        return {"w": self.w, "s": self.s, "e": self.e, "p": self.p}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Word:
        return cls(w=str(d["w"]), s=float(d["s"]), e=float(d["e"]), p=float(d.get("p", 1.0)))


@dataclass(frozen=True)
class Turn:
    """Turno de un hablante (salida de ``Diarizer``)."""

    speaker: str
    start: float
    end: float


@dataclass(frozen=True)
class SpeechRegion:
    """Tramo de voz detectado por VAD."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class Marker:
    """Marcador del usuario (Ctrl+Alt+M)."""

    t: float
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"t": self.t, "label": self.label}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Marker:
        return cls(t=float(d["t"]), label=str(d.get("label", "")))


@dataclass(frozen=True)
class Speaker:
    id: str
    name: str
    voiceprint_match: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "name": self.name}
        if self.voiceprint_match is not None:
            d["voiceprint_match"] = self.voiceprint_match
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Speaker:
        vm = d.get("voiceprint_match")
        return cls(
            id=str(d["id"]), name=str(d["name"]), voiceprint_match=None if vm is None else float(vm)
        )


@dataclass(frozen=True)
class Segment:
    """Frase de un hablante con sus palabras."""

    start: float
    end: float
    speaker: str
    text: str
    words: list[Word] = field(default_factory=list)
    text_original: str | None = None
    """Texto del ASR antes de la limpieza con IA (``micnaga.brain.cleanup``); solo si cambio."""
    merged_from: list[int] = field(default_factory=list)
    """Indices (antes de la limpieza) de los segmentos que la limpieza unio en este."""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "start": self.start,
            "end": self.end,
            "speaker": self.speaker,
            "text": self.text,
            "words": [w.to_dict() for w in self.words],
        }
        if self.text_original is not None:  # opcionales: el JSON de siempre no cambia
            d["text_original"] = self.text_original
        if self.merged_from:
            d["merged_from"] = list(self.merged_from)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Segment:
        original = d.get("text_original")
        return cls(
            start=float(d["start"]),
            end=float(d["end"]),
            speaker=str(d["speaker"]),
            text=str(d["text"]),
            words=[Word.from_dict(w) for w in d.get("words", [])],
            text_original=None if original is None else str(original),
            merged_from=[int(i) for i in d.get("merged_from") or []],
        )


@dataclass(frozen=True)
class MeetingInfo:
    title: str
    start: str
    """Inicio ISO 8601 con zona horaria, p. ej. ``2026-11-18T10:30:00+01:00``."""
    duration_s: float
    source: str = "meet"
    meet_code: str | None = None
    output_device: OutputKind = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "start": self.start,
            "duration_s": self.duration_s,
            "source": self.source,
            "meet_code": self.meet_code,
            "output_device": self.output_device,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MeetingInfo:
        code = d.get("meet_code")
        return cls(
            title=str(d["title"]),
            start=str(d["start"]),
            duration_s=float(d["duration_s"]),
            source=str(d.get("source", "meet")),
            meet_code=None if code is None else str(code),
            output_device=d.get("output_device", "unknown"),
        )


@dataclass(frozen=True)
class EnginesInfo:
    asr: str
    diarization: str | None = None
    aec: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"asr": self.asr, "diarization": self.diarization}
        if self.aec is not None:
            d["aec"] = self.aec
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EnginesInfo:
        return cls(asr=str(d["asr"]), diarization=d.get("diarization"), aec=d.get("aec"))


@dataclass(frozen=True)
class Transcript:
    """Contenido completo de ``transcripcion.json`` (esquema version 1, seccion 9)."""

    meeting: MeetingInfo
    speakers: list[Speaker]
    segments: list[Segment]
    markers: list[Marker]
    engines: EnginesInfo
    version: int = TRANSCRIPT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "meeting": self.meeting.to_dict(),
            "speakers": [s.to_dict() for s in self.speakers],
            "segments": [s.to_dict() for s in self.segments],
            "markers": [m.to_dict() for m in self.markers],
            "engines": self.engines.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Transcript:
        version = int(d.get("version", TRANSCRIPT_VERSION))
        if version != TRANSCRIPT_VERSION:
            raise ValueError(f"unsupported transcript version: {version}")
        return cls(
            version=version,
            meeting=MeetingInfo.from_dict(d["meeting"]),
            speakers=[Speaker.from_dict(s) for s in d.get("speakers", [])],
            segments=[Segment.from_dict(s) for s in d.get("segments", [])],
            markers=[Marker.from_dict(m) for m in d.get("markers", [])],
            engines=EnginesInfo.from_dict(d["engines"]),
        )

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, text: str) -> Transcript:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("transcript JSON must be an object")
        return cls.from_dict(data)


# ----------------------------------------------------------------------------- captura


@dataclass(frozen=True)
class SyncMark:
    """Pareja (muestras escritas, reloj comun) de una pista (seccion 7, trampa 2).

    ``clock_s`` es el reloj comun (QueryPerformanceCounter) en segundos desde el inicio de
    la sesion, tomado al escribir la muestra ``samples``.
    """

    samples: int
    clock_s: float

    def to_dict(self) -> dict[str, Any]:
        return {"samples": self.samples, "clock_s": self.clock_s}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SyncMark:
        return cls(samples=int(d["samples"]), clock_s=float(d["clock_s"]))


@dataclass(frozen=True)
class TrackInfo:
    """Metadatos de una pista grabada (``mic`` o ``system``)."""

    file: str
    """Nombre del fichero dentro de la carpeta de sesion (WAV de trabajo o FLAC final)."""
    device: str
    sample_rate: int = 16000
    samples: int = 0
    sync: list[SyncMark] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "device": self.device,
            "sample_rate": self.sample_rate,
            "samples": self.samples,
            "sync": [m.to_dict() for m in self.sync],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TrackInfo:
        return cls(
            file=str(d["file"]),
            device=str(d.get("device", "")),
            sample_rate=int(d.get("sample_rate", 16000)),
            samples=int(d.get("samples", 0)),
            sync=[SyncMark.from_dict(m) for m in d.get("sync", [])],
        )


@dataclass(frozen=True)
class PrivatePause:
    """Pausa privada (MIC-13): no hay audio en disco para ese tramo.

    ``at_s`` es la posicion en la linea de tiempo del audio (donde se hace el corte);
    ``wall_s`` es lo que duro la pausa en tiempo real.
    """

    at_s: float
    wall_s: float

    def to_dict(self) -> dict[str, Any]:
        return {"at_s": self.at_s, "wall_s": self.wall_s}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PrivatePause:
        return cls(at_s=float(d["at_s"]), wall_s=float(d["wall_s"]))


@dataclass(frozen=True)
class DeviceChange:
    """Cambio de dispositivo a mitad de reunion (reenganche en < 1 s)."""

    t: float
    track: str
    """``"mic"`` o ``"system"``."""
    device: str
    output_kind: OutputKind = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": self.t,
            "track": self.track,
            "device": self.device,
            "output_kind": self.output_kind,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DeviceChange:
        return cls(
            t=float(d["t"]),
            track=str(d["track"]),
            device=str(d["device"]),
            output_kind=d.get("output_kind", "unknown"),
        )


@dataclass(frozen=True)
class CaptureMeta:
    """Contenido de ``capture.json`` (escribe ``capture/``, lee ``pipeline/``).

    Los campos desconocidos se conservan en ``extra`` para poder ampliar el formato sin
    romper a los lectores.
    """

    started_at: str
    """ISO 8601 con zona horaria."""
    mic: TrackInfo
    system: TrackInfo
    output_kind: OutputKind = "unknown"
    confirmed: bool = False
    """``True`` si el usuario pulso «Grabar» (solo exige la compuerta 1, seccion 5)."""
    meet_title: str | None = None
    meet_code: str | None = None
    pauses: list[PrivatePause] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    device_changes: list[DeviceChange] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    version: int = CAPTURE_VERSION

    _KNOWN = (
        "version",
        "started_at",
        "output_kind",
        "confirmed",
        "meet_title",
        "meet_code",
        "mic",
        "system",
        "pauses",
        "markers",
        "device_changes",
    )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "version": self.version,
            "started_at": self.started_at,
            "output_kind": self.output_kind,
            "confirmed": self.confirmed,
            "meet_title": self.meet_title,
            "meet_code": self.meet_code,
            "mic": self.mic.to_dict(),
            "system": self.system.to_dict(),
            "pauses": [p.to_dict() for p in self.pauses],
            "markers": [m.to_dict() for m in self.markers],
            "device_changes": [c.to_dict() for c in self.device_changes],
        }
        d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CaptureMeta:
        version = int(d.get("version", CAPTURE_VERSION))
        if version != CAPTURE_VERSION:
            raise ValueError(f"unsupported capture version: {version}")
        return cls(
            version=version,
            started_at=str(d["started_at"]),
            output_kind=d.get("output_kind", "unknown"),
            confirmed=bool(d.get("confirmed", False)),
            meet_title=d.get("meet_title"),
            meet_code=d.get("meet_code"),
            mic=TrackInfo.from_dict(d["mic"]),
            system=TrackInfo.from_dict(d["system"]),
            pauses=[PrivatePause.from_dict(p) for p in d.get("pauses", [])],
            markers=[Marker.from_dict(m) for m in d.get("markers", [])],
            device_changes=[DeviceChange.from_dict(c) for c in d.get("device_changes", [])],
            extra={k: v for k, v in d.items() if k not in cls._KNOWN},
        )

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, text: str) -> CaptureMeta:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("capture JSON must be an object")
        return cls.from_dict(data)
