"""Actualizaciones (``micnaga.updates``) con una capa HTTP falsa: nunca se toca la red."""

from __future__ import annotations

import email.message
import hashlib
import io
import json
import time
import urllib.error
import urllib.request
import urllib.response
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from micnaga import updates
from micnaga.updates import (
    ReleaseInfo,
    SignatureInfo,
    UpdateHooks,
    UpdateService,
    is_newer,
    parse_checksums,
    parse_release,
    parse_version,
    sanitize_notes,
    signature_verdict,
    url_allowed,
)

ASSET = "https://github.com/JaviCKP/micnaga/releases/download/v0.2.0/Micnaga-Setup-0.2.0.exe"
CDN = "https://release-assets.githubusercontent.com/x/Micnaga-Setup-0.2.0.exe"
SUMS = "https://github.com/JaviCKP/micnaga/releases/download/v0.2.0/SHA256SUMS.txt"
PAYLOAD = bytes(range(256)) * 400  # 100 KiB de «instalador»
SHA = hashlib.sha256(PAYLOAD).hexdigest()


Route = Callable[[urllib.request.Request], tuple[int, dict[str, str], bytes]]


class FakeHttps(urllib.request.BaseHandler):
    """Rutas ``url -> callable(req) -> (status, headers, body)``; registra cada peticion."""

    handler_order = 100

    def __init__(self) -> None:
        self.routes: dict[
            str, Callable[[urllib.request.Request], tuple[int, dict[str, str], bytes]]
        ]
        self.routes = {}
        self.requests: list[urllib.request.Request] = []

    def https_open(self, req: urllib.request.Request) -> Any:
        self.requests.append(req)
        route = self.routes.get(req.full_url)
        if route is None:
            raise urllib.error.URLError("offline")
        status, headers, body = route(req)
        msg = email.message.Message()
        for k, v in headers.items():
            msg[k] = v
        if status >= 400 or status == 304:
            raise urllib.error.HTTPError(req.full_url, status, "err", msg, io.BytesIO(body))
        resp = urllib.response.addinfourl(io.BytesIO(body), msg, req.full_url, status)
        resp.msg = "OK"  # type: ignore[attr-defined]
        return resp


def opener(fake: FakeHttps) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(updates._StrictRedirects(), fake)


def release_json(version: str = "0.2.0", **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-11-30T10:00:00Z",
        "body": "## Novedades\n- **Iconos** nuevos\n- Ver [notas](https://example.com)\n",
        "assets": [
            {
                "name": f"Micnaga-Setup-{version}.exe",
                "size": len(PAYLOAD),
                "state": "uploaded",
                "digest": f"sha256:{SHA}",
                "browser_download_url": ASSET.replace("0.2.0", version),
            },
            {"name": "SHA256SUMS.txt", "size": 90, "browser_download_url": SUMS},
        ],
    }
    data.update(extra)
    return data


def serve_latest(fake: FakeHttps, data: dict[str, Any], etag: str = '"e1"') -> None:
    def route(req: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
        if req.get_header("If-none-match") == etag:
            return 304, {}, b""
        return 200, {"ETag": etag}, json.dumps(data).encode()

    fake.routes[updates.API_LATEST] = route


def serve_asset(fake: FakeHttps, payload: bytes = PAYLOAD, *, sums: str | None = None) -> None:
    fake.routes[ASSET] = lambda _r: (302, {"Location": CDN}, b"")

    def cdn(req: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
        rng = req.get_header("Range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            return 206, {}, payload[start:]
        return 200, {}, payload

    fake.routes[CDN] = cdn
    text = sums if sums is not None else f"{SHA}  Micnaga-Setup-0.2.0.exe\n"
    fake.routes[SUMS] = lambda _r: (200, {}, text.encode())


def make_service(
    tmp_path: Path, fake: FakeHttps, hooks: UpdateHooks | None = None, **kw: Any
) -> UpdateService:
    kw.setdefault("signature", lambda _p: SignatureInfo("NotSigned"))
    kw.setdefault("can_install", True)
    kw.setdefault("sleep", lambda _s: None)
    return UpdateService(
        hooks,
        current="0.1.1",
        opener=opener(fake),
        state_path=tmp_path / "updates.json",
        download_dir=tmp_path / "dl",
        **kw,
    )


# ------------------------------------------------------------------------ funciones puras


def test_versions_compare_semantically_and_reject_prereleases() -> None:
    assert parse_version("v0.10.0") == (0, 10, 0)
    assert parse_version("1.2.3-beta") is None and parse_version(None) is None
    assert is_newer("0.10.0", "0.9.9") and not is_newer("0.1.1", "0.1.1")
    assert not is_newer("garbage", "0.1.0")


def test_parse_release_reads_the_installer_digest_and_checksums() -> None:
    rel = parse_release(release_json())
    assert rel is not None
    assert rel.version == "0.2.0" and rel.asset_size == len(PAYLOAD)
    assert rel.digest == SHA and rel.checksums_url == SUMS
    assert rel.notes.startswith("Novedades\n• Iconos nuevos\n• Ver notas")


@pytest.mark.parametrize(
    "patch",
    [
        {"draft": True},
        {"prerelease": True},
        {"tag_name": "v0.2.0-rc1"},
        {"assets": []},
        {
            "assets": [
                {
                    "name": "Micnaga-Setup-0.2.0.exe",
                    "size": 5,
                    "browser_download_url": "https://evil.example/x.exe",
                }
            ]
        },
        {"assets": [{"name": "Micnaga-Setup-0.2.0.exe", "size": 0, "browser_download_url": ASSET}]},
    ],
)
def test_parse_release_rejects_unusable_releases(patch: dict[str, Any]) -> None:
    assert parse_release(release_json(**patch)) is None


def test_checksums_file_formats() -> None:
    text = f"{SHA}  Micnaga-Setup-0.2.0.exe\n{'0' * 64} *other.exe\n"
    assert parse_checksums(text, "Micnaga-Setup-0.2.0.exe") == SHA
    assert parse_checksums(text, "other.exe") == "0" * 64
    assert parse_checksums("nonsense", "x") is None


def test_only_github_https_hosts_are_allowed() -> None:
    assert url_allowed(ASSET) and url_allowed(CDN) and url_allowed(updates.API_LATEST)
    assert not url_allowed("http://github.com/x")
    assert not url_allowed("https://github.com.evil.example/x")
    assert not url_allowed("https://evilgithubusercontent.com/x")


def test_notes_are_plain_short_text() -> None:
    body = (
        "<script>alert(1)</script>\n# Titulo\n![img](https://x/y.png)\n"
        "* uno con `codigo` y _enfasis_\n‮oculto\n" + "\n".join(f"- l{i}" for i in range(40))
    )
    out = sanitize_notes(body)
    assert "<" not in out and "http" not in out and "‮" not in out
    assert out.splitlines()[0] == "alert(1)"
    assert "• uno con codigo y enfasis" in out
    assert out.endswith("…") and len(out.splitlines()) <= updates.NOTES_MAX_LINES


def test_signature_policy() -> None:
    valid = SignatureInfo("Valid", "CN=Bitnaga S.L., O=Bitnaga", "AB")
    other = SignatureInfo("Valid", "CN=Someone Else", "CD")
    unsigned = SignatureInfo("NotSigned")
    broken = SignatureInfo("HashMismatch", "CN=Bitnaga S.L.")
    assert valid.common_name == "Bitnaga S.L."
    assert signature_verdict(unsigned, None) is None  # hoy: sin certificado, se permite
    assert signature_verdict(unsigned, None, require=True) == "updates.err.unsigned"
    assert signature_verdict(unsigned, valid) == "updates.err.unsigned"  # firmada -> sin firmar
    assert signature_verdict(broken, None) == "updates.err.signature"
    assert signature_verdict(valid, valid) is None
    assert signature_verdict(other, valid) == "updates.err.publisher"
    assert signature_verdict(valid, None, publisher="Otra") == "updates.err.publisher"
    unknown = SignatureInfo("Unknown")
    assert signature_verdict(unknown, None) == "updates.err.signature"
    assert signature_verdict(unknown, valid) == "updates.err.signature"
    assert signature_verdict(unsigned, unknown) == "updates.err.signature"


def test_authenticode_passes_the_path_by_environment(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    class Out:
        stdout = '{"status":"Valid","subject":"CN=X","thumbprint":"T"}'

    def run(argv: list[str], **kw: Any) -> Out:
        seen["argv"], seen["env"] = argv, kw["env"]
        return Out()

    target = tmp_path / "a'b; Remove-Item x.exe"
    info = updates.authenticode(target, run=run)
    if info.status == "Unknown":  # fuera de Windows
        return
    assert info == SignatureInfo("Valid", "CN=X", "T")
    assert str(target) not in " ".join(seen["argv"])
    assert seen["env"]["MICNAGA_VERIFY_PATH"] == str(target)


# ------------------------------------------------------------------------ comprobar


def test_check_finds_a_newer_release_with_a_generic_user_agent(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    seen: list[str] = []
    svc = make_service(tmp_path, fake, UpdateHooks(on_available=lambda r: seen.append(r.version)))
    st = svc.check()
    assert st["state"] == "available" and st["latest"] == "0.2.0" and st["available"]
    assert seen == ["0.2.0"]
    req = fake.requests[0]
    assert req.get_header("User-agent") == "Micnaga/0.1.1"
    assert set(req.headers) <= {"User-agent", "Accept", "X-github-api-version", "If-none-match"}
    # un solo aviso por version
    svc.check()
    assert seen == ["0.2.0"]


def test_etag_avoids_downloading_the_release_again(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    make_service(tmp_path, fake).check()
    svc = make_service(tmp_path, fake)  # estado persistido (otro arranque)
    st = svc.check()
    assert fake.requests[-1].get_header("If-none-match") == '"e1"'
    assert st["latest"] == "0.2.0"


def test_same_or_older_version_is_up_to_date(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json("0.1.1"))
    assert make_service(tmp_path, fake).check()["state"] == "uptodate"


def test_offline_and_rate_limits_are_silent_in_background(tmp_path: Path) -> None:
    fake = FakeHttps()  # sin rutas: URLError
    seen: list[str] = []
    svc = make_service(tmp_path, fake, UpdateHooks(on_available=lambda r: seen.append("x")))
    assert svc.check(manual=False)["state"] == "idle"
    assert svc.check(manual=True)["error"] == "updates.err.offline"
    fake.routes[updates.API_LATEST] = lambda _r: (403, {"X-RateLimit-Reset": "9999999999"}, b"")
    assert svc.check(manual=False)["error"] == ""
    n = len(fake.requests)
    svc.check(manual=False)  # en espera hasta el reset: ni pregunta
    assert len(fake.requests) == n and not seen


def test_skip_hides_that_version_but_not_the_next(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    svc = make_service(tmp_path, fake)
    svc.check()
    st = svc.skip()
    assert not st["available"] and st["skipped"] and st["state"] == "skipped"
    serve_latest(fake, release_json("0.3.0"), etag='"e2"')
    assert svc.check()["available"]


def test_auto_check_loop_respects_the_setting(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    svc = make_service(tmp_path, fake, UpdateHooks(auto_check=lambda: False), first_delay_s=0.0)
    svc.start()
    time.sleep(0.2)
    svc.stop()
    assert not fake.requests


def test_disabled_by_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    assert make_service(tmp_path, fake).check()["available"]
    requests = len(fake.requests)
    monkeypatch.setenv(updates.ENV_DISABLE, "1")
    svc = make_service(tmp_path, fake)
    assert svc.check()["state"] == "disabled"
    assert svc.available() is None
    assert not svc.request_install()["pending"]
    assert len(fake.requests) == requests


# ------------------------------------------------------------------------ descargar e instalar


def _rel(svc: UpdateService) -> ReleaseInfo:
    svc.check()
    rel = svc.available()
    assert rel is not None
    return rel


def test_download_follows_the_cdn_redirect_and_verifies(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    serve_asset(fake)
    svc = make_service(tmp_path, fake)
    path = svc.download(_rel(svc))
    assert path.read_bytes() == PAYLOAD and path.name == "Micnaga-Setup-0.2.0.exe"
    assert not path.with_name(path.name + ".part").exists()


def test_download_resumes_a_partial_file(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    serve_asset(fake)
    svc = make_service(tmp_path, fake)
    rel = _rel(svc)
    part = tmp_path / "dl" / "0.2.0" / "Micnaga-Setup-0.2.0.exe.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(PAYLOAD[:1000])
    svc.download(rel)
    ranges = [r.get_header("Range") for r in fake.requests if r.full_url == CDN]
    assert ranges == ["bytes=1000-"]


def test_download_retries_after_a_cut(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    serve_asset(fake)
    calls = {"n": 0}
    good = fake.routes[CDN]

    def flaky(req: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, {}, PAYLOAD[:5000]  # conexion cortada a mitad
        return good(req)

    fake.routes[CDN] = flaky
    sleeps: list[float] = []
    svc = make_service(tmp_path, fake, sleep=sleeps.append)
    assert svc.download(_rel(svc)).read_bytes() == PAYLOAD
    assert sleeps == [updates.BACKOFF_S[0]]


def test_tampered_installer_is_rejected(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    serve_asset(fake, payload=b"X" * len(PAYLOAD))
    svc = make_service(tmp_path, fake)
    with pytest.raises(updates.UpdateError) as exc:
        svc.download(_rel(svc))
    assert exc.value.key == "updates.err.checksum"
    assert not list((tmp_path / "dl" / "0.2.0").glob("*.exe*"))


def test_disagreeing_checksums_or_none_at_all_refuse(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    serve_asset(fake, sums=f"{'1' * 64}  Micnaga-Setup-0.2.0.exe\n")
    svc = make_service(tmp_path, fake)
    with pytest.raises(updates.UpdateError, match="disagree"):
        svc.download(_rel(svc))
    data = release_json()
    data["assets"] = [dict(data["assets"][0], digest=None)]
    fake2 = FakeHttps()
    serve_latest(fake2, data)
    svc2 = make_service(tmp_path / "b", fake2)
    with pytest.raises(updates.UpdateError, match="no published checksum"):
        svc2.download(_rel(svc2))


def test_digest_alone_is_enough(tmp_path: Path) -> None:
    """Las releases de hoy (v0.1.x) no publican SHA256SUMS.txt: vale el digest de GitHub."""
    data = release_json()
    data["assets"] = [data["assets"][0]]
    fake = FakeHttps()
    serve_latest(fake, data)
    serve_asset(fake)
    svc = make_service(tmp_path, fake)
    assert svc.download(_rel(svc)).read_bytes() == PAYLOAD


def test_bad_signature_blocks_the_install(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    serve_asset(fake)
    svc = make_service(tmp_path, fake, signature=lambda _p: SignatureInfo("HashMismatch"))
    with pytest.raises(updates.UpdateError) as exc:
        svc.download(_rel(svc))
    assert exc.value.key == "updates.err.signature"


def _wait(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("timeout")
        time.sleep(0.02)


def test_install_launches_the_silent_installer_then_quits(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    serve_asset(fake)
    events: list[Any] = []
    hooks = UpdateHooks(
        before_install=lambda: events.append("quiesce"),
        quit_app=lambda: events.append("quit"),
    )
    svc = make_service(tmp_path, fake, hooks, launch=lambda argv: events.append(argv))
    svc.check()
    svc.request_install()
    _wait(lambda: "quit" in events)
    argv = events[1]
    assert argv[0].endswith("Micnaga-Setup-0.2.0.exe")
    assert argv[1:6] == [
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/NOCLOSEAPPLICATIONS",
        "/SP-",
    ]
    assert events[0] == "quiesce" and events[2:] == ["quit"]
    state = json.loads((tmp_path / "updates.json").read_text(encoding="utf-8"))
    assert state["pending_notes"]["version"] == "0.2.0"


def test_install_waits_while_busy_and_runs_when_idle(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    serve_asset(fake)
    busy = {"v": True, "can_reserve": False}
    launched: list[list[str]] = []
    svc = make_service(
        tmp_path,
        fake,
        UpdateHooks(is_busy=lambda: busy["v"], before_install=lambda: busy["can_reserve"]),
        launch=launched.append,
    )
    svc.check()
    svc.request_install()
    _wait(lambda: svc.status()["state"] == "waiting")
    assert not launched and svc.status()["pending"]
    busy["v"] = False
    svc._maybe_install_pending()
    assert not launched and svc.status()["state"] == "waiting"
    busy["can_reserve"] = True
    svc._maybe_install_pending()
    assert launched


def test_install_launch_error_releases_the_reservation(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    serve_asset(fake)
    events: list[str] = []

    def launch(_argv: list[str]) -> None:
        raise OSError("launch failed")

    hooks = UpdateHooks(
        before_install=lambda: events.append("quiesce"),
        install_aborted=lambda: events.append("release"),
        quit_app=lambda: events.append("quit"),
    )
    svc = make_service(tmp_path, fake, hooks, launch=launch)
    rel = _rel(svc)
    path = svc.download(rel)
    with pytest.raises(updates.UpdateError, match=r"updates\.err\.launch"):
        svc.install(rel, path)
    assert events == ["quiesce", "release"]


def test_no_install_from_a_dev_checkout(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    svc = make_service(tmp_path, fake, can_install=False)
    svc.check()
    assert svc.request_install()["error"] == "updates.err.dev"


def test_busy_app_gets_no_notice_until_idle(tmp_path: Path) -> None:
    fake = FakeHttps()
    serve_latest(fake, release_json())
    busy = {"v": True}
    seen: list[str] = []
    svc = make_service(
        tmp_path,
        fake,
        UpdateHooks(is_busy=lambda: busy["v"], on_available=lambda r: seen.append(r.version)),
    )
    svc.check()
    assert not seen
    busy["v"] = False
    svc._maybe_notify()
    assert seen == ["0.2.0"]


# ------------------------------------------------------------------------ novedades


def test_whats_new_shows_once_after_an_update(tmp_path: Path) -> None:
    state = tmp_path / "updates.json"
    state.write_text(
        json.dumps(
            {
                "last_run_version": "0.1.1",
                "pending_notes": {"version": "0.2.0", "notes": "• Iconos nuevos"},
            }
        ),
        encoding="utf-8",
    )
    updated: list[str] = []
    svc = UpdateService(
        UpdateHooks(on_updated=updated.append, auto_check=lambda: False),
        current="0.2.0",
        opener=opener(FakeHttps()),
        state_path=state,
        first_delay_s=3600,
    )
    svc.start()
    svc.stop()
    assert updated == ["0.2.0"]
    assert svc.whats_new() == {"version": "0.2.0", "notes": "• Iconos nuevos", "seen": False}
    svc.mark_whats_new_seen()
    again = UpdateService(current="0.2.0", opener=opener(FakeHttps()), state_path=state)
    again._note_version_change()
    wn = again.whats_new()
    assert wn is not None and wn["seen"]


def test_first_install_has_no_whats_new(tmp_path: Path) -> None:
    svc = UpdateService(current="0.2.0", opener=opener(FakeHttps()), state_path=tmp_path / "u.json")
    svc._note_version_change()
    assert svc.whats_new() is None


def test_status_without_network_has_version_and_build(tmp_path: Path) -> None:
    st = make_service(tmp_path, FakeHttps()).status()
    assert st["current"] == "0.1.1" and st["build"]["version"]
    assert st["state"] == "idle" and not st["available"]


def test_used_installers_are_pruned_at_start(tmp_path: Path) -> None:
    dl = tmp_path / "dl"
    for v in ("0.1.0", "0.1.1", "0.2.0"):
        (dl / v).mkdir(parents=True)
        (dl / v / "x.exe").write_bytes(b"x")
    svc = make_service(tmp_path, FakeHttps(), UpdateHooks(auto_check=lambda: False))
    svc._prune_downloads()
    assert sorted(p.name for p in dl.iterdir()) == ["0.2.0"]  # solo la que aun es nueva
