from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from micnaga.config import Config
from micnaga.pipeline.aec import AEC_ENGINES, AUTO_ORDER, create_echo_canceller
from micnaga.pipeline.aec.base import EchoCanceller
from micnaga.pipeline.engines.asr import ASR_ENGINES, create_asr
from micnaga.pipeline.engines.asr.base import AsrEngine
from micnaga.pipeline.engines.diar import DIARIZERS, create_diarizer
from micnaga.pipeline.engines.diar.base import Diarizer, SpeakerEmbedder
from micnaga.registry import EngineUnavailableError, LazyRegistry, UnknownEngineError
from micnaga.types import AudioArray, Turn, Word


def test_names_match_spec() -> None:
    assert set(ASR_ENGINES.names()) == {"parakeet", "whisper"}
    assert set(DIARIZERS.names()) == {"onnx", "diarize", "pyannote"}
    assert set(AEC_ENGINES.names()) == {"webrtc", "speex"}


def test_targets_follow_the_convention() -> None:
    assert ASR_ENGINES.target("parakeet") == "micnaga.pipeline.engines.asr.parakeet:create"
    assert ASR_ENGINES.target("whisper") == "micnaga.pipeline.engines.asr.whisper:create"
    assert DIARIZERS.target("onnx") == "micnaga.pipeline.engines.diar.onnx:create"
    assert DIARIZERS.target("diarize") == "micnaga.pipeline.engines.diar.diarize:create"
    assert DIARIZERS.target("pyannote") == "micnaga.pipeline.engines.diar.pyannote:create"
    assert AEC_ENGINES.target("webrtc") == "micnaga.pipeline.aec.webrtc:create"
    assert AEC_ENGINES.target("speex") == "micnaga.pipeline.aec.speex:create"


def test_unknown_name() -> None:
    with pytest.raises(UnknownEngineError):
        ASR_ENGINES.create("nope", Config())
    cfg = Config()
    cfg.processing.asr_engine = "nope"  # sin validar a proposito
    with pytest.raises(UnknownEngineError):
        create_asr(cfg)


def test_unimplemented_engine_is_unavailable_not_import_error() -> None:
    """Mientras un motor no exista, el error es EngineUnavailableError con la causa original."""
    reg: LazyRegistry[AsrEngine] = LazyRegistry(
        "ASR", {"x": "micnaga.pipeline.engines.asr.does_not_exist:create"}
    )
    with pytest.raises(EngineUnavailableError) as info:
        reg.create("x", Config())
    assert isinstance(info.value.__cause__, ImportError)


def test_lazy_import_happens_only_on_create(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = types.ModuleType("fake_engine_mod")
    calls: list[str] = []

    class Fake:
        name = "fake"

        def transcribe(self, audio: AudioArray, sr: int) -> list[Word]:
            return [Word("hola", 0.0, 0.5, 0.9)]

        def close(self) -> None:
            calls.append("closed")

    def create(config: Config) -> AsrEngine:
        calls.append("created")
        return Fake()

    mod.create = create  # type: ignore[attr-defined]
    reg: LazyRegistry[AsrEngine] = LazyRegistry("ASR", {"fake": "fake_engine_mod:create"})
    assert "fake_engine_mod" not in sys.modules
    monkeypatch.setitem(sys.modules, "fake_engine_mod", mod)
    engine = reg.create("fake", Config())
    assert isinstance(engine, AsrEngine)
    assert engine.transcribe(np.zeros(10, dtype=np.float32), 16000)[0].w == "hola"
    engine.close()
    assert calls == ["created", "closed"]


def test_missing_factory_attribute_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "empty_mod", types.ModuleType("empty_mod"))
    reg: LazyRegistry[AsrEngine] = LazyRegistry("ASR", {"e": "empty_mod:create"})
    with pytest.raises(EngineUnavailableError):
        reg.create("e", Config())


def test_diarizer_and_embedder_protocols() -> None:
    class D:
        name = "d"

        def diarize(self, audio: AudioArray, sr: int, n_speakers: int | None) -> list[Turn]:
            return []

        def close(self) -> None: ...

    class DE(D):
        def embed(self, audio: AudioArray, sr: int, turns: list[Turn]) -> dict[str, AudioArray]:
            return {}

    assert isinstance(D(), Diarizer)
    assert not isinstance(D(), SpeakerEmbedder)
    assert isinstance(DE(), SpeakerEmbedder)


def test_echo_canceller_protocol_and_off() -> None:
    class Passthrough:
        name = "pass"

        def process(self, mic: AudioArray, ref: AudioArray, sr: int) -> AudioArray:
            return mic

    assert isinstance(Passthrough(), EchoCanceller)
    assert create_echo_canceller(Config(), "off") is None
    cfg = Config()
    cfg.processing.aec = "off"
    assert create_echo_canceller(cfg) is None


def test_auto_picks_first_available(monkeypatch: pytest.MonkeyPatch) -> None:
    class Speexish:
        name = "speex"

        def process(self, mic: AudioArray, ref: AudioArray, sr: int) -> AudioArray:
            return mic

    mod = types.ModuleType("fake_speex")
    mod.create = lambda config: Speexish()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_speex", mod)
    monkeypatch.setattr(
        AEC_ENGINES, "_entries", {"webrtc": "nonexistent.mod:create", "speex": "fake_speex:create"}
    )
    assert AUTO_ORDER == ("speex", "webrtc")
    engine = create_echo_canceller(Config(), "auto")
    assert engine is not None
    assert engine.name == "speex"


def test_auto_with_nothing_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AEC_ENGINES, "_entries", {"webrtc": "no.a:create", "speex": "no.b:create"})
    with pytest.raises(EngineUnavailableError):
        create_echo_canceller(Config(), "auto")


def test_create_diarizer_unknown() -> None:
    with pytest.raises(UnknownEngineError):
        create_diarizer(Config(), "x")
