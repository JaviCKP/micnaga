"""Recuperacion al arrancar: sesiones huerfanas -> cola o «Descartadas» (spec §1.4, MIC-09).

0. Se purgan las sesiones apartadas hace mas de ``storage.discarded_keep_days`` dias.
1. Carpetas con la marca de descartada (un «Ahora no» cuya sesion no llego a apartarse porque la
   app murio): se reparan/cierran y se apartan. **Nunca se encolan** (hallazgo 1).
2. ``recover_sessions``: carpetas que seguian en ``recording`` (cierre brusco).
3. Barrido de carpetas ``stopped`` / ``recovered`` / ``finalized`` sin trabajo en SQLite y sin
   ``transcripcion.json``: la app se cerro entre parar el grabador y encolar.

De 2 y 3, las confirmadas («Si») se encolan; las que nadie confirmo se apartan sin transcribir
(recuperables desde la biblioteca; nada se transcribe ni se borra sin decision del usuario).
"""

from __future__ import annotations

import logging
from pathlib import Path

from micnaga import holding, paths
from micnaga.capture import finalize_session, find_sessions, recover_sessions
from micnaga.capture.session import (
    STATE_FINALIZED,
    STATE_RECORDING,
    STATE_RECOVERED,
    STATE_STOPPED,
    read_capture_json,
)
from micnaga.config import Config
from micnaga.integration.jobs import apply_trim_marker, drop_trim_marker
from micnaga.store.db import Database

log = logging.getLogger(__name__)


def _confirmed(d: Path) -> bool:
    try:
        return bool(read_capture_json(d).confirmed)
    except (OSError, ValueError, KeyError):
        return False


def hold_marked_sessions(config: Config, root: Path) -> list[Path]:
    """Aparta las carpetas marcadas como descartadas (paso 1). Devuelve las rutas nuevas."""
    held: list[Path] = []
    if not root.is_dir():
        return held
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name == holding.HELD_DIRNAME:
            continue
        reason = holding.discard_reason(d)
        if reason is None:
            continue
        try:
            apply_trim_marker(d)  # corte persistido antes de parar (P2-a)
            finalize_session(d, flac_level=config.capture.flac_level)
            drop_trim_marker(d)
        except RuntimeError:
            continue  # otra instancia la sigue grabando (no deberia pasar con el mutex)
        except Exception:
            log.exception("could not finalize a discarded session; holding it as is")
        try:
            held.append(holding.move_to_held(d, reason))
        except OSError:
            log.exception("could not hold a discarded session (marker kept; retried next boot)")
    return held


def recover_and_enqueue(config: Config, db: Database) -> list[Path]:
    root = paths.meetings_root(config)
    try:
        holding.purge_held(root, config.storage.discarded_keep_days)
    except Exception:
        log.exception("purging held sessions failed")
    held = hold_marked_sessions(config, root)
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
            apply_trim_marker(d)  # corte persistido antes de parar (P2-a)
            finalize_session(d, flac_level=config.capture.flac_level)
            drop_trim_marker(d)
        except Exception:
            log.exception("could not finalize a recovered session")
        if not _confirmed(d):
            try:
                held.append(holding.move_to_held(d, holding.REASON_INTERRUPTED))
            except OSError:
                log.exception("could not hold an unconfirmed orphan session")
            continue
        db.enqueue_job(d)
        queued.append(d)
    if queued:
        log.info("%d orphan session(s) queued at startup", len(queued))
    if held:
        log.info("%d orphan session(s) held at startup", len(held))
    return queued


__all__ = ["STATE_RECORDING", "hold_marked_sessions", "recover_and_enqueue"]
