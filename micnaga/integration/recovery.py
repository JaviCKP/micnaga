"""Recuperacion al arrancar: sesiones huerfanas -> cola (spec §1.4, MIC-09).

1. ``recover_sessions``: carpetas que seguian en ``recording`` (cierre brusco).
2. Barrido de carpetas ``stopped`` / ``recovered`` / ``finalized`` sin trabajo en SQLite y sin
   ``transcripcion.json``: la app se cerro entre parar el grabador y encolar. Se encolan.

Las sesiones provisionales recuperadas pasan igualmente por las compuertas (etapa 1).
"""

from __future__ import annotations

import logging
from pathlib import Path

from micnaga import paths
from micnaga.capture import finalize_session, find_sessions, recover_sessions
from micnaga.capture.session import STATE_FINALIZED, STATE_RECOVERED, STATE_STOPPED
from micnaga.config import Config
from micnaga.store.db import Database

log = logging.getLogger(__name__)


def recover_and_enqueue(config: Config, db: Database) -> list[Path]:
    root = paths.meetings_root(config)
    candidates: list[Path] = list(recover_sessions(root))
    for d in find_sessions(root, (STATE_STOPPED, STATE_RECOVERED, STATE_FINALIZED)):
        if d in candidates or (d / paths.TRANSCRIPT_JSON).is_file():
            continue
        if db.find_job(d) is not None:
            continue
        candidates.append(d)
    queued: list[Path] = []
    for d in sorted(candidates):
        try:
            finalize_session(d, flac_level=config.capture.flac_level)
        except Exception:
            log.exception("could not finalize a recovered session; queueing it anyway")
        db.enqueue_job(d)
        queued.append(d)
    if queued:
        log.info("%d orphan session(s) queued at startup", len(queued))
    return queued
