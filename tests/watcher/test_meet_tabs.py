"""Escaneo de pestanas de Meet con ventanas / UIA falsos + muestreador."""

from __future__ import annotations

from micnaga.watcher.meet_tabs import BrowserWindow, MeetTabScanner
from micnaga.watcher.model import MeetTab, MonitorHint, SignalReading, SignalSnapshot
from micnaga.watcher.sampler import SignalSampler
from micnaga.watcher.sources import (
    AudioOutReading,
    ExtensionState,
    MicReading,
    NullExtensionSource,
    TabsReading,
)

CHROME = "chrome.exe"
EDGE = "msedge.exe"
HINT = MonitorHint(handle=3, rect=(0, 0, 10, 10), work_area=(0, 0, 10, 9))


class FakeLister:
    def __init__(self, windows: list[BrowserWindow]) -> None:
        self.windows_list = windows
        self.asked: list[frozenset[str]] = []

    def windows(self, browsers: frozenset[str]) -> list[BrowserWindow]:
        self.asked.append(browsers)
        return [w for w in self.windows_list if w.browser in browsers]


class FakeTabs:
    def __init__(self, names: dict[int, list[str]]) -> None:
        self.names = names
        self.calls = 0
        self.released = 0

    def tab_names(self, hwnd: int) -> list[str]:
        self.calls += 1
        if hwnd == 666:
            raise RuntimeError("window gone")
        return self.names.get(hwnd, [])

    def release(self) -> None:
        self.released += 1


class Clock:
    t = 0.0

    def __call__(self) -> float:
        return self.t


def test_mic_off_skips_all_work() -> None:
    lister, tabs = FakeLister([BrowserWindow(1, CHROME, "Meet - abc-defg-hij")]), FakeTabs({})
    scanner = MeetTabScanner(lister, tabs)
    r = scanner.read(frozenset())
    assert r.tabs == () and lister.asked == [] and tabs.calls == 0


def test_active_tab_from_window_title() -> None:
    lister = FakeLister([BrowserWindow(1, CHROME, "Meet - abc-defg-hij - Google Chrome", True)])
    scanner = MeetTabScanner(lister, FakeTabs({}), lambda _: HINT)
    (tab,) = scanner.read(frozenset({CHROME})).tabs
    assert tab.code == "abc-defg-hij" and tab.active and tab.monitor == HINT and tab.hwnd == 1


def test_background_tab_found_through_uia() -> None:
    lister = FakeLister([BrowserWindow(1, CHROME, "YouTube - Google Chrome", True)])
    tabs = FakeTabs({1: ["YouTube", "Uso de memoria de Meet - abc-defg-hij: 85 MB", "Gmail"]})
    (tab,) = MeetTabScanner(lister, tabs).read(frozenset({CHROME})).tabs
    assert tab.code == "abc-defg-hij" and not tab.active
    assert tab.title is None  # los nombres de UIA no se usan como titulo


def test_unknown_uia_suffix_does_not_split_a_codeless_meeting_in_two() -> None:
    """La misma pestana via ventana y via UIA (con un sufijo desconocido) es UNA sola."""
    lister = FakeLister([BrowserWindow(1, CHROME, "Meet: Revisión semanal - Google Chrome", True)])
    tabs = FakeTabs({1: ["Meet: Revisión semanal - Zorglub activo"]})
    (tab,) = MeetTabScanner(lister, tabs).read(frozenset({CHROME})).tabs
    assert tab.title == "Revisión semanal" and tab.active


def test_unknown_uia_suffix_never_becomes_the_title_or_the_code() -> None:
    lister = FakeLister([BrowserWindow(1, CHROME, "Gmail - Google Chrome", True)])
    tabs = FakeTabs({1: ["Meet - abc-defg-hij - Zorglub activo"]})
    (tab,) = MeetTabScanner(lister, tabs).read(frozenset({CHROME})).tabs
    assert tab.title is None and tab.code == "abc-defg-hij"


def test_active_and_uia_hits_of_the_same_meeting_are_merged_preferring_active() -> None:
    lister = FakeLister([BrowserWindow(1, CHROME, "Meet - abc-defg-hij - Google Chrome", True)])
    tabs = FakeTabs({1: ["Meet - abc-defg-hij"]})
    found = MeetTabScanner(lister, tabs).read(frozenset({CHROME})).tabs
    assert len(found) == 1 and found[0].active


def test_only_windows_of_browsers_using_the_mic_are_scanned() -> None:
    lister = FakeLister(
        [
            BrowserWindow(1, CHROME, "Meet - abc-defg-hij"),
            BrowserWindow(2, EDGE, "Meet - xyz-wxyz-uvw"),
        ]
    )
    found = MeetTabScanner(lister, FakeTabs({})).read(frozenset({EDGE})).tabs
    assert [t.browser for t in found] == [EDGE]


def test_uia_errors_are_reported_not_raised() -> None:
    lister = FakeLister(
        [BrowserWindow(666, CHROME, "x"), BrowserWindow(1, CHROME, "Meet - abc-defg-hij")]
    )
    r = MeetTabScanner(lister, FakeTabs({})).read(frozenset({CHROME}))
    assert len(r.tabs) == 1 and r.reading.error and "window gone" in r.reading.error


def test_positive_result_is_cached_for_a_few_seconds_then_refreshed() -> None:
    clock = Clock()
    lister = FakeLister([BrowserWindow(1, CHROME, "Meet - abc-defg-hij")])
    scanner = MeetTabScanner(lister, FakeTabs({}), clock=clock)
    for _ in range(3):
        scanner.read(frozenset({CHROME}))
        clock.t += 1
    assert len(lister.asked) == 1  # t = 0, 1, 2 -> una sola exploracion
    scanner.read(frozenset({CHROME}))  # t = 3
    assert len(lister.asked) == 2


def test_negative_result_is_rescanned_every_tick() -> None:
    clock = Clock()
    lister = FakeLister([BrowserWindow(1, CHROME, "YouTube")])
    scanner = MeetTabScanner(lister, FakeTabs({}), clock=clock)
    for _ in range(3):
        scanner.read(frozenset({CHROME}))
        clock.t += 1
    assert len(lister.asked) == 3


def test_uia_objects_are_released_after_a_while_without_mic() -> None:
    clock = Clock()
    tabs = FakeTabs({})
    scanner = MeetTabScanner(FakeLister([]), tabs, clock=clock, idle_release_s=60)
    scanner.read(frozenset())
    clock.t += 61
    scanner.read(frozenset())
    assert tabs.released == 1


# --------------------------------------------------------------------------- sampler


class FakeMic:
    def __init__(self, browsers: frozenset[str]) -> None:
        self.browsers = browsers

    def read(self) -> MicReading:
        return MicReading(self.browsers, SignalReading("mic", bool(self.browsers)))


class FakeTabSource:
    def __init__(self, tabs: tuple[MeetTab, ...]) -> None:
        self.tabs = tabs
        self.asked: list[frozenset[str]] = []

    def read(self, mic_browsers: frozenset[str]) -> TabsReading:
        self.asked.append(mic_browsers)
        return TabsReading(self.tabs, SignalReading("meet_tab", bool(self.tabs)))


class FakeAudio:
    def read(self, mic_browsers: frozenset[str]) -> AudioOutReading:
        return AudioOutReading(mic_browsers, SignalReading("audio_out", bool(mic_browsers)))


class FakeExtension:
    def __init__(self, calls: list[ExtensionState]) -> None:
        self.calls = calls

    def available(self) -> bool:
        return True

    def read(self) -> list[ExtensionState]:
        return self.calls


def test_sampler_combines_sources_and_exposes_diagnostics() -> None:
    meet = MeetTab(CHROME, None, "abc-defg-hij")
    sampler = SignalSampler(FakeMic(frozenset({CHROME})), FakeTabSource((meet,)), FakeAudio())
    snap = sampler.sample()
    assert snap == SignalSnapshot(frozenset({CHROME}), (meet,), frozenset({CHROME}))
    diag = sampler.diagnostics()
    assert [s["name"] for s in diag["signals"]] == ["mic", "meet_tab", "audio_out"]


def test_extension_replaces_the_other_signals_when_available() -> None:
    tabs = FakeTabSource(())
    ext = FakeExtension([ExtensionState(True, "Chrome.exe", "Weekly", "abc-defg-hij")])
    sampler = SignalSampler(FakeMic(frozenset()), tabs, None, ext)
    snap = sampler.sample()
    assert snap.source == "extension" and tabs.asked == []
    (tab,) = snap.live_tabs()
    assert tab.code == "abc-defg-hij" and tab.browser == "chrome.exe"


def test_null_extension_is_never_available() -> None:
    assert NullExtensionSource().available() is False
    sampler = SignalSampler(FakeMic(frozenset()), FakeTabSource(()), None, NullExtensionSource())
    assert sampler.sample().source == "native"
