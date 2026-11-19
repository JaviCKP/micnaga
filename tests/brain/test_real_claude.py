"""Extremo a extremo con el ``claude -p`` REAL (tu sesion de Claude Code). Gasta uso del plan.

``MICNAGA_RUN_CLAUDE=1 pytest -m claude -s tests/brain/test_real_claude.py``

* La transcripcion real de ``%TEMP%\\micnaga_podcast\\storage\\2026-11-19_0858_reunion`` se COPIA
  a una carpeta temporal (el original no se toca); si no existe, ese test se salta.
* La reunion de negocio sintetica va contra el Odoo FALSO (nunca un Odoo real).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from micnaga import paths
from micnaga.brain.secretary import Secretary
from micnaga.brain.service import BrainService
from micnaga.config import Config
from tests.brain.conftest import business_transcript, odoo_config, write_meeting
from tests.brain.fake_odoo import FakeOdoo

pytestmark = [pytest.mark.claude, pytest.mark.timeout(600)]

PODCAST = Path(os.environ.get("TEMP", "")) / "micnaga_podcast/storage/2026-11-19_0858_reunion"


def _report(name: str, t0: float, res: object) -> None:
    acta = getattr(res, "acta", {})
    r = getattr(res, "result", None)
    out = {
        "test": name,
        "wall_s": round(time.monotonic() - t0, 1),
        "claude_s": getattr(r, "duration_s", None),
        "turns": getattr(r, "num_turns", None),
        "tools": [c.tool for c in getattr(r, "tool_calls", [])],
        "cost_usd_equiv": getattr(r, "cost_usd", None),
        "model": getattr(r, "model", None),
        "api_key_source": getattr(r, "api_key_source", None),
        "titulo": acta.get("titulo"),
        "resumen": acta.get("resumen"),
        "tareas": [t.get("texto") for t in acta.get("tareas", [])],
        "propuestas": [
            (p["grupo"], p["tipo"], p.get("checked"), p.get("aviso"))
            for p in acta.get("propuestas", [])
        ],
    }
    print("\nREAL-CLAUDE " + json.dumps(out, ensure_ascii=False))


def test_real_session_is_detected_without_inherited_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-bogus-must-not-be-used")
    svc = BrainService(Config)
    det = svc.api("claude_detect", {})
    assert det["logged_in"] is True, det
    t0 = time.monotonic()
    out = svc.api("claude_test", {})
    print(f"\nREAL-CLAUDE title test: {out} wall {time.monotonic() - t0:.1f}s")
    assert out["ok"] and out["title"]


def test_real_acta_on_the_podcast_copy(secrets_file: Path) -> None:
    if not (PODCAST / paths.TRANSCRIPT_JSON).is_file():
        pytest.skip("no real transcript at %TEMP%\\micnaga_podcast")
    dst = paths.meetings_root() / PODCAST.name
    dst.mkdir(parents=True)
    shutil.copy2(PODCAST / paths.TRANSCRIPT_JSON, dst / paths.TRANSCRIPT_JSON)
    cfg = Config()
    t0 = time.monotonic()
    res = Secretary(cfg).analyze(dst, today="2026-11-19")
    _report("podcast", t0, res)
    assert res.acta["titulo"] and isinstance(res.acta["resumen"], list)
    assert res.result is not None and res.result.api_key_source in ("none", "")
    assert (res.folder / "acta.json").is_file() and (res.folder / "acta.md").is_file()


def test_real_business_meeting_with_fake_odoo(fake_odoo: FakeOdoo) -> None:
    folder = write_meeting(business_transcript())
    cfg = odoo_config(Config(), fake_odoo)
    seen: list[dict[str, object]] = []
    t0 = time.monotonic()
    res = Secretary(cfg).analyze(folder, on_status=seen.append, today="2026-11-19")
    _report("business+odoo", t0, res)
    assert res.result is not None and res.result.mcp_servers.get("micnaga") == "connected"
    assert res.result.tool_calls, "the model never looked at Odoo"
    assert fake_odoo.write_calls == []  # analizar solo lee
    props = res.acta["propuestas"]
    crm = [p for p in props if p["grupo"] == "crm"]
    assert crm, props
    assert all(p["datos"] for p in crm)
    linked = [p for p in crm if p["tipo"] in ("vincular_a_oportunidad", "actualizar_oportunidad")]
    assert any(p["datos"].get("id") == 205 for p in linked), crm
    assert res.acta["tareas"]
    assert any(s.get("fase") == "herramienta" for s in seen)
