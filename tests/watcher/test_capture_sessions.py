from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from micnaga.watcher.capture_sessions import CaptureSessionMicSource, CombinedMicSource
from micnaga.watcher.model import MicApp, SignalReading
from micnaga.watcher.sources import MicReading

BROWSERS = ["chrome.exe", "msedge.exe", "brave.exe"]


def _src(sessions: list[tuple[int, str, str]], **kw: object) -> CaptureSessionMicSource:
    return CaptureSessionMicSource(
        BROWSERS,
        lambda: sessions,
        own_paths=frozenset({r"c:\py\pythonw.exe"}),
        own_pid=999,
        **kw,  # type: ignore[arg-type]
    )


def test_active_browser_session_counts_as_mic_in_use() -> None:
    r = _src([(10, "chrome.exe", r"C:\Chrome\chrome.exe")]).read()
    assert r.browsers == frozenset({"chrome.exe"})
    assert [a.key for a in r.apps] == ["app:chrome.exe"]
    assert r.reading.active


def test_own_process_and_excluded_apps_are_ignored() -> None:
    r = _src(
        [
            (999, "pythonw.exe", r"C:\py\pythonw.exe"),
            (11, "pythonw.exe", r"C:\py\pythonw.exe"),
            (12, "micnaga.exe", r"C:\M\micnaga.exe"),
            (13, "handy.exe", r"C:\H\handy.exe"),
        ],
        exclude=["handy.exe"],
    ).read()
    assert r.apps == frozenset()
    assert not r.reading.active
    assert r.reading.detail["excluded_in_use"] == ["handy.exe", "micnaga.exe", "pythonw.exe"]


def test_generic_off_keeps_browsers_only() -> None:
    r = _src(
        [(10, "chrome.exe", r"C:\c.exe"), (14, "teams.exe", r"C:\t.exe")], generic=False
    ).read()
    assert r.browsers == frozenset({"chrome.exe"})
    assert r.apps == frozenset()


def test_lister_error_is_reported_not_raised() -> None:
    def boom() -> list[tuple[int, str, str]]:
        raise OSError("no capture device")

    r = CaptureSessionMicSource(BROWSERS, boom, own_paths=frozenset(), own_pid=1).read()
    assert not r.reading.active
    assert r.reading.error is not None and "no capture device" in r.reading.error


@pytest.mark.skipif(sys.platform != "win32", reason="necesita comtypes")
@pytest.mark.parametrize("failure_at", ["endpoint", "manager", "session"])
def test_failing_wasapi_device_or_session_keeps_other_microphones_visible(
    monkeypatch: pytest.MonkeyPatch, failure_at: str
) -> None:
    import comtypes
    import psutil

    def fail(*args: object) -> None:
        raise comtypes.COMError(-2147467259, "device removed", None)

    proc = SimpleNamespace(name=lambda: "Chrome.exe", exe=lambda: r"C:\Chrome\chrome.exe")
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    good_session = SimpleNamespace(GetState=lambda: 1, GetProcessId=lambda: 42)
    sessions = SimpleNamespace(
        GetCount=lambda: 2,
        GetSession=lambda i: SimpleNamespace(
            QueryInterface=fail if i == 0 and failure_at == "session" else lambda _: good_session
        ),
    )
    good_manager = SimpleNamespace(GetSessionEnumerator=lambda: sessions)
    bad_manager = SimpleNamespace(GetSessionEnumerator=fail)
    endpoints = SimpleNamespace(
        GetCount=lambda: 2,
        Item=lambda i: SimpleNamespace(
            Activate=fail
            if i == 0 and failure_at == "endpoint"
            else lambda *args: SimpleNamespace(
                QueryInterface=lambda _: (
                    bad_manager if i == 0 and failure_at == "manager" else good_manager
                )
            )
        ),
    )
    monkeypatch.setattr(
        comtypes,
        "CoCreateInstance",
        lambda *args: SimpleNamespace(EnumAudioEndpoints=lambda *a: endpoints),
    )

    reading = CaptureSessionMicSource(BROWSERS, own_pid=1).read()

    assert reading.browsers == frozenset({"chrome.exe"})
    assert {app.key for app in reading.apps} == {"app:chrome.exe"}


class _Fixed:
    def __init__(self, reading: MicReading) -> None:
        self._r = reading

    def read(self) -> MicReading:
        return self._r


def test_combined_unions_registry_and_sessions() -> None:
    chrome = MicApp("app:chrome.exe", "navegador", exe="chrome.exe", is_browser=True)
    teams = MicApp("app:ms-teams.exe", "Teams", exe="ms-teams.exe")
    registry = MicReading(frozenset(), SignalReading("mic", False), frozenset({teams}))
    sessions = MicReading(
        frozenset({"chrome.exe"}), SignalReading("mic_sessions", True), frozenset({chrome})
    )
    r = CombinedMicSource(_Fixed(registry), _Fixed(sessions)).read()
    assert r.browsers == frozenset({"chrome.exe"})
    assert {a.key for a in r.apps} == {"app:chrome.exe", "app:ms-teams.exe"}
    assert r.reading.active


def test_combined_inactive_when_both_empty() -> None:
    empty = MicReading(frozenset(), SignalReading("mic", False))
    r = CombinedMicSource(_Fixed(empty), _Fixed(empty)).read()
    assert not r.reading.active


def test_cached_lister_reuses_endpoints_and_recovers_from_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import micnaga.watcher.capture_sessions as cs

    enumerations: list[int] = []
    fail_next = {"on": False}

    def fake_managers() -> list[object]:
        enumerations.append(1)
        return [object()]

    def fake_active(managers: object) -> list[tuple[int, str, str]]:
        if fail_next["on"]:
            fail_next["on"] = False
            raise OSError("device removed")
        return [(7, "chrome.exe", r"C:\chrome.exe")]

    monkeypatch.setattr(cs, "_session_managers", fake_managers)
    monkeypatch.setattr(cs, "_active_sessions", fake_active)
    now = {"t": 0.0}
    lister = cs.CachedCaptureLister(refresh_s=10.0, clock=lambda: now["t"])
    for t in (0.0, 1.0, 9.0):  # una sola enumeracion de micros en 10 s
        now["t"] = t
        assert lister() == [(7, "chrome.exe", r"C:\chrome.exe")]
    assert len(enumerations) == 1
    now["t"] = 10.5  # caduca: se vuelve a enumerar
    lister()
    assert len(enumerations) == 2
    fail_next["on"] = True  # un error tira la cache y reintenta con una enumeracion nueva
    now["t"] = 11.0
    assert lister() == [(7, "chrome.exe", r"C:\chrome.exe")]
    assert len(enumerations) == 3
    combined = cs.CombinedMicSource(
        cs.CaptureSessionMicSource(BROWSERS, lister, own_paths=frozenset(), own_pid=1),
        cs.CaptureSessionMicSource(BROWSERS, lambda: [], own_paths=frozenset(), own_pid=1),
    )
    combined.close()  # suelta los objetos COM de la cache
    assert lister._managers is None
