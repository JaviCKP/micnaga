"""«Corregir con un mensaje» (``micnaga.brain.refine``) con un runner falso y el Odoo falso.

Comprueba el contrato: salida limitada al esquema del tipo, mismo id con ``revision`` nueva, la
marca no la decide el modelo, nunca se activan invitados ni envios, «Deshacer» de un paso y, en la
lista entera, quitar/añadir con lo nuevo sin marcar.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from micnaga.brain import acta as acta_io
from micnaga.brain import capabilities, plan, refine
from micnaga.brain.claude import ClaudeRequest, ClaudeResult, ToolCall, ToolResult
from micnaga.brain.executor import ApplyError, Executor
from micnaga.brain.refine import RefineError, Refiner
from micnaga.config import Config
from tests.brain.conftest import approve, odoo_config
from tests.brain.demo import business_transcript
from tests.brain.fake_odoo import PROJECT_ATLAS, PROJECT_TRAINING, FakeOdoo
from tests.brain.test_odoo_business import TODAY, _acta_with, _raw, client, employee_access


class FakeRunner:
    """``run_once`` devuelve la salida estructurada que se le de (y anota cada peticion)."""

    def __init__(self, *outputs: dict[str, Any], tool_text: str = "", during: Any = None) -> None:
        self.outputs = list(outputs)
        self.requests: list[ClaudeRequest] = []
        self.tool_text = tool_text
        self.during = during

    def run_once(self, req: ClaudeRequest, on_event: Any = None) -> ClaudeResult:
        self.requests.append(req)
        if on_event is not None:
            on_event(ToolCall("mcp__micnaga__proyectos", {"texto": "RegulAI"}))
        if self.during is not None:
            self.during()
        results = [ToolResult("t1", "mcp__micnaga__proyectos", False, self.tool_text)]
        return ClaudeResult(
            structured=self.outputs.pop(0),
            text="",
            cost_usd=0.0,
            usage={},
            duration_s=0.4,
            num_turns=2,
            model="fake",
            tool_results=results,
        )

    def run(self, req: ClaudeRequest, on_event: Any = None) -> ClaudeResult:
        raise AssertionError("refine must not retry blindly (run_once only)")


def _odoo_meeting(srv: FakeOdoo, meeting: Path) -> tuple[Config, list[dict[str, Any]]]:
    employee_access(srv)
    c = client(srv, read_only=True)
    prof = capabilities.ensure(c)
    raw = [
        _raw("crear_tarea", {"proyecto_id": PROJECT_ATLAS, "nombre": "Preparar demo"}),
        _raw(
            "crear_evento",
            {"nombre": "Demo", "fecha": "2026-12-03", "hora_inicio": "10:00", "duracion_h": 1},
        ),
        _raw("crear_gasto", {"nombre": "Taxi", "importe": 18.5, "fecha": "2026-11-18"}),
    ]
    props = plan.build_crm(raw, business_transcript(), c, profile=prof, today=TODAY)
    _acta_with(meeting, props)
    acta = acta_io.read_acta(meeting) or {}
    acta["opciones"] = plan.options(c, prof, acta["propuestas"])
    acta_io.write_acta(meeting, acta)
    return odoo_config(Config(), srv), acta["propuestas"]


def _refiner(cfg: Config, meeting: Path, srv: FakeOdoo | None, runner: FakeRunner) -> Refiner:
    ex = Executor(meeting, odoo=(lambda: client(srv)) if srv else None, today=TODAY)
    return Refiner(cfg, ex, runner=runner, today=TODAY)


def _item(meeting: Path, pid: str) -> dict[str, Any]:
    acta = acta_io.read_acta(meeting) or {}
    return next(p for p in acta["propuestas"] if p["id"] == pid)


# ============================================================================== un item


def test_item_revision_is_schema_constrained_keeps_its_id_and_bumps_revision(
    fake_odoo: FakeOdoo, meeting: Path
) -> None:
    cfg, props = _odoo_meeting(fake_odoo, meeting)
    task = next(p for p in props if p["tipo"] == "crear_tarea")
    checked = task["checked"]
    runner = FakeRunner(
        {
            "cambiado": True,
            "nota": "",
            "datos": {
                "fecha": "2026-11-26",
                "proyecto_id": PROJECT_TRAINING,
                "user_ids": [7],  # no es un campo de este tipo: se ignora
                "empresa_id": 3,
            },
        }
    )
    seen: list[dict[str, Any]] = []
    out = _refiner(cfg, meeting, fake_odoo, runner).item(
        task["id"], "mejor para el jueves y en Formación", on_status=seen.append
    )

    req = runner.requests[0]
    schema = req.schema or {}
    datos = schema["properties"]["datos"]
    assert set(datos["properties"]) == {"proyecto_id", "nombre", "fecha", "descripcion"}
    assert datos["additionalProperties"] is False
    assert schema["required"] == ["cambiado", "nota", "datos"]
    assert "mejor para el jueves" in req.prompt and "jueves 2026-11-19" in req.prompt
    assert "proyecto_id:" in req.prompt  # las opciones conocidas van en la entrada
    assert req.max_turns <= 6 and req.allowed_tools == ["mcp__micnaga__*"]
    assert "mcp__micnaga__*" in req.allowed_tools and req.strict_mcp  # Google apagado

    new = _item(meeting, task["id"])
    assert new["id"] == task["id"] and new["revision"] == 1
    assert new["datos"]["fecha"] == "2026-11-26"
    assert new["datos"]["proyecto_id"] == PROJECT_TRAINING
    assert new["datos"]["proyecto"] == "Formación Ayuntamiento"
    assert "user_ids" not in new["datos"] and "empresa_id" not in new["datos"]
    assert new["checked"] == checked  # la marca no la decide el modelo
    assert set(out.changed) == {"proyecto_id", "fecha"}
    assert new["previa"]["datos"]["proyecto_id"] == PROJECT_ATLAS
    assert [p["id"] for p in out.acta["propuestas"]] == [p["id"] for p in props]
    assert seen[0] == {"fase": "pensando"} and seen[1]["tool"] == "proyectos"
    assert not fake_odoo.data["project.task"] or all(
        t.get("name") != "Preparar demo" for t in fake_odoo.data["project.task"]
    )  # revisar nunca escribe en Odoo


def test_undo_goes_back_one_step_and_revision_keeps_growing(
    fake_odoo: FakeOdoo, meeting: Path
) -> None:
    cfg, props = _odoo_meeting(fake_odoo, meeting)
    gasto = next(p for p in props if p["tipo"] == "crear_gasto")
    runner = FakeRunner({"cambiado": True, "nota": "", "datos": {"importe": 22}})
    _refiner(cfg, meeting, fake_odoo, runner).item(gasto["id"], "eran 22 €")
    assert _item(meeting, gasto["id"])["datos"]["importe"] == 22

    acta = Executor(meeting, odoo=lambda: client(fake_odoo), today=TODAY).revert(gasto["id"])
    back = next(p for p in acta["propuestas"] if p["id"] == gasto["id"])
    assert back["datos"]["importe"] == 18.5 and back["revision"] == 2
    assert back["cambiados"] == ["importe"] and "previa" not in back
    with pytest.raises(ApplyError):  # un solo paso
        Executor(meeting, today=TODAY).revert(gasto["id"])


def test_a_revision_can_never_invite_or_send(fake_odoo: FakeOdoo, meeting: Path) -> None:
    cfg, props = _odoo_meeting(fake_odoo, meeting)
    ev = next(p for p in props if p["tipo"] == "crear_evento")
    runner = FakeRunner(
        {"cambiado": True, "nota": "", "datos": {"asistentes_ids": [41], "hora_inicio": "11:00"}}
    )
    _refiner(cfg, meeting, fake_odoo, runner).item(ev["id"], "a las 11 y que venga Carlos")
    new = _item(meeting, ev["id"])
    assert new["datos"]["hora_inicio"] == "11:00"
    assert new["datos"]["invitados"] and not any(i["on"] for i in new["datos"]["invitados"])

    # Google: los asistentes nuevos entran apagados, «enviar» no se elige, correos sin ver fuera
    acta = acta_io.read_acta(meeting) or {}
    gev = _google_event(on=["ana@cliente.com"])
    mail = _google_mail()
    acta["propuestas"] += [gev, mail]
    acta_io.write_acta(meeting, acta)
    runner = FakeRunner(
        {
            "cambiado": True,
            "nota": "",
            "datos": {
                "asistentes": [
                    {"nombre": "Ana", "email": "ana@cliente.com"},
                    {"nombre": "Marta", "email": "marta@dentaltriana.es"},
                    {"nombre": "X", "email": "inventado@otro.com"},
                ],
                "inicio": "2026-11-26T10:00",
            },
        },
        {
            "cambiado": True,
            "nota": "",
            "datos": {"modo": "send", "para": ["marta@dentaltriana.es", "nadie@x.com"]},
        },
    )
    r = _refiner(cfg, meeting, fake_odoo, runner)
    r.item("p90", "el jueves a las 10 con Marta")
    r.item("p91", "mándaselo ya a Marta")
    g = _item(meeting, "p90")
    people = {a["email"]: a["on"] for a in g["datos"]["asistentes"]}
    assert people == {"ana@cliente.com": True, "marta@dentaltriana.es": False}
    assert g["invita"] == ["ana@cliente.com"] and g["revision"] == 1
    m = _item(meeting, "p91")
    assert m["datos"]["modo"] == "draft" and m["datos"]["para"] == ["marta@dentaltriana.es"]
    assert not m.get("envia")


def test_declined_unchanged_and_stale_revisions_say_so(fake_odoo: FakeOdoo, meeting: Path) -> None:
    cfg, props = _odoo_meeting(fake_odoo, meeting)
    task = next(p for p in props if p["tipo"] == "crear_tarea")
    runner = FakeRunner(
        {"cambiado": False, "nota": "No puedo asignar tareas a otras personas", "datos": {}}
    )
    with pytest.raises(RefineError) as e:
        _refiner(cfg, meeting, fake_odoo, runner).item(task["id"], "asígnasela a Ana")
    assert e.value.kind == "declined" and "asignar" in e.value.note
    assert _item(meeting, task["id"]).get("revision") is None

    runner = FakeRunner(
        {"cambiado": True, "nota": "", "datos": {"nombre": task["datos"]["nombre"]}}
    )
    with pytest.raises(RefineError) as e:
        _refiner(cfg, meeting, fake_odoo, runner).item(task["id"], "igual")
    assert e.value.kind == "unchanged"

    def user_edits_meanwhile() -> None:
        Executor(meeting, odoo=lambda: client(fake_odoo), today=TODAY).edit(
            task["id"], {"nombre": "Lo escribió el usuario"}
        )

    runner = FakeRunner(
        {"cambiado": True, "nota": "", "datos": {"nombre": "Lo dijo la IA"}},
        during=user_edits_meanwhile,
    )
    with pytest.raises(RefineError) as e:
        _refiner(cfg, meeting, fake_odoo, runner).item(task["id"], "llámala de otra forma")
    assert e.value.kind == "stale"
    assert _item(meeting, task["id"])["datos"]["nombre"] == "Lo escribió el usuario"


def test_works_without_odoo_and_without_google(meeting: Path) -> None:
    acta = {
        "titulo": "Seguimiento",
        "resumen": ["r"],
        "propuestas": [
            {
                "id": "p1",
                "grupo": "correcciones",
                "tipo": "palabra",
                "estado": "pendiente",
                "checked": True,
                "datos": {"de": "Odu", "a": "Odú", "n": 1},
            },
            {
                "id": "p2",
                "grupo": "agenda",
                "tipo": "crear_gasto",
                "estado": "pendiente",
                "datos": {"nombre": "Taxi", "importe": 10, "fecha": "2026-11-18"},
            },
        ],
    }
    acta_io.write_acta(meeting, acta)
    runner = FakeRunner({"cambiado": True, "nota": "", "datos": {"a": "Odoo"}})
    r = _refiner(Config(), meeting, None, runner)
    out = r.item("p1", "es Odoo, el ERP")
    assert _item(meeting, "p1")["datos"]["a"] == "Odoo" and out.changed == ["a"]
    assert set(runner.requests[0].schema["properties"]["datos"]["properties"]) == {"a"}  # type: ignore[index]
    with pytest.raises(RefineError) as e:  # sin Odoo: se dice al momento, sin llamar a Claude
        r.item("p2", "eran 12 €")
    assert e.value.kind == "odoo_unavailable" and len(runner.requests) == 1


# ============================================================================== la lista


def test_plan_message_adds_unchecked_items_removes_and_undoes(
    fake_odoo: FakeOdoo, meeting: Path
) -> None:
    cfg, props = _odoo_meeting(fake_odoo, meeting)
    ids = [p["id"] for p in props]
    gasto = next(p for p in props if p["tipo"] == "crear_gasto")
    task = next(p for p in props if p["tipo"] == "crear_tarea")
    runner = FakeRunner(
        {
            "nota": "",
            "cambiar": [{"id": task["id"], "datos": {"fecha": "2026-11-27"}}],
            "quitar": [gasto["id"]],
            "anadir": [
                {
                    "tipo": "crear_tarea",
                    "titulo": "Tarea: plantillas",
                    "motivo": "lo pidió el usuario",
                    "seg": 7,
                    "datos": {"proyecto_id": PROJECT_TRAINING, "nombre": "Revisar plantillas"},
                },
                {  # un tipo que este usuario no puede crear: fuera
                    "tipo": "crear_contacto",
                    "titulo": "x",
                    "motivo": "x",
                    "seg": None,
                    "datos": {"nombre": "Marta Gil"},
                },
            ],
        }
    )
    out = _refiner(cfg, meeting, fake_odoo, runner).plan("añade una tarea para las plantillas")
    schema = runner.requests[0].schema or {}
    assert schema["properties"]["quitar"]["items"]["enum"] == ids
    assert (
        "crear_contacto"
        not in schema["properties"]["anadir"]["items"]["properties"]["tipo"]["enum"]
    )
    acta = acta_io.read_acta(meeting) or {}
    by_id = {p["id"]: p for p in acta["propuestas"]}
    assert by_id[gasto["id"]]["estado"] == "descartada" and out.removed == [gasto["id"]]
    assert by_id[task["id"]]["datos"]["fecha"] == "2026-11-27" and out.changed == [task["id"]]
    assert len(out.added) == 1
    new = by_id[out.added[0]]
    assert (
        new["checked"] is False and new["nueva"] and new["datos"]["nombre"] == "Revisar plantillas"
    )
    assert new["id"] not in ids and new["id"] == f"p{len(ids) + 1}"

    acta = _refiner(cfg, meeting, fake_odoo, FakeRunner()).undo_plan()
    assert [p["id"] for p in acta["propuestas"]] == ids
    back = {p["id"]: p for p in acta["propuestas"]}
    assert back[gasto["id"]]["estado"] == "pendiente"
    assert back[task["id"]]["datos"].get("fecha") != "2026-11-27"
    assert back[task["id"]]["revision"] == 2  # sigue subiendo


def test_service_api_reports_progress_and_errors(fake_odoo: FakeOdoo, meeting: Path) -> None:
    from micnaga.brain.service import BrainService

    cfg, props = _odoo_meeting(fake_odoo, meeting)
    cfg.secretaria.enabled = True
    gasto = next(p for p in props if p["tipo"] == "crear_gasto")
    statuses: list[Any] = []
    svc: BrainService

    def peek() -> None:
        statuses.append(svc.api("refine_status", {"folder": meeting.name})["status"])

    runner = FakeRunner({"cambiado": True, "nota": "", "datos": {"importe": 30}}, during=peek)
    svc = BrainService(
        lambda: cfg,
        runner_factory=lambda _c: runner,
        odoo_factory=lambda _c: client(fake_odoo),
    )
    svc.now = lambda: __import__("datetime").datetime(2026, 11, 19, 12, 0)
    r = svc.api("refine", {"folder": meeting.name, "id": gasto["id"], "texto": "eran 30"})
    assert r["ok"] and r["cambiados"] == ["importe"]
    assert statuses[0]["tool"] == "proyectos" and statuses[0]["id"] == gasto["id"]
    assert svc.api("refine_status", {"folder": meeting.name})["status"] is None
    item = next(p for p in r["acta"]["propuestas"] if p["id"] == gasto["id"])
    assert item["campos"] and {f["clave"]: f["valor"] for f in item["campos"]}["importe"] == 30

    r = svc.api("refine_undo", {"folder": meeting.name, "id": gasto["id"]})
    assert r["ok"]
    runner.outputs.append({"cambiado": False, "nota": "No sé a qué te refieres", "datos": {}})
    r = svc.api("refine", {"folder": meeting.name, "id": gasto["id"], "texto": "eso"})
    assert r == {
        "ok": False,
        "error": "secretaria.refine.err.declined",
        "nota": "No sé a qué te refieres",
    }
    assert threading.active_count() >= 1


def test_applied_odoo_items_carry_their_link(fake_odoo: FakeOdoo, meeting: Path) -> None:
    from micnaga.brain.service import BrainService

    cfg, props = _odoo_meeting(fake_odoo, meeting)
    cfg.secretaria.enabled = True
    task = next(p for p in props if p["tipo"] == "crear_tarea")
    svc = BrainService(lambda: cfg, odoo_factory=lambda _c: client(fake_odoo))
    r = approve(svc, meeting.name, [task["id"]])
    done = next(p for p in r["acta"]["propuestas"] if p["id"] == task["id"])
    assert done["estado"] == "aplicada"
    assert done["enlace"].startswith(fake_odoo.url) and "model=project.task" in done["enlace"]


# ============================================================================== ayudas


def _google_event(*, on: list[str]) -> dict[str, Any]:
    from micnaga.brain.google.analysis import refresh

    p: dict[str, Any] = {
        "id": "p90",
        "grupo": "google",
        "tipo": "evento_google",
        "estado": "pendiente",
        "checked": True,
        "seg": 9,
        "datos": {
            "titulo": "Demo",
            "inicio": "2026-12-03T10:00",
            "duracion_min": 30,
            "huecos": [],
            "asistentes": [
                {"nombre": "Ana", "email": "ana@cliente.com", "on": "ana@cliente.com" in on}
            ],
            "descripcion": "x",
            "calendario": "primary",
        },
    }
    refresh(p)
    return p


def _google_mail() -> dict[str, Any]:
    from micnaga.brain.google.analysis import refresh

    p: dict[str, Any] = {
        "id": "p91",
        "grupo": "google",
        "tipo": "correo_google",
        "estado": "pendiente",
        "checked": True,
        "datos": {
            "modo": "draft",
            "para": ["carlos@dentaltriana.es"],
            "asunto": "Presupuesto",
            "cuerpo": "Hola",
        },
    }
    refresh(p)
    return p


def test_schemas_per_kind_are_closed() -> None:
    for kind in refine.ODOO_KEYS:
        s = refine.item_schema({"grupo": "agenda", "tipo": kind})
        props = s["properties"]["datos"]["properties"]
        assert set(props) == set(refine.ODOO_KEYS[kind]), kind
    for kind in ("evento_google", "correo_google", "acta_drive"):
        s = refine.item_schema({"grupo": "google", "tipo": kind})
        assert s["properties"]["datos"]["additionalProperties"] is False
        people = s["properties"]["datos"]["properties"].get("asistentes")
        if people:  # el modelo no puede decir «on»
            assert "on" not in people["items"]["properties"]
    json.dumps(refine.plan_schema(["p1"], ["crear_tarea", "evento_google"]))
