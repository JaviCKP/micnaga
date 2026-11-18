"""Servidor local: token, Host, recorrido de directorios, lista blanca de audio y Range/206."""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from micnaga import paths
from micnaga.ui.media import AUDIO_TRACKS, LocalServer, parse_range, valid_folder_name


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LocalServer]:
    root = tmp_path / "meetings"
    (root / "2026-11-18_1030_demo").mkdir(parents=True)
    (root / "2026-11-18_1030_demo" / paths.AUDIO_ME_FLAC).write_bytes(
        bytes(range(256)) * 4
    )  # 1024 B
    (root / "2026-11-18_1030_demo" / paths.AUDIO_REMOTE_FLAC).write_bytes(b"R" * 500)
    (root / "2026-11-18_1030_demo" / paths.TRANSCRIPT_JSON).write_text("{}", encoding="utf-8")
    (root / "secret.txt").write_text("top secret", encoding="utf-8")
    web = tmp_path / "web"
    (web / "i18n").mkdir(parents=True)
    (web / "page.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    (web / "i18n" / "es.json").write_text('{"a": "b"}', encoding="utf-8")
    (tmp_path / "outside.txt").write_text("nope", encoding="utf-8")
    with LocalServer(web_root=web, meetings_root=lambda: root) as srv:
        yield srv


def get(
    srv: LocalServer,
    path: str,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    host: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    hdrs = dict(headers or {})
    if host is not None:
        hdrs["Host"] = host
    conn.request(method, path, headers=hdrs)
    r = conn.getresponse()
    body = r.read()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, body)
    conn.close()
    return out


def test_serves_pages_with_security_headers(server: LocalServer) -> None:
    status, h, body = get(server, f"/{server.token}/page.html")
    assert status == 200 and b"<title>x" in body
    assert "default-src 'self'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"
    assert h["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in h  # sin CORS: ninguna web ajena lo lee


def test_json_and_js_have_utf8_types(server: LocalServer) -> None:
    status, h, _ = get(server, f"/{server.token}/i18n/es.json")
    assert status == 200 and h["content-type"].startswith("application/json")


def test_token_is_required(server: LocalServer) -> None:
    assert get(server, "/page.html")[0] == 404
    assert get(server, "/wrongtoken/page.html")[0] == 404
    assert get(server, f"/{server.token}x/page.html")[0] == 404


def test_host_header_must_be_local(server: LocalServer) -> None:
    """Frena el DNS rebinding: una pagina remota que apunte a 127.0.0.1 trae otro Host."""
    assert get(server, f"/{server.token}/page.html", host="evil.example.com")[0] == 403
    assert get(server, f"/{server.token}/page.html", host=f"localhost:{server.port}")[0] == 200


@pytest.mark.parametrize(
    "evil", ["../outside.txt", "%2e%2e/outside.txt", "..%2foutside.txt", "i18n/../../outside.txt"]
)
def test_static_path_traversal_is_blocked(server: LocalServer, evil: str) -> None:
    status, _h, body = get(server, f"/{server.token}/{evil}")
    assert status == 404 and b"nope" not in body


def test_audio_full_and_range_requests(server: LocalServer) -> None:
    base = f"/{server.token}/m/2026-11-18_1030_demo/yo"
    status, h, body = get(server, base)
    assert status == 200 and len(body) == 1024 and h["accept-ranges"] == "bytes"
    assert h["content-type"] == "audio/flac"
    status, h, body = get(server, base, {"Range": "bytes=100-199"})
    assert (
        status == 206
        and body == bytes(range(256))[100:200]
        and h["content-range"] == "bytes 100-199/1024"
    )
    status, h, body = get(server, base, {"Range": "bytes=1000-"})
    assert status == 206 and len(body) == 24 and h["content-range"] == "bytes 1000-1023/1024"
    status, _h, body = get(server, base, {"Range": "bytes=-10"})
    assert status == 206 and len(body) == 10
    assert get(server, base, {"Range": "bytes=5000-6000"})[0] == 416
    assert get(server, base, {"Range": "garbage"})[0] == 416
    assert get(server, base, method="HEAD")[0] == 200


def test_audio_is_whitelisted_and_confined(server: LocalServer) -> None:
    t = server.token
    assert get(server, f"/{t}/m/2026-11-18_1030_demo/remoto")[0] == 200
    assert (
        get(server, f"/{t}/m/2026-11-18_1030_demo/transcripcion.json")[0] == 404
    )  # solo los 2 FLAC
    assert get(server, f"/{t}/m/2026-11-18_1030_demo/capture")[0] == 404
    assert get(server, f"/{t}/m/..%2f/yo")[0] == 404
    assert get(server, f"/{t}/m/%2e%2e/yo")[0] == 404
    assert get(server, f"/{t}/m/nope/yo")[0] == 404


def test_audio_url_builder_validates_input(server: LocalServer) -> None:
    assert server.audio_url("2026-11-18_1030_demo", "yo").endswith("/m/2026-11-18_1030_demo/yo")
    for bad in ("../x", "a/b", "", "..", ".hidden", "a:b"):
        with pytest.raises(ValueError):
            server.audio_url(bad, "yo")
    with pytest.raises(ValueError):
        server.audio_url("ok", "system")
    assert set(AUDIO_TRACKS) == {"yo", "remoto"}


def test_post_api_is_disabled_by_default(server: LocalServer) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("POST", f"/{server.token}/api/library/list_meetings", body="[]")
    assert conn.getresponse().status == 404


def test_dev_api_only_calls_public_methods(tmp_path: Path) -> None:
    class Api:
        def hello(self, name: str) -> dict[str, str]:
            return {"hi": name}

        def _secret(self) -> str:
            return "x"

    with LocalServer(web_root=tmp_path, dev_api={"s": Api()}) as srv:

        def post(p: str, body: str) -> tuple[int, bytes]:
            c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            c.request(
                "POST",
                f"/{srv.token}/api/{p}",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            r = c.getresponse()
            return r.status, r.read()

        status, body = post("s/hello", json.dumps(["Laura"]))
        assert status == 200 and json.loads(body) == {"hi": "Laura"}
        assert post("s/_secret", "[]")[0] == 404
        assert post("s/__class__", "[]")[0] == 404
        assert post("nope/hello", "[]")[0] == 404


def test_parse_range_cases() -> None:
    assert parse_range(None, 100) == (0, 99)
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-5", 100) == (95, 99)
    assert parse_range("bytes=0-1000", 100) == (0, 99)
    assert parse_range("bytes=100-", 100) is None
    assert parse_range("bytes=5-2", 100) is None
    assert parse_range("bytes=-", 100) is None
    assert parse_range("bytes=0-1,5-6", 100) is None
    assert parse_range(None, 0) is None


def test_valid_folder_name() -> None:
    assert valid_folder_name("2026-11-18_1030_revision-semanal")
    assert not any(
        valid_folder_name(n) for n in ("", ".", "..", ".git", "a/b", "a\\b", "c:x", "a\x00b")
    )
