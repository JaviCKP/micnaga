"""La pasada de la Secretaria sobre una reunion ya transcrita (design §5).

``Secretary.analyze(folder)``:

1. Lee ``transcripcion.json``; si ``acta.json`` ya corresponde a esa transcripcion (huella +
   version de prompt + modelo) no repite la llamada.
2. Odoo (si esta configurado y responde): perfil de capacidades del usuario
   (``capabilities.ensure``: modulos + ``check_access_rights``, una vez al dia) -> herramientas
   MCP de solo lectura que se ofrecen y tipos de propuesta permitidos. Si el evento
   ``system/init`` no muestra el servidor conectado, las propuestas de Odoo se descartan (el
   modelo no pudo comprobar nada).
3. ``claude -p`` (Sonnet por defecto) con la reunion por stdin -> acta + correcciones +
   propuestas + aprendizajes. Todo pasa por ``micnaga.brain.plan`` antes de guardarse.
4. Escribe ``acta.json``/``acta.md``, indexa el acta en ``acta_fts``, guarda los aprendizajes
   validados y, si el titulo actual es generico, retitula la reunion (transcripcion + carpeta +
   indice en una sola transaccion).

La transcripcion nunca depende de esto: un fallo deja la reunion exactamente como estaba.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from micnaga import paths
from micnaga.brain import PROMPT_VERSION, learn, plan
from micnaga.brain import acta as acta_io
from micnaga.brain import store as bstore
from micnaga.brain.claude import (
    _ENV_ALLOW,
    ClaudeError,
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
    r"^(reuni[oó]n|reuni[oó]n presencial|meet|meeting|llamada|google meet|sin t[ií]tulo|untitled|navegador|"
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


def mcp_env(
    config: Config, *, tools: list[str] | None = None, today: str | None = None
) -> dict[str, str]:
    """Entorno explicito del servidor MCP (no se confia en lo que herede de ``claude``).

    ``tools``: herramientas de Odoo permitidas por el perfil de capacidades (``MICNAGA_TOOLS``).
    """
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
    if tools is not None:
        env["MICNAGA_TOOLS"] = ",".join(tools)
    if today:
        env["MICNAGA_TODAY"] = today
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


def odoo_brief(profile: dict[str, Any] | None, today: str) -> str:
    """Lo que la Secretaria sabe de este Odoo antes de empezar (va en el texto de entrada)."""
    from micnaga.brain import capabilities

    lines = [
        "Conectado (solo lectura) con las herramientas mcp__micnaga__*. "
        "Compruébalo todo antes de proponer.",
        f"Hoy es {today}.",
    ]
    if profile is not None:
        text = capabilities.summary_text(profile) or "solo lectura básica"
        lines.append(f"Puedes proponer sobre: {text}.")
        targets = capabilities.note_targets(profile)
        names = {
            "project.task": "tareas",
            "project.project": "proyectos",
            "crm.lead": "oportunidades",
            "res.partner": "contactos",
        }
        if targets:
            lines.append(
                "Notas internas y actividades solo sobre: "
                + ", ".join(names.get(t, t) for t in targets)
                + "."
            )
        else:
            lines.append("No puedes dejar notas ni actividades en ningún registro.")
    return "\n".join(lines)


class Secretary:
    def __init__(
        self,
        config: Config,
        *,
        runner: ClaudeRunner | None = None,
        odoo_factory: OdooFactory | None = None,
        db_factory: Callable[[], Any] | None = None,
        timeout_s: float = 300.0,
        budget_s: float = 420.0,
    ) -> None:
        self.config = config
        self.budget_s = budget_s
        """Tope de la pasada entera (con su posible repeticion por no haber investigado)."""
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
        if hasattr(client, "read_only"):
            client.read_only = True  # analizar nunca escribe (ni siquiera validando el plan)
        return client, {"estado": "conectado", "uid": uid}

    def _google(self, folder: Path) -> Any:
        """Lo que Google anade a esta pasada (``None`` si no hay nada activado)."""
        try:
            from micnaga.brain.google.analysis import prepare

            return prepare(self.config, folder)
        except Exception:
            log.exception("google preparation failed: analysing without Google")
        return None

    def _candidates(
        self, folder: Path, transcript: Transcript, terms: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Reuniones anteriores con los mismos participantes y, aunque nadie tenga nombre, las
        que hablan de lo mismo (empresas, nombres y siglas dichos; ``micnaga.brain.research``)."""
        try:
            from micnaga.brain import research

            people = research.people_for(transcript, folder)
            words = research.search_terms(terms or {})
            if not people and not words:
                return []
            root = paths.meetings_root(self.config)

            def lookup(db: Any) -> list[dict[str, Any]]:
                found = research.related(db, root, people, exclude=folder.name) if people else []
                seen = {m["carpeta"] for m in found}
                more = research.prior_by_terms(db, root, words, exclude=folder.name)
                found += [m for m in more if m["carpeta"] not in seen]
                return found[: research.MAX_RELATED + 1]

            out: list[dict[str, Any]] = self._with_db(lookup)
            return out
        except Exception:
            log.exception("previous meetings lookup failed")
            return []

    # ------------------------------------------------------------------ limpieza

    def clean(self, folder: Path, *, on_status: StatusCallback | None = None) -> Any:
        """Limpieza de la transcripcion con Haiku (``micnaga.brain.cleanup``), una vez por
        reunion y antes del acta. Nunca falla: sin Claude se salta en silencio."""
        if not self.config.secretaria.clean_transcript:
            return None
        try:
            from micnaga.brain.cleanup import Cleaner

            stats = Cleaner(self.config, self.runner, with_db=self._with_db).run(
                folder, on_status=on_status
            )
            if stats is not None:
                log.info(
                    "transcript cleanup: %d/%d segments, %d merges, %d rejected, %.1f s, $%.4f%s",
                    stats.corrected,
                    stats.segments,
                    stats.merges,
                    stats.rejected,
                    stats.seconds,
                    stats.cost_usd,
                    f" (skipped: {stats.skipped})" if stats.skipped else "",
                )
            return stats
        except Exception:
            log.exception("transcript cleanup failed: keeping the ASR text")
            return None

    def _profile(self, client: Any) -> dict[str, Any] | None:
        """Perfil de capacidades (guardado si es de hoy). Sin el, solo lo de CRM de siempre."""
        if not hasattr(client, "cfg"):
            return None
        try:
            from micnaga.brain import capabilities

            return capabilities.ensure(client)
        except Exception as exc:
            log.warning("capability discovery failed: %s", getattr(exc, "kind", type(exc)))
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
        day = today or date.today().isoformat()
        profile = self._profile(odoo) if odoo is not None else None
        tools = None
        types = None
        if profile is not None:
            from micnaga.brain import capabilities
            from micnaga.brain.mcp import EVERY_ODOO_TOOL
            from micnaga.brain.schema import PROPOSAL_TYPES

            tools = capabilities.tools_for(profile, EVERY_ODOO_TOOL)
            types = capabilities.actions_for(profile, PROPOSAL_TYPES)
            odoo_info["capacidades"] = capabilities.summary_text(profile)
        servers: dict[str, dict[str, Any]] = {
            MCP_SERVER: {
                "type": "stdio",
                "command": mcp_command()[0],
                "args": mcp_command()[1:],
                "env": mcp_env(self.config, tools=tools, today=today),
            }
        }
        allowed = mcp_allowed_tools(MCP_SERVER)
        extra, extra_allowed = extra_mcp_servers(self.config)
        servers[MCP_SERVER]["env"]["MICNAGA_CURRENT"] = folder.name  # no es «anterior»
        servers.update(extra)
        allowed += extra_allowed
        odoo_text = odoo_brief(profile, day) if with_crm else None
        req = ClaudeRequest(
            prompt=build_input(
                transcript,
                context=learn.load_context(odoo_context_key(self.config)),
                glossary=glossary_lines(self.config),
                odoo=odoo_text,
                user_name=(str(profile.get("user") or "") or None) if profile else None,
            ),
            system_prompt=system_prompt("secretaria"),
            model=model,
            schema=acta_schema(with_crm=with_crm, types=types),
            mcp_servers=servers,
            allowed_tools=allowed,
            max_turns=40 if with_crm else 20,  # investigar primero cuesta turnos
            timeout_s=self.timeout_s,
        )
        from micnaga.brain import research

        terms = research.extract(transcript)
        candidates = self._candidates(folder, transcript, terms)
        gplan = self._google(folder)  # Google (solo lectura): micnaga.brain.google.analysis
        services = list(gplan.services) if gplan else []
        research.extend(
            req,
            research.brief(
                candidates, odoo=with_crm, google=services, mail=bool(extra), terms=terms
            ),
        )
        if gplan is not None:
            gplan.extend(req, today=today or datetime.now().date().isoformat())
        req.prompt += research.reminder()  # tras una transcripcion larga, lo ultimo que lee

        def on_tool(call: ToolCall) -> None:
            status(
                {
                    "fase": "herramienta",
                    "servidor": call.server,
                    "tool": call.tool,
                    "arg": _first_arg(call.input),
                }
            )

        started = time.monotonic()
        result = self.runner.run(req, on_event=on_tool)
        gaps = research.missing(
            result, odoo=with_crm, gmail="gmail" in services, mail_servers=list(extra)
        )
        remaining = self.budget_s - (time.monotonic() - started)
        if _placeholder(result.structured, transcript) or (gaps and remaining > 60):
            # visto en la evaluacion real: un «titulo: x» con todo vacio, o un acta sin haber
            # investigado nada (47 min, 0 herramientas). Una sola repeticion, con tope de tiempo.
            log.warning("acta without research (%s) or placeholder: running it again", gaps)
            retry = replace(req, timeout_s=max(60.0, min(req.timeout_s, remaining)))
            if gaps:
                calls = research.first_calls(
                    terms, odoo=with_crm, gmail="gmail" in services, mail=bool(extra)
                )
                retry.prompt = req.prompt + research.nudge(gaps, calls)
            first = result
            try:
                result = self.runner.run(retry, on_event=on_tool)
            except ClaudeError:
                if _placeholder(first.structured, transcript) or first.structured is None:
                    raise
                log.warning("second pass failed: keeping the first acta")
                result = first
            else:
                if _placeholder(result.structured, transcript) and not _placeholder(
                    first.structured, transcript
                ):
                    result = first
            if result is not first:
                result.cost_usd += first.cost_usd
                result.tool_calls = first.tool_calls + result.tool_calls
                result.tool_results = first.tool_results + result.tool_results
        data = result.structured
        if data is None or _placeholder(data, transcript):
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
        seen = research.Seen(
            [r.text for r in result.tool_results]
            + [json.dumps(r.structured, ensure_ascii=False) for r in result.tool_results],
            root=paths.meetings_root(self.config),
            current=folder.name,
            candidates=candidates,
            odoo_url=self.config.secretaria.odoo_url if with_crm else "",
        )
        acta.update(research.validate(data, transcript, seen))
        known = [n for m in candidates for n in m.get("participantes") or []]
        calendar = "\n".join(known)
        if gplan is not None and gplan.context:
            calendar += "\n" + json.dumps(gplan.context, ensure_ascii=False)
        proposals = [  # un nombre para un hablante, solo si se dijo o se vio en alguna parte
            p
            for p in plan.build_corrections(data.get("correcciones"), transcript)
            if p["tipo"] != "hablante"
            or research.name_seen(p["datos"]["a"], transcript, seen.text, calendar)
        ]
        if odoo is not None and odoo_info.get("estado") == "conectado":
            proposals += plan.build_crm(
                data.get("propuestas"),
                transcript,
                odoo,
                profile=profile,
                today=date.fromisoformat(day),
            )
        if gplan is not None:
            proposals = gplan.finish(data, result, transcript, acta, proposals)
        # regenerar nunca reutiliza un ``pN`` ni pierde lo ya aplicado (y su «Deshacer»)
        kept = plan.carried_over(previous)
        plan.number(proposals, start=plan.max_number(previous))
        top = plan.max_number({"propuestas": proposals, "ultimo_id": plan.max_number(previous)})
        proposals = [*kept, *proposals]
        if odoo is not None and odoo_info.get("estado") == "conectado":
            acta["opciones"] = plan.options(odoo, profile, proposals)
        outcome = learn.add_learnings(data.get("aprendizajes") or [], today=today)
        _remember_names(result, acta)
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
            ultimo_id=top,
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
        from micnaga.brain import full_acta

        text = acta_io.fts_text(acta)
        with contextlib.suppress(OSError):  # el acta completa tambien se encuentra
            text += "\n" + (folder / full_acta.FILE).read_text(encoding="utf-8")
        try:
            self._with_db(
                lambda db: bstore.index_acta(db, folder.name, acta.get("titulo") or "", text)
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
    with output.transcript_lock(folder):
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


def _placeholder(data: dict[str, Any] | None, transcript: Transcript) -> bool:
    """Un acta vacia para una reunion con contenido (titulo de relleno, sin resumen)."""
    if data is None or len(transcript.segments) < 3:
        return False
    title = str(data.get("titulo") or "").strip()
    return not data.get("resumen") and len(title) < 4


def _remember_names(result: ClaudeResult, acta: dict[str, Any]) -> None:
    """Nombres de Odoo vistos en esta pasada (proyectos, empresas, contactos): vocabulario para
    la limpieza de las proximas transcripciones."""
    try:
        from micnaga.brain import cleanup

        texts = [
            r.text
            for r in result.tool_results
            if r.name.rsplit("__", 1)[-1] in ("proyectos", "buscar_empresas", "buscar_contactos")
        ]
        names = cleanup.names_in_results(texts)
        names += [str(p.get("nombre")) for p in (acta.get("opciones") or {}).get("proyectos") or []]
        cleanup.remember_names(names)
    except Exception:
        log.debug("could not remember Odoo names", exc_info=True)


def _first_arg(args: dict[str, Any]) -> str | None:
    for key in ("texto", "id", "partner_id", "modelo"):
        v = args.get(key)
        if isinstance(v, str | int) and not isinstance(v, bool):
            return str(v)[:60]
    return None


def odoo_context_key(cfg: Any) -> str | None:
    """Clave de la conexion de Odoo configurada (para su ``contexto.md``)."""
    from micnaga.brain.odoo_learn import connection_key_for

    return connection_key_for(cfg.secretaria)
