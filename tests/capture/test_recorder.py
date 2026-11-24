"""``Recorder`` de extremo a extremo con fuentes sinteticas pacidas en tiempo real."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from micnaga import paths
from micnaga.capture.recorder import CaptureError, Recorder, RecorderEvent, RecorderState
from micnaga.capture.sources import EndpointInfo, SourceError
from micnaga.capture.sync import align_tracks
from micnaga.capture.synthetic import SyntheticBackend, SyntheticSource
from tests.capture.fakes import (
    FakeDevices,
    bursts,
    cfg,
    make_backend,
    meta_of,
    read_track,
    tone,
    tone_with_sentinel,
)

SR = 16000


def _src(info: EndpointInfo, signal, rate: int = 48000, channels: int = 2, **kw) -> SyntheticSource:  # type: ignore[no-untyped-def]
    return SyntheticSource(info, signal, sample_rate=rate, channels=channels, **kw)


def _tones_backend(devices: FakeDevices | None = None) -> tuple[SyntheticBackend, FakeDevices]:
    devices = devices or FakeDevices()
    backend = make_backend(
        devices,
        lambda info: _src(info, tone(300, 48000)),
        lambda info: _src(info, tone(500, 44100), rate=44100),
    )
    return backend, devices


def test_records_two_tracks_with_metadata(tmp_path: Path) -> None:
    backend, _ = _tones_backend()
    events: list[RecorderEvent] = []
    rec = Recorder(cfg(), backend=backend, on_event=events.append)
    assert rec.state is RecorderState.IDLE and rec.levels() == (0.0, 0.0)
    session = rec.start(
        tmp_path / "s1", confirmed=True, meet_title="Weekly", meet_code="abc-defg-hij"
    )
    assert rec.state is RecorderState.RECORDING
    time.sleep(max(0, 1.2 - rec.elapsed_s()))
    mic_level, sys_level = rec.levels()
    assert mic_level == pytest.approx(0.3 / np.sqrt(2), abs=0.05)
    assert sys_level == pytest.approx(0.3 / np.sqrt(2), abs=0.05)
    time.sleep(max(0, 3.0 - rec.elapsed_s()))
    assert rec.elapsed_s() == pytest.approx(3.0, abs=0.3)
    status = rec.status
    assert status.mic_samples > SR and status.system_samples > SR and status.confirmed
    stopped = rec.stop()
    assert stopped == session and rec.state is RecorderState.STOPPED
    assert rec.levels() == (0.0, 0.0)
    assert rec.stop() == session  # idempotente

    mic = read_track(session / paths.AUDIO_ME_WAV)
    sysm = read_track(session / paths.AUDIO_REMOTE_WAV)
    assert len(mic) == pytest.approx(3.0 * SR, abs=0.3 * SR)
    assert abs(len(mic) - len(sysm)) <= 0.25 * SR
    assert float(np.max(np.abs(mic[SR:-SR]))) == pytest.approx(0.3, abs=0.02)

    meta = meta_of(session)
    assert meta.extra["state"] == "stopped"
    assert meta.confirmed and meta.meet_title == "Weekly" and meta.meet_code == "abc-defg-hij"
    assert meta.mic.device == "Mic (fake)" and meta.system.device == "Speakers (fake)"
    assert meta.mic.file == paths.AUDIO_ME_WAV and meta.system.file == paths.AUDIO_REMOTE_WAV
    assert meta.output_kind == "speakers" and meta.extra["initial_output_kind"] == "speakers"
    assert meta.mic.samples == len(mic) and meta.system.samples == len(sysm)
    for track in (meta.mic, meta.system):
        assert track.sample_rate == SR
        assert len(track.sync) >= 4  # ancla + cada 1 s + cierre
        assert track.sync[0].samples == 0
        assert track.sync[-1].samples == pytest.approx(len(mic), abs=0.25 * SR)
    assert meta.extra["sources"]["system"] == {
        "device": "Speakers (fake)",
        "sample_rate": 44100,
        "channels": 2,
    }
    assert [e.kind for e in events] == ["state", "state"]


def test_private_pause_leaves_nothing_on_disk_and_keeps_alignment(tmp_path: Path) -> None:
    devices = FakeDevices()
    # Valor "centinela" solo durante la pausa (reloj de las fuentes: tiempo desde start()).
    window = (1.3, 2.35)
    backend = make_backend(
        devices,
        lambda info: _src(info, tone_with_sentinel(300, 48000, window)),
        lambda info: _src(info, tone_with_sentinel(500, 44100, window), rate=44100),
    )
    rec = Recorder(cfg(), backend=backend)
    session = rec.start(tmp_path / "s")
    time.sleep(max(0, 1.2 - rec.elapsed_s()))
    assert rec.pause() and not rec.pause()
    assert rec.state is RecorderState.PAUSED
    t_pause = rec.elapsed_s()
    time.sleep(0.4)
    size_a = [(session / n).stat().st_size for n in (paths.AUDIO_ME_WAV, paths.AUDIO_REMOTE_WAV)]
    assert rec.levels() == (0.0, 0.0)
    time.sleep(0.5)
    size_b = [(session / n).stat().st_size for n in (paths.AUDIO_ME_WAV, paths.AUDIO_REMOTE_WAV)]
    assert size_a == size_b, "los WAV crecieron durante la pausa privada"
    assert rec.elapsed_s() == pytest.approx(t_pause, abs=1e-6)  # el cronometro se congela
    assert rec.resume() and not rec.resume()
    time.sleep(1.3)
    rec.stop()

    mic = read_track(session / paths.AUDIO_ME_WAV)
    sysm = read_track(session / paths.AUDIO_REMOTE_WAV)
    assert float(np.max(np.abs(mic))) < 0.6, "audio de la pausa llego al micro"
    assert float(np.max(np.abs(sysm))) < 0.6, "audio de la pausa llego al sistema"
    assert abs(len(mic) - len(sysm)) <= 0.25 * SR
    meta = meta_of(session)
    assert len(meta.pauses) == 1
    pause = meta.pauses[0]
    assert pause.at_s == pytest.approx(1.2, abs=0.15)
    assert pause.wall_s == pytest.approx(0.4 + 0.5 + 0.3, abs=0.2)  # + 0.3 s de margen de guarda
    # el audio dura lo grabado fuera de la pausa (~1.2 s + ~1.3 s)
    assert len(mic) == pytest.approx(2.5 * SR, abs=0.35 * SR)
    # el audio posterior a la pausa es real (no silencio)
    assert float(np.max(np.abs(mic[-SR // 2 :]))) > 0.2


def test_markers_use_recording_time_without_pauses(tmp_path: Path) -> None:
    backend, _ = _tones_backend()
    rec = Recorder(cfg(), backend=backend)
    session = rec.start(tmp_path / "s")
    time.sleep(max(0, 0.5 - rec.elapsed_s()))
    m1 = rec.add_marker("uno")
    rec.pause()
    time.sleep(0.8)
    rec.resume()
    time.sleep(0.6)
    m2 = rec.add_marker()
    t_total = time.perf_counter()
    rec.stop()
    assert m1.label == "uno" and m1.t == pytest.approx(0.5, abs=0.1)
    # 0.5 s antes de la pausa + (0.6 s tras reanudar - 0.3 s de guarda); la pausa no cuenta
    assert m2.t == pytest.approx(0.5 + 0.6 - 0.3, abs=0.15)
    meta = meta_of(session)
    assert [m.label for m in meta.markers] == ["uno", ""]
    assert t_total > 0
    with pytest.raises(CaptureError):
        rec.add_marker("after stop")


def test_device_change_reattaches_in_under_a_second_and_keeps_continuity(tmp_path: Path) -> None:
    devices = FakeDevices()
    events: list[RecorderEvent] = []
    backend = make_backend(
        devices,
        lambda info: _src(info, tone(300, 48000)),
        lambda info: _src(info, tone(500, 48000)),
    )
    rec = Recorder(cfg(), backend=backend, on_event=events.append)
    session = rec.start(tmp_path / "s")
    time.sleep(1.2)
    t_swap = time.perf_counter()
    devices.render = EndpointInfo("render-B", "Headphones (fake)", "headphones")
    deadline = t_swap + 3.0
    while time.perf_counter() < deadline and not any(e.kind == "device_change" for e in events):
        time.sleep(0.02)
    latency = time.perf_counter() - t_swap
    assert latency < 1.0, f"re-attach took {latency:.2f} s"
    time.sleep(1.2)
    rec.stop()

    meta = meta_of(session)
    assert len(meta.device_changes) == 1
    change = meta.device_changes[0]
    assert change.track == "system" and change.device == "Headphones (fake)"
    assert change.output_kind == "headphones"
    assert change.t == pytest.approx(1.2, abs=0.5)
    assert meta.system.device == "Headphones (fake)"
    # politica: si en algun momento sonaron altavoces, el conjunto cuenta como altavoces
    assert meta.output_kind == "speakers"
    assert meta.extra["output_kinds_seen"] == ["speakers", "headphones"]
    sysm = read_track(session / paths.AUDIO_REMOTE_WAV)
    mic = read_track(session / paths.AUDIO_ME_WAV)
    assert abs(len(sysm) - len(mic)) <= 0.25 * SR  # continuidad de linea de tiempo
    # el hueco del cambio (ceros exactos) es corto
    silent = np.abs(sysm) < 1e-4
    longest = _longest_run(silent[SR // 5 :])  # sin contar el relleno inicial
    assert longest < 0.4 * SR
    assert any(e.kind == "device_change" for e in events)


def _longest_run(mask: np.ndarray) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def test_loopback_silence_is_filled_and_the_file_keeps_growing(tmp_path: Path) -> None:
    devices = FakeDevices()
    backend = make_backend(
        devices,
        lambda info: _src(info, tone(300, 48000)),
        lambda info: _src(info, tone(500, 48000), gaps=[(0.8, 2.6)]),
    )
    rec = Recorder(cfg(), backend=backend)
    session = rec.start(tmp_path / "s")
    time.sleep(2.0)  # en pleno silencio del loopback
    on_disk = (session / paths.AUDIO_REMOTE_WAV).stat().st_size // 2
    assert on_disk > 0.7 * SR, "el WAV del sistema no crece durante el silencio"
    time.sleep(1.4)
    rec.stop()
    sysm = read_track(session / paths.AUDIO_REMOTE_WAV)
    mic = read_track(session / paths.AUDIO_ME_WAV)
    assert abs(len(sysm) - len(mic)) <= 0.25 * SR
    gap = sysm[round(1.0 * SR) : round(2.4 * SR)]
    assert not np.any(gap)
    assert float(np.max(np.abs(sysm[round(2.9 * SR) :]))) > 0.2  # el audio vuelve
    assert float(np.max(np.abs(sysm[round(0.2 * SR) : round(0.7 * SR)]))) > 0.2


def test_drift_is_logged_and_corrected_by_align_tracks(tmp_path: Path) -> None:
    """El micro va a ritmo ideal y el sistema 2 % lento: tras ``align_tracks`` casan."""
    times = [0.6, 1.4, 2.2, 3.0, 3.8, 4.6]
    devices = FakeDevices()
    drift = 20000.0
    backend = make_backend(
        devices,
        lambda info: _src(info, bursts(times, 48000), rate=48000),
        lambda info: _src(info, bursts(times, 48000, drift_ppm=drift), rate=48000, drift_ppm=drift),
    )
    rec = Recorder(cfg(), backend=backend)
    session = rec.start(tmp_path / "s")
    time.sleep(5.4)
    rec.stop()
    meta = meta_of(session)
    mic = read_track(session / paths.AUDIO_ME_WAV)
    sysm = read_track(session / paths.AUDIO_REMOTE_WAV)
    t0 = rec._timeline.t0_ns  # type: ignore[union-attr]
    opened = {s.info.id: (s.opened_at_ns - t0) / 1e9 for s in backend.sources}  # type: ignore[attr-defined]

    def burst_peaks(x: np.ndarray, delay_s: float) -> list[float]:
        out = []
        for c in times:
            i = round((c + delay_s) * SR)
            seg = np.abs(x[max(i - 3000, 0) : i + 3000])
            out.append((int(np.argmax(seg)) + max(i - 3000, 0)) / SR - c - delay_s)
        return out

    delay_m, delay_s = opened["cap-A"], opened["render-A"]
    # sin alinear, el sistema se desvia > 50 ms hacia el final
    raw_err = max(abs(e) for e in burst_peaks(sysm, delay_s)[3:])
    assert raw_err > 0.06
    mic_a, sys_a = align_tracks(mic, sysm, meta)
    assert len(mic_a) == len(sys_a)
    err_m = max(abs(e) for e in burst_peaks(mic_a, delay_m))
    err_s = max(abs(e) for e in burst_peaks(sys_a, delay_s))
    assert err_m < 0.015 and err_s < 0.015, (err_m, err_s)


def test_one_source_failing_does_not_stop_the_recording(tmp_path: Path) -> None:
    devices = FakeDevices()
    events: list[RecorderEvent] = []

    def bad_mic(info: EndpointInfo) -> SyntheticSource:
        raise SourceError("mic busy")

    backend = make_backend(devices, bad_mic, lambda info: _src(info, tone(500, 48000)))
    rec = Recorder(cfg(), backend=backend, on_event=events.append)
    session = rec.start(tmp_path / "s")
    time.sleep(1.5)
    rec.stop()
    mic = read_track(session / paths.AUDIO_ME_WAV)
    sysm = read_track(session / paths.AUDIO_REMOTE_WAV)
    assert not np.any(mic) and abs(len(mic) - len(sysm)) <= 0.7 * SR  # mic: ceros (idle)
    assert float(np.max(np.abs(sysm[SR // 2 :]))) > 0.2
    assert any(e.kind == "source_error" and e.track == "mic" for e in events)
    assert meta_of(session).mic.device == ""


def test_both_sources_failing_raises(tmp_path: Path) -> None:
    def bad(info: EndpointInfo) -> SyntheticSource:
        raise SourceError("no device")

    backend = make_backend(FakeDevices(), bad, bad)
    rec = Recorder(cfg(), backend=backend)
    with pytest.raises(CaptureError):
        rec.start(tmp_path / "s")
    assert rec.state is RecorderState.IDLE
    assert not (tmp_path / "s").exists()  # no deja carpetas ni WAV vacios


def test_in_person_start_rejects_a_microphone_that_delivers_no_packets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("micnaga.capture.recorder.STARTUP_AUDIO_TIMEOUT_S", 0.3)
    backend = make_backend(
        FakeDevices(),
        lambda info: _src(info, tone(300, 48000), gaps=[(0, 10)]),
        lambda info: _src(info, tone(500, 48000)),
    )
    events: list[RecorderEvent] = []
    rec = Recorder(cfg(), backend=backend, on_event=events.append)
    folder = tmp_path / "no-mic-packets"
    with pytest.raises(CaptureError, match="no audio packet received from microphone"):
        rec.start(folder, confirmed=True, require_microphone=True)
    assert not any(event.kind == "state" and event.message == "recording" for event in events)
    assert not meta_of(folder).confirmed


def test_a_failed_source_is_retried_and_recovers(tmp_path: Path) -> None:
    devices = FakeDevices()
    events: list[RecorderEvent] = []
    attempts = {"n": 0}

    def flaky_mic(info: EndpointInfo) -> SyntheticSource:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise SourceError("busy")
        return _src(info, tone(300, 48000))

    backend = make_backend(devices, flaky_mic, lambda info: _src(info, tone(500, 48000)))
    rec = Recorder(cfg(), backend=backend, on_event=events.append)
    session = rec.start(tmp_path / "s")  # 1.er intento falla; reintenta cada 1 s
    time.sleep(3.2)
    rec.stop()
    mic = read_track(session / paths.AUDIO_ME_WAV)
    assert float(np.max(np.abs(mic[-SR // 2 :]))) > 0.2
    assert any(e.kind == "source_recovered" for e in events)


def test_cannot_start_twice_or_stop_before_start(tmp_path: Path) -> None:
    backend, _ = _tones_backend()
    rec = Recorder(cfg(), backend=backend)
    with pytest.raises(CaptureError):
        rec.stop()
    rec.start(tmp_path / "s")
    with pytest.raises(CaptureError):
        rec.start(tmp_path / "t")
    rec.stop()
    assert backend.closed


def test_set_confirmed_updates_capture_json_live(tmp_path: Path) -> None:
    backend, _ = _tones_backend()
    rec = Recorder(cfg(), backend=backend)
    session = rec.start(tmp_path / "s")
    assert meta_of(session).confirmed is False
    assert meta_of(session).extra["state"] == "recording"
    rec.set_confirmed(True)
    rec.set_meeting_info("Titulo", "aaa-bbbb-ccc")
    meta = meta_of(session)
    assert meta.confirmed and meta.meet_title == "Titulo" and meta.meet_code == "aaa-bbbb-ccc"
    rec.stop()


def test_levels_are_cheap_enough_for_20_fps(tmp_path: Path) -> None:
    backend, _ = _tones_backend()
    rec = Recorder(cfg(), backend=backend)
    rec.start(tmp_path / "s")
    time.sleep(0.3)
    t = time.perf_counter()
    for _ in range(2000):
        rec.levels()
    per_call = (time.perf_counter() - t) / 2000
    rec.stop()
    assert per_call < 0.001  # << 50 ms (20 fps)
