"""Investigar primero: reuniones anteriores, «desde la ultima vez» y fuentes comprobadas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from micnaga import paths
from micnaga.brain import acta as acta_io
from micnaga.brain import research
from micnaga.brain.claude import ClaudeOutputError, ClaudeRequest, ClaudeResult, ToolCall
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


def test_people_come_from_named_speakers_and_the_calendar_event_but_not_me(
    tmp_path: Path,
) -> None:
    from micnaga.brain.google import context

    tr = case.transcript(case.CURRENT_LINES, "2026-11-19T10:30:00+01:00", s1="Hablante 1")
    ev = {
        "summary": "Dental Triana",
        "attendees": [
            {"email": "yo@example.com", "displayName": "Javier", "self": True},
            {"email": "carlos@dentaltriana.example", "displayName": "Carlos Ruiz"},
            {"email": "sala@resource.calendar.google.com", "resource": True},
        ],
    }
    (tmp_path / "contexto_google.json").write_text(
        json.dumps(context.to_context(ev)), encoding="utf-8"
    )
    assert research.people_for(tr, tmp_path) == ["Carlos Ruiz"]


def test_a_namesake_from_another_client_is_not_a_candidate() -> None:
    case.write_library()
    with open_database() as db:
        root = paths.meetings_root()
        assert research.related(db, root, ["Carlos Pérez"], exclude=case.CURRENT) == []
        assert [m["carpeta"] for m in research.related(db, root, ["Carlos"], exclude=None)] == [
            case.PRIOR
        ]


def test_a_placeholder_acta_is_run_again_then_rejected(
    fake_claude: FakeClaude, secrets_file: Path
) -> None:
    """Visto una vez con el modelo real: «titulo: x» y todo vacio. Se repite y, si sigue, falla
    (la cola lo reintenta luego) en vez de guardar un acta vacia."""
    _, current = case.write_library()
    empty: dict[str, Any] = {
        "titulo": "x",
        "resumen": [],
        "decisiones": [],
        "tareas": [],
        "preguntas_abiertas": [],
        "proximos_pasos": [],
        "correcciones": [],
        "aprendizajes": [],
        "contexto": None,
        "desde_la_ultima": [],
        "fuentes": [],
    }
    fake_claude({"structured": empty})
    with pytest.raises(ClaudeOutputError):
        Secretary(Config()).analyze(current, today="2026-11-19")
    assert not (current / "acta.json").exists()


# ============================================================================== investigar siempre


class Scripted:
    """Un runner falso en proceso: devuelve los resultados en orden y guarda los pedidos."""

    def __init__(self, results: list[ClaudeResult]) -> None:
        self.results = results
        self.requests: list[ClaudeRequest] = []

    def run(self, req: ClaudeRequest, on_event: Any = None) -> ClaudeResult:
        self.requests.append(req)
        return self.results.pop(0)

    run_once = run


def _acta(**extra: Any) -> dict[str, Any]:
    return {
        "titulo": "Dental Triana · acceso al panel",
        "resumen": ["El presupuesto está aprobado; falta el acceso de recepción al panel."],
        "decisiones": [],
        "tareas": [],
        "preguntas_abiertas": [],
        "proximos_pasos": [],
        "correcciones": [
            {"tipo": "hablante", "hablante": "S1", "a": "Carlos Ruiz", "de": None, "seg": 0},
            {"tipo": "palabra", "de": "panel", "a": "Panel", "seg": 3},
        ],
        "aprendizajes": [],
        "contexto": None,
        "desde_la_ultima": [],
        "fuentes": [],
        **extra,
    }


def _result(data: dict[str, Any], calls: list[tuple[str, dict[str, Any]]]) -> ClaudeResult:
    return ClaudeResult(
        structured=data,
        text="",
        cost_usd=0.02,
        usage={},
        duration_s=2.0,
        num_turns=2 + len(calls),
        model="claude-fake",
        mcp_servers={"micnaga": "connected"},
        tool_calls=[ToolCall(f"mcp__micnaga__{t}", a) for t, a in calls],
    )


def test_research_always_happens_without_named_speakers_or_calendar() -> None:
    """La reunion real: el otro hablante sigue «Hablante 1», sin evento de Calendar; el modelo
    contesto en 2 turnos sin una sola herramienta. Ahora: candidatos sacados de la propia
    conversacion, las llamadas exactas del primer turno en el pedido, un recordatorio al final
    y, si aun asi no investiga, una segunda pasada con «Falta investigar»."""
    _, current = case.write_library(named=False)
    assert research.people_for(output_transcript(current), current) == []  # nadie con nombre
    assert not (current / "contexto_google.json").exists()
    runner = Scripted(
        [
            _result(_acta(), []),  # contesta sin investigar
            _result(_acta(), [("reuniones_con", {"texto": "Carlos"})]),
        ]
    )
    sec = Secretary(Config(), runner=runner, db_factory=open_database)  # type: ignore[arg-type]
    out = sec.analyze(current, today="2026-11-19")
    first, second = (r.prompt for r in runner.requests)
    assert '`reuniones_con("Carlos")`' in first and "`buscar_en_reuniones(" in first
    assert f"`{case.PRIOR}`" in first  # la anterior, encontrada por el nombre dicho
    assert "llama «Carlos» a alguien (solo puede ser `S1`)" in first  # pista para nombrarlo
    assert first.rstrip().endswith(
        "escribe el acta con lo que encuentres."
    )  # tras la transcripcion
    assert "## Falta investigar" in second and "reuniones anteriores" in second
    assert out.acta["tool_calls"] == 1
    kinds = [(p["tipo"], p["datos"].get("a")) for p in out.acta["propuestas"]]
    assert ("hablante", "Carlos Ruiz") in kinds


@pytest.mark.parametrize(
    "name",
    [
        "Pedro Gómez",  # no se dice, ni sale en ninguna herramienta
        "Carlos Pérez",  # «Carlos» se dice, pero el apellido es inventado
    ],
)
def test_a_speaker_name_nobody_said_is_dropped(name: str) -> None:
    _, current = case.write_library(named=False)
    data = _acta()
    data["correcciones"][0]["a"] = name
    runner = Scripted([_result(data, [("reuniones_con", {"texto": "Carlos"})])])
    out = Secretary(Config(), runner=runner, db_factory=open_database).analyze(  # type: ignore[arg-type]
        current, today="2026-11-19"
    )
    assert len(runner.requests) == 1  # investigo: no se repite
    assert all(p["tipo"] != "hablante" for p in out.acta["propuestas"])


def _calls(*names: str, servers: dict[str, str] | None = None) -> ClaudeResult:
    res = _result(_acta(), [])
    res.tool_calls = [ToolCall(n, {}) for n in names]
    res.mcp_servers = servers if servers is not None else {"micnaga": "connected"}
    return res


def test_what_counts_as_research_and_what_cannot_be_demanded() -> None:
    gmail = "claude.ai Gmail"
    up = {"micnaga": "connected", gmail: "connected", "correo": "connected"}
    local = "mcp__micnaga__reuniones_con"
    assert research.missing(_calls(servers=up), odoo=True, gmail=True) == [
        "actas",
        "odoo",
        "gmail",
    ]
    done = _calls(local, "mcp__micnaga__buscar_empresas", "mcp__claude_ai_Gmail__search_threads")
    done.mcp_servers = up
    assert research.missing(done, odoo=True, gmail=True) == []
    # el correo de un servidor extra (secretaria.mcp_servers) vale como busqueda de correo
    extra = _calls(local, "mcp__correo__search_threads", servers=up)
    assert research.missing(extra, odoo=False, gmail=False, mail_servers=["correo"]) == []
    assert research.missing(_calls(local, servers=up), odoo=False, gmail=False) == []
    # lo que no esta conectado no se exige: repetir no lo arreglaria
    down = {"micnaga": "failed", gmail: "pending"}
    assert research.missing(_calls(servers=down), odoo=True, gmail=True) == []
    stub = _calls(local, servers={"micnaga": "connected", "correo": "failed"})
    assert research.missing(stub, odoo=False, gmail=False, mail_servers=["correo"]) == []


def test_terms_come_from_the_conversation() -> None:
    tr = case.transcript(case.CURRENT_LINES, "2026-11-19T10:30:00+01:00", s1="Hablante 1")
    terms = research.extract(tr)
    assert terms["personas"] == ["Carlos"]
    assert "WhatsApp" in terms["entidades"]
    calls = research.first_calls(terms, odoo=True, gmail=True)
    assert '`buscar_empresas("WhatsApp")`' in calls and '`buscar_contactos("Carlos")`' in calls
    assert any("newer_than:90d" in c for c in calls)


def output_transcript(folder: Path) -> Any:
    from micnaga.pipeline import output

    return output.read_transcript(folder)
