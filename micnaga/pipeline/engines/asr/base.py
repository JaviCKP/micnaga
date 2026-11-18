"""Contrato de los motores de transcripcion (ASR)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from micnaga.types import AudioArray, Word


@runtime_checkable
class AsrEngine(Protocol):
    """Motor ASR. Un motor = un modelo cargado; ``close()`` lo libera.

    ``transcribe`` recibe audio mono float32 en [-1, 1] a ``sr`` Hz (normalmente 16000) y
    devuelve palabras con tiempos en segundos **relativos al inicio de ``audio``**. El
    llamador desplaza los tiempos si pasa un bloque suelto de una pista.
    """

    name: str
    """Identificador para ``transcripcion.json`` (p. ej. ``"parakeet-tdt-0.6b-v3-int8"``)."""

    def transcribe(self, audio: AudioArray, sr: int) -> list[Word]: ...

    def close(self) -> None: ...
