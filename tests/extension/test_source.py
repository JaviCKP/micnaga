"""``FileExtensionSource`` (ExtensionSignalSource) con estado ausente, caducado, corrupto o vivo."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from micnaga.watcher import extension as ext
from micnaga.watcher.sources import ExtensionSignalSource, ExtensionState

NOW = 10_000.0


def _write(directory: Path, pid: int = 100, **overrides: Any) -> Path:
    data: dict[str, Any] = {
        "v": ext.PROTOCOL_VERSION,
        "pid": pid,
        "created": 1.0,
        "browser": "Chrome.exe",
        "updated_at": NOW,
        "tabs": {"3": {"title": "Daily", "code": "abc-defg-hij", "seen_at": NOW}},
    }
    data.update(overrides)
    directory.mkdir(parents=True, exist_ok=True)
    path = ext.state_file(directory, pid)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _source(
    directory: Path, now: float = NOW, alive: bool = True, **kwargs: Any
) -> ext.FileExtensionSource:
    return ext.FileExtensionSource(
        directory,
        clock=lambda: now,
        pid_alive=lambda pid, created: alive,
        cache_s=0.0,
        **kwargs,
    )


def test_implements_protocol(tmp_path: Path) -> None:
    source = ext.create_extension_source(tmp_path)
    assert isinstance(source, ExtensionSignalSource)


def test_missing_directory_is_unavailable(tmp_path: Path) -> None:
    source = _source(tmp_path / "nope")
    assert source.available() is False
    assert source.read() == []


def test_live_host_with_call(tmp_path: Path) -> None:
    _write(tmp_path)
    source = _source(tmp_path)
    assert source.available() is True
    assert source.read() == [
        ExtensionState(
            in_call=True,
            browser="chrome.exe",
            title="Daily",
            code="abc-defg-hij",
            extra={"tab": "3", "host_pid": 100},
        )
    ]


def test_live_host_without_calls_is_not_available(tmp_path: Path) -> None:
    """Fail-open: sin llamada detectada (o con heuristicas rotas) manda el vigilante nativo."""
    _write(tmp_path, tabs={})
    source = _source(tmp_path)
    assert source.host_alive() is True
    assert source.available() is False
    assert source.read() == []


def test_stale_host_is_unavailable(tmp_path: Path) -> None:
    _write(tmp_path)
    source = _source(tmp_path, now=NOW + ext.STALE_AFTER_S + 1)
    assert source.available() is False
    assert source.read() == []


def test_dead_pid_is_unavailable(tmp_path: Path) -> None:
    _write(tmp_path)
    assert _source(tmp_path, alive=False).available() is False


def test_future_timestamp_is_unavailable(tmp_path: Path) -> None:
    _write(tmp_path, updated_at=NOW + 3600)
    assert _source(tmp_path).available() is False


def test_expired_tab_is_dropped_and_falls_back_to_native(tmp_path: Path) -> None:
    _write(tmp_path, tabs={"3": {"title": "x", "code": None, "seen_at": NOW - ext.TAB_TTL_S - 1}})
    source = _source(tmp_path)
    assert source.host_alive() is True
    assert source.available() is False
    assert source.read() == []


def test_two_hosts_two_browsers(tmp_path: Path) -> None:
    _write(tmp_path, pid=1, browser="chrome.exe")
    _write(
        tmp_path,
        pid=2,
        browser="msedge.exe",
        tabs={"9": {"title": None, "code": None, "seen_at": NOW}},
    )
    browsers = sorted(s.browser for s in _source(tmp_path).read())
    assert browsers == ["chrome.exe", "msedge.exe"]


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{not json",
        "[]",
        json.dumps({"v": 99, "pid": 1, "updated_at": NOW, "browser": "c", "tabs": {}}),
        json.dumps({"v": 1, "pid": "1", "updated_at": NOW, "browser": "c", "tabs": {}}),
        json.dumps({"v": 1, "pid": 1, "updated_at": "now", "browser": "c", "tabs": {}}),
        json.dumps({"v": 1, "pid": 1, "updated_at": NOW, "browser": "c", "tabs": []}),
        json.dumps({"v": 1, "pid": 1, "updated_at": NOW, "tabs": {}}),
    ],
)
def test_corrupt_or_foreign_files_are_ignored(tmp_path: Path, content: str) -> None:
    (tmp_path / "host-1.json").write_text(content, encoding="utf-8")
    source = _source(tmp_path)
    assert source.available() is False
    assert source.read() == []


def test_bad_tab_entries_are_skipped(tmp_path: Path) -> None:
    _write(
        tmp_path, tabs={"1": "oops", "2": {"seen_at": "x"}, "3": {"seen_at": NOW, "code": "bad"}}
    )
    states = _source(tmp_path).read()
    assert [s.extra["tab"] for s in states] == ["3"]
    assert states[0].code is None


def test_cache_avoids_rereading(tmp_path: Path) -> None:
    path = _write(tmp_path)
    source = ext.FileExtensionSource(
        tmp_path, clock=lambda: NOW, pid_alive=lambda p, c: True, cache_s=60.0
    )
    assert source.available() is True
    path.unlink()
    assert source.available() is True  # aun dentro de la cache
    assert source.diagnostics()["live_hosts"] == 1


def test_cleanup_dead_state_files(tmp_path: Path) -> None:
    mine = _write(tmp_path, pid=os.getpid(), created=ext._process_created(os.getpid()) or 0.0)
    dead = _write(tmp_path, pid=2_000_000_000)
    garbage = tmp_path / "host-7.json"
    garbage.write_text("{", encoding="utf-8")
    assert ext.cleanup_dead_state_files(tmp_path) == 2
    assert mine.exists() and not dead.exists() and not garbage.exists()


def test_default_pid_alive_current_process() -> None:
    created = ext._process_created(os.getpid())
    assert ext.default_pid_alive(os.getpid(), created) is True
    assert ext.default_pid_alive(os.getpid(), (created or 0) + 3600) is False  # pid reutilizado
    assert ext.default_pid_alive(2_000_000_000, None) is False


# ------------------------------------------------------------------ host extremo a extremo


def _wait_for(predicate: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_run_host_with_pipe_end_to_end(tmp_path: Path) -> None:
    """Host real sobre una tuberia: mensajes -> fichero -> lector; EOF -> fichero borrado."""
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    writer = os.fdopen(write_fd, "wb", buffering=0)
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            ext.run_host(reader, directory=tmp_path, browser="chrome.exe", heartbeat_s=0.05)
        )
    )
    thread.start()
    source = ext.FileExtensionSource(tmp_path, cache_s=0.0)
    try:
        assert _wait_for(source.host_alive)  # vivo antes de cualquier mensaje
        assert source.available() is False and source.read() == []
        writer.write(
            ext.encode_message(
                {"state": "in_call", "title": "Hola", "code": "abc-defg-hij", "tab": 5}
            )
        )
        assert _wait_for(source.available)
        state = source.read()[0]
        assert (state.browser, state.title, state.code) == ("chrome.exe", "Hola", "abc-defg-hij")
        writer.write(ext.encode_message({"state": "not_in_call", "tab": 5}))
        assert _wait_for(lambda: not source.available())
        assert source.host_alive()  # el host sigue vivo; simplemente no hay llamada
    finally:
        writer.close()  # el navegador se cierra
        thread.join(10)
        reader.close()
    assert result == [0]
    assert list(tmp_path.glob("host-*.json")) == []  # nada de codigos en disco tras salir
    assert source.host_alive() is False


def test_run_host_protocol_error_exits_1_and_cleans_up(tmp_path: Path) -> None:
    import io

    code = ext.run_host(io.BytesIO(b"\x10\x00\x00\x00{bad"), directory=tmp_path, browser="x.exe")
    assert code == 1
    assert list(tmp_path.glob("host-*.json")) == []


def test_host_subprocess_python_dash_m(tmp_path: Path) -> None:
    """``python -m micnaga.watcher.extension``: lo que lanza el .bat de desarrollo."""
    env = {**os.environ, "MICNAGA_HOME": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "micnaga.watcher.extension", "chrome-extension://abc/"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None
    source = ext.FileExtensionSource(tmp_path / "extension", cache_s=0.0)
    try:
        proc.stdin.write(
            ext.encode_message({"state": "in_call", "title": "T", "code": "abc-defg-hij", "tab": 1})
        )
        proc.stdin.flush()
        assert _wait_for(lambda: len(source.read()) == 1, timeout=30)
        assert source.read()[0].code == "abc-defg-hij"
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
    assert proc.returncode == 0
    assert proc.stdout is not None and proc.stdout.read() == b""  # stdout reservado al protocolo
    assert list((tmp_path / "extension").glob("host-*.json")) == []
