"""Contrato entre el publicador de escritorio y el servicio de reuniones."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
LIBRARY_ID = re.compile(r"[0-9a-f]{16}")
DEVICE_ID = re.compile(r"[0-9a-f]{32}")
DEVICE_KEY = re.compile(r"mn_[A-Za-z0-9_-]{43}")


def https_url(value: str, *, path: str) -> str:
    if not value.isascii() or any(ord(c) <= 32 or c in "\\\x7f" for c in value):
        raise ValueError("La URL debe ser HTTPS y no puede contener espacios.")
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.port not in (None, 443)
        or parts.path != path
        or "?" in value
        or "#" in value
    ):
        raise ValueError(f"Usa una URL pública HTTPS que termine en {path or ' el dominio'}.")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"https://{host}{path}"
