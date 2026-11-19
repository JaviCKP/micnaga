"""Un ``claude`` falso (solo stdlib) que imita ``claude -p --output-format stream-json``.

Lo lanzan los tests a traves de un envoltorio generado (``SCENARIO = <ruta>``) porque el
``ClaudeRunner`` construye el entorno del hijo por lista blanca (no pasan variables FAKE_*).

Escenario (JSON):

* ``structured``: el ``structured_output`` final (o ``null``).
* ``calls``: ``[{"tool": "buscar_contactos", "args": {...}}]``: se ejecutan DE VERDAD contra el
  servidor MCP de ``--mcp-config`` (initialize -> tools/list -> tools/call), emitiendo
  ``assistant/tool_use`` y ``user/tool_result`` como el real.
* ``error``: ``auth`` | ``rate_limit`` | ``garbled`` | ``no_result`` | ``exit1`` | ``max_turns``.
* ``sleep``: segundos antes de responder (para los tiempos maximos).
* ``fail_first``: falla con ``no_result`` la primera vez (reintento).
* ``log``: fichero donde se anota argv, claves del entorno, stdin y lo visto del MCP.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCENARIO: str = os.environ.get("FAKE_CLAUDE_SCENARIO", "")


def emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def arg(argv: list[str], name: str) -> str | None:
    if name in argv:
        i = argv.index(name)
        return argv[i + 1] if i + 1 < len(argv) else None
    return None


class Mcp:
    def __init__(self, spec: dict[str, Any]) -> None:
        env = spec.get("env") or None
        self.proc = subprocess.Popen(
            [spec["command"], *spec.get("args", [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
        )
        self._id = 0

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        return json.loads(line)

    def notify(self, method: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode())
        self.proc.stdin.flush()

    def close(self) -> int:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        return self.proc.wait(10)


def main(argv: list[str]) -> int:
    sc: dict[str, Any] = json.loads(Path(SCENARIO).read_text(encoding="utf-8")) if SCENARIO else {}
    log_path = sc.get("log")
    log: dict[str, Any] = {"argv": argv, "env": sorted(os.environ), "cwd": os.getcwd()}
    log["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY")
    log["oauth_token"] = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if argv[:2] == ["auth", "status"]:
        print(
            json.dumps(
                sc.get(
                    "auth", {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}
                )
            )
        )
        return 0
    stdin = sys.stdin.buffer.read().decode("utf-8")
    log["stdin"] = stdin
    counter = Path(str(log_path) + ".count") if log_path else None
    n = 0
    if counter is not None:
        n = int(counter.read_text()) + 1 if counter.is_file() else 1
        counter.write_text(str(n))
    if sc.get("sleep"):
        time.sleep(float(sc["sleep"]))
    error = sc.get("error")
    if sc.get("fail_first") and n == 1:
        error = "no_result"
    servers: dict[str, Any] = {}
    cfg_path = arg(argv, "--mcp-config")
    if cfg_path:
        servers = json.loads(Path(cfg_path).read_text(encoding="utf-8"))["mcpServers"]
        log["mcp_config"] = servers
    status = dict.fromkeys(servers, "connected")
    status.update(sc.get("mcp_status", {}))
    emit(
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-fake-1",
            "apiKeySource": "none",
            "mcp_servers": [{"name": k, "status": v} for k, v in status.items()],
            "tools": [],
        }
    )
    mcp: Mcp | None = None
    if "micnaga" in servers and status.get("micnaga") == "connected":
        mcp = Mcp(servers["micnaga"])
        init = mcp.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        mcp.notify("notifications/initialized")
        tools = mcp.rpc("tools/list")
        log["mcp_init"] = init.get("result")
        log["mcp_tools"] = [t["name"] for t in tools["result"]["tools"]]
        log["mcp_results"] = []
        for i, call in enumerate(sc.get("calls", [])):
            tid = f"toolu_{i}"
            emit(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tid,
                                "name": "mcp__micnaga__" + call["tool"],
                                "input": call.get("args", {}),
                            }
                        ],
                    },
                }
            )
            res = mcp.rpc("tools/call", {"name": call["tool"], "arguments": call.get("args", {})})
            log["mcp_results"].append(res.get("result"))
            emit(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tid,
                                "content": res.get("result", {}).get("content"),
                            }
                        ],
                    },
                }
            )
        log["mcp_exit"] = mcp.close()
    if log_path:
        Path(log_path).write_text(json.dumps(log, ensure_ascii=False), encoding="utf-8")
    if error == "garbled":
        sys.stdout.write('this is not json\n{"type": "assist')
        sys.stdout.flush()
        return 0
    if error == "no_result":
        return 0
    if error == "exit1":
        sys.stderr.write("Error: getaddrinfo ENOTFOUND api.anthropic.com\n")
        return 1
    if error == "rate_limit":
        emit(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected", "resetsAt": 1795222800},
            }
        )
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": "You've hit your usage limit",
                "total_cost_usd": 0,
            }
        )
        return 1
    if error == "auth":
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": "Invalid API key · Please run /login",
                "api_error_status": 401,
            }
        )
        return 1
    if error == "max_turns":
        emit({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""})
        return 1
    structured = sc.get("structured")
    emit(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_so",
                        "name": "StructuredOutput",
                        "input": structured or {},
                    }
                ],
            },
        }
    )
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(structured),
            "structured_output": structured,
            "total_cost_usd": 0.0123,
            "duration_ms": 1500,
            "num_turns": 3,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
