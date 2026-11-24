"""Servidor MCP (stdio, JSON-RPC 2.0 por lineas) con herramientas de **solo lectura**.

``micnaga tools --library`` ofrece solo la biblioteca local de reuniones terminadas.

``python -m micnaga.brain.mcp`` (o ``micnaga tools`` / ``micnaga-cli tools`` congelado). Lo lanza
``claude -p`` con ``--mcp-config``; Micnaga solo autoriza ``mcp__micnaga__*``.

Herramientas de Odoo (si esta configurado), **filtradas por el perfil de capacidades** del
usuario (``MICNAGA_TOOLS``, ver ``micnaga.brain.capabilities``): CRM y contactos
(``buscar_contactos``, ``buscar_empresas``, ``buscar_oportunidades``, ``leer_oportunidad``,
``listar_etapas``, ``historial_reciente``, ``actividades_pendientes``) y negocio
(``mis_partes_de_horas``, ``proyectos``, ``tareas``, ``calendario``, ``mis_actividades``,
``mis_gastos``). Siempre, de la biblioteca local: ``buscar_en_reuniones`` (FTS de frases y
actas), ``reuniones_con`` (reuniones anteriores por participante o empresa) y ``leer_acta``
(el acta completa de una de ellas; ver ``micnaga.brain.research``).

* La clave de Odoo la lee este proceso del Administrador de credenciales: **nunca pasa por
  Claude** (ni por su linea de comandos ni por su entorno).
* No existe ninguna herramienta de escritura: el cliente de Odoo solo se usa con lecturas.
* Respuestas compactas en espanol, con tope de resultados y de tamano.
* Sale al cerrarse stdin (no quedan procesos sueltos).

Configuracion por entorno (la pone ``micnaga.brain.secretary`` en el ``--mcp-config``):
``MICNAGA_ODOO_URL``, ``MICNAGA_ODOO_DB``, ``MICNAGA_ODOO_LOGIN``, ``MICNAGA_TOOLS`` (lista
separada por comas; sin ella, las de CRM de siempre), ``MICNAGA_TODAY`` (solo pruebas) (+
``MICNAGA_HOME``, ``MICNAGA_STORAGE`` para la biblioteca, ``MICNAGA_CURRENT`` = la reunion en
analisis, que no cuenta como anterior). El cliente de Odoo de este proceso es
de **solo lectura**: un metodo de escritura falla antes de salir a la red.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from micnaga import __version__
from micnaga.brain.library_mcp import LibraryError, LibraryTools

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "micnaga"
MAX_RESULT_CHARS = 12_000
ODOO_TOOLS = (
    "buscar_contactos",
    "buscar_empresas",
    "buscar_oportunidades",
    "leer_oportunidad",
    "listar_etapas",
    "historial_reciente",
    "actividades_pendientes",
)
BUSINESS_TOOLS = (
    "mis_partes_de_horas",
    "proyectos",
    "tareas",
    "calendario",
    "mis_actividades",
    "mis_gastos",
)
LOCAL_TOOLS = ("buscar_en_reuniones", "reuniones_con", "leer_acta")
ALL_TOOLS = (*ODOO_TOOLS, *LOCAL_TOOLS)
EVERY_ODOO_TOOL = (*ODOO_TOOLS, *BUSINESS_TOOLS)
MAX_RANGE_DAYS = 92
RECORD_MODELS = ("crm.lead", "res.partner", "project.task", "project.project")
HISTORY_DAYS = 42


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


_TEXT = {"texto": {"type": "string", "description": "nombre, trozo de nombre, email o telefono"}}
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "buscar_contactos": {
        "description": "Busca personas en Odoo (res.partner) por nombre, email o telefono. "
        "Prueba variantes y trozos del nombre. Devuelve parecidos ordenados.",
        "inputSchema": _schema(_TEXT, ["texto"]),
    },
    "buscar_empresas": {
        "description": "Busca empresas en Odoo (res.partner con is_company).",
        "inputSchema": _schema(_TEXT, ["texto"]),
    },
    "buscar_oportunidades": {
        "description": "Busca oportunidades del CRM (crm.lead) por texto y/o por cliente.",
        "inputSchema": _schema(
            {
                "texto": {"type": "string"},
                "partner_id": {"type": "integer", "description": "empresa o contacto"},
            },
            [],
        ),
    },
    "leer_oportunidad": {
        "description": "Lee una oportunidad: etapa, importe, fecha, actividades pendientes y "
        "las ultimas notas de su historial.",
        "inputSchema": _schema({"id": {"type": "integer"}}, ["id"]),
    },
    "listar_etapas": {
        "description": "Etapas del embudo del CRM (crm.stage) con su id y orden.",
        "inputSchema": _schema({}, []),
    },
    "historial_reciente": {
        "description": "Ultimos mensajes y notas del historial de un registro.",
        "inputSchema": _schema(
            {
                "modelo": {"type": "string", "enum": list(RECORD_MODELS)},
                "id": {"type": "integer"},
            },
            ["modelo", "id"],
        ),
    },
    "actividades_pendientes": {
        "description": "Actividades pendientes (mail.activity) de una oportunidad o contacto.",
        "inputSchema": _schema(
            {
                "modelo": {"type": "string", "enum": list(RECORD_MODELS)},
                "id": {"type": "integer"},
            },
            ["id"],
        ),
    },
    "buscar_en_reuniones": {
        "description": "Busca en las reuniones anteriores del usuario (frases y actas).",
        "inputSchema": _schema({"texto": {"type": "string"}}, ["texto"]),
    },
    "reuniones_con": {
        "description": "Reuniones anteriores con una persona o empresa (por participantes, "
        "titulo o acta), las mas recientes primero: carpeta, fecha, titulo, participantes y "
        "cuantos pendientes dejo su acta.",
        "inputSchema": _schema(
            {"texto": {"type": "string", "description": "nombre de persona o empresa"}},
            ["texto"],
        ),
    },
    "leer_acta": {
        "description": "Acta completa de una reunion anterior (por su carpeta): resumen, "
        "decisiones, tareas, proximos pasos, lo que seguia abierto y las propuestas aplicadas "
        "(con el id creado en Odoo).",
        "inputSchema": _schema({"carpeta": {"type": "string"}}, ["carpeta"]),
    },
    "mis_partes_de_horas": {
        "description": "Partes de horas del usuario (account.analytic.line) entre dos fechas: "
        "horas por dia, dias laborables que FALTAN o estan incompletos, festivos, y su PATRON "
        "habitual (proyecto, horas y descripcion por franja). Usalo antes de proponer "
        "registrar_horas.",
        "inputSchema": _schema(
            {
                "desde": {"type": "string", "description": "YYYY-MM-DD (defecto: hace 30 dias)"},
                "hasta": {"type": "string", "description": "YYYY-MM-DD (por defecto, hoy)"},
            },
            [],
        ),
    },
    "proyectos": {
        "description": "Proyectos de Odoo (project.project) activos: id, nombre, cliente y si "
        "admiten partes de horas.",
        "inputSchema": _schema({"activos": {"type": "boolean"}}, []),
    },
    "tareas": {
        "description": "Tareas (project.task): por proyecto, las del usuario y/o abiertas, o "
        "buscando por texto.",
        "inputSchema": _schema(
            {
                "proyecto_id": {"type": "integer"},
                "mias": {"type": "boolean", "description": "solo las asignadas al usuario"},
                "abiertas": {"type": "boolean"},
                "texto": {"type": "string"},
            },
            [],
        ),
    },
    "calendario": {
        "description": "Eventos del calendario del usuario (calendar.event) entre dos fechas, "
        "en hora local.",
        "inputSchema": _schema(
            {"desde": {"type": "string"}, "hasta": {"type": "string"}}, ["desde", "hasta"]
        ),
    },
    "mis_actividades": {
        "description": "Actividades pendientes asignadas al usuario (mail.activity).",
        "inputSchema": _schema({}, []),
    },
    "mis_gastos": {
        "description": "Gastos recientes del usuario (hr.expense).",
        "inputSchema": _schema({}, []),
    },
}


class ToolError(Exception):
    pass


class Tools:
    """Implementacion de las herramientas (inyectable en tests)."""

    specs = TOOL_SPECS
    instructions = "Herramientas de solo lectura de Micnaga (Odoo y biblioteca)."

    def __init__(
        self,
        odoo: Any = None,
        db_factory: Callable[[], Any] | None = None,
        *,
        allowed: list[str] | None = None,
        today: date | None = None,
        root: Path | None = None,
        current: str | None = None,
    ) -> None:
        self.odoo = odoo
        self._db_factory = db_factory
        self.allowed = allowed
        self.today = today
        self.root = root
        """Carpeta de las reuniones (para ``leer_acta``)."""
        self.current = current
        """La reunion que se esta analizando: no cuenta como «anterior»."""

    def names(self) -> list[str]:
        if self.odoo is None:
            return list(LOCAL_TOOLS)
        if self.allowed is None:  # sin perfil de capacidades: las de CRM de siempre
            return [*ODOO_TOOLS, *LOCAL_TOOLS]
        return [*(t for t in EVERY_ODOO_TOOL if t in self.allowed), *LOCAL_TOOLS]

    def _today(self) -> date:
        return self.today or date.today()

    def call(self, name: str, args: dict[str, Any]) -> Any:
        if name not in self.names():
            raise ToolError(f"herramienta desconocida: {name}")
        if name == "buscar_en_reuniones":
            return self.buscar_en_reuniones(_str(args, "texto"))
        if name in ("reuniones_con", "leer_acta"):
            return self._past(name, args)
        o = self.odoo
        if name == "buscar_contactos":
            return o.search_partners(_str(args, "texto"), companies=None)
        if name == "buscar_empresas":
            return o.search_partners(_str(args, "texto"), companies=True)
        if name == "buscar_oportunidades":
            text = args.get("texto") if isinstance(args.get("texto"), str) else None
            pid = _int(args, "partner_id", required=False)
            if not text and pid is None:
                raise ToolError("indica texto o partner_id")
            return o.search_leads(text, partner_id=pid)
        if name == "leer_oportunidad":
            lead = o.read_lead(_int(args, "id"))
            return lead if lead is not None else {"error": "no existe"}
        if name == "listar_etapas":
            return o.stages()
        if name == "historial_reciente":
            return o.history(_model(args), _int(args, "id"))
        if name == "actividades_pendientes":
            return o.pending_activities(_int(args, "id"), model=_model(args))
        if name == "mis_partes_de_horas":
            return self.mis_partes_de_horas(args)
        if name == "proyectos":
            active = args.get("activos") is not False
            return o.projects(active=active)
        if name == "tareas":
            return o.tasks(
                project_id=_int(args, "proyecto_id", required=False),
                mine=args.get("mias") is not False,
                open_only=args.get("abiertas") is not False,
                text=args.get("texto") if isinstance(args.get("texto"), str) else None,
            )
        if name == "calendario":
            a, b = self._range(args, default_days=7)
            return o.calendar_events(a.isoformat(), b.isoformat())
        if name == "mis_actividades":
            return o.my_activities()
        if name == "mis_gastos":
            return o.my_expenses()
        raise ToolError(name)  # pragma: no cover

    def _range(self, args: dict[str, Any], *, default_days: int) -> tuple[date, date]:
        from micnaga.brain.timesheets import parse_day

        today = self._today()
        b = parse_day(args.get("hasta")) or today
        a = parse_day(args.get("desde")) or (b - timedelta(days=default_days))
        if a > b:
            a, b = b, a
        if (b - a).days > MAX_RANGE_DAYS:
            a = b - timedelta(days=MAX_RANGE_DAYS)
        return a, b

    def mis_partes_de_horas(self, args: dict[str, Any]) -> dict[str, Any]:
        from micnaga.brain import horas, timesheets

        today = self._today()
        a, b = self._range(args, default_days=30)
        b = min(b, today)
        history = self.odoo.timesheet_lines(
            (a - timedelta(days=HISTORY_DAYS)).isoformat(), b.isoformat()
        )
        lines = [ln for ln in history if str(ln["fecha"]) >= a.isoformat()]
        days = timesheets.analyze_days(
            lines, a, b, today=today, off_days=horas.off_days(), history=history
        )
        pattern = timesheets.learn_pattern(history)
        logged = sorted(d for d, h in timesheets.totals_by_day(history).items() if h > 0)
        return {
            "hoy": today.isoformat(),
            "desde": a.isoformat(),
            "hasta": b.isoformat(),
            "jornada_tipica": timesheets.typical_day_hours(history),
            "ultimo_dia_con_horas": logged[-1] if logged else None,
            "faltan": [d.to_dict() for d in days if d.estado == "falta"],
            "incompletos": [d.to_dict() for d in days if d.estado == "incompleto"],
            "posibles_libres": [d.fecha for d in days if d.estado in ("libre_probable", "libre")],
            "festivos": [d.to_dict() for d in days if d.estado == "festivo"],
            "horas_por_dia": {d.fecha: d.horas for d in days if d.horas and d.estado != "festivo"},
            "patron": [
                {
                    "proyecto_id": s["proyecto"]["id"],
                    "proyecto": s["proyecto"]["nombre"],
                    "tarea_id": (s["tarea"] or {}).get("id"),
                    "horas": s["horas"],
                    "descripcion": s["descripcion"],
                    "otras_descripciones": s["variantes"][1:4],
                    "dias": "".join(s["dias"]),
                }
                for s in pattern
            ],
        }

    def _past(self, name: str, args: dict[str, Any]) -> Any:
        from micnaga.brain import research

        if self.root is None or self._db_factory is None:
            return [] if name == "reuniones_con" else {"error": "sin biblioteca"}
        if name == "leer_acta":
            return research.read_acta(self.root, _str(args, "carpeta"), exclude=self.current)
        db = self._db_factory()
        try:
            return research.meetings_with(db, self.root, _str(args, "texto"), exclude=self.current)
        finally:
            db.close()

    def buscar_en_reuniones(self, text: str) -> Any:
        if self._db_factory is None:
            return []
        from micnaga.brain import store

        db = self._db_factory()
        try:
            store.ensure_schema(db)
            words = [w for w in text.split() if len(w) >= 3]
            queries = [text] + (words if len(words) > 1 else [])
            hits: list[Any] = []
            actas: list[Any] = []
            for k, q in enumerate(queries):  # todas las palabras; si nada, cada una suelta
                hits += [h for h in db.search(q, 20) if h.folder != self.current][:5]
                actas += [a for a in store.search_actas(db, q, 8) if a.folder != self.current]
                if k == 0 and (hits or actas):
                    break
            hits = list({(h.folder, h.start): h for h in hits}.values())[:10]
            actas = list({a.folder: a for a in actas}.values())[:5]
        finally:
            db.close()

        def strip(s: str) -> str:
            return s.replace("<mark>", "").replace("</mark>", "")

        return {
            "frases": [
                {"reunion": h.title, "carpeta": h.folder, "t": round(h.start), "texto": h.text}
                for h in hits
            ],
            "actas": [
                {"reunion": a.title, "carpeta": a.folder, "texto": strip(a.snippet)} for a in actas
            ],
        }


def _str(args: dict[str, Any], key: str) -> str:
    v = args.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ToolError(f"falta {key}")
    return v.strip()[:120]


def _int(args: dict[str, Any], key: str, *, required: bool = True) -> Any:
    v = args.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        if isinstance(v, str) and v.strip().isdigit():
            return int(v)
        if required:
            raise ToolError(f"falta {key} (entero)")
        return None
    return v


def _model(args: dict[str, Any]) -> str:
    m = args.get("modelo", "crm.lead")
    if m not in RECORD_MODELS:
        raise ToolError("modelo debe ser uno de: " + ", ".join(RECORD_MODELS))
    return str(m)


# ============================================================================== protocolo


class Server:
    def __init__(self, tools: Tools | LibraryTools) -> None:
        self.tools = tools

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        mid = msg.get("id")
        if mid is None:  # notificacion
            return None
        try:
            params = msg.get("params")
            if params is not None and not isinstance(params, dict):
                raise _RpcError(-32602, "invalid params")
            result = self._dispatch(str(method), params or {})
        except _RpcError as exc:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": exc.code, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            client = params.get("protocolVersion")
            capabilities = {"tools": {"listChanged": False}}
            if isinstance(self.tools, LibraryTools):
                capabilities["prompts"] = {"listChanged": False}
            return {
                "protocolVersion": client if isinstance(client, str) else PROTOCOL_VERSION,
                "capabilities": capabilities,
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": self.tools.instructions,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "tools": [
                    {"name": n, "annotations": {"readOnlyHint": True}, **self.tools.specs[n]}
                    for n in self.tools.names()
                ]
            }
        if method == "tools/call":
            name = str(params.get("name", ""))
            args = params.get("arguments")
            try:
                if isinstance(self.tools, LibraryTools) and args is not None and not isinstance(
                    args, dict
                ):
                    raise LibraryError("Los argumentos deben ser un objeto.")
                data = self.tools.call(name, args if isinstance(args, dict) else {})
                text = json.dumps(data, ensure_ascii=False, default=str)
                if len(text) > MAX_RESULT_CHARS:
                    text = text[:MAX_RESULT_CHARS] + "…(recortado)"
                return {"content": [{"type": "text", "text": text}], "isError": False}
            except (ToolError, LibraryError) as exc:
                return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            except Exception as exc:  # Odoo caido, permisos...
                kind = getattr(exc, "kind", "failed")
                log.warning("tool %s failed: %s", name, kind)
                msg = {
                    "offline": "Odoo no responde",
                    "auth": "Odoo rechazó la clave",
                    "access": "Sin permiso en Odoo",
                }.get(kind, "Error al consultar")
                return {"content": [{"type": "text", "text": msg}], "isError": True}
        if method in ("prompts/list", "prompts/get") and isinstance(self.tools, LibraryTools):
            from micnaga.brain import library_prompts

            if method == "prompts/list":
                return {"prompts": library_prompts.descriptions()}
            try:
                return library_prompts.get(params)
            except ValueError as exc:
                raise _RpcError(-32602, str(exc)) from exc
        if method in ("resources/list", "prompts/list"):
            return {method.split("/")[0]: []}
        raise _RpcError(-32601, f"method not found: {method}")


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def serve(tools: Tools | LibraryTools, stdin: BinaryIO, stdout: BinaryIO) -> None:
    server = Server(tools)
    for raw in iter(stdin.readline, b""):
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            reply: dict[str, Any] | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "parse error"},
            }
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()


def tools_from_env() -> Tools:
    from micnaga.brain.odoo import client_from_config
    from micnaga.brain.timesheets import parse_day

    odoo = None
    try:
        odoo = client_from_config(
            os.environ.get("MICNAGA_ODOO_URL", ""),
            os.environ.get("MICNAGA_ODOO_DB", ""),
            os.environ.get("MICNAGA_ODOO_LOGIN", ""),
            read_only=True,
        )
    except Exception:
        log.exception("odoo client unavailable")
    raw = os.environ.get("MICNAGA_TOOLS")
    allowed = [t for t in raw.split(",") if t] if raw is not None else None
    today = parse_day(os.environ.get("MICNAGA_TODAY"))

    def db_factory() -> Any:
        from micnaga.store.db import open_database

        return open_database()

    from micnaga import paths

    return Tools(
        odoo,
        db_factory,
        allowed=allowed,
        today=today,
        root=paths.meetings_root(),
        current=os.environ.get("MICNAGA_CURRENT") or None,
    )


def run(argv: list[str] | None = None) -> int:
    """Punto de entrada (``micnaga tools``)."""
    parser = argparse.ArgumentParser(prog="micnaga tools")
    parser.add_argument(
        "--library",
        action="store_true",
        help="ofrecer solo la biblioteca local de reuniones terminadas",
    )
    if parser.parse_args(argv or []).library:
        serve(LibraryTools(), sys.stdin.buffer, sys.stdout.buffer)
        return 0
    try:
        from micnaga.logging_setup import setup_logging

        setup_logging("tools")
    except Exception:
        pass
    serve(tools_from_env(), sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
