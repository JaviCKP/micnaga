"""Protocols de las fuentes de senales (para probar la maquina y el muestreador con fakes).

Todas las lecturas son sincronas y baratas; se llaman desde el hilo del vigilante cada
``detection.poll_interval_s``. Las implementaciones reales importan win32/comtypes de forma
perezosa.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from micnaga.watcher.model import MeetTab, MicApp, SignalReading


@dataclass(frozen=True)
class MicReading:
    """Navegadores que estan usando el micro ahora mismo."""

    browsers: frozenset[str]
    reading: SignalReading
    apps: frozenset[MicApp] = frozenset()  # todas las apps con el micro (excluidas las ignoradas)


@dataclass(frozen=True)
class TabsReading:
    tabs: tuple[MeetTab, ...]
    reading: SignalReading


@dataclass(frozen=True)
class AudioOutReading:
    browsers: frozenset[str]
    reading: SignalReading


class MicSource(Protocol):
    """Senal obligatoria 1: navegador usando el micro."""

    def read(self) -> MicReading: ...


class MeetTabSource(Protocol):
    """Senal obligatoria 2: pestana de Meet abierta. Solo se consulta con el micro activo."""

    def read(self, mic_browsers: frozenset[str]) -> TabsReading: ...


class AudioOutSource(Protocol):
    """Refuerzo: sesion de audio activa del navegador. Solo se consulta con el micro activo."""

    def read(self, mic_browsers: frozenset[str]) -> AudioOutReading: ...


@dataclass(frozen=True)
class ExtensionState:
    """Estado «en llamada» que publica la extension del navegador (F4, native messaging)."""

    in_call: bool
    browser: str  # "chrome.exe"
    title: str | None = None
    code: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ExtensionSignalSource(Protocol):
    """Fuente por extension (F4). Cuando ``available()`` es True sustituye a las demas senales.

    ``read()`` devuelve una entrada por llamada activa (lista vacia = ninguna llamada).
    """

    def available(self) -> bool: ...

    def read(self) -> list[ExtensionState]: ...


class NullExtensionSource:
    """Stub: la extension aun no existe, nunca esta disponible."""

    def available(self) -> bool:
        return False

    def read(self) -> list[ExtensionState]:
        return []
