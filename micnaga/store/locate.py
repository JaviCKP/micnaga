"""Donde esta la carpeta de una reunion (hallazgo P1-4 de la revision).

Una reunion grabada antes de cambiar la carpeta de reuniones vive en la carpeta antigua, pero
puede estar indexada en el indice de ahora. La biblioteca (transcripcion, «Abrir carpeta»), el
servidor de audio y las ediciones de hablantes la buscan igual: primero bajo la raiz de ahora y,
si no esta, por la ruta que guarda el indice (``meetings.job_id`` -> ``jobs.session_dir``).
Nunca se acepta una ruta que no salga de la raiz o del indice, ni una que no se llame igual.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from micnaga import paths

log = logging.getLogger(__name__)

_BAD_CHARS = '/\\:*?"<>|\x00'


def valid_folder_name(name: object) -> bool:
    """Nombre de carpeta de reunion seguro (un solo componente, sin ``..`` ni separadores)."""
    if not isinstance(name, str) or not name or name in {".", ".."} or name.startswith("."):
        return False
    return not any(c in name for c in _BAD_CHARS)


def indexed_meeting_dir(folder: str, db_factory: Callable[[], Any]) -> Path | None:
    """Ruta que guarda el indice para ``folder`` si existe, se llama igual y tiene transcripcion."""
    try:
        db = db_factory()
    except Exception:
        log.debug("no index to look the meeting up", exc_info=True)
        return None
    try:
        rec = db.get_meeting(folder)
        if rec is None or rec.job_id is None:
            return None
        stored = Path(db.get_job(rec.job_id).session_dir)
    except Exception:
        log.debug("indexed folder lookup failed", exc_info=True)
        return None
    finally:
        with contextlib.suppress(Exception):
            db.close()
    if stored.name != folder or not (stored / paths.TRANSCRIPT_JSON).is_file():
        return None
    return stored


def meeting_dir(
    root: Path,
    folder: object,
    db_factory: Callable[[], Any] | None = None,
    *,
    require: str | None = None,
) -> Path | None:
    """Carpeta de la reunion ``folder``: bajo ``root`` o, si no esta (o le falta ``require``),
    la del indice. ``require``: fichero que debe existir dentro (si no, ``None``)."""
    if not valid_folder_name(folder):
        return None
    assert isinstance(folder, str)
    base = root.resolve()
    target = (base / folder).resolve()
    if (
        target.is_relative_to(base)
        and target != base
        and target.is_dir()
        and (require is None or (target / require).is_file())
    ):
        return target
    if db_factory is None:
        return None
    stored = indexed_meeting_dir(folder, db_factory)
    if stored is None or (require is not None and not (stored / require).is_file()):
        return None
    return stored


def existing_db_factory(db_file: Path) -> Callable[[], Any]:
    """Fabrica que abre ``db_file`` solo si ya existe (buscar no debe crear un indice vacio)."""

    def open_it() -> Any:
        if not db_file.is_file():
            raise FileNotFoundError(db_file)
        from micnaga.store.db import Database

        return Database(db_file)

    return open_it


__all__ = ["existing_db_factory", "indexed_meeting_dir", "meeting_dir", "valid_folder_name"]
