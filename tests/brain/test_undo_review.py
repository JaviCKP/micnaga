"""«Deshacer» seguro (revision externa): conflictos en Odoo, deshacer a medias y lotes de horas."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from micnaga.brain import acta as acta_io
from micnaga.brain import horas
from micnaga.brain.executor import Executor
from micnaga.brain.odoo import OdooError
from tests.brain.fake_google import FakeGoogleRunner
from tests.brain.fake_odoo import PROJECT_ATLAS, PROJECT_TRAINING, FakeOdoo
from tests.brain.test_google import MiniOdoo, applier, event_proposal
from tests.brain.test_odoo_business import client

TODAY = date(2026, 11, 19)


# ============================================================================== P1-8 conflictos


def _lead_acta(folder: Path, datos: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    p = {
        "id": "p1",
        "grupo": "crm",
        "tipo": "actualizar_oportunidad",
        "estado": "pendiente",
        "datos": {"id": 205, **datos},
    }
    acta_io.write_acta(folder, {"titulo": "x", "propuestas": [p]})


def _prop(acta: dict[str, Any]) -> dict[str, Any]:
    p: dict[str, Any] = acta["propuestas"][0]
    return p


def _lead(fake_odoo: FakeOdoo) -> dict[str, Any]:
    lead: dict[str, Any] = fake_odoo.data["crm.lead"][0]
    return lead


def _amount_applied(fake_odoo: FakeOdoo, tmp_path: Path) -> Executor:
    _lead(fake_odoo)["expected_revenue"] = 100.0
    folder = tmp_path / "m"
    _lead_acta(folder, {"importe": 250, "anterior": {"etapa_id": 2, "importe": 100.0}})
    ex = Executor(folder, odoo=lambda: client(fake_odoo))
    assert _prop(ex.apply(["p1"]))["estado"] == "aplicada"
    assert _lead(fake_odoo)["expected_revenue"] == 250
    return ex


def test_undo_restores_when_nobody_changed_it(fake_odoo: FakeOdoo, tmp_path: Path) -> None:
    ex = _amount_applied(fake_odoo, tmp_path)
    p = _prop(ex.undo(["p1"]))
    assert p["estado"] == "deshecha" and not p.get("deshacer_estado")
    assert _lead(fake_odoo)["expected_revenue"] == 100.0


def test_undo_never_overwrites_a_later_change(fake_odoo: FakeOdoo, tmp_path: Path) -> None:
    ex = _amount_applied(fake_odoo, tmp_path)
    _lead(fake_odoo)["expected_revenue"] = 400.0  # alguien lo cambio despues
    writes = len(fake_odoo.write_calls)
    p = _prop(ex.undo(["p1"]))
    assert _lead(fake_odoo)["expected_revenue"] == 400.0
    assert len(fake_odoo.write_calls) == writes  # no se escribe nada
    assert p["estado"] == "aplicada" and p["deshacer_estado"] == "conflicto"
    assert p["error"] == "undo_conflict"
    assert p["conflicto"] == [
        {"campo": "expected_revenue", "actual": 400.0, "escrito": 250, "original": 100.0}
    ]
    # «Deshacer todo» sin resolver sigue sin pisar
    assert _prop(ex.undo(["p1"]))["deshacer_estado"] == "conflicto"
    assert _lead(fake_odoo)["expected_revenue"] == 400.0
    # «Omitir»: queda lo de ahora y el deshacer se cierra
    p = _prop(ex.undo(["p1"], resolve="omitir"))
    assert p["estado"] == "deshecha" and not p.get("deshacer_estado") and not p.get("conflicto")
    assert _lead(fake_odoo)["expected_revenue"] == 400.0


def test_undo_conflict_can_be_forced(fake_odoo: FakeOdoo, tmp_path: Path) -> None:
    ex = _amount_applied(fake_odoo, tmp_path)
    _lead(fake_odoo)["expected_revenue"] = 400.0
    assert _prop(ex.undo(["p1"]))["deshacer_estado"] == "conflicto"
    p = _prop(ex.undo(["p1"], resolve="forzar"))
    assert p["estado"] == "deshecha" and _lead(fake_odoo)["expected_revenue"] == 100.0


def test_stage_and_deadline_reverts_are_checked_too(fake_odoo: FakeOdoo, tmp_path: Path) -> None:
    folder = tmp_path / "m"
    _lead_acta(
        folder,
        {
            "etapa_id": 3,
            "fecha_cierre": "2026-12-15",
            "anterior": {"etapa_id": 2, "importe": 5400.0},
        },
    )
    ex = Executor(folder, odoo=lambda: client(fake_odoo))
    ex.apply(["p1"])
    assert (_lead(fake_odoo)["stage_id"], _lead(fake_odoo)["date_deadline"]) == (3, "2026-12-15")
    _lead(fake_odoo)["stage_id"] = 1  # la etapa la movio otro; la fecha sigue igual
    p = _prop(ex.undo(["p1"]))
    assert p["deshacer_estado"] == "conflicto"
    assert [c["campo"] for c in p["conflicto"]] == ["stage_id"]
    assert p["conflicto"][0]["actual"] == 1 and p["conflicto"][0]["escrito"] == 3
    assert (_lead(fake_odoo)["stage_id"], _lead(fake_odoo)["date_deadline"]) == (1, "2026-12-15")
    _lead(fake_odoo)["stage_id"] = 3  # vuelve a lo que escribimos: ya se puede deshacer
    p = _prop(ex.undo(["p1"]))
    assert p["estado"] == "deshecha"
    assert (_lead(fake_odoo)["stage_id"], _lead(fake_odoo)["date_deadline"]) == (2, False)


def test_old_actas_without_written_values_are_checked_from_the_data(
    fake_odoo: FakeOdoo, tmp_path: Path
) -> None:
    ex = _amount_applied(fake_odoo, tmp_path)
    acta = acta_io.read_acta(ex.folder)
    assert acta is not None
    _prop(acta)["deshacer"].pop("escrito")  # acta aplicada con una version anterior
    acta_io.write_acta(ex.folder, acta)
    _lead(fake_odoo)["expected_revenue"] = 400.0
    assert _prop(ex.undo(["p1"]))["deshacer_estado"] == "conflicto"
    assert _lead(fake_odoo)["expected_revenue"] == 400.0


# ============================================================================== P2-1 a medias


class FlakyOdoo(MiniOdoo):
    """Espejo de Odoo cuyo ``unlink`` falla una vez (fallo transitorio)."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_unlink = 1

    def call(
        self, model: str, method: str, args: list[Any], kwargs: dict[str, Any] | None = None
    ) -> Any:
        if method == "unlink" and self.fail_unlink:
            self.fail_unlink -= 1
            raise OdooError("timeout", "offline")
        return super().call(model, method, args, kwargs)


def _google_acta(tmp_path: Path, odoo: Any, runner: FakeGoogleRunner) -> tuple[Executor, Any]:
    ap = applier(tmp_path, runner, odoo)
    p = event_proposal()
    p["deshacer"] = ap.apply(p, {})
    p["estado"] = "aplicada"
    acta_io.write_acta(ap.folder, {"titulo": "x", "propuestas": [p]})
    return Executor(ap.folder, google=lambda: ap), ap


def _deletes(runner: FakeGoogleRunner) -> int:
    return sum(1 for t, _ in runner.google.calls if t.endswith("delete_event"))


def test_half_undone_event_retries_only_the_failed_step(tmp_path: Path) -> None:
    odoo = FlakyOdoo()
    runner = FakeGoogleRunner()
    ex, _ = _google_acta(tmp_path, odoo, runner)
    assert odoo.events and runner.google.events

    p = _prop(ex.undo(["p1"]))
    assert runner.google.events == {} and odoo.events  # Google borrado, Odoo no
    assert p["estado"] == "aplicada" and p["deshacer_estado"] == "a_medias"
    assert p["deshacer"]["pasos"] == {"google": "hecho", "odoo": "fallido"}
    persisted = acta_io.read_acta(ex.folder)
    assert persisted is not None and _prop(persisted)["deshacer"]["pasos"]["google"] == "hecho"

    p = _prop(ex.undo(["p1"]))  # reintento: solo el paso fallido
    assert p["estado"] == "deshecha" and not p.get("deshacer_estado") and p["error"] is None
    assert p["deshacer"]["pasos"] == {"google": "hecho", "odoo": "hecho"}
    assert odoo.events == {} and _deletes(runner) == 1


def test_a_mirror_without_odoo_configured_is_never_reported_undone(tmp_path: Path) -> None:
    odoo = MiniOdoo()
    runner = FakeGoogleRunner()
    ex, ap = _google_acta(tmp_path, odoo, runner)
    ap._odoo = None  # Odoo quitado de Ajustes despues de aplicar: la copia sigue alli
    p = _prop(ex.undo(["p1"]))
    assert p["estado"] == "aplicada" and p["deshacer_estado"] == "a_medias"
    assert p["deshacer"]["pasos"] == {"google": "hecho", "odoo": "fallido"}
    assert p["error"] == "incomplete" and odoo.events


def test_an_event_already_gone_counts_as_undone(tmp_path: Path) -> None:
    odoo = MiniOdoo()
    runner = FakeGoogleRunner()
    ex, _ = _google_acta(tmp_path, odoo, runner)
    runner.google.events.clear()  # el usuario lo borro a mano en Calendar
    odoo.events.clear()  # y su copia en Odoo
    p = _prop(ex.undo(["p1"]))
    assert p["estado"] == "deshecha", p
    assert p["deshacer"]["pasos"] == {"google": "hecho", "odoo": "hecho"}


def test_a_created_record_already_gone_counts_as_undone(
    fake_odoo: FakeOdoo, tmp_path: Path
) -> None:
    folder = tmp_path / "m"
    folder.mkdir()
    p = {
        "id": "p1",
        "grupo": "negocio",
        "tipo": "crear_tarea",
        "estado": "aplicada",
        "datos": {},
        "deshacer": {"modelo": "project.task", "creado": 987654},  # ya no existe
    }
    acta_io.write_acta(folder, {"titulo": "x", "propuestas": [p]})

    class Strict:
        """Como Odoo real: ``unlink`` de un id que no existe falla."""

        def __init__(self) -> None:
            self.c = client(fake_odoo)

        def unlink(self, model: str, ids: list[int]) -> bool:
            raise OdooError("MissingError: Record does not exist", "failed")

        def call(self, model: str, method: str, *a: Any) -> Any:
            return self.c.call(model, method, *a)

    out = _prop(Executor(folder, odoo=Strict).undo(["p1"]))
    assert out["estado"] == "deshecha", out


# ============================================================================== P2-2 lotes de horas


def _line(key: str, project: int, desc: str) -> dict[str, Any]:
    return {
        "key": key,
        "fecha": "2026-11-05",
        "proyecto_id": project,
        "tarea_id": None,
        "horas": 1.0,
        "descripcion": desc,
    }


def test_half_applied_batch_is_saved_and_can_be_retried_or_undone(
    fake_odoo: FakeOdoo, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = client(fake_odoo)
    real_read_project = c.read_project
    seen_batches: list[list[dict[str, Any]]] = []
    real_create = c.create

    def create(model: str, values: dict[str, Any], **kw: Any) -> int:
        seen_batches.append(list(horas.load_state()["lotes"]))  # el lote existe ANTES de crear
        return real_create(model, values, **kw)

    def read_project(pid: int) -> Any:
        if pid == PROJECT_ATLAS:
            raise OdooError("connection reset", "offline")
        return real_read_project(pid)

    monkeypatch.setattr(c, "create", create)
    monkeypatch.setattr(c, "read_project", read_project)
    lines = [_line("a", PROJECT_TRAINING, "Formacion"), _line("b", PROJECT_ATLAS, "Atlas")]
    res = horas.apply(c, lines, today=TODAY)
    assert res["creadas"] == 1 and res["total"] == 2 and res["parcial"] is True
    assert [r["ok"] for r in res["resultados"]] == [True, False]
    assert res["resultados"][1]["error"] == "odoo_offline"
    assert seen_batches and seen_batches[0][-1]["id"] == res["lote"]
    batch = next(b for b in horas.load_state()["lotes"] if b["id"] == res["lote"])
    assert batch["estado"] == "parcial" and batch["total"] == 2
    assert [ln["odoo_id"] for ln in batch["lineas"]] == [res["resultados"][0]["id"]]

    # reintentar lo que falto: mismo lote, sin duplicar lo ya creado
    monkeypatch.setattr(c, "read_project", real_read_project)
    again = horas.apply(c, lines, today=TODAY, batch=res["lote"])
    assert again["lote"] == res["lote"] and again["creadas"] == 1 and again["parcial"] is False
    assert again["aplicadas"] == 2 and again["total"] == 2
    batch = next(b for b in horas.load_state()["lotes"] if b["id"] == res["lote"])
    assert batch["estado"] == "completo" and len(batch["lineas"]) == 2
    assert (
        len([ln for ln in fake_odoo.data["account.analytic.line"] if ln["date"] == "2026-11-05"])
        == 2
    )

    assert horas.undo(c, res["lote"]) == {"ok": True, "deshechas": 2}
    assert not [ln for ln in fake_odoo.data["account.analytic.line"] if ln["date"] == "2026-11-05"]


def test_a_partial_batch_can_be_undone_directly(
    fake_odoo: FakeOdoo, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = client(fake_odoo)
    real = c.create
    calls = {"n": 0}

    def create(model: str, values: dict[str, Any], **kw: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OdooError("boom", "failed")
        return real(model, values, **kw)

    monkeypatch.setattr(c, "create", create)
    lines = [_line("a", PROJECT_TRAINING, "Uno"), _line("b", PROJECT_TRAINING, "Dos")]
    res = horas.apply(c, lines, today=TODAY)
    assert res["creadas"] == 1 and res["parcial"] is True and res["lote"]
    assert horas.undo(c, res["lote"]) == {"ok": True, "deshechas": 1}
    assert not [ln for ln in fake_odoo.data["account.analytic.line"] if ln["date"] == "2026-11-05"]


# ============================================================================== servicio


def test_service_passes_the_batch_and_the_conflict_resolution(
    fake_odoo: FakeOdoo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from micnaga.brain.service import BrainService
    from micnaga.config import Config
    from tests.brain.conftest import odoo_config

    cfg = odoo_config(Config(), fake_odoo)
    cfg.secretaria.enabled = True
    svc = BrainService(lambda: cfg)
    seen: dict[str, Any] = {}

    def fake_apply(odoo: Any, lines: Any, *, today: date, batch: str | None = None) -> Any:
        seen["batch"] = batch
        return {"resultados": [], "lote": batch, "creadas": 0, "parcial": False}

    monkeypatch.setattr(horas, "apply", fake_apply)
    assert svc.api("horas_apply", {"lineas": [{"key": "a"}], "lote": "abc"})["ok"]
    assert seen["batch"] == "abc"

    class FakeExecutor:
        def undo(self, ids: list[str], *, resolve: str | None = None) -> dict[str, Any]:
            seen["undo"] = (ids, resolve)
            return {"propuestas": []}

    monkeypatch.setattr(svc, "_folder_path", lambda args: tmp_path)
    monkeypatch.setattr(svc, "_executor", lambda path: FakeExecutor())
    assert svc.api("undo", {"folder": "x", "ids": ["p1"], "resolver": "forzar"})["ok"]
    assert seen["undo"] == (["p1"], "forzar")
