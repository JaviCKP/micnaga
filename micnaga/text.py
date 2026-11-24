"""Formato y búsqueda de texto compartidos con el servicio remoto."""

from __future__ import annotations

import re


def format_mmss(seconds: float) -> str:
    total = int(max(0.0, seconds))
    m, s = divmod(total, 60)
    return f"{m:02d}:{s:02d}"


def fts_query(text: str) -> str:
    """Convierte texto libre en una consulta FTS5 con AND de prefijos entre comillas."""
    return " ".join(f'"{token}"*' for token in re.findall(r"\w+", text))
