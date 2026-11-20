"""«Mejorar transcripcion» (``micnaga.pipeline.hq``) con un motor falso; uno lento con el real."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from micnaga import paths
from micnaga.config import Config
from micnaga.pipeline import hq
from micnaga.pipeline.stages.base import write_audio_file
from micnaga.types import (
    EnginesInfo,
    Marker,
    MeetingInfo,
    Segment,
    Speaker,
    Transcript,
    Word,
)

SR = 16000


def _seg(speaker: str, words: list[tuple[str, float, float]]) -> Segment:
    ws = [Word(w, s, e, 0.9) for w, s, e in words]
    return Segment(ws[0].s, ws[-1].e, speaker, " ".join(w.w for w in ws), ws)


def _transcript() -> Transcript:
    return Transcript(
        meeting=MeetingInfo(title="Prueba", start="2026-11-19T10:00:00+01:00", duration_s=20.0),
        speakers=[Speaker("me", "Yo"), Speaker("S1", "Ana"), Speaker("S2", "Luis")],
        segments=[
            _seg("me", [("hola", 0.5, 1.0), ("equipo", 1.1, 1.6)]),
            _seg("S1", [("buenas", 5.0, 5.5), ("tardes", 5.6, 6.2)]),
            _seg("S2", [("yo", 10.0, 10.4), ("tambien", 10.5, 11.0)]),
        ],
        markers=[Marker(7.0, "")],
        engines=EnginesInfo(asr="parakeet", diarization="onnx"),
    )


class FakeEngine:
    """Devuelve palabras fijas por bloque (tiempos relativos al bloque) y cuenta llamadas."""

    name = "fake-large"

    def __init__(self, by_len: dict[int, list[Word]], on_call: Any = None) -> None:
        self.by_len = by_len
        self.calls = 0
        self.closed = False
        self.on_call = on_call

    def transcribe(self, audio: Any, sr: int) -> list[Word]:
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        return list(self.by_len.get(round(len(audio) / sr), []))

    def close(self) -> None:
        self.closed = True


def _meeting(tmp_path: Path) -> Path:
    folder = tmp_path / "2026-11-19_1000_prueba"
    work = paths.work_dir(folder)
    work.mkdir(parents=True)
    from micnaga.pipeline import output

    output.write_transcript(folder, _transcript())
    silence = np.zeros(SR * 20, dtype=np.float32)
    write_audio_file(work / "04_mic.wav", silence, SR)
    write_audio_file(work / "04_system.wav", silence, SR)
    vad = {
        "mic": {"regions": [[0.0, 2.0]], "chunks": [[0.0, 2.0]]},
        "system": {"regions": [[4.0, 7.0], [9.0, 13.0]], "chunks": [[4.0, 7.0], [9.0, 13.0]]},
    }
    (work / "05_vad.json").write_text(json.dumps(vad), encoding="utf-8")
    return folder


def _engine() -> FakeEngine:
    return FakeEngine(
        {
            2: [Word("Hola,", 0.5, 1.0), Word("equipo.", 1.1, 1.6)],  # micro, bloque 0-2
            3: [Word("Buenas", 1.0, 1.5), Word("tardes.", 1.6, 2.2)],  # sistema, 4-7
            4: [Word("Yo", 1.0, 1.4), Word("también.", 1.5, 2.0)],  # sistema, 9-13
        }
    )


@pytest.fixture
def no_vad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hq, "_ensure_vad", lambda *_a, **_k: False)


def test_drop_loops_collapses_repeated_ngrams() -> None:
    ws = [
        Word(t, i, i + 0.5)
        for i, t in enumerate(["a", "b", "c", "a", "b", "c", "a", "b", "c", "d"])
    ]
    assert [w.w for w in hq.drop_loops(ws)] == ["a", "b", "c", "d"]
    once = [Word(t, i, i + 0.5) for i, t in enumerate(["si", "si", "vale"])]
    assert [w.w for w in hq.drop_loops(once)] == ["si", "si", "vale"]  # 2 veces no es bucle
    loop = [Word("gracias.", i, i + 0.5) for i in range(6)]
    assert [w.w for w in hq.drop_loops(loop)] == ["gracias."]


def test_assign_words_by_overlap_and_nearest() -> None:
    tr = _transcript()
    out = hq.assign_words(
        tr,
        [Word("x", 0.6, 0.9)],
        [Word("a", 5.1, 5.4), Word("b", 10.1, 10.3), Word("c", 12.0, 12.3), Word("d", 60, 61)],
    )
    assert [sp for sp, _w in out] == ["me", "S1", "S2", "S2", "S2"]


def test_improve_keeps_speakers_markers_and_saves_previa(tmp_path: Path, no_vad: None) -> None:
    folder = _meeting(tmp_path)
    before = (folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8")
    (folder / hq.CLEAN_MARKER).write_text("{}", encoding="utf-8")
    eng = _engine()
    phases: list[str] = []
    res = hq.improve(folder, Config(), lambda: eng, progress=lambda f, _p: phases.append(f))
    assert eng.closed and eng.calls == 3
    assert res.engine == "fake-large" and res.words_after == 6
    new = Transcript.from_json((folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8"))
    assert [s.speaker for s in new.segments] == ["me", "S1", "S2"]
    assert new.segments[2].text == "Yo también."
    assert new.segments[1].start == pytest.approx(5.0)
    assert {s.id: s.name for s in new.speakers} == {"me": "Yo", "S1": "Ana", "S2": "Luis"}
    assert new.markers == [Marker(7.0, "")]
    assert new.engines.asr == "fake-large"
    assert (folder / hq.PREVIA).read_text(encoding="utf-8") == before
    assert not (folder / hq.CLEAN_MARKER).exists()  # la limpieza con IA debe volver a pasar
    assert (folder / hq.CLEAN_PREVIA).is_file()
    assert "también" in (folder / paths.TRANSCRIPT_MD).read_text(encoding="utf-8")
    assert "transcribe" in phases


def test_restore_brings_back_previa(tmp_path: Path, no_vad: None) -> None:
    folder = _meeting(tmp_path)
    before = Transcript.from_json((folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8"))
    (folder / hq.CLEAN_MARKER).write_text('{"v": 1}', encoding="utf-8")
    hq.improve(folder, Config(), _engine)
    assert hq.has_previa(folder)
    assert hq.restore(folder)
    after = Transcript.from_json((folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8"))
    assert after.to_dict() == before.to_dict()
    assert not hq.has_previa(folder)
    assert (folder / hq.CLEAN_MARKER).read_text(encoding="utf-8") == '{"v": 1}'
    assert not hq.restore(folder)


def test_cancel_leaves_transcript_untouched(tmp_path: Path, no_vad: None) -> None:
    folder = _meeting(tmp_path)
    before = (folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8")
    eng = _engine()
    eng.on_call = lambda n: hq.request_cancel(folder) if n == 1 else None
    with pytest.raises(hq.Cancelled):
        hq.improve(folder, Config(), lambda: eng)
    assert eng.calls == 1 and eng.closed
    assert (folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8") == before
    assert not hq.has_previa(folder)
    assert not (folder / hq.CANCEL).exists()


# ----------------------------------------------------------------------------- revision externa


def _read(folder: Path, name: str = paths.TRANSCRIPT_JSON) -> Transcript:
    return Transcript.from_json((folder / name).read_text(encoding="utf-8"))


def test_empty_result_is_rejected_and_never_published(tmp_path: Path, no_vad: None) -> None:
    folder = _meeting(tmp_path)
    before = (folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8")
    with pytest.raises(hq.HqRejected):
        hq.improve(folder, Config(), lambda: FakeEngine({}))  # seis palabras -> cero
    assert (folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8") == before
    assert not hq.has_previa(folder)


def test_drastically_shorter_result_is_rejected(tmp_path: Path, no_vad: None) -> None:
    folder = _meeting(tmp_path)
    before = (folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8")
    eng = FakeEngine({2: [Word("Hola.", 0.5, 1.0)]})  # 6 -> 1 palabra (< 30 %)
    with pytest.raises(hq.HqRejected):
        hq.improve(folder, Config(), lambda: eng)
    assert (folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8") == before


def test_rejection_is_reported_to_the_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = _meeting(tmp_path)
    monkeypatch.setattr(hq, "download_bytes", lambda _c: 0)
    monkeypatch.setattr(hq, "_ensure_vad", lambda *_a, **_k: False)
    monkeypatch.setattr(hq, "create_engine", lambda _c: FakeEngine({}))
    assert hq._run_locked(folder, Config()) == 1
    st = hq.read_status(folder)
    assert st is not None and st["state"] == "failed" and st["error"] == "HqRejected"


def test_an_empty_transcript_never_replaces_a_good_previa(tmp_path: Path, no_vad: None) -> None:
    folder = _meeting(tmp_path)
    good = (folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8")
    hq.improve(folder, Config(), _engine)
    assert (folder / hq.PREVIA).read_text(encoding="utf-8") == good
    # la transcripcion actual se quedo vacia (p. ej. por una version anterior de la mejora)
    from micnaga.pipeline import output

    output.write_transcript(folder, replace(_read(folder), segments=[]))
    hq.improve(folder, Config(), _engine)
    assert _read(folder, hq.PREVIA).segments, "la previa buena no se pisa con una vacia"
    assert (folder / hq.PREVIA).read_text(encoding="utf-8") == good
    assert hq.restore(folder)
    assert len(_read(folder).segments) == 3


def test_rename_made_while_improving_is_kept(tmp_path: Path, no_vad: None) -> None:
    from micnaga.pipeline import output

    folder = _meeting(tmp_path)

    def rename_midway(n: int) -> None:
        if n == 2:  # el usuario renombra a S1 mientras la mejora esta en marcha
            output.rename_speaker(folder, "S1", "Ana María", reindex=False)

    eng = _engine()
    eng.on_call = rename_midway
    hq.improve(folder, Config(), lambda: eng)
    new = _read(folder)
    assert {s.id: s.name for s in new.speakers}["S1"] == "Ana María"
    assert [s.speaker for s in new.segments] == ["me", "S1", "S2"]
    assert {s.id: s.name for s in _read(folder, hq.PREVIA).speakers}["S1"] == "Ana María"


def test_status_and_estimate(tmp_path: Path) -> None:
    folder = _meeting(tmp_path)
    assert hq.read_status(folder) is None
    hq.write_status(folder, state="running", fase="transcribe", pct=0.5)
    assert hq.read_status(folder)["fase"] == "transcribe"  # type: ignore[index]
    assert hq.speech_seconds(folder) == pytest.approx(9.0)
    assert hq.estimate_minutes(folder, "large-v3") == 1


def test_hq_models_in_manifest() -> None:
    from micnaga import models

    for group in hq.MODELS.values():
        g = models.MANIFEST[group]
        assert {f.name for f in g.files} >= {"model.bin", "config.json", "tokenizer.json"}
        assert all(len(f.sha256) == 64 and f.size > 0 for f in g.files)
        assert "/resolve/main/" not in g.files[0].url
    assert hq.model_group(Config()) == "faster-whisper-large-v3"


@pytest.mark.slow
@pytest.mark.models
@pytest.mark.timeout(600)
def test_real_model_short_clip(tmp_path: Path) -> None:
    """Motor real sobre un clip sintetico corto: solo si el modelo ya esta descargado."""
    if os.environ.get("MICNAGA_RUN_MODELS", "") in ("", "0"):
        pytest.skip("MICNAGA_RUN_MODELS=1 required")
    cfg = Config()
    if hq.download_bytes(cfg):
        pytest.skip("hq model not downloaded")
    from tests.synth import speech_bursts

    eng = hq.create_engine(cfg)
    try:
        audio = speech_bursts([(0.5, 3.0), (4.0, 7.0)], 8.0, SR)
        words = eng.transcribe(np.asarray(audio, dtype=np.float32), SR)
    finally:
        eng.close()
    assert isinstance(words, list)
    assert all(w.e >= w.s for w in words)


def test_restore_refuses_an_empty_previa(tmp_path: Path, no_vad: None) -> None:
    folder = _meeting(tmp_path)
    good = (folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8")
    empty = replace(_read(folder), segments=[])
    (folder / hq.PREVIA).write_text(empty.to_json(), encoding="utf-8")
    assert not hq.restore(folder)
    assert (folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8") == good
