from __future__ import annotations

from pathlib import Path

import pytest
import soundfile as sf

from micnaga import gates, paths
from micnaga.bench.fakes import energy_vad
from micnaga.bench.synthetic import SyntheticSpec, generate_session
from micnaga.config import GatesConfig
from micnaga.types import SpeechRegion

CFG = GatesConfig(min_duration_s=180.0, min_remote_speech_s=30.0, min_turns=6)  # spec §5


def test_evaluate_gates_all_pass() -> None:
    r = gates.evaluate_gates(240, 100, 12, False, CFG)
    assert r.passed and r.reason == "ok" and r.gate is None


@pytest.mark.parametrize(
    ("duration", "remote", "turns", "gate"),
    [
        (120, 100, 12, "duration"),
        (240, 10, 12, "remote_speech"),
        (240, 100, 3, "turns"),
        (120, 0, 0, "duration"),  # la primera que falla manda
    ],
)
def test_evaluate_gates_failures(duration: float, remote: float, turns: int, gate: str) -> None:
    r = gates.evaluate_gates(duration, remote, turns, False, CFG)
    assert not r.passed and r.gate == gate


def test_confirmed_session_only_needs_duration() -> None:
    assert gates.evaluate_gates(200, 0, 0, True, CFG).passed
    r = gates.evaluate_gates(100, 50, 20, True, CFG)
    assert not r.passed and r.gate == "duration"


def test_thresholds_come_from_config() -> None:
    cfg = GatesConfig(min_duration_s=10, min_remote_speech_s=1, min_turns=1)
    assert gates.evaluate_gates(10, 1, 1, False, cfg).passed
    assert not gates.evaluate_gates(9.9, 1, 1, False, cfg).passed


def test_reason_has_no_identifying_data() -> None:
    reason = gates.evaluate_gates(120, 0, 0, False, CFG).reason
    assert reason == "duracion 120 s < 180 s"


def test_remote_speech_is_the_union_of_regions() -> None:
    regions = [SpeechRegion(0, 10), SpeechRegion(5, 12), SpeechRegion(20, 25)]
    assert gates.remote_speech_seconds(regions) == pytest.approx(17.0)
    assert gates.remote_speech_seconds([(0.0, 1.0), (1.0, 2.0)]) == pytest.approx(2.0)


def _alternating(
    n: int, turn: float = 5.0, gap: float = 1.0, pad: float = 0.2
) -> tuple[list, list]:
    me, remote = [], []
    t = 0.0
    for i in range(n):
        target = me if i % 2 == 0 else remote
        target.append(SpeechRegion(max(0.0, t - pad), t + turn + pad))
        t += turn + gap
    return me, remote


def test_alternating_turns_are_counted() -> None:
    me, remote = _alternating(10)
    assert gates.count_turn_changes(me, remote) == 9


@pytest.mark.parametrize("pad", [0.0, 0.2, 0.4])
def test_turn_count_does_not_depend_on_vad_padding(pad: float) -> None:
    me, remote = _alternating(10, gap=0.5, pad=pad)
    assert gates.count_turn_changes(me, remote) == 9


def test_consecutive_regions_of_the_same_track_are_one_turn() -> None:
    me = [SpeechRegion(0, 5), SpeechRegion(6, 10), SpeechRegion(11, 15)]
    remote = [SpeechRegion(20, 30), SpeechRegion(31, 40)]
    assert gates.count_turn_changes(me, remote) == 1


def test_short_overlap_does_not_create_extra_turns() -> None:
    me = [SpeechRegion(0, 10.1)]
    remote = [SpeechRegion(10.0, 20)]  # solape de 0,1 s: un solo relevo
    assert gates.count_turn_changes(me, remote) == 1


def test_youtube_continuous_remote_and_silent_mic_has_no_turns() -> None:
    remote = [SpeechRegion(i * 10.0, i * 10.0 + 9.5) for i in range(30)]
    assert gates.count_turn_changes([], remote) == 0
    result, metrics = gates.evaluate_regions(300, [], remote, False, CFG)
    assert metrics.remote_speech_s > 30
    assert not result.passed and result.gate == "turns"


def test_youtube_on_speakers_mic_is_only_the_echo() -> None:
    """Mic = el sistema desplazado 40 ms y algo mas corto (eco): no debe fabricar turnos."""
    remote = [SpeechRegion(i * 10.0, i * 10.0 + 9.5) for i in range(30)]
    mic = [SpeechRegion(r.start + 0.04, r.end - 0.3 + 0.04) for r in remote]
    assert gates.count_turn_changes(mic, remote) == 0
    mic_long_tail = [SpeechRegion(r.start + 0.04, r.end + 0.25) for r in remote]
    assert gates.count_turn_changes(mic_long_tail, remote) == 0


def test_voice_note_mic_only_has_no_remote_speech() -> None:
    me = [SpeechRegion(i * 8.0, i * 8.0 + 6) for i in range(15)]
    result, metrics = gates.evaluate_regions(120, me, [], False, CFG)
    assert metrics.remote_speech_s == 0 and not result.passed


def test_merge_regions_accepts_tuples_and_regions() -> None:
    merged = gates.merge_regions([(5.0, 6.0), SpeechRegion(0, 2), (1.0, 3.0), (3.0, 4.0)])
    assert merged == [(0.0, 4.0), (5.0, 6.0)]


# ------------------------------------------------------------ con audio sintetico (E/S real)


def _regions(folder: Path) -> tuple[list[SpeechRegion], list[SpeechRegion], float]:
    mic, sr = sf.read(str(folder / paths.AUDIO_ME_FLAC), dtype="float32")
    system, _ = sf.read(str(folder / paths.AUDIO_REMOTE_FLAC), dtype="float32")
    return (
        energy_vad(mic, sr, 250, 500, 200),
        energy_vad(system, sr, 250, 500, 200),
        len(mic) / sr,
    )


@pytest.fixture(scope="module")
def synthetic_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("gates-synth")


def test_voice_note_of_two_minutes_is_discarded(synthetic_root: Path) -> None:
    folder = generate_session(
        synthetic_root / "note", SyntheticSpec(kind="voice_note", duration_s=120.0)
    )
    me, system, duration = _regions(folder)
    result, metrics = gates.evaluate_regions(duration, me, system, False, CFG)
    assert not result.passed
    assert metrics.remote_speech_s == 0
    # incluso con la duracion suficiente cae en la compuerta 2
    long_result = gates.evaluate_gates(240, metrics.remote_speech_s, 0, False, CFG)
    assert long_result.gate == "remote_speech"


def test_real_meeting_of_four_minutes_passes(synthetic_root: Path) -> None:
    folder = generate_session(synthetic_root / "meeting", SyntheticSpec(duration_s=240.0))
    me, system, duration = _regions(folder)
    result, metrics = gates.evaluate_regions(duration, me, system, False, CFG)
    assert result.passed, result.reason
    assert metrics.turn_changes >= 20 and metrics.remote_speech_s > 60


def test_meeting_on_speakers_with_echo_passes(synthetic_root: Path) -> None:
    spec = SyntheticSpec(duration_s=240.0, output_kind="speakers", echo=True, seed=1)
    folder = generate_session(synthetic_root / "meeting-echo", spec)
    me, system, duration = _regions(folder)
    result, metrics = gates.evaluate_regions(duration, me, system, False, CFG)
    assert result.passed, result.reason
    assert metrics.turn_changes >= 20


def test_youtube_like_case_fails_the_turns_gate(synthetic_root: Path) -> None:
    spec = SyntheticSpec(kind="youtube", duration_s=240.0, output_kind="speakers", echo=True)
    folder = generate_session(synthetic_root / "yt", spec)
    me, system, duration = _regions(folder)
    result, metrics = gates.evaluate_regions(duration, me, system, False, CFG)
    assert metrics.remote_speech_s > 100
    assert not result.passed and result.gate == "turns"
