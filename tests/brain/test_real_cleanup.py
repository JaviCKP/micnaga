"""Limpieza con el ``claude -p --model haiku`` REAL sobre una transcripcion sintetica con errores
de ASR sembrados. ``MICNAGA_RUN_CLAUDE=1 pytest -m claude -s tests/brain/test_real_cleanup.py``

Se comprueba que arregla lo evidente (sigla deletreada, nombre de cliente mal oido, cifra dicha,
punto falso dentro de un parrafo y frase partida entre dos parrafos del mismo hablante) y que
deja intacta una frase correcta pero rara. Todo inventado; nada sale de este equipo salvo el
texto sintetico.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from micnaga.brain import cleanup
from micnaga.brain import store as bstore
from micnaga.brain.claude import ClaudeRunner
from micnaga.config import Config
from micnaga.pipeline import output
from micnaga.store.db import open_database
from micnaga.types import EnginesInfo, MeetingInfo, Segment, Speaker, Transcript, Word
from tests.brain.conftest import write_meeting

pytestmark = [pytest.mark.claude, pytest.mark.timeout(300)]

ODD = "El gato de Schrödinger no firma contratos los martes, y eso lo sabe todo el mundo."
LINES = [
    ("me", "Buenos días. Hoy repasamos el plan PQC de la Clínica Dental Triana."),
    ("S1", "Sí, la Clínica Dental Triana quiere cerrar el inventario antes de fin de mes."),
    ("me", "vamos a revisar. el inventario de claves esta semana con su equipo."),
    ("S1", "De acuerdo, ahora mismo tenemos cuatrocientos certificados en producción."),
    ("me", "y la migración a pe cu ce empieza por los certificados que caducan antes"),
    ("me", "porque son los que más riesgo tienen."),
    ("S1", "Perfecto. Lo hablo con la dirección de la clínica dental triano esta tarde."),
    ("me", ODD),
    ("S1", "Ja, ja. Bueno, entonces quedamos el jueves para la auditoría de PQC."),
    ("me", "Hecho, te mando el resumen por correo."),
]
WHERE_SPLIT = 4  # 4 y 5: el mismo hablante, una sola frase partida


def synthetic() -> Transcript:
    segs, t = [], 2.0
    for i, (spk, text) in enumerate(LINES):
        dur = 1.0 + 0.32 * len(text.split())
        toks = text.split()
        step = dur / len(toks)
        words = [Word(w, t + k * step, t + (k + 1) * step, 0.8) for k, w in enumerate(toks)]
        segs.append(Segment(round(t, 2), round(t + dur, 2), spk, text, words))
        t += dur + (0.4 if i == WHERE_SPLIT else 1.8)
    return Transcript(
        meeting=MeetingInfo(title="Plan PQC", start="2026-11-19T10:00:00+01:00", duration_s=t),
        speakers=[Speaker("me", "Yo"), Speaker("S1", "Hablante 1")],
        segments=segs,
        markers=[],
        engines=EnginesInfo(asr="parakeet"),
    )


def with_db(fn: Any) -> Any:
    db = open_database()
    try:
        bstore.ensure_schema(db)
        return fn(db)
    finally:
        db.close()


def test_real_haiku_fixes_the_planted_errors_and_leaves_the_odd_sentence() -> None:
    folder = write_meeting(synthetic(), "2026-11-19_1000_plan_pqc")
    t0 = time.monotonic()
    stats = cleanup.Cleaner(Config(), ClaudeRunner(), with_db=with_db).run(folder)
    wall = time.monotonic() - t0
    assert stats is not None and not stats.skipped, stats
    print(
        f"\n[real cleanup] {stats.windows} ventanas, {stats.corrected}/{stats.segments} parrafos,"
        f" {stats.merges} uniones, {stats.rejected} rechazados {stats.reasons},"
        f" {wall:.1f} s, ${stats.cost_usd:.4f}"
    )
    tr = output.read_transcript(folder)
    text = "\n".join(s.text for s in tr.segments)
    assert "pe cu ce" not in text and "PQC" in text.split("migración", 1)[1]
    assert "triano" not in text.lower()
    assert "400 certificados" in text
    assert "vamos a revisar. el inventario" not in text.lower()  # punto falso fuera
    odd = next(s for s in tr.segments if "Schrödinger" in s.text)
    assert cleanup.tokens(odd.text) == cleanup.tokens(ODD)  # la frase rara, igual
    expected = [spk for i, (spk, _t) in enumerate(LINES) if i != WHERE_SPLIT + 1]
    assert [s.speaker for s in tr.segments] == expected  # solo esa union; nunca se mezclan
    joined = next(s for s in tr.segments if "migración" in s.text)
    assert "riesgo" in joined.text, "la frase partida entre dos parrafos se une"
    assert joined.merged_from == [WHERE_SPLIT, WHERE_SPLIT + 1]
    assert " ".join(w.w for w in joined.words) == joined.text
