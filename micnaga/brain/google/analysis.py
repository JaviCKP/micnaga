"""Lo que Google anade a la pasada de la Secretaria (solo lectura) y lo que sale de ella.

``prepare(config)`` -> ``GoogleRun`` o ``None`` (nada activado). ``run.extend(req)`` amplia la
peticion ``claude -p``: lecturas de Google permitidas, escrituras y servidores ajenos denegados,
lista blanca de servidores, conexion bloqueante, esquema con la seccion ``google`` y unas
instrucciones breves con limites (pocas busquedas, fragmentos cortos: tokens y latencia acotados).

``run.finish(...)`` valida lo devuelto, SIEMPRE en local:

* Si un conector no aparece ``connected`` en ``system/init``, lo suyo se descarta.
* **Enlaces**: solo URLs de Google que aparecen tal cual en los resultados de las herramientas
  (el modelo no puede inventarlas).
* **Contrastes**: solo con fuente citada; si no hay discrepancia real, no hay seccion.
* **Propuestas** (grupo ``google``): ``evento_google`` (Meet siempre; asistentes desmarcados),
  ``correo_google`` (borrador por defecto) y ``acta_drive`` (desmarcada). Los correos de los
  asistentes solo valen si se vieron (evento de Calendar, resultados de Gmail o la transcripcion).
* Una propuesta de Odoo ``crear_evento`` se **convierte** en ``evento_google`` (Google es el
  calendario maestro; el espejo en Odoo lo crea ``mirror`` tras aplicar).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from micnaga.brain import google as g
from micnaga.brain import items
from micnaga.brain.google import detect, policy

log = logging.getLogger(__name__)

CONTEXT_JSON = "contexto_google.json"
MAX_LINKS = 6
MAX_CONTRASTS = 3
MAX_SLOTS = 3
EXTRA_TURNS = 12
GROUP = "google"
TYPES = ("evento_google", "correo_google", "acta_drive")
_URL = re.compile(r"https://[^\s\"'<>\\)]+")
_EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_GOOGLE_HOSTS = (
    "https://mail.google.com/",
    "https://docs.google.com/",
    "https://drive.google.com/",
    "https://calendar.google.com/",
    "https://www.google.com/calendar/",
)


# ============================================================================== contexto


def read_context(folder: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((folder / CONTEXT_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def context_text(ctx: dict[str, Any] | None) -> str:
    if not ctx or not ctx.get("titulo"):
        return ""
    lines = [f"Evento de Calendar de esta reunión: «{ctx['titulo']}»."]
    people = [
        f"{p.get('nombre') or p.get('email')} <{p.get('email')}>"
        for p in ctx.get("asistentes") or []
        if isinstance(p, dict) and p.get("email")
    ]
    if people:
        lines.append(
            "Asistentes invitados (candidatos para nombrar hablantes y para buscar contactos "
            "en Odoo): " + "; ".join(people[:15]) + "."
        )
    return "\n".join(lines)


# ============================================================================== esquema


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


_S = {"type": "string"}
_SN = {"type": ["string", "null"]}
_SEG = {"type": ["integer", "null"]}
_PERSON = _obj({"nombre": _SN, "email": _S})


def google_schema(services: list[str], *, email: bool) -> dict[str, Any]:
    props: dict[str, Any] = {}
    if "gmail" in services or "drive" in services:
        props["enlaces"] = {
            "type": "array",
            "maxItems": MAX_LINKS,
            "items": _obj(
                {
                    "titulo": _S,
                    "url": {"type": "string", "description": "viewUrl/htmlLink exacto"},
                    "fuente": {"type": "string", "enum": ["gmail", "drive", "calendar"]},
                }
            ),
        }
        props["contrastes"] = {
            "type": "array",
            "maxItems": MAX_CONTRASTS,
            "description": "Solo discrepancias reales entre lo dicho y lo escrito",
            "items": _obj({"dicho": _S, "escrito": _S, "fuente": _S, "url": _SN, "seg": _SEG}),
        }
    if "calendar" in services:
        props["reunion_siguiente"] = {
            "anyOf": [
                {"type": "null"},
                _obj(
                    {
                        "titulo": _S,
                        "inicio": {"type": ["string", "null"], "description": "YYYY-MM-DDTHH:MM"},
                        "hora_acordada": {"type": "boolean"},
                        "duracion_min": {"type": "integer"},
                        "huecos": {"type": "array", "items": _S, "maxItems": MAX_SLOTS},
                        "asistentes": {"type": "array", "items": _PERSON, "maxItems": 15},
                        "seg": _SEG,
                    }
                ),
            ]
        }
    if "gmail" in services and email:
        props["correo"] = {
            "anyOf": [
                {"type": "null"},
                _obj(
                    {
                        "para": {"type": "array", "items": _S, "maxItems": 10},
                        "asunto": _S,
                        "cuerpo": {"type": "string", "description": "texto plano, breve"},
                    }
                ),
            ]
        }
    return _obj(props)


# ============================================================================== instrucciones


def brief(services: list[str], *, today: str, email: bool, ctx: dict[str, Any] | None) -> str:
    names = {"gmail": "Gmail", "calendar": "Google Calendar", "drive": "Google Drive"}
    lines = [
        f"Conectado SOLO EN LECTURA a: {', '.join(names[s] for s in services)}. Hoy es {today}.",
        "No puedes escribir nada en Google: lo que haya que crear lo propones en `google` y el "
        "usuario lo revisa.",
    ]
    if "gmail" in services:
        lines.append(
            "Gmail, ANTES de responder: 1-3 search_threads (pageSize ≤ 5, view "
            "THREAD_VIEW_SUMMARY, newer_than:90d) con los correos de los asistentes o el nombre "
            "de la empresa; get_thread solo si un hilo trata de lo hablado (máximo 2)."
        )
    if "drive" in services:
        lines.append(
            "Drive, ANTES de responder, si se menciona un documento (presupuesto, propuesta, "
            "contrato, acta...): 1-3 search_files (fullText con palabras clave y la empresa, "
            "pageSize ≤ 5, snippetVerbosity BRIEF); read_file_content solo del que de verdad "
            "corresponda (máximo 2), para contrastar cifras, fechas y alcance."
        )
    if "gmail" in services or "drive" in services:
        lines.append(
            "`enlaces`: los correos y documentos que de verdad tratan de esta reunión, con la url "
            "EXACTA que devolvió la herramienta (viewUrl). `contrastes`: SOLO si algo dicho "
            "contradice lo escrito (importe, fecha, alcance, nombre), citando la fuente; si no, "
            "lista vacía."
        )
    if "calendar" in services:
        lines.append(
            "`reunion_siguiente`: si se acordó otra reunión. Con hora exacta dicha: `inicio` "
            "YYYY-MM-DDTHH:MM y `hora_acordada` true. Sin hora exacta: usa suggest_time con los "
            "correos de los asistentes y devuelve hasta 3 `huecos`. Asistentes: solo correos que "
            "hayas visto (evento, Gmail) o que se dijeron. Google Calendar es el calendario "
            "maestro: las reuniones NUNCA van en `propuestas` de Odoo."
        )
    if "gmail" in services and email:
        lines.append(
            "`correo`: seguimiento para los asistentes externos (no para mí): asunto y cuerpo "
            "breve en texto plano con el resumen y los acuerdos, en el tono de la reunión. Si no "
            "hay a quién escribir, null."
        )
    extra = context_text(ctx)
    if extra:
        lines.append(extra)
    return "## Google\n" + "\n".join(f"- {x}" for x in lines) + "\n"


# ============================================================================== pasada


@dataclass
class GoogleRun:
    services: list[str]
    calendar_id: str
    folder_id: str
    email: bool
    context: dict[str, Any] | None = None
    pol: policy.Policy | None = None
    connected: list[str] = field(default_factory=list)

    def extend(self, req: Any, *, today: str) -> Any:
        """Amplia una ``ClaudeRequest`` de analisis (la modifica y la devuelve)."""
        stdio = list((req.mcp_servers or {}).keys())
        self.pol = policy.analysis(
            self.services, stdio_names=stdio, foreign=detect.foreign_servers()
        )
        req.allowed_tools = [*req.allowed_tools, *self.pol.allowed]
        req.disallowed_tools = [*req.disallowed_tools, *self.pol.disallowed]
        settings = dict(req.settings or {})
        settings["allowedMcpServers"] = self.pol.settings["allowedMcpServers"]
        req.settings = settings
        req.extra_env = {**req.extra_env, **self.pol.env}
        req.strict_mcp = False
        req.max_turns = req.max_turns + EXTRA_TURNS
        if req.schema is not None:
            schema = dict(req.schema)
            schema["properties"] = {
                **schema["properties"],
                "google": google_schema(self.services, email=self.email),
            }
            schema["required"] = [*schema["required"], "google"]
            req.schema = schema
        text = brief(self.services, today=today, email=self.email, ctx=self.context)
        marker = "## Transcripción"
        req.prompt = (
            req.prompt.replace(marker, text + "\n" + marker, 1)
            if marker in req.prompt
            else req.prompt + "\n" + text
        )
        return req

    def finish(
        self,
        data: dict[str, Any],
        result: Any,
        transcript: Any,
        acta: dict[str, Any],
        proposals: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Valida la seccion ``google``; rellena ``acta`` y devuelve las propuestas finales."""
        servers = getattr(result, "mcp_servers", {}) or {}
        self.connected = [s for s in self.services if servers.get(g.SERVER[s]) == "connected"]
        acta["google"] = {
            "servicios": self.connected,
            "sin_conexion": [s for s in self.services if s not in self.connected],
        }
        if self.context and self.context.get("titulo"):
            acta["google"]["evento_actual"] = {
                k: self.context.get(k) for k in ("titulo", "htmlLink", "meet_url")
            }
            acta["titulo"] = str(self.context["titulo"])[:90]
        raw = data.get("google") if isinstance(data.get("google"), dict) else {}
        assert isinstance(raw, dict)
        seen = _seen_text(result)
        n = len(transcript.segments)
        if "gmail" in self.connected or "drive" in self.connected:
            acta["enlaces"] = _links(raw.get("enlaces"), seen)
            acta["contrastes"] = _contrasts(raw.get("contrastes"), seen, transcript, n)
        known = _known_emails(seen, transcript, self.context)
        out = list(proposals)
        if "calendar" in self.connected:
            ev = _event(raw.get("reunion_siguiente"), known, acta, self, transcript, n)
            if ev is not None:
                out.append(ev)
            out = route_odoo_events(out, self, acta)
        if "gmail" in self.connected and self.email:
            mail = _mail(raw.get("correo"), known, acta)
            if mail is not None:
                out.append(mail)
        if "drive" in self.connected:
            out.append(_drive(acta, transcript, self.folder_id))
        return out


def prepare(config: Any, folder: Path | None = None) -> GoogleRun | None:
    s = config.secretaria
    services = g.enabled_services(s)
    cached = detect.read_cache().get("services")
    if isinstance(cached, dict) and cached:
        # lo que la ultima comprobacion vio desconectado no se ofrece (si no hay cache, se
        # intenta y el system/init decide)
        services = [x for x in services if cached.get(x) in ("connected", "unknown", None)]
    if not services:
        return None
    return GoogleRun(
        services=services,
        calendar_id=s.google_calendar_id or "primary",
        folder_id=s.google_drive_folder_id,
        email=s.google_email_mode != "off",
        context=read_context(folder) if folder is not None else None,
    )


# ============================================================================== validacion


def _seen_text(result: Any) -> str:
    """Todo lo que devolvieron las herramientas de Google (para validar urls y correos)."""
    chunks = []
    for r in getattr(result, "tool_results", []) or []:
        if g.service_of(r.name) is None or r.is_error:
            continue
        chunks.append(r.text)
        if r.structured:
            chunks.append(json.dumps(r.structured, ensure_ascii=False))
    return "\n".join(chunks)


def _clean(value: object, n: int = 300) -> str:
    return " ".join(value.split())[:n] if isinstance(value, str) else ""


def _url_ok(url: str, seen: str) -> bool:
    return url.startswith(_GOOGLE_HOSTS) and url in seen


def _links(raw: object, seen: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict):
            continue
        url = _clean(it.get("url"), 600)
        title = _clean(it.get("titulo"), 160)
        if not title or not _url_ok(url, seen) or any(x["url"] == url for x in out):
            continue
        src = it.get("fuente") if it.get("fuente") in ("gmail", "drive", "calendar") else None
        out.append({"titulo": title, "url": url, "fuente": src})
    return out[:MAX_LINKS]


def _contrasts(raw: object, seen: str, transcript: Any, n: int) -> list[dict[str, Any]]:
    out = []
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict):
            continue
        said, written, src = (_clean(it.get(k), 240) for k in ("dicho", "escrito", "fuente"))
        if not (said and written and src) or said.casefold() == written.casefold():
            continue
        url = _clean(it.get("url"), 600)
        seg = it.get("seg")
        seg = seg if isinstance(seg, int) and not isinstance(seg, bool) and 0 <= seg < n else None
        out.append(
            {
                "dicho": said,
                "escrito": written,
                "fuente": src,
                "url": url if _url_ok(url, seen) else None,
                "seg": seg,
                "t": round(transcript.segments[seg].start, 2) if seg is not None else None,
            }
        )
    return out[:MAX_CONTRASTS]


def _known_emails(seen: str, transcript: Any, ctx: dict[str, Any] | None) -> set[str]:
    blob = " ".join(s.text for s in transcript.segments)
    found = {e.casefold() for e in _EMAIL_IN_TEXT.findall(seen + " " + blob)}
    for p in (ctx or {}).get("asistentes") or []:
        if isinstance(p, dict) and isinstance(p.get("email"), str):
            found.add(p["email"].casefold())
    return found


def _people(raw: object, known: set[str], ctx: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    me = {
        str(p.get("email")).casefold()
        for p in (ctx or {}).get("asistentes") or []
        if isinstance(p, dict) and p.get("self")
    }
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict):
            continue
        email = _clean(it.get("email"), 120)
        if not items.EMAIL.match(email) or email.casefold() not in known:
            continue
        if email.casefold() in me or any(p["email"].casefold() == email.casefold() for p in out):
            continue
        out.append({"nombre": _clean(it.get("nombre"), 80) or None, "email": email, "on": False})
    return out


def _iso_minute(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", ""))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M")


def acta_text(acta: dict[str, Any]) -> str:
    """Descripcion del evento en texto plano (editable); se pasa a HTML al aplicar."""
    parts = []
    if acta.get("resumen"):
        parts.append(" ".join(acta["resumen"]))
    tareas = [t.get("texto", "") for t in acta.get("tareas") or [] if t.get("texto")]
    if tareas:
        parts.append("Acuerdos:\n" + "\n".join(f"• {t}" for t in tareas))
    return "\n\n".join(parts)


def _event(
    raw: object, known: set[str], acta: dict[str, Any], run: GoogleRun, transcript: Any, n: int
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = _clean(raw.get("titulo"), 160) or (acta.get("titulo") or "Seguimiento")
    start = _iso_minute(raw.get("inicio")) if raw.get("hora_acordada") else None
    slots = [s for s in (_iso_minute(x) for x in raw.get("huecos") or []) if s][:MAX_SLOTS]
    if start is None and not slots:
        return None
    dur = raw.get("duracion_min")
    dur = dur if isinstance(dur, int) and not isinstance(dur, bool) and 5 <= dur <= 600 else 30
    seg = raw.get("seg")
    seg = seg if isinstance(seg, int) and not isinstance(seg, bool) and 0 <= seg < n else None
    p: dict[str, Any] = {
        "grupo": GROUP,
        "tipo": "evento_google",
        "motivo": "",
        "confianza": 0.9 if start else 0.6,
        "seg": seg,
        "t": round(transcript.segments[seg].start, 2) if seg is not None else None,
        "checked": start is not None,
        "aviso": None if start else "hueco_sugerido",
        "datos": {
            "titulo": title,
            "inicio": start or slots[0],
            "duracion_min": dur,
            "huecos": slots,
            "asistentes": _people(raw.get("asistentes"), known, run.context),
            "descripcion": acta_text(acta),
            "calendario": run.calendar_id,
        },
    }
    refresh(p)
    return p


def _mail(raw: object, known: set[str], acta: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    to = [
        e
        for e in (_clean(x, 120) for x in raw.get("para") or [])
        if items.EMAIL.match(e) and e.casefold() in known
    ]
    subject = _clean(raw.get("asunto"), 200)
    cuerpo = raw.get("cuerpo")
    body = cuerpo.strip()[: items.MAX_LONG] if isinstance(cuerpo, str) else ""
    if not to or not subject or not body:
        return None
    p: dict[str, Any] = {
        "grupo": GROUP,
        "tipo": "correo_google",
        "motivo": "",
        "confianza": 0.8,
        "seg": None,
        "t": None,
        "checked": True,  # un borrador no sale de tu Gmail
        "aviso": None,
        "datos": {"modo": "draft", "para": to[:10], "asunto": subject, "cuerpo": body},
    }
    refresh(p)
    return p


def _drive(acta: dict[str, Any], transcript: Any, folder_id: str) -> dict[str, Any]:
    day = str(getattr(transcript.meeting, "start", ""))[:10]
    p: dict[str, Any] = {
        "grupo": GROUP,
        "tipo": "acta_drive",
        "motivo": "",
        "confianza": 0.5,
        "seg": None,
        "t": None,
        "checked": False,
        "aviso": None,
        "datos": {
            "titulo": f"Acta · {acta.get('titulo') or 'Reunión'} · {day}".strip(" ·"),
            "carpeta": folder_id or None,
        },
    }
    refresh(p)
    return p


def refresh(p: dict[str, Any]) -> None:
    """Titulo de una linea y datos derivados de una propuesta de Google (tras crear o editar)."""
    d = p.get("datos") or {}
    kind = p.get("tipo")
    if kind == "evento_google":
        invite = [a["email"] for a in d.get("asistentes") or [] if a.get("on")]
        p["invita"] = invite
        when = str(d.get("inicio") or "").replace("T", " ")
        p["titulo"] = f"{d.get('titulo')} · {when}"
    elif kind == "correo_google":
        p["titulo"] = str(d.get("asunto") or "")
        p["envia"] = d.get("modo") == "send"
    elif kind == "acta_drive":
        p["titulo"] = str(d.get("titulo") or "")


# ============================================================================== Odoo -> Google


def route_odoo_events(
    proposals: list[dict[str, Any]], run: GoogleRun, acta: dict[str, Any]
) -> list[dict[str, Any]]:
    """Cualquier evento que la Secretaria fuera a crear en Odoo (``crear_evento`` de la rama de
    Odoo: ``nombre``, ``fecha``, ``hora_inicio``, ``duracion_h``, ``asistentes_ids``) pasa por
    Google: se convierte en ``evento_google`` (con Meet) y el espejo en Odoo lo crea ``mirror``.
    Los ``asistentes_ids`` (partners de Odoo) no tienen correo aqui: quedan en ``odoo_partner_ids``
    para el espejo, sin invitar a nadie."""
    out: list[dict[str, Any]] = []
    for p in proposals:
        if p.get("tipo") != "crear_evento" or p.get("grupo") == GROUP:
            out.append(p)
            continue
        d = p.get("datos") or {}
        day = d.get("fecha")
        hour = d.get("hora_inicio") or "10:00"
        start = _iso_minute(f"{day}T{hour}") if isinstance(day, str) else None
        if start is None:
            out.append(p)
            continue
        hours = d.get("duracion_h")
        minutes = (
            round(float(hours) * 60)
            if isinstance(hours, int | float) and not isinstance(hours, bool) and hours > 0
            else 30
        )
        q: dict[str, Any] = {
            **{k: v for k, v in p.items() if k not in ("datos", "tipo", "grupo")},
            "grupo": GROUP,
            "tipo": "evento_google",
            "origen": "odoo_crear_evento",
            "datos": {
                "titulo": str(d.get("nombre") or p.get("titulo") or "Reunión")[:160],
                "inicio": start,
                "duracion_min": max(5, min(600, minutes)),
                "huecos": [],
                "asistentes": [],
                "descripcion": acta_text(acta),
                "calendario": run.calendar_id,
                "odoo_partner_ids": [
                    i for i in d.get("asistentes_ids") or [] if isinstance(i, int)
                ],
            },
        }
        refresh(q)
        out.append(q)
    return out
