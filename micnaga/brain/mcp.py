"""Servidor MCP (stdio, JSON-RPC 2.0 por lineas) con herramientas de **solo lectura**.

``python -m micnaga.brain.mcp`` (o ``micnaga tools`` / ``micnaga-cli tools`` congelado). Lo lanza
``claude -p`` con ``--mcp-config``; Micnaga solo autoriza ``mcp__micnaga__*``.

Herramientas: ``buscar_contactos``, ``buscar_empresas``, ``buscar_oportunidades``,
``leer_oportunidad``, ``listar_etapas``, ``historial_reciente``, ``actividades_pendientes``
(Odoo, si esta configurado) y ``buscar_en_reuniones`` (biblioteca local: FTS de frases y actas).

* La clave de Odoo la lee este proceso del Administrador de credenciales: **nunca pasa por
  Claude** (ni por su linea de comandos ni por su entorno).
* No existe ninguna herramienta de escritura: el cliente de Odoo solo se usa con lecturas.
* Respuestas compactas en espanol, con tope de resultados y de tamano.
* Sale al cerrarse stdin (no quedan procesos sueltos).

Configuracion por entorno (la pone ``micnaga.brain.secretary`` en el ``--mcp-config``):
``MICNAGA_ODOO_URL``, ``MICNAGA_ODOO_DB``, ``MICNAGA_ODOO_LOGIN`` (+ ``MICNAGA_HOME``,
``MICNAGA_STORAGE`` para la biblioteca).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from typing import Any, BinaryIO

from micnaga import __version__

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
LOCAL_TOOLS = ("buscar_en_reuniones",)
ALL_TOOLS = (*ODOO_TOOLS, *LOCAL_TOOLS)


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
                "modelo": {"type": "string", "enum": ["crm.lead", "res.partner"]},
                "id": {"type": "integer"},
            },
            ["modelo", "id"],
        ),
    },
    "actividades_pendientes": {
        "description": "Actividades pendientes (mail.activity) de una oportunidad o contacto.",
        "inputSchema": _schema(
            {
                "modelo": {"type": "string", "enum": ["crm.lead", "res.partner"]},
                "id": {"type": "integer"},
            },
            ["id"],
        ),
    },
    "buscar_en_reuniones": {
        "description": "Busca en las reuniones anteriores del usuario (frases y actas).",
        "inputSchema": _schema({"texto": {"type": "string"}}, ["texto"]),
    },
}


class ToolError(Exception):
    pass


class Tools:
    """Implementacion de las herramientas (inyectable en tests)."""

    def __init__(self, odoo: Any = None, db_factory: Callable[[], Any] | None = None) -> None:
        self.odoo = odoo
        self._db_factory = db_factory

    def names(self) -> list[str]:
        return [*(ODOO_TOOLS if self.odoo is not None else ()), *LOCAL_TOOLS]

    def call(self, name: str, args: dict[str, Any]) -> Any:
        if name not in self.names():
            raise ToolError(f"herramienta desconocida: {name}")
        if name == "buscar_en_reuniones":
            return self.buscar_en_reuniones(_str(args, "texto"))
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
        raise ToolError(name)  # pragma: no cover

    def buscar_en_reuniones(self, text: str) -> Any:
        if self._db_factory is None:
            return []
        from micnaga.brain import store

        db = self._db_factory()
        try:
            store.ensure_schema(db)
            hits = db.search(text, 10)
            actas = store.search_actas(db, text, 5)
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
    if m not in ("crm.lead", "res.partner"):
        raise ToolError("modelo debe ser crm.lead o res.partner")
    return str(m)


# ============================================================================== protocolo


class Server:
    def __init__(self, tools: Tools) -> None:
        self.tools = tools

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        mid = msg.get("id")
        if mid is None:  # notificacion
            return None
        try:
            result = self._dispatch(str(method), msg.get("params") or {})
        except _RpcError as exc:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": exc.code, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            client = params.get("protocolVersion")
            return {
                "protocolVersion": client if isinstance(client, str) else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": "Herramientas de solo lectura de Micnaga (Odoo y biblioteca).",
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "tools": [
                    {"name": n, **TOOL_SPECS[n], "annotations": {"readOnlyHint": True}}
                    for n in self.tools.names()
                ]
            }
        if method == "tools/call":
            name = str(params.get("name", ""))
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            try:
                data = self.tools.call(name, args)
                text = json.dumps(data, ensure_ascii=False, default=str)
                if len(text) > MAX_RESULT_CHARS:
                    text = text[:MAX_RESULT_CHARS] + "…(recortado)"
                return {"content": [{"type": "text", "text": text}], "isError": False}
            except ToolError as exc:
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
        if method in ("resources/list", "prompts/list"):
            return {method.split("/")[0]: []}
        raise _RpcError(-32601, f"method not found: {method}")


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def serve(tools: Tools, stdin: BinaryIO, stdout: BinaryIO) -> None:
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

    odoo = None
    try:
        odoo = client_from_config(
            os.environ.get("MICNAGA_ODOO_URL", ""),
            os.environ.get("MICNAGA_ODOO_DB", ""),
            os.environ.get("MICNAGA_ODOO_LOGIN", ""),
        )
    except Exception:
        log.exception("odoo client unavailable")

    def db_factory() -> Any:
        from micnaga.store.db import open_database

        return open_database()

    return Tools(odoo, db_factory)


def run(argv: list[str] | None = None) -> int:
    """Punto de entrada (``micnaga tools``)."""
    try:
        from micnaga.logging_setup import setup_logging

        setup_logging("tools")
    except Exception:
        pass
    serve(tools_from_env(), sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
