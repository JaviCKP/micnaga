"""«Arrancar con Windows»: clave ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``.

* El **unico** codigo de la UI que escribe en el registro. La ruta de la clave y el acceso al
  registro son inyectables (``RegistryBackend``) para que los tests y el modo demo usen una
  clave falsa y **jamas** toquen el ``Run`` real de este PC.
* Defensa extra: ``WinRegistry`` se niega a escribir si detecta pytest
  (``PYTEST_CURRENT_TEST``) o el modo demo (``MICNAGA_UI_DEMO=1``), salvo que se le pase una
  ruta de clave distinta de la real.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Protocol

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Micnaga"
ENV_DEMO = "MICNAGA_UI_DEMO"


class RegistryBackend(Protocol):
    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


class RegistryWriteBlockedError(RuntimeError):
    """Escritura en el ``Run`` real bloqueada (tests / demo)."""


class WinRegistry:
    """Acceso real a ``HKCU\\<key_path>`` mediante ``winreg``."""

    def __init__(self, key_path: str = RUN_KEY_PATH) -> None:
        self.key_path = key_path

    def _guard(self) -> None:
        if self.key_path != RUN_KEY_PATH:
            return  # una clave de pruebas no es el Run real
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get(ENV_DEMO):
            raise RegistryWriteBlockedError(
                "refusing to write the real HKCU Run key under pytest/demo; inject a backend"
            )

    def get(self, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_READ) as k:
                value, _kind = winreg.QueryValueEx(k, name)
        except OSError:
            return None
        return str(value)

    def set(self, name: str, value: str) -> None:
        import winreg

        self._guard()
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_SET_VALUE
        ) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> None:
        import winreg

        self._guard()
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_SET_VALUE
            ) as k:
                winreg.DeleteValue(k, name)
        except FileNotFoundError:
            return


class MemoryRegistry:
    """Registro falso en memoria (tests y demo)."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


_default_backend: RegistryBackend | None = None


def set_default_backend(backend: RegistryBackend | None) -> None:
    """Sustituye el backend por defecto (demo/tests). ``None`` restaura el real."""
    global _default_backend
    _default_backend = backend


def default_backend() -> RegistryBackend:
    return _default_backend if _default_backend is not None else WinRegistry()


def default_command(*, frozen: bool | None = None, executable: str | None = None) -> str:
    """Linea de comandos que Windows ejecuta al iniciar sesion.

    Instalado (PyInstaller): ``"<ruta>\\Micnaga.exe" app``. En desarrollo:
    ``pythonw -m micnaga app`` (sin consola).
    """
    exe = executable or sys.executable
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        return f'"{exe}" app'
    p = Path(exe)
    pythonw = p.with_name("pythonw.exe")
    return f'"{pythonw if pythonw.is_file() else p}" -m micnaga app'


def is_run_at_login(*, backend: RegistryBackend | None = None, name: str = VALUE_NAME) -> bool:
    return (backend or default_backend()).get(name) is not None


def get_run_command(
    *, backend: RegistryBackend | None = None, name: str = VALUE_NAME
) -> str | None:
    return (backend or default_backend()).get(name)


def ensure_run_at_login(
    *, backend: RegistryBackend | None = None, command: str | None = None, name: str = VALUE_NAME
) -> bool:
    """Garantiza que el valor ``Run`` existe y apunta al ejecutable actual (p. ej. tras mover o
    actualizar la instalacion). ``True`` si hubo que escribirlo. NO decide si debe estar activo:
    quien llama comprueba antes ``startup.run_at_login`` (la config manda; el instalador escribe
    el mismo valor ``Micnaga`` con el mismo comando)."""
    be = backend or default_backend()
    wanted = command or default_command()
    if be.get(name) == wanted:
        return False
    be.set(name, wanted)
    return True


def set_run_at_login(
    enabled: bool,
    *,
    backend: RegistryBackend | None = None,
    command: str | None = None,
    name: str = VALUE_NAME,
) -> None:
    """Activa/desactiva el arranque con Windows (idempotente)."""
    be = backend or default_backend()
    if enabled:
        be.set(name, command or default_command())
    else:
        be.delete(name)
