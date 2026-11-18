from __future__ import annotations

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
