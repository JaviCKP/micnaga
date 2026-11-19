"""«Corregir con un mensaje» con el ``claude -p`` REAL (tu sesion de Claude Code) y el Odoo FALSO.

``MICNAGA_RUN_CLAUDE=1 pytest -m claude -s tests/brain/test_real_refine.py``

Una tarea sintetica («Preparar demo», Plataforma Atlas, sin fecha) se corrige con un mensaje:
«mejor para el jueves y ponla en el proyecto RegulAI». RegulAI existe en el Odoo falso pero no en
las opciones del acta: el modelo tiene que buscarlo con las herramientas de solo lectura. Nada se
escribe en Odoo ni en Google (Google apagado).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from micnaga.brain import acta as acta_io
from micnaga.brain import capabilities, plan
from micnaga.brain.claude import ClaudeRunner
from micnaga.brain.executor import Executor
from micnaga.brain.refine import Refiner
from micnaga.config import Config
from tests.brain.conftest import odoo_config
from tests.brain.demo import business_transcript
from tests.brain.fake_odoo import PROJECT_ATLAS, FakeOdoo
from tests.brain.test_odoo_business import TODAY, _acta_with, _raw, client, employee_access

pytestmark = [pytest.mark.claude, pytest.mark.timeout(600)]

PROJECT_REGULAI = 12


class Timed(ClaudeRunner):
    """El runner real; guarda el ultimo resultado para el informe."""

    last = None

    def run_once(self, req, on_event=None):  # type: ignore[no-untyped-def]
        res = super().run_once(req, on_event)
        Timed.last = res
        return res


def test_real_refine_changes_date_and_project(fake_odoo: FakeOdoo, meeting: Path) -> None:
    fake_odoo.data["project.project"].append(
        {
            "id": PROJECT_REGULAI,
            "name": "RegulAI",
            "active": True,
            "allow_timesheets": False,  # no sale en las opciones del acta: hay que buscarlo
            "partner_id": False,
        }
    )
    employee_access(fake_odoo)
    c = client(fake_odoo, read_only=True)
    prof = capabilities.ensure(c)
    props = plan.build_crm(
        [_raw("crear_tarea", {"proyecto_id": PROJECT_ATLAS, "nombre": "Preparar demo"})],
        business_transcript(),
        c,
        profile=prof,
        today=TODAY,
    )
    _acta_with(meeting, props)
    acta = acta_io.read_acta(meeting) or {}
    acta["opciones"] = plan.options(c, prof, acta["propuestas"])
    assert all(o["nombre"] != "RegulAI" for o in acta["opciones"]["proyectos"])
    acta_io.write_acta(meeting, acta)
    writes_before = {m: len(rows) for m, rows in fake_odoo.data.items()}

    cfg = odoo_config(Config(), fake_odoo)
    ex = Executor(meeting, odoo=lambda: client(fake_odoo), today=TODAY)
    steps: list[str] = []
    t0 = time.monotonic()
    out = Refiner(cfg, ex, runner=Timed(), today=TODAY).item(
        "p1",
        "mejor para el jueves y ponla en el proyecto RegulAI",
        on_status=lambda s: steps.append(str(s.get("tool") or s.get("fase"))),
    )
    wall = time.monotonic() - t0
    res = Timed.last
    task = next(p for p in out.acta["propuestas"] if p["id"] == "p1")
    print(
        "\nREAL-REFINE "
        + json.dumps(
            {
                "wall_s": round(wall, 1),
                "claude_s": getattr(res, "duration_s", None),
                "turns": getattr(res, "num_turns", None),
                "tools": [t.tool for t in getattr(res, "tool_calls", [])],
                "model": getattr(res, "model", None),
                "cost_usd_equiv": getattr(res, "cost_usd", None),
                "steps": steps,
                "cambiados": out.changed,
                "datos": task["datos"],
                "nota": out.note,
            },
            ensure_ascii=False,
        )
    )
    assert task["id"] == "p1" and task["revision"] == 1
    assert task["datos"]["fecha"] == "2026-11-26"  # hoy es jueves 19: el jueves que viene
    assert task["datos"]["proyecto_id"] == PROJECT_REGULAI
    assert task["datos"]["nombre"] == "Preparar demo"
    assert {"fecha", "proyecto_id"} <= set(out.changed)
    assert {m: len(rows) for m, rows in fake_odoo.data.items()} == writes_before  # sin escrituras
