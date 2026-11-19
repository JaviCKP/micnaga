"""Google (Gmail, Calendar, Drive) en la Secretaria, a traves de los conectores oficiales de la
cuenta de claude.ai del usuario. **Micnaga no tiene tokens de Google**: cada llamada (lectura o
escritura) pasa por ``claude -p``.

* ``policy``: que puede ver/usar cada ``claude -p`` (lista blanca de servidores por URL,
  ``--allowedTools``/``--disallowedTools``, conexion MCP bloqueante).
* ``detect``: que servicios estan conectados (evento ``system/init`` cortado en seco; respaldo
  ``claude mcp list``), con cache.
* ``guard``: el gancho ``PreToolUse`` (``micnaga hook-guard --plan``) que solo deja pasar las
  llamadas aprobadas, con su entrada exacta.
* ``calls``: una llamada a una herramienta concreta por ``claude -p`` (lecturas puntuales y
  escrituras aprobadas).
* ``context``: el evento de Calendar en curso al empezar a grabar (titulo y asistentes).
* ``analysis``: lo que el analisis anade (lecturas, enlaces, «Contrastes», propuestas).
* ``apply`` / ``mirror`` / ``store``: escrituras tras «Aplicar», espejo en Odoo e idempotencia.

Este ``__init__`` es solo stdlib: lo importa el guardian en cada llamada.
"""

from __future__ import annotations

import re

SERVICES = ("gmail", "calendar", "drive")

SERVER = {
    "gmail": "claude.ai Gmail",
    "calendar": "claude.ai Google Calendar",
    "drive": "claude.ai Google Drive",
}
URL = {
    "gmail": "https://gmailmcp.googleapis.com/*",
    "calendar": "https://calendarmcp.googleapis.com/*",
    "drive": "https://drivemcp.googleapis.com/*",
}
PREFIX = {
    "gmail": "mcp__claude_ai_Gmail__",
    "calendar": "mcp__claude_ai_Google_Calendar__",
    "drive": "mcp__claude_ai_Google_Drive__",
}
CONNECTORS_URL = "https://claude.ai/settings/connectors"

READ = {
    "gmail": (
        "search_threads",
        "get_thread",
        "get_message",
        "list_labels",
        "list_drafts",
        "get_draft",
        "download_gmail_attachment",
    ),
    "calendar": ("list_calendars", "list_events", "search_events", "get_event", "suggest_time"),
    "drive": (
        "search_files",
        "read_file_content",
        "download_file_content",
        "get_file_metadata",
        "get_file_permissions",
        "list_recent_files",
        "list_file_revisions",
        "list_shared_drives",
        "get_shared_drive_metadata",
    ),
}
WRITE = {
    "gmail": (
        "create_draft",
        "update_draft",
        "delete_draft",
        "send_message",
        "reply",
        "forward",
        "label_message",
        "label_thread",
        "unlabel_message",
        "unlabel_thread",
        "trash_message",
        "trash_thread",
        "untrash_message",
        "untrash_thread",
        "mark_message_spam",
        "mark_thread_spam",
        "unmark_message_spam",
        "unmark_thread_spam",
        "create_label",
        "update_label",
        "delete_label",
        "update_message_labels",
        "apply_sensitive_message_label",
        "apply_sensitive_thread_label",
    ),
    "calendar": ("create_event", "update_event", "delete_event", "respond_to_event"),
    "drive": (
        "create_file",
        "copy_file",
        "update_file",
        "share_file",
        "trash_file",
        "create_shared_drive",
        "update_shared_drive",
    ),
}

# Lo que el analisis usa de verdad (el resto de lecturas se quita de la sesion: menos tokens).
ANALYSIS_READ = {
    "gmail": ("search_threads", "get_thread"),
    "calendar": ("list_events", "search_events", "get_event", "suggest_time"),
    "drive": ("search_files", "read_file_content", "get_file_metadata"),
}

OTHER_CLAUDE_AI = ("mcp__claude_ai_Claude_Docs",)
"""Conectores de claude.ai que no son de Google: nunca se permiten."""


def tool(service: str, name: str) -> str:
    return PREFIX[service] + name


def all_tools(service: str) -> list[str]:
    return [tool(service, t) for t in (*READ[service], *WRITE[service])]


def read_tools(service: str) -> list[str]:
    return [tool(service, t) for t in READ[service]]


def write_tools(service: str) -> list[str]:
    return [tool(service, t) for t in WRITE[service]]


def service_of(tool_name: str) -> str | None:
    for s, prefix in PREFIX.items():
        if tool_name.startswith(prefix):
            return s
    return None


def is_write(tool_name: str) -> bool:
    s = service_of(tool_name)
    return s is not None and tool_name[len(PREFIX[s]) :] in WRITE[s]


def mcp_server_prefix(name: str) -> str:
    """``"claude.ai Gmail"`` -> ``"mcp__claude_ai_Gmail"`` (como nombra Claude Code las
    herramientas de un servidor)."""
    return "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", name)


def enabled_services(secretaria: object) -> list[str]:
    """Servicios activados en Ajustes (``secretaria.google_*``)."""
    return [s for s in SERVICES if bool(getattr(secretaria, f"google_{s}", False))]
