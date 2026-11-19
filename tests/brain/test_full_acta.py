"""«Acta completa» con un runner falso: entrada, streaming, validacion, cache y «desactualizada»."""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path
from typing import Any

from micnaga.brain import acta as acta_io
from micnaga.brain import full_acta
from micnaga.brain import store as bstore
from micnaga.brain.claude import ClaudeRequest, ClaudeResult
from micnaga.brain.service import BrainService
from micnaga.config import Config
from micnaga.pipeline import output
from micnaga.store.db import open_database
from tests.brain import research_case as case

SEEN_URL = "https://mail.google.com/mail/u/0/#all/hilo-presupuesto"
DOC = f"""# Acta: Dental Triana · acceso al panel

**Fecha:** jueves 19 de noviembre de 2026 · **Duración:** 1 min · **Asistentes:** Yo, Carlos Ruiz

## Orden del día
1. Presupuesto revisado
2. Acceso de recepción al panel

## 1. Presupuesto revisado
Carlos confirma que dirección aprobó el presupuesto [00:06]. Ver el [correo]({SEEN_URL}) y
el [otro](https://mail.google.com/mail/u/0/#all/inventado). Acta {case.PRIOR} y la
2026-01-01_0900_no_existe. Tarea project.task:{case.TASK_TEMPLATES} y project.task:999.

## 2. Acceso de recepción al panel
Se prepara un acceso de prueba para el viernes 21 [00:21] y algo en [59:00].

## Decisiones
- Seguir con el panel [00:16].
"""


class Writer:
    """El modelo falso: manda el texto a trozos (streaming) y lo devuelve entero."""

    def __init__(self) -> None:
        self.requests: list[ClaudeRequest] = []

    def run_once(self, req: ClaudeRequest, on_event: Any = None) -> ClaudeResult:
        self.requests.append(req)
        if req.on_text is not None:
            for k in range(0, len(DOC), 80):
                req.on_text(DOC[k : k + 80])
        return ClaudeResult(None, DOC, 0.21, {}, 30.0, 2, "claude-fake")


def meeting() -> Path:
    _, current = case.write_library()
    acta = {
        "titulo": "Dental Triana · acceso al panel",
        "resumen": ["Presupuesto aprobado."],
        "tareas": [{"texto": "Acceso de prueba", "responsable": "Yo", "t": 21.0}],
        "fuentes": [
            {"tipo": "gmail", "ref": SEEN_URL, "titulo": "Re: presupuesto", "url": SEEN_URL},
            {"tipo": "acta", "ref": case.PRIOR, "titulo": "Dental Triana"},
            {"tipo": "odoo", "ref": f"project.task:{case.TASK_TEMPLATES}", "titulo": "Plantillas"},
        ],
    }
    acta_io.write_acta(current, acta)
    return current


def with_db(fn: Any) -> Any:
    db = open_database()
    try:
        bstore.ensure_schema(db)
        return fn(db)
    finally:
        db.close()


def test_the_input_has_the_acta_research_and_timestamps() -> None:
    folder = meeting()
    tr = output.read_transcript(folder)
    text = full_acta.build_input(tr, acta_io.read_acta(folder) or {}, "Bitnaga vende CRM.")
    assert "Asistentes (hablantes): Yo, Carlos Ruiz" in text
    assert "unas 450 palabras" in text  # 1 pagina como minimo
    assert '"en": "[00:21]"' in text and SEEN_URL in text  # el acta breve y sus fuentes
    assert "[00:06] Carlos Ruiz: Sí. El presupuesto" in text
    assert text.index("## Contexto del negocio") < text.index("## Transcripción")
    assert full_acta.target_words(47 * 60) == 1760  # ~4 paginas para 47 min


def test_it_streams_validates_and_is_cached() -> None:
    folder = meeting()
    runner = Writer()
    chunks: list[str] = []
    out = full_acta.FullActa(Config(), runner, with_db=with_db).make(folder, on_text=chunks.append)
    req = runner.requests[0]
    assert req.model == "sonnet" and req.schema is None and req.on_text is not None
    assert "micnaga acta completa" in req.system_prompt
    assert req.mcp_servers is not None and req.mcp_servers["micnaga"]["env"]["MICNAGA_TOOLS"] == ""
    assert "".join(chunks) == DOC  # lo que la vista ve mientras se escribe
    md = out["md"]
    assert SEEN_URL in md and "inventado" not in md  # solo URLs vistas
    assert case.PRIOR in md and "no_existe" not in md  # solo actas que existen y se vieron
    assert f"project.task:{case.TASK_TEMPLATES}" in md and "project.task:999" not in md
    assert full_acta.stamps(md) == [6, 21, 16]  # [59:00] cae fuera de la reunion
    assert (folder / full_acta.FILE).read_text(encoding="utf-8") == md
    doc = full_acta.read(folder)
    assert doc is not None and not doc["stale"]
    assert doc["meta"]["sections"] == 4 and doc["meta"]["words"] > 50
    with open_database() as db:
        bstore.ensure_schema(db)
        assert [h.folder for h in bstore.search_actas(db, "recepción panel")] == [folder.name]

    tr = output.read_transcript(folder)  # la transcripcion cambia: «desactualizada»
    segs = [dataclasses.replace(tr.segments[0], text="Hola Carlos, retomamos lo del 12.")]
    output.write_transcript(folder, dataclasses.replace(tr, segments=segs + tr.segments[1:]))
    doc = full_acta.read(folder)
    assert doc is not None and doc["stale"]


def test_the_service_writes_it_once_and_serves_it_instantly() -> None:
    folder = meeting()
    runner = Writer()
    cfg = Config()
    cfg.secretaria.enabled = True
    svc = BrainService(lambda: cfg, db_factory=open_database, runner_factory=lambda _c: runner)
    opened: list[Path] = []
    svc._open_file = opened.append
    assert svc.api("full_acta", {"folder": folder.name}) == {"ok": True, "doc": None, "live": None}
    assert svc.api("full_acta_make", {"folder": folder.name})["ok"]
    for _ in range(100):
        r = svc.api("full_acta", {"folder": folder.name})
        if r["doc"] is not None and (r["live"] or {}).get("fase") == "lista":
            break
        time.sleep(0.05)
    assert r["doc"]["md"].startswith("# Acta: Dental Triana")
    again = svc.api("full_acta", {"folder": folder.name})
    assert again["doc"]["md"] == r["doc"]["md"] and len(runner.requests) == 1  # cache
    assert svc.api("full_acta_open", {"folder": folder.name}) == {"ok": True}
    assert opened == [folder / full_acta.FILE]
    assert svc.api("full_acta", {"folder": "../fuera"})["ok"] is False


def test_streamed_text_is_visible_while_it_writes() -> None:
    folder = meeting()
    gate = threading.Event()

    class Slow(Writer):
        def run_once(self, req: ClaudeRequest, on_event: Any = None) -> ClaudeResult:
            assert req.on_text is not None
            req.on_text("# Acta: Dental")
            gate.wait(5)
            return super().run_once(dataclasses.replace(req, on_text=None))

    cfg = Config()
    cfg.secretaria.enabled = True
    svc = BrainService(lambda: cfg, db_factory=open_database, runner_factory=lambda _c: Slow())
    svc.api("full_acta_make", {"folder": folder.name})
    for _ in range(50):
        live = svc.api("full_acta", {"folder": folder.name})["live"]
        if live and live.get("texto"):
            break
        time.sleep(0.02)
    assert live == {"fase": "escribiendo", "texto": "# Acta: Dental", "inicio": live["inicio"]}
    gate.set()
