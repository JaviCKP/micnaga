"""Que ve y que puede usar cada ``claude -p`` cuando Google esta activado.

Medido con Claude Code 2.1.327 (``DECISIONS.md`` §19):

* ``--strict-mcp-config`` **oculta** los conectores de claude.ai: con Google no se usa.
* Sin el, la conexion MCP es **no bloqueante** por defecto: con ``--setting-sources project`` el
  ``system/init`` sale antes de que lleguen los conectores y la sesion no los tiene. Con
  ``MCP_CONNECTION_NONBLOCKING=0`` espera (~1,3 s) y aparecen.
* ``--settings {"allowedMcpServers": [...]}`` es una lista blanca real: por URL para los conectores
  (``https://gmailmcp.googleapis.com/*``...) y por nombre para ``micnaga``. Todo lo demas
  (otros conectores de claude.ai, servidores del usuario) **no se carga**.
* ``--disallowedTools`` quita la herramienta de la sesion (tambien a nivel de servidor,
  ``mcp__<servidor>``). Se deniegan todas las escrituras de Google y todo servidor ajeno conocido.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from micnaga.brain import google as g

ENV = {"MCP_CONNECTION_NONBLOCKING": "0"}
GUARD_TIMEOUT_S = 15


@dataclass
class Policy:
    services: list[str]
    allowed: list[str] = field(default_factory=list)
    disallowed: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=lambda: dict(ENV))
    tool: str | None = None
    """La herramienta de una llamada concreta (la autoriza solo el guardian)."""


def server_allowlist(services: list[str], stdio_names: list[str]) -> list[dict[str, str]]:
    """``allowedMcpServers``: los servidores propios por nombre y los conectores por URL."""
    out = [{"serverName": n} for n in stdio_names]
    out += [{"serverUrl": g.URL[s]} for s in services]
    return out


def foreign_denials(services: list[str], foreign: list[str]) -> list[str]:
    """Prefijos de servidor que nunca se permiten: otros conectores de claude.ai, los servicios
    de Google desactivados y los servidores ajenos vistos al detectar."""
    deny = list(g.OTHER_CLAUDE_AI)
    deny += [g.PREFIX[s].rstrip("_") for s in g.SERVICES if s not in services]
    for name in foreign:
        prefix = g.mcp_server_prefix(name)
        if prefix not in deny and name not in g.SERVER.values() and name != "micnaga":
            deny.append(prefix)
    return deny


def analysis(
    services: list[str], *, stdio_names: list[str], foreign: list[str] | None = None
) -> Policy:
    """Pasada de analisis: SOLO lecturas de Google (las que usa), todo lo demas denegado."""
    allowed = [g.tool(s, t) for s in services for t in g.ANALYSIS_READ[s]]
    disallowed = [t for s in services for t in g.all_tools(s) if t not in allowed]
    disallowed += foreign_denials(services, foreign or [])
    return Policy(
        services=list(services),
        allowed=allowed,
        disallowed=disallowed,
        settings={"allowedMcpServers": server_allowlist(services, stdio_names)},
    )


def single_call(service: str, tool_name: str, *, foreign: list[str] | None = None) -> Policy:
    """Una llamada concreta (lectura puntual o escritura aprobada): solo esa herramienta.

    La herramienta NO va en ``--allowedTools``: el unico permiso lo da el guardian (``allow``
    del gancho). Medido: si el gancho falla (sale con 1, no existe), ``dontAsk`` la deniega;
    con ``--allowedTools`` se ejecutaria igualmente. Asi un guardian roto = nada se ejecuta."""
    full = g.tool(service, tool_name)
    disallowed = [t for s in g.SERVICES for t in g.all_tools(s) if t != full and s == service]
    disallowed += foreign_denials([service], foreign or [])
    return Policy(
        services=[service],
        allowed=[],
        disallowed=disallowed,
        settings={"allowedMcpServers": server_allowlist([service], [])},
        tool=full,
    )


# ============================================================================== guardian


def guard_command(plan: Path) -> str:
    """Linea de comandos del gancho (la ejecuta Git Bash en Windows: barras normales y
    comillas; rutas con espacios probadas)."""
    plan_s = str(plan).replace("\\", "/")
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        cli = exe.with_name("micnaga-cli.exe")
        target = str(cli if cli.is_file() else exe).replace("\\", "/")
        return f'"{target}" hook-guard --plan "{plan_s}"'
    py = sys.executable.replace("\\", "/")
    script = str(Path(__file__).with_name("guard.py")).replace("\\", "/")
    return f'"{py}" "{script}" --plan "{plan_s}"'


def guard_argv(plan: Path) -> list[str]:
    """Lo mismo como lista (para la prueba previa del gancho desde Python)."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        cli = exe.with_name("micnaga-cli.exe")
        return [str(cli if cli.is_file() else exe), "hook-guard", "--plan", str(plan)]
    return [sys.executable, str(Path(__file__).with_name("guard.py")), "--plan", str(plan)]


def with_guard(settings: dict[str, Any], plan: Path) -> dict[str, Any]:
    out = dict(settings)
    out["hooks"] = {
        "PreToolUse": [
            {
                "matcher": "*",
                "hooks": [
                    {"type": "command", "command": guard_command(plan), "timeout": GUARD_TIMEOUT_S}
                ],
            }
        ]
    }
    return out


def write_plan(
    path: Path, items: list[dict[str, Any]], *, receipts: Path, allow_read: list[str] | None = None
) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "items": items,
                "allow_read": list(allow_read or []),
                "receipts": str(receipts),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path
