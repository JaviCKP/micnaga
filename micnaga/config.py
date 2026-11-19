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
    end_after_s: float = 15.0  # una reunion ignorada se olvida tras este tiempo sin verse
    popup_timeout_s: float = 15.0  # sin respuesta: el popup se convierte en pastilla provisional
    on_timeout: str = "provisional"  # provisional | discard (discard = apartar, nunca borrar)
    end_confirm_s: float = 2.0  # micro soltado este tiempo seguido = fin -> popup de fin
    end_popup_timeout_s: float = 10.0  # popup de fin sin respuesta: termina (o aparta)
    undo_s: float = 8.0  # «Ahora no» / «Descartar»: tiempo para deshacer
    # Ruta generica: cualquier app que use el micro (no solo Meet en un navegador).
    generic_apps: bool = True
    mic_grace_s: float = 20.0  # uso continuo del micro antes de grabar provisionalmente
    popup_after_recording_s: float = 20.0  # grabando provisional, cuando sale el popup
    exclude_apps: list[str] = field(default_factory=list)  # exe o PackageFamilyName a ignorar


@dataclass
class GatesConfig:
    # Desvio pedido por el usuario (DECISIONS §18): decide el usuario con el popup, no las
    # compuertas. Solo queda una comprobacion minima de duracion; 0 desactiva cada compuerta.
    min_duration_s: float = 30.0
    min_remote_speech_s: float = 0.0
    min_turns: int = 0
    require_confirmed: bool = True  # una sesion sin «Si» nunca se transcribe (se aparta)


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
    discarded_keep_days: int = 7  # «Descartadas»: audio apartado (sin transcribir) N dias


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


CONFIG_VERSION = 2


@dataclass
class MetaConfig:
    version: int = CONFIG_VERSION  # formato de config.toml (ver ``_migrate``)


@dataclass
class SecretariaConfig:
    """Capa de IA (``micnaga/brain``, ``docs/design/ai_brain.md``). Los secretos (token o clave
    de Claude, clave de Odoo) viven en el Administrador de credenciales, nunca aqui."""

    enabled: bool = False  # se activa al conectar Claude (Ajustes > Secretaria)
    auth: str = "session"  # session | token | api_key
    model_acta: str = "sonnet"
    model_title: str = "haiku"
    live_title: bool = True  # titulo provisional hacia el minuto 2 de una grabacion confirmada
    odoo_url: str = ""
    odoo_db: str = ""
    odoo_login: str = ""
    # Servidores MCP extra (solo lectura, contexto): un objeto JSON por linea, p. ej.
    # {"name": "drive", "command": "npx", "args": ["..."], "env": {}, "tools": ["search"]}
    mcp_servers: list[str] = field(default_factory=list)


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
    meta: MetaConfig = field(default_factory=MetaConfig)
    secretaria: SecretariaConfig = field(default_factory=SecretariaConfig)

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
            "end_confirm_s",
            "end_popup_timeout_s",
            "undo_s",
        ):
            if getattr(d, key) < 0:
                raise ConfigError(f"detection.{key} must be >= 0")
        if d.poll_interval_s <= 0:
            raise ConfigError("detection.poll_interval_s must be > 0")
        if d.end_after_s <= 0:
            raise ConfigError("detection.end_after_s must be > 0")
        if self.storage.discarded_keep_days < 1:
            raise ConfigError("storage.discarded_keep_days must be >= 1")
        if self.processing.threads < 1:
            raise ConfigError("processing.threads must be >= 1")
        if self.capture.sample_rate <= 0:
            raise ConfigError("capture.sample_rate must be > 0")
        if not 0.0 <= self.processing.echo_similarity <= 1.0:
            raise ConfigError("processing.echo_similarity must be in [0, 1]")
        if not 0.0 <= self.privacy.voiceprint_threshold <= 1.0:
            raise ConfigError("privacy.voiceprint_threshold must be in [0, 1]")
        sec = self.secretaria
        _check_choice("secretaria.auth", sec.auth, ("session", "token", "api_key"))
        _check_choice("secretaria.model_acta", sec.model_acta, ("sonnet", "opus", "haiku"))
        _check_choice("secretaria.model_title", sec.model_title, ("haiku", "sonnet"))
        for line in sec.mcp_servers:
            _check_mcp_server(line)

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


MCP_SERVER_NAME_RE = r"^[a-z][a-z0-9_-]{0,31}$"


def _check_mcp_server(line: str) -> None:
    """Una linea de ``secretaria.mcp_servers``: objeto JSON con ``name``, ``command``, ``args``
    (lista), ``env`` (objeto) y ``tools`` (las herramientas permitidas, al menos una)."""
    import json
    import re

    where = "secretaria.mcp_servers"
    try:
        data = json.loads(line)
    except ValueError as exc:
        raise ConfigError(f"{where}: each line must be a JSON object ({exc})") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: each line must be a JSON object")
    name = data.get("name")
    if not isinstance(name, str) or not re.match(MCP_SERVER_NAME_RE, name) or name == "micnaga":
        raise ConfigError(f"{where}: invalid server name {name!r}")
    if not isinstance(data.get("command"), str) or not data["command"].strip():
        raise ConfigError(f"{where}: {name}: 'command' must be a non-empty string")
    args = data.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ConfigError(f"{where}: {name}: 'args' must be a list of strings")
    env = data.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise ConfigError(f"{where}: {name}: 'env' must map strings to strings")
    tools = data.get("tools")
    if (
        not isinstance(tools, list)
        or not tools
        or not all(isinstance(t, str) and re.match(r"^[A-Za-z0-9_.-]+$", t) for t in tools)
    ):
        raise ConfigError(f"{where}: {name}: 'tools' must list the allowed tool names")


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


_V1_GATES: dict[str, float] = {"min_duration_s": 180.0, "min_remote_speech_s": 30.0, "min_turns": 6}


def _migrate(data: Mapping[str, Any]) -> dict[str, Any]:
    """Sube un ``config.toml`` antiguo al formato actual.

    v1 -> v2: las compuertas por defecto pasan a ``30 s / 0 / 0`` (decide el usuario con el
    popup). Solo se cambian los valores que seguian en el defecto antiguo: un valor elegido por
    el usuario se respeta. ``save_config`` escribe todas las claves, asi que sin esto un fichero
    guardado una vez fijaria para siempre los defectos viejos.
    """
    out: dict[str, Any] = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in data.items()}
    meta = out.get("meta")
    version = meta.get("version", 1) if isinstance(meta, Mapping) else 1
    if not isinstance(version, int) or isinstance(version, bool):
        version = 1
    if version < 2:
        gates = out.get("gates")
        if isinstance(gates, dict):
            for key, old in _V1_GATES.items():
                if key in gates and gates[key] == old:
                    gates[key] = getattr(GatesConfig(), key)
    out["meta"] = {"version": CONFIG_VERSION}
    return out


def config_from_dict(data: Mapping[str, Any]) -> Config:
    """Construye un ``Config`` desde un dict (p. ej. el TOML ya parseado) y lo valida."""
    data = _migrate(data)
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
    cfg = load_config(path)
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        meta = raw.get("meta")
        if not isinstance(meta, dict) or meta.get("version") != CONFIG_VERSION:
            save_config(cfg, path)  # migrado: se reescribe una vez con el formato actual
    except (OSError, tomllib.TOMLDecodeError, ConfigError):
        pass
    return cfg
