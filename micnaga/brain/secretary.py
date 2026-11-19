"""La pasada de la Secretaria sobre una reunion ya transcrita (design §5).

``Secretary.analyze(folder)``:

1. Lee ``transcripcion.json``; si ``acta.json`` ya corresponde a esa transcripcion (huella +
   version de prompt + modelo) no repite la llamada.
2. Odoo (si esta configurado y responde): da a ``claude`` el servidor MCP de solo lectura. Si el
   evento ``system/init`` no lo muestra conectado, las propuestas de CRM se descartan (el modelo
   no pudo comprobar nada).
3. ``claude -p`` (Sonnet por defecto) con la reunion por stdin -> acta + correcciones +
   propuestas + aprendizajes. Todo pasa por ``micnaga.brain.plan`` antes de guardarse.
4. Escribe ``acta.json``/``acta.md``, indexa el acta en ``acta_fts``, guarda los aprendizajes
   validados y, si el titulo actual es generico, retitula la reunion (transcripcion + carpeta +
   indice en una sola transaccion).

La transcripcion nunca depende de esto: un fallo deja la reunion exactamente como estaba.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from micnaga import paths
from micnaga.brain import PROMPT_VERSION, learn, plan
from micnaga.brain import acta as acta_io
from micnaga.brain import store as bstore
from micnaga.brain.claude import (
    _ENV_ALLOW,
    ClaudeOutputError,
    ClaudeRequest,
    ClaudeResult,
    ClaudeRunner,
    ToolCall,
    mcp_allowed_tools,
)
from micnaga.brain.prompt import build_input, system_prompt, transcript_hash
from micnaga.brain.schema import acta_schema
from micnaga.config import Config
from micnaga.pipeline import output
from micnaga.types import Transcript

log = logging.getLogger(__name__)

MCP_SERVER = "micnaga"
_GENERIC = re.compile(
    r"^(reuni[oó]n|meet|meeting|llamada|google meet|sin t[ií]tulo|untitled|navegador|"
    r"(microsoft )?teams|whatsapp|zoom|discord|slack|skype|webex|[\w.-]+\.exe|"
    r"meet\s*[–-]\s*[a-z]{3}-[a-z]{4}-[a-z]{3}|[a-z]{3}-[a-z]{4}-[a-z]{3})$",
    re.IGNORECASE,
)

StatusCallback = Callable[[dict[str, Any]], None]


def is_generic_title(title: str) -> bool:
    t = " ".join(title.split()).strip(" .")
    return not t or bool(_GENERIC.match(t))


def mcp_command() -> list[str]:
    """Como lanzar el servidor MCP: el ``micnaga-cli.exe`` hermano (congelado) o este Python."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        cli = exe.with_name("micnaga-cli.exe")
        return [str(cli if cli.is_file() else exe), "tools"]
    return [sys.executable, "-m", "micnaga.brain.mcp"]


def mcp_env(config: Config) -> dict[str, str]:
    """Entorno explicito del servidor MCP (no se confia en lo que herede de ``claude``)."""
    upper = {k.upper(): v for k, v in os.environ.items()}
    env = {k: upper[k] for k in _ENV_ALLOW if k in upper}
    package_root = str(Path(__file__).resolve().parents[2])
    env.update(
        PYTHONPATH=package_root,
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        MICNAGA_HOME=str(paths.app_data_dir()),
        MICNAGA_STORAGE=str(paths.meetings_root(config)),
        MICNAGA_ODOO_URL=config.secretaria.odoo_url,
        MICNAGA_ODOO_DB=config.secretaria.odoo_db,
        MICNAGA_ODOO_LOGIN=config.secretaria.odoo_login,
    )
    from micnaga.brain.secrets import ENV_SECRETS_FILE

    if os.environ.get(ENV_SECRETS_FILE):  # solo pruebas
        env[ENV_SECRETS_FILE] = os.environ[ENV_SECRETS_FILE]
    return env


def extra_mcp_servers(config: Config) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Servidores MCP de contexto (``secretaria.mcp_servers``) y sus herramientas permitidas."""
    servers: dict[str, dict[str, Any]] = {}
    allowed: list[str] = []
    for line in config.secretaria.mcp_servers:
        try:
            spec = json.loads(line)
        except ValueError:
            continue
        name = str(spec.get("name", ""))
        if not name or name == MCP_SERVER:
            continue
        servers[name] = {
            "type": "stdio",
            "command": spec["command"],
            "args": list(spec.get("args", [])),
            "env": dict(spec.get("env", {})),
        }
        allowed += mcp_allowed_tools(name, list(spec.get("tools", [])))
    return servers, allowed


def glossary_lines(config: Config) -> list[str]:
    from micnaga.glossary import glossary_path

    lines: list[str] = []
    f = glossary_path()
    if f.is_file():
        for raw in f.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if s and not s.startswith("#"):
                lines.append(s)
    lines += [g for g in config.processing.glossary if g.strip()]
    return lines[:200]


@dataclass
class AnalysisResult:
    folder: Path
    acta: dict[str, Any]
    proposals: int
    cached: bool = False
    renamed: bool = False
    result: ClaudeResult | None = None


OdooFactory = Callable[[Config], Any]


def default_odoo_factory(config: Config) -> Any:
    from micnaga.brain.odoo import client_from_config

    s = config.secretaria
    return client_from_config(s.odoo_url, s.odoo_db, s.odoo_login)


class Secretary:
    def __init__(
        self,
        config: Config,
        *,
        runner: ClaudeRunner | None = None,
        odoo_factory: OdooFactory | None = None,
        db_factory: Callable[[], Any] | None = None,
        timeout_s: float = 300.0,
    ) -> None:
        self.config = config
        self.runner = runner or ClaudeRunner(auth=config.secretaria.auth)  # type: ignore[arg-type]
        self._odoo_factory = odoo_factory or default_odoo_factory
        self._db_factory = db_factory
        self.timeout_s = timeout_s

    # ------------------------------------------------------------------ odoo

    def _odoo(self) -> tuple[Any | None, dict[str, Any]]:
        try:
            client = self._odoo_factory(self.config)
        except Exception as exc:
            return None, {"estado": "error", "detalle": str(exc)[:120]}
        if client is None:
            return None, {"estado": "sin_configurar"}
        try:
            uid = client.authenticate()
        except Exception as exc:
            kind = getattr(exc, "kind", "failed")
            return None, {"estado": "clave" if kind == "auth" else "sin_conexion"}
        return client, {"estado": "conectado", "uid": uid}

    def _google(self, folder: Path) -> Any:
        """Lo que Google anade a esta pasada (``None`` si no hay nada activado)."""
        try:
            from micnaga.brain.google.analysis import prepare

            return prepare(self.config, folder)
        except Exception:
            log.exception("google preparation failed: analysing without Google")
            return None

    # ------------------------------------------------------------------ pasada

    def analyze(
        self,
        folder: Path,
        *,
        force: bool = False,
        on_status: StatusCallback | None = None,
        today: str | None = None,
    ) -> AnalysisResult:
        transcript = output.read_transcript(folder)
        digest = transcript_hash(transcript)
        model = self.config.secretaria.model_acta
        previous = acta_io.read_acta(folder)
        if (
            not force
            and previous is not None
            and previous.get("transcript_hash") == digest
            and previous.get("prompt_version") == PROMPT_VERSION
            and previous.get("model") == model
        ):
            return AnalysisResult(folder, previous, acta_io.pending_count(previous), cached=True)

        status = on_status or (lambda _s: None)
        status({"fase": "leyendo"})
        odoo, odoo_info = self._odoo()
        with_crm = odoo is not None
        servers: dict[str, dict[str, Any]] = {
            MCP_SERVER: {
                "type": "stdio",
                "command": mcp_command()[0],
                "args": mcp_command()[1:],
                "env": mcp_env(self.config),
            }
        }
        allowed = mcp_allowed_tools(MCP_SERVER)
        extra, extra_allowed = extra_mcp_servers(self.config)
        servers.update(extra)
        allowed += extra_allowed
        odoo_text = None
        if with_crm:
            odoo_text = (
                "Conectado (solo lectura) con las herramientas mcp__micnaga__*. "
                "Compruébalo todo antes de proponer."
            )
        req = ClaudeRequest(
            prompt=build_input(
                transcript,
                context=learn.load_context(),
                glossary=glossary_lines(self.config),
                odoo=odoo_text,
            ),
            system_prompt=system_prompt("secretaria"),
            model=model,
            schema=acta_schema(with_crm=with_crm),
            mcp_servers=servers,
            allowed_tools=allowed,
            max_turns=30 if with_crm else 12,
            timeout_s=self.timeout_s,
        )
        gplan = self._google(folder)  # Google (solo lectura): micnaga.brain.google.analysis
        if gplan is not None:
            gplan.extend(req, today=today or datetime.now().date().isoformat())

        def on_tool(call: ToolCall) -> None:
            status(
                {
                    "fase": "herramienta",
                    "servidor": call.server,
                    "tool": call.tool,
                    "arg": _first_arg(call.input),
                }
            )

        result = self.runner.run(req, on_event=on_tool)
        data = result.structured
        if data is None:
            raise ClaudeOutputError("claude returned no structured output")
        if with_crm and result.mcp_servers.get(MCP_SERVER) != "connected":
            log.warning(
                "micnaga MCP server not connected (%s): dropping CRM proposals",
                result.mcp_servers.get(MCP_SERVER),
            )
            odoo_info = {"estado": "sin_herramientas"}
            data = {**data, "propuestas": []}
        status({"fase": "comprobando"})
        acta = plan.build_acta(data, transcript)
        proposals = plan.build_corrections(data.get("correcciones"), transcript)
        if odoo is not None and odoo_info.get("estado") == "conectado":
            proposals += plan.build_crm(data.get("propuestas"), transcript, odoo)
        if gplan is not None:
            proposals = gplan.finish(data, result, transcript, acta, proposals)
        plan.number(proposals)
        outcome = learn.add_learnings(data.get("aprendizajes") or [], today=today)
        acta.update(
            version=acta_io.ACTA_VERSION,
            prompt_version=PROMPT_VERSION,
            model=model,
            model_id=result.model,
            transcript_hash=digest,
            created=datetime.now().astimezone().isoformat(timespec="seconds"),
            cost_usd=round(result.cost_usd, 4),
            duration_s=round(result.duration_s, 1),
            num_turns=result.num_turns,
            tool_calls=len(result.tool_calls),
            propuestas=proposals,
            odoo=odoo_info,
            aprendizajes={"anadidos": len(outcome.added), "rechazados": len(outcome.rejected)},
        )
        acta_io.write_acta(folder, acta)
        final, renamed = self.retitle(folder, transcript, acta.get("titulo") or "")
        self._index(final, acta)
        status({"fase": "lista"})
        return AnalysisResult(
            final, acta, acta_io.pending_count(acta), renamed=renamed, result=result
        )

    # ------------------------------------------------------------------ indice y titulo

    def _with_db(self, fn: Callable[[Any], Any]) -> Any:
        if self._db_factory is None:
            from micnaga.store.db import open_database

            db = open_database(self.config)
        else:
            db = self._db_factory()
        try:
            bstore.ensure_schema(db)
            return fn(db)
        finally:
            db.close()

    def _index(self, folder: Path, acta: dict[str, Any]) -> None:
        try:
            self._with_db(
                lambda db: bstore.index_acta(
                    db, folder.name, acta.get("titulo") or "", acta_io.fts_text(acta)
                )
            )
        except Exception:
            log.exception("could not index the acta")

    def retitle(self, folder: Path, transcript: Transcript, title: str) -> tuple[Path, bool]:
        """Pone ``title`` si el actual es generico. Devuelve ``(carpeta final, renombrada)``."""
        title = " ".join(title.split()).strip()
        if not title or not is_generic_title(transcript.meeting.title):
            return folder, False
        return retitle_meeting(folder, title, self._with_db)


def retitle_meeting(
    folder: Path, title: str, with_db: Callable[[Callable[[Any], Any]], Any]
) -> tuple[Path, bool]:
    """Titulo nuevo en ``transcripcion.json`` (+ MD/SRT), carpeta ``YYYY-MM-DD_HHMM_slug`` y
    todo lo que lleva el nombre de la carpeta en la base, en una transaccion. Si la carpeta no se
    puede renombrar (un FLAC abierto en la biblioteca...) se queda solo el titulo."""
    transcript = output.read_transcript(folder)
    updated = replace(transcript, meeting=replace(transcript.meeting, title=title))
    output.write_transcript(folder, updated)
    try:
        start = datetime.fromisoformat(transcript.meeting.start)
        target = paths.unique_folder(folder.parent, paths.meeting_folder_name(start, title))
    except ValueError:
        target = folder
    if target == folder or target.name == folder.name:
        with_db(lambda db: bstore.set_meeting_title(db, folder.name, title))
        return folder, False
    with_db(
        lambda db: bstore.rename_folder(
            db, folder.name, target.name, new_path=str(target), title=title
        )
    )
    for _ in range(5):
        try:
            os.replace(folder, target)
            return target, True
        except OSError:
            time.sleep(0.3)
    log.warning("meeting folder is busy; keeping the old folder name")
    with_db(
        lambda db: bstore.rename_folder(
            db, target.name, folder.name, new_path=str(folder), title=title
        )
    )
    return folder, False


def _first_arg(args: dict[str, Any]) -> str | None:
    for key in ("texto", "id", "partner_id", "modelo"):
        v = args.get(key)
        if isinstance(v, str | int) and not isinstance(v, bool):
            return str(v)[:60]
    return None
