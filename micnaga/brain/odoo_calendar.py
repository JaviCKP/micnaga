"""Escritura minima de ``calendar.event`` en Odoo, **sin enviar correos**.

Una pieza pequena y aislada a proposito: la usa el ejecutor de la Secretaria («crear evento» tras
una reunion) y la puede llamar la capa de Google Calendar (que replica los eventos de Google en
Odoo con su enlace de Meet y su propia deduplicacion). Aqui no hay mas logica de calendario:

* ``event_values``: valores de Odoo a partir de fecha + hora **local** (o todo el dia).
* ``create_event`` / ``update_event``: siempre con ``NO_MAIL_CONTEXT``
  (``no_mail_to_attendees``): Odoo no manda invitaciones ni avisos a los asistentes.

Los asistentes los decide quien llama (por defecto, solo el propio usuario).
"""

from __future__ import annotations

from datetime import tzinfo
from typing import Any

from micnaga.brain.odoo import add_hours, to_utc

MODEL = "calendar.event"
NO_MAIL_CONTEXT: dict[str, Any] = {"no_mail_to_attendees": True}


def event_values(
    *,
    name: str,
    day: str,
    tz: tzinfo,
    start: str | None = None,
    duration_h: float = 1.0,
    partner_ids: list[int] | None = None,
    user_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``day`` = ``YYYY-MM-DD``; ``start`` = ``HH:MM`` local (sin hora: evento de todo el dia)."""
    values: dict[str, Any] = {"name": name, "partner_ids": [(6, 0, list(partner_ids or []))]}
    if user_id is not None:
        values["user_id"] = user_id
    if start:
        local = f"{day} {start}"
        values["start"] = to_utc(local, tz)
        values["stop"] = to_utc(add_hours(local, duration_h), tz)
    else:
        values.update(allday=True, start=f"{day} 08:00:00", stop=f"{day} 18:00:00")
    values.update(extra or {})
    return values


def create_event(client: Any, values: dict[str, Any]) -> int:
    return int(client.create(MODEL, values, context=NO_MAIL_CONTEXT))


def update_event(client: Any, event_id: int, values: dict[str, Any]) -> bool:
    return bool(client.write(MODEL, [event_id], values, context=NO_MAIL_CONTEXT))
