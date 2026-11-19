"""``BrainService``: aviso «Lista» con propuestas, reintentos, arranque y API de la UI."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from micnaga.brain import acta as acta_io
from micnaga.brain import secrets
from micnaga.brain import store as bstore
from micnaga.brain.claude import ClaudeOfflineError
from micnaga.brain.secretary import AnalysisResult
from micnaga.brain.service import BrainService
from micnaga.config import Config
from micnaga.pipeline import output
from micnaga.store.db import open_database
from tests.brain import canned
from tests.brain.conftest import FakeClaude, business_transcript, write_meeting

RESEARCH = [{"tool": "reuniones_con", "args": {"texto": "Dental Triana"}}]
"""El modelo falso investiga (sin investigar, la Secretaria repite la pasada una vez)."""


class Notes:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, float, int, int]] = []
        self.event = threading.Event()

    def __call__(self, *args: Any) -> None:
        self.items.append(args)
        self.event.set()


def cfg(enabled: bool = True) -> Config:
    c = Config()
    c.secretaria.enabled = enabled
    return c


def test_disabled_notifies_immediately_without_proposals(meeting: Path) -> None:
    svc = BrainService(lambda: cfg(False))
    notes = Notes()
    svc.on_job_done(meeting.name, "Reunión", 600.0, 3, notes)
    assert notes.items == [(meeting.name, "Reunión", 600.0, 3, 0)]


def test_toast_waits_for_the_pass_and_carries_the_count(
    fake_claude: FakeClaude, meeting: Path, secrets_file: Path
) -> None:
    fake_claude({"structured": canned.structured(with_crm=False)})
    svc = BrainService(lambda: cfg())
    svc.start()
    try:
        notes = Notes()
        svc.on_job_done(meeting.name, "Reunión", 600.0, 3, notes)
        assert notes.event.wait(60)
        folder, title, _d, _s, n = notes.items[0]
        assert folder == "2026-11-19_1030_dental-triana-alcance-ampliado"
        assert title == "Dental Triana: alcance ampliado"
        assert n == 4  # cuatro correcciones
        with open_database() as db:
            run = bstore.get_run(db, folder)
            assert run is not None and run.state == "done" and run.proposals == 4
    finally:
        svc.stop()


def test_failure_backs_off_and_uses_the_live_title(meeting: Path) -> None:
    acta_io.write_live_title(meeting, "Dental Triana presupuesto")

    class Boom:
        def analyze(self, folder: Path, **_kw: Any) -> AnalysisResult:
            raise ClaudeOfflineError("no network")

    svc = BrainService(lambda: cfg(), secretary_factory=lambda _c: Boom(), toast_wait_s=30)
    svc.start()
    try:
        notes = Notes()
        svc.on_job_done(meeting.name, "Reunión", 600.0, 3, notes)
        assert notes.event.wait(30)
        folder = notes.items[0][0]
        assert folder == "2026-11-19_1030_dental-triana-presupuesto"
        assert notes.items[0][1] == "Dental Triana presupuesto"
        assert notes.items[0][4] == 0
        with open_database() as db:
            run = bstore.get_run(db, folder)
            assert run is not None and run.state == "failed" and run.error_kind == "offline"
            assert run.next_try > datetime.now().timestamp() + 30  # espera creciente
            assert bstore.due(db) == []
    finally:
        svc.stop()


def test_slow_pass_does_not_hold_the_toast(meeting: Path) -> None:
    gate = threading.Event()

    class Slow:
        def analyze(self, folder: Path, **_kw: Any) -> AnalysisResult:
            gate.wait(10)
            return AnalysisResult(folder, {}, 0)

    svc = BrainService(lambda: cfg(), secretary_factory=lambda _c: Slow(), toast_wait_s=0.3)
    svc.start()
    try:
        notes = Notes()
        svc.on_job_done(meeting.name, "Reunión", 60.0, 2, notes)
        assert notes.event.wait(5)
        assert notes.items == [(meeting.name, "Reunión", 60.0, 2, 0)]
    finally:
        gate.set()
        svc.stop()


def test_boot_retries_what_failed_for_lack_of_a_claude_session(
    fake_claude: FakeClaude, secrets_file: Path
) -> None:
    now = datetime.now().astimezone().replace(microsecond=0).isoformat()
    folder = write_meeting(business_transcript(start=now), "2026-11-19_0900_reunion")
    with open_database() as db:
        bstore.ensure_schema(db)
        bstore.enqueue(db, folder.name)
        assert bstore.claim(db, folder.name)
        bstore.fail(db, folder.name, "not logged in", "auth")
        assert bstore.due(db) == []  # sin reintento automatico mientras la app sigue
    log = fake_claude({"structured": canned.structured(with_crm=False), "calls": RESEARCH})
    svc = BrainService(lambda: cfg())  # la app arranca de nuevo (ya con sesion)
    svc.start()
    try:
        for _ in range(100):
            if svc.resolve_folder(folder.name) != folder.name:
                break
            threading.Event().wait(0.1)
        assert Path(str(log) + ".count").read_text() == "1"
        final = svc.resolve_folder(folder.name)
        assert final.endswith("_dental-triana-alcance-ampliado")
        assert svc.resolve_folder("otra") == "otra"
    finally:
        svc.stop()


def test_boot_picks_up_recent_meetings_without_acta(
    fake_claude: FakeClaude, secrets_file: Path
) -> None:
    now = datetime.now().astimezone().replace(microsecond=0).isoformat()
    folder = write_meeting(business_transcript(start=now), "2026-11-19_0900_reunion")
    old = write_meeting(
        business_transcript(start="2020-01-01T10:00:00+01:00"), "2020-01-01_1000_reunion"
    )
    log = fake_claude({"structured": canned.structured(with_crm=False), "calls": RESEARCH})
    svc = BrainService(lambda: cfg())
    svc.start()
    try:
        for _ in range(100):
            if Path(str(log) + ".count").is_file() and svc.status(
                "2026-11-19_0900_dental-triana-alcance-ampliado"
            ):
                break
            threading.Event().wait(0.1)
        assert Path(str(log) + ".count").read_text() == "1"
        assert not (old / acta_io.ACTA_JSON).exists()
        assert not folder.exists()  # renombrada por el titulo del acta
    finally:
        svc.stop()


def test_ui_api(fake_claude: FakeClaude, meeting: Path, secrets_file: Path) -> None:
    fake_claude({"structured": canned.structured(with_crm=False)})
    svc = BrainService(lambda: cfg())
    from micnaga.brain.secretary import Secretary

    res = Secretary(cfg()).analyze(meeting)
    name = res.folder.name
    got = svc.api("acta", {"folder": name})
    assert got["ok"] and got["acta"]["titulo"] == "Dental Triana: alcance ampliado"
    assert svc.api("acta", {"folder": "../.."})["ok"] is False
    ids = [p["id"] for p in got["acta"]["propuestas"] if p["tipo"] == "hablante"]
    applied = svc.api("apply", {"folder": name, "ids": ids})
    assert all(p["estado"] == "aplicada" for p in applied["acta"]["propuestas"] if p["id"] in ids)
    names = {s.id: s.name for s in output.read_transcript(res.folder).speakers}
    assert names["S2"] == "Marta Gil"
    undone = svc.api("undo", {"folder": name, "ids": ids})
    assert all(p["estado"] == "deshecha" for p in undone["acta"]["propuestas"] if p["id"] in ids)
    rest = [p["id"] for p in got["acta"]["propuestas"] if p["id"] not in ids]
    dismissed = svc.api("dismiss", {"folder": name, "ids": rest})
    assert {p["estado"] for p in dismissed["acta"]["propuestas"] if p["id"] in rest} == {
        "descartada"
    }
    # secretos: entran, nunca salen
    assert svc.api("set_secret", {"kind": "token", "value": "oat-123"}) == {
        "ok": True,
        "stored": True,
    }
    status = svc.api("status", {})
    assert status["has_token"] is True and "oat-123" not in str(status)
    assert secrets.get(secrets.CLAUDE_TOKEN) == "oat-123"
    assert svc.api("set_secret", {"kind": "token", "value": None})["stored"] is False
    assert svc.api("set_secret", {"kind": "bogus", "value": "x"})["ok"] is False
    assert svc.api("claude_detect", {})["logged_in"] is True
    assert svc.api("nope", {})["ok"] is False
    assert svc.search("alcance")[0]["folder"] == name
    learned = svc.api("learned", {})
    assert learned["ok"] and len(learned["items"]) == 1
    assert svc.api("forget", {"index": 0})["items"] == []


def test_boot_reopens_only_failures_from_before_this_start() -> None:
    with open_database() as db:
        bstore.ensure_schema(db)
        for name in ("antes", "ahora"):
            bstore.enqueue(db, name)
            assert bstore.claim(db, name)
        bstore.fail(db, "antes", "not logged in", "auth")
        started = datetime.now().timestamp() + 1
        bstore.fail(db, "ahora", "no network", "offline")  # fallo de esta sesion: se respeta
        with db.transaction() as conn:
            conn.execute(
                "UPDATE secretary_runs SET updated_at = ? WHERE folder = 'ahora'", (started + 1,)
            )
        assert bstore.enqueue(db, "antes", failed_before=started).state == "pending"
        again = bstore.enqueue(db, "ahora", failed_before=started)
        assert again.state == "failed" and again.error_kind == "offline"
