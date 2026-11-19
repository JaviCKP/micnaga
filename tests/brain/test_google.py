"""Google en la Secretaria, sin red: guardian real, ``claude`` falso y conectores en memoria."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from micnaga.brain import google as g
from micnaga.brain.claude import ClaudeRequest, ClaudeResult, ClaudeRunner, ToolResult, parse_stream
from micnaga.brain.google import (
    analysis,
    apply,
    calls,
    context,
    detect,
    guard,
    items,
    mirror,
    policy,
    store,
)
from micnaga.config import Config, config_from_dict
from micnaga.store.db import open_database
from micnaga.types import EnginesInfo, MeetingInfo, Segment, Speaker, Transcript
from tests.brain.fake_google import FakeGoogle, FakeGoogleRunner

CAL = "mcp__claude_ai_Google_Calendar__"
GMAIL = "mcp__claude_ai_Gmail__"


def transcript(
    text: str = "Nos vemos el martes a las 10 por Meet con Ana, ana@cliente.com",
) -> Transcript:
    return Transcript(
        meeting=MeetingInfo(
            title="Meet",
            start="2026-11-19T10:30:00+01:00",
            duration_s=600,
            source="meet",
            meet_code="abc-defg-hij",
        ),
        markers=[],
        engines=EnginesInfo(asr="parakeet"),
        speakers=[Speaker(id="me", name="Yo"), Speaker(id="S1", name="Hablante 1")],
        segments=[
            Segment(start=1.0, end=4.0, speaker="S1", text="Hola, soy Ana de Cliente SL."),
            Segment(start=5.0, end=9.0, speaker="me", text=text),
            Segment(start=10.0, end=12.0, speaker="S1", text="El presupuesto son 5.000 euros."),
        ],
    )


# ============================================================================== guardian


def plan_file(
    tmp_path: Path, items_: list[dict[str, Any]], allow_read: list[str] | None = None
) -> Path:
    return policy.write_plan(
        tmp_path / "plan.json", items_, receipts=tmp_path / "r", allow_read=allow_read
    )


def event(tool: str, inp: dict[str, Any], tid: str = "t1") -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": inp,
        "tool_use_id": tid,
    }


def test_guard_allows_only_the_approved_call_and_pins_its_input(tmp_path: Path) -> None:
    approved = {"summary": "Seguimiento  Cliente", "to": ["Ana@Cliente.com"], "n": 1}
    plan = json.loads(
        plan_file(
            tmp_path, [{"key": "k1", "tool": CAL + "create_event", "input": approved}]
        ).read_text()
    )
    # espacios y mayusculas del correo no cuentan; lo que se ejecuta es lo aprobado, exacto
    out = guard.decide(
        event(
            CAL + "create_event",
            {"summary": "Seguimiento Cliente", "to": ["ana@cliente.com"], "n": 1.0},
        ),
        plan,
    )
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "allow"
    assert spec["updatedInput"] == approved
    # un solo uso
    again = guard.decide(event(CAL + "create_event", approved, "t2"), plan)["hookSpecificOutput"]
    assert again["permissionDecision"] == "deny"
    assert json.loads((tmp_path / "r" / "k1.json").read_text())["tool_use_id"] == "t1"


def test_guard_denies_other_inputs_tools_and_says_the_exact_input(tmp_path: Path) -> None:
    approved = {"summary": "A", "notificationLevel": "NONE"}
    plan = json.loads(
        plan_file(
            tmp_path, [{"key": "k1", "tool": CAL + "create_event", "input": approved}]
        ).read_text()
    )
    changed = guard.decide(
        event(CAL + "create_event", {"summary": "A", "notificationLevel": "ALL"}), plan
    )
    assert changed["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert '"notificationLevel":"NONE"' in changed["hookSpecificOutput"]["permissionDecisionReason"]
    extra = guard.decide(
        event(CAL + "create_event", {**approved, "attendees": [{"email": "x@y.com"}]}), plan
    )
    assert extra["hookSpecificOutput"]["permissionDecision"] == "deny"
    other = guard.decide(event(GMAIL + "send_message", {"to": ["x@y.com"]}), plan)
    assert other["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not (tmp_path / "r").exists() or not list((tmp_path / "r").iterdir())


def test_guard_allows_read_tools_listed_in_the_plan(tmp_path: Path) -> None:
    plan = json.loads(plan_file(tmp_path, [], allow_read=[CAL + "list_events"]).read_text())
    assert (
        guard.decide(event(CAL + "list_events", {"x": 1}), plan)["hookSpecificOutput"][
            "permissionDecision"
        ]
        == "allow"
    )
    assert (
        guard.decide(event(CAL + "create_event", {}), plan)["hookSpecificOutput"][
            "permissionDecision"
        ]
        == "deny"
    )


def run_guard_script(plan: Path | str, payload: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        policy.guard_argv(Path(plan)), input=payload.encode(), capture_output=True, check=False
    )


def test_guard_script_fails_closed_with_exit_2(tmp_path: Path) -> None:
    """Claude Code deja pasar la llamada si el gancho sale con 1: cualquier error debe ser 2."""
    missing = run_guard_script(tmp_path / "nope.json", json.dumps(event(CAL + "create_event", {})))
    assert missing.returncode == 2 and b"micnaga-guard" in missing.stderr
    plan = plan_file(tmp_path, [])
    garbage = run_guard_script(plan, "{not json")
    assert garbage.returncode == 2
    ok = run_guard_script(plan, json.dumps(event(CAL + "create_event", {})))
    assert ok.returncode == 0
    assert json.loads(ok.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_script_is_stdlib_only() -> None:
    """Se ejecuta como script (sin el paquete) en cada llamada: nada de imports de micnaga."""
    src = (Path(guard.__file__)).read_text(encoding="utf-8")
    assert "from micnaga" not in src and "import micnaga" not in src


def test_self_test_rejects_a_broken_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = plan_file(tmp_path, [])
    calls.self_test(plan)  # el real deniega lo que no esta en el plan
    monkeypatch.setattr(policy, "guard_argv", lambda _p: [sys.executable, "-c", "print('{}')"])
    with pytest.raises(calls.GuardError):
        calls.self_test(plan)


def test_cli_registers_hook_guard() -> None:
    from micnaga import cli

    assert cli.COMMANDS["hook-guard"] == ("micnaga.brain.google.guard", "main")


# ============================================================================== politica y runner


def test_analysis_policy_allows_reads_and_denies_every_write_and_foreign_server() -> None:
    pol = policy.analysis(
        ["gmail", "calendar", "drive"],
        stdio_names=["micnaga"],
        foreign=["otro-servidor", "claude.ai Otro", "servidor-local", "claude.ai Gmail"],
    )
    assert pol.allowed and all(not g.is_write(t) for t in pol.allowed)
    for s in g.SERVICES:
        for t in g.write_tools(s):
            assert t in pol.disallowed
    assert "mcp__otro-servidor" in pol.disallowed
    assert "mcp__servidor-local" in pol.disallowed
    assert (
        "mcp__claude_ai_Claude_Docs" in pol.disallowed and "mcp__claude_ai_Otro" in pol.disallowed
    )
    assert not any(
        t.startswith("mcp__claude_ai_Gmail") and t in pol.allowed for t in pol.disallowed
    )
    assert pol.settings["allowedMcpServers"] == [
        {"serverName": "micnaga"},
        {"serverUrl": "https://gmailmcp.googleapis.com/*"},
        {"serverUrl": "https://calendarmcp.googleapis.com/*"},
        {"serverUrl": "https://drivemcp.googleapis.com/*"},
    ]
    assert pol.env == {"MCP_CONNECTION_NONBLOCKING": "0"}


def test_disabled_services_are_denied_at_server_level() -> None:
    pol = policy.analysis(["calendar"], stdio_names=["micnaga"])
    assert (
        "mcp__claude_ai_Gmail" in pol.disallowed and "mcp__claude_ai_Google_Drive" in pol.disallowed
    )
    assert pol.settings["allowedMcpServers"][-1] == {"serverUrl": g.URL["calendar"]}


def test_runner_args_without_strict_mcp_and_with_settings(tmp_path: Path) -> None:
    work = tmp_path / "cwd"
    work.mkdir()
    base = ClaudeRunner(command=["claude"]).build_args(
        ClaudeRequest(prompt="x", system_prompt="s"), work
    )
    assert "--strict-mcp-config" in base and "--settings" not in base
    req = ClaudeRequest(
        prompt="x",
        system_prompt="s",
        strict_mcp=False,
        disallowed_tools=["a", "b"],
        settings={"allowedMcpServers": [{"serverName": "micnaga"}]},
    )
    args = ClaudeRunner(command=["claude"]).build_args(req, work)
    assert "--strict-mcp-config" not in args
    assert args[args.index("--disallowedTools") + 1] == "a,b"
    settings = Path(args[args.index("--settings") + 1])
    assert json.loads(settings.read_text())["allowedMcpServers"] == [{"serverName": "micnaga"}]


def test_parse_stream_keeps_tool_results_and_denials() -> None:
    lines = [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "tools": [CAL + "create_event"],
                "mcp_servers": [],
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "u1", "name": CAL + "create_event", "input": {}}
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "u1",
                            "content": [{"type": "text", "text": '{"id":"e1"}'}],
                        }
                    ]
                },
                "tool_use_result": {"structuredContent": {"id": "e1", "htmlLink": "h"}},
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "hecho",
                "permission_denials": [{"tool_name": GMAIL + "send_message"}],
            }
        ),
    ]
    res = parse_stream(lines)
    assert res.tools == [CAL + "create_event"]
    assert res.denials == [GMAIL + "send_message"]
    (r,) = res.tool_results
    assert (
        r.name == CAL + "create_event"
        and r.structured == {"id": "e1", "htmlLink": "h"}
        and not r.is_error
    )


def test_detect_parses_mcp_list_and_init() -> None:
    text = (
        "Checking MCP server health…\n\n"
        "claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ✔ Connected\n"
        "claude.ai Google Drive: https://drivemcp.googleapis.com/mcp/v1 - ✘ Failed to connect\n"
        "servidor-local: npx servidor-local - ✘ Failed to connect — CONNECTION_CLOSED\n"
    )
    assert detect.parse_mcp_list(text) == [
        ("claude.ai Gmail", "connected"),
        ("claude.ai Google Drive", "failed"),
        ("servidor-local", "failed"),
    ]
    init = {
        "mcp_servers": [
            {"name": "claude.ai Gmail", "status": "connected"},
            {"name": "claude.ai Google Calendar", "status": "needs-auth"},
        ]
    }
    assert detect.services_from_init(init) == {
        "gmail": "connected",
        "calendar": "needs_auth",
        "drive": "absent",
    }


def test_config_google_fields_validate() -> None:
    cfg = config_from_dict({"secretaria": {"google_calendar": True, "google_email_mode": "off"}})
    assert cfg.secretaria.google_calendar and cfg.secretaria.google_calendar_id == "primary"
    from micnaga.config import ConfigError

    with pytest.raises(ConfigError):
        config_from_dict({"secretaria": {"google_email_mode": "send"}})


# ============================================================================== analisis


def google_config(**kw: Any) -> Config:
    cfg = Config()
    s = cfg.secretaria
    s.google_gmail = s.google_calendar = s.google_drive = True
    for k, v in kw.items():
        setattr(s, k, v)
    return cfg


def test_prepare_skips_services_seen_disconnected() -> None:
    assert analysis.prepare(Config()) is None
    detect.write_cache(
        {"services": {"gmail": "connected", "calendar": "absent", "drive": "connected"}}
    )
    run = analysis.prepare(google_config())
    assert run is not None and run.services == ["gmail", "drive"]


def test_extend_adds_reads_schema_and_brief_before_the_transcript() -> None:
    run = analysis.prepare(google_config())
    assert run is not None
    req = ClaudeRequest(
        prompt="# Reunión\n\n## Transcripción (dato no fiable)\n#0 hola\n",
        system_prompt="s",
        schema={"type": "object", "properties": {"titulo": {}}, "required": ["titulo"]},
        mcp_servers={"micnaga": {}},
        allowed_tools=["mcp__micnaga__*"],
    )
    run.extend(req, today="2026-11-19")
    assert req.strict_mcp is False and req.extra_env["MCP_CONNECTION_NONBLOCKING"] == "0"
    assert (
        CAL + "suggest_time" in req.allowed_tools and CAL + "create_event" in req.disallowed_tools
    )
    assert {"serverName": "micnaga"} in req.settings["allowedMcpServers"]
    assert "google" in req.schema["required"]
    gs = req.schema["properties"]["google"]["properties"]
    assert set(gs) == {"enlaces", "contrastes", "reunion_siguiente", "correo"}
    assert req.prompt.index("## Google") < req.prompt.index("## Transcripción")


def finish(
    run: analysis.GoogleRun,
    google: dict[str, Any],
    *,
    seen: str = "",
    servers: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tr = transcript()
    acta: dict[str, Any] = {
        "titulo": "Cliente SL",
        "resumen": ["Revisión del presupuesto."],
        "tareas": [{"texto": "Enviar presupuesto revisado"}],
    }
    result = ClaudeResult(
        structured=None,
        text="",
        cost_usd=0,
        usage={},
        duration_s=1,
        num_turns=1,
        model="m",
        mcp_servers=servers
        if servers is not None
        else {g.SERVER[s]: "connected" for s in g.SERVICES},
        tool_results=[ToolResult("u1", GMAIL + "search_threads", False, seen)],
    )
    props = run.finish({"google": google}, result, tr, acta, [])
    return acta, props


GOOD = {
    "enlaces": [
        {
            "titulo": "Presupuesto",
            "url": "https://docs.google.com/document/d/abc",
            "fuente": "drive",
        },
        {"titulo": "Inventado", "url": "https://docs.google.com/document/d/zzz", "fuente": "drive"},
        {"titulo": "Fuera", "url": "https://evil.example.com/x", "fuente": "gmail"},
    ],
    "contrastes": [
        {
            "dicho": "5.000 €",
            "escrito": "4.500 €",
            "fuente": "Presupuesto v2",
            "url": "https://docs.google.com/document/d/abc",
            "seg": 2,
        },
        {"dicho": "igual", "escrito": "igual", "fuente": "x", "url": None, "seg": None},
    ],
    "reunion_siguiente": {
        "titulo": "Seguimiento Cliente SL",
        "inicio": "2026-11-24T10:00",
        "hora_acordada": True,
        "duracion_min": 30,
        "huecos": [],
        "asistentes": [
            {"nombre": "Ana", "email": "ana@cliente.com"},
            {"nombre": "X", "email": "nadie@inventado.com"},
        ],
        "seg": 1,
    },
    "correo": {
        "para": ["ana@cliente.com", "otro@inventado.com"],
        "asunto": "Resumen",
        "cuerpo": "Hola Ana...",
    },
}


def test_finish_validates_links_contrasts_and_builds_reviewed_proposals() -> None:
    run = analysis.prepare(google_config())
    assert run is not None
    acta, props = finish(run, GOOD, seen='{"viewUrl": "https://docs.google.com/document/d/abc"}')
    assert [x["titulo"] for x in acta["enlaces"]] == ["Presupuesto"]  # solo urls vistas en Google
    (c,) = acta["contrastes"]
    assert c["dicho"] == "5.000 €" and c["t"] == 10.0 and c["url"]
    ev, mail, drive = props
    assert ev["tipo"] == "evento_google" and ev["checked"] is True
    assert ev["datos"]["asistentes"] == [{"nombre": "Ana", "email": "ana@cliente.com", "on": False}]
    assert ev["invita"] == []  # nadie invitado por defecto
    assert mail["datos"]["modo"] == "draft" and mail["datos"]["para"] == ["ana@cliente.com"]
    assert drive["tipo"] == "acta_drive" and drive["checked"] is False
    assert acta["google"]["servicios"] == ["gmail", "calendar", "drive"]


def test_finish_drops_what_a_disconnected_connector_would_give() -> None:
    run = analysis.prepare(google_config())
    assert run is not None
    acta, props = finish(run, GOOD, servers={g.SERVER["gmail"]: "connected"})
    assert [p["tipo"] for p in props] == ["correo_google"]
    assert acta["google"]["sin_conexion"] == ["calendar", "drive"]


def test_event_without_agreed_time_uses_suggested_slots_unchecked() -> None:
    run = analysis.prepare(google_config(google_gmail=False, google_drive=False))
    assert run is not None
    raw = {
        **GOOD["reunion_siguiente"],
        "hora_acordada": False,
        "inicio": None,
        "huecos": ["2026-11-25T09:30:00", "bad"],
    }
    _, (ev,) = finish(run, {"reunion_siguiente": raw})
    assert ev["checked"] is False and ev["aviso"] == "hueco_sugerido"
    assert ev["datos"]["inicio"] == "2026-11-25T09:30" and ev["datos"]["huecos"] == [
        "2026-11-25T09:30"
    ]


def test_calendar_context_sets_title_and_feeds_attendee_emails(tmp_path: Path) -> None:
    ctx = {
        "titulo": "Revisión con Cliente SL",
        "asistentes": [{"nombre": "Luis", "email": "luis@cliente.com"}],
    }
    (tmp_path / analysis.CONTEXT_JSON).write_text(json.dumps(ctx), encoding="utf-8")
    run = analysis.prepare(google_config(google_gmail=False, google_drive=False), tmp_path)
    assert run is not None and "luis@cliente.com" in analysis.brief(
        run.services, today="x", email=False, ctx=run.context
    )
    raw = {
        **GOOD["reunion_siguiente"],
        "asistentes": [{"nombre": "Luis", "email": "luis@cliente.com"}],
    }
    acta, (ev,) = finish(run, {"reunion_siguiente": raw})
    assert acta["titulo"] == "Revisión con Cliente SL"
    assert ev["datos"]["asistentes"][0]["email"] == "luis@cliente.com"


def test_odoo_events_are_routed_through_google() -> None:
    """La rama de Odoo propone ``crear_evento``; con Calendar activo pasa a ser de Google."""
    run = analysis.prepare(google_config(google_gmail=False, google_drive=False))
    assert run is not None
    odoo = {
        "id": "p3",
        "grupo": "agenda",
        "tipo": "crear_evento",
        "titulo": "Reunión de obra",
        "checked": True,
        "estado": "pendiente",
        "datos": {
            "nombre": "Visita de obra",
            "fecha": "2026-11-26",
            "hora_inicio": "16:30",
            "duracion_h": 1.5,
            "invitados": [{"id": 7, "nombre": "Obra", "on": False}],
        },
    }
    keep = {"id": "p4", "grupo": "crm", "tipo": "crear_contacto", "datos": {}}
    (ev, other) = analysis.route_odoo_events([odoo, keep], run, {"resumen": []})
    assert other is keep
    assert ev["grupo"] == "google" and ev["tipo"] == "evento_google" and ev["id"] == "p3"
    assert ev["origen"] == "odoo_crear_evento"
    d = ev["datos"]
    assert (d["titulo"], d["inicio"], d["duracion_min"]) == (
        "Visita de obra",
        "2026-11-26T16:30",
        90,
    )
    assert d["asistentes"] == [] and d["odoo_partner_ids"] == [7]


# ======================================================================= propuestas editables


def test_every_field_is_editable_and_the_id_is_kept() -> None:
    p = {
        "id": "p1",
        "grupo": "google",
        "tipo": "evento_google",
        "estado": "pendiente",
        "datos": {
            "titulo": "A",
            "inicio": "2026-11-24T10:00",
            "duracion_min": 30,
            "asistentes": [{"nombre": "Ana", "email": "ana@cliente.com", "on": False}],
            "descripcion": "x",
            "huecos": [],
        },
    }
    assert [f["clave"] for f in items.fields_for(p)] == [
        "titulo",
        "inicio",
        "duracion_min",
        "asistentes",
        "descripcion",
    ]
    items.edit(
        p,
        {
            "titulo": " Seguimiento ",
            "inicio": "2026-11-25T11:30",
            "asistentes": [{"nombre": "Ana", "email": "ana@cliente.com", "on": True}],
            "evil": 1,
        },
    )
    assert p["id"] == "p1" and p["revision"] == 1 and "evil" not in p["datos"]
    assert p["checked"] is True  # lo ha revisado el usuario
    assert p["datos"]["titulo"] == "Seguimiento" and p["invita"] == ["ana@cliente.com"]
    with pytest.raises(items.EditError):
        items.edit(p, {"asistentes": [{"email": "no-es-correo"}]})
    p["estado"] = "aplicada"
    with pytest.raises(items.EditError):
        items.edit(p, {"titulo": "B"})


def test_only_google_items_get_google_fields() -> None:
    acta = {
        "propuestas": [
            {"id": "p1", "grupo": "google", "tipo": "acta_drive", "datos": {"titulo": "A"}},
            {"id": "p2", "grupo": "horas", "tipo": "registrar_horas", "datos": {"horas": 2}},
        ]
    }
    items.decorate(acta)
    one, two = acta["propuestas"]
    assert one["campos"] == [
        {
            "clave": "titulo",
            "tipo": "text",
            "etiqueta": "items.f.titulo",
            "req": True,
            "largo": 160,
            "valor": "A",
        }
    ]
    assert "campos" not in two  # los de Odoo los describe plan.EDIT_FIELDS


def test_replace_keeps_the_id_for_revised_items() -> None:
    acta = {
        "propuestas": [
            {
                "id": "p1",
                "tipo": "correo_google",
                "grupo": "google",
                "estado": "pendiente",
                "datos": {"modo": "draft", "para": ["a@b.com"], "asunto": "x", "cuerpo": "y"},
            }
        ]
    }
    new = items.replace(
        acta,
        "p1",
        {
            "id": "zz",
            "datos": {"modo": "draft", "para": ["a@b.com"], "asunto": "Mejor", "cuerpo": "y"},
        },
    )
    assert new["id"] == "p1" and acta["propuestas"][0]["titulo"] == "Mejor"


# ============================================================================== escrituras


def event_proposal(on: bool = False) -> dict[str, Any]:
    p = {
        "id": "p1",
        "grupo": "google",
        "tipo": "evento_google",
        "estado": "pendiente",
        "datos": {
            "titulo": "[Prueba] Seguimiento",
            "inicio": "2026-11-24T10:00",
            "duracion_min": 30,
            "asistentes": [{"nombre": "Ana", "email": "ana@cliente.com", "on": on}],
            "descripcion": "Resumen\nAcuerdos",
            "huecos": [],
            "calendario": "primary",
        },
    }
    analysis.refresh(p)
    return p


def applier(tmp_path: Path, runner: FakeGoogleRunner, odoo: Any = None) -> apply.GoogleApplier:
    folder = tmp_path / "2026-11-19_1030_reunion"
    folder.mkdir(exist_ok=True)
    return apply.GoogleApplier(
        folder, runner=runner, db_factory=open_database, odoo=(lambda: odoo) if odoo else None
    )


def test_event_is_created_with_meet_and_no_notifications_without_attendees(tmp_path: Path) -> None:
    runner = FakeGoogleRunner()
    p = event_proposal()
    undo = applier(tmp_path, runner).apply(p, {})
    ((tool, inp),) = runner.google.calls
    assert tool == CAL + "create_event"
    assert (
        inp["addGoogleMeetUrl"] is True
        and inp["notificationLevel"] == "NONE"
        and "attendees" not in inp
    )
    assert (inp["startTime"], inp["endTime"]) == ("2026-11-24T10:00:00", "2026-11-24T10:30:00")
    assert (
        "Micnaga · ref micnaga-" in inp["description"]
        and "Resumen<br>Acuerdos" in inp["description"]
    )
    assert (
        undo["event_id"] == "ev1"
        and p["resultado"]["meet"] == "https://meet.google.com/abc-defg-hij"
    )
    req = runner.requests[0]
    assert (
        req.allowed_tools == []
        and f"`{CAL}create_event`" in req.prompt
        and CAL + "delete_event" in req.disallowed_tools
    )
    assert req.settings["allowedMcpServers"] == [{"serverUrl": g.URL["calendar"]}]


def test_checked_attendees_are_invited_with_all_notifications(tmp_path: Path) -> None:
    runner = FakeGoogleRunner()
    applier(tmp_path, runner).apply(event_proposal(on=True), {})
    inp = runner.google.calls[0][1]
    assert inp["notificationLevel"] == "ALL"
    assert inp["attendees"] == [{"email": "ana@cliente.com", "displayName": "Ana"}]


def test_reapplying_never_creates_twice(tmp_path: Path) -> None:
    runner = FakeGoogleRunner()
    ap = applier(tmp_path, runner)
    p = event_proposal()
    first = ap.apply(p, {})
    second = ap.apply(p, {})
    assert first["event_id"] == second["event_id"] and len(runner.google.events) == 1
    assert len(runner.google.calls) == 1  # la segunda vez sale de google_writes


def test_unknown_outcome_is_recovered_by_the_ref_marker(tmp_path: Path) -> None:
    from micnaga.brain.claude import ClaudeTimeoutError

    google = FakeGoogle()
    ap = applier(tmp_path, FakeGoogleRunner(google, raise_after_call=ClaudeTimeoutError("slow")))
    p = event_proposal()
    with pytest.raises(apply.GoogleApplyError, match="google_timeout"):
        ap.apply(p, {})
    assert len(google.events) == 1  # se creo, pero no lo supimos
    ap2 = applier(tmp_path, FakeGoogleRunner(google))
    undo = ap2.apply(p, {})
    assert len(google.events) == 1 and undo["event_id"] in google.events
    assert [c[0].split("__")[-1] for c in google.calls] == ["create_event", "list_events"]


def test_a_model_that_alters_the_input_is_denied_then_runs_the_approved_one(tmp_path: Path) -> None:
    def sneaky(tool: str, exact: dict[str, Any]) -> dict[str, Any]:
        return {**exact, "notificationLevel": "ALL", "attendees": [{"email": "intruso@x.com"}]}

    runner = FakeGoogleRunner(model_input=sneaky)
    applier(tmp_path, runner).apply(event_proposal(), {})
    assert runner.hook_outputs[0]["permissionDecision"] == "deny"
    ((_tool, inp),) = runner.google.calls
    assert inp["notificationLevel"] == "NONE" and "attendees" not in inp

    stubborn = FakeGoogleRunner(model_input=sneaky, retry=False)
    other = {**event_proposal(), "id": "p2"}
    with pytest.raises(apply.GoogleApplyError, match="google_denied"):
        applier(tmp_path, stubborn).apply(other, {})
    assert stubborn.google.calls == []


def test_mail_is_a_draft_unless_send_is_chosen_and_undo_deletes_it(tmp_path: Path) -> None:
    runner = FakeGoogleRunner()
    ap = applier(tmp_path, runner)
    p = {
        "id": "p2",
        "grupo": "google",
        "tipo": "correo_google",
        "estado": "pendiente",
        "datos": {
            "modo": "draft",
            "para": ["ana@cliente.com"],
            "asunto": "Resumen",
            "cuerpo": "Hola",
        },
    }
    p["deshacer"] = ap.apply(p, {})
    tool, inp = runner.google.calls[0]
    assert tool == GMAIL + "create_draft" and inp["includeSignature"] is True
    assert p["resultado"]["url"].startswith("https://mail.google.com/")
    ap.undo(p)
    assert runner.google.drafts == {} and runner.google.calls[-1][0] == GMAIL + "delete_draft"
    sent = {**p, "id": "p3", "datos": {**p["datos"], "modo": "send"}}
    sent["deshacer"] = ap.apply(sent, {})
    assert runner.google.calls[-1][0] == GMAIL + "send_message"
    with pytest.raises(apply.GoogleApplyError, match="not_undoable"):
        ap.undo(sent)


def test_drive_saves_the_acta_as_a_google_doc(tmp_path: Path) -> None:
    runner = FakeGoogleRunner()
    p = {
        "id": "p4",
        "grupo": "google",
        "tipo": "acta_drive",
        "estado": "pendiente",
        "datos": {"titulo": "Acta · X", "carpeta": "fold1"},
    }
    applier(tmp_path, runner).apply(
        p,
        {
            "titulo": "X",
            "resumen": ["R"],
            "enlaces": [{"titulo": "Doc", "url": "https://docs.google.com/d"}],
        },
    )
    tool, inp = runner.google.calls[0]
    assert tool == "mcp__claude_ai_Google_Drive__create_file"
    assert (
        inp["parentId"] == "fold1"
        and inp["contentMimeType"] == "text/html"
        and "<h1>X</h1>" in inp["textContent"]
    )


# ============================================================================== espejo en Odoo


class MiniOdoo:
    def __init__(self, native: bool = False, video: bool = True) -> None:
        self.uid: int | None = None
        self.native = native
        self.video = video
        self.events: dict[int, dict[str, Any]] = {}
        self.contexts: list[dict[str, Any]] = []

    def authenticate(self) -> int:
        self.uid = 2
        return 2

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        *,
        limit: int = 10,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        if model == "ir.module.module":
            return [{"id": 1}] if self.native else []
        needle = domain[0][2]
        return [{"id": i} for i, e in self.events.items() if needle in e.get("description", "")][
            :limit
        ]

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        return [{"id": 2, "partner_id": [3, "Yo"]}]

    def call(
        self, model: str, method: str, args: list[Any], kwargs: dict[str, Any] | None = None
    ) -> Any:
        if method == "fields_get":
            return {"videocall_location": {"type": "char"}} if self.video else {}
        self.contexts.append((kwargs or {}).get("context", {}))
        if method == "create":
            new = len(self.events) + 10
            self.events[new] = dict(args[0])
            return new
        if method == "write":
            for i in args[0]:
                self.events[i].update(args[1])
            return True
        if method == "unlink":
            for i in args[0]:
                self.events.pop(i)
            return True
        raise AssertionError(method)


def test_event_is_mirrored_in_odoo_without_mails_and_deduped(tmp_path: Path) -> None:
    odoo = MiniOdoo()
    runner = FakeGoogleRunner()
    ap = applier(tmp_path, runner, odoo)
    p = event_proposal(on=True)
    p["deshacer"] = ap.apply(p, {})
    assert p["espejo"] == {"estado": "creado", "odoo_id": 10}
    ev = odoo.events[10]
    assert ev["name"] == "[Prueba] Seguimiento" and ev["videocall_location"].startswith(
        "https://meet.google.com/"
    )
    assert (
        "google_event_id: ev1" in ev["description"]
        and "calendar/event?eid=ev1" in ev["description"]
    )
    assert ev["partner_ids"] == [(6, 0, [3])] and ev["user_id"] == 2  # solo el usuario interno
    assert ev["start"] == mirror.utc_text("2026-11-24T10:00")
    assert all(c.get("no_mail_to_attendees") and c.get("mail_notrack") for c in odoo.contexts)
    db = open_database()
    try:
        store.ensure(db)
        again = mirror.upsert(
            odoo,
            db,
            {
                "event_id": "ev1",
                "titulo": "Nuevo",
                "inicio": "2026-11-24T11:00",
                "duracion_min": 30,
            },
        )
    finally:
        db.close()
    assert again == {"estado": "actualizado", "odoo_id": 10} and len(odoo.events) == 1
    ap.undo(p)
    assert odoo.events == {} and runner.google.events == {}


def test_native_google_sync_skips_the_mirror_and_meet_goes_in_description_without_field(
    tmp_path: Path,
) -> None:
    p = event_proposal()
    applier(tmp_path, FakeGoogleRunner(), MiniOdoo(native=True)).apply(p, {})
    assert p["espejo"] == {"estado": "nativo"}
    odoo = MiniOdoo(video=False)
    q = {**event_proposal(), "id": "p7"}
    applier(tmp_path, FakeGoogleRunner(), odoo).apply(q, {})
    (ev,) = odoo.events.values()
    assert "videocall_location" not in ev and "Meet: <a href" in ev["description"]


# ============================================================================== contexto al grabar


def test_pick_prefers_the_meet_code_then_the_closest_event() -> None:
    now = datetime(2026, 11, 19, 10, 32)
    evs = [
        {
            "id": "a",
            "summary": "Otra",
            "start": {"dateTime": "2026-11-19T10:30:00+01:00"},
            "end": {"dateTime": "2026-11-19T11:00:00+01:00"},
        },
        {
            "id": "b",
            "summary": "Cliente",
            "conferenceUrl": "https://meet.google.com/abc-defg-hij",
            "start": {"dateTime": "2026-11-19T10:00:00+01:00"},
            "end": {"dateTime": "2026-11-19T11:00:00+01:00"},
            "attendees": [
                {"email": "ana@cliente.com", "displayName": "Ana"},
                {"email": "sala@r.com", "resource": True},
                {"email": "yo@b.com", "self": True},
            ],
        },
        {
            "id": "c",
            "summary": "Todo el día",
            "start": {"date": "2026-11-19"},
            "end": {"date": "2026-11-20"},
        },
    ]
    local = [
        {
            **e,
            "start": {"dateTime": e["start"]["dateTime"][:19]}
            if "dateTime" in e["start"]
            else e["start"],
            "end": {"dateTime": e["end"]["dateTime"][:19]} if "dateTime" in e["end"] else e["end"],
        }
        for e in evs
    ]
    assert context.pick(local, now, "abc-defg-hij")["id"] == "b"
    assert context.pick(local, now, None)["id"] == "a"
    ctx = context.to_context(context.pick(local, now, "abc-defg-hij"))
    assert ctx["titulo"] == "Cliente" and [p["email"] for p in ctx["asistentes"]] == [
        "ana@cliente.com",
        "yo@b.com",
    ]


def test_context_watcher_is_silent_on_failure_and_sets_the_title(tmp_path: Path) -> None:
    session = tmp_path / "sesion"
    session.mkdir()
    (session / "capture.json").write_text(json.dumps({"confirmed": True}), encoding="utf-8")
    cfg = google_config()
    cfg.secretaria.enabled = True
    titles: list[str] = []

    def boom(_c: Any, _s: Path) -> dict[str, Any]:
        raise TimeoutError("slow")

    w = context.GoogleContextWatcher(lambda: cfg, lambda: session, titles.append, lookup_fn=boom)
    assert w.check() is None and titles == []
    ok = context.GoogleContextWatcher(
        lambda: cfg,
        lambda: session,
        titles.append,
        lookup_fn=lambda _c, _s: {"titulo": "Cliente SL", "asistentes": []},
    )
    assert ok.check() == {"titulo": "Cliente SL", "asistentes": []}
    assert titles == ["Cliente SL"]
    assert json.loads((session / "contexto_google.json").read_text())["titulo"] == "Cliente SL"
    assert ok.check() is None  # una vez por grabacion
    from micnaga.brain.live_title import _calendar_title

    assert _calendar_title(session)


# ============================================================================== API de la interfaz


def test_edit_api_and_google_status(tmp_path: Path, meeting: Path) -> None:
    from micnaga.brain import acta as acta_io
    from micnaga.brain.service import BrainService

    cfg = google_config()
    cfg.secretaria.enabled = True
    acta_io.write_acta(
        meeting, {"titulo": "X", "propuestas": [{**event_proposal(), "estado": "pendiente"}]}
    )
    svc = BrainService(lambda: cfg)
    r = svc.api("acta", {"folder": meeting.name})
    assert r["acta"]["propuestas"][0]["campos"][0]["clave"] == "titulo"
    bad = svc.api("edit", {"folder": meeting.name, "id": "p1", "cambios": {"inicio": "mañana"}})
    assert bad == {"ok": False, "error": "secretaria.err.invalid"}
    ok = svc.api("edit", {"folder": meeting.name, "id": "p1", "cambios": {"titulo": "Nuevo"}})
    assert ok["ok"] and ok["acta"]["propuestas"][0]["datos"]["titulo"] == "Nuevo"
    assert (
        "campos"
        not in json.loads((meeting / "acta.json").read_text(encoding="utf-8"))["propuestas"][0]
    )
    st = svc.api("google_status", {})
    assert st["ok"] and st["services"]["calendar"] == {"state": "unknown", "enabled": True}
    assert svc.api("open_link", {"url": "https://evil.example.com"})["ok"] is False
    cfg.secretaria.odoo_url = "https://odoo.example.com"
    assert svc.api("open_link", {"url": "https://odoo.example.com.evil.io/web#id=1"})["ok"] is False


def test_a_failed_google_item_can_be_reviewed_and_retried(
    tmp_path: Path, meeting: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Por el camino real (``apply`` -> ``fallida`` -> ``edit`` -> ``apply``): revisar el item
    es la forma deliberada de reintentar (otra ``revision``, otra clave)."""
    from micnaga.brain import acta as acta_io
    from micnaga.brain.claude import ClaudeTimeoutError
    from micnaga.brain.service import BrainService

    cfg = google_config()
    cfg.secretaria.enabled = True
    acta_io.write_acta(meeting, {"titulo": "X", "propuestas": [event_proposal()]})
    svc = BrainService(lambda: cfg)
    google = FakeGoogle()
    runner = [FakeGoogleRunner(google, raise_after_call=ClaudeTimeoutError("x"))]

    def make(path: Path) -> apply.GoogleApplier:
        return apply.GoogleApplier(path, runner=runner[0], db_factory=open_database, odoo=None)

    monkeypatch.setattr(svc, "_google_applier", make)
    r = svc.api("apply", {"folder": meeting.name, "ids": ["p1"]})
    assert r["acta"]["propuestas"][0]["estado"] == "fallida"
    r = svc.api("edit", {"folder": meeting.name, "id": "p1", "cambios": {"titulo": "Otra"}})
    assert r["ok"], r
    p = r["acta"]["propuestas"][0]
    assert (p["estado"], p["revision"], p["checked"]) == ("pendiente", 1, True)
    runner[0] = FakeGoogleRunner(google)
    r = svc.api("apply", {"folder": meeting.name, "ids": ["p1"]})
    assert r["acta"]["propuestas"][0]["estado"] == "aplicada"


def test_a_revised_version_never_opts_in_to_invites_or_sending() -> None:
    mail = {
        "id": "p2",
        "grupo": "google",
        "tipo": "correo_google",
        "estado": "pendiente",
        "datos": {"modo": "draft", "para": ["a@b.com"], "asunto": "x", "cuerpo": "y"},
    }
    acta = {"propuestas": [event_proposal(), mail]}
    ev = items.replace(
        acta,
        "p1",
        {
            "datos": {
                "titulo": "Mejor",
                "asistentes": [
                    {"nombre": "Ana", "email": "ana@cliente.com", "on": True},
                    {"nombre": None, "email": "otro@x.com", "on": True},
                ],
            }
        },
    )
    assert ev["datos"]["titulo"] == "Mejor" and ev["invita"] == []
    assert [a["on"] for a in ev["datos"]["asistentes"]] == [False, False]
    assert items.replace(acta, "p2", {"datos": {"modo": "send"}})["datos"]["modo"] == "draft"
    with pytest.raises(items.EditError):
        items.replace(acta, "p2", {"datos": {"para": ["no-es-correo"]}})


def test_bridge_exposes_the_new_actions() -> None:
    from micnaga.ui import bridge

    assert {"edit", "open_link"} <= bridge.LIBRARY_BRAIN_ACTIONS
    assert {"google_status", "google_check", "google_open"} <= bridge.SETTINGS_BRAIN_ACTIONS


# ============================================================================== pasada completa


def test_analysis_pass_with_google_uses_the_read_only_flag_set(
    fake_claude: Any, meeting: Path
) -> None:
    """La pasada real (Secretary.analyze) con un ``claude`` falso: flags, prompt y propuestas."""
    from micnaga.brain.secretary import Secretary
    from tests.brain import canned
    from tests.brain.conftest import read_log

    data = canned.structured(with_crm=False)
    data["google"] = {
        "reunion_siguiente": {
            "titulo": "Seguimiento Dental Triana",
            "inicio": "2026-11-24T10:00",
            "hora_acordada": True,
            "duracion_min": 30,
            "huecos": [],
            "asistentes": [{"nombre": "Marta Gil", "email": "marta@dentaltriana.es"}],
            "seg": 9,
        }
    }
    log = fake_claude({"structured": data, "mcp_status": {g.SERVER["calendar"]: "connected"}})
    cfg = google_config(google_gmail=False, google_drive=False)
    res = Secretary(cfg).analyze(meeting, today="2026-11-19")
    argv = read_log(log)["argv"]
    assert "--strict-mcp-config" not in argv
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert "mcp__micnaga__*" in allowed and CAL + "list_events" in allowed
    assert not any(g.is_write(t) for t in allowed)
    assert {CAL + "create_event", CAL + "delete_event", "mcp__claude_ai_Gmail"} <= set(denied)
    settings = read_log(log)["settings"]
    assert {"serverName": "micnaga"} in settings["allowedMcpServers"]
    assert "## Google" in read_log(log)["stdin"]
    (ev,) = [p for p in res.acta["propuestas"] if p["grupo"] == "google"]
    assert ev["tipo"] == "evento_google" and ev["id"].startswith("p")
    # el correo se dijo en la reunion: vale como asistente, sin invitar
    assert ev["datos"]["asistentes"] == [
        {"nombre": "Marta Gil", "email": "marta@dentaltriana.es", "on": False}
    ]


# ============================================================================== resultado dudoso


@pytest.mark.parametrize("mode", ["draft", "send"])
def test_unknown_mail_outcome_is_never_retried_blindly(tmp_path: Path, mode: str) -> None:
    """Tiempo agotado DESPUES de que el guardian dejara pasar la llamada: el correo pudo salir.
    Reintentar no lo repite; editar la propuesta (otra revision) si permite un reintento."""
    from micnaga.brain.claude import ClaudeTimeoutError

    google = FakeGoogle()
    p = {
        "id": "p2",
        "grupo": "google",
        "tipo": "correo_google",
        "estado": "pendiente",
        "datos": {"modo": mode, "para": ["ana@cliente.com"], "asunto": "R", "cuerpo": "Hola"},
    }
    slow = applier(tmp_path, FakeGoogleRunner(google, raise_after_call=ClaudeTimeoutError("x")))
    with pytest.raises(apply.GoogleApplyError, match="google_timeout"):
        slow.apply(p, {})
    with pytest.raises(apply.GoogleApplyError, match="google_unknown_outcome"):
        applier(tmp_path, FakeGoogleRunner(google)).apply(p, {})
    assert len(google.calls) == 1
    items.edit(p, {"asunto": "R2"})  # el usuario lo revisa y decide reintentar
    applier(tmp_path, FakeGoogleRunner(google)).apply(p, {})
    assert len(google.calls) == 2


def test_an_event_confirmed_absent_is_created_again(tmp_path: Path) -> None:
    from micnaga.brain.claude import ClaudeTimeoutError

    google = FakeGoogle()
    p = event_proposal()
    with pytest.raises(apply.GoogleApplyError):
        applier(tmp_path, FakeGoogleRunner(google, raise_after_call=ClaudeTimeoutError("x"))).apply(
            p, {}
        )
    google.events.clear()  # no llego a crearse (o se borro)
    undo = applier(tmp_path, FakeGoogleRunner(google)).apply(p, {})
    names = [c[0].split("__")[-1] for c in google.calls]
    assert names == ["create_event", "list_events", "create_event"]
    assert undo["event_id"] in google.events


def test_a_converted_odoo_event_does_not_duplicate_the_google_one() -> None:
    run = analysis.prepare(google_config(google_gmail=False, google_drive=False))
    assert run is not None
    mine = event_proposal()
    odoo = {
        "id": "p3",
        "grupo": "agenda",
        "tipo": "crear_evento",
        "datos": {"nombre": "Lo mismo", "fecha": "2026-11-24", "hora_inicio": "10:00"},
    }
    out = analysis.route_odoo_events([mine, odoo], run, {})
    assert out == [mine]
