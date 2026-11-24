"""Cancelacion de eco. Implementaciones: ``webrtc.py`` (AEC3) y ``speex.py``.

Cada modulo expone ``create(config: Config) -> EchoCanceller``. ``config.processing.aec`` es un
modo (``policy.py``): ``"auto"`` y ``"always"`` usan el primero de ``AUTO_ORDER`` que cargue (lo
que cambia es *donde* se aplica, no el motor) y ``"off"`` da ``None``. ``name`` permite pedir un
motor concreto (banco de pruebas).

La comparativa de F1 (docs/aec_comparison.md, solo escenarios sinteticos) dio ventaja a speex
(``pyaec``), pero su ``aec.dll`` no esta firmada y sin editor conocido: Windows/antivirus bloquearon
la instalacion en un PC. Por eso el instalador ya NO lleva ``pyaec`` y ``auto`` prefiere webrtc
(AEC3, ``aec-audio-processing``). ``speex`` sigue disponible en desarrollo si ``pyaec`` esta
instalado (extra ``dev``); si se pide y no esta, se cae al orden de ``auto`` en vez de fallar
(una config antigua con ``aec = "speex"`` sigue cancelando eco).
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

AUTO_ORDER: tuple[str, ...] = ("webrtc", "speex")
FALLBACK_TO_AUTO: frozenset[str] = frozenset({"speex"})
"""Motores que no viajan en el instalador: si se piden y faltan, se usa ``AUTO_ORDER``."""


def create_echo_canceller(config: Config, name: str | None = None) -> EchoCanceller | None:
    """Devuelve el cancelador pedido, ``None`` si es ``"off"``."""
    choice = name or config.processing.aec
    if choice == "off":
        return None
    if choice in ("auto", "always"):
        return _first_available(config, AUTO_ORDER)
    if choice in FALLBACK_TO_AUTO:
        try:
            return AEC_ENGINES.create(choice, config)
        except EngineUnavailableError as exc:
            log.warning("echo canceller %r unavailable (%s); using auto order", choice, exc)
            return _first_available(config, tuple(c for c in AUTO_ORDER if c != choice))
    return AEC_ENGINES.create(choice, config)


def _first_available(config: Config, order: tuple[str, ...]) -> EchoCanceller:
    errors: list[str] = []
    for candidate in order:
        try:
            return AEC_ENGINES.create(candidate, config)
        except EngineUnavailableError as exc:
            log.warning("echo canceller %r skipped: %s", candidate, exc)
            errors.append(str(exc))
    raise EngineUnavailableError("no echo canceller available: " + "; ".join(errors))


__all__ = ["AEC_ENGINES", "AUTO_ORDER", "EchoCanceller", "create_echo_canceller"]
