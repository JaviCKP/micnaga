"""Senal «pestana de Meet abierta»: logica de escaneo (pura) sobre proveedores inyectables.

El escaneo solo se ejecuta cuando algun navegador usa el micro (la senal del registro es la
puerta barata). Tiene dos niveles:

1. Titulo de la ventana (= pestana activa), coste ~0.
2. Barra de pestanas por UI Automation, para las pestanas que no estan en primer plano.

Las implementaciones reales (win32 + UIA con ``comtypes``) viven en ``winapi.py``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from micnaga.watcher.model import MeetTab, MonitorHint, SignalReading
from micnaga.watcher.sources import TabsReading
from micnaga.watcher.titles import parse_meet_name


@dataclass(frozen=True)
class BrowserWindow:
    hwnd: int
    browser: str  # basename del exe en minusculas
    title: str
    foreground: bool = False


class BrowserWindowLister(Protocol):
    def windows(self, browsers: frozenset[str]) -> list[BrowserWindow]:
        """Ventanas visibles de nivel superior de los navegadores indicados."""
        ...


class TabNameReader(Protocol):
    def tab_names(self, hwnd: int) -> list[str]:
        """Nombres de las pestanas de una ventana (UIA ``TabItem``). Puede lanzar."""
        ...

    def release(self) -> None:
        """Suelta caches y objetos COM (se llama tras un rato sin micro y al parar)."""
        ...


MonitorLocator = Callable[[int], MonitorHint | None]


class MeetTabScanner:
    """Implementa ``MeetTabSource``."""

    def __init__(
        self,
        lister: BrowserWindowLister,
        tab_reader: TabNameReader,
        monitor_for: MonitorLocator | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        idle_release_s: float = 60.0,
        positive_cache_s: float = 3.0,
    ) -> None:
        self._lister = lister
        self._tabs = tab_reader
        self._monitor_for = monitor_for
        self._clock = clock
        self._idle_release_s = idle_release_s
        self._idle_since: float | None = None
        self._positive_cache_s = positive_cache_s
        self._cached: tuple[float, frozenset[str], TabsReading] | None = None

    def read(self, mic_browsers: frozenset[str]) -> TabsReading:
        now = self._clock()
        if not mic_browsers:
            if self._idle_since is None:
                self._idle_since = now
            elif now - self._idle_since >= self._idle_release_s:
                self._tabs.release()
                self._idle_since = now
            self._cached = None
            return TabsReading((), SignalReading("meet_tab", False, {"skipped": "mic off"}))
        self._idle_since = None
        cached = self._cached
        if (
            cached is not None
            and cached[1] == mic_browsers
            and now - cached[0] < self._positive_cache_s
        ):
            # Con una pestana de Meet ya localizada basta revisar cada pocos segundos: el fin
            # de reunion necesita 15 s seguidos, asi que el coste de UIA se divide por 3.
            return cached[2]
        reading = self._scan(mic_browsers)
        self._cached = (now, mic_browsers, reading) if reading.tabs else None
        return reading

    def _scan(self, mic_browsers: frozenset[str]) -> TabsReading:
        t0 = time.perf_counter()
        errors: list[str] = []
        found: dict[tuple[str, str], MeetTab] = {}
        windows_seen = 0
        for win in self._lister.windows(mic_browsers):
            windows_seen += 1
            title_hit = parse_meet_name(win.title)
            if title_hit is not None:
                tab = MeetTab(
                    browser=win.browser,
                    title=title_hit.title,
                    code=title_hit.code,
                    raw_name=win.title,
                    active=True,
                    hwnd=win.hwnd,
                    monitor=self._monitor(win.hwnd),
                )
                found[(tab.browser, tab.key)] = tab
            try:
                names = self._tabs.tab_names(win.hwnd)
            except Exception as exc:  # UIA puede fallar si la ventana se cierra a mitad
                errors.append(f"{type(exc).__name__}: {exc}")
                continue
            for name in names:
                hit = parse_meet_name(name)
                if hit is None:
                    continue
                other = MeetTab(
                    browser=win.browser,
                    title=None,  # los nombres de UIA llevan decoraciones: solo se usa el codigo
                    code=hit.code,
                    raw_name=name,
                    active=False,
                    hwnd=win.hwnd,
                    monitor=self._monitor(win.hwnd),
                )
                found.setdefault((other.browser, other.key), other)
        tabs = tuple(found.values())
        detail: dict[str, object] = {
            "windows": windows_seen,
            "tabs": [
                {"browser": t.browser, "title": t.title, "code": t.code, "active": t.active}
                for t in tabs
            ],
        }
        cost = (time.perf_counter() - t0) * 1000
        return TabsReading(
            tabs,
            SignalReading(
                "meet_tab", bool(tabs), detail, "; ".join(errors) if errors else None, cost
            ),
        )

    def close(self) -> None:
        self._tabs.release()

    def _monitor(self, hwnd: int) -> MonitorHint | None:
        if self._monitor_for is None:
            return None
        try:
            return self._monitor_for(hwnd)
        except Exception:
            return None
