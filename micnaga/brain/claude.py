"""``claude -p`` como subproceso (design §3.1 y §5.3).

Una llamada::

    claude -p --model sonnet --tools "" --strict-mcp-config --mcp-config <fichero>
           --allowedTools "mcp__micnaga__*" --permission-mode dontAsk
           --setting-sources project --disable-slash-commands --no-session-persistence
           --max-turns 25 [--max-budget-usd X]
           --output-format stream-json --verbose --json-schema <esquema>
           --system-prompt-file <prompt>      < reunion por stdin (UTF-8)

* Se ejecuta desde una **carpeta vacia propia** (sin ``CLAUDE.md`` del proyecto) y nunca con
  ``--bare`` (no lee el login de la suscripcion) ni ``--safe-mode`` (anula ``--mcp-config``).
* **Entorno por lista blanca** (``build_env``): solo variables del sistema y de red; nunca se
  heredan ``ANTHROPIC_*``/``CLAUDE_CODE_*``. Asi una ``ANTHROPIC_API_KEY`` suelta del entorno no
  gana en silencio. El token (``CLAUDE_CODE_OAUTH_TOKEN``) o la clave (``ANTHROPIC_API_KEY``) se
  anaden solo si ese es el modo elegido, y solo en el entorno del hijo. Telemetria apagada.
* ``stream-json``: se lee stdout linea a linea; cada ``tool_use`` (salvo ``StructuredOutput``)
  llama a ``on_event`` (la UI muestra «Buscando a Marta en Odoo…»); el evento final ``result``
  trae ``structured_output``, coste, uso e ``is_error``. Sin ``result`` = error.
* Errores con nombre (``ClaudeError.kind``): ``not_found``, ``auth``, ``rate_limit`` (con
  ``resets_at``), ``offline``, ``timeout``, ``output``, ``failed``. Un reintento para los
  transitorios. ``CREATE_NO_WINDOW``, objeto de trabajo con «matar al cerrar» y arbol de
  procesos muerto al vencer el tiempo: no quedan procesos sueltos.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

ENV_CLAUDE_EXE = "MICNAGA_CLAUDE_EXE"
"""Ruta explicita del ejecutable (tests: un ``claude`` falso)."""

AuthMode = Literal["session", "token", "api_key"]
ErrorKind = Literal["not_found", "auth", "rate_limit", "offline", "timeout", "output", "failed"]

CREATE_NO_WINDOW = 0x08000000
DEFAULT_TIMEOUT_S = 180.0
STRUCTURED_TOOL = "StructuredOutput"

# Variables que el hijo puede heredar (sistema, perfil, red/proxy). Nada de ANTHROPIC_*.
_ENV_ALLOW = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "COMMONPROGRAMFILES",
    "USERNAME",
    "USERDOMAIN",
    "COMPUTERNAME",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
    "LANG",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "CLAUDE_CONFIG_DIR",
)
_QUIET = {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
}


# ============================================================================== errores


class ClaudeError(RuntimeError):
    kind: ErrorKind = "failed"
    transient = False

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class ClaudeNotFoundError(ClaudeError):
    kind = "not_found"


class ClaudeAuthError(ClaudeError):
    kind = "auth"


class ClaudeRateLimitError(ClaudeError):
    kind = "rate_limit"

    def __init__(self, message: str, *, resets_at: float | None = None, detail: str = "") -> None:
        super().__init__(message, detail=detail)
        self.resets_at = resets_at


class ClaudeOfflineError(ClaudeError):
    kind = "offline"
    transient = True


class ClaudeTimeoutError(ClaudeError):
    kind = "timeout"


class ClaudeOutputError(ClaudeError):
    kind = "output"
    transient = True


class ClaudeRunError(ClaudeError):
    kind = "failed"
    transient = True


_AUTH_HINTS = (
    "invalid api key",
    "/login",
    "not logged in",
    "authentication_failed",
    "authentication_error",
    "oauth token",
    "401",
    "credit balance",
    "billing_error",
)
_OFFLINE_HINTS = (
    "enotfound",
    "econnrefused",
    "econnreset",
    "etimedout",
    "connection error",
    "fetch failed",
    "network",
    "getaddrinfo",
    "unable to connect",
    "overloaded",
    "529",
    "api_error",
)
_RATE_HINTS = ("rate_limit", "rate limit", "usage limit", "429", "limit reached")


def classify(text: str) -> type[ClaudeError]:
    low = text.lower()
    if any(h in low for h in _RATE_HINTS):
        return ClaudeRateLimitError
    if any(h in low for h in _AUTH_HINTS):
        return ClaudeAuthError
    if any(h in low for h in _OFFLINE_HINTS):
        return ClaudeOfflineError
    return ClaudeRunError


# ============================================================================== ejecutable


def _npm_shim_target(cmd: Path) -> list[str] | None:
    """``claude.cmd`` de npm -> ``[node, cli.js]`` (un ``.cmd`` sin shell destroza el JSON)."""
    root = cmd.parent / "node_modules" / "@anthropic-ai" / "claude-code"
    for name in ("cli.js", "cli.mjs"):
        script = root / name
        if script.is_file():
            node = cmd.parent / "node.exe"
            node_path = str(node) if node.is_file() else shutil.which("node")
            if node_path:
                return [node_path, str(script)]
    exe = root / "bin" / "claude.exe"
    if exe.is_file():
        return [str(exe)]
    return None


def find_claude(env: Mapping[str, str] | None = None) -> list[str] | None:
    """Prefijo de linea de comandos para lanzar ``claude`` o ``None`` si no esta instalado.

    Orden: ``MICNAGA_CLAUDE_EXE``; ``%USERPROFILE%\\.local\\bin\\claude.exe`` (instalador
    nativo); ``claude.exe`` en el ``PATH``; ``claude.cmd`` de npm (``%APPDATA%\\npm``) resuelto
    a ``node cli.js``.
    """
    env = os.environ if env is None else env
    explicit = env.get(ENV_CLAUDE_EXE, "").strip()
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            return None
        if p.suffix.lower() == ".py":
            import sys

            return [sys.executable, str(p)]
        return [str(p)]
    home = env.get("USERPROFILE") or str(Path.home())
    native = Path(home) / ".local" / "bin" / "claude.exe"
    if native.is_file():
        return [str(native)]
    path = env.get("PATH")
    found = shutil.which("claude.exe", path=path) or shutil.which("claude", path=path)
    if found and Path(found).suffix.lower() == ".exe":
        return [found]
    candidates = []
    if found:
        candidates.append(Path(found))
    appdata = env.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "claude.cmd")
    for cand in candidates:
        if cand.suffix.lower() in (".cmd", ".bat", "") and cand.with_suffix(".cmd").is_file():
            target = _npm_shim_target(cand.with_suffix(".cmd"))
            if target:
                return target
    return None


# ============================================================================== entorno


def build_env(
    mode: AuthMode = "session",
    *,
    secret: str | None = None,
    base: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Entorno del hijo por lista blanca + modo de autenticacion elegido."""
    base = os.environ if base is None else base
    upper = {k.upper(): v for k, v in base.items()}
    env = {k: upper[k] for k in _ENV_ALLOW if k in upper}
    for k in ("http_proxy", "https_proxy", "no_proxy"):
        if k in base:
            env[k] = base[k]
    env.update(_QUIET)
    env["PYTHONUTF8"] = "1"
    if mode == "token":
        if not secret:
            raise ClaudeAuthError("no Claude token stored")
        env["CLAUDE_CODE_OAUTH_TOKEN"] = secret
    elif mode == "api_key":
        if not secret:
            raise ClaudeAuthError("no Anthropic API key stored")
        env["ANTHROPIC_API_KEY"] = secret
    if extra:
        env.update(extra)
    return env


# ============================================================================== eventos


@dataclass(frozen=True)
class ToolCall:
    """Una llamada a herramienta vista en el flujo (``mcp__micnaga__buscar_contactos``...)."""

    name: str
    input: dict[str, Any]

    @property
    def server(self) -> str | None:
        parts = self.name.split("__")
        return parts[1] if len(parts) >= 3 and parts[0] == "mcp" else None

    @property
    def tool(self) -> str:
        parts = self.name.split("__")
        return parts[2] if len(parts) >= 3 and parts[0] == "mcp" else self.name


@dataclass(frozen=True)
class ToolResult:
    """El resultado de una llamada (evento ``user/tool_result``): texto y, si el servidor lo da,
    ``structuredContent`` (ids, enlaces). Las escrituras de Google se leen de aqui."""

    tool_use_id: str
    name: str
    is_error: bool
    text: str
    structured: dict[str, Any] | None = None


@dataclass
class ClaudeResult:
    structured: dict[str, Any] | None
    text: str
    cost_usd: float
    usage: dict[str, Any]
    duration_s: float
    num_turns: int
    model: str
    mcp_servers: dict[str, str] = field(default_factory=dict)
    """Estado de cada servidor MCP segun ``system/init`` (``connected``, ``failed``...)."""
    tool_calls: list[ToolCall] = field(default_factory=list)
    api_key_source: str = ""
    tool_results: list[ToolResult] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    """Herramientas que ``system/init`` declara disponibles en esta sesion."""
    denials: list[str] = field(default_factory=list)
    """Herramientas denegadas (``permission_denials`` del resultado final)."""


@dataclass
class ClaudeRequest:
    prompt: str
    system_prompt: str
    model: str = "sonnet"
    schema: dict[str, Any] | None = None
    mcp_servers: dict[str, dict[str, Any]] | None = None
    """``{"micnaga": {"command": ..., "args": [...], "env": {...}}}`` (formato ``mcpServers``)."""
    allowed_tools: list[str] = field(default_factory=list)
    max_turns: int = 25
    max_budget_usd: float | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    disallowed_tools: list[str] = field(default_factory=list)
    settings: dict[str, Any] | None = None
    """``--settings`` (fichero temporal): ``allowedMcpServers``, ganchos ``PreToolUse``..."""
    extra_env: dict[str, str] = field(default_factory=dict)
    strict_mcp: bool = True
    """``--strict-mcp-config``. Debe ser ``False`` para ver los conectores de claude.ai
    (Google): entonces ``settings`` lleva la lista blanca ``allowedMcpServers``."""


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(c.get("text", ""))
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


def parse_stream(
    lines: Any,
    on_event: Callable[[ToolCall], None] | None = None,
) -> ClaudeResult:
    """Interpreta las lineas de ``--output-format stream-json`` (iterable de ``str``).

    Lineas que no son JSON se ignoran (avisos) pero se cuentan; tipos desconocidos tambien.
    Lanza ``ClaudeError`` si el resultado final falta, es un error o no trae lo esperado.
    """
    result: dict[str, Any] | None = None
    init: dict[str, Any] = {}
    calls: list[ToolCall] = []
    names: dict[str, str] = {}
    results: list[ToolResult] = []
    garbled = 0
    rejected_reset: float | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            garbled += 1
            continue
        if not isinstance(ev, dict):
            garbled += 1
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            init = ev
        elif kind == "assistant":
            msg = ev.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            for item in content if isinstance(content, list) else []:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                name = str(item.get("name", ""))
                names[str(item.get("id", ""))] = name
                if name == STRUCTURED_TOOL or not name:
                    continue
                args = item.get("input")
                call = ToolCall(name, args if isinstance(args, dict) else {})
                calls.append(call)
                if on_event is not None:
                    try:
                        on_event(call)
                    except Exception:
                        log.debug("on_event failed", exc_info=True)
        elif kind == "user":
            results += _tool_results(ev, names)
        elif kind == "rate_limit_event":
            info = ev.get("rate_limit_info")
            if isinstance(info, dict) and info.get("status") == "rejected":
                with contextlib.suppress(TypeError, ValueError):
                    rejected_reset = float(info.get("resetsAt", "x"))
        elif kind == "result":
            result = ev
    if result is None:
        if rejected_reset is not None:
            raise ClaudeRateLimitError("usage limit reached", resets_at=rejected_reset)
        raise ClaudeOutputError(f"no result event in the stream ({garbled} garbled lines)")
    text = str(result.get("result") or "")
    if result.get("is_error") or str(result.get("subtype", "success")) != "success":
        detail = f"{result.get('subtype')}: {text[:300]} (api {result.get('api_error_status')})"
        if rejected_reset is not None:
            raise ClaudeRateLimitError("usage limit reached", resets_at=rejected_reset)
        subtype = str(result.get("subtype", ""))
        if subtype.startswith("error_max"):
            raise ClaudeRunError(f"claude stopped: {subtype}", detail=detail)
        err_cls = classify(detail)
        if err_cls is ClaudeRateLimitError:
            raise ClaudeRateLimitError("usage limit reached", detail=detail)
        raise err_cls(f"claude returned an error ({subtype or 'error'})", detail=detail)
    structured = result.get("structured_output")
    servers: dict[str, str] = {}
    for s in init.get("mcp_servers") or []:
        if isinstance(s, dict) and "name" in s:
            servers[str(s["name"])] = str(s.get("status", ""))
    usage = result.get("usage")
    return ClaudeResult(
        structured=structured if isinstance(structured, dict) else None,
        text=text,
        cost_usd=float(result.get("total_cost_usd") or 0.0),
        usage=usage if isinstance(usage, dict) else {},
        duration_s=float(result.get("duration_ms") or 0.0) / 1000.0,
        num_turns=int(result.get("num_turns") or 0),
        model=str(init.get("model", "")),
        mcp_servers=servers,
        tool_calls=calls,
        api_key_source=str(init.get("apiKeySource", "")),
        tool_results=results,
        tools=[str(t) for t in init.get("tools") or [] if isinstance(t, str)],
        denials=[
            str(d.get("tool_name", ""))
            for d in result.get("permission_denials") or []
            if isinstance(d, dict)
        ],
    )


def _tool_results(ev: dict[str, Any], names: dict[str, str]) -> list[ToolResult]:
    msg = ev.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    extra = ev.get("tool_use_result")
    structured = extra.get("structuredContent") if isinstance(extra, dict) else None
    out: list[ToolResult] = []
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict) or item.get("type") != "tool_result":
            continue
        tid = str(item.get("tool_use_id", ""))
        name = names.get(tid, "")
        if name == STRUCTURED_TOOL:
            continue
        out.append(
            ToolResult(
                tool_use_id=tid,
                name=name,
                is_error=bool(item.get("is_error")),
                text=_result_text(item.get("content")),
                structured=structured if isinstance(structured, dict) else None,
            )
        )
    return out


# ============================================================================== proceso


class _Job:
    """Objeto de trabajo de Windows con «matar al cerrar»: si Micnaga muere, el arbol de
    ``claude`` (y sus servidores MCP) muere con el."""

    def __init__(self) -> None:
        self.handle: Any = None
        try:
            import win32job as _wj

            wj: Any = _wj  # los stubs de pywin32 tipan mal estas funciones
            job = wj.CreateJobObject(None, "")
            info = wj.QueryInformationJobObject(job, wj.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= wj.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            wj.SetInformationJobObject(job, wj.JobObjectExtendedLimitInformation, info)
            self.handle = job
        except Exception:
            log.debug("job object unavailable", exc_info=True)

    def assign(self, proc: subprocess.Popen[bytes]) -> None:
        if self.handle is None:
            return
        try:
            import win32api
            import win32con
            import win32job

            h = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, proc.pid
            )
            win32job.AssignProcessToJobObject(self.handle, h)
            win32api.CloseHandle(h)
        except Exception:
            log.debug("could not assign claude to the job object", exc_info=True)

    def close(self) -> None:
        if self.handle is not None:
            with contextlib.suppress(Exception):
                self.handle.Close()
            self.handle = None


def kill_tree(pid: int) -> None:
    try:
        import psutil

        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for p in [*children, parent]:
            with contextlib.suppress(Exception):
                p.kill()
        psutil.wait_procs([*children, parent], timeout=3)
    except Exception:
        log.debug("kill_tree(%d) failed", pid, exc_info=True)


SecretGetter = Callable[[str], str | None]


class ClaudeRunner:
    """Lanza ``claude -p``. ``auth`` decide el modo; el secreto sale de ``secret_getter``."""

    def __init__(
        self,
        *,
        auth: AuthMode = "session",
        secret_getter: SecretGetter | None = None,
        command: list[str] | None = None,
        base_env: Mapping[str, str] | None = None,
        retries: int = 1,
        retry_delay_s: float = 3.0,
    ) -> None:
        self.auth = auth
        self._secret_getter = secret_getter
        self._command = command
        self._base_env = base_env
        self.retries = retries
        self.retry_delay_s = retry_delay_s

    # ------------------------------------------------------------------ utilidades

    def command(self) -> list[str]:
        cmd = self._command or find_claude(self._base_env)
        if not cmd:
            raise ClaudeNotFoundError("Claude Code is not installed (claude not found)")
        return list(cmd)

    def env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        secret: str | None = None
        if self.auth != "session":
            from micnaga.brain import secrets

            getter = self._secret_getter or secrets.get
            target = secrets.CLAUDE_TOKEN if self.auth == "token" else secrets.ANTHROPIC_KEY
            secret = getter(target)
        return build_env(self.auth, secret=secret, base=self._base_env, extra=extra)

    # ------------------------------------------------------------------ auth status

    def auth_status(self, timeout_s: float = 20.0) -> dict[str, Any]:
        """``claude auth status`` (sesion de Claude Code). Con token/clave no aplica: se informa
        del modo y de si hay secreto guardado."""
        if self.auth != "session":
            try:
                self.env()
            except ClaudeAuthError:
                return {"loggedIn": False, "authMethod": self.auth}
            return {"loggedIn": True, "authMethod": self.auth}
        cmd = self.command()
        env = self.env()
        with tempfile.TemporaryDirectory(prefix="micnaga-claude-") as cwd:
            try:
                proc = subprocess.run(
                    [*cmd, "auth", "status"],
                    capture_output=True,
                    cwd=cwd,
                    env=env,
                    timeout=timeout_s,
                    creationflags=CREATE_NO_WINDOW,
                    stdin=subprocess.DEVNULL,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ClaudeTimeoutError("claude auth status timed out") from exc
            except OSError as exc:
                raise ClaudeNotFoundError(f"cannot start claude: {exc}") from exc
        out = proc.stdout.decode("utf-8", "replace").strip()
        try:
            data = json.loads(out)
        except ValueError:
            data = {"loggedIn": False, "raw": out[:200]}
        return data if isinstance(data, dict) else {"loggedIn": False}

    # ------------------------------------------------------------------ llamada

    def build_args(self, req: ClaudeRequest, workdir: Path) -> list[str]:
        sp = workdir.parent / "system_prompt.md"
        sp.write_text(req.system_prompt, encoding="utf-8")
        args = [
            "-p",
            "--model",
            req.model,
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--setting-sources",
            "project",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--max-turns",
            str(req.max_turns),
            "--output-format",
            "stream-json",
            "--verbose",
            "--system-prompt-file",
            str(sp),
        ]
        if req.strict_mcp:
            args.append("--strict-mcp-config")
        if req.max_budget_usd is not None:
            args += ["--max-budget-usd", f"{req.max_budget_usd:.2f}"]
        if req.schema is not None:
            args += ["--json-schema", json.dumps(req.schema, ensure_ascii=False)]
        if req.mcp_servers:
            cfg = workdir.parent / "mcp.json"
            cfg.write_text(json.dumps({"mcpServers": req.mcp_servers}), encoding="utf-8")
            args += ["--mcp-config", str(cfg)]
        if req.allowed_tools:
            args += ["--allowedTools", ",".join(req.allowed_tools)]
        if req.disallowed_tools:
            args += ["--disallowedTools", ",".join(req.disallowed_tools)]
        if req.settings is not None:
            st = workdir.parent / "settings.json"
            st.write_text(json.dumps(req.settings, ensure_ascii=False), encoding="utf-8")
            args += ["--settings", str(st)]
        return args

    def run(
        self, req: ClaudeRequest, on_event: Callable[[ToolCall], None] | None = None
    ) -> ClaudeResult:
        attempt = 0
        while True:
            try:
                return self._run_once(req, on_event)
            except ClaudeError as exc:
                if not exc.transient or attempt >= self.retries:
                    raise
                attempt += 1
                log.warning("claude call failed (%s): retrying once", exc.kind)
                time.sleep(self.retry_delay_s)

    def run_once(
        self, req: ClaudeRequest, on_event: Callable[[ToolCall], None] | None = None
    ) -> ClaudeResult:
        """Sin reintento (escrituras: un reintento ciego podria duplicar)."""
        return self._run_once(req, on_event)

    def _run_once(
        self, req: ClaudeRequest, on_event: Callable[[ToolCall], None] | None
    ) -> ClaudeResult:
        cmd = self.command()
        env = self.env(req.extra_env or None)
        with tempfile.TemporaryDirectory(prefix="micnaga-claude-") as tmp:
            workdir = Path(tmp) / "cwd"
            workdir.mkdir()
            args = self.build_args(req, workdir)
            job = _Job()
            started = time.monotonic()
            try:
                proc = subprocess.Popen(
                    [*cmd, *args],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(workdir),
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                    close_fds=True,
                )
            except OSError as exc:
                job.close()
                raise ClaudeNotFoundError(f"cannot start claude: {exc}") from exc
            job.assign(proc)
            try:
                return self._communicate(proc, req, on_event, started)
            finally:
                if proc.poll() is None:
                    kill_tree(proc.pid)
                job.close()

    def _communicate(
        self,
        proc: subprocess.Popen[bytes],
        req: ClaudeRequest,
        on_event: Callable[[ToolCall], None] | None,
        started: float,
    ) -> ClaudeResult:
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        lines: queue.Queue[str | None] = queue.Queue()
        err_tail: list[bytes] = []

        def feed() -> None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(req.prompt.encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass

        def read_out() -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                lines.put(raw.decode("utf-8", "replace"))
            lines.put(None)

        def read_err() -> None:
            assert proc.stderr is not None
            for raw in iter(proc.stderr.readline, b""):
                err_tail.append(raw)
                del err_tail[:-40]

        threads = [
            threading.Thread(target=f, name=f"claude-{f.__name__}", daemon=True)
            for f in (feed, read_out, read_err)
        ]
        for t in threads:
            t.start()
        deadline = started + req.timeout_s

        def stream() -> Any:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    kill_tree(proc.pid)
                    raise ClaudeTimeoutError(f"claude took more than {req.timeout_s:.0f} s")
                try:
                    item = lines.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if item is None:
                    return
                yield item

        try:
            result = parse_stream(stream(), on_event)
        except ClaudeOutputError as exc:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(5)
            stderr = b"".join(err_tail).decode("utf-8", "replace")
            if stderr.strip():
                cls = classify(stderr)
                if cls is not ClaudeRunError:
                    raise cls(
                        f"claude failed (exit {proc.returncode})", detail=stderr[-500:]
                    ) from exc
            raise ClaudeOutputError(
                f"{exc} (exit {proc.returncode})", detail=stderr[-500:]
            ) from exc
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(10)
        result.duration_s = result.duration_s or (time.monotonic() - started)
        return result


def mcp_allowed_tools(server: str, tools: list[str] | None = None) -> list[str]:
    """``["mcp__micnaga__*"]`` o la lista explicita ``mcp__<server>__<tool>``."""
    if tools is None:
        return [f"mcp__{server}__*"]
    return [f"mcp__{server}__{t}" for t in tools]
