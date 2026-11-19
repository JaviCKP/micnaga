"""Comprobacion de eco (``aec.echo_check``) y su uso en la etapa 3: con auriculares se salta
Speex; si el sistema se oye en el micro, se cancela."""

from __future__ import annotations

import numpy as np
import pytest
from scipy import signal

from micnaga.pipeline.aec.echo_check import measure_echo

SR = 16000
T = 60.0


def _talk(spans: list[tuple[float, float]], seed: int) -> np.ndarray:
    """«Voz» sin armonicos comunes: ruido filtrado a banda de voz con envolvente silabica."""
    rng = np.random.default_rng(seed)
    n = int(T * SR)
    noise = rng.standard_normal(n)
    sos = signal.butter(4, (150, 3800), btype="bandpass", fs=SR, output="sos")
    voiced = signal.sosfilt(sos, noise)
    t = np.arange(n) / SR
    env = 0.5 * (1 + np.sin(2 * np.pi * rng.uniform(3, 5) * t + rng.uniform(0, 6))) ** 2
    gate = np.zeros(n)
    for a, b in spans:
        gate[int(a * SR) : int(b * SR)] = 1.0
    x = voiced * env * gate
    return (0.2 * x / (np.max(np.abs(x)) or 1.0)).astype(np.float32)


def _room(x: np.ndarray, delay_s: float, gain: float, seed: int = 0) -> np.ndarray:
    """Lo que el micro capta del altavoz: retraso + reverberacion exponencial de 300 ms."""
    rng = np.random.default_rng(seed)
    n = int(0.3 * SR)
    h = rng.standard_normal(n) * np.exp(-np.arange(n) / (0.06 * SR))
    h[0] = 4.0
    h /= np.sqrt(np.sum(h**2))
    y = signal.fftconvolve(x, h)[: len(x)]
    k = int(delay_s * SR)
    out = np.zeros_like(x)
    out[k:] = gain * y[: len(x) - k]
    return out.astype(np.float32)


REMOTE = [(1, 9), (13, 21), (25, 33), (40, 51)]
ME = [(9, 13), (21, 25), (33, 40), (51, 59)]


@pytest.fixture
def tracks() -> tuple[np.ndarray, np.ndarray]:
    system = _talk(REMOTE, seed=3)
    rng = np.random.default_rng(9)
    mic = _talk(ME, seed=7) + (0.002 * rng.standard_normal(int(T * SR))).astype(np.float32)
    return mic, system


def test_headphones_no_leak_is_negligible(tracks: tuple[np.ndarray, np.ndarray]) -> None:
    mic, system = tracks
    check = measure_echo(mic, system, SR)
    assert not check.echo
    assert check.reason == "negligible echo"


def test_double_talk_without_leak_is_negligible() -> None:
    system = _talk([(0, 60)], seed=3)
    mic = _talk([(0, 60)], seed=11)
    assert not measure_echo(mic, system, SR).echo


@pytest.mark.parametrize("gain", [0.5, 0.15, 0.05])
def test_speaker_leak_is_detected(tracks: tuple[np.ndarray, np.ndarray], gain: float) -> None:
    mic, system = tracks
    leaked = mic + _room(system, 0.12, gain)
    check = measure_echo(leaked, system, SR)
    assert check.echo, check.to_dict()
    assert 100 <= check.delay_ms <= 200


def test_leak_is_detected_when_people_talk_over_each_other() -> None:
    system = _talk([(0, 40)], seed=3)
    mic = _talk([(10, 30), (45, 60)], seed=11) + _room(system, 0.2, 0.15)
    assert measure_echo(mic, system, SR).echo


def test_very_weak_leak_is_negligible(tracks: tuple[np.ndarray, np.ndarray]) -> None:
    mic, system = tracks
    check = measure_echo(mic + _room(system, 0.12, 0.003), system, SR)
    assert not check.echo
    assert check.echo_db < -30


def test_silent_system_track_needs_no_cancelling(tracks: tuple[np.ndarray, np.ndarray]) -> None:
    mic, _ = tracks
    check = measure_echo(mic, np.zeros_like(mic), SR)
    assert not check.echo and check.reason == "no system audio"
