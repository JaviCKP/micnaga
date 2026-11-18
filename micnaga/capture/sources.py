"""Contratos del backend de audio: lo unico que el grabador sabe de WASAPI.

El grabador (``recorder.py``) solo habla con estos protocolos, asi que los tests usan fuentes
falsas con audio sintetico y la captura real vive aislada en ``wasapi.py`` (import perezoso).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from micnaga.types import OutputKind


class SourceError(Exception):
    """El dispositivo fallo (desconectado, en uso, formato no soportado...)."""


@dataclass(frozen=True)
class Packet:
    """Bloque de audio de una fuente.

    ``frames``: ``float32`` (o ``int16``) con forma ``(n,)`` o ``(n, canales)`` a la frecuencia
    nativa de la fuente. ``arrival_ns``: ``time.perf_counter_ns()`` (QPC) tomado **al llegar el
    bloque**, es decir, aproximadamente en el instante de su ultima muestra.
    """

    frames: NDArray[np.generic]
    arrival_ns: int


@dataclass(frozen=True)
class EndpointInfo:
    """Un punto final de audio de Windows (dispositivo)."""

    id: str
    name: str
    output_kind: OutputKind = "unknown"
    """Solo relevante para salidas (render)."""


@dataclass(frozen=True)
class EndpointSnapshot:
    """Dispositivos por defecto en un instante (lo que sondea el vigilante de cambios)."""

    render: EndpointInfo | None
    """Salida por defecto (de la que se captura el loopback)."""
    capture: EndpointInfo | None
    """Entrada por defecto de comunicaciones (``eCommunications``)."""


@runtime_checkable
class AudioSource(Protocol):
    """Una fuente de audio con paquetes sellados con el reloj comun."""

    info: EndpointInfo
    sample_rate: int
    channels: int

    def open(self) -> None:
        """Empieza a capturar. Lanza ``SourceError`` si no puede."""

    def read(self, timeout: float) -> Packet | None:
        """Siguiente paquete, o ``None`` si no llego ninguno en ``timeout`` s (silencio del
        loopback). Lanza ``SourceError`` si el dispositivo fallo."""

    def close(self) -> None:
        """Libera el dispositivo (idempotente)."""


class CaptureBackend(Protocol):
    """Fabrica de fuentes y sondeo de dispositivos por defecto."""

    def open_mic(self) -> AudioSource:
        """Fuente del micro por defecto de comunicaciones (ya abierta)."""

    def open_system(self) -> AudioSource:
        """Loopback de la salida por defecto (ya abierto)."""

    def probe(self) -> EndpointSnapshot | None:
        """Dispositivos por defecto actuales; ``None`` si el backend no puede sondear."""

    def close(self) -> None:
        """Libera todo (PortAudio, COM)."""
