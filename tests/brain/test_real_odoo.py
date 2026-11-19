"""Comprobaciones REALES contra el Odoo del usuario, solo lectura (nunca se escribe).

    ODOO_URL=... ODOO_DB=... ODOO_LOGIN=... ODOO_KEY=... MICNAGA_RUN_REAL_ODOO=1 \\
        pytest -s tests/brain/test_real_odoo.py              (+ MICNAGA_RUN_CLAUDE=1 para el ultimo)

* La clave solo se lee del entorno y se queda en memoria: el servidor MCP hijo habla con un proxy
  local de solo lectura (``tests/brain/odoo_proxy.py``) con una clave ficticia.
* Doble cerrojo de escritura: ``OdooClient(read_only=True)`` + el proxy rechaza todo lo que no sea
  lectura. Los tests comprueban que no hubo ni un intento de escritura.
* Las fechas son las del dia de la verificacion (hoy = 2026-11-19); con otros datos los
  asertos de dias concretos se ajustan solos (se calculan, no se escriben a mano), salvo el
  de la verificacion inicial, que solo corre si hoy es ese dia.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from micnaga.brain import capabilities, mcp, timesheets
from micnaga.brain.odoo import OdooClient, OdooConfig
from micnaga.config import Config
from micnaga.types import EnginesInfo, MeetingInfo, Segment, Speaker, Transcript, Word
from tests.brain.conftest import write_meeting
from tests.brain.odoo_proxy import DUMMY_KEY, ReadOnlyOdooProxy

ENV = ("ODOO_URL", "ODOO_DB", "ODOO_LOGIN", "ODOO_KEY")
pytestmark = [
    pytest.mark.skipif(
        not (os.environ.get("MICNAGA_RUN_REAL_ODOO") and all(os.environ.get(k) for k in ENV)),
        reason="set MICNAGA_RUN_REAL_ODOO=1 and ODOO_URL/ODOO_DB/ODOO_LOGIN/ODOO_KEY",
    ),
    pytest.mark.timeout(600),
]
TODAY = date(2026, 11, 19)


def real_client() -> OdooClient:
    c = OdooClient(OdooConfig(*(os.environ[k] for k in ENV)), read_only=True, timeout_s=30)
    c.authenticate()
    return c


def test_capability_discovery_on_the_real_odoo() -> None:
    c = real_client()
    prof = capabilities.discover(c)
    acc = prof["access"]
    print("\nREAL-ODOO profile:", json.dumps({"modules": prof["modules"], "access": acc}))
    print("REAL-ODOO summary:", capabilities.summary_text(prof))
    assert prof["odoo_version"].startswith("19")
    # lo verificado a mano el 2026-11-19 para la cuenta de empleado
    assert acc["account.analytic.line"] == {"read": True, "create": True, "write": True}
    assert acc["crm.lead"]["read"] is False
    assert acc["res.partner"] == {"read": True, "create": False, "write": False}
    assert capabilities.note_targets(prof) == ["project.task"]
    allowed = capabilities.actions_for(prof, mcp_types())
    assert "registrar_horas" in allowed and "crear_tarea" in allowed
    assert "crear_contacto" not in allowed and "vincular_a_oportunidad" not in allowed
    tools = capabilities.tools_for(prof, mcp.EVERY_ODOO_TOOL)
    assert "buscar_oportunidades" not in tools and "mis_partes_de_horas" in tools


def mcp_types() -> tuple[str, ...]:
    from micnaga.brain.schema import PROPOSAL_TYPES

    return PROPOSAL_TYPES


def test_real_read_tools_and_missing_days() -> None:
    c = real_client()
    tools = mcp.Tools(c, allowed=list(mcp.EVERY_ODOO_TOOL), today=TODAY)
    out = tools.call("mis_partes_de_horas", {"desde": "2026-10-20", "hasta": "2026-11-19"})
    missing = [d["fecha"] for d in out["faltan"]]
    print("\nREAL-ODOO faltan:", missing, "posibles libres:", out["posibles_libres"])
    print("REAL-ODOO patron:", json.dumps(out["patron"], ensure_ascii=False))
    expected = [
        d.isoformat()
        for d in timesheets.daterange(date(2026, 11, 5), TODAY)
        if timesheets.is_working_day(d)
    ]
    if out["ultimo_dia_con_horas"] == "2026-11-04":  # el estado verificado hoy
        assert missing == expected and len(expected) == 11
        assert "2026-11-02" in out["posibles_libres"]  # lunes sin horas entre dias con horas
    assert out["patron"] and len(json.dumps(out)) < mcp.MAX_RESULT_CHARS
    projects = tools.call("proyectos", {})
    tasks = tools.call("tareas", {"mias": False})
    cal = tools.call("calendario", {"desde": "2026-10-20", "hasta": "2026-11-19"})
    print(f"REAL-ODOO proyectos={len(projects)} tareas_abiertas={len(tasks)} eventos={len(cal)}")
    assert projects and all("id" in p for p in projects)


BUSINESS_FICHAR = [
    ("me", "Hola, buenos días. ¿Repasamos cómo va la formación de los funcionarios?"),
    ("S1", "Sí. Las sesiones de la mañana van bien, pero piden más ejercicios prácticos."),
    ("me", "Vale. Yo preparo un cuaderno de ejercicios prácticos para el viernes."),
    ("S1", "Perfecto. Y el informe de asistencia lo necesitan antes de fin de mes."),
    ("me", "Eso también lo hago yo, el informe de asistencia, para el día veintisiete."),
    ("S1", "¿Nos vemos el martes veinticuatro a las diez para revisar los materiales?"),
    ("me", "Sí, el martes a las diez, una hora."),
    ("S1", "Genial. Oye, ¿tienes los partes de horas al día?"),
    (
        "me",
        "Qué va. Tengo que fichar los días hasta hoy, que llevo desde el día cuatro sin "
        "apuntar nada.",
    ),
    (
        "S1",
        "Pues apúntalo como siempre, con tu horario de la formación y la hora del otro proyecto.",
    ),
    ("me", "Sí, lo dejo hecho hoy. Gracias."),
]


def fichar_transcript() -> Transcript:
    segs = []
    t = 2.0
    for spk, text in BUSINESS_FICHAR:
        words = []
        wt = t
        for w in text.split():
            words.append(Word(w, round(wt, 2), round(wt + 0.3, 2), 0.95))
            wt += 0.35
        segs.append(Segment(round(t, 2), round(wt, 2), spk, text, words))
        t = wt + 1.5
    return Transcript(
        meeting=MeetingInfo(title="Reunión", start="2026-11-19T10:30:00+01:00", duration_s=t),
        speakers=[Speaker("me", "Yo"), Speaker("S1", "Hablante 1")],
        markers=[],
        segments=segs,
        engines=EnginesInfo(asr="parakeet"),
    )


@pytest.mark.claude
def test_real_claude_proposes_the_missing_timesheets(secrets_file: Path) -> None:
    from micnaga.brain import secrets
    from micnaga.brain.secretary import Secretary

    url, db, login, key = (os.environ[k] for k in ENV)
    proxy = ReadOnlyOdooProxy(url, db, login, key).start()
    secrets.put(secrets.ODOO_KEY, DUMMY_KEY)  # solo la clave ficticia del proxy toca disco
    try:
        cfg = Config()
        cfg.secretaria.odoo_url, cfg.secretaria.odoo_db = proxy.url, db
        cfg.secretaria.odoo_login = login
        folder = write_meeting(fichar_transcript(), "2026-11-19_1030_reunion")
        seen: list[dict[str, Any]] = []
        t0 = time.monotonic()
        res = Secretary(cfg).analyze(folder, on_status=seen.append, today=TODAY.isoformat())
        wall = time.monotonic() - t0
    finally:
        proxy.stop()
    assert proxy.blocked == [], proxy.blocked  # ni un intento de escritura
    assert res.result is not None and res.result.mcp_servers.get("micnaga") == "connected"
    props = [p for p in res.acta["propuestas"] if p["grupo"] != "correcciones"]
    hours = [p for p in props if p["tipo"] == "registrar_horas"]
    per_day: dict[str, list[str]] = defaultdict(list)
    for p in hours:
        d = p["datos"]
        per_day[d["fecha"]].append(f"{d['proyecto']} · {d['horas']:g} h · {d['descripcion']}")
    report = {
        "wall_s": round(wall, 1),
        "claude_s": res.result.duration_s,
        "turns": res.result.num_turns,
        "tools": [c.tool for c in res.result.tool_calls],
        "odoo": res.acta.get("odoo"),
        "otras": [
            (p["tipo"], p["titulo"], p["checked"], p.get("datos"))
            for p in props
            if p["tipo"] != "registrar_horas"
        ],
        "horas_por_dia": {k: per_day[k] for k in sorted(per_day)},
        "total_por_dia": {
            k: sum(p["datos"]["horas"] for p in hours if p["datos"]["fecha"] == k)
            for k in sorted(per_day)
        },
    }
    print("\nREAL-CLAUDE-FICHAR " + json.dumps(report, ensure_ascii=False, indent=1))
    assert any(c.tool == "mis_partes_de_horas" for c in res.result.tool_calls)
    expected = {
        d.isoformat()
        for d in timesheets.daterange(date(2026, 11, 5), TODAY)
        if timesheets.is_working_day(d)
    }
    assert hours, props
    assert set(per_day) <= expected | {TODAY.isoformat()}
    assert len(set(per_day) & expected) >= len(expected) - 1  # todos (o casi) los que faltan
    assert not any(p["checked"] for p in hours)  # relleno por patron: nada marcado
    assert all(p["datos"]["horas"] <= timesheets.MAX_LINE_H for p in hours)
    assert any(p["tipo"] == "crear_evento" for p in props), props
