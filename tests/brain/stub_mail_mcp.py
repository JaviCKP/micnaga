"""Servidor MCP de correo FALSO (stdio, solo stdlib) para la evaluacion de la investigacion.

Imita lo minimo de un conector de correo: ``search_threads`` (hilos recientes con un texto) y
``get_thread``. Datos inventados (``research_case.MAIL_THREADS``); nunca correo real.
"""

from __future__ import annotations

import json
import sys
from typing import Any

THREADS = [
    {
        "threadId": "stub-thread-presupuesto",
        "subject": "Re: Presupuesto revisado · recordatorios WhatsApp",
        "from": "Carlos Ruiz <carlos@dentaltriana.example>",
        "date": "2026-11-14",
        "snippet": "Recibido el presupuesto revisado, gracias. Lo vemos con dirección.",
        "url": "https://mail.google.com/mail/u/0/#all/stub-thread-presupuesto",
    }
]
TOOLS = [
    {
        "name": "search_threads",
        "description": "Busca hilos de correo recientes por texto (persona, empresa o asunto).",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_thread",
        "description": "Lee un hilo por su threadId.",
        "inputSchema": {
            "type": "object",
            "properties": {"threadId": {"type": "string"}},
            "required": ["threadId"],
        },
        "annotations": {"readOnlyHint": True},
    },
]


def _norm(s: str) -> str:
    return s.casefold()


def call(name: str, args: dict[str, Any]) -> Any:
    if name == "search_threads":
        words = [w for w in _norm(str(args.get("query", ""))).split() if len(w) > 2]
        hits = [
            t for t in THREADS if any(w in _norm(json.dumps(t, ensure_ascii=False)) for w in words)
        ]
        return {"threads": hits or THREADS[:0]}
    if name == "get_thread":
        hit = next((t for t in THREADS if t["threadId"] == args.get("threadId")), None)
        return hit or {"error": "no existe"}
    raise ValueError(name)


def main() -> None:
    for line in sys.stdin:
        msg = json.loads(line)
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            continue
        if method == "initialize":
            result: Any = {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "correo", "version": "0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            p = msg.get("params", {})
            out = call(p.get("name"), p.get("arguments") or {})
            text = json.dumps(out, ensure_ascii=False)
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            result = {}
        reply = {"jsonrpc": "2.0", "id": mid, "result": result}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
