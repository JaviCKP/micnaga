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

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from micnaga import holding, paths
from micnaga.capture import finalize_session, wavio
from micnaga.capture.recovery import _owner_alive, recover_session
from micnaga.capture.session import (
    STATE_FINALIZED,
    STATE_RECORDING,
    STATE_RECOVERED,
    STATE_STOPPED,
    forget_recording,
    pending_sessions_dir,
    read_capture_json,
    write_capture_json,
)
from micnaga.config import Config
from micnaga.integration.jobs import apply_trim_marker, drop_trim_marker
from micnaga.store.db import Database
from micnaga.types import CaptureMeta, TrackInfo

log = logging.getLogger(__name__)


def _rebuild_metadata(folder: Path) -> CaptureMeta | None:
    """Recognize the recorder's PCM files even if capture.json never committed."""
    tracks: dict[str, TrackInfo] = {}
    started: list[float] = []
    for name in (paths.AUDIO_ME_WAV, paths.AUDIO_REMOTE_WAV):
        file = folder / name
        if file.is_symlink():
            return None
        if not file.is_file():
            continue
        with file.open("rb") as fh:
            header = fh.read(wavio.HEADER_BYTES)
        rate = int.from_bytes(header[24:28], "little")
        if rate <= 0:
            return None
        expected = wavio.build_header(0, rate)
        if header[:4] != expected[:4] or header[8:40] != expected[8:40]:
            return None
        stat = file.stat()
        frames = max(0, stat.st_size - wavio.HEADER_BYTES) // wavio.BYTES_PER_SAMPLE
        tracks[name] = TrackInfo(name, "", rate, frames)
        started.append(stat.st_ctime)
    if not any(t.samples for t in tracks.values()):
        return None
    for name in (paths.AUDIO_ME_WAV, paths.AUDIO_REMOTE_WAV):
        if name not in tracks:
            rate = next(iter(tracks.values())).sample_rate
            wavio.ChunkedWavWriter(folder / name, rate).close()
            tracks[name] = TrackInfo(name, "", rate)
    duration = max(t.samples / t.sample_rate for t in tracks.values())
    meta = CaptureMeta(
        datetime.fromtimestamp(min(started)).astimezone().isoformat(timespec="seconds"),
        tracks[paths.AUDIO_ME_WAV],
        tracks[paths.AUDIO_REMOTE_WAV],
        confirmed=False,
        extra={"state": STATE_RECOVERED, "duration_s": duration, "metadata_rebuilt": True},
    )
    if not write_capture_json(folder, meta):
        raise OSError(f"could not save reconstructed metadata for {folder}")
    return meta


def _pending() -> tuple[set[Path], set[Path]]:
    active: set[Path] = set()
    interrupted: set[Path] = set()
    for record in pending_sessions_dir().glob("*.json"):
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
            session = Path(data["session"])
            if session.is_absolute():
                (active if _owner_alive(data) else interrupted).add(session)
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("could not read capture journal entry %s", record.name)
    return active, interrupted


def recover_and_enqueue(
    config: Config, db: Database, *, on_recovered_db: Callable[[Path], None] | None = None
) -> list[Path]:
    root = paths.meetings_root(config).resolve()
    try:
        holding.purge_held(root, config.storage.discarded_keep_days)
    except Exception:
        log.exception("purging held sessions failed")
    held: list[Path] = []
    active, interrupted = _pending()
    try:
        folders = set(root.iterdir()) if root.is_dir() else set()
    except OSError:
        log.exception("could not list the current meeting folder")
        folders = set()
    queued: list[Path] = []
    for d in sorted((folders | interrupted) - active):
        if not d.is_dir() or d.is_symlink() or d.name == holding.HELD_DIRNAME:
            continue
        discarded = holding.discard_reason(d)
        previous_index: Path | None = None
        if d in interrupted and d.parent != root and discarded is None:
            # A crash after enqueuing may have left a journal entry but the old DB owns this job.
            previous_index = d.parent / paths.db_path(config).name
            if previous_index.is_file():
                try:
                    with Database(previous_index) as previous:
                        known = previous.find_job(d) is not None
                except Exception:
                    log.exception("could not check the previous index for %s", d)
                    continue
                if known:
                    if on_recovered_db is not None:
                        on_recovered_db(previous_index)
                    forget_recording(d)
                    continue
        if discarded is None and ((d / paths.TRANSCRIPT_JSON).is_file()
                                  or db.find_job(d) is not None):
            forget_recording(d)
            continue
        try:
            meta: CaptureMeta | None
            try:
                meta = read_capture_json(d)
            except (OSError, ValueError, KeyError, TypeError):
                meta = _rebuild_metadata(d)
            if meta is None:
                if discarded is not None:
                    held.append(holding.move_to_held(d, discarded, meetings_root=root))
                    forget_recording(d)
                continue
            state = meta.extra.get("state")
            if state == STATE_RECORDING:
                meta = recover_session(d)
                if meta is None:
                    continue
            elif state not in (STATE_STOPPED, STATE_RECOVERED, STATE_FINALIZED) and not discarded:
                continue
            try:
                apply_trim_marker(d)  # corte persistido antes de parar (P2-a)
                finalize_session(d, flac_level=config.capture.flac_level)
                drop_trim_marker(d)
            except Exception:
                log.exception("could not finalize a recovered session")
            if discarded is not None or not meta.confirmed:
                held.append(
                    holding.move_to_held(
                        d,
                        discarded or holding.REASON_INTERRUPTED,
                        meetings_root=root,
                        retain=discarded is None and bool(meta.extra.get("metadata_rebuilt")),
                    )
                )
            elif previous_index is not None:
                with Database(previous_index) as previous:
                    previous.enqueue_job(d)
                if on_recovered_db is not None:
                    on_recovered_db(previous_index)
                queued.append(d)
            else:
                db.enqueue_job(d)
                queued.append(d)
            forget_recording(d)
        except Exception:
            log.exception("could not recover session %s", d)
    if queued:
        log.info("%d orphan session(s) queued at startup", len(queued))
    if held:
        log.info("%d orphan session(s) held at startup", len(held))
    return queued


__all__ = ["STATE_RECORDING", "recover_and_enqueue"]
