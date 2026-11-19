"""Interfaces estrechas entre el controlador de la app y la UI/captura (para poder falsearlas)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from micnaga.config import CaptureConfig
from micnaga.ui.contracts import LevelsProvider, MonitorHint


class UiPort(Protocol):
    """Lo que el controlador usa de ``UiHost`` (``UiHost`` lo cumple tal cual)."""

    def show_popup(
        self,
        title: str,
        code: str | None = None,
        monitor_hint: MonitorHint = None,
        *,
        timeout_s: float | None = None,
        on_timeout: str | None = None,
        countdown_total_s: float | None = None,
    ) -> None: ...

    def hide_popup(self, reason: str = "ended") -> None: ...

    def show_end_popup(
        self,
        title: str,
        *,
        duration_s: float,
        confirmed: bool,
        monitor_hint: MonitorHint = None,
        countdown_total_s: float = 10.0,
    ) -> None: ...

    def hide_end_popup(self, reason: str = "answered") -> None: ...

    def show_undo(self, seconds: float) -> None: ...

    def hide_undo(self) -> None: ...

    def notify_error(self, key: str) -> None: ...

    def accept_popup(self) -> None: ...

    def dismiss_popup(self) -> None: ...

    def expire_popup(self) -> None: ...

    def show_pill(
        self, *, elapsed_s: float = 0.0, provisional: bool = False, monitor_hint: MonitorHint = None
    ) -> None: ...

    def hide_pill(self) -> None: ...

    def set_pill_paused(self, paused: bool) -> None: ...

    def set_pill_provisional(self, provisional: bool) -> None: ...

    def sync_pill(self, elapsed_s: float) -> None: ...

    def set_levels_provider(self, provider: LevelsProvider) -> None: ...

    def set_popup_state_provider(
        self, provider: Callable[[], tuple[float, bool] | None] | None
    ) -> None: ...

    def set_processing(self, processing: bool) -> None: ...

    def set_watching_paused(self, paused: bool) -> None: ...

    def notify_ready(self, folder: str, title: str, duration_s: float, speakers: int) -> None: ...

    def open_library(self, folder: str | None = None, seek_s: float | None = None) -> None: ...

    def open_settings(self) -> None: ...

    def open_wizard(self) -> None: ...

    def apply_config(self, config: Any) -> None: ...

    def quit(self, reason: str = "quit") -> None: ...

    def run(self, on_ready: Callable[[Any], None] | None = None) -> None: ...


class RecorderLike(Protocol):
    """Subconjunto de ``micnaga.capture.Recorder`` que usa el controlador."""

    def start(
        self,
        session_dir: Path,
        *,
        confirmed: bool = False,
        meet_title: str | None = None,
        meet_code: str | None = None,
    ) -> Path: ...

    def pause(self) -> bool: ...

    def resume(self) -> bool: ...

    def add_marker(self, label: str = "") -> Any: ...

    def set_confirmed(self, confirmed: bool = True) -> None: ...

    def set_meeting_info(self, title: str | None = None, code: str | None = None) -> None: ...

    def stop(self) -> Path: ...

    def levels(self) -> tuple[float, float]: ...

    def elapsed_s(self) -> float: ...


RecorderFactory = Callable[[CaptureConfig], RecorderLike]
