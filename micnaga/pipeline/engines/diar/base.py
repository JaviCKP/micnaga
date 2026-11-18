"""Contrato de los diarizadores y de los extractores de huella de voz."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from micnaga.types import AudioArray, Turn


@runtime_checkable
class Diarizer(Protocol):
    """Diarizador de la pista remota.

    ``diarize`` recibe audio mono float32 a ``sr`` Hz y devuelve turnos (``Turn``) con
    etiquetas ``"S1"``, ``"S2"``... (ya normalizadas, ordenadas por primera aparicion) y
    tiempos relativos al inicio de ``audio``. ``n_speakers=None`` = estimacion automatica.
    """

    name: str

    def diarize(self, audio: AudioArray, sr: int, n_speakers: int | None) -> list[Turn]: ...

    def close(self) -> None: ...


@runtime_checkable
class SpeakerEmbedder(Protocol):
    """Capacidad opcional (MIC-11): un embedding por hablante.

    Un ``Diarizer`` que la ofrezca la cumple ademas de ``Diarizer``; el llamador comprueba
    con ``isinstance(d, SpeakerEmbedder)``. Devuelve ``{speaker_id: vector float32}``
    calculado con el audio de los ``turns`` de ese hablante.
    """

    def embed(
        self, audio: AudioArray, sr: int, turns: list[Turn]
    ) -> dict[str, NDArray[np.float32]]: ...
