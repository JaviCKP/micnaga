"""MIC-09: matar el proceso a mitad de grabacion pierde <= 2 s; recuperacion y FLAC."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import psutil
import pytest
import soundfile as sf

from micnaga import paths
from micnaga.capture import wavio
from micnaga.capture.dsp import float_to_int16
from micnaga.capture.recovery import (
    finalize_session,
    find_sessions,
    recover_session,
    recover_sessions,
)
from micnaga.capture.session import read_capture_json, write_capture_json
from micnaga.types import CaptureMeta, SyncMark, TrackInfo
from tests.synth import SR, noise

CHILD = Path(__file__).with_name("_crash_child.py")
REPO = Path(__file__).resolve().parents[2]
DEAD_PID = 2**22 + 12345


# --------------------------------------------------------------------------- crash real


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Mata el lanzador y todos sus descendientes (no debe quedar ningun proceso grabando)."""
    victims: list[psutil.Process] = []
    with contextlib.suppress(psutil.NoSuchProcess):
        victims = psutil.Process(proc.pid).children(recursive=True)
    with contextlib.suppress(psutil.NoSuchProcess):
        victims.append(psutil.Process(proc.pid))
    for victim in victims:
        with contextlib.suppress(psutil.NoSuchProcess):
            victim.kill()
    psutil.wait_procs(victims, timeout=10)
    if proc.stdout is not None:
        proc.stdout.close()


@pytest.mark.timeout(90)
@pytest.mark.parametrize("kill_after_s", [3.4, 4.9])
def test_killing_the_process_loses_at_most_two_seconds(tmp_path: Path, kill_after_s: float) -> None:
    root = tmp_path / "meetings"
    session = root / "crash-session"
    progress = tmp_path / "progress.txt"
    proc = subprocess.Popen(
        [sys.executable, str(CHILD), str(session), str(progress)],
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        deadline = time.monotonic() + 30
        line = ""
        while time.monotonic() < deadline and "READY" not in line:
            line = proc.stdout.readline()
            if proc.poll() is not None and "READY" not in line:
                break
        assert "READY" in line, "el hijo no arranco"
        time.sleep(kill_after_s)
        elapsed = -1.0
        for _ in range(100):
            try:
                elapsed = float(progress.read_text())
                break
            except (OSError, ValueError):  # el hijo lo esta reescribiendo
                time.sleep(0.005)
        assert elapsed > 0
        # En Windows el python.exe del venv es un lanzador que arranca el interprete real como
        # hijo: hay que matar al que graba (pid en capture.json), no solo al lanzador.
        real_pid = int(read_capture_json(session).extra["pid"])
        victims = [psutil.Process(real_pid)]
        if real_pid != proc.pid:
            victims.append(psutil.Process(proc.pid))
        for victim in victims:
            with contextlib.suppress(psutil.NoSuchProcess):
                victim.kill()  # TerminateProcess: sin limpieza ni flush
        proc.wait(10)
        psutil.wait_procs(victims, timeout=10)
    finally:
        _kill_tree(proc)

    assert read_capture_json(session).extra["state"] == "recording"
    recovered = recover_sessions(root)
    assert recovered == [session]
    meta = read_capture_json(session)
    assert meta.extra["state"] == "recovered"
    mic_n = wavio.wav_samples(session / paths.AUDIO_ME_WAV)
    sys_n = wavio.wav_samples(session / paths.AUDIO_REMOTE_WAV)
    assert mic_n == sys_n == meta.mic.samples == meta.system.samples  # pistas igualadas
    lost = elapsed - mic_n / SR
    print(f"killed at {elapsed:.2f} s; recovered {mic_n / SR:.2f} s; lost {lost:.2f} s")
    assert lost + 0.05 >= 0  # no se "recupera" mas de lo grabado
    assert lost <= 2.0, f"se perdieron {lost:.2f} s (> 2 s)"
    assert mic_n / SR > 1.0
    # el audio recuperado es audio, no relleno
    data, _ = sf.read(str(session / paths.AUDIO_ME_WAV), dtype="float32")
    assert float(np.max(np.abs(data[SR : SR + SR]))) > 0.1
    # idempotente: una segunda pasada no devuelve nada
    assert recover_sessions(root) == []
    # y se puede pasar a FLAC
    finalized = finalize_session(session)
    assert finalized.extra["state"] == "finalized"
    assert (session / paths.AUDIO_ME_FLAC).exists() and not (session / paths.AUDIO_ME_WAV).exists()


# --------------------------------------------------------------------- sesiones a mano


def make_session(
    root: Path,
    name: str = "s",
    *,
    mic_samples: int = 5 * SR,
    sys_samples: int = 5 * SR,
    state: str = "recording",
    pid: int = DEAD_PID,
) -> Path:
    d = root / name
    d.mkdir(parents=True)
    for fname, n, seed in (
        (paths.AUDIO_ME_WAV, mic_samples, 1),
        (paths.AUDIO_REMOTE_WAV, sys_samples, 2),
    ):
        w = wavio.ChunkedWavWriter(d / fname, SR, fsync=False)
        w.append(float_to_int16(noise(n / SR, seed=seed, amp=0.2)))
        w.close()
    marks = [SyncMark(0, 0.0), SyncMark(2 * SR, 2.0), SyncMark(4 * SR, 4.0)]
    meta = CaptureMeta(
        started_at="2026-11-18T10:00:00+01:00",
        mic=TrackInfo(paths.AUDIO_ME_WAV, "mic", SR, 3 * SR, list(marks)),
        system=TrackInfo(paths.AUDIO_REMOTE_WAV, "sys", SR, 3 * SR, list(marks)),
        extra={"state": state, "pid": pid, "pid_create_time": None},
    )
    write_capture_json(d, meta)
    return d


def test_recover_repairs_pads_and_marks_recovered(tmp_path: Path) -> None:
    root = tmp_path / "root"
    d = make_session(root, mic_samples=5 * SR, sys_samples=4 * SR + 5000)
    # cierre entre datos y cabecera: la cabecera dice menos, y un byte suelto
    with open(d / paths.AUDIO_ME_WAV, "ab") as f:
        f.write(b"\x07")
    assert find_sessions(root, ("recording",)) == [d]
    assert recover_sessions(root) == [d]
    meta = read_capture_json(d)
    assert meta.extra["state"] == "recovered"
    assert meta.extra["recovered"]["padded_samples"] == {"mic": 0, "system": SR - 5000}
    assert meta.mic.samples == meta.system.samples == 5 * SR
    assert wavio.wav_samples(d / paths.AUDIO_ME_WAV) == 5 * SR
    assert wavio.wav_samples(d / paths.AUDIO_REMOTE_WAV) == 5 * SR
    assert all(m.samples <= 5 * SR for m in meta.mic.sync)
    data, _ = sf.read(str(d / paths.AUDIO_REMOTE_WAV), dtype="int16")
    assert not data[4 * SR + 5000 :].any()  # relleno = silencio
    assert recover_sessions(root) == []


def test_recover_skips_live_owner_and_other_states(tmp_path: Path) -> None:
    root = tmp_path / "root"
    make_session(root, "live", pid=os.getpid())
    make_session(root, "stopped", state="stopped")
    make_session(root, "done", state="finalized")
    (root / "junk").mkdir()
    (root / "junk" / paths.CAPTURE_JSON).write_text("{not json", encoding="utf-8")
    (root / "plain_file.txt").parent.mkdir(exist_ok=True)
    (root / "plain_file.txt").write_text("x")
    assert recover_sessions(root) == []
    assert recover_session(root / "live") is None
    assert recover_sessions(tmp_path / "does-not-exist") == []


def test_pid_reuse_is_detected_by_create_time(tmp_path: Path) -> None:
    root = tmp_path / "root"
    d = make_session(root, pid=os.getpid())
    meta = read_capture_json(d)
    # mismo pid pero otra hora de creacion != nosotros -> no es nuestro proceso... pero
    # ``pid == getpid()`` se considera vivo siempre (somos nosotros)
    assert recover_session(d) is None
    meta = replace(meta, extra={**meta.extra, "pid": DEAD_PID})
    write_capture_json(d, meta)
    assert recover_session(d) is not None


# ------------------------------------------------------------------------------ FLAC


def test_finalize_converts_verifies_and_is_idempotent(tmp_path: Path) -> None:
    d = make_session(tmp_path / "root", state="stopped", pid=DEAD_PID)
    mic_pcm, _ = sf.read(str(d / paths.AUDIO_ME_WAV), dtype="int16")
    meta = finalize_session(d, flac_level=5)
    assert meta.extra["state"] == "finalized" and meta.extra["flac_level"] == 5
    assert meta.mic.file == paths.AUDIO_ME_FLAC and meta.system.file == paths.AUDIO_REMOTE_FLAC
    assert meta.mic.samples == 5 * SR
    assert not (d / paths.AUDIO_ME_WAV).exists() and not (d / paths.AUDIO_REMOTE_WAV).exists()
    flac_pcm, rate = sf.read(str(d / paths.AUDIO_ME_FLAC), dtype="int16")
    assert rate == SR
    np.testing.assert_array_equal(flac_pcm, mic_pcm)
    assert read_capture_json(d).mic.file == paths.AUDIO_ME_FLAC
    again = finalize_session(d)  # idempotente
    assert again.extra["state"] == "finalized" and again.mic.samples == 5 * SR


def test_finalize_after_interrupted_run_reverifies_and_cleans_up(tmp_path: Path) -> None:
    """Murio entre crear el FLAC y borrar el WAV: se reconvierte y se limpia."""
    d = make_session(tmp_path / "root", state="stopped")
    (d / paths.AUDIO_ME_FLAC).write_bytes(b"half written")
    finalize_session(d)
    assert wavio.wav_samples(d / paths.AUDIO_ME_FLAC) == 5 * SR
    assert not (d / paths.AUDIO_ME_WAV).exists()


def test_finalize_recovers_an_orphan_and_refuses_a_live_session(tmp_path: Path) -> None:
    root = tmp_path / "root"
    orphan = make_session(root, "orphan", pid=DEAD_PID)
    meta = finalize_session(orphan)
    assert meta.extra["state"] == "finalized" and "recovered" in meta.extra
    live = make_session(root, "live", pid=os.getpid())
    with pytest.raises(RuntimeError):
        finalize_session(live)
    assert (live / paths.AUDIO_ME_WAV).exists()  # no se toco nada


def test_finalize_without_audio_raises(tmp_path: Path) -> None:
    d = make_session(tmp_path / "root", state="stopped")
    (d / paths.AUDIO_ME_WAV).unlink()
    with pytest.raises(FileNotFoundError):
        finalize_session(d)
