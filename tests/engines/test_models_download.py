"""Descarga de modelos robusta: cortes, reintentos, cancelar, disco y fase «verificando».

Servidor HTTP local en un hilo (sin internet), con fallos programados por peticion.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from micnaga import models
from micnaga.config import Config
from micnaga.models import (
    DownloadCancelled,
    DownloadProgress,
    ModelError,
    ModelFile,
    ModelGroup,
    download_models,
)

PAYLOAD = (b"micnaga-robust-download-" * 4000)[:65536]


@dataclass
class Flaky:
    blob: bytes = PAYLOAD
    plan: list[str] = field(default_factory=list)
    """Una accion por peticion: ``cut`` (manda la mitad y cierra), ``503``, ``404``, ``ok``."""
    requests: list[str | None] = field(default_factory=list)
    port: int = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/m.bin"


@pytest.fixture
def flaky() -> Iterator[Flaky]:
    state = Flaky()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            rng = self.headers.get("Range")
            state.requests.append(rng)
            action = state.plan.pop(0) if state.plan else "ok"
            if action in ("503", "404"):
                self.send_error(int(action))
                return
            start = int(rng.split("=")[1].split("-")[0]) if rng else 0
            body = state.blob[start:]
            self.send_response(206 if rng else 200)
            if rng:
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(state.blob) - 1}/{len(state.blob)}"
                )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if action == "cut":
                self.wfile.write(body[: len(body) // 2])
                self.wfile.flush()
                self.close_connection = True
                return
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()


def manifest(server: Flaky) -> dict[str, ModelGroup]:
    f = ModelFile(
        "tiny", "m.bin", server.url, len(server.blob), hashlib.sha256(server.blob).hexdigest()
    )
    return {"tiny": ModelGroup("tiny", "tiny", "test", "local", (f,))}


def allow_local(url: str) -> bool:
    return url.startswith("http://127.0.0.1:")


def run(server: Flaky, **kw: Any) -> list[Path]:
    kw.setdefault("sleep", lambda _s: None)
    return download_models(
        Config(), groups=["tiny"], manifest=manifest(server), allow_url=allow_local, **kw
    )


def test_a_cut_connection_resumes_with_range(flaky: Flaky, tmp_models: Path) -> None:
    flaky.plan = ["cut"]
    sleeps: list[float] = []
    run(flaky, sleep=sleeps.append)
    assert (tmp_models / "tiny" / "m.bin").read_bytes() == PAYLOAD
    assert flaky.requests[0] is None and flaky.requests[1] == f"bytes={len(PAYLOAD) // 2}-"
    assert sleeps == [models.RETRY_BACKOFF_S[0]]


def test_server_errors_are_retried_with_backoff(flaky: Flaky, tmp_models: Path) -> None:
    flaky.plan = ["503", "503"]
    sleeps: list[float] = []
    run(flaky, sleep=sleeps.append)
    assert sleeps == list(models.RETRY_BACKOFF_S[:2])
    assert (tmp_models / "tiny" / "m.bin").is_file()


def test_retries_give_up_with_a_readable_key_and_keep_the_part(
    flaky: Flaky, tmp_models: Path
) -> None:
    flaky.plan = ["cut"] * 10
    with pytest.raises(ModelError) as exc:
        run(flaky)
    assert exc.value.key == "models.err.network"
    part = tmp_models / "tiny" / "m.bin.part"
    have = part.stat().st_size
    assert 0 < have < len(PAYLOAD)  # se reanuda luego
    flaky.plan = []
    run(flaky)  # «Reintentar» (o tras reiniciar la app): sigue donde iba
    assert flaky.requests[-1] == f"bytes={have}-"
    assert (tmp_models / "tiny" / "m.bin").read_bytes() == PAYLOAD


def test_cancel_event_stops_and_keeps_the_part(flaky: Flaky, tmp_models: Path) -> None:
    cancel = threading.Event()

    def progress(p: DownloadProgress) -> None:
        if p.phase == "download" and p.file_done > 0:
            cancel.set()

    with pytest.raises(DownloadCancelled) as exc:
        run(flaky, cancel=cancel, progress_cb=progress)
    assert exc.value.key == "models.err.cancelled"
    assert (tmp_models / "tiny" / "m.bin.part").is_file()
    assert not (tmp_models / "tiny" / "m.bin").exists()


def test_exception_from_the_progress_callback_aborts(flaky: Flaky, tmp_models: Path) -> None:
    class Stop(Exception):
        pass

    def progress(p: DownloadProgress) -> None:
        if p.file_done > 0:
            raise Stop

    with pytest.raises(Stop):
        run(flaky, progress_cb=progress)
    assert len(flaky.requests) == 1  # sin reintentos: lo pidio la UI


def test_not_enough_disk_space_fails_before_any_request(
    flaky: Flaky, tmp_models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Usage:
        free = 1000

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage())
    with pytest.raises(ModelError) as exc:
        run(flaky)
    assert exc.value.key == "models.err.disk"
    assert exc.value.info["free"] == 1000 and exc.value.info["need"] > len(PAYLOAD)
    assert flaky.requests == []


def test_verify_phase_is_reported_before_done_and_on_skip(flaky: Flaky, tmp_models: Path) -> None:
    phases: list[str] = []
    run(flaky, progress_cb=lambda p: phases.append(p.phase))
    assert phases[-2:] == ["verify", "done"]
    phases.clear()
    run(flaky, progress_cb=lambda p: phases.append(p.phase))
    assert phases == ["verify", "skip"]


def test_a_different_announced_size_is_not_retried(flaky: Flaky, tmp_models: Path) -> None:
    flaky.blob = PAYLOAD + b"extra"
    m = manifest(flaky)
    f = m["tiny"].files[0]
    m["tiny"] = ModelGroup(
        "tiny", "t", "t", "t", (ModelFile("tiny", "m.bin", f.url, len(PAYLOAD), f.sha256),)
    )
    sleeps: list[float] = []
    with pytest.raises(ModelError) as exc:
        download_models(
            Config(), groups=["tiny"], manifest=m, allow_url=allow_local, sleep=sleeps.append
        )
    assert exc.value.key == "models.err.checksum" and not sleeps


def test_client_errors_are_not_retried(flaky: Flaky, tmp_models: Path) -> None:
    flaky.plan = ["404"]
    sleeps: list[float] = []
    with pytest.raises(ModelError) as exc:
        run(flaky, sleep=sleeps.append)
    assert exc.value.key == "models.err.http" and "404" in str(exc.value) and not sleeps


def test_network_down_retries_then_gives_up(tmp_models: Path) -> None:
    class Down(urllib.request.BaseHandler):
        handler_order = 100

        def http_open(self, req: urllib.request.Request) -> None:
            raise urllib.error.URLError("no route to host")

    f = ModelFile("tiny", "m.bin", "http://127.0.0.1:9/m.bin", len(PAYLOAD), "0" * 64)
    sleeps: list[float] = []
    with pytest.raises(ModelError) as exc:
        download_models(
            Config(),
            groups=["tiny"],
            manifest={"tiny": ModelGroup("tiny", "t", "t", "t", (f,))},
            allow_url=allow_local,
            opener=urllib.request.build_opener(Down()),
            sleep=sleeps.append,
        )
    assert exc.value.key == "models.err.network"
    assert sleeps == list(models.RETRY_BACKOFF_S)
