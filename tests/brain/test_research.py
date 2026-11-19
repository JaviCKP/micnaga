"""Investigar primero: reuniones anteriores, «desde la ultima vez» y fuentes comprobadas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from micnaga import paths
from micnaga.brain import acta as acta_io
from micnaga.brain import research
from micnaga.brain.claude import ClaudeRequest
from micnaga.brain.mcp import Tools
from micnaga.brain.secretary import Secretary
from micnaga.config import Config
from micnaga.store.db import open_database
from tests.brain import research_case as case
from tests.brain.conftest import FakeClaude, read_log


def tools() -> Tools:
    return Tools(None, open_database, root=paths.meetings_root(), current=case.CURRENT)


def test_previous_meetings_by_person_and_the_full_acta() -> None:
    case.write_library()
    t = tools()
    found = t.call("reuniones_con", {"texto": "carlos"})
    assert [m["carpeta"] for m in found] == [case.PRIOR]  # la actual no cuenta
    assert found[0]["con_acta"] and found[0]["pendientes"] == 3
    assert found[0]["participantes"] == ["Carlos Ruiz"]
    full = t.call("leer_acta", {"carpeta": case.PRIOR})
    assert [x["texto"] for x in full["tareas"]] == [
        "Enviar el presupuesto revisado a Carlos",
        "Configurar las plantillas de recordatorio de WhatsApp",
    ]
    assert full["propuestas"][0]["odoo"] == f"project.task:{case.TASK_TEMPLATES}"
    assert t.call("leer_acta", {"carpeta": case.CURRENT}) == {"error": "es la reunion actual"}
    assert "error" in t.call("leer_acta", {"carpeta": "../fuera"})


def test_candidates_share_participants_and_go_in_the_brief() -> None:
    _, current = case.write_library()
    tr = case.transcript(case.CURRENT_LINES, "2026-11-19T10:30:00+01:00")
    with open_database() as db:
        found = research.related(
            db, paths.meetings_root(), research.people_for(tr, current), exclude=current.name
        )
    assert [m["carpeta"] for m in found] == [case.PRIOR]
    text = research.brief(found, odoo=True, google=[], mail=True)
    assert f"`{case.PRIOR}`" in text and "correo" in text and "a la vez" in text
    req = ClaudeRequest(prompt="x\n## Transcripción\n#0", system_prompt="", schema=_schema())
    research.extend(req, text)
    assert req.prompt.index("Investigación") < req.prompt.index("## Transcripción")
    assert req.schema is not None
    assert {"contexto", "desde_la_ultima", "fuentes"} <= set(req.schema["required"])


def _schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "required": []}


def test_only_what_was_seen_gets_in_and_nothing_is_claimed_without_proof() -> None:
    case.write_library()
    tr = case.transcript(case.CURRENT_LINES, "2026-11-19T10:30:00+01:00")
    seen = research.Seen(
        [json.dumps({"id": 302, "nombre": "Configurar plantillas"}), case.MAIL_URL],
        root=paths.meetings_root(),
        current=case.CURRENT,
        candidates=[{"carpeta": case.PRIOR}],
        odoo_url="https://odoo.example.com/",
    )
    mail = {"tipo": "gmail", "ref": case.MAIL_URL, "titulo": "Re: Presupuesto"}
    out = research.validate(
        {
            "contexto": "2.ª reunión con Carlos; la última, el 12/11",
            "desde_la_ultima": [
                {
                    "texto": "Presupuesto",
                    "estado": "hecho",
                    "acta": case.PRIOR,
                    "seg": 1,
                    "evidencia": mail,
                },
                {
                    "texto": "Plantillas",
                    "estado": "abierto",
                    "acta": case.PRIOR,
                    "seg": None,
                    "evidencia": {"tipo": "odoo", "ref": "project.task:302", "titulo": "Tarea"},
                },
                {
                    "texto": "Sin prueba",
                    "estado": "hecho",
                    "acta": case.PRIOR,
                    "seg": None,
                    "evidencia": None,
                },
                {
                    "texto": "Inventada",
                    "estado": "hecho",
                    "acta": "2026-01-01_x",
                    "seg": 1,
                    "evidencia": None,
                },
            ],
            "fuentes": [
                {"tipo": "gmail", "ref": "https://mail.google.com/no-visto", "titulo": "x"},
                {"tipo": "odoo", "ref": "project.task:999", "titulo": "no leido"},
                {
                    "tipo": "drive",
                    "ref": "https://evil.example.com/" + case.MAIL_URL,
                    "titulo": "x",
                },
            ],
        },
        tr,
        seen,
    )
    since = out["desde_la_ultima"]
    assert [(x["texto"], x["estado"]) for x in since] == [
        ("Presupuesto", "hecho"),
        ("Plantillas", "abierto"),
        ("Sin prueba", "desconocido"),
    ]
    assert since[0]["t"] == 6.0 and since[0]["evidencia"]["url"] == case.MAIL_URL
    odoo = since[1]["evidencia"]
    assert odoo["url"] == "https://odoo.example.com/web#id=302&model=project.task&view_type=form"
    assert {(f["tipo"], f["ref"]) for f in out["fuentes"]} == {
        ("acta", case.PRIOR),
        ("gmail", case.MAIL_URL),
        ("odoo", "project.task:302"),
    }
    assert out["contexto"].startswith("2.ª reunión")
    bare = research.validate({"contexto": "Cliente desde 2019"}, tr, seen)
    assert bare == {"contexto": None, "desde_la_ultima": [], "fuentes": []}


def test_markdown_has_context_since_and_one_sources_list() -> None:
    md = acta_io.render_markdown(
        {
            "titulo": "T",
            "contexto": "2.ª reunión",
            "desde_la_ultima": [
                {
                    "texto": "Presupuesto",
                    "estado": "hecho",
                    "t": 6.0,
                    "evidencia": {"tipo": "gmail", "titulo": "Re", "url": "u"},
                }
            ],
            "fuentes": [
                {"tipo": "acta", "ref": case.PRIOR, "titulo": "Anterior"},
                {"tipo": "gmail", "ref": "u", "titulo": "Re", "url": "u"},
            ],
            "enlaces": [{"titulo": "Re", "url": "u"}, {"titulo": "Doc", "url": "d"}],
        }
    )
    assert "_2.ª reunión_" in md and "## Desde la última vez" in md
    assert "- [x] Presupuesto (00:06) · Gmail: [Re](u)" in md
    assert md.count("](u)") == 2 and "Drive: [Doc](d)" in md and "## Enlaces" not in md


def test_a_full_pass_reads_the_previous_acta_and_fills_since(
    fake_claude: FakeClaude, secrets_file: Path
) -> None:
    """Con el ``claude`` falso: la llamada ``leer_acta`` va DE VERDAD al servidor MCP y lo que
    devuelve es lo que luego permite citar la reunion anterior."""
    _, current = case.write_library()
    structured = {
        "titulo": "Dental Triana · acceso al panel",
        "resumen": ["Presupuesto aprobado; plantillas a medias; acceso para recepción."],
        "decisiones": [],
        "tareas": [],
        "preguntas_abiertas": [],
        "proximos_pasos": [],
        "correcciones": [],
        "aprendizajes": [],
        "contexto": "2.ª reunión con Carlos; la última, el 12/11",
        "desde_la_ultima": [
            {
                "texto": "Enviar el presupuesto revisado",
                "estado": "hecho",
                "acta": case.PRIOR,
                "seg": 1,
                "evidencia": None,
            },
            {
                "texto": "Configurar las plantillas",
                "estado": "abierto",
                "acta": case.PRIOR,
                "seg": 2,
                "evidencia": None,
            },
        ],
        "fuentes": [{"tipo": "acta", "ref": case.PRIOR, "titulo": "Anterior"}],
    }
    log = fake_claude(
        {
            "structured": structured,
            "calls": [{"tool": "leer_acta", "args": {"carpeta": case.PRIOR}}],
        }
    )
    res = Secretary(Config()).analyze(current, today="2026-11-19")
    data = read_log(log)
    assert f"`{case.PRIOR}`" in data["stdin"]  # candidata en el brief
    assert data["mcp_config"]["micnaga"]["env"]["MICNAGA_CURRENT"] == case.CURRENT
    acta = res.acta
    assert [x["estado"] for x in acta["desde_la_ultima"]] == ["hecho", "abierto"]
    assert acta["contexto"] and acta["fuentes"][0]["ref"] == case.PRIOR
    assert "## Desde la última vez" in (res.folder / "acta.md").read_text(encoding="utf-8")
