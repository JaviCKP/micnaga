from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from micnaga.logging_setup import setup_logging, shutdown_logging
from tests import synth


@pytest.fixture
def clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()


def test_logging_writes_under_home_and_is_idempotent(tmp_home: Path, clean_logging: None) -> None:
    f1 = setup_logging("unit")
    f2 = setup_logging("unit")
    assert f1 == f2 == tmp_home / "logs" / "unit.log"
    logging.getLogger("micnaga.test").warning("hola ñandú")
    for h in logging.getLogger("micnaga").handlers:
        h.flush()
    assert "hola ñandú" in f1.read_text(encoding="utf-8")
    assert len(logging.getLogger("micnaga").handlers) == 1


def test_importing_logging_setup_creates_nothing(tmp_home: Path) -> None:
    assert not tmp_home.exists()


# ----------------------------------------------------------------------------- marcadores


@pytest.mark.slow
def test_slow_marker_is_skipped_by_default() -> None:
    assert os.environ.get("MICNAGA_RUN_MODELS") == "1" or os.environ.get("MICNAGA_RUN_SLOW") == "1"


@pytest.mark.models
def test_models_marker_is_skipped_by_default() -> None:
    assert os.environ.get("MICNAGA_RUN_MODELS") == "1"


@pytest.mark.realdevice
def test_realdevice_marker_is_skipped_by_default() -> None:
    assert os.environ.get("MICNAGA_RUN_REALDEVICE") == "1"


# ----------------------------------------------------------------------------- synth


def test_synth_shapes_and_levels() -> None:
    s = synth.sine(440, 1.0)
    assert s.shape == (16000,)
    assert s.dtype == np.float32
    assert 0.45 < float(np.max(s)) <= 0.5
    n = synth.noise(1.0, amp=0.1)
    assert n.dtype == np.float32
    assert 0.05 < float(np.std(n)) < 0.15
    sp = synth.speech_like(1.0)
    assert float(np.max(np.abs(sp))) <= 0.3 + 1e-6


def test_speech_bursts_only_voiced_inside_spans() -> None:
    x = synth.speech_bursts([(1.0, 2.0), (3.0, 3.5)], total=4.0)
    assert len(x) == 4 * 16000
    assert float(np.max(np.abs(x[:16000]))) == 0.0
    assert float(np.max(np.abs(x[16000:32000]))) > 0.1
    assert float(np.max(np.abs(x[32000:48000]))) == 0.0
    assert float(np.max(np.abs(x[48000:56000]))) > 0.05
    assert float(np.max(np.abs(x[56000:]))) == 0.0


def test_echo_and_delay() -> None:
    ref = synth.speech_like(1.0, seed=1)
    mic = synth.silence(1.0)
    echoed = synth.add_echo(mic, ref, delay_s=0.05, gain=0.5)
    assert len(echoed) == len(mic)
    lag = round(0.05 * 16000)
    np.testing.assert_allclose(echoed[lag:], 0.5 * ref[:-lag], atol=1e-6)


def test_write_wav_roundtrip(tmp_path: Path) -> None:
    import soundfile as sf

    s = synth.sine(220, 0.5)
    p = synth.write_wav(tmp_path / "x.wav", s)
    data, sr = sf.read(str(p), dtype="float32")
    assert sr == 16000
    assert data.shape == s.shape
    np.testing.assert_allclose(data, s, atol=1e-3)
