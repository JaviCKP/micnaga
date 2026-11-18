"""Diarizadores. Implementaciones: ``diarize.py`` (por defecto) y ``pyannote.py``.

Cada modulo expone ``create(config: Config) -> Diarizer``. Dentro de ``diarize.py`` el
``import diarize`` (la libreria) es absoluto y no choca con el modulo del mismo nombre.
"""

from __future__ import annotations

from micnaga.config import Config
from micnaga.pipeline.engines.diar.base import Diarizer, SpeakerEmbedder
from micnaga.registry import LazyRegistry

DIARIZERS: LazyRegistry[Diarizer] = LazyRegistry(
    "diarizer",
    {
        "diarize": "micnaga.pipeline.engines.diar.diarize:create",
        "pyannote": "micnaga.pipeline.engines.diar.pyannote:create",
    },
    install_hint="pip install micnaga[diar]  /  micnaga[pyannote]",
)


def create_diarizer(config: Config, name: str | None = None) -> Diarizer:
    """Crea el diarizador ``name`` (por defecto ``config.processing.diarization``)."""
    return DIARIZERS.create(name or config.processing.diarization, config)


__all__ = ["DIARIZERS", "Diarizer", "SpeakerEmbedder", "create_diarizer"]
