"""Procesado en vivo (``pipeline.live``): trozos, bloques estables, cola al colgar y cosido.

Con motores falsos (``bench.fakes.fake_live_deps``): el mismo VAD por probabilidades en la etapa
5 y en vivo, asi que lote y vivo solo difieren en la ganancia del acondicionado (acumulada en
vivo). La prueba con modelos reales esta en ``tests/e2e/test_e2e_live.py``.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from micnaga import holding, paths
from micnaga.bench.fakes import fake_live_deps, load_script
from micnaga.bench.synthetic import SyntheticSpec
from micnaga.capture.session import STATE_RECORDING
from micnaga.config import Config
from micnaga.pipeline import live
from micnaga.pipeline.live import LiveProcessor, engines_from_deps, stitch_words
from micnaga.types import CaptureMeta, SyncMark, Transcript, Word
from tests.pipeline.helpers import make_session, runner_for

SR = 16000
DURATION_S = 150.0


def _config() -> Config:
    cfg = Config()
    cfg.gates.min_duration_s = 20.0
    return cfg


def _session(name: str, duration_s: float = DURATION_S) -> Path:
    return make_session(name, SyntheticSpec(duration_s=duration_s, confirmed=True, seed=4))


def _as_recording(session: Path) -> None:
    """La sesion como mientras se graba: marcas de sincronia cada 10 s y estado ``recording``."""
    meta = CaptureMeta.from_json((session / paths.CAPTURE_JSON).read_text(encoding="utf-8"))
    n = meta.mic.samples
    marks = [SyncMark(k, k / SR) for k in range(0, n + 1, 10 * SR)]
    extra = {**meta.extra, "state": STATE_RECORDING}
    meta = replace(
        meta,
        mic=replace(meta.mic, sync=marks),
        system=replace(meta.system, sync=marks),
        extra=extra,
    )
    (session / paths.CAPTURE_JSON).write_text(meta.to_json(), encoding="utf-8")


def _finalized(session: Path) -> None:
    meta = CaptureMeta.from_json((session / paths.CAPTURE_JSON).read_text(encoding="utf-8"))
    meta = replace(meta, extra={**meta.extra, "state": "finalized"})
    (session / paths.CAPTURE_JSON).write_text(meta.to_json(), encoding="utf-8")


def _processor(session: Path, cfg: Config) -> LiveProcessor:
    engines = engines_from_deps(fake_live_deps(load_script(session)))
    assert engines is not None
    proc = LiveProcessor(session, cfg, engines)
    proc.ensure_manifest()
    return proc


def _run(session: Path, cfg: Config) -> Transcript:
    runner = runner_for(config=cfg, deps=fake_live_deps(load_script(session)), rename=False)
    runner.keep_work = True
    result = runner.run(session)
    assert result.status == "done", result.reason
    assert result.transcript is not None
    return result.transcript


def _words(t: Transcript) -> list[tuple[str, str]]:
    return [(s.speaker, w.w) for s in t.segments for w in s.words]


def _marker_info(session: Path, name: str) -> dict[str, Any]:
    from micnaga.pipeline.stages import get_stages

    stage = next(s for s in get_stages() if s.name == name)
    data = json.loads((paths.work_dir(session) / stage.marker_name).read_text(encoding="utf-8"))
    return dict(data["info"])


@pytest.fixture
def pair() -> tuple[Path, Path]:
    a = _session("live-a")
    b = a.parent / "live-b"
    shutil.copytree(a, b)
    return a, b


# ----------------------------------------------------------------------------- cosido


def w(text: str, s: float, e: float) -> Word:
    return Word(text, s, e, 0.9)


def test_stitch_keeps_every_word_of_disjoint_blocks_in_order() -> None:
    blocks = [
        (10.0, 12.0, [w("c", 10.1, 10.5), w("d", 11.0, 11.9)]),
        (0.0, 5.0, [w("a", 0.2, 0.6), w("b", 4.5, 5.0)]),
    ]
    assert [x.w for x in stitch_words(blocks)] == ["a", "b", "c", "d"]


def test_stitch_overlapping_blocks_never_duplicates_or_drops_a_word() -> None:
    # el mismo tramo [8, 10] transcrito dos veces (en vivo y rehecho con otra frontera)
    first = (0.0, 10.0, [w("uno", 1.0, 1.5), w("dos", 8.2, 8.6), w("tres", 9.4, 9.9)])
    second = (8.0, 15.0, [w("dos", 8.25, 8.6), w("tres", 9.45, 9.9), w("cuatro", 12.0, 12.5)])
    out = [x.w for x in stitch_words([first, second])]
    assert out == ["uno", "dos", "tres", "cuatro"]


def test_stitch_removes_exact_repeats_in_an_overlap_but_keeps_real_repeats() -> None:
    blocks = [
        (0.0, 5.2, [w("sí", 4.6, 4.9)]),
        (4.8, 9.0, [w("Sí,", 4.75, 5.0), w("sí", 6.0, 6.3)]),  # repetido en el solape + uno real
    ]
    out = stitch_words(blocks)
    assert [x.w for x in out] == ["sí", "sí"]
    assert [x.s for x in out] == [4.6, 6.0]


def test_stitch_keeps_quick_genuine_repeats_inside_a_block() -> None:
    blocks = [
        (0.0, 3.0, [w("no,", 1.0, 1.15), w("no", 1.16, 1.4), w("sí,", 2.0, 2.1)]),
        (3.0, 6.0, [w("sí", 3.1, 3.3), w("sí", 3.32, 3.5)]),
    ]
    assert [x.w for x in stitch_words(blocks)] == ["no,", "no", "sí,", "sí", "sí"]


# ----------------------------------------------------------------------------- VAD con estado


class _FakeSession:
    """Una red «recurrente» trivial: la salida depende del estado y de la entrada."""

    def run(self, _outputs: Any, feeds: dict[str, Any]) -> tuple[Any, Any]:
        x = feeds["input"]
        state = feeds["state"] * 0.9 + float(np.mean(np.abs(x)))
        prob = np.array([[float(np.tanh(state[0, 0, 0]))]], dtype=np.float32)
        return prob, state.astype(np.float32)


def test_silero_state_carried_across_chunks_equals_one_pass() -> None:
    from micnaga.pipeline.vad import SileroVad

    vad = SileroVad.__new__(SileroVad)
    vad._sess = _FakeSession()
    vad._sr = np.array(SR, dtype=np.int64)
    x = np.random.default_rng(1).standard_normal(512 * 40 + 100).astype(np.float32) * 0.1
    whole = vad.probs(x)
    p1, carry = vad.probs_from(x[: 512 * 15], None)
    p2, carry = vad.probs_from(x[512 * 15 : 512 * 31], carry)
    p3, _ = vad.probs_from(x[512 * 31 :], carry)
    np.testing.assert_array_equal(np.concatenate([p1, p2, p3]), whole)


# ----------------------------------------------------------------------------- alineacion


def test_aligned_range_matches_the_whole_track_alignment() -> None:
    from micnaga.capture.sync import clock_length, resample_range_to_clock, resample_to_clock

    rng = np.random.default_rng(0)
    x = rng.standard_normal(SR * 50).astype(np.float32) * 0.1
    # tarjeta 300 ppm lenta: el reloj avanza mas deprisa que las muestras
    marks = [SyncMark(k, k / SR * 1.0003) for k in range(0, len(x) + 1, 10 * SR)]
    whole = resample_to_clock(x, marks, SR)
    assert clock_length(marks, len(x), SR) == len(whole)
    for j0, j1 in ((0, SR * 7), (SR * 13, SR * 31), (len(whole) - SR * 3, len(whole) + 500)):
        part = resample_range_to_clock(lambda a, b: x[a:b], len(x), marks, j0, j1, SR)
        ref = np.zeros(j1 - j0, dtype=np.float32)
        ref[: max(0, min(len(whole), j1) - j0)] = whole[j0:j1]
        np.testing.assert_allclose(part, ref, atol=2e-4)


# ----------------------------------------------------------------------------- vivo -> final


def test_live_slices_then_finish_match_the_batch_transcript(pair: tuple[Path, Path]) -> None:
    session, batch = pair
    cfg = _config()
    _as_recording(session)
    proc = _processor(session, cfg)
    assert proc.step()  # dos trozos completos (0-60 s y 60-120 s); la cola espera
    proc.close()
    assert [s.index for s in proc.cached_slices()] == [0, 1]
    asr_cache = list((live.live_dir(session) / "asr").glob("*.json"))
    assert asr_cache, "los bloques estables se transcriben mientras se graba"
    _finalized(session)

    t_live = _run(session, cfg)
    t_batch = _run(batch, cfg)
    assert _words(t_live) == _words(t_batch)
    assert len(t_live.segments) == len(t_batch.segments)
    info = _marker_info(session, "asr")
    assert info["live"] is True
    align = _marker_info(session, "align")["live"]
    assert align["slices"] == 3 and align["slices_cached"] == 2
    assert align["blocks_cached"] >= len(asr_cache)
    # mismo formato de salida que el lote
    for name in (paths.TRANSCRIPT_JSON, paths.TRANSCRIPT_MD, paths.TRANSCRIPT_SRT):
        assert (session / name).is_file()


def test_no_word_is_lost_or_repeated_at_slice_boundaries(pair: tuple[Path, Path]) -> None:
    session, _ = pair
    cfg = _config()
    _as_recording(session)
    proc = _processor(session, cfg)
    proc.step()
    proc.close()
    _finalized(session)
    transcript = _run(session, cfg)
    script = load_script(session)
    expected = sum(len(str(u["text"]).split()) for u in script)
    words = [(round(wd.s, 2), wd.w) for s in transcript.segments for wd in s.words]
    assert len(words) == len(set(words))
    assert len(words) == expected


def test_crash_mid_slice_redoes_it_and_never_reads_half_written_files(
    pair: tuple[Path, Path],
) -> None:
    session, batch = pair
    cfg = _config()
    _as_recording(session)
    proc = _processor(session, cfg)
    proc.step()
    proc.close()
    folder = live.live_dir(session)
    # el proceso murio a mitad del trozo 1: sin slice.json y con un temporal huerfano
    (folder / "slice_00001" / live.SLICE_META).unlink()
    (folder / "slice_00001" / ".tmp-abc.wav").write_bytes(b"garbage")
    _finalized(session)
    t_live = _run(session, cfg)
    assert _marker_info(session, "align")["live"]["slices_cached"] == 1
    assert _words(t_live) == _words(_run(batch, cfg))


def test_trim_drops_live_content_past_the_end_of_the_meeting(pair: tuple[Path, Path]) -> None:
    session, batch = pair
    cfg = _config()
    _as_recording(session)
    proc = _processor(session, cfg)
    proc.step()  # trozos hasta 120 s
    proc.close()
    end_s = 100.0  # el micro se solto a los 100 s: lo de despues no es de la reunion
    for s in (session, batch):
        _trim(s, end_s)
    t_live = _run(session, cfg)
    assert all(wd.e <= end_s + 1e-6 for seg in t_live.segments for wd in seg.words)
    assert _marker_info(session, "align")["live"]["slices_cached"] == 1  # el 60-120 se rehizo
    assert t_live.meeting.duration_s == pytest.approx(end_s, abs=0.01)
    assert _words(t_live) == _words(_run(batch, cfg))


def _trim(session: Path, end_s: float) -> None:
    n = round(end_s * SR)
    meta = CaptureMeta.from_json((session / paths.CAPTURE_JSON).read_text(encoding="utf-8"))
    for name in (paths.AUDIO_ME_FLAC, paths.AUDIO_REMOTE_FLAC):
        data, sr = sf.read(str(session / name), dtype="int16")
        sf.write(str(session / name), data[:n], sr, subtype="PCM_16", format="FLAC")
    meta = replace(
        meta,
        mic=replace(meta.mic, samples=n, sync=[m for m in meta.mic.sync if m.samples <= n]),
        system=replace(
            meta.system, samples=n, sync=[m for m in meta.system.sync if m.samples <= n]
        ),
        extra={**meta.extra, "state": "finalized", "trim_applied": True},
    )
    (session / paths.CAPTURE_JSON).write_text(meta.to_json(), encoding="utf-8")


def test_changed_configuration_discards_the_live_cache_and_runs_in_batch(
    pair: tuple[Path, Path],
) -> None:
    session, batch = pair
    cfg = _config()
    _as_recording(session)
    proc = _processor(session, cfg)
    proc.step()
    proc.close()
    _finalized(session)
    cfg.vad.min_silence_ms = 400  # otra configuracion: lo calculado ya no vale
    t = _run(session, cfg)
    assert "live" not in _marker_info(session, "asr")
    assert _words(t) == _words(_run(batch, cfg))


def test_live_disabled_in_config_ignores_the_cache(pair: tuple[Path, Path]) -> None:
    session, _ = pair
    cfg = _config()
    _as_recording(session)
    proc = _processor(session, cfg)
    proc.step()
    proc.close()
    _finalized(session)
    cfg.processing.live = False
    _run(session, cfg)
    assert "live" not in _marker_info(session, "asr")
    assert not live.live_dir(session).exists()


def test_live_cache_key_includes_effective_whisper_options() -> None:
    cfg = Config()
    start = live.fingerprint(cfg)
    cfg.processing.glossary = ["Termino nuevo"]
    assert live.fingerprint(cfg) != start


def test_holding_a_session_deletes_its_live_transcription(pair: tuple[Path, Path]) -> None:
    session, _ = pair
    _as_recording(session)
    proc = _processor(session, _config())
    proc.step()
    proc.close()
    assert live.live_dir(session).is_dir()
    dest = holding.move_to_held(session, holding.REASON_DECLINED)
    assert not (paths.work_dir(dest) / "live").exists()
    assert (dest / paths.AUDIO_ME_FLAC).is_file() and (dest / paths.AUDIO_REMOTE_FLAC).is_file()


# ----------------------------------------------------------------------------- bucle del proceso


def test_run_live_stops_when_asked_and_yields_when_the_cpu_is_busy(
    pair: tuple[Path, Path],
) -> None:
    session, _ = pair
    _as_recording(session)
    engines = engines_from_deps(fake_live_deps(load_script(session)))
    assert engines is not None
    meta = CaptureMeta.from_json((session / paths.CAPTURE_JSON).read_text(encoding="utf-8"))
    import os

    import psutil

    meta = replace(
        meta,
        extra={
            **meta.extra,
            "pid": os.getpid(),
            "pid_create_time": psutil.Process().create_time(),
        },
    )
    (session / paths.CAPTURE_JSON).write_text(meta.to_json(), encoding="utf-8")
    sleeps: list[float] = []
    loads = iter([99.0, 10.0])  # primero ocupado (cede), despues libre

    def sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) >= 2:
            live.request_stop(session)

    reason = live.run_live(
        session, _config(), engines, cpu_percent=lambda: next(loads, 10.0), sleep=sleep
    )
    assert reason == "stop requested"
    assert sleeps[0] == live.BUSY_WAIT_S
    assert [s.index for s in LiveProcessor(session, _config(), engines).cached_slices()] == [0, 1]


def _owned_recording(session: Path) -> None:
    import os

    import psutil

    _as_recording(session)
    meta = CaptureMeta.from_json((session / paths.CAPTURE_JSON).read_text(encoding="utf-8"))
    meta = replace(
        meta,
        extra={
            **meta.extra,
            "pid": os.getpid(),
            "pid_create_time": psutil.Process().create_time(),
        },
    )
    (session / paths.CAPTURE_JSON).write_text(meta.to_json(), encoding="utf-8")


def test_final_handoff_saves_one_asr_load_with_identical_words(pair: tuple[Path, Path]) -> None:
    cfg = _config()

    def finish_recording(session: Path, *, warm: bool) -> Transcript:
        _owned_recording(session)
        deps = fake_live_deps(load_script(session))
        create_asr = deps.create_asr
        loads = 0

        def counted(config: Config) -> Any:
            nonlocal loads
            loads += 1
            return create_asr(config)

        deps.create_asr = counted
        engines = engines_from_deps(deps)
        assert engines is not None

        def end_capture(_seconds: float) -> None:
            _finalized(session)
            if warm:
                folder = live.live_dir(session)
                (folder / live.FINISH_REQUEST).touch()
                live.atomic_write_json(folder / live.FINISH_READY, live.fingerprint(cfg))
            else:
                live.request_stop(session)

        reason = live.run_live(session, cfg, engines, cpu_percent=lambda: 0.0, sleep=end_capture)
        assert reason == ("warm ASR handoff completed" if warm else "stop requested")
        assert loads == 1
        runner = runner_for(config=cfg, deps=deps, rename=False)
        runner.keep_work = True
        result = runner.run(session)
        assert result.transcript is not None
        assert loads == (1 if warm else 2)
        stats = _marker_info(session, "align")["live"]
        assert stats["slices_cached"] == (3 if warm else 2)
        assert (stats["blocks_done"] == 0) is warm
        return result.transcript

    warm, cold = pair
    assert finish_recording(warm, warm=True).segments == finish_recording(cold, warm=False).segments


def test_run_live_still_makes_progress_when_the_cpu_never_frees_up(
    pair: tuple[Path, Path],
) -> None:
    """Con la CPU siempre ocupada por otros no se espera sin fin: tras ``MAX_BUSY_WAIT_S`` se
    hace un paso (el vivo va con prioridad baja) y la cola al colgar queda corta."""
    session, _ = pair
    _owned_recording(session)
    engines = engines_from_deps(fake_live_deps(load_script(session)))
    assert engines is not None
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        if sum(sleeps) > 3 * live.MAX_BUSY_WAIT_S:
            live.request_stop(session)

    live.run_live(session, _config(), engines, cpu_percent=lambda: 99.0, sleep=sleep)
    assert [s.index for s in LiveProcessor(session, _config(), engines).cached_slices()] == [0, 1]
    busy = live.MAX_BUSY_WAIT_S / live.BUSY_WAIT_S
    assert sleeps[: int(busy)] == [live.BUSY_WAIT_S] * int(busy)


def test_cpu_load_does_not_count_the_live_process_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Justo despues de un paso de ASR el equipo va al 60 %, pero 50 de esos puntos son del
    propio vivo (4 nucleos de 8): para decidir si ceder solo cuenta el 10 % ajeno."""
    import psutil

    class Me:
        def cpu_percent(self, interval: float | None = None) -> float:
            return 400.0

    monkeypatch.setattr(psutil, "Process", lambda: Me())
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 8)
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 60.0)
    assert live.other_cpu_percent()() == pytest.approx(10.0)


@pytest.mark.parametrize("why", ["stopped", "discarded", "gone"])
def test_run_live_exits_when_recording_ends_or_is_discarded(
    pair: tuple[Path, Path], why: str
) -> None:
    session, _ = pair
    _as_recording(session)
    if why == "stopped":
        _finalized(session)
    elif why == "discarded":
        holding.mark_discarded(session, holding.REASON_DECLINED)
    # "gone": capture.json tiene el pid de otro proceso (la app murio)
    engines = engines_from_deps(fake_live_deps(load_script(session)))
    assert engines is not None
    reason = live.run_live(session, _config(), engines, cpu_percent=lambda: 0.0)
    assert (
        reason
        in {
            "stopped": "recording stopped",
            "discarded": "discarded",
            "gone": "recorder is gone",
        }[why]
    )


# ----------------------------------------------------------------------------- revision externa


class _DeafVad:
    """VAD en vivo que no oye nada (como leer audio de 8 kHz como si fuera de 16 kHz)."""

    def probs(self, audio: Any) -> Any:
        return self.probs_from(audio, None)[0]

    def probs_from(self, audio: Any, carry: Any) -> tuple[Any, Any]:
        from micnaga.pipeline.vad import VadCarry

        n = -(-len(audio) // 512)
        out = carry or VadCarry(np.zeros((2, 1, 128), np.float32), np.zeros(64, np.float32))
        return np.full(n, 0.05, dtype=np.float32), out


def test_live_with_zero_words_falls_back_to_batch(pair: tuple[Path, Path]) -> None:
    session, batch = pair
    cfg = _config()
    _as_recording(session)
    deps = fake_live_deps(load_script(session))
    deps.live_vad = lambda _c: _DeafVad()
    engines = engines_from_deps(deps)
    assert engines is not None
    proc = LiveProcessor(session, cfg, engines)
    proc.ensure_manifest()
    proc.step()
    proc.close()
    _finalized(session)
    runner = runner_for(config=cfg, deps=deps, rename=False)
    runner.keep_work = True
    result = runner.run(session)
    assert result.status == "done" and result.transcript is not None
    assert "live" not in _marker_info(session, "asr"), "un vivo sin palabras no da por hecha"
    assert result.transcript.segments
    assert _words(result.transcript) == _words(_run(batch, cfg))


def test_live_is_disabled_for_sample_rates_other_than_16k(pair: tuple[Path, Path]) -> None:
    from micnaga.pipeline.stages import get_stages
    from micnaga.pipeline.stages.base import StageContext

    session, _ = pair
    cfg = _config()
    cfg.capture.sample_rate = 8000
    assert not live.live_supported(cfg)
    _as_recording(session)
    engines = engines_from_deps(fake_live_deps(load_script(session)))
    assert engines is not None
    reason = live.run_live(session, cfg, engines, cpu_percent=lambda: 0.0)
    assert reason == "unsupported sample rate"
    assert not any(live.live_dir(session).glob("slice_*"))
    # una cache hecha a 16 kHz no se usa si la grabacion es de 8 kHz: se procesa por lotes
    proc = _processor(session, _config())
    proc.step()
    proc.close()
    meta = CaptureMeta.from_json((session / paths.CAPTURE_JSON).read_text(encoding="utf-8"))
    meta8 = replace(meta, mic=replace(meta.mic, sample_rate=8000))
    assert not live.live_supported(_config(), meta8)
    deps = fake_live_deps(load_script(session))
    ctx = StageContext(session, _config(), deps, meta8, lambda _f: None)
    assert live.finish(ctx, get_stages()[1:6], engines) is False
    assert not live.has_cache(session)
