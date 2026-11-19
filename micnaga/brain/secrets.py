"""Secretos de la Secretaria en el Administrador de credenciales de Windows (DPAPI, por usuario).

Destinos (``CRED_TYPE_GENERIC``):

* ``Micnaga/claude-token``  token OAuth de ``claude setup-token`` (modo ``token``).
* ``Micnaga/anthropic-key`` clave de API de la consola (modo ``api_key``).
* ``Micnaga/odoo``          clave de API de Odoo (la lee solo el servidor MCP y el ejecutor).

Nunca se escriben en ``config.toml``, ni en logs, ni en la linea de comandos.

Pruebas: con ``MICNAGA_SECRETS_FILE=<ruta.json>`` se usa un almacen JSON en claro (solo para
tests; cruza la frontera de procesos, asi el servidor MCP lanzado por un ``claude`` falso lee el
mismo almacen). Bajo pytest sin esa variable se usa un almacen en memoria: los tests nunca tocan
el Administrador de credenciales real.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Protocol

ENV_SECRETS_FILE = "MICNAGA_SECRETS_FILE"

CLAUDE_TOKEN = "Micnaga/claude-token"
ANTHROPIC_KEY = "Micnaga/anthropic-key"
ODOO_KEY = "Micnaga/odoo"
ALL_TARGETS = (CLAUDE_TOKEN, ANTHROPIC_KEY, ODOO_KEY)


class SecretStore(Protocol):
    def get(self, target: str) -> str | None: ...

    def set(self, target: str, value: str) -> None: ...

    def delete(self, target: str) -> None: ...


class MemorySecretStore:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, target: str) -> str | None:
        with self._lock:
            return self._data.get(target)

    def set(self, target: str, value: str) -> None:
        with self._lock:
            self._data[target] = value

    def delete(self, target: str) -> None:
        with self._lock:
            self._data.pop(target, None)


class FileSecretStore:
    """Almacen JSON en claro. **Solo pruebas** (``MICNAGA_SECRETS_FILE``)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def get(self, target: str) -> str | None:
        return self._load().get(target)

    def set(self, target: str, value: str) -> None:
        data = self._load()
        data[target] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def delete(self, target: str) -> None:
        data = self._load()
        if data.pop(target, None) is not None:
            self.path.write_text(json.dumps(data), encoding="utf-8")


class WindowsCredentialStore:
    """``win32cred`` (pywin32). Persistencia ``LOCAL_MACHINE`` = este usuario en este equipo."""

    def get(self, target: str) -> str | None:
        import pywintypes
        import win32cred

        try:
            cred = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
        except pywintypes.error:
            return None
        blob = cred.get("CredentialBlob") or b""
        if isinstance(blob, str):  # algunas versiones devuelven str
            return blob or None
        try:
            return bytes(blob).decode("utf-16-le") or None
        except UnicodeDecodeError:
            return None

    def set(self, target: str, value: str) -> None:
        import win32cred

        win32cred.CredWrite(
            {
                "Type": win32cred.CRED_TYPE_GENERIC,
                "TargetName": target,
                "UserName": "micnaga",
                "CredentialBlob": value,
                "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                "Comment": "Micnaga · Secretaria",
            },
            0,
        )

    def delete(self, target: str) -> None:
        import pywintypes
        import win32cred

        with contextlib.suppress(pywintypes.error):
            win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)


_lock = threading.Lock()
_override: SecretStore | None = None
_test_memory: MemorySecretStore | None = None


def set_store(store: SecretStore | None) -> None:
    """Fija el almacen del proceso (tests y demo). ``None`` vuelve al de por defecto."""
    global _override
    with _lock:
        _override = store


def store() -> SecretStore:
    """El almacen en uso: el fijado, ``MICNAGA_SECRETS_FILE``, memoria bajo pytest o Windows."""
    global _test_memory
    with _lock:
        if _override is not None:
            return _override
        file = os.environ.get(ENV_SECRETS_FILE, "").strip()
        if file:
            return FileSecretStore(Path(file))
        if "pytest" in sys.modules or os.environ.get("MICNAGA_UI_DEMO"):
            if _test_memory is None:
                _test_memory = MemorySecretStore()
            return _test_memory
        return WindowsCredentialStore()


def get(target: str) -> str | None:
    try:
        return store().get(target)
    except Exception:
        return None


def put(target: str, value: str | None) -> None:
    """Guarda (o borra con ``None``/vacio) un secreto."""
    s = store()
    if value is None or not value.strip():
        s.delete(target)
    else:
        s.set(target, value.strip())


def has(target: str) -> bool:
    return bool(get(target))
