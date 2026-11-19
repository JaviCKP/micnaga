"""Una llamada concreta a una herramienta de Google a traves de ``claude -p``, con guardian.

Micnaga no tiene tokens de Google: para leer un evento o crear un borrador pide a ``claude -p``
(Haiku, barato) que llame a UNA herramienta con una entrada exacta. Garantias:

* La herramienta NO va en ``--allowedTools``: solo la autoriza el guardian (si el gancho falla,
  ``dontAsk`` la deniega). El resto de Google y todo servidor ajeno, denegado;
  lista blanca de servidores por URL (solo ese conector se carga).
* Gancho ``PreToolUse`` (``guard``) con el plan de esta llamada: si el nombre o la entrada no
  coinciden, se deniega; si coinciden, se ejecuta la entrada aprobada exacta (``updatedInput``).
* Antes de lanzar, **prueba previa** del gancho (``self_test``): si no deniega lo que debe, no se
  lanza nada (Claude Code deja pasar las llamadas si el gancho falla).
* Recibos de un solo uso en ``<datos>/secretaria/google/receipts``: un reintento de la misma
  escritura (misma clave) queda denegado por el propio gancho. Si la escritura falla (resultado
  con error), el recibo se borra para poder reintentar.
* Tras la ejecucion, cada resultado con exito de una escritura debe tener su recibo; si no, es
  ``unguarded`` (no deberia pasar nunca: se registra como error).
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from micnaga import paths
from micnaga.brain import google as g
from micnaga.brain.claude import CREATE_NO_WINDOW, ClaudeRequest, ClaudeResult, ToolResult
from micnaga.brain.google import policy

log = logging.getLogger(__name__)

SYSTEM = (
    "Eres un ejecutor mecánico. Llamas exactamente una vez a la herramienta indicada con la "
    "entrada JSON indicada, copiada tal cual. No llamas a ninguna otra herramienta, no "
    "cambias ningún valor y no añades campos. Si la herramienta responde con un error, no "
    "reintentes salvo que el error te indique la entrada exacta a usar. Al terminar respondes "
    "solo: hecho."
)


class GuardError(RuntimeError):
    kind = "guard"


def receipts_dir() -> Path:
    return paths.app_data_dir() / "secretaria" / "google" / "receipts"


def receipt_path(key: str) -> Path:
    return receipts_dir() / f"{key}.json"


def new_key() -> str:
    return "r" + uuid.uuid4().hex[:16]


@dataclass
class CallOutcome:
    ok: bool
    tool: str
    input: dict[str, Any]
    result: ToolResult | None = None
    claude: ClaudeResult | None = None
    error: str | None = None
    receipt: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def structured(self) -> dict[str, Any]:
        if self.result is not None and isinstance(self.result.structured, dict):
            return self.result.structured
        if self.result is not None:
            try:
                data = json.loads(self.result.text)
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}
        return {}


def self_test(plan: Path) -> None:
    """Ejecuta el gancho con una llamada que NO esta en el plan: debe denegar. Si no, nada se
    lanza (un gancho roto deja pasar todo)."""
    probe = {
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__micnaga_selftest__nope",
        "tool_input": {"x": 1},
        "tool_use_id": "selftest",
    }
    try:
        proc = subprocess.run(
            policy.guard_argv(plan),
            input=json.dumps(probe).encode("utf-8"),
            capture_output=True,
            timeout=policy.GUARD_TIMEOUT_S,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        out = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
        decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
    except Exception as exc:
        raise GuardError(f"guard self-test failed: {type(exc).__name__}") from exc
    if proc.returncode != 0 or decision != "deny":
        raise GuardError("guard self-test did not deny")


def prompt_for(full: str, tool_input: dict[str, Any]) -> str:
    exact = json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))
    return (
        f"Llama una sola vez a la herramienta `{full}` con exactamente esta entrada JSON "
        f"(cópiala tal cual):\n{exact}\n"
        "No llames a ninguna otra herramienta. Cuando termine, responde solo: hecho.\n"
    )


def run_tool(
    runner: Any,
    service: str,
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    key: str | None = None,
    foreign: list[str] | None = None,
    timeout_s: float = 120.0,
    model: str = "haiku",
) -> CallOutcome:
    """Ejecuta UNA llamada. ``key``: clave de idempotencia (escrituras); sin ella, una nueva."""
    full = g.tool(service, tool_name)
    write = g.is_write(full)
    key = key or new_key()
    out = CallOutcome(ok=False, tool=full, input=dict(tool_input))
    if write and receipt_path(key).is_file():
        out.error = "already_done"
        out.receipt = _read_receipt(key)
        return out
    pol = policy.single_call(service, tool_name, foreign=foreign)
    with tempfile.TemporaryDirectory(prefix="micnaga-gcall-") as tmp:
        # escrituras: recibos persistentes (un reintento no duplica); lecturas: desechables
        receipts = receipts_dir() if write else Path(tmp) / "receipts"
        receipts.mkdir(parents=True, exist_ok=True)
        plan = policy.write_plan(
            Path(tmp) / "plan.json",
            [{"key": key, "tool": full, "input": tool_input}],
            receipts=receipts,
        )
        self_test(plan)
        req = ClaudeRequest(
            prompt=prompt_for(full, tool_input),
            system_prompt=SYSTEM,
            model=model,
            max_turns=4,
            timeout_s=timeout_s,
            allowed_tools=pol.allowed,
            disallowed_tools=pol.disallowed,
            settings=policy.with_guard(pol.settings, plan),
            extra_env=dict(pol.env),
            strict_mcp=False,
        )
        run_once = getattr(runner, "run_once", None) or runner.run
        res: ClaudeResult = run_once(req)
        receipt = _read_receipt(key, receipts)
    out.claude = res
    mine = [r for r in res.tool_results if r.name == full]
    out.receipt = receipt
    good = [r for r in mine if not r.is_error]
    if receipt is not None:
        good = [r for r in good if r.tool_use_id == receipt.get("tool_use_id")] or good
    if good:
        out.result = good[-1]
        out.ok = True
        if write and receipt is None:
            out.ok = False
            out.error = "unguarded"
            log.error("a Google write ran without a guard receipt (%s)", full)
        return out
    out.result = mine[-1] if mine else None
    if write and receipt is not None:
        receipt_path(key).unlink(missing_ok=True)  # no se ejecuto con exito: se puede reintentar
    if full in res.denials:
        out.error = "denied"
    elif out.result is not None:
        out.error = "tool_error"
    else:
        out.error = "not_called"
    return out


def _read_receipt(key: str, folder: Path | None = None) -> dict[str, Any] | None:
    path = (folder / f"{key}.json") if folder is not None else receipt_path(key)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def forget_receipt(key: str) -> None:
    receipt_path(key).unlink(missing_ok=True)
