"""Infraestructura comun de las etapas: contexto, dependencias inyectables y marcas de fin.

Cada etapa es una funcion ``run(ctx) -> StageResult`` que lee sus entradas de la carpeta de la
sesion / ``work/`` y escribe sus resultados en ``work/`` de forma atomica (fichero temporal +
``os.replace``). La marca de fin ``work/NN_nombre.done.json`` se escribe **la ultima**; una etapa
con marca y con todas sus salidas presentes se salta (idempotencia / reanudacion tras un cierre).

Memoria: ``engine_scope`` garantiza «un modelo cargado a la vez» (cerrar, ``del``,
``gc.collect()``).
"""

from __future__ import annotations

import contextlib
import gc
import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

from micnaga import paths
from micnaga.types import AudioArray, CaptureMeta, SpeechRegion

if TYPE_CHECKING:
    from micnaga.config import Config
    from micnaga.pipeline.aec.base import EchoCanceller
    from micnaga.pipeline.engines.asr.base import AsrEngine
    from micnaga.pipeline.engines.diar.base import Diarizer
    from micnaga.store.voiceprints import VoiceprintStore

log = logging.getLogger(__name__)

ALIGN_TOLERANCE_S = 0.05


class StageError(RuntimeError):
    """Fallo de una etapa (el trabajo pasa a ``failed`` y se conservan los ficheros)."""


class SessionDiscarded(Exception):
    """Las compuertas finales no se cumplen: la sesion se descarta y se borra."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _CloseableEngine(Protocol):
    def close(self) -> None: ...


class Aligner(Protocol):
    def __call__(
        self, mic: AudioArray, system: AudioArray, meta: CaptureMeta
    ) -> tuple[AudioArray, AudioArray]: ...


class SpeechDetector(Protocol):
    def __call__(self, audio: AudioArray, sr: int, config: Config) -> list[SpeechRegion]: ...


@dataclass
class Deps:
    """Dependencias externas de las etapas (inyectables en los tests)."""

    create_asr: Callable[[Config], AsrEngine]
    create_diarizer: Callable[[Config], Diarizer]
    create_aec: Callable[[Config], EchoCanceller | None]
    detect_speech: SpeechDetector
    group_regions: Callable[[Sequence[SpeechRegion], float], Any]
    """Devuelve bloques: ``(inicio, fin)``, ``SpeechRegion`` o listas de ``SpeechRegion``."""
    align_tracks: Aligner
    release_vad: Callable[[], None] = lambda: None
    voiceprints: VoiceprintStore | None = None


def default_deps() -> Deps:
    """Dependencias reales; todo se importa de forma perezosa (cuando la etapa lo usa)."""

    def create_asr(config: Config) -> AsrEngine:
        from micnaga.pipeline.engines.asr import create_asr as factory

        return factory(config)

    def create_diarizer(config: Config) -> Diarizer:
        from micnaga.pipeline.engines.diar import create_diarizer as factory

        return factory(config)

    def create_aec(config: Config) -> EchoCanceller | None:
        from micnaga.pipeline.aec import create_echo_canceller

        return create_echo_canceller(config)

    def detect_speech(audio: AudioArray, sr: int, config: Config) -> list[SpeechRegion]:
        from micnaga.pipeline.vad import detect_speech as detect

        v = config.vad
        return list(detect(audio, sr, v.min_speech_ms, v.min_silence_ms, v.pad_ms, config=config))

    def group_regions(regions: Sequence[SpeechRegion], max_len_s: float) -> Any:
        from micnaga.pipeline.vad import group_regions as group

        return group(regions, max_len_s)

    def align_tracks(
        mic: AudioArray, system: AudioArray, meta: CaptureMeta
    ) -> tuple[AudioArray, AudioArray]:
        from micnaga.capture.sync import align_tracks as align

        return align(mic, system, meta)

    def release_vad() -> None:
        try:
            from micnaga.pipeline.vad import release_model
        except ImportError:  # pragma: no cover
            return
        release_model()

    return Deps(
        create_asr=create_asr,
        create_diarizer=create_diarizer,
        create_aec=create_aec,
        detect_speech=detect_speech,
        group_regions=group_regions,
        align_tracks=align_tracks,
        release_vad=release_vad,
    )


@dataclass(frozen=True)
class StageResult:
    outputs: list[str] = field(default_factory=list)
    """Ficheros de ``work/`` que produjo (se comprueban al decidir si la etapa esta hecha)."""
    info: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Stage:
    index: int
    name: str
    run: Callable[[StageContext], StageResult]

    @property
    def marker_name(self) -> str:
        return f"{self.index:02d}_{self.name}.done.json"


# ----------------------------------------------------------------------------- E/S atomica


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def write_audio_file(path: Path, audio: AudioArray, sr: int) -> None:
    """WAV PCM 16 bits mono, atomico."""
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".wav")
    os.close(fd)
    try:
        sf.write(tmp, np.clip(audio, -1.0, 1.0), sr, subtype="PCM_16", format="WAV")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_audio_file(path: Path, target_sr: int) -> AudioArray:
    """Lee un WAV/FLAC como mono float32 a ``target_sr`` (remuestrea con soxr si hace falta)."""
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    mono = np.ascontiguousarray(mono, dtype=np.float32)
    if sr != target_sr:
        import soxr

        mono = np.asarray(soxr.resample(mono, sr, target_sr, quality="HQ"), dtype=np.float32)
    return mono


def audio_frames(path: Path) -> tuple[int, int]:
    """``(muestras, frecuencia)`` de un fichero de audio sin cargarlo."""
    import soundfile as sf

    info = sf.info(str(path))
    return int(info.frames), int(info.samplerate)


# ----------------------------------------------------------------------------- contexto


class StageContext:
    """Todo lo que una etapa necesita: rutas, configuracion, dependencias y E/S de ``work/``."""

    def __init__(
        self,
        session: Path,
        config: Config,
        deps: Deps,
        meta: CaptureMeta,
        progress: Callable[[float], None] | None = None,
    ) -> None:
        self.session = session
        self.work = paths.work_dir(session)
        self.config = config
        self.deps = deps
        self.meta = meta
        self.sr = config.capture.sample_rate
        self._progress = progress or (lambda _f: None)

    # -- progreso dentro de la etapa
    def progress(self, fraction: float) -> None:
        self._progress(min(1.0, max(0.0, fraction)))

    # -- ficheros de work/
    def path(self, name: str) -> Path:
        return self.work / name

    def exists(self, name: str) -> bool:
        return (self.work / name).is_file()

    def write_json(self, name: str, obj: Any) -> str:
        atomic_write_json(self.work / name, obj)
        return name

    def read_json(self, name: str) -> Any:
        return json.loads((self.work / name).read_text(encoding="utf-8"))

    def write_audio(self, name: str, audio: AudioArray) -> str:
        write_audio_file(self.work / name, audio, self.sr)
        return name

    def read_work_audio(self, name: str) -> AudioArray:
        return read_audio_file(self.work / name, self.sr)

    # -- audio original de la sesion (nombres tomados de capture.json)
    def source_track_path(self, track: str) -> Path:
        info = self.meta.mic if track == "mic" else self.meta.system
        flac, wav = (
            (paths.AUDIO_ME_FLAC, paths.AUDIO_ME_WAV)
            if track == "mic"
            else (paths.AUDIO_REMOTE_FLAC, paths.AUDIO_REMOTE_WAV)
        )
        for name in (info.file, flac, wav):
            candidate = self.session / name
            if name and candidate.is_file():
                return candidate
        raise StageError(f"audio of track {track!r} not found in session folder")

    def read_source_track(self, track: str) -> AudioArray:
        return read_audio_file(self.source_track_path(track), self.sr)

    # -- marcas de etapas anteriores
    def stage_info(self, name: str) -> dict[str, Any]:
        """``info`` que guardo la etapa ``name`` en su marca (``{}`` si no existe)."""
        idx = _stage_index(name)
        marker = self.work / f"{idx:02d}_{name}.done.json"
        if not marker.is_file():
            return {}
        data = json.loads(marker.read_text(encoding="utf-8"))
        return dict(data.get("info", {}))

    def conditioned(self, track: str) -> AudioArray:
        """Audio acondicionado (etapa 4) de una pista, entrada de las etapas 5-7."""
        name = "04_mic.wav" if track == "mic" else "04_system.wav"
        if not self.exists(name):
            raise StageError(f"conditioned audio of track {track!r} is missing")
        return self.read_work_audio(name)


def _stage_index(name: str) -> int:
    from micnaga.types import PIPELINE_STAGES

    return PIPELINE_STAGES.index(name) + 1


# ----------------------------------------------------------------------------- marcas


def read_marker(session: Path, stage: Stage) -> dict[str, Any] | None:
    marker = paths.work_dir(session) / stage.marker_name
    if not marker.is_file():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_stage_done(session: Path, stage: Stage) -> bool:
    data = read_marker(session, stage)
    if data is None:
        return False
    work = paths.work_dir(session)
    return all((work / out).is_file() for out in data.get("outputs", []))


def write_marker(session: Path, stage: Stage, result: StageResult, elapsed_s: float) -> None:
    payload = {
        "stage": stage.name,
        "index": stage.index,
        "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "elapsed_s": round(elapsed_s, 3),
        "outputs": result.outputs,
        "info": result.info,
    }
    atomic_write_json(paths.work_dir(session) / stage.marker_name, payload)


def clear_marker(session: Path, stage: Stage) -> None:
    with contextlib.suppress(OSError):
        (paths.work_dir(session) / stage.marker_name).unlink(missing_ok=True)


# ----------------------------------------------------------------------------- memoria


@contextmanager
def engine_scope(engine: _CloseableEngine) -> Iterator[_CloseableEngine]:
    """Usa un motor y lo libera (``close`` + ``gc.collect``) al salir, con o sin error."""
    try:
        yield engine
    finally:
        try:
            engine.close()
        except Exception:
            log.exception("error closing engine %r", type(engine).__name__)
        del engine
        gc.collect()
