"""Fabricas y registro: nombres, errores utiles cuando faltan modelos o librerias."""

from __future__ import annotations

import pytest

from micnaga.config import Config
from micnaga.pipeline.aec import AEC_ENGINES, create_echo_canceller
from micnaga.pipeline.aec.base import EchoCanceller
from micnaga.pipeline.engines.asr import ASR_ENGINES, create_asr
from micnaga.pipeline.engines.diar import DIARIZERS, create_diarizer
from micnaga.registry import EngineUnavailableError


@pytest.mark.parametrize("name", ["parakeet", "whisper"])
def test_asr_factories_resolve(name: str) -> None:
    assert callable(ASR_ENGINES.resolve(name))


@pytest.mark.parametrize("name", ["diarize"])
def test_diarizer_factories_resolve(name: str) -> None:
    assert callable(DIARIZERS.resolve(name))


@pytest.mark.parametrize("name", ["webrtc", "speex"])
def test_aec_factories_resolve(name: str) -> None:
    assert callable(AEC_ENGINES.resolve(name))


@pytest.mark.parametrize("name", ["parakeet", "whisper"])
def test_asr_without_models_gives_helpful_error(name: str, tmp_models: object) -> None:
    with pytest.raises(EngineUnavailableError) as exc:
        create_asr(Config(), name)
    msg = str(exc.value)
    assert "download_models" in msg
    assert "missing" in msg


def test_whisper_medium_not_in_manifest() -> None:
    cfg = Config()
    cfg.processing.whisper_model = "medium"
    with pytest.raises(EngineUnavailableError, match="manifest"):
        create_asr(cfg, "whisper")


def test_diarize_without_wespeaker_model_gives_helpful_error(tmp_models: object) -> None:
    with pytest.raises(EngineUnavailableError, match="download_models"):
        create_diarizer(Config(), "diarize")


def test_aec_auto_and_off() -> None:
    cfg = Config()
    ec = create_echo_canceller(cfg)
    assert isinstance(ec, EchoCanceller)
    assert ec.name  # el primero que cargue
    assert create_echo_canceller(cfg, "off") is None
    assert create_echo_canceller(cfg, "webrtc").name == "webrtc-aec3"
    assert create_echo_canceller(cfg, "speex").name == "speexdsp-pyaec"


def test_importing_engine_modules_is_light() -> None:
    """Importar los modulos de motores no arrastra torch ni librerias de modelos."""
    import subprocess
    import sys
    from pathlib import Path

    # ``-I`` ignora PYTHONPATH y el directorio actual: sin esto se importaria la copia del repo
    # instalada en modo editable (p. ej. la principal al probar desde un worktree).
    root = str(Path(__file__).resolve().parents[2])
    code = (
        f"import sys; sys.path.insert(0, {root!r})\n"
        "import micnaga.pipeline.engines.asr.parakeet, micnaga.pipeline.engines.asr.whisper\n"
        "import micnaga.pipeline.engines.diar.diarize, micnaga.pipeline.engines.diar.room\n"
        "import micnaga.pipeline.aec.webrtc, micnaga.pipeline.aec.speex\n"
        "import micnaga.pipeline.vad, micnaga.models\n"
        "bad = [m for m in ('torch', 'torchaudio', 'diarize', 'wespeakerruntime',\n"
        "        'faster_whisper', 'onnx_asr', 'onnxruntime', 'pyannote.audio')\n"
        "       if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""
