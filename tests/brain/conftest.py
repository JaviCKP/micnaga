"""Fixtures de la Secretaria: ``claude`` falso, Odoo falso, almacen de secretos y reuniones."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from micnaga import paths
from micnaga.brain import secrets
from micnaga.config import Config
from micnaga.store.db import open_database
from micnaga.types import Transcript
from tests.brain.demo import business_transcript
from tests.brain.fake_odoo import DB, KEY, LOGIN, FakeOdoo

HERE = Path(__file__).resolve().parent


def write_meeting(transcript: Transcript, name: str = "2026-11-19_1030_reunion") -> Path:
    from micnaga.pipeline import output

    root = paths.meetings_root()
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    output.write_transcript(folder, transcript)
    with open_database() as db:
        output.index_meeting(db, folder, transcript)
    return folder


@pytest.fixture
def meeting() -> Path:
    return write_meeting(business_transcript())


@pytest.fixture
def secrets_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Almacen de secretos de prueba que tambien ve el servidor MCP (otro proceso)."""
    path = tmp_path / "secrets.json"
    monkeypatch.setenv(secrets.ENV_SECRETS_FILE, str(path))
    secrets.set_store(None)
    return path


@pytest.fixture
def fake_odoo(secrets_file: Path) -> Iterator[FakeOdoo]:
    srv = FakeOdoo().start()
    secrets.put(secrets.ODOO_KEY, KEY)
    try:
        yield srv
    finally:
        srv.stop()


def odoo_config(cfg: Config, srv: FakeOdoo) -> Config:
    cfg.secretaria.odoo_url = srv.url
    cfg.secretaria.odoo_db = DB
    cfg.secretaria.odoo_login = LOGIN
    return cfg


FakeClaude = Callable[[dict[str, Any]], Path]


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    """``fake_claude(escenario) -> ruta del log``; fija ``MICNAGA_CLAUDE_EXE``."""
    counter = {"n": 0}

    def make(scenario: dict[str, Any]) -> Path:
        counter["n"] += 1
        n = counter["n"]
        log = tmp_path / f"claude-{n}.log.json"
        sc = tmp_path / f"scenario-{n}.json"
        sc.write_text(json.dumps({"log": str(log), **scenario}), encoding="utf-8")
        wrapper = tmp_path / f"claude_{n}.py"
        wrapper.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(HERE)!r})\n"
            "import fake_claude\n"
            f"fake_claude.SCENARIO = {str(sc)!r}\n"
            "sys.exit(fake_claude.main(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MICNAGA_CLAUDE_EXE", str(wrapper))
        return log

    return make


def read_log(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


PYTHON = sys.executable
