"""Motores ASR. Implementaciones: ``parakeet.py`` y ``whisper.py`` (cada una con ``create``).

Para anadir un motor: crear ``micnaga/pipeline/engines/asr/<nombre>.py`` con una funcion
``create(config: Config) -> AsrEngine`` y registrar su nombre en ``ASR_ENGINES`` (una linea).
"""

from __future__ import annotations

from micnaga.config import Config
from micnaga.pipeline.engines.asr.base import AsrEngine
from micnaga.registry import LazyRegistry

ASR_ENGINES: LazyRegistry[AsrEngine] = LazyRegistry(
    "ASR engine",
    {
        "parakeet": "micnaga.pipeline.engines.asr.parakeet:create",
        "whisper": "micnaga.pipeline.engines.asr.whisper:create",
    },
    install_hint="pip install micnaga[asr]",
)


def create_asr(config: Config, name: str | None = None) -> AsrEngine:
    """Crea el motor ``name`` (por defecto ``config.processing.asr_engine``)."""
    return ASR_ENGINES.create(name or config.processing.asr_engine, config)


__all__ = ["ASR_ENGINES", "AsrEngine", "create_asr"]
