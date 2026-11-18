"""Pruebas con dispositivos reales. Se saltan salvo ``MICNAGA_RUN_REALDEVICE=1`` y hay que
ejecutarlas en una sesion interactiva de Windows con micro y altavoces."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from micnaga import paths
from micnaga.capture import devices
from micnaga.capture.recorder import Recorder
from micnaga.capture.sync import estimate_drift
from micnaga.config import CaptureConfig
from tests.capture.fakes import meta_of, read_track

pytestmark = pytest.mark.realdevice


def test_default_endpoints_and_output_kind() -> None:
    snap = devices.snapshot()
    assert snap.render is not None, "no default output device"
    print("render:", snap.render, "capture:", snap.capture)
    assert snap.render.output_kind in ("headphones", "speakers", "unknown")


def test_record_a_few_seconds_from_the_real_devices(tmp_path: Path) -> None:
    rec = Recorder(CaptureConfig(sync_log_interval_s=1.0))
    session = rec.start(tmp_path / "real")
    time.sleep(6.0)
    levels = rec.levels()
    rec.stop()
    meta = meta_of(session)
    mic = read_track(session / paths.AUDIO_ME_WAV)
    sysm = read_track(session / paths.AUDIO_REMOTE_WAV)
    print(
        "levels",
        levels,
        "output",
        meta.output_kind,
        "devices",
        meta.mic.device,
        "|",
        meta.system.device,
    )
    assert abs(len(mic) - len(sysm)) < 0.5 * 16000
    assert len(mic) > 5 * 16000
    assert meta.output_kind in ("headphones", "speakers", "unknown")
    for track in (meta.mic, meta.system):
        assert len(track.sync) >= 3
        print(track.device, estimate_drift(track.sync, 16000))
    assert np.isfinite(mic).all() and np.isfinite(sysm).all()
