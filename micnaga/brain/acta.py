"""``acta.json`` (fuente de verdad) y ``acta.md`` en la carpeta de la reunion.

``acta.json``::

    {"version": 1, "prompt_version": 1, "model": "...", "transcript_hash": "...",
     "created": "...", "cost_usd": 0.05, "duration_s": 20.1, "num_turns": 8,
     "titulo": "...", "resumen": ["..."], "decisiones": [{"texto", "seg", "t"}],
     "tareas": [{"texto", "responsable_id", "responsable", "es_mia", "fecha", "seg", "t"}],
     "preguntas_abiertas": [...], "proximos_pasos": [...],
     "propuestas": [{"id", "grupo": "correcciones"|"crm", "tipo", "titulo"?, "motivo",
                     "confianza", "seg", "t", "datos", "checked", "aviso",
                     "estado": "pendiente"|"aplicada"|"fallida"|"deshecha", "deshacer", "error"}],
     "odoo": {"estado": "conectado"|"sin_configurar"|"sin_conexion", ...},
     "aprendizajes": {"anadidos": n, "rechazados": n}}

``acta.md`` es lo legible (y lo que va como nota al historial de Odoo).
"""

from __future__ import annotations

import html
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from micnaga.pipeline.output import format_mmss

ACTA_JSON = "acta.json"
ACTA_MD = "acta.md"
LIVE_TITLE_JSON = "titulo_provisional.json"
ACTA_VERSION = 1


def _atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_acta(folder: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((folder / ACTA_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_acta(folder: Path, acta: dict[str, Any]) -> None:
    _atomic(folder / ACTA_JSON, json.dumps(acta, ensure_ascii=False, indent=2))
    _atomic(folder / ACTA_MD, render_markdown(acta))


def _when(t: object) -> str:
    return f" ({format_mmss(float(t))})" if isinstance(t, int | float) else ""


def render_markdown(acta: dict[str, Any]) -> str:
    lines = [f"# {acta.get('titulo') or 'Acta'}", ""]
    for s in acta.get("resumen") or []:
        lines.append(s)
    lines.append("")

    def block(title: str, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        lines.append(f"## {title}")
        for it in items:
            lines.append(f"- {it.get('texto', '')}{_when(it.get('t'))}")
        lines.append("")

    block("Decisiones", acta.get("decisiones") or [])
    tareas = acta.get("tareas") or []
    if tareas:
        lines.append("## Tareas")
        for t in tareas:
            who = t.get("responsable") or "—"
            when = f" · {t['fecha']}" if t.get("fecha") else ""
            lines.append(f"- [ ] {who}: {t.get('texto', '')}{when}{_when(t.get('t'))}")
        lines.append("")
    block("Preguntas abiertas", acta.get("preguntas_abiertas") or [])
    block("Próximos pasos", acta.get("proximos_pasos") or [])
    contrasts = acta.get("contrastes") or []  # Google: solo si hay discrepancia real
    if contrasts:
        lines.append("## Contrastes")
        for c in contrasts:
            src = f"[{c['fuente']}]({c['url']})" if c.get("url") else c.get("fuente", "")
            lines.append(
                f"- Dicho: {c.get('dicho', '')}{_when(c.get('t'))} · Escrito: "
                f"{c.get('escrito', '')} ({src})"
            )
        lines.append("")
    links = acta.get("enlaces") or []
    if links:
        lines.append("## Enlaces")
        lines += [f"- [{x.get('titulo', '')}]({x.get('url', '')})" for x in links]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_note_html(acta: dict[str, Any]) -> str:
    """Nota interna para Odoo (HTML sencillo, escapado)."""
    e = html.escape
    parts = [f"<p><b>Acta · {e(acta.get('titulo') or '')}</b> <i>(Micnaga)</i></p>"]
    res = acta.get("resumen") or []
    if res:
        parts.append("<p>" + " ".join(e(s) for s in res) + "</p>")

    def ul(title: str, items: list[str]) -> None:
        if items:
            parts.append(f"<p><b>{e(title)}</b></p><ul>")
            parts.extend(f"<li>{e(x)}</li>" for x in items)
            parts.append("</ul>")

    ul("Decisiones", [d.get("texto", "") for d in acta.get("decisiones") or []])
    ul(
        "Tareas",
        [
            f"{t.get('responsable') or '—'}: {t.get('texto', '')}"
            + (f" ({t['fecha']})" if t.get("fecha") else "")
            for t in acta.get("tareas") or []
        ],
    )
    ul("Abierto", [q.get("texto", "") for q in acta.get("preguntas_abiertas") or []])
    return "".join(parts)


def fts_text(acta: dict[str, Any]) -> str:
    chunks: list[str] = [str(acta.get("titulo") or "")]
    chunks += [str(s) for s in acta.get("resumen") or []]
    for key in ("decisiones", "tareas", "preguntas_abiertas", "proximos_pasos"):
        for it in acta.get(key) or []:
            chunks.append(str(it.get("texto", "")))
            if it.get("responsable"):
                chunks.append(str(it["responsable"]))
    return "\n".join(c for c in chunks if c)


def pending_count(acta: dict[str, Any] | None) -> int:
    if not acta:
        return 0
    return sum(1 for p in acta.get("propuestas") or [] if p.get("estado") == "pendiente")


def read_live_title(folder: Path) -> str | None:
    try:
        data = json.loads((folder / LIVE_TITLE_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    title = data.get("titulo") if isinstance(data, dict) else None
    return title if isinstance(title, str) and title.strip() else None


def write_live_title(folder: Path, title: str, **extra: Any) -> None:
    _atomic(folder / LIVE_TITLE_JSON, json.dumps({"titulo": title, **extra}, ensure_ascii=False))
