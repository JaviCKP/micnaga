"""Manifiesto y descarga de modelos. Sin internet: servidor HTTP local en un hilo."""

from __future__ import annotations

import hashlib
import re
import threading
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from micnaga import models
from micnaga.config import Config
from micnaga.models import (
    MANIFEST,
    DownloadProgress,
    ModelError,
    ModelFile,
    ModelGroup,
    download_models,
    missing_models,
    required_groups,
    verify_models,
)

HEX64 = re.compile(r"^[0-9a-f]{64}$")


# --------------------------------------------------------------------------- manifiesto


def test_manifest_entries_are_pinned_and_complete() -> None:
    seen: set[str] = set()
    assert not set(MANIFEST) & set(models.CANDIDATE_MANIFEST)
    for gid, group in [*MANIFEST.items(), *models.CANDIDATE_MANIFEST.items()]:
        assert group.id == gid and group.files
        for f in group.files:
            assert f.group == gid
            assert f.rel_path not in seen
            seen.add(f.rel_path)
            u = urlparse(f.url)
            assert u.scheme == "https" and u.hostname == "huggingface.co"
            # commit fijado (40 hex), nunca una rama
            assert re.search(r"/resolve/[0-9a-f]{40}/", f.url), f.url
            assert "/main/" not in f.url
            assert f.url.endswith("/" + f.name)
            assert HEX64.match(f.sha256) and f.size > 0


def test_manifest_total_size_is_about_a_gigabyte_for_defaults() -> None:
    total = models.total_size(Config())
    assert 0.6e9 < total < 1.1e9  # Parakeet int8 + Silero + WeSpeaker


def test_required_groups_follow_config() -> None:
    cfg = Config()
    assert required_groups(cfg) == [
        models.SILERO_GROUP,
        models.PARAKEET_GROUP,
        models.WESPEAKER_GROUP,
    ]
    cfg.processing.asr_engine = "whisper"
    cfg.processing.diarization = "pyannote"
    assert required_groups(cfg) == [models.SILERO_GROUP, models.WHISPER_SMALL_GROUP]
    assert models.WHISPER_SMALL_GROUP in required_groups(Config(), include_optional=True)
    # los modelos evaluados y no adoptados nunca se piden
    every = set(required_groups(Config(), include_optional=True))
    assert not every & set(models.CANDIDATE_MANIFEST)


def test_whisper_group_unknown_size() -> None:
    assert models.whisper_group("small") == models.WHISPER_SMALL_GROUP
    with pytest.raises(ModelError):
        models.whisper_group("medium")


def test_missing_models_reports_everything_when_empty(tmp_models: Path) -> None:
    cfg = Config()
    names = {f.rel_path for f in missing_models(cfg)}
    assert f"{models.SILERO_GROUP}/silero_vad.onnx" in names
    assert f"{models.PARAKEET_GROUP}/encoder-model.int8.onnx" in names
    assert not any(n.startswith(models.WHISPER_SMALL_GROUP) for n in names)  # opcional


# ---------------------------------------------------------------- servidor HTTP de prueba


@dataclass
class Served:
    blobs: dict[str, bytes]
    support_range: bool = True
    requests: list[tuple[str, str | None]] = field(default_factory=list)
    redirects: dict[str, str] = field(default_factory=dict)
    port: int = 0

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"


@pytest.fixture
def server() -> Iterator[Served]:
    state = Served(blobs={})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            name = self.path.lstrip("/")
            state.requests.append((name, self.headers.get("Range")))
            if name in state.redirects:
                self.send_response(302)
                self.send_header("Location", state.redirects[name])
                self.end_headers()
                return
            blob = state.blobs.get(name)
            if blob is None:
                self.send_error(404)
                return
            rng = self.headers.get("Range")
            if rng and state.support_range:
                start = int(rng.split("=")[1].split("-")[0])
                if start >= len(blob):
                    self.send_error(416)
                    return
                body = blob[start:]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(blob) - 1}/{len(blob)}")
            else:
                body = blob
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()


def make_manifest(
    server: Served, files: dict[str, bytes], group: str = "tiny"
) -> dict[str, ModelGroup]:
    server.blobs.update(files)
    return {
        group: ModelGroup(
            id=group,
            title="tiny",
            license="test",
            source="local",
            files=tuple(
                ModelFile(
                    group=group,
                    name=name,
                    url=server.url(name),
                    size=len(blob),
                    sha256=hashlib.sha256(blob).hexdigest(),
                )
                for name, blob in files.items()
            ),
        )
    }


def allow_local(url: str) -> bool:
    return url.startswith("http://127.0.0.1:")


PAYLOAD = (b"micnaga-test-model-" * 1000)[:12345]


def test_download_verifies_and_renames_atomically(server: Served, tmp_models: Path) -> None:
    manifest = make_manifest(server, {"a.bin": PAYLOAD, "b.txt": b"hello"})
    events: list[DownloadProgress] = []
    done = download_models(
        Config(), events.append, groups=["tiny"], manifest=manifest, allow_url=allow_local
    )
    assert {p.name for p in done} == {"a.bin", "b.txt"}
    assert (tmp_models / "tiny" / "a.bin").read_bytes() == PAYLOAD
    assert not list(tmp_models.rglob("*.part"))
    assert events[-1].overall_done == events[-1].overall_total == len(PAYLOAD) + 5
    assert events[-1].phase == "done"
    assert any(e.phase == "download" and 0 < e.file_done <= e.file_total for e in events)
    assert verify_models(Config(), groups=["tiny"], manifest=manifest) == []
    assert missing_models(Config(), groups=["tiny"], manifest=manifest) == []


def test_existing_valid_files_are_skipped_without_network(server: Served, tmp_models: Path) -> None:
    manifest = make_manifest(server, {"a.bin": PAYLOAD})
    download_models(Config(), groups=["tiny"], manifest=manifest, allow_url=allow_local)
    server.requests.clear()
    events: list[DownloadProgress] = []
    again = download_models(
        Config(), events.append, groups=["tiny"], manifest=manifest, allow_url=allow_local
    )
    assert again == [] and server.requests == []
    assert events[-1].phase == "skip"


def test_sha_mismatch_discards_file_and_raises(server: Served, tmp_models: Path) -> None:
    manifest = make_manifest(server, {"a.bin": PAYLOAD})
    bad = ModelFile(
        group="tiny", name="a.bin", url=server.url("a.bin"), size=len(PAYLOAD), sha256="0" * 64
    )
    manifest["tiny"] = ModelGroup("tiny", "t", "t", "t", (bad,))
    with pytest.raises(ModelError, match="mismatch"):
        download_models(Config(), groups=["tiny"], manifest=manifest, allow_url=allow_local)
    assert not (tmp_models / "tiny" / "a.bin").exists()
    assert not list(tmp_models.rglob("*.part"))


def test_size_mismatch_is_rejected(server: Served, tmp_models: Path) -> None:
    manifest = make_manifest(server, {"a.bin": PAYLOAD})
    f = manifest["tiny"].files[0]
    manifest["tiny"] = ModelGroup(
        "tiny", "t", "t", "t", (ModelFile("tiny", "a.bin", f.url, f.size + 1, f.sha256),)
    )
    with pytest.raises(ModelError):
        download_models(Config(), groups=["tiny"], manifest=manifest, allow_url=allow_local)


def test_resume_uses_range_and_completes(server: Served, tmp_models: Path) -> None:
    manifest = make_manifest(server, {"a.bin": PAYLOAD})
    part = tmp_models / "tiny" / "a.bin.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(PAYLOAD[:5000])
    download_models(Config(), groups=["tiny"], manifest=manifest, allow_url=allow_local)
    assert (tmp_models / "tiny" / "a.bin").read_bytes() == PAYLOAD
    assert server.requests[0] == ("a.bin", "bytes=5000-")
    assert not part.exists()


def test_resume_with_server_that_ignores_range(server: Served, tmp_models: Path) -> None:
    server.support_range = False
    manifest = make_manifest(server, {"a.bin": PAYLOAD})
    part = tmp_models / "tiny" / "a.bin.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(PAYLOAD[:5000])
    download_models(Config(), groups=["tiny"], manifest=manifest, allow_url=allow_local)
    assert (tmp_models / "tiny" / "a.bin").read_bytes() == PAYLOAD


def test_corrupt_part_is_restarted(server: Served, tmp_models: Path) -> None:
    manifest = make_manifest(server, {"a.bin": PAYLOAD})
    part = tmp_models / "tiny" / "a.bin.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"X" * 5000)  # prefijo corrupto
    download_models(Config(), groups=["tiny"], manifest=manifest, allow_url=allow_local)
    assert (tmp_models / "tiny" / "a.bin").read_bytes() == PAYLOAD


def test_corrupt_final_file_is_replaced_and_verify_detects_it(
    server: Served, tmp_models: Path
) -> None:
    manifest = make_manifest(server, {"a.bin": PAYLOAD})
    final = tmp_models / "tiny" / "a.bin"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"Y" * len(PAYLOAD))  # mismo tamano, contenido distinto
    assert missing_models(Config(), groups=["tiny"], manifest=manifest) == []  # solo mira tamano
    assert len(verify_models(Config(), groups=["tiny"], manifest=manifest)) == 1
    download_models(Config(), groups=["tiny"], manifest=manifest, allow_url=allow_local)
    assert final.read_bytes() == PAYLOAD


def test_http_error_is_a_model_error(server: Served, tmp_models: Path) -> None:
    manifest = make_manifest(server, {"a.bin": PAYLOAD})
    server.blobs.clear()  # 404
    with pytest.raises(ModelError, match="404"):
        download_models(Config(), groups=["tiny"], manifest=manifest, allow_url=allow_local)


def test_default_policy_rejects_non_https_and_foreign_hosts(
    server: Served, tmp_models: Path
) -> None:
    manifest = make_manifest(server, {"a.bin": PAYLOAD})
    with pytest.raises(ModelError, match="not allowed"):
        download_models(Config(), groups=["tiny"], manifest=manifest)  # politica por defecto
    assert server.requests == []
    assert models._host_allowed("https://huggingface.co/x")
    assert models._host_allowed("https://us.aws.cdn.hf.co/x")
    assert not models._host_allowed("http://huggingface.co/x")
    assert not models._host_allowed("https://evilhuggingface.co/x")
    assert not models._host_allowed("https://huggingface.co.evil.com/x")
    assert not models._host_allowed("https://example.com/x")


def test_redirect_to_a_disallowed_host_is_blocked(server: Served, tmp_models: Path) -> None:
    manifest = make_manifest(server, {"a.bin": PAYLOAD})
    server.redirects["a.bin"] = "http://evil.example/a.bin"
    with pytest.raises(ModelError, match="redirect"):
        download_models(Config(), groups=["tiny"], manifest=manifest, allow_url=allow_local)


def test_redirect_to_an_allowed_host_is_followed(server: Served, tmp_models: Path) -> None:
    manifest = make_manifest(server, {"real.bin": PAYLOAD})
    f = manifest["tiny"].files[0]
    server.redirects["a.bin"] = server.url("real.bin")
    manifest["tiny"] = ModelGroup(
        "tiny", "t", "t", "t", (ModelFile("tiny", "a.bin", server.url("a.bin"), f.size, f.sha256),)
    )
    download_models(Config(), groups=["tiny"], manifest=manifest, allow_url=allow_local)
    assert (tmp_models / "tiny" / "a.bin").read_bytes() == PAYLOAD


def test_unknown_group_is_an_error(tmp_models: Path) -> None:
    with pytest.raises(ModelError, match="unknown model group"):
        missing_models(Config(), groups=["nope"])


def test_require_models_lists_missing_files(tmp_models: Path) -> None:
    from micnaga.registry import EngineUnavailableError

    with pytest.raises(EngineUnavailableError, match=r"encoder-model.int8.onnx"):
        models.require_models(Config(), models.PARAKEET_GROUP)


def test_no_unexpected_network_calls_in_unit_tests(
    monkeypatch: pytest.MonkeyPatch, tmp_models: Path
) -> None:
    """Defensa: ``missing_models``/``verify_models`` nunca abren conexiones."""

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", boom)
    missing_models(Config())
    verify_models(Config())
