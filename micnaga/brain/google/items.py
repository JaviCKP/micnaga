"""Items de Google editables en la revision (``evento_google``, ``correo_google``, ``acta_drive``).

Mismo contrato que los items de Odoo (``micnaga.brain.plan``): cada item lleva ``campos``
(``[{clave, tipo, valor, ...}]``, se calcula al servirlo) y la interfaz devuelve ``cambios``
(``{clave: valor}``) que pasan por ``Executor.edit``. Aqui se validan por tipo; el item
**conserva su id**, sube ``revision`` y queda marcado (lo ha revisado el usuario).

``replace(acta, id, nueva)`` sustituye un item por una version revisada (p. ej. la de un
«corrigelo con un mensaje») con el mismo id.

Tipos (``tipo``): ``text``, ``textarea``, ``number``, ``datetime``, ``choice`` (``opciones_fijas``),
``emails`` (lista de correos) y ``people`` (asistentes ``{nombre, email, on}``; «on» = se invita).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
_ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")
MAX_TEXT = 400
MAX_LONG = 20_000
TYPES = ("evento_google", "correo_google", "acta_drive")


class EditError(ValueError):
    kind = "invalid"


def _f(k: str, t: str, **extra: Any) -> dict[str, Any]:
    return {"clave": k, "tipo": t, "etiqueta": f"items.f.{k}", **extra}


SCHEMAS: dict[str, list[dict[str, Any]]] = {
    "evento_google": [
        _f("titulo", "text", req=True, largo=160),
        _f("inicio", "datetime", req=True),
        _f("duracion_min", "number", req=True, min=5, max=600, paso=5),
        _f("asistentes", "people"),
        _f("descripcion", "textarea", largo=MAX_LONG),
    ],
    "correo_google": [
        _f("modo", "choice", opciones_fijas=["draft", "send"], req=True),
        _f("para", "emails", req=True),
        _f("asunto", "text", req=True, largo=200),
        _f("cuerpo", "textarea", req=True, largo=MAX_LONG),
    ],
    "acta_drive": [_f("titulo", "text", req=True, largo=160)],
}


def fields_for(p: dict[str, Any]) -> list[dict[str, Any]]:
    datos = p.get("datos") if isinstance(p.get("datos"), dict) else {}
    assert isinstance(datos, dict)
    return [{**s, "valor": datos.get(s["clave"])} for s in SCHEMAS.get(str(p.get("tipo")), [])]


def decorate(acta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Anade ``campos`` a los items de Google (los de Odoo los pone ``plan.decorate``)."""
    if acta:
        for p in acta.get("propuestas") or []:
            if isinstance(p, dict) and p.get("tipo") in SCHEMAS:
                p["campos"] = fields_for(p)
    return acta


# ============================================================================== validacion


def _clean(value: Any, spec: dict[str, Any]) -> Any:
    t, key = spec["tipo"], spec["clave"]
    req = bool(spec.get("req"))
    if t in ("text", "textarea"):
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise EditError(key)
        text = value.strip() if t == "textarea" else " ".join(value.split())
        if len(text) > int(spec.get("largo") or (MAX_LONG if t == "textarea" else MAX_TEXT)):
            raise EditError(key)
        if req and not text:
            raise EditError(key)
        return text or None
    if t == "number":
        if value in (None, "") and not req:
            return None
        if isinstance(value, str):
            try:
                value = float(value.replace(",", "."))
            except ValueError as exc:
                raise EditError(key) from exc
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise EditError(key)
        lo, hi = spec.get("min"), spec.get("max")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise EditError(key)
        return int(value) if float(value).is_integer() else float(value)
    if t == "datetime":
        if not isinstance(value, str) or not _ISO_DT.match(value):
            raise EditError(key)
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%dT%H:%M")
        except ValueError as exc:
            raise EditError(key) from exc
    if t == "choice":
        if value not in spec.get("opciones_fijas", []):
            raise EditError(key)
        return value
    if t == "emails":
        if isinstance(value, str):
            value = [x for x in re.split(r"[,;\s]+", value) if x]
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise EditError(key)
        emails: list[str] = []
        for x in value:
            x = x.strip()
            if not EMAIL.match(x):
                raise EditError(key)
            if x.casefold() not in (e.casefold() for e in emails):
                emails.append(x)
        if req and not emails:
            raise EditError(key)
        return emails[:20]
    if t == "people":
        if not isinstance(value, list):
            raise EditError(key)
        people = []
        seen: set[str] = set()
        for it in value[:30]:
            if not isinstance(it, dict):
                raise EditError(key)
            email = str(it.get("email") or "").strip()
            if not EMAIL.match(email) or email.casefold() in seen:
                raise EditError(key)
            seen.add(email.casefold())
            name = " ".join(str(it.get("nombre") or "").split())[:80] or None
            people.append({"nombre": name, "email": email, "on": bool(it.get("on"))})
        return people
    raise EditError(key)  # pragma: no cover


def edit(p: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Aplica ``changes`` (clave -> valor) a ``p['datos']`` validando por tipo."""
    if p.get("estado") not in (None, "pendiente", "fallida"):
        raise EditError("estado")
    specs = {s["clave"]: s for s in SCHEMAS.get(str(p.get("tipo")), [])}
    datos = dict(p.get("datos") or {})
    for k, v in changes.items():
        spec = specs.get(k)
        if spec is None:
            continue  # lo que no es editable se ignora (no se puede inyectar)
        datos[k] = _clean(v, spec)
    for k, spec in specs.items():
        if spec.get("req") and datos.get(k) in (None, "", []):
            raise EditError(k)
    if p.get("tipo") == "evento_google" and datos.get("inicio") != (p.get("datos") or {}).get(
        "inicio"
    ):
        p["aviso"] = None  # la hora la ha elegido el usuario (ya no es un hueco sugerido)
    p["datos"] = datos
    p["estado"] = "pendiente"
    p["error"] = None
    p["checked"] = True
    p["revision"] = int(p.get("revision") or 0) + 1
    _refresh(p)
    return p


def replace(acta: dict[str, Any], pid: str, new: dict[str, Any]) -> dict[str, Any]:
    """Sustituye el item ``pid`` por ``new`` (version revisada), con el mismo id."""
    props = acta.get("propuestas") or []
    for i, p in enumerate(props):
        if p.get("id") != pid:
            continue
        if p.get("estado") != "pendiente" or new.get("tipo", p.get("tipo")) != p.get("tipo"):
            raise EditError("estado")
        merged = {**p, **{k: v for k, v in new.items() if k not in ("id", "uid", "estado", "checked")}}
        merged["datos"] = _revised(p, new.get("datos"))
        merged["id"] = pid
        merged["estado"] = "pendiente"
        merged["revision"] = int(p.get("revision") or 0) + 1
        _refresh(merged)
        props[i] = merged
        return merged
    raise EditError("id")


def _revised(old: dict[str, Any], datos: Any) -> dict[str, Any]:
    """Datos de una version revisada (p. ej. por la IA): se validan como una edicion y nunca
    activan por su cuenta lo que sale de la cuenta (invitar a alguien, enviar en vez de borrador):
    eso solo lo decide el usuario en la revision."""
    before = dict(old.get("datos") or {})
    if not isinstance(datos, dict):
        return before
    out = {**before, **datos}
    for spec in SCHEMAS.get(str(old.get("tipo")), []):
        key = spec["clave"]
        if key in datos:
            out[key] = _clean(datos[key], spec)
        if spec.get("req") and out.get(key) in (None, "", []):
            raise EditError(key)
    was_on = {str(a.get("email")).casefold() for a in before.get("asistentes") or [] if a.get("on")}
    for a in out.get("asistentes") or []:
        a["on"] = bool(a.get("on")) and str(a.get("email")).casefold() in was_on
    if out.get("modo") == "send" and before.get("modo") != "send":
        out["modo"] = before.get("modo") or "draft"
    return out


def _refresh(p: dict[str, Any]) -> None:
    """Datos derivados tras editar (titulo, a quien se invita, a quien se envia)."""
    from micnaga.brain.google.analysis import refresh

    refresh(p)
