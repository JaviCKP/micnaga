"""Rutas de Micnaga. Todo es sobreescribible por variables de entorno.

Variables (se leen **en cada llamada**, nunca al importar; este modulo no crea carpetas
salvo que se llame a ``ensure_dir``):

``MICNAGA_HOME``     carpeta de datos de la app (config, logs, huellas, modelos por defecto).
                     Por defecto ``%APPDATA%\\Micnaga``.
``MICNAGA_STORAGE``  carpeta raiz de las reuniones (+ ``micnaga.db``). Gana sobre
                     ``[storage] root`` de ``config.toml``.
``MICNAGA_MODELS``   carpeta de modelos. Gana sobre ``[storage] models_dir``; por defecto
                     ``<app data>/models``.

Los tests ponen las tres en carpetas temporales (``tests/conftest.py``).
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from micnaga.config import Config

ENV_HOME = "MICNAGA_HOME"
ENV_STORAGE = "MICNAGA_STORAGE"
ENV_MODELS = "MICNAGA_MODELS"

WORK_DIRNAME = "work"

# Nombres de fichero dentro de la carpeta de una reunion (spec seccion 9 / 7).
TRANSCRIPT_MD = "transcripcion.md"
TRANSCRIPT_SRT = "transcripcion.srt"
TRANSCRIPT_JSON = "transcripcion.json"
ANALYSIS_PENDING = ".micnaga-analysis-pending"
"""Marca duradera «acta pendiente»: la escribe el worker al terminar la transcripcion (con la
Secretaria activa) en la carpeta de la reunion, asi viaja con los renombrados. El arranque de la
Secretaria recoge todas las marcadas sin acta, sin limite de fecha ni de numero (hallazgo P2-c:
el aviso ``JobDone`` puede perderse)."""
AUDIO_ME_FLAC = "audio_yo.flac"
AUDIO_REMOTE_FLAC = "audio_remoto.flac"
CAPTURE_JSON = "capture.json"
# WAV de trabajo durante la grabacion (se convierten a FLAC al confirmar la sesion).
AUDIO_ME_WAV = "audio_yo.wav"
AUDIO_REMOTE_WAV = "audio_remoto.wav"
SLUG_MAX_LEN = 60
DEFAULT_SLUG = "reunion"


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def ensure_dir(path: Path) -> Path:
    """Crea ``path`` (y padres) si no existe y lo devuelve."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def app_data_dir() -> Path:
    """``MICNAGA_HOME`` o ``%APPDATA%\\Micnaga``."""
    env = _env_path(ENV_HOME)
    if env is not None:
        return env
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Micnaga"


def config_path() -> Path:
    return app_data_dir() / "config.toml"


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def voiceprints_dir() -> Path:
    return app_data_dir() / "voiceprints"


def models_dir(config: Config | None = None) -> Path:
    """``MICNAGA_MODELS`` > ``[storage] models_dir`` > ``<app data>/models``."""
    env = _env_path(ENV_MODELS)
    if env is not None:
        return env
    if config is None:
        config = _load_config()
    if config.storage.models_dir.strip():
        return Path(config.storage.models_dir).expanduser()
    return app_data_dir() / "models"


def meetings_root(config: Config | None = None) -> Path:
    """``MICNAGA_STORAGE`` > ``[storage] root`` (con ``~`` expandido)."""
    env = _env_path(ENV_STORAGE)
    if env is not None:
        return env
    if config is None:
        config = _load_config()
    return Path(config.storage.root).expanduser()


def db_path(config: Config | None = None) -> Path:
    """Indice SQLite: ``<meetings root>/micnaga.db``."""
    return meetings_root(config) / "micnaga.db"


def session_dir(folder_name: str, config: Config | None = None) -> Path:
    """Carpeta de una reunion/sesion dentro de la raiz de reuniones."""
    return meetings_root(config) / folder_name


def work_dir(session: Path) -> Path:
    """Carpeta ``work/`` con los resultados intermedios de cada etapa."""
    return session / WORK_DIRNAME


def _load_config() -> Config:
    from micnaga.config import load_config  # import tardio: config importa paths

    return load_config()


# ------------------------------------------------------------------------------ nombres


def slugify(title: str, *, max_len: int = SLUG_MAX_LEN) -> str:
    """Minusculas, sin acentos, sin espacios: ``"Revisión semanal!"`` -> ``"revision-semanal"``."""
    text = unicodedata.normalize("NFKD", title)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    text = text[:max_len].strip("-")
    return text or DEFAULT_SLUG


def meeting_folder_name(start: datetime, title: str) -> str:
    """``YYYY-MM-DD_HHMM_slug``, p. ej. ``2026-11-18_1030_revision-semanal``."""
    return f"{start:%Y-%m-%d_%H%M}_{slugify(title)}"


def unique_folder(root: Path, name: str) -> Path:
    """``root/name``; si existe, ``root/name-2``, ``-3``... (no crea nada)."""
    candidate = root / name
    n = 2
    while candidate.exists():
        candidate = root / f"{name}-{n}"
        n += 1
    return candidate
