"""VAD: post-proceso con probabilidades sinteticas y modelo falso (sin ONNX)."""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from micnaga.config import Config
from micnaga.pipeline import vad
from micnaga.pipeline.vad import FRAME_S, detect_speech, group_regions, regions_from_probs
from micnaga.registry import EngineUnavailableError
from micnaga.types import AudioArray, SpeechRegion


def probs_for(spans: list[tuple[float, float]], total: float) -> NDArray[np.float32]:
    n = round(total / FRAME_S)
    p = np.full(n, 0.02, dtype=np.float32)
    for a, b in spans:
        p[round(a / FRAME_S) : round(b / FRAME_S)] = 0.95
    return p


def test_single_region_gets_padding() -> None:
    p = probs_for([(2.0, 4.0)], 8.0)
    (r,) = regions_from_probs(p, 8.0, pad_ms=200)
    assert r.start == pytest.approx(1.8, abs=0.04)
    assert r.end == pytest.approx(4.2, abs=0.04)


def test_padding_is_clamped_to_audio_bounds() -> None:
    p = probs_for([(0.0, 1.0), (7.0, 8.0)], 8.0)
    regs = regions_from_probs(p, 8.0, pad_ms=500)
    assert regs[0].start == 0.0
    assert regs[-1].end == pytest.approx(8.0)


def test_short_blip_below_min_speech_is_discarded() -> None:
    p = probs_for([(1.0, 1.15), (3.0, 4.0)], 6.0)
    regs = regions_from_probs(p, 6.0, min_speech_ms=250)
    assert len(regs) == 1
    assert regs[0].start > 2.5


def test_exactly_min_speech_kept() -> None:
    p = probs_for([(1.0, 1.28)], 3.0)  # 9 tramas de 32 ms = 288 ms
    assert len(regions_from_probs(p, 3.0, min_speech_ms=250)) == 1


def test_short_silence_does_not_split_but_long_silence_does() -> None:
    short_gap = probs_for([(1.0, 2.0), (2.3, 3.3)], 5.0)  # 0,3 s < 0,5 s
    assert len(regions_from_probs(short_gap, 5.0, min_silence_ms=500, pad_ms=0)) == 1
    long_gap = probs_for([(1.0, 2.0), (2.8, 3.8)], 6.0)  # 0,8 s > 0,5 s
    assert len(regions_from_probs(long_gap, 6.0, min_silence_ms=500, pad_ms=0)) == 2


def test_padded_regions_that_touch_are_merged() -> None:
    p = probs_for([(1.0, 2.0), (2.6, 3.6)], 6.0)  # hueco 0,6 s -> con pad 400 ms se tocan
    regs = regions_from_probs(p, 6.0, min_silence_ms=500, pad_ms=400)
    assert len(regs) == 1


def test_hysteresis_keeps_region_open_between_thresholds() -> None:
    p = probs_for([(1.0, 3.0)], 5.0)
    p[int(1.8 / FRAME_S) : int(2.4 / FRAME_S)] = 0.4  # entre neg (0,35) y umbral (0,5)
    regs = regions_from_probs(p, 5.0, pad_ms=0)
    assert len(regs) == 1
    assert regs[0].end == pytest.approx(3.0, abs=0.05)


def test_speech_until_the_end_is_closed() -> None:
    p = probs_for([(3.0, 5.0)], 5.0)
    (r,) = regions_from_probs(p, 5.0, pad_ms=100)
    assert r.end == pytest.approx(5.0, abs=0.04)


def test_long_region_is_split_at_probability_valley() -> None:
    p = probs_for([(0.0, 50.0)], 50.0)
    p[int(27.0 / FRAME_S)] = 0.4  # valle (sigue siendo voz)
    regs = regions_from_probs(p, 50.0, pad_ms=0, max_speech_s=30.0)
    assert len(regs) == 2
    assert all(r.duration <= 30.0 + 1e-6 for r in regs)
    assert regs[0].end == pytest.approx(27.0, abs=0.05)
    assert regs[0].end == regs[1].start


def test_group_regions_packs_blocks_up_to_max() -> None:
    regs = [SpeechRegion(0, 10), SpeechRegion(12, 20), SpeechRegion(22, 31), SpeechRegion(40, 45)]
    blocks = group_regions(regs, 30.0)
    assert blocks == [(0, 20), (22, 45)]
    assert all(b - a <= 30.0 + 1e-6 for a, b in blocks)


def test_group_regions_exact_blocks() -> None:
    regs = [SpeechRegion(0, 10), SpeechRegion(12, 20), SpeechRegion(22, 31)]
    assert group_regions(regs, 30.0) == [(0, 20), (22, 31)]


def test_group_regions_splits_oversize_region_evenly() -> None:
    blocks = group_regions([SpeechRegion(0, 70)], 30.0)
    assert len(blocks) == 3
    assert blocks[0][0] == 0 and blocks[-1][1] == pytest.approx(70)
    assert all(b - a <= 30.0 + 1e-6 for a, b in blocks)


def test_group_regions_empty_and_invalid() -> None:
    assert group_regions([], 30.0) == []
    with pytest.raises(ValueError):
        group_regions([SpeechRegion(0, 1)], 0)


class FakeModel:
    """Modelo falso: voz donde la amplitud del audio supera un umbral."""

    def __init__(self) -> None:
        self.calls = 0

    def probs(self, audio_16k: AudioArray) -> NDArray[np.float32]:
        self.calls += 1
        n = -(-len(audio_16k) // vad.FRAME)
        padded = np.zeros(n * vad.FRAME, dtype=np.float32)
        padded[: len(audio_16k)] = audio_16k
        frames = np.abs(padded.reshape(n, vad.FRAME)).mean(axis=1)
        return (frames > 0.05).astype(np.float32) * 0.9 + 0.01


def test_detect_speech_with_fake_model_times_and_resampling() -> None:
    sr = 16000
    x = np.zeros(6 * sr, dtype=np.float32)
    x[2 * sr : 3 * sr] = 0.3
    regs = detect_speech(x, sr, model=FakeModel())
    assert len(regs) == 1
    assert regs[0].start == pytest.approx(1.8, abs=0.05)
    assert regs[0].end == pytest.approx(3.2, abs=0.05)
    # a 8 kHz se remuestrea: los tiempos (segundos) se conservan
    x8 = np.zeros(6 * 8000, dtype=np.float32)
    x8[2 * 8000 : 3 * 8000] = 0.3
    regs8 = detect_speech(x8, 8000, model=FakeModel())
    assert regs8[0].start == pytest.approx(1.8, abs=0.1)
    assert regs8[0].end == pytest.approx(3.2, abs=0.1)


def test_detect_speech_reads_config_and_explicit_args_win() -> None:
    sr = 16000
    x = np.zeros(6 * sr, dtype=np.float32)
    x[2 * sr : 3 * sr] = 0.3
    cfg = Config()
    cfg.vad.pad_ms = 0
    (r,) = detect_speech(x, sr, model=FakeModel(), config=cfg)
    assert r.start == pytest.approx(2.0, abs=0.04)
    (r2,) = detect_speech(x, sr, pad_ms=500, model=FakeModel(), config=cfg)
    assert r2.start == pytest.approx(1.5, abs=0.04)


def test_detect_speech_empty_and_non_mono() -> None:
    assert detect_speech(np.zeros(0, dtype=np.float32), 16000, model=FakeModel()) == []
    with pytest.raises(ValueError):
        detect_speech(np.zeros((10, 2), dtype=np.float32), 16000, model=FakeModel())


def test_speech_seconds() -> None:
    assert vad.speech_seconds([SpeechRegion(0, 1.5), SpeechRegion(3, 4)]) == pytest.approx(2.5)


def test_missing_model_file_is_engine_unavailable(tmp_models: object) -> None:
    with pytest.raises(EngineUnavailableError, match="download"):
        detect_speech(np.ones(16000, dtype=np.float32) * 0.1, 16000)
    vad.release_model()
