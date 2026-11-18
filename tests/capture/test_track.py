"""``Track``: colocacion en la linea de tiempo, huecos, silencio, deriva, pausas y marcas."""

from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import pytest

from micnaga.capture.sync import estimate_drift
from micnaga.capture.timeline import Timeline
from tests.capture.helpers import ManualClock, feed_stream, make_track, read_wav, s_to_ns
from tests.synth import SR, noise, sine


def test_leading_silence_pads_to_session_start(tmp_path: Path) -> None:
    track, writer, _, _ = make_track(tmp_path)
    sig = sine(440, 0.1, amp=0.5)
    track.feed(sig, s_to_ns(0.35))  # primer paquete: ocupa [0.25, 0.35]
    track.finish(s_to_ns(0.35))
    writer.close()
    out = read_wav(tmp_path / "t.wav")
    assert len(out) == round(0.35 * SR)
    assert not np.any(out[: round(0.25 * SR)])
    np.testing.assert_allclose(out[round(0.25 * SR) :], sig, atol=1e-4)


def test_gap_fill_has_exact_duration(tmp_path: Path) -> None:
    track, writer, _, _ = make_track(tmp_path, gap_threshold_s=0.1)
    a = sine(300, 0.1)
    b = sine(500, 0.1)
    track.feed(a, s_to_ns(0.1))  # [0.0, 0.1]
    track.feed(b, s_to_ns(1.6))  # [1.5, 1.6]: hueco de 1.4 s
    track.finish(s_to_ns(1.6))
    writer.close()
    out = read_wav(tmp_path / "t.wav")
    assert len(out) == round(1.6 * SR)
    gap = out[round(0.1 * SR) : round(1.5 * SR)]
    assert len(gap) == round(1.4 * SR) and not np.any(gap)
    np.testing.assert_allclose(out[round(1.5 * SR) :], b, atol=1e-4)
    assert track.stats.gap_fills == 1
    assert track.stats.gap_fill_s == pytest.approx(1.4, abs=1 / SR)


def test_small_jitter_is_not_filled(tmp_path: Path) -> None:
    track, writer, _, _ = make_track(tmp_path, gap_threshold_s=0.1)
    sig = noise(10.0, seed=1)
    rng = np.random.default_rng(0)
    n = SR // 10
    for k in range(100):
        late = float(rng.uniform(0, 0.03))  # llegadas tarde de hasta 30 ms
        track.feed(sig[k * n : (k + 1) * n], s_to_ns((k + 1) * 0.1 + late))
    track.finish(s_to_ns(10.03))
    writer.close()
    assert track.stats.gap_fills == 0


def test_slow_device_clock_causes_no_false_fills(tmp_path: Path) -> None:
    """500 ppm lento y sin huecos: cero rellenos (la deriva no es un hueco)."""
    track, writer, _, _ = make_track(tmp_path, gap_threshold_s=0.05)
    sig = noise(120.0, seed=2)
    feed_stream(track, sig, SR, slow=1.0 + 500e-6)
    writer.close()
    assert track.stats.gap_fills == 0
    assert track.stats.idle_fill_s == 0
    assert abs(track.samples - len(sig)) <= 2  # ni una muestra anadida ni quitada


def test_48k_stereo_float32_becomes_16k_mono(tmp_path: Path) -> None:
    track, writer, _, _ = make_track(tmp_path, in_rate=48000)
    dur = 3.0
    t = np.arange(round(dur * 48000)) / 48000
    left = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    right = (0.3 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    n = 4800
    for k in range(round(dur * 10)):
        frames = np.stack([left[k * n : (k + 1) * n], right[k * n : (k + 1) * n]], axis=1)
        track.feed(frames, s_to_ns((k + 1) * 0.1))
    track.finish(s_to_ns(dur))
    writer.close()
    out = read_wav(tmp_path / "t.wav")
    assert abs(len(out) - round(dur * SR)) <= 2
    mid = out[SR // 2 : -SR // 2]
    assert float(np.max(np.abs(mid))) == pytest.approx(0.4, abs=0.01)  # media de L y R
    spec = np.abs(np.fft.rfft(mid * np.hanning(len(mid))))
    assert abs(np.argmax(spec) * SR / len(mid) - 1000) < 5  # sigue siendo 1 kHz


def test_idle_fill_keeps_growing_and_late_packet_is_trimmed(tmp_path: Path) -> None:
    track, writer, _, clock = make_track(tmp_path, idle_margin_s=0.5, idle_step_s=0.25)
    track.feed(sine(300, 0.1), s_to_ns(0.1))
    clock.set_s(3.0)
    track.idle(clock())  # silencio del loopback: rellena hasta 3.0 - 0.5
    assert track.samples == pytest.approx(2.5 * SR, abs=2)
    # llega un paquete tardio que cubre [2.3, 2.6]: solapa 0.2 s con el relleno
    track.feed(sine(500, 0.3), s_to_ns(2.6))
    track.finish(s_to_ns(3.0))
    writer.close()
    out = read_wav(tmp_path / "t.wav")
    assert len(out) == round(3.0 * SR)  # el solape no alarga la pista
    assert track.stats.trimmed_s == pytest.approx(0.2, abs=0.01)
    assert not np.any(out[round(0.2 * SR) : round(2.5 * SR)])
    assert np.any(out[round(2.5 * SR) : round(2.6 * SR)])


def test_sync_marks_track_drift_and_include_anchors(tmp_path: Path) -> None:
    """100 ppm lento con jitter de llegada (solo retrasa): las marcas dan la deriva."""
    track, writer, _, _ = make_track(tmp_path, sync_interval_s=10.0, in_rate=48000)
    rng = np.random.default_rng(5)
    n = 4800
    slow = 1.0 + 100e-6
    total = 180.0
    sig = noise(total, sr=48000, seed=4)
    for k in range(round(total * 10)):
        late = float(rng.uniform(0, 0.02))
        track.feed(sig[k * n : (k + 1) * n], s_to_ns((k + 1) * 0.1 * slow + late))
    end = s_to_ns(total * slow)
    track.finish(end)
    writer.close()
    marks = track.marks
    assert marks[0].samples == 0 and marks[0].clock_s == 0.0
    assert len(marks) >= 17
    assert all(
        b.samples > a.samples and b.clock_s > a.clock_s for a, b in itertools.pairwise(marks)
    )
    # la ultima marca es el anclaje exacto del cierre
    assert marks[-1].clock_s == pytest.approx(total * slow, abs=0.025)  # (o fin del ultimo dato)
    est = estimate_drift(marks, SR)
    assert est.ppm == pytest.approx(100, abs=40)


def test_pause_writes_nothing_and_cuts_both_tracks_at_the_same_instant(tmp_path: Path) -> None:
    """Dos pistas con paquetes distintos: la pausa [1.0, 2.0) no deja audio en ninguna."""
    clock = ManualClock()
    tl = Timeline(clock, t0_ns=0, pause_guard_s=0.3)
    mic, mw, _, _ = make_track(tmp_path, "mic", clock=clock, timeline=tl)
    sysm, sw, _, _ = make_track(tmp_path, "sys", clock=clock, timeline=tl, in_rate=48000)
    sentinel_s = 0.9  # valor que solo existe DURANTE la pausa
    total = 4.0
    t = np.arange(round(total * SR)) / SR
    audio16 = (0.4 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    audio16[(t >= 1.0) & (t < 2.0)] = sentinel_s
    t48 = np.arange(round(total * 48000)) / 48000
    audio48 = (0.4 * np.sin(2 * np.pi * 300 * t48)).astype(np.float32)
    audio48[(t48 >= 1.0) & (t48 < 2.0)] = sentinel_s

    # Los paquetes van llegando mientras el "reloj" avanza; la pausa se abre y cierra en el medio.
    mic_pk, sys_pk = 0.1, 0.07
    events = sorted(
        [("mic", (k + 1) * mic_pk) for k in range(round(total / mic_pk))]
        + [("sys", (k + 1) * sys_pk) for k in range(int(total / sys_pk))]
        + [("pause", 1.0), ("resume", 2.0)],
        key=lambda e: (e[1], e[0]),
    )
    counters = {"mic": 0, "sys": 0}
    for what, at in events:
        clock.set_s(at)
        if what == "pause":
            tl.pause()
        elif what == "resume":
            tl.resume()
        elif what == "mic":
            k = counters["mic"]
            counters["mic"] += 1
            n = round(mic_pk * SR)
            mic.feed(audio16[k * n : (k + 1) * n], s_to_ns(at))
        else:
            k = counters["sys"]
            counters["sys"] += 1
            n = round(sys_pk * 48000)
            sysm.feed(audio48[k * n : (k + 1) * n], s_to_ns(at))
        mic.idle(clock())
        sysm.idle(clock())
    clock.set_s(total)
    end = s_to_ns(total)
    mic.finish(end)
    sysm.finish(end)
    mw.close()
    sw.close()
    a, b = read_wav(tmp_path / "mic.wav"), read_wav(tmp_path / "sys.wav")
    for out in (a, b):
        assert float(np.max(np.abs(out))) < 0.5, "audio de la pausa llego a disco"
    # linea de tiempo grabada: 4 s - (1.0 + 0.3 de margen) = 2.7 s; las dos pistas casan
    assert len(a) == len(b) == pytest.approx(2.7 * SR, abs=2)
    pauses = tl.pause_records()
    assert pauses[0][0] == pytest.approx(1.0) and pauses[0][1] == pytest.approx(1.3)
    # los dos cortes caen en el mismo indice (rt = 1.0)
    for out in (a, b):
        assert np.any(out[round(0.9 * SR) : round(1.0 * SR) - 400])
        assert float(np.max(np.abs(out[round(1.0 * SR) - 400 : round(1.0 * SR)]))) < 0.5
    cut_mic = np.flatnonzero(np.abs(a[round(0.95 * SR) : round(1.2 * SR)]) > 0.1)
    assert track_marks_equal_at_cut(mic.marks, sysm.marks, 1.0)
    assert len(cut_mic) > 0


def track_marks_equal_at_cut(m1: list, m2: list, rt: float) -> bool:  # type: ignore[type-arg]
    """Ambas pistas tienen una marca exacta en el corte de la pausa (mismo reloj, mismo indice)."""
    a = [m for m in m1 if abs(m.clock_s - rt) < 1e-6]
    b = [m for m in m2 if abs(m.clock_s - rt) < 1e-6]
    return bool(a) and bool(b) and a[0].samples == b[0].samples == round(rt * SR)


def test_pause_keeps_tracks_aligned_on_the_recording_timeline(tmp_path: Path) -> None:
    """Un clic cada 0,5 s de reloj: tras una pausa caen en el mismo indice en ambas pistas."""
    clock = ManualClock()
    tl = Timeline(clock, t0_ns=0, pause_guard_s=0.3)
    mic, mw, _, _ = make_track(tmp_path, "mic", clock=clock, timeline=tl)
    sysm, sw, _, _ = make_track(tmp_path, "sys", clock=clock, timeline=tl, in_rate=48000)
    clicks = [0.5, 1.5, 2.55, 3.0, 3.5]  # 1.5 cae dentro de la pausa [1.0, 2.0)
    total = 4.0

    def signal(rate: int) -> np.ndarray:
        x = np.zeros(round(total * rate), dtype=np.float32)
        for c in clicks:
            x[round(c * rate)] = 0.8
        return x

    a16, a48 = signal(SR), signal(48000)
    events = sorted(
        [("mic", (k + 1) * 0.1) for k in range(40)]
        + [("sys", (k + 1) * 0.0625) for k in range(64)]
        + [("pause", 1.0), ("resume", 2.0)],
        key=lambda e: (e[1], e[0]),
    )
    cm = cs = 0
    for what, at in events:
        clock.set_s(at)
        if what == "pause":
            tl.pause()
        elif what == "resume":
            tl.resume()
        elif what == "mic":
            mic.feed(a16[cm * 1600 : (cm + 1) * 1600], s_to_ns(at))
            cm += 1
        else:
            sysm.feed(a48[cs * 3000 : (cs + 1) * 3000], s_to_ns(at))
            cs += 1
    mic.finish(s_to_ns(total))
    sysm.finish(s_to_ns(total))
    mw.close()
    sw.close()
    a, b = read_wav(tmp_path / "mic.wav"), read_wav(tmp_path / "sys.wav")
    # reloj comun -> tiempo de grabacion: tras la pausa (efectiva 1.0..2.3) se restan 1.3 s
    expected_rt = [0.5, 2.55 - 1.3, 3.0 - 1.3, 3.5 - 1.3]
    assert len(a) == len(b)
    for rt in expected_rt:
        i = round(rt * SR)
        pa = int(np.argmax(np.abs(a[i - 80 : i + 80]))) + i - 80
        pb = int(np.argmax(np.abs(b[i - 80 : i + 80]))) + i - 80
        assert abs(pa - i) <= 3 and abs(pb - i) <= 3, (rt, pa, pb, i)
        assert abs(pa - pb) <= 3  # < 0.2 ms entre pistas


def test_stop_while_paused_pads_to_the_cut(tmp_path: Path) -> None:
    clock = ManualClock()
    tl = Timeline(clock, t0_ns=0)
    track, writer, _, _ = make_track(tmp_path, clock=clock, timeline=tl)
    for k in range(10):
        clock.set_s((k + 1) * 0.1)
        track.feed(sine(300, 0.1), clock())
    clock.set_s(1.5)
    tl.pause()
    track.idle(clock())
    clock.set_s(3.0)
    tl.close(clock())
    track.finish(clock())
    writer.close()
    assert len(read_wav(tmp_path / "t.wav")) == round(1.5 * SR)


def test_levels_decay_to_zero_when_stale(tmp_path: Path) -> None:
    track, _, _, clock = make_track(tmp_path)
    clock.set_s(0.1)
    track.feed(sine(300, 0.1, amp=0.5), clock())
    rms, peak = track.levels(clock())
    assert rms == pytest.approx(0.5 / np.sqrt(2), abs=0.02) and peak == pytest.approx(0.5, abs=0.01)
    assert track.levels(s_to_ns(1.0)) == (0.0, 0.0)
