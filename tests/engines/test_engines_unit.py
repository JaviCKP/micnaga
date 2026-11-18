"""Motores ASR y diarizador con modelos/librerias falsos (rapidos, sin descargas)."""

from __future__ import annotations

import itertools
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from micnaga.config import Config
from micnaga.pipeline.engines.asr import _common
from micnaga.pipeline.engines.asr.parakeet import ParakeetEngine, words_from_tokens
from micnaga.pipeline.engines.asr.whisper import WhisperEngine
from micnaga.pipeline.engines.diar import SpeakerEmbedder
from micnaga.pipeline.engines.diar import diarize as diar_mod
from micnaga.pipeline.engines.diar.diarize import DiarizeDiarizer, turns_from_segments
from micnaga.types import Turn
from tests.synth import SR, noise

# --------------------------------------------------------------------------- Parakeet


def test_words_from_tokens_groups_subwords_and_punctuation() -> None:
    toks = [" Bu", "en", "os", " d", "ías", " a", " todos", ".", " ¿", "Me", " o", "ís", "?"]
    ts = [0.0, 0.24, 0.4, 0.48, 0.64, 0.8, 1.04, 1.52, 1.9, 2.0, 2.4, 2.5, 2.8]
    lp = [-0.0] * len(toks)
    words = words_from_tokens(toks, ts, lp)
    assert [w.w for w in words] == ["Buenos", "días", "a", "todos.", "¿Me", "oís?"]
    assert words[0].s == 0.0 and words[1].s == 0.48
    assert words[4].s == pytest.approx(1.9)  # el «¿» adelanta el inicio de la palabra
    assert all(w.e >= w.s for w in words)
    assert all(a.s <= b.s for a, b in itertools.pairwise(words))
    assert all(a.e <= b.s + 1e-9 for a, b in itertools.pairwise(words))


def test_words_from_tokens_standalone_punctuation_and_confidence() -> None:
    toks = [" hola", " ,", " mundo"]
    words = words_from_tokens(toks, [0.0, 0.5, 1.0], [np.log(0.9), np.log(0.5), np.log(0.8)])
    assert [w.w for w in words] == ["hola,", "mundo"]
    assert words[0].p == pytest.approx(np.sqrt(0.9 * 0.5))
    assert words[1].p == pytest.approx(0.8)
    no_lp = words_from_tokens(toks, [0.0, 0.5, 1.0], None)
    assert all(w.p == 1.0 for w in no_lp)


def test_words_from_tokens_offset_tail_cap_and_duration() -> None:
    words = words_from_tokens([" a", " b"], [0.0, 5.0], None, offset=10.0, duration=5.1)
    assert words[0].s == 10.0 and words[0].e == pytest.approx(10.32)  # tope de 0,32 s en pausa
    assert words[1].s == 15.0 and words[1].e <= 15.1 + 1e-9
    assert words_from_tokens([], [], None) == []
    assert words_from_tokens([" ", ""], [0.0, 1.0], None) == []


@dataclass
class FakeResult:
    text: str
    timestamps: list[float]
    tokens: list[str]
    logprobs: list[float] | None


class FakeOnnxModel:
    def __init__(self) -> None:
        self.lengths: list[int] = []

    def recognize(self, audio: Any, sample_rate: int = 16000) -> FakeResult:
        self.lengths.append(len(audio))
        return FakeResult("x", [0.0, 0.8], [" hola", " mundo"], [-0.1, -0.2])


def make_parakeet(model: Any) -> ParakeetEngine:
    eng = ParakeetEngine.__new__(ParakeetEngine)
    eng._model = model
    return eng


def test_parakeet_chunks_long_audio_and_offsets_times() -> None:
    fake = FakeOnnxModel()
    eng = make_parakeet(fake)
    audio = noise(100.0, amp=0.05)
    words = eng.transcribe(audio, SR)
    assert len(fake.lengths) >= 4
    assert max(fake.lengths) <= 30 * SR + 1
    assert sum(fake.lengths) == len(audio)
    starts = [w.s for w in words if w.w == "hola"]
    assert starts == sorted(starts) and starts[0] == 0.0 and starts[-1] > 60.0
    assert all(w.e <= 100.0 + 1e-6 for w in words)


def test_parakeet_short_audio_is_padded_and_resampled() -> None:
    fake = FakeOnnxModel()
    eng = make_parakeet(fake)
    assert eng.transcribe(noise(0.1), SR)
    assert fake.lengths[-1] == int(0.3 * SR)  # rellenado al minimo
    eng.transcribe(np.zeros(44100 * 2, dtype=np.float32), 44100)
    assert fake.lengths[-1] == 2 * SR  # remuestreado a 16 kHz
    assert eng.transcribe(np.zeros(0, dtype=np.float32), SR) == []
    eng.close()
    with pytest.raises(RuntimeError):
        eng.transcribe(noise(1.0), SR)


def test_split_points_and_to_mono_16k() -> None:
    a = noise(95.0, amp=0.1)
    a[int(28 * SR) : int(28.2 * SR)] = 0.0  # silencio dentro de la ventana de busqueda
    pts = _common.split_points(a)
    assert pts[0][0] == 0 and pts[-1][1] == len(a)
    assert all(b - s <= 30 * SR for s, b in pts)
    assert all(pts[i][1] == pts[i + 1][0] for i in range(len(pts) - 1))
    assert 27.9 * SR <= pts[0][1] <= 28.3 * SR  # corta en el silencio
    assert _common.split_points(noise(39.0)) == [(0, 39 * SR)]
    stereo = np.stack([noise(1.0), noise(1.0)], axis=1)
    assert _common.to_mono_16k(stereo, SR).shape == (SR,)
    with pytest.raises(ValueError):
        _common.to_mono_16k(np.zeros((2, 2, 2), dtype=np.float32), SR)


# ---------------------------------------------------------------------------- Whisper


class FakeWord:
    def __init__(self, word: str, start: float, end: float, p: float) -> None:
        self.word, self.start, self.end, self.probability = word, start, end, p


class FakeWhisperModel:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[list[Any], Any]:
        self.kwargs = kwargs
        seg1 = types.SimpleNamespace(
            words=[FakeWord(" Hola", 0.0, 0.4, 0.97), FakeWord(" ", 0.4, 0.5, 1)]
        )
        seg2 = types.SimpleNamespace(words=[FakeWord(" mundo.", 0.6, 1.0, 1.3)])
        seg3 = types.SimpleNamespace(words=None)
        return [seg1, seg2, seg3], None


def test_whisper_words_are_stripped_and_params_forced() -> None:
    eng = WhisperEngine.__new__(WhisperEngine)
    eng.name, eng.language = "faster-whisper-small-int8", "es"
    fake = FakeWhisperModel()
    eng._model = fake
    words = eng.transcribe(noise(2.0), SR)
    assert [(w.w, w.s, w.e) for w in words] == [("Hola", 0.0, 0.4), ("mundo.", 0.6, 1.0)]
    assert words[1].p == 1.0  # recortado a [0, 1]
    assert fake.kwargs["language"] == "es"
    assert fake.kwargs["word_timestamps"] is True
    assert fake.kwargs["vad_filter"] is False
    assert eng.transcribe(np.zeros(10, dtype=np.float32), SR) == []
    eng.close()
    with pytest.raises(RuntimeError):
        eng.transcribe(noise(1.0), SR)


# -------------------------------------------------------------------------- diarize


def test_turns_from_segments_normalizes_by_first_appearance() -> None:
    segs = [(5.0, 6.0, "SPEAKER_09"), (0.0, 1.0, "SPEAKER_03"), (2.0, 3.0, "SPEAKER_09")]
    turns = turns_from_segments(segs)
    assert turns == [Turn("S1", 0.0, 1.0), Turn("S2", 2.0, 3.0), Turn("S2", 5.0, 6.0)]
    assert turns_from_segments([]) == []


class FakeDiarizeLib(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("diarize")
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.seen_wav: Path | None = None
        self.speaker_factory_used = False

    def diarize(self, path: str, **kwargs: Any) -> Any:
        self.calls.append((path, kwargs))
        self.seen_wav = Path(path)
        assert self.seen_wav.is_file() and self.seen_wav.stat().st_size > 1000
        import wespeakerruntime  # el falso, parcheado por el adaptador

        wespeakerruntime.Speaker(lang="en")  # lo que hace diarize.embeddings
        segs = [
            types.SimpleNamespace(start=1.0, end=2.0, speaker="SPEAKER_07"),
            types.SimpleNamespace(start=0.0, end=0.9, speaker="SPEAKER_02"),
        ]
        return types.SimpleNamespace(segments=segs)


class FakeWespeaker(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("wespeakerruntime")
        self.created: list[dict[str, Any]] = []

        outer = self

        class Speaker:  # la "original"
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                outer.created.append(kwargs)

        self.Speaker = Speaker


@pytest.fixture
def fake_libs(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeDiarizeLib, FakeWespeaker]:
    lib, wes = FakeDiarizeLib(), FakeWespeaker()
    monkeypatch.setitem(sys.modules, "diarize", lib)
    monkeypatch.setitem(sys.modules, "wespeakerruntime", wes)
    return lib, wes


def test_diarize_adapter_writes_and_removes_temp_wav(
    fake_libs: tuple[FakeDiarizeLib, FakeWespeaker], tmp_path: Path
) -> None:
    lib, wes = fake_libs
    original = wes.Speaker
    d = DiarizeDiarizer(tmp_path / "w.onnx", threads=3, work_dir=tmp_path)
    assert isinstance(d, SpeakerEmbedder)
    turns = d.diarize(noise(3.0, amp=0.1), SR, n_speakers=2)
    assert turns == [Turn("S1", 0.0, 0.9), Turn("S2", 1.0, 2.0)]
    _path, kwargs = lib.calls[0]
    assert kwargs == {"num_speakers": 2}
    assert lib.seen_wav is not None and not lib.seen_wav.exists()  # borrado
    assert not list(tmp_path.glob("micnaga-diar-*"))
    # la fabrica local recibio la ruta ONNX y los hilos; el original se restauro
    assert wes.created == [{"onnx_path": str(tmp_path / "w.onnx"), "intra_op_num_threads": 3}]
    assert wes.Speaker is original


def test_diarize_adapter_auto_uses_capped_max_speakers(
    fake_libs: tuple[FakeDiarizeLib, FakeWespeaker], tmp_path: Path
) -> None:
    lib, _ = fake_libs
    d = DiarizeDiarizer(tmp_path / "w.onnx", work_dir=tmp_path, merge_threshold=None)
    d.diarize(noise(2.0, amp=0.1), SR, None)
    assert lib.calls[0][1] == {"min_speakers": 1, "max_speakers": diar_mod.DEFAULT_MAX_SPEAKERS}


def test_diarize_adapter_edge_cases(
    fake_libs: tuple[FakeDiarizeLib, FakeWespeaker], tmp_path: Path
) -> None:
    lib, _ = fake_libs
    d = DiarizeDiarizer(tmp_path / "w.onnx", work_dir=tmp_path)
    assert d.diarize(np.zeros(100, dtype=np.float32), SR, None) == []  # demasiado corto
    assert lib.calls == []
    with pytest.raises(ValueError):
        d.diarize(noise(2.0), SR, 0)
    d.close()
    with pytest.raises(RuntimeError):
        d.diarize(noise(2.0), SR, None)


def test_diarize_adapter_cleans_up_when_the_library_fails(
    fake_libs: tuple[FakeDiarizeLib, FakeWespeaker], tmp_path: Path
) -> None:
    lib, wes = fake_libs
    original = wes.Speaker

    def boom(path: str, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    lib.diarize = boom  # type: ignore[method-assign]
    d = DiarizeDiarizer(tmp_path / "w.onnx", work_dir=tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        d.diarize(noise(2.0, amp=0.1), SR, None)
    assert not list(tmp_path.glob("micnaga-diar-*"))
    assert wes.Speaker is original


def test_create_diarize_requires_wespeaker_model(tmp_models: Path) -> None:
    from micnaga.registry import EngineUnavailableError

    with pytest.raises(EngineUnavailableError, match="wespeaker"):
        diar_mod.create(Config())


def unit(*v: float) -> Any:
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def test_merge_similar_speakers_fuses_close_centroids_only() -> None:
    from micnaga.pipeline.engines.diar.diarize import merge_similar_speakers

    turns = [
        Turn("S1", 0, 20),
        Turn("S2", 20, 40),
        Turn("S3", 40, 60),
        Turn("S2", 60, 70),
        Turn("S4", 70, 90),
    ]
    emb = {
        "S1": unit(1, 0, 0),
        "S2": unit(0.95, 0.3, 0),  # parecido a S1
        "S3": unit(0, 1, 0),
        "S4": unit(0, 0.9, 0.2),  # parecido a S3
    }
    out = merge_similar_speakers(turns, emb, 0.65)
    assert [t.speaker for t in out] == ["S1", "S1", "S2", "S1", "S2"]
    assert [(t.start, t.end) for t in out] == [(t.start, t.end) for t in turns]
    # umbrales inalcanzables: nada cambia; umbral bajo: todos juntos
    same = merge_similar_speakers(turns, emb, 0.999, small_threshold=0.999)
    assert [t.speaker for t in same] == ["S1", "S2", "S3", "S2", "S4"]
    assert {t.speaker for t in merge_similar_speakers(turns, emb, -1.0)} == {"S1"}


def test_small_speakers_are_absorbed_with_a_looser_threshold() -> None:
    from micnaga.pipeline.engines.diar.diarize import merge_similar_speakers

    turns = [Turn("S1", 0, 60), Turn("S2", 60, 120), Turn("S3", 120, 125), Turn("S4", 125, 129)]
    emb = {
        "S1": unit(1, 0, 0),
        "S2": unit(0, 1, 0),
        "S3": unit(0, 0.6, 0.8),  # coseno 0,6 con S2: < 0,65 pero >= 0,5 y es pequeno
        "S4": unit(-1, 0, 0),  # opuesto a S1 y ortogonal al resto: se queda aparte
    }
    out = merge_similar_speakers(turns, emb, 0.65)
    assert [t.speaker for t in out] == ["S1", "S2", "S2", "S3"]
    # un hablante grande con el mismo parecido NO se absorbe
    big = [Turn("S1", 0, 60), Turn("S2", 60, 120), Turn("S3", 120, 180)]
    emb2 = {"S1": unit(1, 0, 0), "S2": unit(0, 1, 0), "S3": unit(0, 0.6, 0.8)}
    assert len({t.speaker for t in merge_similar_speakers(big, emb2, 0.65)}) == 3


def test_merge_assigns_speakers_without_embedding_to_the_nearest_turn() -> None:
    from micnaga.pipeline.engines.diar.diarize import merge_similar_speakers

    turns = [Turn("S1", 0, 50), Turn("S2", 50, 50.3), Turn("S3", 60, 110), Turn("S4", 52, 52.2)]
    emb = {"S1": unit(1, 0), "S3": unit(0, 1)}
    out = merge_similar_speakers(turns, emb, 0.65)
    by_time = {round(t.start): t.speaker for t in out}
    assert by_time[50] == by_time[0]  # S2 (50 s) esta pegado a S1
    assert by_time[52] == by_time[0]
    assert by_time[60] != by_time[0]


def test_diarize_auto_mode_merges_over_segmented_speakers(
    fake_libs: tuple[FakeDiarizeLib, FakeWespeaker],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = DiarizeDiarizer(tmp_path / "w.onnx", work_dir=tmp_path)
    monkeypatch.setattr(
        d, "embed", lambda audio, sr, turns: {"S1": unit(1, 0), "S2": unit(0.99, 0.1)}
    )
    auto = d.diarize(noise(3.0, amp=0.1), SR, None)
    assert {t.speaker for t in auto} == {"S1"}  # las dos etiquetas del falso se funden
    known = d.diarize(noise(3.0, amp=0.1), SR, 2)
    assert {t.speaker for t in known} == {"S1", "S2"}  # con n_speakers no se funde
