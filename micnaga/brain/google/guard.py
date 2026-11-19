"""Gancho ``PreToolUse`` de Claude Code: solo deja pasar lo aprobado (``micnaga hook-guard``).

``claude -p --settings <json>`` lo ejecuta antes de CADA llamada a herramienta, con un JSON por
stdin (``tool_name``, ``tool_input``, ``tool_use_id``...). Decide con el **plan** (``--plan``):

* ``allow_read``: herramientas de lectura permitidas tal cual.
* ``items``: escrituras aprobadas ``{"key", "tool", "input"}``. Una llamada pasa solo si el
  nombre coincide y la entrada coincide **normalizada** (espacios, Unicode NFC, correos sin
  mayusculas) con lo aprobado. Al pasar se devuelve ``updatedInput`` = la entrada aprobada
  exacta: lo que se ejecuta es lo aprobado, no lo que escribio el modelo.
* Cada aprobacion es de **un solo uso**: un recibo ``<receipts>/<key>.json`` creado con
  ``O_EXCL`` (llamadas en paralelo lanzan ganchos en paralelo). Micnaga comprueba despues que cada
  escritura con exito tiene su recibo.
* Todo lo demas se **deniega**.

**Falla cerrado.** Claude Code deja pasar la llamada si el gancho sale con 1, no existe (127) o
tarda demasiado (medido con 2.1.327); por eso cualquier excepcion sale con codigo **2** (bloquea)
y Micnaga prueba el gancho antes de cada ejecucion (``self_test``).

Solo stdlib y sin imports del paquete: en desarrollo se ejecuta como script
(``python .../guard.py --plan X``); congelado, ``micnaga-cli.exe hook-guard --plan X``.
"""

from __future__ import annotations

import json
import os
import sys
import unicodedata
from typing import Any

DENY_PREFIX = "micnaga-guard: "


def normalize(value: Any, key: str = "") -> Any:
    """Forma canonica para comparar (no para ejecutar)."""
    if isinstance(value, str):
        text = " ".join(unicodedata.normalize("NFC", value).split())
        if "mail" in key.lower() or key.lower() in ("to", "cc", "bcc"):
            text = text.casefold()
        return text
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, dict):
        return {k: normalize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v, key) for v in value]
    return value


def matches(given: Any, approved: Any) -> bool:
    return bool(normalize(given) == normalize(approved))


def _out(decision: str, reason: str, updated: dict[str, Any] | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": DENY_PREFIX + reason,
    }
    if updated is not None:
        spec["updatedInput"] = updated
    return {"hookSpecificOutput": spec}


def _claim(receipts: str, key: str, payload: dict[str, Any]) -> bool:
    """Recibo de un solo uso. ``False`` si esa aprobacion ya se uso."""
    os.makedirs(receipts, exist_ok=True)
    path = os.path.join(receipts, f"{key}.json")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return True


def decide(event: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    name = str(event.get("tool_name") or "")
    given = event.get("tool_input")
    if not isinstance(given, dict):
        given = {}
    if name in set(plan.get("allow_read") or []):
        return _out("allow", "lectura permitida")
    expected: dict[str, Any] | None = None
    for item in plan.get("items") or []:
        if not isinstance(item, dict) or item.get("tool") != name:
            continue
        approved = item.get("input")
        if not isinstance(approved, dict):
            continue
        if not matches(given, approved):
            expected = approved
            continue
        key = str(item.get("key") or "")
        receipt = {"key": key, "tool": name, "tool_use_id": event.get("tool_use_id")}
        if not key or not _claim(str(plan["receipts"]), key, receipt):
            return _out("deny", "esta accion aprobada ya se ejecuto")
        return _out("allow", "coincide con lo aprobado", updated=approved)
    if expected is not None:
        exact = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
        return _out("deny", "la entrada no coincide con lo aprobado. Usa exactamente: " + exact)
    return _out("deny", "no esta en el plan aprobado")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) != 2 or args[0] != "--plan":
            raise ValueError("usage: hook-guard --plan <file>")
        with open(args[1], encoding="utf-8") as fh:
            plan = json.load(fh)
        raw = sys.stdin.buffer.read().decode("utf-8")
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict) or not isinstance(plan, dict):
            raise ValueError("bad input")
        if event.get("hook_event_name", "PreToolUse") != "PreToolUse":
            return 0
        sys.stdout.write(json.dumps(decide(event, plan), ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0
    except BaseException as exc:  # falla cerrado: 2 = bloquear
        sys.stderr.write(f"{DENY_PREFIX}error ({type(exc).__name__}); bloqueado\n")
        return 2


if __name__ == "__main__":  # pragma: no cover - lo ejecuta Claude Code
    sys.exit(main())
