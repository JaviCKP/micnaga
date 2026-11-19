"""Escrituras de Google aprobadas (tras «Aplicar») y su «Deshacer».

Por cada propuesta marcada del grupo ``google``:

1. La entrada exacta de la herramienta se construye **aqui, en Python**, desde los ``datos`` ya
   editados y validados por el usuario (nunca desde la salida del modelo):
   ``addGoogleMeetUrl`` siempre ``true``; ``attendees`` solo los marcados;
   ``notificationLevel`` = ``ALL`` si hay alguno marcado y ``NONE`` si no; correo como borrador
   (``create_draft`` con firma) salvo que el usuario elija «Enviar» (``send_message``).
2. Idempotencia (``store.google_writes``): ``intent`` antes de lanzar, ``done`` con ids y
   enlaces. Si una ejecucion anterior quedo a medias (``intent``/``unknown``), antes de repetir
   se busca la marca ``ref`` de Micnaga en Calendar; ademas el recibo de un solo uso del
   guardian impide repetir una llamada ya permitida.
3. ``calls.run_tool``: ``claude -p`` con SOLO esa herramienta y el guardian ``PreToolUse``.
4. Eventos: espejo en Odoo (``mirror``), salvo sincronizacion nativa. Un fallo del espejo no
   deshace el evento: queda anotado en la propuesta.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from micnaga.brain import acta as acta_io
from micnaga.brain.executor import ApplyError
from micnaga.brain.google import calls, detect, mirror, store

log = logging.getLogger(__name__)


class GoogleApplyError(ApplyError):
    """``str(exc)`` es la clave de error que ve la interfaz (``google_denied``...)."""


def _local(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def ref_of(key: str) -> str:
    return "micnaga-" + key[1:13]


# ============================================================================== entradas


def event_input(d: dict[str, Any], key: str) -> dict[str, Any]:
    start = datetime.fromisoformat(str(d["inicio"]))
    end = start + timedelta(minutes=int(d.get("duracion_min") or 30))
    invited = [a for a in d.get("asistentes") or [] if a.get("on")]
    text = str(d.get("descripcion") or "").strip()
    desc = "<br>".join(html.escape(line) for line in text.splitlines())
    desc += f'<p style="color:#888">Micnaga · ref {ref_of(key)}</p>'
    out: dict[str, Any] = {
        "summary": str(d["titulo"]),
        "startTime": _local(start),
        "endTime": _local(end),
        "addGoogleMeetUrl": True,
        "description": desc,
        "notificationLevel": "ALL" if invited else "NONE",
    }
    if invited:
        out["attendees"] = [
            {"email": a["email"], **({"displayName": a["nombre"]} if a.get("nombre") else {})}
            for a in invited
        ]
    cal = str(d.get("calendario") or "primary")
    if cal != "primary":
        out["calendarId"] = cal
    return out


def mail_input(d: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    tool = "send_message" if d.get("modo") == "send" else "create_draft"
    return tool, {
        "to": list(d.get("para") or []),
        "subject": str(d.get("asunto") or ""),
        "body": str(d.get("cuerpo") or ""),
        "includeSignature": True,
    }


def drive_input(d: dict[str, Any], acta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "title": str(d.get("titulo") or "Acta"),
        "textContent": acta_document(acta),
        "contentMimeType": "text/html",
    }
    if d.get("carpeta"):
        out["parentId"] = str(d["carpeta"])
    return out


def acta_document(acta: dict[str, Any]) -> str:
    e = html.escape
    body = [f"<h1>{e(acta.get('titulo') or 'Acta')}</h1>", acta_io.render_note_html(acta)]
    links = acta.get("enlaces") or []
    if links:
        body.append("<h3>Enlaces</h3><ul>")
        body += [f'<li><a href="{e(x["url"])}">{e(x["titulo"])}</a></li>' for x in links]
        body.append("</ul>")
    return "<html><body>" + "".join(body) + "</body></html>"


# ============================================================================== resultados


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return None


def event_links(s: dict[str, Any]) -> dict[str, Any]:
    ev = s.get("event") if isinstance(s.get("event"), dict) else s
    assert isinstance(ev, dict)
    meet = _first(ev, "conferenceUrl", "hangoutLink", "meetUrl", "googleMeetUrl")
    conf = ev.get("conferenceData")
    if not meet and isinstance(conf, dict):
        video = conf.get("videoEntryPoint")
        if isinstance(video, dict):
            meet = video.get("uri")
        for ep in conf.get("entryPoints") or []:
            if not meet and isinstance(ep, dict) and ep.get("entryPointType") == "video":
                meet = ep.get("uri")
    return {"event_id": ev.get("id"), "html_link": ev.get("htmlLink"), "meet": meet}


def mail_links(s: dict[str, Any]) -> dict[str, Any]:
    msg = s.get("message") if isinstance(s.get("message"), dict) else {}
    assert isinstance(msg, dict)
    return {
        "id": s.get("id"),
        "message_id": msg.get("id"),
        "thread_id": s.get("threadId") or msg.get("threadId"),
        "url": s.get("viewUrl") or msg.get("viewUrl"),
    }


def file_links(s: dict[str, Any]) -> dict[str, Any]:
    f = s.get("file") if isinstance(s.get("file"), dict) else s
    assert isinstance(f, dict)
    return {"id": f.get("id"), "url": _first(f, "viewUrl", "webViewLink", "alternateLink")}


# ============================================================================== aplicar


DbFactory = Callable[[], Any]
OdooGetter = Callable[[], Any]


class GoogleApplier:
    def __init__(
        self,
        folder: Path,
        *,
        runner: Any,
        db_factory: DbFactory,
        odoo: OdooGetter | None = None,
    ) -> None:
        self.folder = folder
        self.runner = runner
        self._db_factory = db_factory
        self._odoo = odoo

    def _db(self) -> Any:
        db = self._db_factory()
        store.ensure(db)
        return db

    def _put(self, key: str, p: dict[str, Any], op: str, tool: str, state: str, **res: Any) -> None:
        db = self._db()
        try:
            store.put(
                db,
                key,
                folder=self.folder.name,
                proposal=str(p.get("id")),
                op=op,
                tool=tool,
                state=state,
                result=res or None,
            )
        finally:
            db.close()

    def _call(
        self, p: dict[str, Any], op: str, service: str, tool: str, tool_input: dict[str, Any]
    ) -> calls.CallOutcome:
        key = store.write_key(self.folder.name, str(p.get("id")), op, int(p.get("revision") or 0))
        db = self._db()
        try:
            prior = store.get(db, key)
        finally:
            db.close()
        if prior is not None and prior.state == "done":
            out = calls.CallOutcome(ok=True, tool=tool, input=tool_input)
            out.extra = {"stored": prior.result, "key": key}
            return out
        if prior is not None and prior.state in ("intent", "unknown"):
            found = self._recover(service, op, tool_input, key)
            if found is not None:
                self._put(key, p, op, tool, "done", **found)
                out = calls.CallOutcome(ok=True, tool=tool, input=tool_input)
                out.extra = {"stored": found, "key": key}
                return out
            calls.forget_receipt(key)  # no hay rastro en Google: no llego a ejecutarse
        self._put(key, p, op, tool, "intent")
        try:
            outcome = calls.run_tool(
                self.runner, service, tool, tool_input, key=key, foreign=detect.foreign_servers()
            )
        except calls.GuardError:
            self._put(key, p, op, tool, "failed", error="guard")
            raise GoogleApplyError("google_guard") from None
        except Exception as exc:
            kind = str(getattr(exc, "kind", "failed"))
            state = "unknown" if kind in ("timeout", "output", "offline") else "failed"
            self._put(key, p, op, tool, state, error=kind)
            raise GoogleApplyError(f"google_{kind}") from exc
        outcome.extra["key"] = key
        if not outcome.ok:
            if outcome.error == "unguarded":
                self._put(key, p, op, tool, "done", error="unguarded")
            else:
                self._put(key, p, op, tool, "failed", error=outcome.error)
            raise GoogleApplyError(f"google_{outcome.error or 'failed'}")
        return outcome

    def _recover(
        self, service: str, op: str, tool_input: dict[str, Any], key: str
    ) -> dict[str, Any] | None:
        """¿Se ejecuto la vez anterior? Solo para crear eventos: buscar la marca ``ref``."""
        if service != "calendar" or op != "create":
            return None
        start = datetime.fromisoformat(str(tool_input["startTime"]))
        q: dict[str, Any] = {
            "fullText": ref_of(key),
            "startTime": _local(start - timedelta(days=1)),
            "endTime": _local(start + timedelta(days=1)),
            "pageSize": 5,
        }
        if tool_input.get("calendarId"):
            q["calendarId"] = tool_input["calendarId"]
        try:
            res = calls.run_tool(self.runner, "calendar", "list_events", q)
        except Exception:
            return None
        for ev in res.structured.get("events") or []:
            if isinstance(ev, dict) and ref_of(key) in str(ev.get("description") or ""):
                return event_links(ev)
        return None

    # ------------------------------------------------------------------ por tipo

    def apply(self, p: dict[str, Any], acta: dict[str, Any]) -> dict[str, Any]:
        kind = p.get("tipo")
        d = p.get("datos") or {}
        if kind == "evento_google":
            key = store.write_key(
                self.folder.name, str(p["id"]), "create", int(p.get("revision") or 0)
            )
            tool_input = event_input(d, key)
            out = self._call(p, "create", "calendar", "create_event", tool_input)
            links = out.extra.get("stored") or event_links(out.structured)
            if not links.get("event_id"):
                raise GoogleApplyError("google_no_id")
            if "stored" not in out.extra:
                self._put(out.extra["key"], p, "create", out.tool, "done", **links)
            p["resultado"] = {"url": links.get("html_link"), "meet": links.get("meet")}
            p["espejo"] = self._mirror(d, links)
            return {
                "google": "evento",
                "event_id": links["event_id"],
                "calendario": tool_input.get("calendarId"),
                "notificacion": tool_input["notificationLevel"],
            }
        if kind == "correo_google":
            tool, tool_input = mail_input(d)
            out = self._call(p, "create", "gmail", tool, tool_input)
            links = out.extra.get("stored") or mail_links(out.structured)
            if "stored" not in out.extra:
                self._put(out.extra["key"], p, "create", out.tool, "done", **links)
            p["resultado"] = {"url": links.get("url")}
            if tool == "send_message":
                return {"google": "enviado", "message_id": links.get("id")}
            return {"google": "borrador", "draft_id": links.get("id")}
        if kind == "acta_drive":
            out = self._call(p, "create", "drive", "create_file", drive_input(d, acta))
            links = out.extra.get("stored") or file_links(out.structured)
            if "stored" not in out.extra:
                self._put(out.extra["key"], p, "create", out.tool, "done", **links)
            p["resultado"] = {"url": links.get("url")}
            return {"google": "fichero", "file_id": links.get("id")}
        raise GoogleApplyError("google_unknown_type")

    def _mirror(self, d: dict[str, Any], links: dict[str, Any]) -> dict[str, Any]:
        if self._odoo is None:
            return {"estado": "sin_odoo"}
        try:
            odoo = self._odoo()
        except Exception:
            odoo = None
        if odoo is None:
            return {"estado": "sin_odoo"}
        db = self._db()
        try:
            return mirror.upsert(
                odoo,
                db,
                {
                    "event_id": links["event_id"],
                    "titulo": d.get("titulo"),
                    "inicio": d.get("inicio"),
                    "duracion_min": d.get("duracion_min"),
                    "meet": links.get("meet"),
                    "html_link": links.get("html_link"),
                    "descripcion": d.get("descripcion"),
                },
            )
        except Exception as exc:
            log.warning("odoo mirror failed: %s", getattr(exc, "kind", type(exc).__name__))
            return {"estado": "error", "error": str(getattr(exc, "kind", "failed"))}
        finally:
            db.close()

    # ------------------------------------------------------------------ deshacer

    def undo(self, p: dict[str, Any]) -> None:
        u = p.get("deshacer") or {}
        kind = u.get("google")
        if kind == "evento":
            q: dict[str, Any] = {
                "eventId": u["event_id"],
                "notificationLevel": u.get("notificacion") or "NONE",
            }
            if u.get("calendario"):
                q["calendarId"] = u["calendario"]
            self._call(p, "undo", "calendar", "delete_event", q)
            if self._odoo is not None and (p.get("espejo") or {}).get("estado") in (
                "creado",
                "actualizado",
            ):
                odoo = self._odoo()
                db = self._db()
                try:
                    mirror.remove(odoo, db, str(u["event_id"]))
                finally:
                    db.close()
            return
        if kind == "borrador":
            self._call(p, "undo", "gmail", "delete_draft", {"draftId": u["draft_id"]})
            return
        if kind == "fichero":
            self._call(p, "undo", "drive", "trash_file", {"fileId": u["file_id"]})
            return
        raise GoogleApplyError("not_undoable")  # un correo enviado no se puede deshacer
