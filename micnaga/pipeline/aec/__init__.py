"""Cancelacion de eco. Implementaciones: ``webrtc.py`` (AEC3) y ``speex.py``.

Cada modulo expone ``create(config: Config) -> EchoCanceller``. ``config.processing.aec``
admite ademas ``"auto"`` (el primero de ``AUTO_ORDER`` que cargue) y ``"off"`` (``None``).
Ganador de la comparativa de F1 (docs/aec_comparison.md): speex (``pyaec``, filtro de 256 ms,
alineacion automatica y preprocesador) por delante de webrtc (AEC3).
"""

from __future__ import annotations

import logging

from micnaga.config import Config
from micnaga.pipeline.aec.base import EchoCanceller
from micnaga.registry import EngineUnavailableError, LazyRegistry

AEC_ENGINES: LazyRegistry[EchoCanceller] = LazyRegistry(
    "echo canceller",
    {
        "webrtc": "micnaga.pipeline.aec.webrtc:create",
        "speex": "micnaga.pipeline.aec.speex:create",
    },
    install_hint="pip install micnaga[aec]",
)

log = logging.getLogger(__name__)

AUTO_ORDER: tuple[str, ...] = ("speex", "webrtc")


def create_echo_canceller(config: Config, name: str | None = None) -> EchoCanceller | None:
    """Devuelve el cancelador pedido, ``None`` si es ``"off"``."""
    choice = name or config.processing.aec
    if choice == "off":
        return None
    if choice == "auto":
        errors: list[str] = []
        for candidate in AUTO_ORDER:
            try:
                return AEC_ENGINES.create(candidate, config)
            except EngineUnavailableError as exc:
                log.warning("echo canceller %r skipped: %s", candidate, exc)
                errors.append(str(exc))
        raise EngineUnavailableError("no echo canceller available: " + "; ".join(errors))
    return AEC_ENGINES.create(choice, config)


__all__ = ["AEC_ENGINES", "AUTO_ORDER", "EchoCanceller", "create_echo_canceller"]
