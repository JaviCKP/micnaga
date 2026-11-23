"""Diarizador: ``diarize.py`` (libreria ``diarize`` + torch, WeSpeaker ResNet34-LM).

Es el unico motor. El ONNX propio («Integrado (rapido)») y ``pyannote`` se retiraron: el
primero no separaba bien a las personas en reuniones reales y el segundo nunca se instalaba.
``diarize.py`` expone ``create(config: Config) -> Diarizer``; dentro, el ``import diarize`` (la
libreria) es absoluto y no choca con el modulo del mismo nombre.
"""

from __future__ import annotations

from micnaga.config import Config
from micnaga.pipeline.engines.diar.base import Diarizer, SpeakerEmbedder
from micnaga.registry import LazyRegistry

DIARIZERS: LazyRegistry[Diarizer] = LazyRegistry(
    "diarizer",
    {"diarize": "micnaga.pipeline.engines.diar.diarize:create"},
    install_hint="pip install micnaga[diar]",
)


def create_diarizer(config: Config, name: str | None = None) -> Diarizer:
    """Crea el diarizador ``name`` (por defecto ``config.processing.diarization``)."""
    return DIARIZERS.create(name or config.processing.diarization, config)


__all__ = ["DIARIZERS", "Diarizer", "SpeakerEmbedder", "create_diarizer"]
