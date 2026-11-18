"""Deriva y alineacion (``micnaga.capture.sync``): matematica pura con arrays moderados."""

from __future__ import annotations

import numpy as np
import pytest

from micnaga.capture.sync import (
    align_tracks,
    clock_knots,
    estimate_drift,
    resample_to_clock,
    track_duration_s,
)
from micnaga.types import CaptureMeta, SyncMark, TrackInfo
from tests.synth import SR, noise, sine


def _marks(
    duration_s: float,
    *,
    rate: float,
    sr: int,
    every: float = 10.0,
    jitter_s: float = 0.0,
    seed: int = 0,
) -> list[SyncMark]:
    """Marcas de un dispositivo cuyo reloj tarda ``rate`` s de reloj comun por s de pista."""
    rng = np.random.default_rng(seed)
    out: list[SyncMark] = []
    t = every
    while t <= duration_s:
        samples = round(t / rate * sr)  # t = reloj comun; el reloj de pista va a 1/rate
        out.append(SyncMark(samples, t + float(rng.uniform(0.0, jitter_s))))  # jitter solo retrasa
        t += every
    return out


def _meta(mic: list[SyncMark], system: list[SyncMark], sr: int = SR) -> CaptureMeta:
    return CaptureMeta(
        started_at="2026-11-18T10:00:00+01:00",
        mic=TrackInfo(file="m.wav", device="m", sample_rate=sr, sync=mic),
        system=TrackInfo(file="s.wav", device="s", sample_rate=sr, sync=system),
    )


def _impulse_positions(x: np.ndarray, expected: list[int], half: int) -> list[int]:
    return [
        int(np.argmax(np.abs(x[max(e - half, 0) : e + half]))) + max(e - half, 0) for e in expected
    ]


def test_estimate_drift_recovers_ppm_and_total() -> None:
    marks = _marks(3600, rate=1.0 + 417e-6, sr=SR)  # +1.5 s en 1 h
    est = estimate_drift(marks, SR)
    assert est.ppm == pytest.approx(417, abs=5)
    assert est.drift_s == pytest.approx(1.5, abs=0.05)
    assert est.residual_s < 0.005


def test_estimate_drift_is_robust_to_jitter_and_a_step() -> None:
    marks = _marks(600, rate=1.0 + 200e-6, sr=SR, jitter_s=0.01)
    # salto de 0.3 s (hueco sin rellenar) a mitad de sesion
    marks = [m if i < 30 else SyncMark(m.samples, m.clock_s + 0.3) for i, m in enumerate(marks)]
    est = estimate_drift(marks, SR)
    assert est.ppm == pytest.approx(200, abs=60)
    assert est.residual_s > 0.1  # el salto se nota en el residuo


def test_estimate_drift_needs_two_marks() -> None:
    assert estimate_drift([], SR).rate == 1.0
    assert estimate_drift([SyncMark(100, 0.1)], SR).ppm == 0.0


def test_clock_knots_anchor_monotone_and_extrapolate() -> None:
    marks = [
        SyncMark(16000, 1.0),
        SyncMark(16000, 1.0),
        SyncMark(15000, 0.95),
        SyncMark(32000, 2.001),
    ]
    ks, kc = clock_knots(marks, SR, n_samples=48000)
    assert ks[0] == 0 and kc[0] == 0
    assert list(ks) == sorted(set(ks))
    assert ks[-1] == 48000
    assert kc[-1] == pytest.approx(3.0, abs=0.05)


def test_one_hour_drift_residual_below_50ms() -> None:
    """1 h con +1.5 s de deriva en una pista: tras ``align_tracks`` casan a < 50 ms.

    Se trabaja a 1 kHz (la matematica no depende de ``sr``): 3,6 M muestras por pista.
    """
    sr = 1000
    rate = 1.0 + 417e-6  # el sistema va lento: 1 h de reloj comun = 3600/rate s de pista
    secs = 3600
    event_clock = [100.0 + 300.0 * k for k in range(11)]  # instantes comunes de los "clics"
    mic = np.zeros(secs * sr, dtype=np.float32)
    system = np.zeros(round(secs / rate * sr), dtype=np.float32)
    for t in event_clock:
        mic[round(t * sr)] = 1.0
        system[round(t / rate * sr)] = 1.0
    meta = _meta(
        _marks(secs, rate=1.0, sr=sr, jitter_s=0.003, seed=1),
        _marks(secs, rate=rate, sr=sr, jitter_s=0.003, seed=2),
        sr,
    )
    # Sin corregir, la pista del sistema esta ~1.5 s adelantada al final.
    raw_off = abs(
        int(np.argmax(system[-200 * sr :])) / sr + len(system) / sr - 200 - event_clock[-1]
    )
    assert raw_off > 1.0
    mic_a, sys_a = align_tracks(mic, system, meta, sample_rate=sr)
    assert len(mic_a) == len(sys_a)
    expected = [round(t * sr) for t in event_clock]
    pos_m = _impulse_positions(mic_a, expected, 200)
    pos_s = _impulse_positions(sys_a, expected, 200)
    worst = max(abs(a - b) for a, b in zip(pos_m, pos_s, strict=True)) / sr
    assert worst < 0.050
    assert worst < 0.010  # en la practica, ms


def test_piecewise_map_absorbs_a_step() -> None:
    """Un hueco sin rellenar (0,3 s) entre marcas se corrige: la correspondencia es a trozos."""
    sr = 1000
    n = 60 * sr
    system = np.zeros(n, dtype=np.float32)
    system[20 * sr] = 1.0
    system[50 * sr] = 1.0  # tras el hueco: llega 0,3 s tarde respecto a la pista
    marks = [SyncMark(10 * sr, 10.0), SyncMark(30 * sr, 30.0), SyncMark(40 * sr, 40.3),
             SyncMark(59 * sr, 59.3)]  # fmt: skip
    out = resample_to_clock(system, marks, sr)
    first = int(np.argmax(np.abs(out[15 * sr : 25 * sr]))) + 15 * sr
    second = int(np.argmax(np.abs(out[45 * sr : 55 * sr]))) + 45 * sr
    assert abs(first - 20 * sr) <= 2
    assert abs(second - round(50.3 * sr)) <= 40  # interpolado dentro del tramo 30..40 s


def test_tone_fidelity_after_drift_correction() -> None:
    """El remuestreo conserva un tono (1 kHz y 6 kHz) con error relativo pequeno."""
    rate = 1.25  # 25 % lento (exagerado): las marcas caen en muestras enteras
    dur = 6.0
    for freq, tol in ((1000.0, 0.01), (6000.0, 0.06)):
        n_dev = round(dur / rate * SR)
        # el dispositivo muestrea la senal continua sin(2 pi f t_comun) con su reloj lento
        t_common = np.arange(n_dev) / SR * rate
        x = np.sin(2 * np.pi * freq * t_common).astype(np.float32)
        marks = _marks(dur, rate=rate, sr=SR, every=1.0)
        out = resample_to_clock(x, marks, SR)
        t = np.arange(len(out)) / SR
        ref = np.sin(2 * np.pi * freq * t)
        mid = slice(SR // 2, len(out) - SR // 2)
        err = np.sqrt(np.mean((out[mid] - ref[mid]) ** 2)) / np.sqrt(0.5)
        assert err < tol, (freq, err)


def test_no_marks_only_equalizes_lengths() -> None:
    mic = sine(440, 1.0)
    system = sine(220, 1.5)
    meta = _meta([], [])
    m, s = align_tracks(mic, system, meta)
    assert len(m) == len(s) == len(system)
    np.testing.assert_allclose(m[: len(mic)], mic, atol=1e-6)
    assert not np.any(m[len(mic) :])


def test_constant_offset_is_a_pure_shift() -> None:
    x = noise(2.0, seed=3)
    marks = [SyncMark(0, 0.0625), SyncMark(16000, 1.0625), SyncMark(32000, 2.0625)]
    out = resample_to_clock(x, marks, SR)
    k = 1000
    # la pista llega 1000 muestras mas tarde en el reloj: el audio se desplaza hacia delante
    np.testing.assert_allclose(out[k : k + 20000], x[:20000], atol=1e-6)


def test_track_duration_uses_marks() -> None:
    marks = [SyncMark(16000 * 10, 10.0), SyncMark(16000 * 20, 20.01)]
    assert track_duration_s(marks, 16000 * 20, SR) == pytest.approx(20.01, abs=1e-6)
