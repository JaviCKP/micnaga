"""Configuracion (``config.toml``): dataclasses tipadas, ``tomllib`` + ``tomli-w``.

* ``load_config()`` nunca falla por un fichero ausente: devuelve los valores por defecto.
  Claves desconocidas se ignoran; un valor con tipo incorrecto lanza ``ConfigError``.
* ``save_config()`` escribe de forma atomica (los comentarios del fichero no se conservan).
* La ubicacion del fichero es ``paths.config_path()`` (``MICNAGA_HOME`` manda).
"""

from __future__ import annotations

import dataclasses
import os
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from micnaga import paths


class ConfigError(ValueError):
    """``config.toml`` invalido (tipo incorrecto o valor fuera de rango)."""


@dataclass
class DetectionConfig:
    browsers: list[str] = field(default_factory=lambda: ["chrome.exe", "msedge.exe", "brave.exe"])
    poll_interval_s: float = 1.0
    confirm_after_s: float = 5.0  # senales estables antes del popup
    end_after_s: float = 15.0  # una senal obligatoria caida este tiempo = fin
    popup_timeout_s: float = 15.0
    on_timeout: str = "provisional"  # provisional | discard
    # Ruta generica: cualquier app que use el micro (no solo Meet en un navegador).
    generic_apps: bool = True
    mic_grace_s: float = 20.0  # uso continuo del micro antes de grabar provisionalmente
    popup_after_recording_s: float = 20.0  # grabando provisional, cuando sale el popup
    exclude_apps: list[str] = field(default_factory=list)  # exe o PackageFamilyName a ignorar


@dataclass
class GatesConfig:
    min_duration_s: float = 180.0
    min_remote_speech_s: float = 30.0
    min_turns: int = 6


@dataclass
class CaptureConfig:
    sample_rate: int = 16000
    chunk_flush_s: float = 2.0
    sync_log_interval_s: float = 10.0
    flac_level: int = 5


@dataclass
class VadConfig:
    min_speech_ms: int = 250
    min_silence_ms: int = 500
    pad_ms: int = 200
    max_block_s: float = 30.0


@dataclass
class ProcessingConfig:
    asr_engine: str = "parakeet"  # parakeet | whisper
    whisper_model: str = "small"
    diarization: str = "onnx"  # onnx | diarize | pyannote
    aec: str = "auto"  # auto | webrtc | speex | off
    threads: int = 6
    low_priority: bool = True  # BELOW_NORMAL_PRIORITY_CLASS en el worker
    echo_similarity: float = 0.8  # etapa 9 (rapidfuzz, 0..1)
    merge_gap_s: float = 1.5  # etapa 11
    glossary: list[str] = field(default_factory=list)  # MIC-16


@dataclass
class StorageConfig:
    root: str = "~/Documents/Micnaga"
    models_dir: str = ""  # vacio = <app data>/models


@dataclass
class HotkeysConfig:
    marker: str = "ctrl+alt+m"
    private_pause: str = "ctrl+alt+p"


@dataclass
class UiConfig:
    theme: str = "system"  # system | light | dark
    language: str = "es"
    sound: bool = False  # sonido suave del popup


@dataclass
class PrivacyConfig:
    voiceprints_enabled: bool = False  # MIC-11, biometria: desactivada por defecto
    voiceprint_threshold: float = 0.75  # similitud minima para proponer un nombre
    remind_notice: bool = True  # «Recuerda avisar de que grabas» en el popup


@dataclass
class StartupConfig:
    run_at_login: bool = True


@dataclass
class Config:
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    gates: GatesConfig = field(default_factory=GatesConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    hotkeys: HotkeysConfig = field(default_factory=HotkeysConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    startup: StartupConfig = field(default_factory=StartupConfig)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def validate(self) -> None:
        """Comprueba los valores enumerados y los rangos; lanza ``ConfigError``."""
        _check_choice("detection.on_timeout", self.detection.on_timeout, ("provisional", "discard"))
        _check_choice("processing.asr_engine", self.processing.asr_engine, ("parakeet", "whisper"))
        _check_choice(
            "processing.diarization", self.processing.diarization, ("onnx", "diarize", "pyannote")
        )
        _check_choice("processing.aec", self.processing.aec, ("auto", "webrtc", "speex", "off"))
        _check_choice("ui.theme", self.ui.theme, ("system", "light", "dark"))
        d = self.detection
        for key in (
            "confirm_after_s",
            "popup_timeout_s",
            "mic_grace_s",
            "popup_after_recording_s",
        ):
            if getattr(d, key) < 0:
                raise ConfigError(f"detection.{key} must be >= 0")
        if d.poll_interval_s <= 0:
            raise ConfigError("detection.poll_interval_s must be > 0")
        if d.end_after_s <= 0:
            raise ConfigError("detection.end_after_s must be > 0")
        if self.processing.threads < 1:
            raise ConfigError("processing.threads must be >= 1")
        if self.capture.sample_rate <= 0:
            raise ConfigError("capture.sample_rate must be > 0")
        if not 0.0 <= self.processing.echo_similarity <= 1.0:
            raise ConfigError("processing.echo_similarity must be in [0, 1]")
        if not 0.0 <= self.privacy.voiceprint_threshold <= 1.0:
            raise ConfigError("privacy.voiceprint_threshold must be in [0, 1]")

    # Atajos utiles para el resto de modulos --------------------------------------------

    def meetings_root(self) -> Path:
        return paths.meetings_root(self)

    def models_dir(self) -> Path:
        return paths.models_dir(self)

    def db_path(self) -> Path:
        return paths.db_path(self)


def _check_choice(key: str, value: str, choices: tuple[str, ...]) -> None:
    if value not in choices:
        raise ConfigError(f"{key} must be one of {choices}, got {value!r}")


def _coerce(section: str, key: str, value: Any, default: Any) -> Any:
    """Valida que ``value`` tenga el tipo del valor por defecto (bool/int/float/str/list[str])."""
    where = f"{section}.{key}"
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be a boolean")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} must be an integer")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{where} must be a number")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"{where} must be a string")
        return value
    if isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{where} must be a list of strings")
        return list(value)
    raise ConfigError(f"{where}: unsupported config type")  # pragma: no cover


def _build_section(cls: type[Any], name: str, data: Mapping[str, Any]) -> Any:
    base = cls()
    values: dict[str, Any] = {}
    for f in dataclasses.fields(base):
        if f.name in data:
            values[f.name] = _coerce(name, f.name, data[f.name], getattr(base, f.name))
    return cls(**values)


def config_from_dict(data: Mapping[str, Any]) -> Config:
    """Construye un ``Config`` desde un dict (p. ej. el TOML ya parseado) y lo valida."""
    sections: dict[str, Any] = {}
    for f in dataclasses.fields(Config):
        raw = data.get(f.name, {})
        if not isinstance(raw, Mapping):
            raise ConfigError(f"[{f.name}] must be a table")
        section_cls = type(getattr(Config(), f.name))
        sections[f.name] = _build_section(section_cls, f.name, raw)
    cfg = Config(**sections)
    cfg.validate()
    return cfg


def load_config(path: Path | None = None) -> Config:
    """Lee ``config.toml`` (``paths.config_path()`` por defecto). Ausente = valores por defecto."""
    path = path or paths.config_path()
    if not path.exists():
        return Config()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    return config_from_dict(data)


def save_config(config: Config, path: Path | None = None) -> Path:
    """Escribe ``config.toml`` de forma atomica y devuelve la ruta."""
    config.validate()
    path = path or paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = tomli_w.dumps(config.to_dict())
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def ensure_config(path: Path | None = None) -> Config:
    """Carga la configuracion; si no existe el fichero lo crea con los valores por defecto."""
    path = path or paths.config_path()
    if not path.exists():
        cfg = Config()
        save_config(cfg, path)
        return cfg
    return load_config(path)
