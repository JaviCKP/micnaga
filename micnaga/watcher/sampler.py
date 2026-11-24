"""Muestreador: combina las fuentes de senales en un ``SignalSnapshot`` por tick."""

from __future__ import annotations

import time
from typing import Any

from micnaga.config import DetectionConfig
from micnaga.watcher.audio_out import PycawAudioOutSource
from micnaga.watcher.meet_tabs import MeetTabScanner
from micnaga.watcher.model import MeetTab, SignalReading, SignalSnapshot
from micnaga.watcher.registry_mic import RegistryMicSource
from micnaga.watcher.sources import (
    AudioOutSource,
    ExtensionSignalSource,
    MeetTabSource,
    MicSource,
    NullExtensionSource,
)


class SignalSampler:
    """Una llamada a ``sample()`` = una lectura de todas las senales.

    Orden y puertas de coste: micro (barato, siempre) -> pestanas de Meet y audio de salida
    (solo si el micro esta en uso). Si hay extension disponible (F4) sustituye a todo lo demas.
    """

    def __init__(
        self,
        mic: MicSource,
        tabs: MeetTabSource,
        audio_out: AudioOutSource | None = None,
        extension: ExtensionSignalSource | None = None,
    ) -> None:
        self._mic = mic
        self._tabs = tabs
        self._audio = audio_out
        self._extension: ExtensionSignalSource = extension or NullExtensionSource()
        self.last_readings: list[SignalReading] = []
        self.last_cost_ms = 0.0

    def sample(self) -> SignalSnapshot:
        t0 = time.perf_counter()
        try:
            snap = self._sample()
        finally:
            self.last_cost_ms = (time.perf_counter() - t0) * 1000
        return snap

    def _sample(self) -> SignalSnapshot:
        if self._extension.available():
            states = [s for s in self._extension.read() if s.in_call]
            tabs = tuple(
                MeetTab(s.browser.lower(), s.title, s.code, raw_name="extension", active=True)
                for s in states
            )
            self.last_readings = [
                SignalReading(
                    "extension",
                    bool(states),
                    {"calls": [(s.browser, s.title, s.code) for s in states]},
                )
            ]
            return SignalSnapshot(
                mic_browsers=frozenset(t.browser for t in tabs), meet_tabs=tabs, source="extension"
            )
        mic = self._mic.read()
        tabs_r = self._tabs.read(mic.browsers)
        audio_r = self._audio.read(mic.browsers) if self._audio is not None else None
        self.last_readings = [mic.reading, tabs_r.reading] + ([audio_r.reading] if audio_r else [])
        return SignalSnapshot(
            mic_browsers=mic.browsers,
            meet_tabs=tabs_r.tabs,
            audio_out_browsers=audio_r.browsers if audio_r else frozenset(),
            mic_apps=mic.apps,
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "total_cost_ms": round(self.last_cost_ms, 3),
            "signals": [r.to_dict() for r in self.last_readings],
        }

    def close(self) -> None:
        """Suelta objetos COM (llamar desde el hilo que creo las fuentes)."""
        for source in (self._tabs, self._mic):
            close = getattr(source, "close", None)
            if callable(close):
                close()


def build_default_sampler(
    config: DetectionConfig, extension: ExtensionSignalSource | None = None
) -> SignalSampler:
    """Fuentes reales de Windows para ``config.browsers``."""
    from micnaga.watcher.capture_sessions import CaptureSessionMicSource, CombinedMicSource
    from micnaga.watcher.winapi import (
        UiaTabNameReader,
        Win32BrowserWindows,
        monitor_hint_for_window,
    )

    browsers = [b.lower() for b in config.browsers]
    # El registro de privacidad no siempre refleja el uso (p. ej. Escritorio remoto): se une
    # con las sesiones de captura WASAPI activas.
    mic = CombinedMicSource(
        RegistryMicSource(browsers, generic=config.generic_apps, exclude=config.exclude_apps),
        CaptureSessionMicSource(browsers, generic=config.generic_apps, exclude=config.exclude_apps),
    )
    return SignalSampler(
        mic=mic,
        tabs=MeetTabScanner(Win32BrowserWindows(), UiaTabNameReader(), monitor_hint_for_window),
        audio_out=PycawAudioOutSource(browsers),
        extension=extension,
    )
