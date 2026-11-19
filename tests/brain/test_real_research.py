"""Evaluacion con el ``claude -p`` REAL: investigar primero y escribir un acta mejor.

``MICNAGA_RUN_CLAUDE=1 pytest -m claude -s tests/brain/test_real_research.py``

Todo sintetico (``research_case``): una reunion ANTERIOR con su acta (dos pendientes), el Odoo
FALSO con la tarea de las plantillas abierta y un correo FALSO (servidor MCP stub) que confirma
que llego el presupuesto. La segunda reunion dice que uno esta hecho y trae una accion nueva.
Sin Google real: aqui no se lee nada del usuario.
"""

from __future__ import annotations

import json
import sys
import time
import unicodedata
from typing import Any

import pytest

from micnaga.brain.secretary import Secretary
from micnaga.config import Config
from tests.brain import research_case as case
from tests.brain.conftest import odoo_config
from tests.brain.fake_odoo import FakeOdoo

pytestmark = [pytest.mark.claude, pytest.mark.timeout(600)]


def _n(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def _find(items: list[dict[str, Any]], word: str) -> list[dict[str, Any]]:
    return [x for x in items if word in _n(str(x.get("texto", "")))]


def test_real_research_first_then_a_better_acta(fake_odoo: FakeOdoo) -> None:
    case.seed_odoo(fake_odoo)
    _, current = case.write_library()
    cfg = odoo_config(Config(), fake_odoo)
    cfg.secretaria.mcp_servers = [case.mail_stub_spec(sys.executable)]
    seen: list[dict[str, Any]] = []
    t0 = time.monotonic()
    res = Secretary(cfg).analyze(current, on_status=seen.append, today="2026-11-19")
    wall = round(time.monotonic() - t0, 1)
    acta, r = res.acta, res.result
    assert r is not None
    report = {
        "wall_s": wall,
        "claude_s": r.duration_s,
        "turns": r.num_turns,
        "usage": {k: v for k, v in r.usage.items() if "tokens" in k},
        "cost_usd_equiv": r.cost_usd,
        "mcp": r.mcp_servers,
        "mail_tools": [t for t in r.tools if "correo" in t],
        "tools": [f"{c.server}:{c.tool}" for c in r.tool_calls],
        "status": [s.get("tool") for s in seen if s.get("fase") == "herramienta"],
        "titulo": acta.get("titulo"),
        "contexto": acta.get("contexto"),
        "resumen": acta.get("resumen"),
        "desde_la_ultima": [
            (x["estado"], x["texto"], (x.get("evidencia") or {}).get("tipo"), x.get("seg"))
            for x in acta.get("desde_la_ultima") or []
        ],
        "fuentes": [(f["tipo"], f.get("titulo")) for f in acta.get("fuentes") or []],
        "tareas": [t.get("texto") for t in acta.get("tareas") or []],
        "propuestas": [
            {
                "tipo": p["tipo"],
                "titulo": p.get("titulo"),
                "datos": {
                    k: v
                    for k, v in (p.get("datos") or {}).items()
                    if k in ("nombre", "proyecto_id", "tarea_id", "fecha", "descripcion", "id")
                },
                "checked": p.get("checked"),
                "aviso": p.get("aviso"),
            }
            for p in acta.get("propuestas") or []
        ],
    }
    print("\nREAL-RESEARCH " + json.dumps(report, ensure_ascii=False, indent=1))
    assert fake_odoo.write_calls == []
    called = {c.tool for c in r.tool_calls}
    assert "leer_acta" in called, called
    since = acta["desde_la_ultima"]
    assert any(x["acta"] == case.PRIOR for x in since), since  # cita la reunion anterior
    assert any(x["estado"] == "hecho" for x in _find(since, "presupuesto")), since
    assert any(x["estado"] == "abierto" for x in _find(since, "plantilla")), since
    assert any(f["tipo"] == "acta" and f["ref"] == case.PRIOR for f in acta["fuentes"])
    new_tasks = [p for p in acta["propuestas"] if p["tipo"] == "crear_tarea"]
    dupes = [p for p in new_tasks if "plantilla" in _n(str(p["datos"].get("nombre")))]
    assert not dupes, dupes  # la tarea ya existe en Odoo: no se duplica
