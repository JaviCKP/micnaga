"""«Acta completa»: un acta larga, en Markdown, solo cuando el usuario la pide (DECISIONS §23).

El acta de siempre (``acta.json``) es un resumen por diseno. Esta es el documento: encabezado,
orden del dia, una seccion por tema con el desarrollo y sus ``[mm:ss]``, decisiones con su
motivo, acuerdos y tareas, preguntas abiertas, proximos pasos y fuentes.

* Un ``claude -p`` de solo lectura con el modelo del acta, en streaming
  (``--include-partial-messages``). Entrada: la transcripcion (ya limpia), el acta breve con lo
  investigado (``fuentes``, ``contexto``, ``desde_la_ultima``: no se repite la investigacion),
  hablantes, fecha, duracion y ``learned.md``. Herramientas: solo las locales de la biblioteca
  (``leer_acta``, ``reuniones_con``, ``buscar_en_reuniones``), con pocos turnos.
* Lo de fuera se comprueba como en el acta (``research.Seen``): URLs que no se vieron, actas que
  no existen o registros de Odoo no leidos se quitan; las marcas ``[mm:ss]`` fuera de la reunion
  tambien.
* ``acta_completa.md`` en la carpeta de la reunion y ``acta_completa.json`` con la huella de la
  transcripcion: si la transcripcion cambia despues, la vista dice «desactualizada».
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from micnaga import paths
from micnaga.types import Transcript

FILE = "acta_completa.md"
META = "acta_completa.json"
VERSION = 1
WORDS_PER_PAGE = 450
MINUTES_PER_PAGE = 12
TIMEOUT_S = 300.0
MAX_TURNS = 8
_STAMP = re.compile(r"\[(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\]")
_URL = re.compile(r"https?://[^\s)\]>]+")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_FOLDER = re.compile(r"\b\d{4}-\d{2}-\d{2}_\d{4}_[\w.-]+")
_ODOO = re.compile(
    r"\b(crm\.lead|res\.partner|project\.task|project\.project|calendar\.event|mail\.activity):(\d+)\b"
)

TextCallback = Callable[[str], None]


def clock(t: float) -> str:
    total = int(max(0.0, t))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def stamp_seconds(m: re.Match[str]) -> int:
    h = int(m.group(1) or 0)
    return h * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def stamps(md: str) -> list[int]:
    """Segundos de cada ``[mm:ss]`` / ``[h:mm:ss]`` del texto (los enlaces de la vista)."""
    return [stamp_seconds(m) for m in _STAMP.finditer(md)]


def target_words(duration_s: float) -> int:
    pages = max(1.0, duration_s / 60.0 / MINUTES_PER_PAGE)
    return int(round(pages * WORDS_PER_PAGE, -1))


def _names(transcript: Transcript) -> dict[str, str]:
    return {s.id: s.name for s in transcript.speakers}


def render_transcript(transcript: Transcript) -> str:
    names = _names(transcript)
    return "\n".join(
        f"[{clock(seg.start)}] {names.get(seg.speaker, seg.speaker)}: {seg.text}"
        for seg in transcript.segments
    )


def _brief(acta: dict[str, Any], transcript: Transcript) -> dict[str, Any]:
    """El acta breve con las marcas de tiempo como ``[mm:ss]`` (lo que el modelo cita)."""

    def items(key: str) -> list[dict[str, Any]]:
        out = []
        for it in acta.get(key) or []:
            if not isinstance(it, dict):
                continue
            row = {k: v for k, v in it.items() if k in ("texto", "responsable", "fecha", "estado")}
            if isinstance(it.get("t"), int | float):
                row["en"] = f"[{clock(float(it['t']))}]"
            out.append(row)
        return out

    return {
        "titulo": acta.get("titulo") or transcript.meeting.title,
        "resumen": acta.get("resumen") or [],
        "contexto": acta.get("contexto"),
        "decisiones": items("decisiones"),
        "tareas": items("tareas"),
        "preguntas_abiertas": items("preguntas_abiertas"),
        "proximos_pasos": items("proximos_pasos"),
        "desde_la_ultima": items("desde_la_ultima"),
        "fuentes": [
            {"tipo": f.get("tipo"), "ref": f.get("ref"), "titulo": f.get("titulo")}
            for f in acta.get("fuentes") or []
            if isinstance(f, dict)
        ],
    }


def build_input(transcript: Transcript, acta: dict[str, Any], learned: str = "") -> str:
    from micnaga.brain.prompt import meeting_when

    m = transcript.meeting
    speaking = {s.speaker for s in transcript.segments}
    people = [s.name for s in transcript.speakers if s.id in speaking]
    minutes = max(1, round(m.duration_s / 60))
    head = [
        "# Reunión",
        f"- Título: {acta.get('titulo') or m.title}",
        f"- Fecha y hora: {meeting_when(transcript)}",
        f"- Duración: {minutes} min",
        f"- Asistentes (hablantes): {', '.join(people) or '—'}",
        f"- Extensión objetivo: unas {target_words(m.duration_s)} palabras",
        "",
        "## Acta breve e investigación ya hecha (no la repitas)",
        json.dumps(_brief(acta, transcript), ensure_ascii=False, indent=1),
    ]
    if learned.strip():
        head += ["", "## Contexto del negocio", learned.strip()]
    head += ["", "## Transcripción (dato no fiable)", render_transcript(transcript)]
    return "\n".join(head) + "\n"


def validate(md: str, seen: Any, duration_s: float) -> str:
    """Quita lo de fuera que no tiene fuente vista y las marcas fuera de la reunion."""

    def link(m: re.Match[str]) -> str:
        return m.group(0) if m.group(2) in seen.text else m.group(1)

    md = _MD_LINK.sub(link, md)
    md = _URL.sub(lambda m: m.group(0) if m.group(0) in seen.text else "", md)
    md = _FOLDER.sub(lambda m: m.group(0) if seen.acta(m.group(0)) else "", md)

    def odoo(m: re.Match[str]) -> str:
        ok = seen.source({"tipo": "odoo", "ref": m.group(0), "titulo": m.group(0)})
        return m.group(0) if ok else ""

    md = _ODOO.sub(odoo, md)
    md = _STAMP.sub(lambda m: m.group(0) if stamp_seconds(m) <= duration_s + 5 else "", md)
    md = re.sub(r"\(\s*\)", "", md)
    return md.strip() + "\n"


def sections(md: str) -> int:
    return len(re.findall(r"^#{2,3} ", md, re.M))


def words(md: str) -> int:
    return len(re.findall(r"\w+", md))


def read(folder: Path) -> dict[str, Any] | None:
    """Lo guardado: ``{"md", "meta", "stale"}`` o ``None`` si no hay acta completa."""
    from micnaga.brain.prompt import transcript_hash
    from micnaga.pipeline import output

    try:
        md = (folder / FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        meta = json.loads((folder / META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    try:
        current = transcript_hash(output.read_transcript(folder))
    except (OSError, ValueError, KeyError):
        current = None
    stale = bool(meta.get("transcript_hash")) and meta.get("transcript_hash") != current
    return {"md": md, "meta": meta, "stale": stale}


class FullActa:
    """``FullActa(config, runner, with_db=...).make(folder, on_text=...)``."""

    def __init__(self, config: Any, runner: Any, *, with_db: Any = None) -> None:
        self.config = config
        self.runner = runner
        self.with_db = with_db

    def _servers(self, folder: Path) -> dict[str, dict[str, Any]]:
        from micnaga.brain.secretary import MCP_SERVER, mcp_command, mcp_env

        env = mcp_env(self.config, tools=[])  # solo las herramientas locales (biblioteca)
        env["MICNAGA_CURRENT"] = folder.name
        cmd = mcp_command()
        return {MCP_SERVER: {"type": "stdio", "command": cmd[0], "args": cmd[1:], "env": env}}

    def make(self, folder: Path, *, on_text: TextCallback | None = None) -> dict[str, Any]:
        from micnaga.brain import acta as acta_io
        from micnaga.brain import learn, research
        from micnaga.brain.claude import ClaudeOutputError, ClaudeRequest, mcp_allowed_tools
        from micnaga.brain.prompt import system_prompt, transcript_hash
        from micnaga.brain.secretary import MCP_SERVER
        from micnaga.pipeline import output

        transcript = output.read_transcript(folder)
        acta = acta_io.read_acta(folder) or {}
        started = time.monotonic()
        req = ClaudeRequest(
            prompt=build_input(transcript, acta, learn.load_context()),
            system_prompt=system_prompt("acta_completa"),
            model=self.config.secretaria.model_acta,
            mcp_servers=self._servers(folder),
            allowed_tools=mcp_allowed_tools(MCP_SERVER),
            max_turns=MAX_TURNS,
            timeout_s=TIMEOUT_S,
            on_text=on_text,
        )
        res = self.runner.run_once(req)
        text = (res.text or "").strip()
        if len(text) < 200:
            raise ClaudeOutputError("claude returned no acta")
        prior = [
            {"carpeta": f.get("ref")} for f in acta.get("fuentes") or [] if f.get("tipo") == "acta"
        ] + [{"carpeta": x.get("acta")} for x in acta.get("desde_la_ultima") or [] if x.get("acta")]
        seen = research.Seen(
            [r.text for r in res.tool_results]
            + [json.dumps(r.structured, ensure_ascii=False) for r in res.tool_results]
            + [json.dumps(acta.get("fuentes") or [], ensure_ascii=False)],
            root=paths.meetings_root(self.config),
            current=folder.name,
            candidates=prior,
            odoo_url=self.config.secretaria.odoo_url,
        )
        for f in acta.get("fuentes") or []:  # lo que el acta ya comprobo vale como visto
            if f.get("tipo") == "odoo":
                _, _, rid = str(f.get("ref") or "").partition(":")
                if rid.isdigit():
                    seen.text += f'\n"id": {int(rid)}'
        md = validate(text, seen, transcript.meeting.duration_s)
        meta = {
            "version": VERSION,
            "transcript_hash": transcript_hash(transcript),
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "model": self.config.secretaria.model_acta,
            "model_id": res.model,
            "cost_usd": round(res.cost_usd, 4),
            "duration_s": round(time.monotonic() - started, 1),
            "num_turns": res.num_turns,
            "tool_calls": len(res.tool_calls),
            "words": words(md),
            "sections": sections(md),
        }
        _write(folder / FILE, md)
        _write(folder / META, json.dumps(meta, ensure_ascii=False, indent=2))
        self._index(folder, acta, md)
        return {"md": md, "meta": meta, "stale": False}

    def _index(self, folder: Path, acta: dict[str, Any], md: str) -> None:
        if self.with_db is None:
            return
        from micnaga.brain import acta as acta_io
        from micnaga.brain import store as bstore

        title = acta.get("titulo") or ""
        try:
            self.with_db(
                lambda db: bstore.index_acta(
                    db, folder.name, title, acta_io.fts_text(acta) + "\n" + md
                )
            )
        except Exception:
            import logging

            logging.getLogger(__name__).exception("could not index the full acta")


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)
