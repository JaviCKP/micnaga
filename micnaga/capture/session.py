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


IN_PERSON_MARKER = ".micnaga-in-person.json"
"""Marca de «reunion presencial» escrita al empezar a grabar. ``capture.json`` no sirve mientras
graba (el grabador lo reescribe entero, ``extra`` incluido): tras parar, ``apply_in_person`` la
pasa a ``extra.in_person``. Si la app muere grabando, la marca sigue ahi (``is_in_person``)."""


def mark_in_person(session_dir: Path) -> bool:
    """Escribe la marca de reunion presencial (``False`` si no se pudo; nunca lanza)."""
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / IN_PERSON_MARKER).write_text('{"in_person": true}', encoding="utf-8")
        return True
    except OSError:
        log.exception("could not persist the in-person marker")
        return False


def apply_in_person(session_dir: Path) -> bool:
    """``capture.json -> extra.in_person = true`` (con el grabador ya parado). Tolerante: sin
    ``capture.json`` legible devuelve ``False`` sin lanzar."""
    import dataclasses

    from micnaga.gates import IN_PERSON_KEY

    try:
        meta = read_capture_json(session_dir)
    except (OSError, ValueError, KeyError):
        log.warning("in-person session has no readable capture.json")
        return False
    if meta.extra.get(IN_PERSON_KEY) is True:
        return True
    extra = {**meta.extra, IN_PERSON_KEY: True}
    return write_capture_json(session_dir, dataclasses.replace(meta, extra=extra))


def is_in_person(session_dir: Path, extra: object = None) -> bool:
    """Reunion presencial: ``extra.in_person`` de ``capture.json`` o la marca en la carpeta."""
    from micnaga.gates import in_person

    return in_person(extra) or (session_dir / IN_PERSON_MARKER).is_file()


def read_state(session_dir: Path) -> str | None:
    """``extra["state"]`` de la sesion, o ``None`` si no hay ``capture.json`` legible."""
    try:
        data = json.loads(capture_json_path(session_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    state = data.get("state") if isinstance(data, dict) else None
    return state if isinstance(state, str) else None
