"""Revision externa: identidad de las propuestas, aprobacion ligada a una version concreta e
idempotencia de las escrituras (P1-5, P1-6, P1-7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from micnaga.brain import acta as acta_io
from micnaga.brain import plan
from micnaga.brain.executor import Executor
from micnaga.brain.google import analysis, apply
from micnaga.brain.odoo import client_from_config
from micnaga.config import Config
from micnaga.store.db import open_database
from tests.brain import canned
from tests.brain.conftest import FakeClaude, odoo_config
from tests.brain.fake_google import FakeGoogle, FakeGoogleRunner
from tests.brain.fake_odoo import FakeOdoo
from tests.brain.test_secretary import run


def _odoo(cfg: Config) -> Any:
    def get() -> Any:
        c = client_from_config(
            cfg.secretaria.odoo_url, cfg.secretaria.odoo_db, cfg.secretaria.odoo_login
        )
        c.authenticate()
        return c

    return get


def _event(uid: str | None, title: str) -> dict[str, Any]:
    p: dict[str, Any] = {
        "id": "p1",
        "grupo": "google",
        "tipo": "evento_google",
        "estado": "pendiente",
        "datos": {
            "titulo": title,
            "inicio": "2026-11-24T10:00",
            "duracion_min": 30,
            "asistentes": [],
            "descripcion": "",
            "huecos": [],
            "calendario": "primary",
        },
    }
    if uid:
        p["uid"] = uid
    analysis.refresh(p)
    return p


def _applier(tmp_path: Path, google: FakeGoogle, **kw: Any) -> apply.GoogleApplier:
    folder = tmp_path / "2026-11-19_1030_reunion"
    folder.mkdir(exist_ok=True)
    return apply.GoogleApplier(
        folder, runner=FakeGoogleRunner(google, **kw), db_factory=open_database
    )


# ============================================================================== P1-7


def test_two_generations_with_the_same_label_never_share_a_calendar_result(
    tmp_path: Path,
) -> None:
    """Dos actas de la misma reunion, cada una con su ``p1``: el segundo evento se crea de
    verdad y su «Deshacer» borra el suyo, nunca el de la primera generacion."""
    google = FakeGoogle()
    first = _event(plan.new_uid(), "Generacion 1")
    first["deshacer"] = _applier(tmp_path, google).apply(first, {})
    second = _event(plan.new_uid(), "Generacion 2")
    second["deshacer"] = _applier(tmp_path, google).apply(second, {})
    assert len(google.events) == 2
    assert first["deshacer"]["event_id"] != second["deshacer"]["event_id"]
    _applier(tmp_path, google).undo(second)
    assert list(google.events) == [first["deshacer"]["event_id"]]


def test_uids_are_unique_and_labels_can_continue() -> None:
    props = plan.number([{"tipo": "crear_gasto"}, {"tipo": "crear_gasto"}])
    assert props[0]["uid"] != props[1]["uid"] and props[0]["id"] == "p1"
    again = plan.number([{"tipo": "crear_gasto"}])
    assert again[0]["id"] == "p1" and again[0]["uid"] not in {p["uid"] for p in props}
    later = plan.number([{"tipo": "x"}], start=7)
    assert later[0]["id"] == "p8"


def test_regenerating_the_acta_keeps_what_was_applied_and_never_reuses_labels(
    fake_claude: FakeClaude, fake_odoo: FakeOdoo, meeting: Path
) -> None:
    fake_claude({"structured": canned.structured(), "calls": canned.CALLS[:1]})
    cfg = odoo_config(Config(), fake_odoo)
    res, _ = run(cfg, meeting)
    folder = res.folder
    gen1 = res.acta["propuestas"]
    assert all(p.get("uid") for p in gen1)
    note = next(p for p in gen1 if p["tipo"] in ("nota_en_historial", "vincular_a_oportunidad"))
    applied = Executor(folder, odoo=_odoo(cfg)).apply([note["id"]])
    done = next(p for p in applied["propuestas"] if p["id"] == note["id"])
    assert done["estado"] == "aplicada"

    res2, _ = run(cfg, folder, force=True)
    gen2 = res2.acta["propuestas"]
    kept = [p for p in gen2 if p["uid"] == note["uid"]]
    assert len(kept) == 1 and kept[0]["estado"] == "aplicada"
    assert kept[0]["deshacer"] == done["deshacer"]  # su «Deshacer» sigue siendo el suyo
    new = [p for p in gen2 if p["uid"] != note["uid"]]
    old_numbers = {int(p["id"][1:]) for p in gen1}
    assert new and all(int(p["id"][1:]) > max(old_numbers) for p in new)
    assert not {p["uid"] for p in new} & {p["uid"] for p in gen1}
    ids = [p["id"] for p in gen2]
    assert len(ids) == len(set(ids))
    saved = acta_io.read_acta(res2.folder) or {}
    assert saved["ultimo_id"] >= max(int(i[1:]) for i in ids)
