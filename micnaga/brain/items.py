"""Propuestas editables: cada una es ``{id, grupo, tipo, datos, ...}`` y aqui se describe, por
tipo, que campos de ``datos`` se pueden editar en la revision y como se validan.

* ``fields_for(p)`` -> ``[{"k", "t", "v", ...}]``: lo que pinta la interfaz (una capa uniforme
  para todos los tipos). Tipos conocidos con su esquema; **tipos desconocidos** (p. ej. los que
  anada otra rama) con un esquema generico deducido de ``datos`` (texto, numero, fecha, si/no).
* ``edit(p, valores)`` valida y escribe en ``datos``; la propuesta **conserva su id**, sube su
  ``revision`` y vuelve a ``pendiente``. Una aplicada no se edita.
* ``replace(acta, id, nueva)``: sustituye una propuesta por una version revisada (p. ej. la que
  devuelva un «corrigelo con un mensaje»), con el mismo id.
* ``decorate(acta)``: anade ``campos`` a cada propuesta para la interfaz (no se guarda).

Tipos ``kind`` (``t``): ``text``, ``textarea``, ``number``, ``date``, ``datetime``, ``bool``,
``choice`` (``opts``), ``emails`` (lista de correos), ``people`` (asistentes ``{nombre, email,
on}``: «on» = se invita).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")
MAX_TEXT = 400
MAX_LONG = 20_000


class EditError(ValueError):
    kind = "invalid"


def _f(k: str, t: str, **extra: Any) -> dict[str, Any]:
    return {"k": k, "t": t, **extra}


# Esquemas por tipo (orden = orden en pantalla).
SCHEMAS: dict[str, list[dict[str, Any]]] = {
    "hablante": [_f("a", "text", req=True, max=60)],
    "palabra": [_f("de", "text", req=True, max=60), _f("a", "text", req=True, max=60)],
    "vincular_a_oportunidad": [_f("id", "number", req=True)],
    "nota_en_historial": [_f("id", "number", req=True)],
    "crear_contacto": [
        _f("nombre", "text", req=True, max=80),
        _f("email", "text", max=120),
        _f("telefono", "text", max=40),
        _f("cargo", "text", max=80),
        _f("empresa_id", "number"),
    ],
    "actualizar_oportunidad": [
        _f("etapa_id", "number"),
        _f("importe", "number"),
        _f("fecha_cierre", "date"),
    ],
    "programar_actividad": [
        _f("resumen", "text", req=True, max=160),
        _f("fecha", "date", req=True),
    ],
    # Google
    "evento_google": [
        _f("titulo", "text", req=True, max=160),
        _f("inicio", "datetime", req=True),
        _f("duracion_min", "number", req=True, min=5, max=600),
        _f("asistentes", "people"),
        _f("descripcion", "textarea", max=MAX_LONG),
    ],
    "correo_google": [
        _f("modo", "choice", opts=["draft", "send"], req=True),
        _f("para", "emails", req=True),
        _f("asunto", "text", req=True, max=200),
        _f("cuerpo", "textarea", req=True, max=MAX_LONG),
    ],
    "acta_drive": [_f("titulo", "text", req=True, max=160)],
}


def _generic(datos: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for k, v in datos.items():
        if k.startswith("_") or k in ("modelo",):
            continue
        if isinstance(v, bool):
            out.append(_f(k, "bool"))
        elif isinstance(v, int | float):
            out.append(_f(k, "number"))
        elif isinstance(v, str) and _ISO_DATE.match(v):
            out.append(_f(k, "date"))
        elif isinstance(v, str) and _ISO_DT.match(v):
            out.append(_f(k, "datetime"))
        elif isinstance(v, str):
            out.append(_f(k, "textarea" if len(v) > 80 else "text", max=MAX_LONG))
        elif v is None and k in ("fecha", "fecha_cierre"):
            out.append(_f(k, "date"))
    return out


def fields_for(p: dict[str, Any]) -> list[dict[str, Any]]:
    datos = p.get("datos") if isinstance(p.get("datos"), dict) else {}
    assert isinstance(datos, dict)
    schema = SCHEMAS.get(str(p.get("tipo")))
    specs = [dict(s) for s in schema] if schema is not None else _generic(datos)
    for s in specs:
        s["v"] = datos.get(s["k"])
    return specs


def decorate(acta: dict[str, Any] | None) -> dict[str, Any] | None:
    if acta:
        for p in acta.get("propuestas") or []:
            if isinstance(p, dict):
                p["campos"] = fields_for(p)
    return acta


def strip(acta: dict[str, Any]) -> dict[str, Any]:
    """Quita lo que solo es para la interfaz antes de guardar."""
    for p in acta.get("propuestas") or []:
        if isinstance(p, dict):
            p.pop("campos", None)
    return acta


# ============================================================================== validacion


def _clean(value: Any, spec: dict[str, Any]) -> Any:
    t = spec["t"]
    req = bool(spec.get("req"))
    if t in ("text", "textarea"):
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise EditError(spec["k"])
        text = value.strip() if t == "textarea" else " ".join(value.split())
        if len(text) > int(spec.get("max") or (MAX_LONG if t == "textarea" else MAX_TEXT)):
            raise EditError(spec["k"])
        if req and not text:
            raise EditError(spec["k"])
        return text or None
    if t == "number":
        if value in (None, "") and not req:
            return None
        if isinstance(value, str):
            try:
                value = float(value.replace(",", "."))
            except ValueError as exc:
                raise EditError(spec["k"]) from exc
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise EditError(spec["k"])
        lo, hi = spec.get("min"), spec.get("max")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise EditError(spec["k"])
        return int(value) if float(value).is_integer() else float(value)
    if t == "date":
        if value in (None, "") and not req:
            return None
        if not isinstance(value, str) or not _ISO_DATE.match(value):
            raise EditError(spec["k"])
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise EditError(spec["k"]) from exc
    if t == "datetime":
        if value in (None, "") and not req:
            return None
        if not isinstance(value, str) or not _ISO_DT.match(value):
            raise EditError(spec["k"])
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%dT%H:%M")
        except ValueError as exc:
            raise EditError(spec["k"]) from exc
    if t == "bool":
        if not isinstance(value, bool):
            raise EditError(spec["k"])
        return value
    if t == "choice":
        if value not in spec.get("opts", []):
            raise EditError(spec["k"])
        return value
    if t == "emails":
        if isinstance(value, str):
            value = [x for x in re.split(r"[,;\s]+", value) if x]
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise EditError(spec["k"])
        emails: list[str] = []
        for x in value:
            x = x.strip()
            if not EMAIL.match(x):
                raise EditError(spec["k"])
            if x.casefold() not in (e.casefold() for e in emails):
                emails.append(x)
        if req and not emails:
            raise EditError(spec["k"])
        return emails[:20]
    if t == "people":
        if not isinstance(value, list):
            raise EditError(spec["k"])
        people = []
        seen: set[str] = set()
        for it in value[:30]:
            if not isinstance(it, dict):
                raise EditError(spec["k"])
            email = str(it.get("email") or "").strip()
            if not EMAIL.match(email) or email.casefold() in seen:
                raise EditError(spec["k"])
            seen.add(email.casefold())
            name = " ".join(str(it.get("nombre") or "").split())[:80] or None
            people.append({"nombre": name, "email": email, "on": bool(it.get("on"))})
        return people
    raise EditError(spec["k"])  # pragma: no cover


def edit(p: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Aplica ``values`` (clave -> valor) a ``p['datos']`` validando por tipo."""
    if p.get("estado") not in (None, "pendiente", "fallida", "descartada"):
        raise EditError("estado")
    specs = {s["k"]: s for s in fields_for(p)}
    datos = dict(p.get("datos") or {})
    for k, v in values.items():
        spec = specs.get(k)
        if spec is None:
            continue  # lo que no es editable se ignora (no se puede inyectar)
        datos[k] = _clean(v, spec)
    for k, spec in specs.items():
        if spec.get("req") and datos.get(k) in (None, "", []):
            raise EditError(k)
    p["datos"] = datos
    p["estado"] = "pendiente"
    p["error"] = None
    p["editada"] = True
    p["revision"] = int(p.get("revision") or 0) + 1
    _refresh(p)
    return p


def replace(acta: dict[str, Any], pid: str, new: dict[str, Any]) -> dict[str, Any]:
    """Sustituye la propuesta ``pid`` por ``new`` (version revisada), con el mismo id."""
    props = acta.get("propuestas") or []
    for i, p in enumerate(props):
        if p.get("id") != pid:
            continue
        if p.get("estado") == "aplicada":
            raise EditError("estado")
        merged = {**p, **{k: v for k, v in new.items() if k not in ("id", "estado")}}
        merged["id"] = pid
        merged["estado"] = "pendiente"
        merged["revision"] = int(p.get("revision") or 0) + 1
        _refresh(merged)
        props[i] = merged
        return merged
    raise EditError("id")


def _refresh(p: dict[str, Any]) -> None:
    """Datos derivados tras editar (p. ej. el titulo de una propuesta de Google)."""
    if p.get("grupo") == "google":
        from micnaga.brain.google.analysis import refresh

        refresh(p)
