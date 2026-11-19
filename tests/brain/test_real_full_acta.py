"""«Acta completa» con el ``claude -p`` REAL sobre la reunion sintetica de negocio.

``MICNAGA_RUN_CLAUDE=1 pytest -m claude -s tests/brain/test_real_full_acta.py``
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from micnaga.brain import full_acta
from micnaga.brain import store as bstore
from micnaga.brain.claude import ClaudeRunner
from micnaga.config import Config
from micnaga.store.db import open_database
from tests.brain.conftest import write_meeting
from tests.brain.demo import business_transcript

pytestmark = [pytest.mark.claude, pytest.mark.timeout(420)]


def with_db(fn: Any) -> Any:
    db = open_database()
    try:
        bstore.ensure_schema(db)
        return fn(db)
    finally:
        db.close()


def test_real_full_acta_is_a_long_document_with_timestamps() -> None:
    tr = business_transcript()
    folder = write_meeting(tr, "2026-11-19_1030_dental_triana")
    chunks: list[str] = []
    t0 = time.monotonic()
    out = full_acta.FullActa(Config(), ClaudeRunner(), with_db=with_db).make(
        folder, on_text=chunks.append
    )
    wall = time.monotonic() - t0
    md, meta = out["md"], out["meta"]
    print(
        f"\n[real acta completa] {meta['words']} palabras, {meta['sections']} secciones,"
        f" {len(chunks)} trozos en streaming, {wall:.1f} s, ${meta['cost_usd']:.4f},"
        f" {meta['tool_calls']} herramientas"
    )
    assert len(chunks) > 3  # llego a trozos
    for title in ("Orden del día", "Decisiones", "Acuerdos y tareas", "Próximos pasos", "Fuentes"):
        assert title in md, title
    stamps = full_acta.stamps(md)
    assert len(stamps) >= 5 and max(stamps) <= tr.meeting.duration_s + 5
    assert meta["words"] >= 250
