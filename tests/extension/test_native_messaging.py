"""Framing de native messaging, estado del host y escritura atomica."""

from __future__ import annotations

import io
import json
import os
import struct
from pathlib import Path

import pytest

from micnaga.watcher import extension as ext


def test_encode_decode_roundtrip_unicode() -> None:
    message = {"state": "in_call", "title": "Revisión semanal – ñandú", "code": "abc-defg-hij"}
    data = ext.encode_message(message)
    (length,) = struct.unpack("<I", data[:4])
    assert length == len(data) - 4
    assert ext.decode_message(io.BytesIO(data)) == message


def test_encode_is_compact_utf8() -> None:
    data = ext.encode_message({"a": "é"})
    assert data[4:] == '{"a":"é"}'.encode()


def test_decode_eof_returns_none() -> None:
    assert ext.decode_message(io.BytesIO(b"")) is None


def test_decode_multiple_messages_then_eof() -> None:
    stream = io.BytesIO(ext.encode_message({"n": 1}) + ext.encode_message({"n": 2}))
    assert list(ext.iter_messages(stream)) == [{"n": 1}, {"n": 2}]


def test_decode_truncated_header() -> None:
    with pytest.raises(ext.ProtocolError, match="truncated"):
        ext.decode_message(io.BytesIO(b"\x05\x00"))


def test_decode_truncated_body() -> None:
    data = ext.encode_message({"state": "in_call"})
    with pytest.raises(ext.ProtocolError, match="truncated"):
        ext.decode_message(io.BytesIO(data[:-3]))


def test_decode_rejects_oversized_length_without_reading_it() -> None:
    header = struct.pack("<I", ext.MAX_MESSAGE_BYTES + 1)
    with pytest.raises(ext.ProtocolError, match="too large"):
        ext.decode_message(io.BytesIO(header + b"x"))


def test_encode_rejects_oversized() -> None:
    with pytest.raises(ext.ProtocolError, match="too large"):
        ext.encode_message({"x": "a" * ext.MAX_MESSAGE_BYTES})


def test_decode_invalid_json_and_utf8() -> None:
    bad = b"{nope"
    with pytest.raises(ext.ProtocolError, match="JSON"):
        ext.decode_message(io.BytesIO(struct.pack("<I", len(bad)) + bad))
    bad = b"\xff\xfe"
    with pytest.raises(ext.ProtocolError):
        ext.decode_message(io.BytesIO(struct.pack("<I", len(bad)) + bad))


def test_decode_reads_from_short_reads() -> None:
    """Una tuberia puede devolver trozos pequenos: se reensambla."""

    class Dribble(io.RawIOBase):
        def __init__(self, data: bytes) -> None:
            self._buf = io.BytesIO(data)

        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            return self._buf.read(min(size, 3) if size > 0 else 3)

    message = {"state": "not_in_call", "title": None, "code": None}
    stream = Dribble(ext.encode_message(message))
    assert ext.decode_message(stream) == message  # type: ignore[arg-type]


def test_write_message_flushes() -> None:
    out = io.BytesIO()
    ext.write_message(out, {"ok": True})
    assert ext.decode_message(io.BytesIO(out.getvalue())) == {"ok": True}


# ----------------------------------------------------------------------------- HostState


def _state(clock: list[float] | None = None) -> ext.HostState:
    now = clock if clock is not None else [1000.0]
    return ext.HostState(pid=42, browser="chrome.exe", created=1.0, clock=lambda: now[0])


def test_state_apply_in_call_and_leave() -> None:
    state = _state()
    assert state.apply(
        {"state": "in_call", "title": "  Daily \n sync ", "code": "ABC-defg-hij", "tab": 7}
    )
    assert state.tabs["7"] == {"title": "Daily sync", "code": "abc-defg-hij", "seen_at": 1000.0}
    assert state.apply({"state": "not_in_call", "tab": 7})
    assert state.tabs == {}


@pytest.mark.parametrize(
    "message",
    [None, [], "in_call", 3, {}, {"state": "maybe"}, {"state": None}, {"title": "x"}],
)
def test_state_ignores_invalid_messages(message: object) -> None:
    state = _state()
    assert state.apply(message) is False
    assert state.tabs == {}


def test_state_sanitises_fields() -> None:
    state = _state()
    state.apply({"state": "in_call", "title": 5, "code": "not-a-code", "tab": True})
    assert state.tabs["0"]["title"] is None
    assert state.tabs["0"]["code"] is None
    state.apply({"state": "in_call", "title": "x" * 1000, "code": "abc-defg-hij", "tab": "t" * 100})
    assert len(state.tabs["t" * 32]["title"]) == ext.MAX_TITLE_CHARS


def test_state_to_json_shape() -> None:
    clock = [5.0]
    state = _state(clock)
    state.apply({"state": "in_call", "code": "abc-defg-hij", "tab": 1})
    clock[0] = 9.0
    data = state.to_json()
    assert data["v"] == ext.PROTOCOL_VERSION
    assert data["pid"] == 42 and data["browser"] == "chrome.exe"
    assert data["updated_at"] == 9.0
    assert data["tabs"]["1"]["seen_at"] == 5.0


# ----------------------------------------------------------------------------- escritura atomica


def test_write_state_atomic_creates_dirs_and_leaves_no_tmp(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "host-1.json"
    ext.write_state_atomic(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert [p.name for p in target.parent.iterdir()] == ["host-1.json"]


def test_write_state_atomic_retries_on_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src: object, dst: object) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("locked by reader")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(ext.os, "replace", flaky)
    target = tmp_path / "host-2.json"
    ext.write_state_atomic(target, {"ok": True}, delay_s=0)
    assert calls["n"] == 3
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


def test_write_state_atomic_gives_up_and_cleans_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def locked(src: object, dst: object) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(ext.os, "replace", locked)
    target = tmp_path / "host-3.json"
    with pytest.raises(PermissionError):
        ext.write_state_atomic(target, {"x": 1}, retries=2, delay_s=0)
    assert list(tmp_path.iterdir()) == []


def test_state_dir_uses_micnaga_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MICNAGA_HOME", str(tmp_path / "home"))
    assert ext.state_dir() == tmp_path / "home" / "extension"
    assert ext.state_dir(tmp_path / "x") == tmp_path / "x"
