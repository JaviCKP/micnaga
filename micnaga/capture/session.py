"""``capture.json``: lectura/escritura atomica y utilidades de sesion compartidas."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path

from micnaga import paths
from micnaga.types import CaptureMeta

log = logging.getLogger(__name__)

STATE_RECORDING = "recording"
STATE_STOPPED = "stopped"
STATE_RECOVERED = "recovered"
STATE_FINALIZED = "finalized"
"""Valores de ``capture.json -> state`` (campo ``extra``): ciclo de vida de la sesion."""

_REPLACE_RETRIES = 10


def capture_json_path(session_dir: Path) -> Path:
    return session_dir / paths.CAPTURE_JSON


def write_capture_json(session_dir: Path, meta: CaptureMeta) -> bool:
    """Escribe ``capture.json`` de forma atomica (tmp + ``os.replace``).

    En Windows ``os.replace`` falla si otro proceso (antivirus, visor) tiene el fichero abierto
    justo en ese instante; se reintenta unas decenas de ms y, si no se puede, se devuelve
    ``False`` sin lanzar (una grabacion no debe morir por esto).
    """
    target = capture_json_path(session_dir)
    tmp = target.with_name(target.name + ".tmp")
    text = meta.to_json()
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            with contextlib.suppress(OSError):
                os.fsync(f.fileno())
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, target)
                return True
            except PermissionError:
                if attempt == _REPLACE_RETRIES - 1:
                    raise
                time.sleep(0.02)
    except OSError as exc:
        log.warning("could not write %s: %s", target, exc)
        with contextlib.suppress(OSError):
            tmp.unlink()
    return False


def read_capture_json(session_dir: Path) -> CaptureMeta:
    """Lee ``capture.json`` de una carpeta de sesion (lanza si falta o es invalido)."""
    return CaptureMeta.from_json(capture_json_path(session_dir).read_text(encoding="utf-8"))


def read_state(session_dir: Path) -> str | None:
    """``extra["state"]`` de la sesion, o ``None`` si no hay ``capture.json`` legible."""
    try:
        data = json.loads(capture_json_path(session_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    state = data.get("state") if isinstance(data, dict) else None
    return state if isinstance(state, str) else None
