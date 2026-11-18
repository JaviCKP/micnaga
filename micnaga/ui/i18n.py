"""Textos de la UI externalizados (``web/i18n/<idioma>.json``).

El mismo fichero lo leen el JavaScript (``mic.t``) y Python (bandeja, notificaciones), asi que
traducir la app es anadir un ``xx.json`` con las mismas claves.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

I18N_DIR = Path(__file__).resolve().parent / "web" / "i18n"
DEFAULT_LANGUAGE = "es"

_VAR_RE = re.compile(r"\{(\w+)\}")


@lru_cache(maxsize=8)
def load(language: str = DEFAULT_LANGUAGE) -> dict[str, str]:
    """Carga el diccionario de ``language``; si no existe, el idioma por defecto."""
    path = I18N_DIR / f"{language}.json"
    if not path.is_file():
        path = I18N_DIR / f"{DEFAULT_LANGUAGE}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return {str(k): str(v) for k, v in data.items()}


def t(key: str, language: str = DEFAULT_LANGUAGE, /, **values: object) -> str:
    """Texto traducido con ``{variables}`` sustituidas. Una clave ausente devuelve la clave."""
    text = load(language).get(key)
    if text is None:
        return key
    return _VAR_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)


def clear_cache() -> None:
    load.cache_clear()
