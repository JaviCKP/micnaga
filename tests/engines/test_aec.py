"""AEC: troceado en tramas, longitudes, latencia compensada y reduccion de eco sintetica."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from micnaga.pipeline.aec import _common
from micnaga.pipeline.aec.speex import SpeexEchoCanceller
from micnaga.pipeline.aec.webrtc import WebRtcEchoCanceller
from micnaga.types import AudioArray
from tests.synth import SR, add_echo, noise, speech_like

CANCELLERS = [
    pytest.param(lambda: WebRtcEchoCanceller(), id="webrtc"),
    pytest.param(lambda: SpeexEchoCanceller(), id="speex"),
    pytest.param(
        lambda: SpeexEchoCanceller(enable_preprocess=False, auto_align=False), id="speex-plain"
    ),
]


# ------------------------------------------------------------------------- utilidades


def test_float_int16_roundtrip_and_clipping() -> None:
    x = np.array([0.0, 0.5, -0.5, 1.5, -2.0], dtype=np.float32)
    i = _common.float_to_int16(x)
    assert i.dtype == np.int16
    assert list(i) == [0, 16384, -16384, 32767, -32768]
    back = _common.int16_to_float(i)
    assert back.dtype == np.float32
    assert back[1] == pytest.approx(0.5)


def test_pad_to_multiple_and_match_length() -> None:
    x = np.arange(250, dtype=np.int16)
    p = _common.pad_to_multiple(x, 160)
    assert len(p) == 320 and p[249] == 249 and p[250] == 0
    assert _common.pad_to_multiple(x[:160], 160).shape == (160,)
    r = np.ones(10, dtype=np.float32)
    assert len(_common.match_length(r, 4)) == 4
    longer = _common.match_length(r, 25)
    assert len(longer) == 25 and longer[9] == 1 and longer[10] == 0


def test_delay_ref_delays_and_keeps_length() -> None:
    r = np.arange(1, 11, dtype=np.float32)
    d = _common.delay_ref(r, 3)
    assert list(d[:5]) == [0, 0, 0, 1, 2] and len(d) == 10
    assert _common.delay_ref(r, 0) is r
    assert not _common.delay_ref(r, 50).any()


@pytest.mark.parametrize("delay_ms", [20, 87, 150, 310])
def test_estimate_bulk_delay_finds_known_delay(delay_ms: int) -> None:
    ref = noise(12.0, amp=0.2, seed=3)
    mic = 0.4 * np.roll(ref, int(delay_ms * 16)) + noise(12.0, amp=0.02, seed=4)
    est = _common.estimate_bulk_delay(mic.astype(np.float32), ref, SR)
    assert abs(est - delay_ms * 16) <= 2


def test_estimate_bulk_delay_silent_ref_is_zero() -> None:
    z = np.zeros(SR * 3, dtype=np.float32)
    assert _common.estimate_bulk_delay(noise(3.0), z, SR) == 0


# ------------------------------------------------------------------------ comportamiento


@pytest.mark.parametrize("make", CANCELLERS)
@pytest.mark.parametrize("n", [1, 77, 159, 160, 161, 16000 + 77, 48000])
def test_output_length_equals_mic_length(make: Any, n: int) -> None:
    ec = make()
    mic = noise(n / SR, amp=0.1, seed=1)
    ref = noise(n / SR, amp=0.1, seed=2)
    out = ec.process(mic, ref, SR)
    assert out.dtype == np.float32
    assert len(out) == len(mic)


@pytest.mark.parametrize("make", CANCELLERS)
def test_ref_length_mismatch_and_empty(make: Any) -> None:
    ec = make()
    mic = noise(2.0, amp=0.1)
    assert len(ec.process(mic, noise(1.0), SR)) == len(mic)  # ref corta
    assert len(ec.process(mic, noise(3.0), SR)) == len(mic)  # ref larga
    assert len(ec.process(np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32), SR)) == 0


@pytest.mark.parametrize("sr", [8000, 16000, 44100])
def test_other_sample_rates_keep_length(sr: int) -> None:
    n = int(2.5 * sr) + 13
    rng = np.random.default_rng(0)
    mic = (0.1 * rng.standard_normal(n)).astype(np.float32)
    ref = (0.1 * rng.standard_normal(n)).astype(np.float32)
    for ec in (WebRtcEchoCanceller(), SpeexEchoCanceller()):
        assert len(ec.process(mic, ref, sr)) == n


def _best_lag(out: AudioArray, ref_sig: AudioArray, skip: int = SR) -> tuple[int, float]:
    a, b = out[skip:], ref_sig[skip:]
    corr = {k: float(np.dot(np.roll(a, -k), b)) for k in range(-6, 7)}
    best = max(corr, key=lambda k: corr[k])
    return best, corr[best] / float(np.linalg.norm(a) * np.linalg.norm(b))


@pytest.mark.parametrize("make", CANCELLERS)
def test_silent_ref_keeps_a_voice_like_mic(make: Any) -> None:
    """Sin referencia no hay eco: la voz sale casi igual (los filtros <300 Hz dan +-3 muestras)."""
    from scipy.signal import butter, sosfilt

    sos = butter(4, 400, btype="highpass", fs=SR, output="sos")
    mic = sosfilt(sos, broadband(8.0, seed=11) + speech_like(8.0, f0=1100, seed=3)).astype(
        np.float32
    )
    out = make().process(mic, np.zeros_like(mic), SR)
    lag, ncc = _best_lag(out, mic)
    assert abs(lag) <= 3
    assert float(np.std(out)) > 0.2 * float(np.std(mic))  # el preprocesador no se lo come todo
    assert ncc > 0.5


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: WebRtcEchoCanceller(), id="webrtc"),
        pytest.param(lambda: SpeexEchoCanceller(enable_preprocess=False), id="speex-plain"),
    ],
)
def test_latency_is_compensated_exactly(make: Any) -> None:
    """``out[i]`` corresponde a ``mic[i]``: la latencia interna se descuenta (ruido blanco)."""
    mic = np.random.default_rng(3).standard_normal(SR * 10).astype(np.float32) * 0.1
    lag, ncc = _best_lag(make().process(mic, np.zeros_like(mic), SR), mic)
    assert lag == 0
    assert ncc > 0.85


def broadband(duration: float, seed: int) -> AudioArray:
    """Ruido con envolvente silabica: banda ancha (a diferencia de ``speech_like``, periodico)."""
    rng = np.random.default_rng(seed)
    n = int(duration * SR)
    env = 0.5 * (1 + np.sin(2 * np.pi * 3.7 * np.arange(n) / SR + rng.uniform(0, 6))) ** 0.8
    return (0.25 * env * rng.standard_normal(n)).astype(np.float32)


def _echo_scenario() -> tuple[AudioArray, AudioArray, AudioArray]:
    far = speech_like(14.0, f0=135, amp=0.3, syllable_hz=3.5, seed=5)
    far = far + 0.5 * speech_like(14.0, f0=210, amp=0.2, syllable_hz=5.5, seed=6)
    echo_only = add_echo(np.zeros_like(far), far, delay_s=0.04, gain=0.5)
    return far.astype(np.float32), echo_only, echo_only + noise(14.0, amp=0.001, seed=8)


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: WebRtcEchoCanceller(), id="webrtc"),
        pytest.param(lambda: SpeexEchoCanceller(), id="speex"),
    ],
)
def test_echo_is_reduced_on_far_end_only(make: Any) -> None:
    far, _echo, mic = _echo_scenario()
    out = make().process(mic, far, SR)
    tail = slice(8 * SR, 14 * SR)
    erle = 10 * np.log10(np.mean(mic[tail] ** 2) / np.mean(out[tail] ** 2))
    assert erle > 10.0, f"ERLE {erle:.1f} dB"


def test_auto_align_handles_delay_beyond_the_filter() -> None:
    """Retardo de 200 ms con filtro de 128 ms: solo funciona con la pre-alineacion."""
    far = broadband(14.0, seed=5)
    mic = add_echo(noise(14.0, amp=0.001, seed=9), far, delay_s=0.2, gain=0.5)
    tail = slice(8 * SR, 14 * SR)

    def erle(ec: SpeexEchoCanceller) -> float:
        out = ec.process(mic, far, SR)
        return float(10 * np.log10(np.mean(mic[tail] ** 2) / np.mean(out[tail] ** 2)))

    plain = SpeexEchoCanceller(filter_ms=128, auto_align=False, enable_preprocess=False)
    aligned = SpeexEchoCanceller(filter_ms=128, auto_align=True, enable_preprocess=False)
    assert erle(plain) < 6.0
    assert erle(aligned) > 15.0


def test_auto_align_ignores_unreliable_delay_estimates() -> None:
    """Sin eco real (micro y referencia independientes) no se retrasa la referencia."""
    far = broadband(10.0, seed=1)
    mic = broadband(10.0, seed=2)
    assert _common.estimate_bulk_delay(mic, far, SR) == 0
    # y con un eco claro si se detecta
    echo = mic + 0.5 * np.roll(far, 1600)
    assert abs(_common.estimate_bulk_delay(echo.astype(np.float32), far, SR) - 1600) <= 2


@pytest.mark.parametrize("make", CANCELLERS)
def test_each_call_is_an_independent_session(make: Any) -> None:
    far, _e, mic = _echo_scenario()
    ec = make()
    a = ec.process(mic, far, SR)
    b = ec.process(mic, far, SR)
    assert np.array_equal(a, b)
