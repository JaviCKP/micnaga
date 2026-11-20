"""Revision externa: identidad de las propuestas, aprobacion ligada a una version concreta e
idempotencia de las escrituras (P1-5, P1-6, P1-7)."""

from __future__ import annotations

import xmlrpc.client
from pathlib import Path
from typing import Any

import pytest

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


# ============================================================================== P1-5


def _two_meetings(meeting: Path) -> tuple[str, str]:
    """Dos reuniones con un ``p1`` cada una (propuestas distintas)."""
    from tests.brain.conftest import write_meeting
    from tests.brain.demo import business_transcript

    other = write_meeting(business_transcript(), name="2026-11-20_0900_otra")
    a = _event(None, "Reunion A")
    b = _event(None, "Reunion B")
    plan.number([a])
    plan.number([b])
    acta_io.write_acta(meeting, {"titulo": "A", "propuestas": [a]})
    acta_io.write_acta(other, {"titulo": "B", "propuestas": [b]})
    return meeting.name, other.name


def _service(monkeypatch: pytest.MonkeyPatch, google: FakeGoogle) -> Any:
    from micnaga.brain.service import BrainService
    from tests.brain.test_google import google_config

    cfg = google_config()
    cfg.secretaria.enabled = True
    svc = BrainService(lambda: cfg)

    def make(path: Path) -> apply.GoogleApplier:
        return apply.GoogleApplier(
            path, runner=FakeGoogleRunner(google), db_factory=open_database, odoo=None
        )

    monkeypatch.setattr(svc, "_google_applier", make)
    return svc


def test_approval_from_one_meeting_never_applies_another_meetings_p1(
    meeting: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La vista de A se queda con la respuesta pendiente y el usuario ya mira B: un «Aplicar»
    con lo que vio en A (``p1`` + huella) no ejecuta el ``p1`` de B."""
    google = FakeGoogle()
    svc = _service(monkeypatch, google)
    name_a, name_b = _two_meetings(meeting)
    seen_a = svc.api("acta", {"folder": name_a})
    assert seen_a["folder"] == name_a  # cada respuesta dice de que reunion es
    item = {"id": "p1", "huella": seen_a["acta"]["propuestas"][0]["huella"]}
    r = svc.api("apply", {"folder": name_b, "items": [item]})
    assert r == {"ok": False, "error": "secretaria.err.stale", "stale": ["p1"]}
    assert google.calls == []
    # sin huella tampoco: una aprobacion siempre va ligada a una version concreta
    assert svc.api("apply", {"folder": name_b, "ids": ["p1"]})["ok"] is False
    assert google.calls == []
    # con la huella de B, si
    seen_b = svc.api("acta", {"folder": name_b})
    item = {"id": "p1", "huella": seen_b["acta"]["propuestas"][0]["huella"]}
    r = svc.api("apply", {"folder": name_b, "items": [item]})
    assert r["ok"] and r["folder"] == name_b
    assert r["acta"]["propuestas"][0]["estado"] == "aplicada"
    assert [e["summary"] for e in google.events.values()] == ["Reunion B"]


def test_a_changed_version_is_refused_and_the_served_hash_is_the_stored_one(
    meeting: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    google = FakeGoogle()
    svc = _service(monkeypatch, google)
    name_a, _ = _two_meetings(meeting)
    seen = svc.api("acta", {"folder": name_a})["acta"]["propuestas"][0]
    stored = (acta_io.read_acta(meeting) or {})["propuestas"][0]
    assert seen["huella"] == plan.fingerprint(stored) and seen["uid"] == stored["uid"]
    edited = svc.api("edit", {"folder": name_a, "id": "p1", "cambios": {"titulo": "Otra"}})
    assert edited["ok"] and edited["folder"] == name_a
    r = svc.api("apply", {"folder": name_a, "items": [{"id": "p1", "huella": seen["huella"]}]})
    assert r["error"] == "secretaria.err.stale" and google.calls == []


def test_the_view_ignores_responses_for_another_meeting() -> None:
    """Cada ``await`` de la revision comprueba que la reunion sigue siendo la misma antes de
    tocar el estado; ``apply`` manda la huella de lo que se ve."""
    js = (Path(__file__).parents[2] / "micnaga" / "ui" / "web" / "secretaria.js").read_text(
        encoding="utf-8"
    )
    for fn in ("edit", "refine", "undoRefine", "refinePlan", "undoLast", "act", "analyze"):
        start = js.index(f"async function {fn}(")
        body = js[start : js.index("\n  }\n", start)]
        assert "const folder = S.folder" in body, fn
        assert "stale(r, folder)" in body, fn
    assert "huella: p.huella" in js


# ============================================================================== P1-6


def _lossy(client: Any, method: str, *, executes: bool) -> Any:
    """La llamada llega (o no) a Odoo, pero la respuesta se pierde (tiempo agotado)."""
    from micnaga.brain.odoo import OdooError

    real = getattr(client, method)

    def broken(*a: Any, **kw: Any) -> Any:
        if executes:
            real(*a, **kw)
        try:
            raise TimeoutError("read timed out")
        except TimeoutError as exc:
            raise OdooError("Odoo is not responding", "offline") from exc

    setattr(client, method, broken)
    return client


def _odoo_item(meeting: Path, p: dict[str, Any]) -> None:
    plan.number([p])
    acta_io.write_acta(meeting, {"titulo": "X", "resumen": ["R"], "propuestas": [p]})


def test_a_lost_odoo_note_response_never_posts_the_note_twice(
    fake_odoo: FakeOdoo, meeting: Path
) -> None:
    cfg = odoo_config(Config(), fake_odoo)
    note = {
        "grupo": "crm",
        "tipo": "nota_en_historial",
        "datos": {"modelo": "crm.lead", "id": 205, "cuerpo": "Resumen de la reunion"},
    }
    _odoo_item(meeting, note)
    before = len(fake_odoo.data["mail.message"])
    flaky = Executor(meeting, odoo=lambda: _lossy(_odoo(cfg)(), "message_post", executes=True))
    acta = flaky.apply(["p1"])
    p = acta["propuestas"][0]
    assert (p["estado"], p["error"], p["incierta"]) == ("fallida", "resultado_incierto", True)
    assert p["en_curso"]["clave"]  # el diario se guardo antes de llamar
    assert len(fake_odoo.data["mail.message"]) == before + 1  # se hizo, pero no lo supimos
    acta = Executor(meeting, odoo=_odoo(cfg)).apply(["p1"])  # «Reintentar»
    p = acta["propuestas"][0]
    assert p["estado"] == "aplicada" and "en_curso" not in p and "incierta" not in p
    assert len(fake_odoo.data["mail.message"]) == before + 1  # encontrada por su marca
    assert p["deshacer"]["creado"] == fake_odoo.data["mail.message"][-1]["id"]


def test_a_note_confirmed_absent_is_posted_on_retry(fake_odoo: FakeOdoo, meeting: Path) -> None:
    cfg = odoo_config(Config(), fake_odoo)
    note = {
        "grupo": "crm",
        "tipo": "nota_en_historial",
        "datos": {"modelo": "crm.lead", "id": 205, "cuerpo": "Hola"},
    }
    _odoo_item(meeting, note)
    before = len(fake_odoo.data["mail.message"])
    Executor(meeting, odoo=lambda: _lossy(_odoo(cfg)(), "message_post", executes=False)).apply(
        ["p1"]
    )
    acta = Executor(meeting, odoo=_odoo(cfg)).apply(["p1"])
    assert acta["propuestas"][0]["estado"] == "aplicada"
    assert len(fake_odoo.data["mail.message"]) == before + 1


def test_an_unverifiable_lost_write_needs_explicit_confirmation(
    fake_odoo: FakeOdoo, meeting: Path
) -> None:
    cfg = odoo_config(Config(), fake_odoo)
    task = {
        "grupo": "agenda",
        "tipo": "crear_tarea",
        "datos": {"nombre": "Preparar demo", "proyecto_id": 50, "fecha": None},
    }
    _odoo_item(meeting, task)
    n = len(fake_odoo.data["project.task"])
    Executor(meeting, odoo=lambda: _lossy(_odoo(cfg)(), "create", executes=False)).apply(["p1"])
    acta = Executor(meeting, odoo=_odoo(cfg)).apply(["p1"])  # reintento normal: no se repite
    p = acta["propuestas"][0]
    assert (p["estado"], p["error"]) == ("fallida", "resultado_incierto")
    assert len(fake_odoo.data["project.task"]) == n
    # el usuario lo comprobo en Odoo y confirma: ahora si
    acta = Executor(meeting, odoo=_odoo(cfg)).apply(["p1"], confirm=["p1"])
    assert acta["propuestas"][0]["estado"] == "aplicada"
    assert len(fake_odoo.data["project.task"]) == n + 1


def test_a_definitive_odoo_error_is_not_uncertain(fake_odoo: FakeOdoo, meeting: Path) -> None:
    from micnaga.brain.executor import uncertain
    from micnaga.brain.odoo import OdooError

    try:
        raise xmlrpc.client.Fault(1, "ValidationError")
    except xmlrpc.client.Fault as fault:
        try:
            raise OdooError("Odoo error", "failed") from fault
        except OdooError as exc:
            assert uncertain(exc) is False
    try:
        raise ConnectionRefusedError()
    except ConnectionRefusedError as refused:
        try:
            raise OdooError("Odoo is not responding", "offline") from refused
        except OdooError as exc:
            assert uncertain(exc) is False  # no llego a enviarse


@pytest.mark.parametrize("mode", ["draft", "send"])
def test_a_lost_gmail_response_is_found_instead_of_sending_twice(tmp_path: Path, mode: str) -> None:
    from micnaga.brain.claude import ClaudeTimeoutError

    google = FakeGoogle()
    p = {
        "id": "p2",
        "uid": plan.new_uid(),
        "grupo": "google",
        "tipo": "correo_google",
        "estado": "pendiente",
        "datos": {"modo": mode, "para": ["ana@cliente.com"], "asunto": "R", "cuerpo": "Hola"},
    }
    slow = _applier(tmp_path, google, raise_after_call=ClaudeTimeoutError("x"))
    with pytest.raises(apply.GoogleApplyError):
        slow.apply(p, {})
    undo = _applier(tmp_path, google).apply(p, {})
    writes = [c for c in google.calls if c[0].endswith(("create_draft", "send_message"))]
    assert len(writes) == 1
    if mode == "draft":
        assert undo["draft_id"] in google.drafts


class _LostResult(FakeGoogleRunner):
    """El conector hace la escritura, pero su respuesta no vuelve (sin resultado)."""

    def run_once(self, req: Any, on_event: Any = None) -> Any:
        res = super().run_once(req, on_event)
        res.tool_results = []
        return res


def test_a_write_without_result_is_uncertain_never_failed(tmp_path: Path) -> None:
    google = FakeGoogle()
    p = {
        "id": "p2",
        "uid": plan.new_uid(),
        "grupo": "google",
        "tipo": "correo_google",
        "estado": "pendiente",
        "datos": {"modo": "draft", "para": ["ana@cliente.com"], "asunto": "R", "cuerpo": "Hola"},
    }
    folder = tmp_path / "2026-11-19_1030_reunion"
    folder.mkdir(exist_ok=True)
    lost = apply.GoogleApplier(folder, runner=_LostResult(google), db_factory=open_database)
    with pytest.raises(apply.GoogleApplyError, match="google_unknown_outcome"):
        lost.apply(p, {})
    _applier(tmp_path, google).apply(p, {})  # se comprueba en Gmail: ya estaba
    assert len(google.drafts) == 1
    google.search_fails = True
    other = {**p, "uid": plan.new_uid()}
    with pytest.raises(apply.GoogleApplyError, match="google_unknown_outcome"):
        lost.apply(other, {})
    with pytest.raises(apply.GoogleApplyError, match="google_unknown_outcome"):
        _applier(tmp_path, google).apply(other, {})  # no se puede comprobar: no se repite
    assert len(google.drafts) == 2
    _applier(tmp_path, google).apply(other, {}, confirm=True)  # confirmado por el usuario
    assert len(google.drafts) == 3
