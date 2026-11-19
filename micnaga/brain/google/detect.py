"""¿Que servicios de Google ve el ``claude`` de este equipo? Barato y con cache.

1. **Evento ``system/init`` cortado en seco** (preferido): ``claude -p`` con la lista blanca de
   los tres conectores y conexion bloqueante; en cuanto llega ``init`` (~1,5 s) se mata el
   proceso: no llega a llamar al modelo (cero tokens). Su ``mcp_servers`` dice ``connected`` /
   ``failed`` / ``needs-auth`` por conector.
2. **``claude mcp list``** (~5-10 s: comprueba la salud de todos): respaldo si (1) falla y fuente
   de los nombres de los servidores AJENOS (se deniegan explicitamente en cada ``claude -p``).

El resultado se guarda en ``<datos>/secretaria/google.json``; «Comprobar» lo rehace (fuera del
hilo de la interfaz).
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from micnaga import paths
from micnaga.brain import google as g
from micnaga.brain.claude import CREATE_NO_WINDOW, ClaudeRequest, ClaudeRunner, kill_tree
from micnaga.brain.google import policy

log = logging.getLogger(__name__)

CACHE = "google.json"
INIT_TIMEOUT_S = 30.0
LIST_TIMEOUT_S = 60.0
STATES = ("connected", "failed", "needs_auth", "absent", "unknown")


def cache_path() -> Path:
    return paths.app_data_dir() / "secretaria" / CACHE


def read_cache() -> dict[str, Any]:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_cache(data: dict[str, Any]) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def foreign_servers() -> list[str]:
    """Servidores ajenos vistos en la ultima comprobacion (para ``--disallowedTools``)."""
    names = read_cache().get("foreign")
    return [str(n) for n in names] if isinstance(names, list) else []


def connected_services() -> list[str]:
    services = read_cache().get("services")
    if not isinstance(services, dict):
        return []
    return [s for s in g.SERVICES if services.get(s) == "connected"]


def _state(raw: str) -> str:
    low = raw.lower().replace("-", "_")
    if "connected" in low and "not" not in low and "fail" not in low:
        return "connected"
    if "auth" in low:
        return "needs_auth"
    if "fail" in low or "error" in low:
        return "failed"
    if "pending" in low:
        return "unknown"
    return "unknown"


def services_from_init(init: dict[str, Any]) -> dict[str, str]:
    seen = {
        str(s.get("name")): _state(str(s.get("status", "")))
        for s in init.get("mcp_servers") or []
        if isinstance(s, dict)
    }
    return {s: seen.get(g.SERVER[s], "absent") for s in g.SERVICES}


# ============================================================================== init


def probe_init(runner: ClaudeRunner, timeout_s: float = INIT_TIMEOUT_S) -> dict[str, Any]:
    """Lanza ``claude -p`` y lo corta al llegar ``system/init``. Devuelve ese evento."""
    req = ClaudeRequest(
        prompt="ok",
        system_prompt="Responde ok.",
        model="haiku",
        max_turns=1,
        strict_mcp=False,
        settings={"allowedMcpServers": policy.server_allowlist(list(g.SERVICES), [])},
        extra_env=dict(policy.ENV),
    )
    cmd = runner.command()
    env = runner.env(req.extra_env)
    with tempfile.TemporaryDirectory(prefix="micnaga-gdetect-") as tmp:
        workdir = Path(tmp) / "cwd"
        workdir.mkdir()
        args = runner.build_args(req, workdir)
        proc = subprocess.Popen(
            [*cmd, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(workdir),
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
        lines: queue.Queue[bytes | None] = queue.Queue()

        def read() -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                lines.put(raw)
            lines.put(None)

        threading.Thread(target=read, name="micnaga-gdetect", daemon=True).start()
        try:
            assert proc.stdin is not None
            with contextlib.suppress(OSError):
                proc.stdin.write(b"ok")
                proc.stdin.close()
            deadline = time.monotonic() + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("no system/init")
                try:
                    raw = lines.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if raw is None:
                    raise RuntimeError("claude exited before system/init")
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if (
                    isinstance(ev, dict)
                    and ev.get("type") == "system"
                    and ev.get("subtype") == "init"
                ):
                    return ev
        finally:
            kill_tree(proc.pid)


# ============================================================================== mcp list

_LINE = re.compile(r"^(?P<name>[^:\n]+?):\s+(?P<target>.+?)\s+-\s+(?P<status>.+)$")


def parse_mcp_list(text: str) -> list[tuple[str, str]]:
    """``claude mcp list`` -> ``[(nombre, estado)]`` (estado como en ``STATES``)."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        status = m.group("status")
        state = (
            "connected"
            if "Connected" in status and "Fail" not in status
            else "needs_auth"
            if "auth" in status.lower()
            else "failed"
        )
        out.append((m.group("name").strip(), state))
    return out


def mcp_list(runner: ClaudeRunner, timeout_s: float = LIST_TIMEOUT_S) -> list[tuple[str, str]]:
    cmd = runner.command()
    env = runner.env()
    with tempfile.TemporaryDirectory(prefix="micnaga-gdetect-") as cwd:
        proc = subprocess.run(
            [*cmd, "mcp", "list"],
            capture_output=True,
            cwd=cwd,
            env=env,
            timeout=timeout_s,
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    return parse_mcp_list(proc.stdout.decode("utf-8", "replace"))


# ============================================================================== comprobar


def check(runner: ClaudeRunner, *, with_list: bool = True) -> dict[str, Any]:
    """Rehace la deteccion y la guarda. Nunca lanza: los fallos quedan en ``error``."""
    t0 = time.monotonic()
    out: dict[str, Any] = {
        "checked": time.time(),
        "services": dict.fromkeys(g.SERVICES, "unknown"),
        "method": None,
        "error": None,
        "foreign": foreign_servers(),
    }
    try:
        init = probe_init(runner)
        out["services"] = services_from_init(init)
        out["method"] = "init"
        out["init_s"] = round(time.monotonic() - t0, 2)
    except Exception as exc:
        log.warning("google detection via system/init failed: %s", type(exc).__name__)
        out["error"] = getattr(exc, "kind", None) or type(exc).__name__
    if with_list:
        t1 = time.monotonic()
        try:
            listed = mcp_list(runner)
            google_names = {v: k for k, v in g.SERVER.items()}
            out["foreign"] = sorted(
                {n for n, _ in listed if n not in google_names and n != "micnaga"}
            )
            if out["method"] is None:
                states = dict(listed)
                out["services"] = {s: states.get(g.SERVER[s], "absent") for s in g.SERVICES}
                out["method"] = "mcp_list"
                out["error"] = None
            out["list_s"] = round(time.monotonic() - t1, 2)
        except Exception as exc:
            log.warning("claude mcp list failed: %s", type(exc).__name__)
    write_cache(out)
    return out
