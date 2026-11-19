"""Enganches de la Secretaria en la UI y en la app (puente, aviso, pastilla, Application)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from micnaga.config import Config
from micnaga.store.db import Database
from micnaga.ui import toast
from micnaga.ui.bridge import LibraryApi, SettingsApi, call_brain
from micnaga.ui.contracts import BrainPort, UiCallbacks


class FakeBrain:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def api(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, args))
        return {"ok": True, "action": action}

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return [{"folder": "f1", "title": "Dental", "snippet": "panel <mark>ventas</mark>"}]


def test_bridge_gates_actions_per_surface(tmp_path: Path) -> None:
    brain = FakeBrain()
    assert isinstance(brain, BrainPort)
    lib = LibraryApi(
        db_factory=lambda: Database(tmp_path / "x.db"),
        meetings_root=lambda: tmp_path,
        audio_url=lambda f, t: "",
        config=Config,
        brain=brain,
    )
    assert lib.secretaria("acta", {"folder": "x"}) == {"ok": True, "action": "acta"}
    assert lib.secretaria("set_secret", {"kind": "token"})["ok"] is False  # no es de la biblioteca
    assert lib.secretaria("apply", "not-a-dict")["ok"] is True  # type: ignore[arg-type]
    assert brain.calls[-1] == ("apply", {})
    hits = lib.search("ventas")["hits"]
    assert hits[-1]["speaker"] == "Acta" and hits[-1]["acta"] is True
    assert hits[-1]["parts"] == [{"t": "panel ", "hit": False}, {"t": "ventas", "hit": True}]
    assert lib.bootstrap()["secretaria"] is True

    class Store:
        def load(self) -> Config:
            return Config()

        def save(self, c: Config) -> None:
            pass

    st = SettingsApi(store=Store(), callbacks=UiCallbacks(), brain=brain)
    assert st.secretaria("odoo_test", {})["ok"] is True
    assert st.secretaria("apply", {})["error"] == "secretaria.err.unknown"
    assert call_brain(None, frozenset({"acta"}), "acta", {})["error"] == "secretaria.err.disabled"


def test_toast_mentions_proposals() -> None:
    assert toast.format_ready_text("Dental", 1440, 3) == "Dental · 24 min · 3 hablantes"
    assert toast.format_ready_text("Dental", 1440, 3, proposals=5) == (
        "Dental · 24 min · 3 hablantes · 5 propuestas"
    )
    assert toast.format_ready_text("D", 60, 1, proposals=1).endswith("· 1 propuesta")
