"""Puentes nuevos: descarga de modelos (cancelar, velocidad, errores legibles) en el asistente y en
Ajustes, y version/actualizaciones en «Acerca de». Sin red ni ventanas."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from micnaga import updates
from micnaga.config import Config
from micnaga.models import ModelError
from micnaga.ui import bridge
from micnaga.ui.bridge import ModelDownloadJob, SettingsApi, WizardApi, human_bytes
from micnaga.ui.contracts import ModelProgress, UiCallbacks, set_app_action


class MemStore:
    def __init__(self) -> None:
        self.cfg = Config()

    def load(self) -> Config:
        return self.cfg

    def save(self, config: Config) -> None:
        self.cfg = config


def wait_for(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < end, "timeout"
        time.sleep(0.01)


def test_human_bytes_reads_in_spanish() -> None:
    assert human_bytes(1_617_884_929) == "1,6 GB"
    assert human_bytes(734_000_000) == "734 MB"
    assert human_bytes(2_327_524) == "2,3 MB"
    assert human_bytes(12) == "12 B"


def test_speed_and_eta_count_only_network_bytes() -> None:
    now = {"t": 0.0}
    gate = threading.Event()

    def dl(cb: Callable[[ModelProgress], None]) -> None:
        cb(ModelProgress("VAD", "skipped", 2_000_000, 2_000_000))  # ya estaba: no es velocidad
        cb(ModelProgress("Whisper", "downloading", 0, 100_000_000))
        now["t"] = 2.0
        cb(ModelProgress("Whisper", "downloading", 20_000_000, 100_000_000))
        gate.wait(5)

    job = ModelDownloadJob(dl, clock=lambda: now["t"])
    job.start()
    wait_for(lambda: job.status()["done"] == 22_000_000)
    st = job.status()
    assert st["speed"] == 10_000_000 and st["speed_text"] == "10 MB"
    assert st["eta_s"] == 8  # 80 MB a 10 MB/s
    assert st["done_text"] == "22 MB" and st["total_text"] == "102 MB"
    gate.set()
    wait_for(lambda: job.status()["state"] == "done")


def test_cancel_stops_the_download_from_the_progress_callback() -> None:
    started = threading.Event()
    seen: list[str] = []

    def dl(cb: Callable[[ModelProgress], None]) -> None:
        for i in range(500):
            cb(ModelProgress("Whisper", "downloading", i, 1000))
            started.set()
            time.sleep(0.005)
        seen.append("ran to the end")

    w = WizardApi(download_models=dl, levels=lambda: (0, 0), callbacks=UiCallbacks())
    w.start_download()
    started.wait(5)
    assert w.cancel_download()["ok"]
    wait_for(lambda: w.download_status()["state"] != "running")
    assert w.download_status()["state"] == "cancelled" and not seen
    w.start_download()  # «Reanudar»
    wait_for(lambda: w.download_status()["state"] == "done", 10)


def test_model_errors_reach_the_ui_as_a_key_with_readable_values() -> None:
    def dl(cb: Callable[[ModelProgress], None]) -> None:
        raise ModelError("no space", "models.err.disk", need=1_700_000_000, free=300_000_000)

    w = WizardApi(download_models=dl, levels=lambda: (0, 0), callbacks=UiCallbacks())
    w.start_download()
    wait_for(lambda: w.download_status()["state"] == "failed")
    st = w.download_status()
    assert st["error_key"] == "models.err.disk"
    assert st["error_args"] == {"need": "1,7 GB", "free": "300 MB"}
    assert "no space" in st["error"]


def test_cancelled_model_error_is_a_pause_not_a_failure() -> None:
    from micnaga.models import DownloadCancelled

    def dl(cb: Callable[[ModelProgress], None]) -> None:
        raise DownloadCancelled()

    job = ModelDownloadJob(dl)
    job.start()
    wait_for(lambda: job.status()["state"] != "running")
    assert job.status()["state"] == "cancelled"


# ------------------------------------------------------------------ Ajustes: modelos


@pytest.fixture
def no_settings_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "_SETTINGS_JOB", None)


def test_settings_models_status_and_download(tmp_models: Path, no_settings_job: None) -> None:
    api = SettingsApi(store=MemStore(), callbacks=UiCallbacks())
    st = api.models_status()
    assert not st["ready"] and st["need_text"].endswith("GB") and not st["can_download"]
    assert api.models_download()["error"] == "wizard.err.no_downloader"

    ran = threading.Event()

    def dl(cb: Callable[[ModelProgress], None]) -> None:
        cb(ModelProgress("Whisper", "verified", 10, 10))
        ran.set()

    set_app_action(bridge.ACTION_MODEL_DOWNLOADER, lambda: dl)
    try:
        assert api.models_status()["can_download"]
        assert api.models_download()["ok"]
        assert ran.wait(5)
        wait_for(lambda: api.models_status()["job"]["state"] == "done")
    finally:
        set_app_action(bridge.ACTION_MODEL_DOWNLOADER, None)


# ------------------------------------------------------------------ Ajustes: actualizaciones


def test_update_status_without_a_service_still_shows_the_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(updates, "_SERVICE", None)
    api = SettingsApi(store=MemStore(), callbacks=UiCallbacks())
    st = api.update_status()
    from micnaga import __version__

    assert st["current"] == __version__ and st["state"] == "unavailable"
    assert api.update_check()["state"] == "unavailable"
    assert api.update_seen()["ok"]


def test_update_methods_delegate_to_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    class Svc:
        def status(self) -> dict[str, Any]:
            return {"state": "available"}

        def check(self, *, manual: bool) -> dict[str, Any]:
            calls.append(("check", manual))
            return self.status()

        def request_install(self) -> dict[str, Any]:
            calls.append("install")
            return self.status()

        def skip(self) -> dict[str, Any]:
            calls.append("skip")
            return self.status()

        def mark_whats_new_seen(self) -> None:
            calls.append("seen")

    monkeypatch.setattr(updates, "_SERVICE", Svc())
    api = SettingsApi(store=MemStore(), callbacks=UiCallbacks())
    api.update_check()
    api.update_install()
    api.update_skip()
    api.update_seen()
    assert calls == [("check", True), "install", "skip", "seen"]
