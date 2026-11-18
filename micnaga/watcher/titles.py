"""Reconocimiento de pestanas de Meet por su nombre (titulo de ventana o ``TabItem`` de UIA).

Los nombres que da UI Automation no son el titulo crudo de la pagina: Chrome y Edge anaden
decoraciones localizadas, p. ej. (observado en este PC)::

    Uso de memoria de <titulo>: 85,3 MB
    Pestana inactiva: <titulo> - Memoria liberada ...
    <titulo>: en suspension - Uso de memoria - 52 MB

y los titulos de ventana acaban en ``- Google Chrome`` / ``- Perfil 1: Microsoft Edge``. Por eso el
reconocimiento no esta anclado al principio: se limpian las decoraciones conocidas y despues se
busca el codigo ``xxx-xxxx-xxx`` o el token ``Meet`` seguido de un separador.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CODE_RE = re.compile(r"(?<![a-z0-9-])([a-z]{3}-[a-z]{4}-[a-z]{3})(?![a-z0-9-])")

_SEP = r"[-\u2013\u2014:|]"  # - \u2013 \u2014 : |
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_WS_RE = re.compile(r"\s+")

_BROWSER_SUFFIX_RE = re.compile(
    r"\s[-\u2013\u2014]\s(?:[^:\u2013\u2014-]{1,40}:\s*)?"
    r"(?:Google Chrome|Microsoft\W*Edge|Brave)\s*$",
    re.IGNORECASE,
)
_DECORATION_PREFIX_RE = re.compile(
    r"^(?:Uso de memoria de|Memory usage of|Pesta[\u00f1n]a inactiva:|Inactive tab:)\s*",
    re.IGNORECASE,
)
_DECORATION_SUFFIX_RES = (
    re.compile(r":\s*[\d.,]+\s*[KMGT]?B\b.*$", re.IGNORECASE),  # ": 85,3 MB"
    re.compile(r":\s*(?:en suspensi[\u00f3o]n|sleeping)\b.*$", re.IGNORECASE),
    re.compile(r"\s[-\u2013\u2014]\s(?:Memoria liberada|Memory freed|Uso de memoria)\b.*$", re.I),
    re.compile(
        r"[,\s][-\u2013\u2014]?\s*(?:Audio playing|Reproduciendo audio|Audio muted|Audio silenciado"
        r"|Se est\u00e1 usando\b.*|Using (?:the )?(?:camera|microphone)\b.*)$",
        re.IGNORECASE,
    ),
)
_MEET_PREFIX_RE = re.compile(rf"^(?:Google\s+)?Meet(?:\s*{_SEP}\s*(?P<rest>.+))?$", re.IGNORECASE)
_MEET_SUFFIX_RE = re.compile(rf"^(?P<rest>.+?)\s*{_SEP}\s*(?:Google\s+)?Meet$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedMeetName:
    title: str | None
    code: str | None


def clean_name(raw: str) -> str:
    """Quita caracteres invisibles, el sufijo del navegador y las decoraciones de pestana."""
    text = _WS_RE.sub(" ", _INVISIBLE_RE.sub("", raw)).strip()
    text = _BROWSER_SUFFIX_RE.sub("", text).strip()
    text = _DECORATION_PREFIX_RE.sub("", text).strip()
    for _ in range(4):  # las decoraciones pueden apilarse
        before = text
        for rx in _DECORATION_SUFFIX_RES:
            text = rx.sub("", text).strip()
        if text == before:
            break
    return text


def _tidy_title(text: str, code: str | None) -> str | None:
    if code:
        text = text.replace(code, " ")
    text = re.sub(r"^[\s\-\u2013\u2014:|]+|[\s\-\u2013\u2014:|]+$", "", text)
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def parse_meet_name(raw: str) -> ParsedMeetName | None:
    """Devuelve ``(titulo, codigo)`` si ``raw`` es una pestana de Meet; ``None`` si no.

    Es una pestana de Meet si contiene un codigo ``xxx-xxxx-xxx`` o si, tras limpiar
    decoraciones, empieza por ``Meet`` (o ``Google Meet``) o termina en ``- Meet``.
    ``Meetup`` o ``Meet the team`` no cuentan.
    """
    text = clean_name(raw)
    if not text:
        return None
    code_match = CODE_RE.search(text)
    code = code_match.group(1) if code_match else None
    rest: str | None = None
    is_meet = False
    m = _MEET_PREFIX_RE.match(text)
    if m:
        is_meet = True
        rest = m.group("rest")
    else:
        m = _MEET_SUFFIX_RE.match(text)
        if m:
            is_meet = True
            rest = m.group("rest")
    if not is_meet and code is None:
        return None
    source = (rest or "") if is_meet else text
    return ParsedMeetName(title=_tidy_title(source, code), code=code)
