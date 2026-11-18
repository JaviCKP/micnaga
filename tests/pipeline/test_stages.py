"""Etapas sueltas: acondicionado, alineacion, AEC, VAD, marcas de fin."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from micnaga.bench.fakes import fake_deps
from micnaga.config import Config
from micnaga.pipeline.stages import get_stages
from micnaga.pipeline.stages.base import (
    Stage,
    StageContext,
    StageResult,
    is_stage_done,
    write_marker,
)
from micnaga.pipeline.stages.s02_align import equalize
from micnaga.pipeline.stages.s03_aec import needs_aec
from micnaga.pipeline.stages.s04_condition import (
    PEAK_LIMIT,
    TARGET_RMS_DBFS,
    condition,
    highpass,
    normalize_loudness,
)
from micnaga.registry import EngineUnavailableError
from micnaga.types import PIPELINE_STAGES, CaptureMeta, DeviceChange, TrackInfo
from tests.synth import SR, silence, sine


def rms_db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12))


def meta(kind: str = "headphones", changes: list[DeviceChange] | None = None) -> CaptureMeta:
    return CaptureMeta(
        started_at="2026-11-18T10:30:00+01:00",
        mic=TrackInfo("audio_yo.flac", "mic"),
        system=TrackInfo("audio_remoto.flac", "sys"),
        output_kind=kind,  # type: ignore[arg-type]
        device_changes=changes or [],
    )


def test_there_are_eleven_stages_in_spec_order() -> None:
    stages = get_stages()
    assert [s.name for s in stages] == list(PIPELINE_STAGES)
    assert [s.index for s in stages] == list(range(1, 12))


def test_highpass_removes_rumble_and_keeps_speech_band() -> None:
    low = sine(20, 2.0, amp=0.5)
    mid = sine(400, 2.0, amp=0.5)
    assert rms_db(highpass(low, SR)) < rms_db(low) - 25
    assert abs(rms_db(highpass(mid, SR)) - rms_db(mid)) < 0.5
    assert len(highpass(mid, SR)) == len(mid)


def test_loudness_normalization_hits_target_and_respects_limits() -> None:
    quiet = sine(300, 3.0, amp=0.01)
    out, gain_db = normalize_loudness(quiet, SR)
    assert rms_db(out) == pytest.approx(TARGET_RMS_DBFS, abs=0.5)
    assert gain_db == pytest.approx(rms_db(out) - rms_db(quiet), abs=0.1)
    loud = sine(300, 3.0, amp=0.95)
    out2, _ = normalize_loudness(loud, SR)
    assert float(np.max(np.abs(out2))) <= PEAK_LIMIT + 1e-6


def test_silent_track_is_left_untouched() -> None:
    out, gain = condition(silence(2.0), SR)
    assert gain == 0.0 and float(np.max(np.abs(out))) == 0.0
    noise_floor = (0.0005 * np.random.default_rng(0).standard_normal(SR * 2)).astype(np.float32)
    out2, gain2 = normalize_loudness(noise_floor, SR)
    assert gain2 == 0.0 and np.array_equal(out2, noise_floor)


def test_equalize_pads_the_shorter_track_with_silence() -> None:
    a, b = equalize(sine(300, 1.0), sine(300, 1.5))
    assert len(a) == len(b) == int(1.5 * SR)
    assert float(np.max(np.abs(a[SR:]))) == 0.0


def test_aec_needed_for_speakers_unknown_or_a_switch_to_speakers() -> None:
    assert not needs_aec(meta("headphones"))
    assert needs_aec(meta("speakers"))
    assert needs_aec(meta("unknown"))
    assert needs_aec(meta("headphones", [DeviceChange(300, "system", "Altavoces", "speakers")]))
    # un cambio del micro no es un cambio de salida
    assert not needs_aec(meta("headphones", [DeviceChange(300, "mic", "Otro micro", "unknown")]))
    assert not needs_aec(
        meta("headphones", [DeviceChange(300, "system", "Auriculares 2", "headphones")])
    )


def test_marker_requires_all_outputs(tmp_path: Path) -> None:
    stage = Stage(1, "gates", lambda ctx: StageResult())
    work = tmp_path / "work"
    work.mkdir()
    assert not is_stage_done(tmp_path, stage)
    (work / "x.json").write_text("{}")
    write_marker(tmp_path, stage, StageResult(["x.json"], {"a": 1}), 0.5)
    assert is_stage_done(tmp_path, stage)
    (work / "x.json").unlink()
    assert not is_stage_done(tmp_path, stage)


def test_corrupt_marker_means_not_done(tmp_path: Path) -> None:
    stage = Stage(2, "align", lambda ctx: StageResult())
    work = tmp_path / "work"
    work.mkdir()
    (work / stage.marker_name).write_text("{truncado")
    assert not is_stage_done(tmp_path, stage)


def test_stage3_skips_without_a_canceller(tmp_path: Path) -> None:
    from micnaga.pipeline.stages import s03_aec

    deps = fake_deps()
    deps.create_aec = lambda _c: None
    ctx = StageContext(tmp_path, Config(), deps, meta("speakers"))
    assert s03_aec.run(ctx).info == {"skipped": "off"}

    def unavailable(_c: Config) -> object:
        raise EngineUnavailableError("no aec")

    deps.create_aec = unavailable  # type: ignore[assignment]
    result = s03_aec.run(StageContext(tmp_path, Config(), deps, meta("speakers")))
    assert result.info["skipped"] == "unavailable" and result.outputs == []
    assert s03_aec.run(StageContext(tmp_path, Config(), deps, meta("headphones"))).info == {
        "skipped": "headphones"
    }
