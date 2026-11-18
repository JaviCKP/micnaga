"""Contrato de los cancelacores de eco (etapa 3, solo con altavoces)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from micnaga.types import AudioArray


@runtime_checkable
class EchoCanceller(Protocol):
    """Limpia la pista del micro usando la del sistema como referencia.

    ``mic`` y ``ref`` son mono float32 en [-1, 1], misma longitud y ya alineadas en el
    tiempo (etapa 2), a ``sr`` Hz. Devuelve un array float32 **de la misma longitud que
    ``mic``**. La conversion a int16 y el troceado en tramas de 10 ms que exigen las
    librerias nativas son cosa del adaptador. Cada llamada es una sesion independiente
    (estado del filtro adaptativo nuevo).
    """

    name: str

    def process(self, mic: AudioArray, ref: AudioArray, sr: int) -> AudioArray: ...
