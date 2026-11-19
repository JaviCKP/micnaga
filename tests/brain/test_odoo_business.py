"""Odoo de negocio contra el Odoo FALSO: capacidades, herramientas, horas, acciones y deshacer."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from micnaga.brain import capabilities, horas, learn, mcp, plan, timesheets
from micnaga.brain.executor import ApplyError, Executor
from micnaga.brain.odoo import OdooClient, OdooConfig, OdooError
from micnaga.brain.schema import PROPOSAL_TYPES
from micnaga.brain.secretary import Secretary
from micnaga.config import Config
from tests.brain import canned
from tests.brain.conftest import FakeClaude, odoo_config, read_log
from tests.brain.demo import business_transcript
from tests.brain.fake_odoo import (
    DB,
    KEY,
    LOGIN,
    PARTNER_ME,
    PROJECT_ATLAS,
    PROJECT_CLOSED,
    PROJECT_TRAINING,
    FakeOdoo,
)

TODAY = date(2026, 11, 19)


def test_calendar_piece_never_mails_attendees(fake_odoo: FakeOdoo) -> None:
    """La pieza aislada que tambien usa la capa de Google Calendar."""
    from micnaga.brain import odoo_calendar
    from micnaga.brain.odoo import local_tz

    c = OdooClient(OdooConfig(fake_odoo.url, DB, LOGIN, KEY), timeout_s=5)
    tz = local_tz("Europe/Madrid")
    vals = odoo_calendar.event_values(
        name="Demo", day="2026-11-24", tz=tz, start="10:00", duration_h=1.5, partner_ids=[3]
    )
    assert (vals["start"], vals["stop"]) == ("2026-11-24 09:00:00", "2026-11-24 10:30:00")
    eid = odoo_calendar.create_event(c, vals)
    assert odoo_calendar.update_event(c, eid, {"name": "Demo 2"})
    assert fake_odoo.data["calendar.event"][-1]["name"] == "Demo 2"
    assert fake_odoo.contexts == [
        ("calendar.event", {"no_mail_to_attendees": True}),
        ("calendar.event", {"no_mail_to_attendees": True}),
    ]
    allday = odoo_calendar.event_values(name="Feria", day="2026-12-01", tz=tz)
    assert allday["allday"] is True and allday["partner_ids"] == [(6, 0, [])]


MISSING = [
    "2026-11-05",
    "2026-11-06",
    "2026-11-09",
    "2026-11-10",
    "2026-11-11",
    "2026-11-12",
    "2026-11-13",
    "2026-11-16",
    "2026-11-17",
    "2026-11-18",
    "2026-11-19",
]


def client(srv: FakeOdoo, *, read_only: bool = False) -> OdooClient:
    c = OdooClient(OdooConfig(srv.url, DB, LOGIN, KEY), timeout_s=5, read_only=read_only)
    c.authenticate()
    return c


def employee_access(srv: FakeOdoo) -> None:
    """Lo mismo que la cuenta real de empleado: sin CRM, contactos y proyectos de solo lectura."""
    srv.access.update(
        {
            "crm.lead": set(),
            "res.partner": {"read"},
            "project.project": {"read"},
            "crm.stage": {"read"},
        }
    )


# ============================================================================== capacidades


def test_capabilities_follow_access_rights_not_modules(fake_odoo: FakeOdoo) -> None:
    employee_access(fake_odoo)
    prof = capabilities.discover(client(fake_odoo))
    assert "crm" in prof["modules"]  # instalado...
    assert prof["access"]["crm.lead"]["read"] is False  # ...pero sin acceso: no cuenta
    assert capabilities.summary_text(prof) == (
        "Partes de horas · Tareas · Calendario · Actividades · Gastos · Contactos (lectura)"
    )
    assert capabilities.note_targets(prof) == ["project.task"]
    tools = capabilities.tools_for(prof, mcp.EVERY_ODOO_TOOL)
    assert "buscar_oportunidades" not in tools and "leer_oportunidad" not in tools
    assert {"buscar_contactos", "mis_partes_de_horas", "proyectos", "tareas"} <= set(tools)
    actions = capabilities.actions_for(prof, PROPOSAL_TYPES)
    assert "crear_contacto" not in actions and "actualizar_oportunidad" not in actions
    assert {"registrar_horas", "crear_tarea", "crear_evento", "nota_en_historial"} <= set(actions)
    assert fake_odoo.write_calls == []  # descubrir nunca escribe


def test_profile_is_stored_per_connection_and_refreshed_daily(fake_odoo: FakeOdoo) -> None:
    c = client(fake_odoo)
    now = datetime(2026, 11, 19, 10, 0)
    prof = capabilities.ensure(c, now=now)
    key = capabilities.connection_key(fake_odoo.url, DB, LOGIN)
    assert capabilities.load(key) == prof
    assert capabilities.load(capabilities.connection_key(fake_odoo.url, DB, "jefe@x")) is None
    n = len(fake_odoo.calls)
    capabilities.ensure(c, now=datetime(2026, 11, 19, 18, 0))  # mismo dia: sin red
    assert len(fake_odoo.calls) == n
    capabilities.ensure(c, now=datetime(2026, 11, 20, 11, 0))  # al dia siguiente se rehace
    assert len(fake_odoo.calls) > n


def test_read_only_client_refuses_writes_before_the_network(fake_odoo: FakeOdoo) -> None:
    c = client(fake_odoo, read_only=True)
    with pytest.raises(OdooError) as exc:
        c.create("account.analytic.line", {"name": "x"})
    assert exc.value.kind == "readonly"
    assert fake_odoo.write_calls == []


def test_secretary_offers_only_what_the_user_can_do(
    fake_claude: FakeClaude, fake_odoo: FakeOdoo, meeting: Path
) -> None:
    employee_access(fake_odoo)
    log = fake_claude({"structured": canned.structured()})
    res = Secretary(odoo_config(Config(), fake_odoo)).analyze(meeting, today="2026-11-19")
    data = read_log(log)
    assert "buscar_oportunidades" not in data["mcp_tools"]
    assert "mis_partes_de_horas" in data["mcp_tools"]
    schema = json.loads(data["argv"][data["argv"].index("--json-schema") + 1])
    enum = schema["properties"]["propuestas"]["items"]["properties"]["tipo"]["enum"]
    assert "crear_contacto" not in enum and "registrar_horas" in enum
    assert "Contactos (lectura)" in data["stdin"] and "Hoy es 2026-11-19" in data["stdin"]
    # el plan del modelo traia CRM: nada de eso sobrevive (ni la nota en el contacto)
    odoo_props = [p for p in res.acta["propuestas"] if p["grupo"] != "correcciones"]
    assert odoo_props == []
    assert res.acta["odoo"]["capacidades"].startswith("Partes de horas")
    assert fake_odoo.write_calls == []


# ============================================================================== herramientas


def test_business_read_tools(fake_odoo: FakeOdoo) -> None:
    fake_odoo.data["calendar.event"].append(
        {
            "id": 700,
            "name": "Revisión con el cliente",
            "start": "2026-11-10 09:00:00",  # UTC -> 10:00 en Madrid
            "stop": "2026-11-10 10:00:00",
            "allday": False,
            "duration": 1.0,
            "partner_ids": [PARTNER_ME, 57],
            "location": False,
        }
    )
    fake_odoo.data["hr.expense"].append(
        {"id": 1, "name": "Taxi", "date": "2026-11-03", "total_amount": 18.5, "state": "draft"}
    )
    tools = mcp.Tools(client(fake_odoo), allowed=list(mcp.EVERY_ODOO_TOOL), today=TODAY)
    out = tools.call("mis_partes_de_horas", {"desde": "2026-10-20", "hasta": "2026-11-19"})
    assert [d["fecha"] for d in out["faltan"]] == MISSING
    assert out["posibles_libres"] == ["2026-11-02"]
    assert out["ultimo_dia_con_horas"] == "2026-11-04" and out["jornada_tipica"] == 8.0
    assert [(s["proyecto_id"], s["horas"]) for s in out["patron"]] == [
        (PROJECT_TRAINING, 4.5),
        (PROJECT_TRAINING, 0.5),
        (PROJECT_TRAINING, 2.0),
        (PROJECT_ATLAS, 1.0),
    ]
    assert out["horas_por_dia"]["2026-11-04"] == 8.0
    names = [p["nombre"] for p in tools.call("proyectos", {})]
    assert "Proyecto archivado" not in names and "Formación Ayuntamiento" in names
    assert [t["id"] for t in tools.call("tareas", {})] == [300]  # mias y abiertas
    ev = tools.call("calendario", {"desde": "2026-11-09", "hasta": "2026-11-13"})
    assert ev[0]["fecha"] == "2026-11-10" and ev[0]["inicio"] == "10:00"
    assert tools.call("mis_gastos", {})[0]["concepto"] == "Taxi"
    assert tools.call("mis_actividades", {})[0]["resumen"] == "Llamar a Carlos"
    assert fake_odoo.write_calls == []


def test_tools_are_filtered_by_the_allow_list(fake_odoo: FakeOdoo) -> None:
    server = mcp.Server(mcp.Tools(client(fake_odoo), allowed=["proyectos", "tareas"]))
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listed is not None
    assert [t["name"] for t in listed["result"]["tools"]] == [
        "proyectos",
        "tareas",
        "buscar_en_reuniones",
    ]
    bad = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "buscar_oportunidades", "arguments": {"texto": "x"}},
        }
    )
    assert bad is not None and bad["result"]["isError"] is True


# ============================================================================== dias y patron


def test_working_days_skip_weekends_and_national_holidays() -> None:
    assert timesheets.easter(2026) == date(2026, 4, 5)
    assert timesheets.holiday_name(date(2026, 4, 3)) == "Viernes Santo"
    assert timesheets.holiday_name(date(2026, 10, 12)) == "Fiesta Nacional"
    assert timesheets.holiday_name(date(2026, 12, 8)) == "Inmaculada"
    assert not timesheets.is_working_day(date(2026, 11, 21))  # sabado
    assert not timesheets.is_working_day(date(2026, 12, 25))
    assert not timesheets.is_working_day(date(2026, 11, 20), ["2026-11-20"])  # marcado libre
    assert timesheets.is_working_day(date(2026, 11, 20))


def _line(day: str, hours: float, desc: str = "x", pid: int = 5) -> dict[str, Any]:
    return {
        "fecha": day,
        "horas": hours,
        "descripcion": desc,
        "proyecto": {"id": pid, "nombre": "P"},
        "tarea": None,
    }


def test_missing_partial_off_and_probably_off_days() -> None:
    lines = [
        _line("2026-10-05", 8),  # lunes
        _line("2026-10-07", 8),
        _line("2026-10-08", 3),  # incompleto
        _line("2026-10-09", 8),
        _line("2026-10-13", 8),  # el 12 es festivo nacional
    ]
    days = timesheets.analyze_days(
        lines,
        date(2026, 10, 5),
        date(2026, 10, 16),
        today=date(2026, 10, 16),
        off_days=["2026-10-15"],
    )
    by = {d.fecha: d for d in days}
    assert "2026-10-10" not in by and "2026-10-11" not in by  # fin de semana
    assert by["2026-10-06"].estado == "libre_probable"  # sin horas entre dias con horas
    assert by["2026-10-08"].estado == "incompleto" and by["2026-10-08"].faltan == 5.0
    assert by["2026-10-12"].estado == "festivo"
    assert by["2026-10-14"].estado == "falta" and by["2026-10-14"].faltan == 8.0
    assert by["2026-10-15"].estado == "libre"
    assert by["2026-10-16"].estado == "falta"


def test_pattern_and_its_learned_line(fake_odoo: FakeOdoo) -> None:
    lines = client(fake_odoo).timesheet_lines("2026-09-01", "2026-11-19")
    pattern = timesheets.learn_pattern(lines)
    descs = [s["descripcion"] for s in pattern]
    assert descs[:2] == [
        "Formación a técnicos (9:00-13:30)",
        "Reunión de seguimiento (13:30-14:00)",
    ]
    assert descs[2].endswith("(15:00-17:00)")  # la variante mas frecuente (aqui, empatadas)
    assert pattern[3]["proyecto"]["id"] == PROJECT_ATLAS and pattern[3]["horas"] == 1.0
    assert len(pattern[2]["variantes"]) == 3 and pattern[0]["dias"] == list("LMXJV")
    text = timesheets.pattern_summary(pattern)
    assert text == (
        "Partes de horas habituales: Formación Ayuntamiento L–V 9:00-13:30 + 13:30-14:00 + "
        "15:00-17:00; Plataforma Atlas L–V 1 h"
    )
    assert learn.reject_reason(text) is None
    assert learn.upsert_prefixed(timesheets.LEARN_PREFIX, text) is True
    assert learn.upsert_prefixed(timesheets.LEARN_PREFIX, text) is False  # sin duplicar
    assert [i.fact for i in learn.list_learned()] == [text]


def test_line_validation(fake_odoo: FakeOdoo) -> None:
    book = horas.HoursBook(client(fake_odoo), TODAY)
    ok = book.validate(
        {"fecha": "2026-11-18", "proyecto_id": PROJECT_TRAINING, "horas": 4.4, "descripcion": " a "}
    )
    assert ok["horas"] == 4.5 and ok["descripcion"] == "a"
    bad = [
        ({"fecha": "2026-11-20"}, "futuro"),
        ({"horas": 0.1}, "horas"),
        ({"horas": 13}, "horas"),
        ({"proyecto_id": PROJECT_CLOSED}, "proyecto"),
        ({"proyecto_id": 999}, "proyecto"),
        ({"tarea_id": 301, "proyecto_id": PROJECT_ATLAS}, "tarea"),  # cerrada
        ({"tarea_id": 300}, "tarea"),  # de otro proyecto
        ({"fecha": "2026-11-04", "horas": 3}, "dia_lleno"),  # ya hay 8 h ese dia
    ]
    base = {"fecha": "2026-11-18", "proyecto_id": PROJECT_TRAINING, "horas": 2, "descripcion": "x"}
    for change, err in bad:
        with pytest.raises(timesheets.LineError) as exc:
            book.validate({**base, **change})
        assert str(exc.value) == err, change
    # pedido expresamente: se permite pasar de 10 h
    assert book.validate({**base, "fecha": "2026-11-04", "horas": 3}, explicit=True)["horas"] == 3


# ============================================================================== plan del modelo


def _raw(tipo: str, datos: dict[str, Any], seg: int | None = 2) -> dict[str, Any]:
    return {
        "tipo": tipo,
        "titulo": tipo,
        "motivo": "m",
        "seg": seg,
        "confianza": 0.9,
        "datos": datos,
    }


def test_hours_proposals_expand_per_day_and_only_explicit_ones_are_checked(
    fake_odoo: FakeOdoo,
) -> None:
    employee_access(fake_odoo)
    c = client(fake_odoo, read_only=True)
    prof = capabilities.discover(c)
    raw = [
        _raw(
            "registrar_horas",
            {
                "proyecto_id": PROJECT_TRAINING,
                "fechas": [*MISSING, "2026-11-20", "2026-11-04"],  # futuro y dia lleno: fuera
                "horas": 4.5,
                "descripcion": "Formación a técnicos (9:00-13:30)",
            },
        ),
        _raw(
            "registrar_horas",
            {
                "proyecto_id": PROJECT_ATLAS,
                "fechas": ["2026-11-19"],
                "horas": 2,
                "descripcion": "Revisión",
                "explicito": True,
            },
        ),
        _raw("registrar_horas", {"proyecto_id": 999, "fechas": ["2026-11-18"], "horas": 1}),
    ]
    out = plan.build_crm(raw, business_transcript(), c, profile=prof, today=TODAY)
    hours = [p for p in out if p["tipo"] == "registrar_horas"]
    assert [p["datos"]["fecha"] for p in hours[:12]] == sorted([*MISSING, "2026-11-19"])
    assert len(hours) == 12 and all(p["grupo"] == "horas" for p in hours)
    explicit = [p for p in hours if p["datos"]["proyecto_id"] == PROJECT_ATLAS]
    assert explicit[0]["checked"] is True
    assert not any(p["checked"] for p in hours if p["datos"]["proyecto_id"] == PROJECT_TRAINING)
    assert fake_odoo.write_calls == []


def test_notes_and_activities_only_where_the_user_can_write(fake_odoo: FakeOdoo) -> None:
    employee_access(fake_odoo)
    c = client(fake_odoo, read_only=True)
    prof = capabilities.discover(c)
    raw = [
        _raw("nota_en_historial", {"partner_id": 41}),  # contacto de solo lectura: fuera
        _raw("nota_en_historial", {"partner_id": 41, "tarea_id": 300}),  # cae en la tarea
        _raw("programar_actividad", {"tarea_id": 300, "resumen": "Llamar", "fecha": "2026-11-23"}),
        _raw("programar_actividad", {"partner_id": 41, "resumen": "Llamar", "fecha": "2026-11-23"}),
        _raw(
            "crear_tarea",
            {"proyecto_id": PROJECT_ATLAS, "nombre": "Preparar demo", "fecha": "2026-11-27"},
        ),
        _raw(
            "crear_tarea", {"proyecto_id": PROJECT_ATLAS, "nombre": "Migrar los informes de Atlas"}
        ),
        _raw("crear_tarea", {"proyecto_id": PROJECT_CLOSED, "nombre": "X"}),
        _raw(
            "crear_evento",
            {
                "nombre": "Revisión de materiales",
                "fecha": "2026-11-24",
                "hora_inicio": "10:00",
                "duracion_h": 1,
                "asistentes_ids": [57, 9999],
            },
        ),
        _raw("crear_evento", {"nombre": "Pasado", "fecha": "2026-11-02", "hora_inicio": "10:00"}),
        _raw("crear_gasto", {"nombre": "Taxi", "importe": 18.5, "fecha": "2026-11-18"}),
    ]
    out = plan.build_crm(raw, business_transcript(), c, profile=prof, today=TODAY)
    kinds = [(p["tipo"], (p["datos"] or {}).get("modelo")) for p in out]
    assert ("nota_en_historial", "project.task") in kinds
    assert ("nota_en_historial", "res.partner") not in kinds
    assert [k for k in kinds if k[0] == "programar_actividad"] == [
        ("programar_actividad", "project.task")
    ]
    tasks = [p for p in out if p["tipo"] == "crear_tarea"]
    assert sorted((t["datos"]["nombre"], t["checked"], t["aviso"]) for t in tasks) == [
        ("Migrar los informes de Atlas", False, "tarea_parecida"),
        ("Preparar demo", True, None),
    ]
    (event,) = [p for p in out if p["tipo"] == "crear_evento"]
    assert event["checked"] and event["datos"]["invitados"] == [
        {"id": 57, "nombre": "Carlos Ruiz Ortega", "on": False}
    ]
    (gasto,) = [p for p in out if p["tipo"] == "crear_gasto"]
    assert gasto["checked"] is False
    assert {p["grupo"] for p in out} == {"agenda"}
    assert fake_odoo.write_calls == []


# ============================================================================== aplicar


def _acta_with(folder: Path, proposals: list[dict[str, Any]]) -> None:
    from micnaga.brain import acta as acta_io

    plan.number(proposals)
    acta_io.write_acta(folder, {"titulo": "Seguimiento", "resumen": ["r"], "propuestas": proposals})


def test_apply_and_undo_business_actions(fake_odoo: FakeOdoo, meeting: Path) -> None:
    c = client(fake_odoo, read_only=True)
    prof = capabilities.discover(c)
    raw = [
        _raw("nota_en_historial", {"tarea_id": 300}),
        _raw("programar_actividad", {"tarea_id": 300, "resumen": "Llamar", "fecha": "2026-11-23"}),
        _raw(
            "crear_tarea",
            {
                "proyecto_id": PROJECT_ATLAS,
                "nombre": "Preparar demo",
                "fecha": "2026-11-27",
                "descripcion": "<b>x</b>",
            },
        ),
        _raw(
            "crear_evento",
            {
                "nombre": "Revisión",
                "fecha": "2026-11-24",
                "hora_inicio": "10:00",
                "asistentes_ids": [57],
            },
        ),
        _raw(
            "registrar_horas",
            {
                "proyecto_id": PROJECT_ATLAS,
                "fechas": ["2026-11-19"],
                "horas": 2,
                "descripcion": "Revisión",
                "explicito": True,
            },
        ),
        _raw("crear_gasto", {"nombre": "Taxi", "importe": 18.5, "fecha": "2026-11-18"}),
    ]
    props = plan.build_crm(raw, business_transcript(), c, profile=prof, today=TODAY)
    _acta_with(meeting, props)
    ids = [p["id"] for p in props]

    def odoo() -> Any:
        return client(fake_odoo)

    ex = Executor(meeting, odoo=odoo, today=TODAY)
    acta = ex.apply(ids)
    assert all(p["estado"] == "aplicada" for p in acta["propuestas"]), acta["propuestas"]
    d = fake_odoo.data
    task = next(t for t in d["project.task"] if t["name"] == "Preparar demo")
    assert task["user_ids"] == [7] and task["date_deadline"] == "2026-11-27 12:00:00"
    assert task["description"] == "<p>&lt;b&gt;x&lt;/b&gt;</p>"
    event = next(e for e in d["calendar.event"] if e["name"] == "Revisión")
    assert event["partner_ids"] == [PARTNER_ME]  # invitados apagados: solo el usuario
    assert event["start"] == "2026-11-24 09:00:00" and event["stop"] == "2026-11-24 10:00:00"
    assert ("calendar.event", {"no_mail_to_attendees": True}) in fake_odoo.contexts
    line = d["account.analytic.line"][-1]
    assert (line["project_id"], line["unit_amount"], line["date"]) == (
        PROJECT_ATLAS,
        2,
        "2026-11-19",
    )
    assert line["employee_id"] == 70  # lo puso Odoo, no Micnaga
    note = [m for m in d["mail.message"] if m.get("model") == "project.task"]
    assert note and "Seguimiento" in str(note[0]["body"])
    act = next(a for a in d["mail.activity"] if a.get("summary") == "Llamar")
    assert act["res_id"] == 300 and act["res_model_id"] == 302
    assert d["hr.expense"][-1]["total_amount_currency"] == 18.5

    n = len(fake_odoo.write_calls)
    Executor(meeting, odoo=odoo, today=TODAY).apply(ids)  # reaplicar no duplica
    assert len(fake_odoo.write_calls) == n

    undone = Executor(meeting, odoo=odoo, today=TODAY).undo(ids)
    assert all(p["estado"] == "deshecha" for p in undone["propuestas"])
    assert not any(t["name"] == "Preparar demo" for t in d["project.task"])
    assert not d["calendar.event"] and not d["hr.expense"]
    assert d["account.analytic.line"][-1]["date"] == "2026-11-04"


def test_every_item_is_editable_and_replaced_keeping_its_id(
    fake_odoo: FakeOdoo, meeting: Path
) -> None:
    from micnaga.brain import acta as acta_io

    employee_access(fake_odoo)

    c = client(fake_odoo, read_only=True)
    prof = capabilities.discover(c)
    raw = [
        _raw("nota_en_historial", {"tarea_id": 300}),
        _raw("crear_tarea", {"proyecto_id": PROJECT_ATLAS, "nombre": "Preparar demo"}),
        _raw("programar_actividad", {"tarea_id": 300, "resumen": "Llamar", "fecha": "2026-11-23"}),
        _raw("crear_gasto", {"nombre": "Taxi", "importe": 18.5, "fecha": "2026-11-18"}),
    ]
    props = plan.build_crm(raw, business_transcript(), c, profile=prof, today=TODAY)
    _acta_with(meeting, props)
    opts = plan.options(c, prof, props)
    assert {"proyectos", "destinos", "tipos_actividad"} <= set(opts)
    assert {"id": "project.task:300", "nombre": "Migrar informes de Atlas · Plataforma Atlas"} in (
        opts["destinos"]
    )
    acta = plan.decorate(acta_io.read_acta(meeting) or {})
    by_type = {p["tipo"]: p for p in acta["propuestas"]}
    note_fields = {f["clave"]: f for f in by_type["nota_en_historial"]["campos"]}
    assert note_fields["destino"]["valor"] == "project.task:300"
    assert note_fields["cuerpo"]["valor"].startswith("Seguimiento")
    assert [f["clave"] for f in by_type["crear_tarea"]["campos"]] == [
        "nombre",
        "proyecto_id",
        "fecha",
        "descripcion",
    ]

    ex = Executor(meeting, odoo=lambda: client(fake_odoo), today=TODAY)
    task_id = by_type["crear_tarea"]["id"]
    acta = ex.edit(
        task_id,
        {"nombre": "Demo para el cliente", "proyecto_id": PROJECT_TRAINING, "fecha": "2026-11-30"},
    )
    task = next(p for p in acta["propuestas"] if p["id"] == task_id)
    assert task["datos"]["nombre"] == "Demo para el cliente"
    assert task["datos"]["proyecto"] == "Formación Ayuntamiento" and task["checked"]
    with pytest.raises(ApplyError):
        ex.edit(task_id, {"proyecto_id": PROJECT_CLOSED})  # misma validacion que al proponer
    note_id = by_type["nota_en_historial"]["id"]
    ex.edit(note_id, {"cuerpo": "Resumen corto\n\n<script>x</script>"})
    with pytest.raises(ApplyError):
        ex.edit(note_id, {"destino": "res.partner:41"})  # sin escritura en contactos
    act_id = by_type["programar_actividad"]["id"]
    ex.edit(act_id, {"tipo_actividad_id": 4, "fecha": "2026-11-25"})
    # una version revisada (p. ej. por la IA) sustituye al item con el mismo id
    acta = ex.replace(
        by_type["crear_gasto"]["id"],
        _raw("crear_gasto", {"nombre": "Taxi aeropuerto", "importe": 22, "fecha": "2026-11-18"}),
    )
    gasto = next(p for p in acta["propuestas"] if p["tipo"] == "crear_gasto")
    assert gasto["id"] == by_type["crear_gasto"]["id"] and gasto["datos"]["importe"] == 22
    assert [p["id"] for p in acta["propuestas"]] == [p["id"] for p in props]

    Executor(meeting, odoo=lambda: client(fake_odoo), today=TODAY).apply([note_id, act_id, task_id])
    note = [m for m in fake_odoo.data["mail.message"] if m.get("model") == "project.task"][-1]
    assert note["body"] == "<p>Resumen corto</p><p>&lt;script&gt;x&lt;/script&gt;</p>"
    act = fake_odoo.data["mail.activity"][-1]
    assert act["activity_type_id"] == 4 and act["date_deadline"] == "2026-11-25"
    assert fake_odoo.data["project.task"][-1]["project_id"] == PROJECT_TRAINING


def test_edit_hours_and_attendees_are_validated_in_python(
    fake_odoo: FakeOdoo, meeting: Path
) -> None:
    c = client(fake_odoo, read_only=True)
    prof = capabilities.discover(c)
    raw = [
        _raw(
            "registrar_horas",
            {
                "proyecto_id": PROJECT_TRAINING,
                "fechas": ["2026-11-18"],
                "horas": 4.5,
                "descripcion": "F",
            },
        ),
        _raw(
            "crear_evento",
            {"nombre": "R", "fecha": "2026-11-24", "hora_inicio": "10:00", "asistentes_ids": [57]},
        ),
    ]
    props = plan.build_crm(raw, business_transcript(), c, profile=prof, today=TODAY)
    _acta_with(meeting, props)
    hours_id = next(p["id"] for p in props if p["tipo"] == "registrar_horas")
    event_id = next(p["id"] for p in props if p["tipo"] == "crear_evento")
    ex = Executor(meeting, odoo=lambda: client(fake_odoo), today=TODAY)
    acta = ex.edit(hours_id, {"horas": 3, "descripcion": "Formación (9:00-12:00)"})
    p = next(x for x in acta["propuestas"] if x["id"] == hours_id)
    assert p["datos"]["horas"] == 3 and p["checked"]
    with pytest.raises(timesheets.LineError):
        ex.edit(hours_id, {"horas": 20})
    with pytest.raises(timesheets.LineError):
        ex.edit(hours_id, {"proyecto_id": PROJECT_CLOSED})
    acta = ex.edit(event_id, {"invitados_on": [57]})
    ev = next(x for x in acta["propuestas"] if x["id"] == event_id)
    assert ev["datos"]["invitados"][0]["on"] is True
    Executor(meeting, odoo=lambda: client(fake_odoo), today=TODAY).apply([event_id])
    assert fake_odoo.data["calendar.event"][-1]["partner_ids"] == [PARTNER_ME, 57]
    with pytest.raises(ApplyError):
        ex.edit(event_id, {"invitados_on": []})  # ya aplicada


# ============================================================================== panel


def test_hours_panel_plan_apply_undo_and_days_off(fake_odoo: FakeOdoo) -> None:
    from micnaga.brain.timesheets import Evidence

    c = client(fake_odoo)
    meetings = [Evidence("2026-11-10", "Seguimiento semanal", "13:30", 0.6, "reunion")]
    out = horas.plan(c, today=TODAY, meetings=meetings)
    assert [d["fecha"] for d in out["dias"]] == MISSING
    assert out["posibles_libres"] == ["2026-11-02"]
    assert out["aprendido"] and learn.list_learned()[0].fact.startswith("Partes de horas")
    day = next(d for d in out["dias"] if d["fecha"] == "2026-11-10")
    origins = [(ln["origen"], ln["checked"], ln["horas"]) for ln in day["lineas"]]
    # la reunion grabada sustituye a la franja 13:30-14:00 y es la unica marcada
    assert origins == [
        ("patron", False, 4.5),
        ("reunion", True, 0.5),
        ("patron", False, 2.0),
        ("patron", False, 1.0),
    ]
    assert day["lineas"][1]["proyecto_id"] == PROJECT_TRAINING
    other = next(d for d in out["dias"] if d["fecha"] == "2026-11-05")
    assert not any(ln["checked"] for ln in other["lineas"])
    assert sum(ln["horas"] for ln in other["lineas"]) == 8.0

    lines = [*other["lineas"], {**other["lineas"][0], "key": "bad", "horas": 30}]
    res = horas.apply(c, lines, today=TODAY)
    assert res["creadas"] == 4
    assert [r["error"] for r in res["resultados"]] == [None, None, None, None, "horas"]
    created = [ln for ln in fake_odoo.data["account.analytic.line"] if ln["date"] == "2026-11-05"]
    assert len(created) == 4 and all(ln["employee_id"] == 70 for ln in created)
    again = horas.apply(c, other["lineas"][:1], today=TODAY)
    assert again["resultados"][0]["error"] == "ya_existe" and again["creadas"] == 0
    assert horas.undo(c, res["lote"]) == {"ok": True, "deshechas": 4}
    assert not [ln for ln in fake_odoo.data["account.analytic.line"] if ln["date"] == "2026-11-05"]
    assert horas.undo(c, res["lote"])["ok"] is False

    horas.set_off("2026-11-06", True)
    out = horas.plan(c, today=TODAY)
    assert "2026-11-06" not in [d["fecha"] for d in out["dias"]]
    assert out["libres"] == ["2026-11-06"]


def test_short_or_unrelated_evidence_never_pre_checks_a_guessed_project(
    fake_odoo: FakeOdoo,
) -> None:
    from micnaga.brain.timesheets import Evidence

    c = client(fake_odoo)
    lines = c.timesheet_lines("2026-09-01", "2026-11-19")
    pattern = timesheets.learn_pattern(lines)
    projects = c.projects()
    # 15 min dentro de la franja de 9:00-13:30: la franja sigue, sin linea extra
    short = [Evidence("2026-11-12", "Llamada rápida", "10:00", 0.25, "reunion")]
    out = timesheets.propose_day("2026-11-12", pattern, [], short, projects)
    assert [(ln["origen"], ln["horas"], ln["checked"]) for ln in out] == [
        ("patron", 4.5, False),
        ("patron", 0.5, False),
        ("patron", 2.0, False),
        ("patron", 1.0, False),
    ]
    # fuera de las franjas: solo si el titulo se parece MUCHO a un proyecto sale marcada
    evening = [
        Evidence("2026-11-12", "Plataforma Atlas: revisión", "18:00", 1.0, "calendario"),
        Evidence("2026-11-12", "Cena con amigos", "21:00", 2.0, "calendario"),
    ]
    out = timesheets.propose_day("2026-11-12", pattern, [], evening, projects)
    extra = [ln for ln in out if ln["origen"] != "patron"]
    assert [(ln["proyecto_id"], ln["checked"]) for ln in extra] == [(PROJECT_ATLAS, True)]


def test_user_typed_contact_details_are_kept(fake_odoo: FakeOdoo, meeting: Path) -> None:
    c = client(fake_odoo, read_only=True)
    prof = capabilities.discover(c)
    props = plan.build_crm(
        [_raw("crear_contacto", {"nombre": "Ana Ruiz", "empresa_id": 41})],
        business_transcript(),
        c,
        profile=prof,
        today=TODAY,
    )
    _acta_with(meeting, props)
    ex = Executor(meeting, odoo=lambda: client(fake_odoo), today=TODAY)
    acta = ex.edit(props[0]["id"], {"email": "ana@cliente.es", "telefono": "600 000 000"})
    d = acta["propuestas"][0]["datos"]
    assert (d["email"], d["telefono"]) == ("ana@cliente.es", "600 000 000")


def test_daily_odoo_check_backs_off_after_a_failure() -> None:
    from micnaga.brain.service import BrainService

    cfg = Config()
    cfg.secretaria.enabled = True
    cfg.secretaria.odoo_url, cfg.secretaria.odoo_db = "http://127.0.0.1:9", "x"
    cfg.secretaria.odoo_login = "y"
    calls: list[int] = []

    def factory(_cfg: Config) -> Any:
        calls.append(1)
        raise OdooError("Odoo is not responding", "offline")

    svc = BrainService(lambda: cfg, odoo_factory=factory)
    svc._odoo_daily()
    svc._odoo_daily()
    assert calls == [1]  # el segundo tic no reintenta (una hora de espera)


def test_weekly_nudge_only_on_friday_afternoon_once(fake_odoo: FakeOdoo) -> None:
    fri = datetime(2026, 11, 20, 16, 0)
    assert horas.should_nudge(fri, enabled=True, last=None)
    assert not horas.should_nudge(fri, enabled=False, last=None)
    assert not horas.should_nudge(fri.replace(hour=11), enabled=True, last=None)
    assert not horas.should_nudge(fri, enabled=True, last="2026-11-20")
    assert not horas.should_nudge(datetime(2026, 11, 19, 16, 0), enabled=True, last=None)
    assert horas.week_gaps(client(fake_odoo), fri.date()) == [
        "2026-11-16",
        "2026-11-17",
        "2026-11-18",
        "2026-11-19",
        "2026-11-20",
    ]


def test_service_nudges_through_the_ui_and_serves_the_panel(fake_odoo: FakeOdoo) -> None:
    from micnaga.brain.service import BrainService

    cfg = odoo_config(Config(), fake_odoo)
    cfg.secretaria.enabled = True
    svc = BrainService(lambda: cfg)
    svc.now = lambda: datetime(2026, 11, 20, 16, 30)
    seen: list[int] = []
    svc.set_nudge(seen.append)
    svc._odoo_daily()
    assert seen == []  # apagado por defecto
    cfg.secretaria.horas_nudge = True
    svc._odoo_daily()
    svc._odoo_daily()  # una vez por semana
    assert seen == [5]
    caps = svc.api("caps", {})
    assert caps["horas"] is True and caps["capacidades"][0]["clave"] == "horas"
    plan_out = svc.api("horas_plan", {})
    assert plan_out["ok"] and len(plan_out["dias"]) == 12  # hasta el viernes 20
    first = plan_out["dias"][0]["lineas"][0]
    applied = svc.api("horas_apply", {"lineas": [first]})
    assert applied["ok"] and applied["creadas"] == 1
    assert svc.api("horas_undo", {"lote": applied["lote"]})["ok"]
    assert svc.api("horas_off", {"fecha": "nope"})["ok"] is False
    assert fake_odoo.write_calls.count(("account.analytic.line", "create")) == 1
