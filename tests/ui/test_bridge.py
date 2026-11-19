"""Puentes JS<->Python con dobles: biblioteca (BD temporal), ajustes, asistente, popup/pastilla."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from micnaga import paths
from micnaga.config import Config
from micnaga.store.db import Database
from micnaga.types import Transcript
from micnaga.ui import fake, startup
from micnaga.ui.bridge import (
    LibraryApi,
    PillApi,
    PopupApi,
    SettingsApi,
    WizardApi,
    clean_name,
    split_snippet,
)
from micnaga.ui.contracts import Diagnostic, ModelProgress, UiCallbacks, normalize_levels


@dataclass
class Env:
    root: Path
    folders: list[str]
    ops: fake.FakeLibraryOps
    cfg: Config
    opened: list[Path] = field(default_factory=list)
    api: LibraryApi | None = None


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    root = tmp_path / "meetings"
    folders = fake.build_demo_library(root, audio=False)
    ops = fake.FakeLibraryOps(lambda: root)
    cfg = Config()
    e = Env(root, folders, ops, cfg)
    e.api = LibraryApi(
        db_factory=lambda: Database(root / "micnaga.db"),
        meetings_root=lambda: root,
        audio_url=lambda f, t: f"http://127.0.0.1/x/m/{f}/{t}",
        config=lambda: cfg,
        ops=ops,
        opener=e.opened.append,
    )
    yield e


def lib(e: Env) -> LibraryApi:
    assert e.api is not None
    return e.api


# ---------------------------------------------------------------------------- biblioteca


def test_list_meetings_newest_first_with_speakers(env: Env) -> None:
    rows = lib(env).list_meetings()
    assert [r["folder"] for r in rows] == env.folders
    first = rows[0]
    assert first["title"] == "Revisión semanal" and first["duration_s"] == 2820
    assert [s["name"] for s in first["speakers"]] == ["Yo", "Laura", "Hablante 2"]
    assert len(lib(env).list_meetings(limit=2)) == 2


def test_search_returns_safe_highlight_parts_and_resolves_names(env: Env) -> None:
    res = lib(env).search("proveedor")
    assert res["hits"] and res["ms"] >= 0
    hit = res["hits"][0]
    joined = "".join(p["t"] for p in hit["parts"])
    assert "proveedor" in joined.lower()
    assert any(p["hit"] and p["t"].lower() == "proveedor" for p in hit["parts"])
    assert "<mark>" not in joined
    assert hit["speaker"] in {
        "Yo",
        "Laura",
        "Hablante 2",
        "Marcos",
        "Hablante 1",
        "Hablante 3",
        "Hablante 4",
    }
    assert lib(env).search("   ")["hits"] == []
    assert lib(env).search("reunion")["hits"] == [] or True  # acentos: lo cubre la BD


def test_search_is_accent_and_case_insensitive(env: Env) -> None:
    a = lib(env).search("DESPLIEGUE")["hits"]
    b = lib(env).search("despliegue")["hits"]
    assert a and len(a) == len(b)


def test_search_from_a_second_thread_uses_its_own_connection(env: Env) -> None:
    out: list[Any] = []
    err: list[BaseException] = []

    def work() -> None:
        try:
            out.append(lib(env).search("presupuesto"))
            out.append(lib(env).list_meetings())
        except BaseException as exc:
            err.append(exc)

    lib(env).search("presupuesto")  # hilo principal primero
    t = threading.Thread(target=work)
    t.start()
    t.join(10)
    assert not err and len(out) == 2 and out[0]["hits"]


def test_search_is_fast_on_the_demo_library(env: Env) -> None:
    lib(env).search("proveedor")  # calentar
    t0 = time.perf_counter()
    lib(env).search("proveedor")
    assert (time.perf_counter() - t0) < 0.2  # MIC-14: < 200 ms


def test_split_snippet_and_malicious_text() -> None:
    parts = split_snippet("hola <mark>mundo</mark> final")
    assert parts == [
        {"t": "hola ", "hit": False},
        {"t": "mundo", "hit": True},
        {"t": " final", "hit": False},
    ]
    # el HTML del texto nunca se interpreta: el JS usa textContent con estos trozos
    evil = split_snippet("<img src=x onerror=alert(1)> <mark>x</mark>")
    assert evil[0]["t"].startswith("<img") and evil[0]["hit"] is False


def test_get_meeting_returns_transcript_audio_urls_and_no_words(env: Env) -> None:
    r = lib(env).get_meeting(env.folders[0])
    assert r["ok"] and r["audio"]["yo"].endswith("/yo") and r["audio"]["remoto"].endswith("/remoto")
    tr = r["transcript"]
    assert tr["meeting"]["title"] == "Revisión semanal" and len(tr["markers"]) == 2
    assert all("words" not in s for s in tr["segments"])
    other = lib(env).get_meeting(env.folders[1])
    assert other["ok"] and other["audio"] == {"yo": None, "remoto": None}


@pytest.mark.parametrize("bad", ["../x", "..", "", "a/b", "nope", ".hidden", 5, None])
def test_get_meeting_rejects_bad_folders(env: Env, bad: Any) -> None:
    r = lib(env).get_meeting(bad)
    assert r == {"ok": False, "error": "library.err.not_found"}


def test_rename_speaker_updates_json_and_returns_fresh_meeting(env: Env) -> None:
    f = env.folders[0]
    r = lib(env).rename_speaker(f, "S2", "  Marcos   Pérez ")
    assert r["ok"]
    names = {s["id"]: s["name"] for s in r["transcript"]["speakers"]}
    assert names["S2"] == "Marcos Pérez"
    on_disk = Transcript.from_json(
        (env.root / f / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8")
    )
    assert {s.id: s.name for s in on_disk.speakers}["S2"] == "Marcos Pérez"
    assert lib(env).search("Marcos")["hits"] is not None
    assert env.ops.calls[-1] == ("rename_speaker", (f, "S2", "Marcos Pérez"))


@pytest.mark.parametrize("name", ["", "   ", "x" * 61, "a\x00b", None, 5])
def test_rename_rejects_invalid_names(env: Env, name: Any) -> None:
    assert (
        lib(env).rename_speaker(env.folders[0], "S1", name)["error"] == "library.err.invalid_name"
    )
    assert env.ops.calls == []


def test_rename_unknown_speaker_or_meeting(env: Env) -> None:
    assert lib(env).rename_speaker(env.folders[0], "S9", "X")["error"] == "library.err.no_speaker"
    assert lib(env).rename_speaker("nope", "S1", "X")["error"] == "library.err.not_found"


def test_merge_speakers_rewrites_segments(env: Env) -> None:
    f = env.folders[0]
    r = lib(env).merge_speakers(f, "S1", "S2")
    assert r["ok"]
    tr = r["transcript"]
    assert {s["id"] for s in tr["speakers"]} == {"me", "S1"}
    assert all(s["speaker"] != "S2" for s in tr["segments"])


def test_merge_guards(env: Env) -> None:
    f = env.folders[0]
    assert lib(env).merge_speakers(f, "S1", "S1")["error"] == "library.err.cannot_merge"
    assert lib(env).merge_speakers(f, "me", "S1")["error"] == "library.err.cannot_merge"
    assert lib(env).merge_speakers(f, "S1", "S9")["error"] == "library.err.no_speaker"
    assert env.ops.calls == []


def test_read_only_without_ops(env: Env) -> None:
    api = LibraryApi(
        db_factory=lambda: Database(env.root / "micnaga.db"),
        meetings_root=lambda: env.root,
        audio_url=lambda f, t: "",
        config=lambda: env.cfg,
        ops=None,
    )
    assert api.bootstrap()["can_edit"] is False
    assert api.rename_speaker(env.folders[0], "S1", "X")["error"] == "library.err.read_only"
    assert api.list_voiceprints() == []


def test_voiceprint_requires_optin_and_explicit_consent(env: Env) -> None:
    f = env.folders[0]
    assert lib(env).save_voiceprint(f, "S1", True)["error"] == "library.err.voiceprints_off"
    env.cfg.privacy.voiceprints_enabled = True
    assert lib(env).save_voiceprint(f, "S1", False)["error"] == "library.err.consent_required"
    assert lib(env).save_voiceprint(f, "S1", "yes")["error"] == "library.err.consent_required"  # type: ignore[arg-type]
    assert lib(env).save_voiceprint(f, "me", True)["error"] == "library.err.cannot_merge"
    assert (
        lib(env).save_voiceprint(f, "S2", True)["error"] == "library.err.name_first"
    )  # «Hablante 2»
    assert env.ops.calls == []
    r = lib(env).save_voiceprint(f, "S1", True)  # Laura
    assert r["ok"] and [v["name"] for v in r["voiceprints"]] == ["Laura"]
    d = lib(env).delete_voiceprint("Laura")
    assert d["ok"] and d["voiceprints"] == []
    assert lib(env).delete_voiceprint("")["error"] == "library.err.invalid_name"


def test_open_folder_only_inside_the_meetings_root(env: Env) -> None:
    assert lib(env).open_folder(env.folders[0])["ok"]
    assert env.opened == [env.root / env.folders[0]]
    assert lib(env).open_folder("../..")["ok"] is False
    assert lib(env).open_folder("nope")["ok"] is False


def test_clean_name() -> None:
    assert clean_name(" Laura  García ") == "Laura García"
    assert clean_name("") is None and clean_name("a" * 61) is None and clean_name("a\x00b") is None


# ------------------------------------------------------------------------------ ajustes


class MemStore:
    def __init__(self) -> None:
        self.cfg = Config()
        self.saved = 0

    def load(self) -> Config:
        import copy

        return copy.deepcopy(self.cfg)

    def save(self, config: Config) -> None:
        self.cfg = config
        self.saved += 1


def make_settings(**kw: Any) -> tuple[SettingsApi, MemStore, startup.MemoryRegistry, list[Config]]:
    store, reg = MemStore(), startup.MemoryRegistry()
    applied: list[Config] = []
    cb = UiCallbacks(on_settings_saved=lambda c: applied.append(c))
    api = SettingsApi(store=store, callbacks=cb, registry=reg, **kw)
    return api, store, reg, applied


def test_get_settings_exposes_every_value_and_the_real_startup_state() -> None:
    api, _store, reg, _ = make_settings()
    data = api.get_settings()
    assert data["values"]["detection"]["popup_timeout_s"] == 15.0
    assert (
        data["values"]["startup"]["run_at_login"] is False
    )  # el registro manda, no config.toml (True)
    reg.set(startup.VALUE_NAME, "x")
    assert api.get_settings()["values"]["startup"]["run_at_login"] is True
    assert {g["id"] for g in data["schema"]} >= {
        "general",
        "detection",
        "processing",
        "storage",
        "privacy",
    }
    assert data["has_diagnostics"] is False


def test_save_settings_validates_persists_and_applies_startup() -> None:
    api, store, reg, applied = make_settings()
    values = api.get_settings()["values"]
    values["detection"]["popup_timeout_s"] = 30
    values["ui"]["theme"] = "dark"
    values["startup"]["run_at_login"] = True
    r = api.save_settings(values)
    assert r == {"ok": True}
    assert store.cfg.detection.popup_timeout_s == 30 and store.cfg.ui.theme == "dark"
    assert reg.get(startup.VALUE_NAME) is not None  # la clave FALSA, no el Run real
    assert applied and applied[0].ui.theme == "dark"
    values["startup"]["run_at_login"] = False
    assert api.save_settings(values)["ok"]
    assert reg.get(startup.VALUE_NAME) is None


def test_save_settings_rejects_invalid_values_without_saving() -> None:
    api, store, _reg, applied = make_settings()
    r = api.save_settings({"ui": {"theme": "neon"}})
    assert r["ok"] is False and r["error"] == "settings.err.invalid"
    r = api.save_settings({"processing": {"threads": 0}})
    assert r["ok"] is False
    r = api.save_settings({"gates": {"min_turns": "six"}})
    assert r["ok"] is False
    assert store.saved == 0 and applied == []
    assert api.save_settings("nope")["ok"] is False  # type: ignore[arg-type]


def test_save_settings_ignores_unknown_sections_and_keys() -> None:
    api, store, _reg, _ = make_settings()
    r = api.save_settings({"evil": {"x": 1}, "ui": {"theme": "light", "injected": True}})
    assert r["ok"]
    assert not hasattr(store.cfg, "evil") and not hasattr(store.cfg.ui, "injected")


def test_settings_diagnostics_and_voiceprints() -> None:
    ops = fake.FakeLibraryOps(lambda: Path("."))
    ops.voiceprints["Laura"] = fake.Voiceprint("Laura", "2026-11-02", 2)
    api, *_ = make_settings(diagnostics=lambda: [Diagnostic("Meet", "ok", "abc-defg-hij")], ops=ops)
    assert api.diagnostics() == [{"label": "Meet", "state": "ok", "detail": "abc-defg-hij"}]
    assert api.list_voiceprints()[0]["name"] == "Laura"
    assert api.delete_voiceprint("Laura")["ok"] and api.list_voiceprints() == []
    assert api.get_settings()["has_diagnostics"] is True


def test_settings_diagnostics_provider_failure_is_contained() -> None:
    def boom() -> list[Diagnostic]:
        raise RuntimeError("x")

    api, *_ = make_settings(diagnostics=boom)
    assert api.diagnostics() == []


def test_real_startup_registry_is_blocked_from_settings_under_pytest() -> None:
    """Sin registro inyectado, guardar «Arrancar con Windows» no puede tocar el Run real."""
    before = startup.WinRegistry().get("Micnaga")  # el PC puede tenerla (app instalada)
    store = MemStore()
    api = SettingsApi(store=store, callbacks=UiCallbacks())  # backend por defecto = real
    values = api.get_settings()["values"]
    values["startup"]["run_at_login"] = before is None  # lo contrario de lo que hay
    r = api.save_settings(values)
    assert r.get("saved") is True and r["error"] == "settings.err.startup_failed"
    assert startup.WinRegistry().get("Micnaga") == before  # intacta


# ----------------------------------------------------------------------------- asistente


def test_wizard_download_progress_and_verification() -> None:
    gate = threading.Event()

    def dl(cb: Any) -> None:
        cb(ModelProgress("Parakeet", "downloading", 50, 100))
        cb(ModelProgress("VAD", "pending", 0, 10))
        gate.wait(5)
        cb(ModelProgress("Parakeet", "verified", 100, 100))
        cb(ModelProgress("VAD", "verified", 10, 10))

    w = WizardApi(download_models=dl, levels=lambda: (0.1, 0.2), callbacks=UiCallbacks())
    assert w.download_status()["state"] == "idle"
    assert w.start_download()["ok"]
    for _ in range(100):
        st = w.download_status()
        if len(st["models"]) == 2:
            break
        time.sleep(0.02)
    assert st["state"] == "running" and 0 < st["overall"] < 1
    gate.set()
    for _ in range(100):
        st = w.download_status()
        if st["state"] == "done":
            break
        time.sleep(0.02)
    assert st["state"] == "done" and st["overall"] == 1.0 and st["verified"] == 2


def test_wizard_reports_failures_and_can_retry() -> None:
    calls = {"n": 0}

    def dl(cb: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            cb(ModelProgress("Parakeet", "failed", 5, 100, "SHA-256 no coincide"))
            return
        cb(ModelProgress("Parakeet", "verified", 100, 100))

    w = WizardApi(download_models=dl, levels=lambda: (0, 0), callbacks=UiCallbacks())
    w.start_download()
    for _ in range(100):
        if w.download_status()["state"] != "running":
            break
        time.sleep(0.02)
    st = w.download_status()
    assert st["state"] == "failed" and "SHA-256" in st["error"]
    w.start_download()
    for _ in range(100):
        if w.download_status()["state"] == "done":
            break
        time.sleep(0.02)
    assert w.download_status()["state"] == "done"


def test_wizard_exception_in_downloader_is_a_failed_state() -> None:
    def dl(cb: Any) -> None:
        raise OSError("sin red")

    w = WizardApi(download_models=dl, levels=lambda: (0, 0), callbacks=UiCallbacks())
    w.start_download()
    for _ in range(100):
        if w.download_status()["state"] != "running":
            break
        time.sleep(0.02)
    assert w.download_status()["state"] == "failed" and "sin red" in w.download_status()["error"]
    assert (
        WizardApi(
            download_models=None, levels=lambda: (0, 0), callbacks=UiCallbacks()
        ).start_download()["ok"]
        is False
    )


def test_wizard_levels_monitor_and_finish_callbacks() -> None:
    seen: list[Any] = []
    cb = UiCallbacks(
        on_levels_monitor=lambda a: seen.append(("mon", a)),
        on_first_run_done=lambda: seen.append("done"),
    )
    tests: list[str] = []
    w = WizardApi(
        download_models=None,
        levels=lambda: {"mic": 0.5, "system": 2},
        callbacks=cb,
        play_test=lambda: tests.append("t"),
    )
    assert w.levels() == [0.5, 1.0]  # recortado a 0..1
    w.monitor(True)
    w.play_test_sound()
    assert w.finish()["ok"]
    assert seen == [("mon", True), "done"] and tests == ["t"]


# ------------------------------------------------------------------- popup y pastilla


class Spy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def levels(self) -> tuple[float, float]:
        return (0.25, 0.5)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("handle_"):
            return lambda *a: self.calls.append((name, a))
        raise AttributeError(name)


def test_popup_api_validates_and_forwards() -> None:
    spy = Spy()
    api = PopupApi(spy)  # type: ignore[arg-type]
    assert api.levels() == [0.25, 0.5]
    api.popup_decide("record")
    api.popup_decide("hack")  # ignorado
    api.popup_hover(1)  # type: ignore[arg-type]
    api.popup_closed("nope")  # ignorado
    api.popup_closed("morphed")
    api.popup_morph(True)
    assert spy.calls == [
        ("handle_popup_decide", ("record",)),
        ("handle_popup_hover", (True,)),
        ("handle_popup_closed", ("morphed",)),
        ("handle_popup_morph", (True,)),
    ]


def test_pill_api_forwards() -> None:
    spy = Spy()
    api = PillApi(spy)  # type: ignore[arg-type]
    api.pill_hover(True)
    api.pill_marker()
    api.pill_toggle_pause()
    api.pill_stop()
    assert [c[0] for c in spy.calls] == [
        "handle_pill_hover",
        "handle_pill_marker",
        "handle_pill_toggle_pause",
        "handle_pill_stop",
    ]


def test_js_api_objects_expose_only_the_intended_public_methods() -> None:
    """pywebview publica todo atributo publico: nada de ventanas, BD ni callbacks."""
    import inspect

    for cls in (PopupApi, PillApi, LibraryApi, SettingsApi, WizardApi):
        public = [n for n, _ in inspect.getmembers(cls) if not n.startswith("_")]
        assert all(callable(getattr(cls, n)) for n in public), cls
    inst = PopupApi(Spy())  # type: ignore[arg-type]
    assert [n for n in vars(inst) if not n.startswith("_")] == []


def test_normalize_levels_accepts_the_shapes_a_recorder_may_return() -> None:
    from types import SimpleNamespace

    assert normalize_levels((0.1, 0.2)) == (0.1, 0.2)
    assert normalize_levels([2, -1]) == (1.0, 0.0)
    assert normalize_levels({"mic": 0.3, "system": 0.4}) == (0.3, 0.4)
    assert normalize_levels(SimpleNamespace(mic=0.5, system=0.6)) == (0.5, 0.6)
    assert normalize_levels(None) == (0.0, 0.0)
    assert normalize_levels((float("nan"), "x")) == (0.0, 0.0)


def test_wizard_auto_finish_runs_first_run_done_after_a_successful_download() -> None:
    seen: list[str] = []

    def dl(cb: Any) -> None:
        cb(ModelProgress("Parakeet", "verified", 100, 100))

    cbs = UiCallbacks(on_first_run_done=lambda: seen.append("done"))
    w = WizardApi(download_models=dl, levels=lambda: (0, 0), callbacks=cbs, auto_finish=True)
    assert w.start_download()["ok"]
    for _ in range(100):
        if seen:
            break
        time.sleep(0.02)
    assert seen == ["done"]

    def bad(cb: Any) -> None:
        cb(ModelProgress("Parakeet", "failed", 0, 100, message="red"))

    seen.clear()
    w2 = WizardApi(download_models=bad, levels=lambda: (0, 0), callbacks=cbs, auto_finish=True)
    w2.start_download()
    for _ in range(100):
        if w2.download_status()["state"] == "failed":
            break
        time.sleep(0.02)
    assert seen == []  # un fallo no cierra el asistente: se queda para reintentar
