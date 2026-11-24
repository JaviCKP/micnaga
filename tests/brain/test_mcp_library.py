from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from micnaga import holding, paths
from micnaga.brain import store
from micnaga.brain.library_mcp import LibraryError, LibraryTools, _index
from micnaga.meeting_links import meeting_link
from micnaga.store.db import Database, MeetingRecord
from micnaga.types import Segment


def _record(
    db: Database,
    root: Path,
    name: str,
    title: str,
    segments: list[Segment],
    *,
    day: int = 1,
    status: str | None = None,
) -> int:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / paths.TRANSCRIPT_JSON).write_text(
        json.dumps(
            {
                "speakers": [{"id": "me", "name": "Rosa"}],
                "segments": [s.to_dict() for s in segments],
            }
        ),
        encoding="utf-8",
    )
    job = db.enqueue_job(folder) if status else None
    if status == "done":
        assert job is not None
        db.claim_next_job()
        db.finish_job(job.id)
    return db.upsert_meeting(
        MeetingRecord(
            name, title, f"2026-11-{day:02d}T10:00:00", 130, job_id=job.id if job else None
        ),
        segments,
    )


@pytest.fixture
def meetings(tmp_storage: Path) -> tuple[Path, int, int]:
    root = tmp_storage.resolve()
    root.mkdir()
    with Database(root / paths.DB_FILENAME) as db:
        main = _record(
            db,
            root,
            "revision",
            "Revisión semanal",
            [
                Segment(10, 11, "me", "fuera de ventana"),
                Segment(42, 45, "me", "umbral aprobado", text_original="umbral aprobao"),
                Segment(80, 81, "me", "después de ventana"),
            ],
            status="done",
        )
        pending = _record(
            db,
            root,
            "pendiente",
            "Privada en proceso",
            [Segment(2, 3, "me", "umbral")],
            day=2,
            status="pending",
        )
        _record(db, root, "descartada", "Privada descartada", [], day=3)
        holding.mark_discarded(root / "descartada")
        _record(db, root, "../fuera", "Índice fuera de biblioteca", [], day=4)
        (root / "revision" / "acta.json").write_text(
            json.dumps(
                {
                    "resumen": ["Cambios del plan de pruebas"],
                    "decisiones": [{"texto": "activar validación", "t": 42}],
                    "propuestas": [{"api_key": "interno-isolado"}],
                }
            ),
            encoding="utf-8",
        )
        store.ensure_schema(db)
        store.index_acta(db, "revision", "Revisión semanal", "activar validación")
    return root, main, pending


def test_library_limits_search_to_completed_meetings_before_pagination(
    meetings: tuple[Path, int, int],
) -> None:
    root, main, pending = meetings
    tools = LibraryTools(root)
    listed = tools.call("buscar_reuniones", {"limite": 1})
    assert [row["reunion_id"] for row in listed["reuniones"]] == [main]
    assert listed["siguiente_pagina"] is None
    hit = tools.call("buscar_reuniones", {"texto": "umbral"})["reuniones"][0]
    assert hit["fragmento"] == "umbral aprobado"
    assert hit["enlace"] == meeting_link(root, main, 42)
    assert tools.call("buscar_reuniones", {"texto": "revision"})["reuniones"]
    assert tools.call("buscar_reuniones", {"texto": "validación"})["reuniones"]
    assert tools.call("buscar_reuniones", {"texto": "umbral", "hasta": "2026-11-01"})["reuniones"]
    assert not tools.call("buscar_reuniones", {"desde": "2026-11-02"})["reuniones"]
    with pytest.raises(LibraryError, match="YYYY-MM-DD"):
        tools.call("buscar_reuniones", {"hasta": "2026-11-31"})
    with pytest.raises(LibraryError, match="no disponible"):
        tools.call("leer_transcripcion", {"reunion_id": pending})


def test_library_returns_timed_versions_safe_acta_and_read_only_index(
    meetings: tuple[Path, int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, main, pending = meetings
    tools = LibraryTools(root)
    query = {"reunion_id": main, "desde_s": 41, "hasta_s": 47}
    result = tools.call("leer_transcripcion", query)
    assert [(s["inicio_s"], s["fin_s"], s["texto"]) for s in result["segmentos"]] == [
        (42, 45, "umbral aprobado")
    ]
    result = tools.call("leer_transcripcion", {**query, "version": "original"})
    assert [s["texto"] for s in result["segmentos"]] == ["umbral aprobao"]
    acta = tools.call("leer_acta", {"reunion_id": main})["acta"]
    assert "Cambios del plan de pruebas" in acta and "interno-isolado" not in acta
    opened: list[str] = []
    monkeypatch.setattr("micnaga.meeting_links.open_in_micnaga", opened.append)
    tools.call("abrir_en_micnaga", {"reunion_id": main, "segundo": 42})
    with pytest.raises(LibraryError, match="no disponible"):
        tools.call("abrir_en_micnaga", {"reunion_id": pending})
    assert opened == [meeting_link(root, main, 42)]
    with _index(root) as conn:
        assert conn is not None
        conn.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM meetings")


def test_library_stdio_has_no_secretary_tools(meetings: tuple[Path, int, int]) -> None:
    _, main, _ = meetings

    def request(mid: int, method: str, params: dict[str, Any]) -> bytes:
        msg = {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}
        return json.dumps(msg).encode()

    messages = [
        request(1, "initialize", {"protocolVersion": "2025-06-18"}),
        request(2, "tools/list", {}),
        request(3, "tools/call", {"name": "leer_acta", "arguments": {"reunion_id": main}}),
        request(4, "prompts/list", {}),
        request(5, "prompts/get", {"name": "revisar_acta"}),
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "micnaga", "tools", "--library"],
        input=b"\n".join(messages) + b"\n",
        capture_output=True,
        timeout=10,
        check=True,
    )
    init, listed, called, prompts, prompt = [
        json.loads(line)["result"] for line in completed.stdout.splitlines()
    ]
    assert {tool["name"] for tool in listed["tools"]} == {
        "buscar_reuniones",
        "leer_acta",
        "leer_transcripcion",
        "abrir_en_micnaga",
    }
    assert called["isError"] is False
    assert "activar validación" in json.loads(called["content"][0]["text"])["acta"]
    assert "prompts" in init["capabilities"]
    assert {p["name"] for p in prompts["prompts"]} == {
        "revisar_acta", "preparar_reunion", "seguir_acuerdos"
    }
    assert "transcripción" in prompt["messages"][0]["content"]["text"]
