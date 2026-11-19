"""«Corregir con un mensaje»: una propuesta (o la lista entera) revisada por la IA.

El usuario escribe «mejor para el jueves y en el proyecto RegulAI» sobre una propuesta y pulsa
Intro. Micnaga lanza un ``claude -p`` corto y de **solo lectura**:

* entrada: el mensaje, la propuesta (lo que el modelo ve al proponer), sus campos editables, las
  opciones ya conocidas (proyectos, destinos, etapas), la fecha de hoy, el acta y el fragmento de
  la reunion alrededor de la prueba;
* las mismas herramientas de lectura que el analisis (servidor ``micnaga`` con lo que permite el
  perfil de Odoo y, si Google esta activado, solo sus lecturas, con la misma lista blanca);
* ``--json-schema`` limitado a ese tipo: solo los campos editables de ese item.

Lo que devuelve se filtra otra vez en Python (claves de ese tipo, correos que ya se vieron) y pasa
por ``Executor.revise`` -> ``Executor.replace`` (misma validacion que una edicion; mismo id;
``revision`` sube; nunca activa invitados ni envia). «Deshacer» vuelve a la version anterior.

La lista entera (``plan``): ``cambiar`` / ``quitar`` / ``anadir``. Lo nuevo sale **sin marcar** y
pasa por la misma validacion que el analisis (``plan.build_crm`` y los constructores de Google);
lo quitado queda «descartado»; un «Deshacer» devuelve la lista como estaba.

Una sola ejecucion (``run_once``): un reintento ciego duplicaria la espera. Medido en
``DECISIONS.md`` §22.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from micnaga.brain import acta as acta_io
from micnaga.brain import plan
from micnaga.brain import schema as sc
from micnaga.brain.claude import ClaudeRequest, ClaudeResult, ToolCall
from micnaga.brain.executor import ApplyError, Executor
from micnaga.brain.prompt import WEEKDAYS, segment_text, system_prompt
from micnaga.config import Config
from micnaga.pipeline import output
from micnaga.pipeline.output import format_mmss
from micnaga.types import Transcript

log = logging.getLogger(__name__)

MODEL = "sonnet"
MAX_TURNS = 6
"""Una ronda de consultas en paralelo + la salida estructurada, con margen."""
TIMEOUT_S = 90.0
CONTEXT_SEGS = 8
MAX_MESSAGE = 400
MAX_OPTIONS = 60

StatusCallback = Callable[[dict[str, Any]], None]


class RefineError(RuntimeError):
    """Algo que se le dice al usuario en una linea (``kind``) y, si el modelo lo explico,
    su ``note`` («No puedo asignar tareas a otras personas»)."""

    def __init__(self, kind: str, note: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.note = note


# ============================================================================== esquemas

_DATOS: dict[str, Any] = sc.DATOS_PROPS
_EXTRA = {
    "cuerpo": {"type": ["string", "null"], "description": "texto de la nota"},
    "tipo_actividad_id": {"type": ["integer", "null"]},
}
ODOO_KEYS: dict[str, tuple[str, ...]] = {
    "nota_en_historial": ("tarea_id", "proyecto_id", "oportunidad_id", "partner_id", "cuerpo"),
    "crear_tarea": ("proyecto_id", "nombre", "fecha", "descripcion"),
    "programar_actividad": (
        "tarea_id",
        "proyecto_id",
        "oportunidad_id",
        "partner_id",
        "resumen",
        "fecha",
        "tipo_actividad_id",
    ),
    "crear_evento": (
        "nombre",
        "fecha",
        "hora_inicio",
        "duracion_h",
        "asistentes_ids",
        "tarea_id",
        "proyecto_id",
    ),
    "registrar_horas": ("proyecto_id", "tarea_id", "fecha", "horas", "descripcion"),
    "crear_gasto": ("nombre", "importe", "fecha"),
    "crear_contacto": ("nombre", "email", "telefono", "cargo", "empresa_id"),
    "vincular_a_oportunidad": ("oportunidad_id", "cuerpo"),
    "actualizar_oportunidad": ("oportunidad_id", "etapa_id", "importe", "fecha_cierre"),
}
CORRECTION_KEYS = ("a",)
_STR = {"type": ["string", "null"]}


def _google_prop(spec: dict[str, Any]) -> dict[str, Any]:
    t = spec["tipo"]
    if t == "number":
        return {"type": ["number", "null"]}
    if t == "datetime":
        return {"type": "string", "description": "YYYY-MM-DDTHH:MM (hora local)"}
    if t == "choice":
        return {"type": "string", "enum": list(spec.get("opciones_fijas") or [])}
    if t == "emails":
        return {"type": "array", "items": {"type": "string"}}
    if t == "people":
        return {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"nombre": _STR, "email": {"type": "string"}},
                "required": ["nombre", "email"],
                "additionalProperties": False,
            },
        }
    return _STR


def google_specs(kind: str) -> list[dict[str, Any]]:
    from micnaga.brain.google import items

    return list(items.SCHEMAS.get(kind, []))


def editable_keys(p: dict[str, Any]) -> tuple[str, ...]:
    kind = str(p.get("tipo"))
    if p.get("grupo") == "correcciones":
        return CORRECTION_KEYS
    if p.get("grupo") == "google":
        return tuple(s["clave"] for s in google_specs(kind))
    return ODOO_KEYS.get(kind, ())


def datos_props(p: dict[str, Any]) -> dict[str, Any]:
    """Propiedades JSON de los campos que el modelo puede tocar en este item."""
    kind = str(p.get("tipo"))
    if p.get("grupo") == "correcciones":
        return {"a": {"type": "string", "description": "la forma correcta, sin cifras"}}
    if p.get("grupo") == "google":
        return {s["clave"]: _google_prop(s) for s in google_specs(kind)}
    out: dict[str, Any] = {}
    for k in ODOO_KEYS.get(kind, ()):
        if k == "fecha" and kind == "registrar_horas":
            out[k] = {"type": ["string", "null"], "description": "YYYY-MM-DD (un solo dia)"}
        else:
            out[k] = _EXTRA.get(k) or _DATOS[k]
    return out


def item_schema(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "cambiado": {"type": "boolean"},
            "nota": {"type": "string", "description": "vacia o una frase corta"},
            "datos": {
                "type": "object",
                "description": "solo los campos que cambian",
                "properties": datos_props(p),
                "additionalProperties": False,
            },
        },
        "required": ["cambiado", "nota", "datos"],
        "additionalProperties": False,
    }


GOOGLE_ADD = ("evento_google", "correo_google", "acta_drive")
_GOOGLE_ADD_PROPS = {
    "titulo": _STR,
    "inicio": {"type": ["string", "null"], "description": "YYYY-MM-DDTHH:MM"},
    "duracion_min": {"type": ["integer", "null"]},
    "asistentes": {"type": ["array", "null"], "items": {"type": "string"}},
    "para": {"type": ["array", "null"], "items": {"type": "string"}},
    "asunto": _STR,
}


def _union_props() -> dict[str, Any]:
    props: dict[str, Any] = {**_DATOS, **_EXTRA, "a": _STR}
    from micnaga.brain.google import items

    for kind in items.SCHEMAS:
        for s in items.SCHEMAS[kind]:
            prop = _google_prop(s)
            if s["clave"] in props:
                continue
            if isinstance(prop.get("type"), str):  # en la union todo puede faltar
                prop = {**prop, "type": [prop["type"], "null"]}
                prop.pop("enum", None)
            props[s["clave"]] = prop
    props.update({k: v for k, v in _GOOGLE_ADD_PROPS.items() if k not in props})
    return props


def plan_schema(ids: list[str], kinds: list[str]) -> dict[str, Any]:
    union = {"type": "object", "properties": _union_props(), "additionalProperties": False}
    id_prop: dict[str, Any] = {"type": "string", "enum": ids} if ids else {"type": "string"}
    add_kind: dict[str, Any] = {"type": "string", "enum": kinds} if kinds else {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "nota": {"type": "string"},
            "cambiar": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": id_prop, "datos": union},
                    "required": ["id", "datos"],
                    "additionalProperties": False,
                },
            },
            "quitar": {"type": "array", "items": id_prop},
            "anadir": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "tipo": add_kind,
                        "titulo": {"type": "string"},
                        "motivo": {"type": "string"},
                        "seg": {"type": ["integer", "null"]},
                        "datos": union,
                    },
                    "required": ["tipo", "titulo", "motivo", "seg", "datos"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["nota", "cambiar", "quitar", "anadir"],
        "additionalProperties": False,
    }


# ============================================================================== entrada


def clean_message(text: object) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:MAX_MESSAGE]


def today_line(today: date) -> str:
    return f"Hoy es {WEEKDAYS[today.weekday()]} {today.isoformat()}."


def item_view(p: dict[str, Any]) -> dict[str, Any]:
    """Lo que el modelo ve de un item (la forma de su salida; sin estado interno)."""
    if p.get("grupo") in ("google", "correcciones"):
        d = {k: v for k, v in (p.get("datos") or {}).items() if k != "huecos"}
        if p.get("tipo") == "evento_google" and (p.get("datos") or {}).get("huecos"):
            d["huecos_sugeridos"] = p["datos"]["huecos"]
        return {"id": p.get("id"), "tipo": p.get("tipo"), "titulo": p.get("titulo"), "datos": d}
    raw = plan.to_input(p)
    d = {k: v for k, v in raw["datos"].items() if v not in (None, "", [])}
    for k in ("proyecto", "tarea", "nombre", "etapa", "empresa"):  # nombres legibles
        v = (p.get("datos") or {}).get(k)
        if v and k not in d:
            d[k] = v
    d.pop("fechas", None)
    if p.get("tipo") == "registrar_horas":
        d["fecha"] = (p.get("datos") or {}).get("fecha")
    return {"id": p.get("id"), "tipo": p.get("tipo"), "titulo": p.get("titulo"), "datos": d}


def options_text(acta: dict[str, Any]) -> str:
    opts = acta.get("opciones") or {}
    lines = []
    for key, label in (
        ("proyectos", "proyecto_id"),
        ("destinos", "destino (modelo:id)"),
        ("tipos_actividad", "tipo_actividad_id"),
        ("etapas", "etapa_id"),
    ):
        rows = opts.get(key) or []
        if rows:
            body = "; ".join(f"{r.get('id')} = {r.get('nombre')}" for r in rows[:MAX_OPTIONS])
            lines.append(f"- {label}: {body}")
    return "\n".join(lines)


def acta_context(acta: dict[str, Any]) -> str:
    parts = [f"Título: {acta.get('titulo') or ''}"]
    if acta.get("contexto"):
        parts.append(f"Contexto: {acta['contexto']}")
    if acta.get("resumen"):
        parts.append("Resumen: " + " ".join(acta["resumen"]))
    tasks = [t.get("texto") for t in acta.get("tareas") or [] if t.get("texto")]
    if tasks:
        parts.append("Tareas: " + "; ".join(tasks))
    return "\n".join(parts)


def transcript_slice(transcript: Transcript, seg: int | None, *, around: int = CONTEXT_SEGS) -> str:
    """Las frases alrededor de la prueba (o el final de la reunion si no la hay)."""
    n = len(transcript.segments)
    if n == 0:
        return ""
    if seg is None or not 0 <= seg < n:
        lo, hi = max(0, n - 2 * around), n
    else:
        lo, hi = max(0, seg - around), min(n, seg + around + 1)
    names = {s.id: s.name for s in transcript.speakers}
    out = []
    for i in range(lo, hi):
        s = transcript.segments[i]
        name = names.get(s.speaker, s.speaker)
        label = s.speaker if name == s.speaker else f"{s.speaker} ({name})"
        out.append(f"#{i} [{format_mmss(s.start)}] {label}: {segment_text(s)}")
    return "\n".join(out)


def field_lines(p: dict[str, Any]) -> str:
    props = datos_props(p)
    return "\n".join(f"- {k}" for k in props)


def item_input(
    p: dict[str, Any], message: str, acta: dict[str, Any], transcript: Transcript, today: date
) -> str:
    opts = options_text(acta)
    parts = [
        "# Corregir una propuesta",
        today_line(today),
        "## Mensaje del usuario",
        message,
        "## La propuesta",
        json.dumps(item_view(p), ensure_ascii=False),
        "## Campos que puedes cambiar",
        field_lines(p),
    ]
    if opts:
        parts += ["## Opciones (ids válidos)", opts]
    parts += [
        "## Acta",
        acta_context(acta),
        "## Fragmento de la reunión",
        transcript_slice(transcript, p.get("seg") if isinstance(p.get("seg"), int) else None),
    ]
    return "\n\n".join(parts) + "\n"


def plan_input(
    pending: list[dict[str, Any]],
    message: str,
    kinds: list[str],
    acta: dict[str, Any],
    transcript: Transcript,
    today: date,
) -> str:
    from micnaga.brain.prompt import render_transcript

    opts = options_text(acta)
    parts = [
        "# Cambiar la lista de propuestas",
        today_line(today),
        "## Mensaje del usuario",
        message,
        "## Propuestas pendientes",
        "\n".join(json.dumps(item_view(p), ensure_ascii=False) for p in pending) or "(ninguna)",
        "## Tipos que puedes añadir",
        ", ".join(kinds) or "(ninguno)",
        "Datos por tipo (Odoo): " + str(sc.DATOS_DESCRIPTION),
        "Google: evento_google (titulo, inicio, duracion_min, asistentes = correos), "
        "correo_google (para, asunto, cuerpo; siempre borrador), acta_drive (titulo).",
    ]
    if opts:
        parts += ["## Opciones (ids válidos)", opts]
    parts += [
        "## Acta",
        acta_context(acta),
        "## Transcripción",
        render_transcript(transcript, max_chars=60_000),
    ]
    return "\n\n".join(parts) + "\n"


# ============================================================================== herramientas


@dataclass
class Tools:
    """Herramientas de lectura para la revision (las del analisis)."""

    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    allowed: list[str] = field(default_factory=list)
    disallowed: list[str] = field(default_factory=list)
    settings: dict[str, Any] | None = None
    env: dict[str, str] = field(default_factory=dict)
    strict: bool = True
    google: list[str] = field(default_factory=list)
    odoo_types: list[str] | None = None


def read_tools(config: Config, folder: Path, today: date) -> Tools:
    from micnaga.brain import capabilities
    from micnaga.brain.claude import mcp_allowed_tools
    from micnaga.brain.mcp import EVERY_ODOO_TOOL
    from micnaga.brain.schema import PROPOSAL_TYPES
    from micnaga.brain.secretary import MCP_SERVER, extra_mcp_servers, mcp_command, mcp_env

    s = config.secretaria
    tools: list[str] | None = []
    types: list[str] | None = None
    if s.odoo_url and s.odoo_db and s.odoo_login:
        profile = capabilities.load(
            capabilities.connection_key(s.odoo_url, s.odoo_db, s.odoo_login)
        )
        tools = capabilities.tools_for(profile, EVERY_ODOO_TOOL)
        types = capabilities.actions_for(profile, PROPOSAL_TYPES)
    cmd = mcp_command()
    env = mcp_env(config, tools=tools, today=today.isoformat())
    env["MICNAGA_CURRENT"] = folder.name
    out = Tools(
        servers={MCP_SERVER: {"type": "stdio", "command": cmd[0], "args": cmd[1:], "env": env}},
        allowed=mcp_allowed_tools(MCP_SERVER),
        odoo_types=types,
    )
    extra, extra_allowed = extra_mcp_servers(config)
    out.servers.update(extra)
    out.allowed += extra_allowed
    services = _google_services(config)
    if services:
        from micnaga.brain.google import detect, policy

        pol = policy.analysis(
            services, stdio_names=list(out.servers), foreign=detect.foreign_servers()
        )
        out.allowed += pol.allowed
        out.disallowed += pol.disallowed
        out.settings = {"allowedMcpServers": pol.settings["allowedMcpServers"]}
        out.env = dict(pol.env)
        out.strict = False
        out.google = services
    return out


def _google_services(config: Config) -> list[str]:
    from micnaga.brain import google as g
    from micnaga.brain.google import detect

    services = g.enabled_services(config.secretaria)
    cached = detect.read_cache().get("services")
    if isinstance(cached, dict) and cached:
        services = [x for x in services if cached.get(x) in ("connected", "unknown", None)]
    return services


def _request(prompt: str, schema: dict[str, Any], tools: Tools, *, model: str) -> ClaudeRequest:
    return ClaudeRequest(
        prompt=prompt,
        system_prompt=system_prompt("revision"),
        model=model,
        schema=schema,
        mcp_servers=tools.servers,
        allowed_tools=tools.allowed,
        disallowed_tools=tools.disallowed,
        settings=tools.settings,
        extra_env=tools.env,
        strict_mcp=tools.strict,
        max_turns=MAX_TURNS,
        timeout_s=TIMEOUT_S,
    )


# ============================================================================== filtros


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def seen_text(result: ClaudeResult, *extra: str) -> str:
    """Todo lo que el modelo vio de verdad: resultados de herramientas + textos dados."""
    parts = [r.text for r in result.tool_results]
    parts += [json.dumps(r.structured, ensure_ascii=False) for r in result.tool_results]
    return (" ".join([*parts, *extra])).replace("\\/", "/").casefold()


def whitelist(p: dict[str, Any], datos: object, seen: str) -> dict[str, Any]:
    """Solo las claves editables de ese tipo; los correos nuevos, solo si ya se vieron (en la
    propuesta, la reunion, el mensaje del usuario o un resultado de herramienta)."""
    if not isinstance(datos, dict):
        return {}
    keys = editable_keys(p)
    out = {k: v for k, v in datos.items() if k in keys}
    for k in ("para",):
        if isinstance(out.get(k), list):
            out[k] = [e for e in out[k] if isinstance(e, str) and e.strip().casefold() in seen]
    if isinstance(out.get("asistentes"), list):
        # quien el usuario ya invito sigue invitado; nadie nuevo entra encendido
        was_on = {
            str(a.get("email")).casefold()
            for a in (p.get("datos") or {}).get("asistentes") or []
            if isinstance(a, dict) and a.get("on")
        }
        out["asistentes"] = [
            {**a, "on": str(a.get("email") or "").strip().casefold() in was_on}
            for a in out["asistentes"]
            if isinstance(a, dict) and str(a.get("email") or "").strip().casefold() in seen
        ]
    if isinstance(out.get("email"), str) and out["email"].strip().casefold() not in seen:
        out.pop("email")
    return out


# ============================================================================== revisor


@dataclass
class Outcome:
    acta: dict[str, Any]
    changed: list[str] = field(default_factory=list)
    note: str = ""
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    duration_s: float = 0.0


class Refiner:
    def __init__(
        self,
        config: Config,
        executor: Executor,
        *,
        runner: Any,
        today: date | None = None,
        model: str = MODEL,
        lock: Any = None,
    ) -> None:
        self.config = config
        self.lock = lock if lock is not None else contextlib.nullcontext()
        self.executor = executor
        self.runner = runner
        self.today = today or date.today()
        self.model = model

    @property
    def folder(self) -> Path:
        return self.executor.folder

    def _run(self, req: ClaudeRequest, status: StatusCallback) -> ClaudeResult:
        def on_tool(call: ToolCall) -> None:
            from micnaga.brain.secretary import _first_arg

            status(
                {
                    "fase": "herramienta",
                    "servidor": call.server,
                    "tool": call.tool,
                    "arg": _first_arg(call.input),
                }
            )

        result: ClaudeResult = self.runner.run_once(req, on_event=on_tool)
        return result

    # ------------------------------------------------------------------ un item

    def item(self, pid: str, message: str, *, on_status: StatusCallback | None = None) -> Outcome:
        status = on_status or (lambda _s: None)
        text = clean_message(message)
        if not text:
            raise RefineError("empty")
        acta = acta_io.read_acta(self.folder)
        if acta is None:
            raise RefineError("no_acta")
        p = next((x for x in acta.get("propuestas") or [] if x.get("id") == pid), None)
        if p is None or p.get("estado") != "pendiente":
            raise RefineError("not_editable")
        if p.get("grupo") not in ("google", "correcciones") and not self.config.secretaria.odoo_url:
            raise RefineError("odoo_unavailable")
        revision = int(p.get("revision") or 0)
        transcript = output.read_transcript(self.folder)
        status({"fase": "pensando"})
        tools = read_tools(self.config, self.folder, self.today)
        req = _request(
            item_input(p, text, acta, transcript, self.today),
            item_schema(p),
            tools,
            model=self.model,
        )
        result = self._run(req, status)
        data = result.structured or {}
        note = sc.clean_text(data.get("nota"), 160)
        blob = " ".join(s.text for s in transcript.segments)
        seen = seen_text(result, blob, text, json.dumps(p.get("datos"), ensure_ascii=False))
        changes = whitelist(p, data.get("datos"), seen) if data.get("cambiado") else {}
        if not changes:
            raise RefineError("declined", note)
        status({"fase": "comprobando"})
        try:
            with self.lock:
                acta = self.executor.revise(pid, changes, expect_revision=revision, typed=text)
        except ApplyError as exc:
            raise RefineError(str(exc), note) from exc
        except ValueError as exc:  # LineError de una linea de horas: motivo exacto
            raise RefineError(f"horas_{exc}", note) from exc
        new = next(x for x in acta["propuestas"] if x.get("id") == pid)
        return Outcome(acta, list(new.get("cambiados") or []), note, duration_s=result.duration_s)

    # ------------------------------------------------------------------ la lista

    def plan(self, message: str, *, on_status: StatusCallback | None = None) -> Outcome:
        status = on_status or (lambda _s: None)
        text = clean_message(message)
        if not text:
            raise RefineError("empty")
        acta = acta_io.read_acta(self.folder)
        if acta is None:
            raise RefineError("no_acta")
        before = copy.deepcopy(acta.get("propuestas") or [])
        pending = [p for p in before if p.get("estado") == "pendiente"]
        revisions = {str(p["id"]): int(p.get("revision") or 0) for p in pending}
        transcript = output.read_transcript(self.folder)
        status({"fase": "pensando"})
        tools = read_tools(self.config, self.folder, self.today)
        kinds = self._addable(tools)
        req = _request(
            plan_input(pending, text, kinds, acta, transcript, self.today),
            plan_schema(list(revisions), kinds),
            tools,
            model=self.model,
        )
        result = self._run(req, status)
        data = result.structured or {}
        note = sc.clean_text(data.get("nota"), 160)
        status({"fase": "comprobando"})
        blob = " ".join(s.text for s in transcript.segments)
        seen = seen_text(
            result, blob, text, json.dumps([p.get("datos") for p in pending], ensure_ascii=False)
        )
        with self.lock:
            return self._commit_plan(
                data, note, pending, revisions, before, kinds, transcript, result, text, seen
            )

    def _commit_plan(
        self,
        data: dict[str, Any],
        note: str,
        pending: list[dict[str, Any]],
        revisions: dict[str, int],
        before: list[dict[str, Any]],
        kinds: list[str],
        transcript: Transcript,
        result: ClaudeResult,
        text: str,
        seen: str,
    ) -> Outcome:
        changed: list[str] = []
        failed = 0
        for ch in sc.as_list(data.get("cambiar")):
            pid = str((ch or {}).get("id"))
            p = next((x for x in pending if x.get("id") == pid), None)
            if p is None:
                continue
            changes = whitelist(p, ch.get("datos"), seen)
            if not changes:
                continue
            try:
                self.executor.revise(pid, changes, expect_revision=revisions[pid], typed=text)
                changed.append(pid)
            except (ApplyError, ValueError) as exc:
                failed += 1
                log.info("plan change for %s rejected: %s", pid, exc)
        acta = acta_io.read_acta(self.folder) or {}
        drop = {str(x) for x in sc.as_list(data.get("quitar")) if str(x) in revisions}
        removed = []
        for p in acta.get("propuestas") or []:
            if p.get("id") in drop and p.get("estado") == "pendiente":
                p["estado"] = "descartada"
                removed.append(str(p["id"]))
        new = self._build_new(data.get("anadir"), kinds, acta, transcript, result, text)
        acta["propuestas"] = [*(acta.get("propuestas") or []), *new]
        if not (changed or removed or new):
            raise RefineError("declined", note)
        acta["plan_previo"] = {"propuestas": before, "fecha": self.today.isoformat()}
        acta_io.write_acta(self.folder, acta)
        if failed and not note:
            note = "rejected"
        return Outcome(
            acta,
            changed,
            note,
            added=[str(p["id"]) for p in new],
            removed=removed,
            duration_s=result.duration_s,
        )

    def _addable(self, tools: Tools) -> list[str]:
        kinds: list[str] = []
        s = self.config.secretaria
        if s.odoo_url:
            from micnaga.brain.schema import PROPOSAL_TYPES

            kinds += list(tools.odoo_types if tools.odoo_types is not None else PROPOSAL_TYPES)
        if "calendar" in tools.google:
            kinds.append("evento_google")
            kinds = [k for k in kinds if k != "crear_evento"]  # todo evento pasa por Google
        if "gmail" in tools.google and s.google_email_mode != "off":
            kinds.append("correo_google")
        if "drive" in tools.google:
            kinds.append("acta_drive")
        return kinds

    def _build_new(
        self,
        raw: object,
        kinds: list[str],
        acta: dict[str, Any],
        transcript: Transcript,
        result: ClaudeResult,
        message: str,
    ) -> list[dict[str, Any]]:
        """Propuestas nuevas: misma validacion que el analisis; **siempre sin marcar**."""
        items = [x for x in sc.as_list(raw, 10) if isinstance(x, dict) and x.get("tipo") in kinds]
        odoo_raw = [x for x in items if x["tipo"] not in GOOGLE_ADD]
        google_raw = [x for x in items if x["tipo"] in GOOGLE_ADD]
        out: list[dict[str, Any]] = []
        if odoo_raw:
            out += self._new_odoo(odoo_raw, transcript)
        if google_raw:
            out += self._new_google(google_raw, acta, transcript, result, message)
        counter = _max_id(acta)
        for p in out:
            counter += 1
            p["id"] = f"p{counter}"
            p["estado"] = "pendiente"
            p["checked"] = False
            p["nueva"] = True
            p["revision"] = 0
        acta["ultimo_id"] = counter
        return out

    def _new_odoo(self, raw: list[dict[str, Any]], transcript: Transcript) -> list[dict[str, Any]]:
        from micnaga.brain import capabilities

        try:
            o = self.executor.odoo()
            o.authenticate()
        except Exception as exc:
            log.info("plan adds skipped: odoo unavailable (%s)", getattr(exc, "kind", exc))
            return []
        if hasattr(o, "read_only"):
            o.read_only = True
        try:
            try:
                profile = capabilities.ensure(o)
            except Exception:
                profile = None
            fixed = []
            for x in raw:
                d = {k: v for k, v in (x.get("datos") or {}).items() if k in _DATOS or k in _EXTRA}
                fixed.append({**x, "confianza": 0.5, "datos": d})
            built: list[dict[str, Any]] = plan.build_crm(
                fixed, transcript, o, profile=profile, today=self.today
            )
            return built
        finally:
            if hasattr(o, "read_only"):
                o.read_only = False

    def _new_google(
        self,
        raw: list[dict[str, Any]],
        acta: dict[str, Any],
        transcript: Transcript,
        result: ClaudeResult,
        message: str,
    ) -> list[dict[str, Any]]:
        from micnaga.brain.google import analysis

        run = analysis.prepare(self.config, self.folder)
        if run is None:
            return []
        known = analysis._known_emails(
            analysis._seen_text(result) + " " + message, transcript, run.context
        )
        n = len(transcript.segments)
        out: list[dict[str, Any]] = []
        for x in raw:
            d = x.get("datos") or {}
            kind = x["tipo"]
            p: dict[str, Any] | None = None
            if kind == "evento_google":
                people = [{"nombre": None, "email": e} for e in d.get("asistentes") or []]
                p = analysis._event(
                    {
                        "titulo": d.get("titulo") or x.get("titulo"),
                        "inicio": d.get("inicio"),
                        "hora_acordada": bool(d.get("inicio")),
                        "huecos": [],
                        "duracion_min": d.get("duracion_min"),
                        "asistentes": people,
                        "seg": x.get("seg"),
                    },
                    known,
                    acta,
                    run,
                    transcript,
                    n,
                )
            elif kind == "correo_google":
                p = analysis._mail(
                    {"para": d.get("para"), "asunto": d.get("asunto"), "cuerpo": d.get("cuerpo")},
                    known,
                    acta,
                )
            elif kind == "acta_drive":
                p = analysis._drive(acta, transcript, run.folder_id)
                title = sc.clean_text(d.get("titulo"), 160)
                if title:
                    p["datos"]["titulo"] = title
                    analysis.refresh(p)
            if p is not None:
                out.append(p)
        return out

    # ------------------------------------------------------------------ deshacer

    def undo_plan(self) -> dict[str, Any]:
        """La lista vuelve a como estaba antes del ultimo mensaje (si no se aplico nada desde
        entonces). Los items que vuelven suben de ``revision``."""
        acta = acta_io.read_acta(self.folder)
        if acta is None:
            raise RefineError("no_acta")
        prev = (acta.get("plan_previo") or {}).get("propuestas")
        if not isinstance(prev, list):
            raise RefineError("not_editable")
        now = {p.get("id"): p for p in acta.get("propuestas") or []}
        for p in now.values():
            if p.get("estado") not in ("pendiente", "descartada"):
                old = next((x for x in prev if x.get("id") == p.get("id")), None)
                if old is None or old.get("estado") != p.get("estado"):
                    raise RefineError("not_editable")  # algo se aplico desde entonces
        restored = []
        for old in prev:
            cur = now.get(old.get("id"))
            item = dict(old)
            if cur is not None and cur.get("revision") != old.get("revision"):
                item["revision"] = int(cur.get("revision") or 0) + 1
            restored.append(item)
        acta["propuestas"] = restored
        acta.pop("plan_previo", None)
        acta_io.write_acta(self.folder, acta)
        return acta


def _max_id(acta: dict[str, Any]) -> int:
    top = int(acta.get("ultimo_id") or 0)
    for p in acta.get("propuestas") or []:
        m = re.fullmatch(r"p(\d+)", str(p.get("id") or ""))
        if m:
            top = max(top, int(m.group(1)))
    for p in (acta.get("plan_previo") or {}).get("propuestas") or []:
        m = re.fullmatch(r"p(\d+)", str(p.get("id") or ""))
        if m:
            top = max(top, int(m.group(1)))
    return top
