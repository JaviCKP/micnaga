"""«Conocer tu Odoo» contra el Odoo falso: solo lectura, por permisos, privacidad y diferencias."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from micnaga.brain import learn, odoo_learn
from micnaga.brain.odoo import OdooClient, OdooConfig
from micnaga.brain.service import BrainService
from micnaga.config import Config
from tests.brain.conftest import odoo_config
from tests.brain.fake_odoo import DB, KEY, LOGIN, FakeOdoo

NOW = datetime(2026, 11, 20, 10, 0)
CRM_MODELS = ("crm.lead", "crm.stage", "crm.team", "crm.lost.reason", "sale.order")


def client(srv: FakeOdoo) -> OdooClient:
    return OdooClient(OdooConfig(srv.url, DB, LOGIN, KEY), timeout_s=5)


def with_crm(srv: FakeOdoo) -> FakeOdoo:
    """El Odoo del jefe: CRM completo, con datos que NO pueden acabar en el contexto."""
    d = srv.data
    d["crm.team"] = [
        {"id": 1, "name": "Ventas", "sequence": 1},
        {"id": 2, "name": "Web", "sequence": 2},
    ]
    d["crm.lost.reason"] = [{"id": 1, "name": "Precio"}, {"id": 2, "name": "Sin respuesta"}]
    d["project.task.type"] = [
        {"id": 1, "name": "Por hacer", "sequence": 1},
        {"id": 2, "name": "En curso", "sequence": 2},
    ]
    d["product.category"] = [
        {"id": 1, "complete_name": "All"},
        {"id": 2, "complete_name": "Servicios / Formación"},
    ]
    d["hr.employee"] = [{"id": 70, "name": "Javier", "user_id": 7, "job_title": "Comercial"}]
    d["res.partner"] += [
        {"id": 90, "name": "Hotel Sol", "is_company": True, "email": "h@sol.es", "phone": "1"},
        {"id": 91, "name": "ana@privado.es", "is_company": False},
        {"id": 92, "name": "Bodegas Ruiz", "is_company": True},
    ]
    leads = [
        (41, "Dental Triana · CRM + recordatorios"),
        (90, "Hotel Sol · Web nueva 12.000 €"),
        (90, "Hotel Sol · Mantenimiento"),
        (92, "Bodegas Ruiz · Tienda online"),
        (91, "ana@privado.es · consulta"),
    ]
    d["crm.lead"] = [
        {
            "id": 400 + i,
            "name": name,
            "partner_id": pid,
            "stage_id": 2,
            "expected_revenue": 9999.0,
            "type": "opportunity",
            "write_date": "2026-11-1" + str(i) + " 09:00:00",
        }
        for i, (pid, name) in enumerate(leads)
    ]
    return srv


def read_calls(srv: FakeOdoo, models: tuple[str, ...]) -> list[tuple[str, str]]:
    return [c for c in srv.calls if c[0] in models and c[1] in ("search_read", "read")]


def test_scan_on_a_full_crm_writes_a_compact_private_context(fake_odoo: FakeOdoo) -> None:
    with_crm(fake_odoo)
    c = client(fake_odoo)  # aunque llegue con escritura, el escaneo lo pone en solo lectura
    state = odoo_learn.refresh(c, now=NOW)
    assert c.read_only is True
    assert fake_odoo.write_calls == []
    snap = state["snapshot"]
    assert snap["etapas"] == ["Nuevo", "Propuesta", "Negociación", "Ganado"]
    assert snap["equipos"] == ["Ventas", "Web"]
    assert snap["motivos_perdida"] == ["Precio", "Sin respuesta"]
    assert snap["clientes"][0] == "Hotel Sol"  # el de mas actividad reciente
    assert "ana@privado.es" not in snap["clientes"]  # un nombre que es un correo no entra
    assert {p["nombre"] for p in snap["proyectos"]} >= {"Plataforma Atlas", "Interno"}
    assert snap["etapas_tareas"] == ["Por hacer", "En curso"]
    assert snap["categorias"] == ["Servicios / Formación"]
    assert snap["usuario"] == {"nombre": "Javier", "cargo": "Comercial"}
    assert "«cliente · asunto»" in snap["nombres_oportunidad"]["patron"]
    assert state["counts"] == {"etapas": 4, "clientes": 3, "proyectos": 3}

    text = odoo_learn.context_path(str(state["key"])).read_text(encoding="utf-8")
    assert len(text.splitlines()) <= odoo_learn.MAX_LINES
    assert "Nuevo → Propuesta → Negociación → Ganado" in text
    assert "Plataforma Atlas (Clínica Dental Triana)" in text
    assert "Horas" not in text or "Formación Ayuntamiento" in text
    for bad in ("@", "€", "12.000", "9999", "954000000"):
        assert bad not in text, bad
    # la Secretaria lo lee en cada pasada
    assert "Equipos de venta: Ventas, Web" in learn.load_context(str(state["key"]))
    assert "Equipos de venta" not in learn.load_context()

    # primera vez = base: solo lineas resumen (fuente odoo), nada de «nuevo»
    items = learn.list_learned()
    assert {i.source for i in items} == {"odoo"}
    facts = [i.fact for i in items]
    assert any(f.startswith(odoo_learn.STAGES_PREFIX) for f in facts)
    assert any(f.startswith(odoo_learn.BASELINE_PREFIX) for f in facts)
    assert not any("nuevo en Odoo" in f or "nueva" in f for f in facts)


def test_without_crm_access_the_crm_is_never_read(fake_odoo: FakeOdoo) -> None:
    """La cuenta de empleado: sin permiso de CRM no se pide ni una lectura de esos modelos."""
    with_crm(fake_odoo)
    for m in CRM_MODELS:
        fake_odoo.access[m] = set()
    state = odoo_learn.refresh(client(fake_odoo), now=NOW)
    assert read_calls(fake_odoo, CRM_MODELS) == []
    snap = state["snapshot"]
    assert snap["etapas"] is None and snap["nombres_oportunidad"] is None
    assert snap["crm"] is False
    assert snap["clientes"] == ["Clínica Dental Triana"]  # de los proyectos
    text = odoo_learn.context_path(str(state["key"])).read_text(encoding="utf-8")
    assert "### CRM" not in text and "Proyectos activos" in text


def test_a_failing_section_is_unknown_not_empty(
    fake_odoo: FakeOdoo, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_crm(fake_odoo)
    odoo_learn.refresh(client(fake_odoo), now=NOW)
    fake_odoo.denied.add("crm.stage")  # un fallo de lectura (AccessError) en esa seccion
    later = NOW + timedelta(days=8)
    state = odoo_learn.refresh(client(fake_odoo), now=later)
    assert state["snapshot"]["etapas"] is None
    # lo que ya se conocia sigue: si vuelve, no se «aprende» de nuevo
    assert state["conocido"]["etapas"] == ["Nuevo", "Propuesta", "Negociación", "Ganado"]
    fake_odoo.denied.discard("crm.stage")
    state = odoo_learn.refresh(client(fake_odoo), now=later + timedelta(days=8))
    assert state["nuevos"] == []


def test_the_diff_learns_new_things_and_drops_removed_ones(fake_odoo: FakeOdoo) -> None:
    with_crm(fake_odoo)
    odoo_learn.refresh(client(fake_odoo), now=NOW)
    d = fake_odoo.data
    d["crm.stage"].append({"id": 9, "name": "Demo", "sequence": 1})
    for i, name in ((20, "Web A"), (21, "Web B")):  # nombres cortos parecidos: los dos cuentan
        d["project.project"].append(
            {"id": i, "name": name, "active": True, "partner_id": 92, "allow_timesheets": True}
        )
    d["project.project"] = [p for p in d["project.project"] if p["name"] != "Interno"]
    d["res.partner"].append({"id": 93, "name": "Talleres Mora", "is_company": True})
    d["crm.lead"].append(
        {
            "id": 499,
            "name": "Talleres Mora · ERP",
            "partner_id": 93,
            "type": "opportunity",
            "write_date": "2026-11-27 09:00:00",
        }
    )
    later = NOW + timedelta(days=8)
    state = odoo_learn.refresh(client(fake_odoo), now=later)
    assert state["nuevos"] == [
        "Etapa nueva en el CRM de Odoo: Demo",
        "Web A (cliente Bodegas Ruiz): proyecto nuevo en Odoo",
        "Web B (cliente Bodegas Ruiz): proyecto nuevo en Odoo",
        "Clientes con actividad nueva en Odoo: Talleres Mora",
    ]
    items = learn.list_learned()
    for fact in state["nuevos"]:
        assert any(i.fact == fact and i.source == "odoo" and not i.review for i in items)
    stages = [i.fact for i in items if i.fact.startswith(odoo_learn.STAGES_PREFIX)]
    assert stages == [f"{odoo_learn.STAGES_PREFIX} Nuevo → Demo → Propuesta → Negociación → Ganado"]
    text = odoo_learn.context_path(str(state["key"])).read_text(encoding="utf-8")
    assert "- Interno" not in text  # lo que ya no esta sale del contexto
    assert learn.history_versions()  # con historial: se puede volver atras

    # nada nuevo -> nada que aprender (ni duplicados)
    n = len(learn.list_learned())
    state = odoo_learn.refresh(client(fake_odoo), now=later + timedelta(days=8))
    assert state["nuevos"] == [] and len(learn.list_learned()) == n


def test_privacy_filter() -> None:
    ok = odoo_learn.safe_name
    assert ok("Clínica Dental Triana") == "Clínica Dental Triana"
    assert ok([41, "Hotel Sol"]) == "Hotel Sol"
    for bad in ("ana@privado.es", "Llamar al 954 000 000", "Web 12.000 €", "x", None, 7):
        assert ok(bad) is None, bad
    assert len(ok("A" * 20 + " " + "b" * 200) or "") <= odoo_learn.MAX_NAME


def test_naming_pattern() -> None:
    titles = ["Hotel Sol · Web", "Bodegas Ruiz · Tienda", "Talleres · ERP"]
    pat = odoo_learn.naming_pattern(titles, ["Hotel Sol", "Bodegas Ruiz", "Talleres"])
    assert pat == "suelen ser «cliente · asunto» (3 de 3)"
    assert odoo_learn.naming_pattern(["a", "b"], [None, None]) is None


def test_a_recording_stops_the_scan_and_nothing_is_written(fake_odoo: FakeOdoo) -> None:
    with_crm(fake_odoo)
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(odoo_learn.ScanStopped):
        odoo_learn.refresh(client(fake_odoo), now=NOW, stop=stop)
    assert not any(odoo_learn.load_state(k) for k in _keys())
    assert learn.list_learned() == []


def _keys() -> list[str]:
    root = learn.secretaria_dir() / "odoo"
    return [p.name for p in root.iterdir()] if root.is_dir() else []


# ============================================================================== servicio


def service(fake: FakeOdoo) -> tuple[BrainService, list[Callable[[], None]]]:
    cfg = odoo_config(Config(), fake)
    cfg.secretaria.enabled = True
    svc = BrainService(lambda: cfg, odoo_factory=lambda _c: client(fake))
    pending: list[Callable[[], None]] = []

    def spawn(fn: Callable[[], None]) -> Any:
        pending.append(fn)
        return None

    svc._spawn = spawn
    svc.now = lambda: NOW
    return svc, pending


def run(pending: list[Callable[[], None]]) -> int:
    n = len(pending)
    while pending:
        pending.pop(0)()
    return n


def test_weekly_scan_never_while_recording(fake_odoo: FakeOdoo) -> None:
    svc, pending = service(with_crm(fake_odoo))
    recording = {"on": True}
    svc.set_busy(lambda: recording["on"])
    svc._odoo_learn_weekly()
    assert run(pending) == 0  # grabando: ni empieza

    recording["on"] = False
    svc._queue.put("una-reunion")  # la cola tiene trabajo: tampoco
    svc._odoo_learn_weekly()
    assert run(pending) == 0
    svc._queue.get_nowait()

    svc._odoo_learn_weekly()  # conectado y sin escaneo previo: ahora si
    assert run(pending) == 1
    st = svc.api("odoo_learn", {})
    assert st["ok"] and st["contadores"] == {"etapas": 4, "clientes": 3, "proyectos": 3}
    assert st["actualizado"].startswith("2026-11-20") and st["hay"] is True

    svc._odoo_learn_weekly()  # hecho hoy: hasta dentro de una semana nada
    assert run(pending) == 0
    svc.now = lambda: NOW + timedelta(days=8)
    svc._odoo_learn_weekly()
    assert run(pending) == 1
    assert fake_odoo.write_calls == []


def test_recording_mid_scan_pauses_it_without_failing(fake_odoo: FakeOdoo) -> None:
    svc, pending = service(with_crm(fake_odoo))
    flips = {"n": 0}

    def busy() -> bool:
        flips["n"] += 1
        return flips["n"] > 3  # empieza a grabar a mitad

    svc.set_busy(busy)
    svc._odoo_learn_weekly()
    assert run(pending) == 1
    st = svc.api("odoo_learn", {})
    assert st["actualizado"] is None and st["error"] is None
    assert svc._learn_failed_at == 0.0  # se reintenta en el siguiente tic, no en una hora


def test_refresh_and_open_from_settings(fake_odoo: FakeOdoo) -> None:
    svc, pending = service(with_crm(fake_odoo))
    svc.set_busy(lambda: True)  # «Actualizar» lo pide el usuario: va aunque se grabe
    opened: list[Path] = []
    svc._open_file = opened.append
    assert svc.api("odoo_learn_open", {})["ok"] is False
    assert svc.api("odoo_learn_refresh", {})["ok"] is True
    assert run(pending) == 1
    assert svc.api("odoo_learn_open", {})["ok"] is True
    assert opened and opened[0].name == odoo_learn.CONTEXT_FILE


def test_probar_with_a_new_connection_starts_a_scan(fake_odoo: FakeOdoo) -> None:
    svc, pending = service(with_crm(fake_odoo))
    r = svc.api("odoo_test", {})
    assert r["ok"] is True
    assert run(pending) == 1
    assert svc.api("odoo_learn", {})["contadores"]["etapas"] == 4
    assert svc.api("odoo_test", {})["ok"] is True
    assert run(pending) == 0  # ya conocida: «Probar» no vuelve a escanear
    assert fake_odoo.write_calls == []


def test_unconfigured_odoo_does_nothing() -> None:
    svc = BrainService(lambda: Config())
    svc._odoo_learn_weekly()
    assert svc._learn_thread is None
    st = svc.api("odoo_learn", {})
    assert st["ok"] and st["actualizado"] is None and st["hay"] is False
    assert svc.api("odoo_learn_refresh", {})["error"] == "secretaria.err.odoo_incomplete"


def test_bridge_allows_the_new_actions() -> None:
    from micnaga.ui import bridge

    assert {"odoo_learn", "odoo_learn_refresh", "odoo_learn_open"} <= (
        bridge.SETTINGS_BRAIN_ACTIONS
    )


def test_learned_lines_with_odoo_source_parse() -> None:
    learn.add_learnings(
        [{"hecho": "Web A (cliente X): proyecto nuevo en Odoo", "confianza": 1}],
        source="odoo",
        exact=True,
    )
    learn.add_learnings(
        [{"hecho": "Web B (cliente X): proyecto nuevo en Odoo", "confianza": 1}],
        source="odoo",
        exact=True,
    )
    items = learn.list_learned()
    assert [i.source for i in items] == ["odoo", "odoo"]
    assert re.search(r"· odoo\]", learn.learned_path().read_text(encoding="utf-8"))
