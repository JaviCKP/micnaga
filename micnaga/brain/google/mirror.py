"""Espejo en Odoo (``calendar.event``) de los eventos que Micnaga crea en Google Calendar.

Google es el calendario maestro (todo evento lleva Meet). Tras crear o cambiar un evento en
Google, se refleja en Odoo:

* mismo titulo y hora (Odoo guarda UTC sin zona: se convierte desde la hora local),
* enlace de Meet en ``videocall_location`` (o en la descripcion si ese campo no existe) y el
  enlace del evento de Google en la descripcion, junto a ``google_event_id: <id>``,
* solo el usuario interno como asistente; **ningun correo de Odoo** (contexto
  ``no_mail_to_attendees``, ``mail_notrack``, ``mail_create_nolog``...),
* el id de Google se guarda (``google_odoo`` + descripcion) para no duplicar y para actualizar.

Si Odoo tiene instalado el modulo nativo ``google_calendar`` (``ir.module.module`` en estado
``installed``), no se hace nada: la sincronizacion nativa ya lo trae (recomendado, ver
``docs/ONBOARDING.md``).
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from micnaga.brain import odoo_calendar
from micnaga.brain.google import store

log = logging.getLogger(__name__)

NATIVE_MODULE = "google_calendar"
CONTEXT = {  # lo de ``odoo_calendar`` (sin correos a asistentes) y ademas sin seguimiento
    **odoo_calendar.NO_MAIL_CONTEXT,
    "mail_create_nolog": True,
    "mail_create_nosubscribe": True,
    "mail_notrack": True,
    "tracking_disable": True,
    "mail_auto_subscribe_no_notify": True,
}


def utc_text(local_iso: str) -> str:
    """``2026-11-24T10:00`` (hora local de este equipo) -> ``2026-11-24 09:00:00`` (UTC)."""
    dt = datetime.fromisoformat(local_iso)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def native_sync(odoo: Any) -> bool:
    try:
        rows = odoo.search_read(
            "ir.module.module",
            [("name", "=", NATIVE_MODULE), ("state", "=", "installed")],
            ["id"],
            limit=1,
        )
    except Exception as exc:  # un usuario normal puede no leer ir.module.module
        log.info("cannot read ir.module.module (%s): mirroring", getattr(exc, "kind", exc))
        return False
    return bool(rows)


def _has_field(odoo: Any, name: str) -> bool:
    try:
        fields = odoo.call("calendar.event", "fields_get", [[name]], {"attributes": ["type"]})
    except Exception:
        return False
    return isinstance(fields, dict) and name in fields


def description(ev: dict[str, Any], with_meet: bool) -> str:
    e = html.escape
    parts = []
    if with_meet and ev.get("meet"):
        parts.append(f'<p>Meet: <a href="{e(ev["meet"])}">{e(ev["meet"])}</a></p>')
    if ev.get("html_link"):
        parts.append(f'<p>Google Calendar: <a href="{e(ev["html_link"])}">abrir</a></p>')
    if ev.get("descripcion"):
        text = str(ev["descripcion"]).strip()
        parts.append("<p>" + "<br>".join(e(x) for x in text.splitlines()) + "</p>")
    parts.append(f"<p>google_event_id: {e(str(ev['event_id']))}</p>")
    return "".join(parts)


def _values(odoo: Any, ev: dict[str, Any]) -> dict[str, Any]:
    start = datetime.fromisoformat(ev["inicio"])
    stop = start + timedelta(minutes=int(ev.get("duracion_min") or 30))
    has_video = _has_field(odoo, "videocall_location")
    values: dict[str, Any] = {
        "name": ev["titulo"],
        "start": utc_text(start.isoformat()),
        "stop": utc_text(stop.isoformat()),
        "description": description(ev, with_meet=not has_video),
    }
    if has_video and ev.get("meet"):
        values["videocall_location"] = ev["meet"]
    uid = getattr(odoo, "uid", None)
    if uid:
        values["user_id"] = uid
        with_partner = odoo.read("res.users", [uid], ["partner_id"])
        partner = with_partner[0].get("partner_id") if with_partner else None
        if isinstance(partner, list | tuple) and partner:
            values["partner_ids"] = [(6, 0, [int(partner[0])])]
    return values


def find(odoo: Any, db: Any, event_id: str) -> int | None:
    known = store.mirror_get(db, event_id)
    if known is not None:
        return known
    rows = odoo.search_read(
        "calendar.event",
        [("description", "ilike", f"google_event_id: {event_id}")],
        ["id"],
        limit=1,
    )
    return int(rows[0]["id"]) if rows else None


def upsert(odoo: Any, db: Any, ev: dict[str, Any]) -> dict[str, Any]:
    """Crea o actualiza el espejo. ``ev``: ``event_id``, ``titulo``, ``inicio`` (local),
    ``duracion_min``, ``meet``, ``html_link``, ``descripcion``."""
    if odoo.uid is None:
        odoo.authenticate()
    if native_sync(odoo):
        return {"estado": "nativo"}
    values = _values(odoo, ev)
    existing = find(odoo, db, str(ev["event_id"]))
    if existing is not None:
        odoo.call("calendar.event", "write", [[existing], values], {"context": CONTEXT})
        store.mirror_put(db, str(ev["event_id"]), existing)
        return {"estado": "actualizado", "odoo_id": existing}
    out = odoo.call("calendar.event", "create", [values], {"context": CONTEXT})
    new_id = int(out[0] if isinstance(out, list) else out)
    store.mirror_put(db, str(ev["event_id"]), new_id)
    return {"estado": "creado", "odoo_id": new_id}


def remove(odoo: Any, db: Any, event_id: str) -> bool:
    if odoo.uid is None:
        odoo.authenticate()
    existing = find(odoo, db, event_id)
    if existing is None:
        return False
    try:
        odoo.call("calendar.event", "unlink", [[existing]], {"context": CONTEXT})
    except Exception:
        # ¿ya lo borraron? se comprueba releyendo por la marca (sin la cache local)
        rows = odoo.search_read(
            "calendar.event",
            [("description", "ilike", f"google_event_id: {event_id}")],
            ["id"],
            limit=5,
        )
        if any(int(r["id"]) == existing for r in rows):
            raise
    store.mirror_forget(db, event_id)
    return True
