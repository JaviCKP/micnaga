"""Whisper como motor por defecto (DECISIONS §32): idioma forzado, glosario y guardas, con un
modelo falso (sin faster-whisper real ni descargas)."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from micnaga import models, paths
from micnaga.config import Config
from micnaga.pipeline.engines.asr import whisper as whisper_mod
from micnaga.pipeline.engines.asr.whisper import (
    WhisperEngine,
    asr_language,
    engine_options,
    glossary_prompt,
)
from tests.synth import SR, noise


class W:
    def __init__(self, word: str, start: float, end: float, p: float = 0.9) -> None:
        self.word, self.start, self.end, self.probability = word, start, end, p


def seg(text: str, t0: float = 0.0, **kw: float) -> Any:
    words = [W(" " + w, t0 + 0.3 * i, t0 + 0.3 * i + 0.25) for i, w in enumerate(text.split())]
    return types.SimpleNamespace(text=" " + text, words=words, **kw)


class FakeModel:
    def __init__(self, segments: list[Any]) -> None:
        self.segments = segments
        self.kwargs: dict[str, Any] = {}

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[list[Any], Any]:
        self.kwargs = kwargs
        return self.segments, None


def engine(
    segments: list[Any], *, guards: bool = True, hotwords: str | None = None
) -> tuple[WhisperEngine, FakeModel]:
    eng = WhisperEngine.__new__(WhisperEngine)
    eng.name, eng.language, eng.guards = "faster-whisper-large-v3-turbo-int8", "es", guards
    eng.hotwords = hotwords
    eng._prompt_terms = frozenset(whisper_mod._norm_tokens(hotwords or ""))
    fake = FakeModel(segments)
    eng._model = fake
    return eng, fake


def text(words: list[Any]) -> str:
    return " ".join(w.w for w in words)


def test_language_and_hotwords_are_passed_on_every_call() -> None:
    eng, fake = engine([seg("hola Claude")], hotwords="Glosario: Claude, PQC.")
    assert text(eng.transcribe(noise(2.0), SR)) == "hola Claude"
    assert fake.kwargs["language"] == "es"
    assert fake.kwargs["hotwords"] == "Glosario: Claude, PQC."
    assert "initial_prompt" not in fake.kwargs  # solo llegaria a la primera pasada del bloque
    assert fake.kwargs["condition_on_previous_text"] is False
    assert fake.kwargs["beam_size"] == 5
    assert fake.kwargs["no_speech_threshold"] == pytest.approx(0.6)  # guardas de «Mejorar»


def test_known_silence_hallucinations_are_dropped() -> None:
    eng, _ = engine(
        [
            seg("y Fernando me ha encargado"),
            seg("¡Gracias por ver el video!", 2.0),
            seg("Subtítulos realizados por la comunidad de Amara.org", 4.0),
            seg("como para guiarme", 8.0),
        ]
    )
    assert text(eng.transcribe(noise(10.0), SR)) == "y Fernando me ha encargado como para guiarme"


def test_a_real_sentence_with_gracias_is_kept() -> None:
    eng, _ = engine([seg("vale, gracias por ver el informe")])
    assert text(eng.transcribe(noise(2.0), SR)) == "vale, gracias por ver el informe"


def test_segment_that_only_echoes_the_glossary_is_dropped() -> None:
    eng, _ = engine(
        [
            seg("Glosario: Claude, PQC."),
            seg("Claude, PQC, Shodan."),
            seg("San Francisco."),
            seg("lo hace Claude", 2.0),
        ],
        hotwords="Glosario: Claude, PQC, Shodan, San Francisco.",
    )
    # con «Glosario» o tres terminos seguidos es eco del prompt; «San Francisco.» puede ser real
    assert text(eng.transcribe(noise(4.0), SR)) == "San Francisco. lo hace Claude"


def test_confident_text_over_silence_and_loops_are_dropped() -> None:
    eng, _ = engine(
        [
            seg("texto en silencio", no_speech_prob=0.9, avg_logprob=-1.5),
            seg("bien", compression_ratio=3.0),
            seg("sí sí sí y vale vale vale vale vale vale", 2.0),
        ]
    )
    assert text(eng.transcribe(noise(5.0), SR)) == "sí sí sí y vale"


def test_without_guards_nothing_is_filtered() -> None:
    eng, fake = engine([seg("¡Gracias por ver el video!")], guards=False)
    assert text(eng.transcribe(noise(2.0), SR)) == "¡Gracias por ver el video!"
    assert "no_speech_threshold" not in fake.kwargs and "hotwords" not in fake.kwargs


def test_language_follows_the_ui_or_falls_back_to_spanish() -> None:
    cfg = Config()
    assert asr_language(cfg) == "es"
    cfg.ui.language = "en"
    assert asr_language(cfg) == "en"
    cfg.ui.language = "xx"
    assert asr_language(cfg) == "es"


def test_glossary_reaches_the_engine_options(tmp_home: Path) -> None:
    cfg = Config()
    assert glossary_prompt(cfg) is None  # sin glosario no hay prompt
    paths.app_data_dir().mkdir(parents=True, exist_ok=True)
    (paths.app_data_dir() / "glossary.txt").write_text(
        "# nombres\nShodan\nDunning-Kruger: in kruger\n", encoding="utf-8"
    )
    cfg.processing.glossary = ["Censys"]
    opts = engine_options(cfg)
    assert opts == {
        "language": "es",
        "guards": True,
        "hotwords": "Glosario: Shodan, Dunning-Kruger, Censys.",
    }


def test_create_uses_the_configured_size_and_options(
    monkeypatch: pytest.MonkeyPatch, tmp_home: Path
) -> None:
    seen: dict[str, Any] = {}

    class Spy:
        def __init__(self, model_dir: str, size: str, threads: int, **kw: Any) -> None:
            seen.update(model_dir=model_dir, size=size, threads=threads, **kw)

    monkeypatch.setattr(whisper_mod, "WhisperEngine", Spy)
    monkeypatch.setattr(models, "require_models", lambda _c, g: Path("/m") / g)
    whisper_mod.create(Config())
    assert seen["size"] == "large-v3-turbo"
    assert seen["model_dir"].endswith("faster-whisper-large-v3-turbo")
    assert seen["language"] == "es" and seen["guards"] is True and seen["threads"] == 6
