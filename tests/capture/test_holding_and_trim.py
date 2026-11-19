"""«Descartadas» (apartar, listar, recuperar, purgar) y recorte al soltar el micro."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import soundfile as sf

from micnaga import holding, paths
from micnaga.capture import wavio
from micnaga.capture.recovery import TRIM_KEY, finalize_session, find_sessions, set_trim
from micnaga.capture.session import read_capture_json
from tests.capture.test_crash_recovery import DEAD_PID, SR, make_session


def test_marker_is_written_read_and_removed(tmp_path: Path) -> None:
    d = make_session(tmp_path, state="stopped", pid=DEAD_PID)
    assert holding.discard_reason(d) is None
    assert holding.mark_discarded(d)
    assert holding.discard_reason(d) == holding.REASON_DECLINED
    # una carpeta marcada no la recoge la recuperacion de arranque como sesion normal
    assert d not in find_sessions(tmp_path, ("stopped",))
    holding.unmark_discarded(d)
    assert holding.discard_reason(d) is None


def test_move_list_restore_cycle(tmp_path: Path) -> None:
    d = make_session(tmp_path, "2026-11-19_1000_reunion", state="finalized", pid=DEAD_PID)
    holding.mark_discarded(d)
    dest = holding.move_to_held(d, holding.REASON_DECLINED)
    assert not d.exists() and dest.parent == holding.held_root(tmp_path)
    assert holding.discard_reason(dest) is None, "la marca se quita al apartar"
    (held,) = holding.list_held(tmp_path, keep_days=7)
    assert held.name == dest.name and held.reason == holding.REASON_DECLINED and held.has_audio
    when = datetime.fromisoformat(held.held_at)
    assert datetime.fromisoformat(held.expires_at) - when == timedelta(days=7)
    # idempotente: apartar lo ya apartado solo actualiza el motivo
    assert holding.move_to_held(dest, holding.REASON_UNANSWERED) == dest
    assert holding.list_held(tmp_path)[0].reason == holding.REASON_UNANSWERED

    back = holding.restore_held(tmp_path, dest.name)
    assert back == d and back.is_dir()
    assert not (back / holding.HELD_FILE).exists()
    assert read_capture_json(back).confirmed is True, "recuperar = el usuario la quiere"
    assert holding.list_held(tmp_path) == []


def test_held_dir_rejects_paths(tmp_path: Path) -> None:
    holding.held_root(tmp_path).mkdir()
    for bad in ("", ".", "..", "../x", "a/b", r"a\b"):
        assert holding.held_dir(tmp_path, bad) is None
    assert holding.delete_held(tmp_path, "..") is False


def test_purge_removes_only_expired(tmp_path: Path) -> None:
    old = holding.move_to_held(make_session(tmp_path, "old", state="finalized"), "declined")
    new = holding.move_to_held(make_session(tmp_path, "new", state="finalized"), "declined")
    info = json.loads((old / holding.HELD_FILE).read_text(encoding="utf-8"))
    info["held_at"] = (datetime.now().astimezone() - timedelta(days=8)).isoformat()
    (old / holding.HELD_FILE).write_text(json.dumps(info), encoding="utf-8")
    assert holding.purge_held(tmp_path, keep_days=7) == 1
    assert not old.exists() and new.exists()
    assert holding.purge_held(tmp_path / "missing", keep_days=7) == 0


def test_purge_falls_back_to_mtime_without_held_json(tmp_path: Path) -> None:
    d = holding.move_to_held(make_session(tmp_path, "x", state="finalized"), "declined")
    (d / holding.HELD_FILE).unlink()
    past = (datetime.now() - timedelta(days=30)).timestamp()
    os.utime(d, (past, past))
    assert holding.purge_held(tmp_path, keep_days=7) == 1


# ---------------------------------------------------------------------------- recorte


def test_finalize_trims_both_tracks_to_the_mic_release_point(tmp_path: Path) -> None:
    d = make_session(tmp_path, state="stopped", pid=DEAD_PID)  # 5 s por pista
    head, _ = sf.read(str(d / paths.AUDIO_ME_WAV), dtype="int16")
    assert set_trim(d, 3.0)
    assert read_capture_json(d).extra[TRIM_KEY] == 3 * SR
    meta = finalize_session(d)
    assert meta.mic.samples == meta.system.samples == 3 * SR
    assert meta.extra["trim_applied"] is True and meta.extra["duration_s"] == 3.0
    assert all(m.samples <= 3 * SR for m in meta.mic.sync)
    assert wavio.wav_samples(d / paths.AUDIO_ME_FLAC) == 3 * SR
    assert wavio.wav_samples(d / paths.AUDIO_REMOTE_FLAC) == 3 * SR
    flac, _ = sf.read(str(d / paths.AUDIO_ME_FLAC), dtype="int16")
    assert (flac == head[: 3 * SR]).all(), "se corta la cola, no el principio"
    again = finalize_session(d)  # idempotente
    assert again.mic.samples == 3 * SR


def test_trim_beyond_the_end_keeps_everything(tmp_path: Path) -> None:
    d = make_session(tmp_path, state="stopped", pid=DEAD_PID)
    set_trim(d, 60.0)
    meta = finalize_session(d)
    assert meta.mic.samples == 5 * SR
