"""Registro con importacion perezosa de implementaciones (motores ASR, diarizadores, AEC).

Una implementacion se declara como ``"paquete.modulo:funcion"``; el modulo NO se importa
hasta que alguien pide esa implementacion. Asi los motores pueden escribirse mas tarde (o
faltar sus dependencias opcionales) sin tocar a quien los usa.

Convencion de fabricas: ``create(config: Config) -> <Protocol>`` en el modulo indicado.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class UnknownEngineError(ValueError):
    """El nombre no esta registrado."""


class EngineUnavailableError(RuntimeError):
    """La implementacion existe en el registro pero no se puede cargar.

    Causas tipicas: el modulo aun no esta escrito, o falta una dependencia opcional
    (``pip install micnaga[asr]``). La causa original va en ``__cause__``.
    """


class LazyRegistry(Generic[T]):
    """Mapa ``nombre -> "modulo:funcion"`` con resolucion perezosa."""

    def __init__(self, kind: str, entries: Mapping[str, str], install_hint: str = "") -> None:
        self.kind = kind
        self._entries = dict(entries)
        self._hint = install_hint

    def names(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def target(self, name: str) -> str:
        """``"modulo:funcion"`` registrado para ``name``."""
        try:
            return self._entries[name]
        except KeyError:
            raise UnknownEngineError(
                f"unknown {self.kind} {name!r}; expected one of {self.names()}"
            ) from None

    def resolve(self, name: str) -> Callable[..., T]:
        """Importa el modulo y devuelve la fabrica."""
        module_name, _, attr = self.target(name).partition(":")
        try:
            module = importlib.import_module(module_name)
            factory: Callable[..., T] = getattr(module, attr)
        except (ImportError, AttributeError) as exc:
            hint = f" ({self._hint})" if self._hint else ""
            raise EngineUnavailableError(
                f"{self.kind} {name!r} is not available: {exc}{hint}"
            ) from exc
        return factory

    def create(self, name: str, *args: Any, **kwargs: Any) -> T:
        return self.resolve(name)(*args, **kwargs)

    def register(self, name: str, target: str) -> None:
        """Anade o sustituye una entrada (util en tests)."""
        self._entries[name] = target
