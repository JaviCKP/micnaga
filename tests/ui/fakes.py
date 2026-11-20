"""Dobles de prueba de la UI: Win32 falso (sin ventanas ni registro reales)."""

from __future__ import annotations

from micnaga.ui import winapi
from micnaga.ui.contracts import MonitorHint, Rect


class FakeWin32(winapi.Win32):
    """Registra las llamadas y simula estilos, monitores y valores del sistema."""

    def __init__(self) -> None:
        self.styles: dict[int, int] = {}
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rects: dict[int, Rect] = {}
        self.monitor = winapi.MonitorInfo((0, 0, 1920, 1040), 1.25)
        self.animation = True
        self.qstate: int | None = winapi.QUNS_ACCEPTS_NOTIFICATIONS
        self.hkcu: dict[tuple[str, str], int] = {}
        self.build = 26200
        self.dwm: list[tuple[int, int, int]] = []
        self.dwm_ok = True
        self.foreground: list[int] = []

    def get_ex_style(self, hwnd: int) -> int:
        return self.styles.get(hwnd, 0)

    def set_ex_style(self, hwnd: int, style: int) -> None:
        self.calls.append(("set_ex_style", (hwnd, style)))
        self.styles[hwnd] = style

    def set_window_pos(
        self, hwnd: int, after: int, x: int, y: int, w: int, h: int, flags: int
    ) -> bool:
        self.calls.append(("set_window_pos", (hwnd, after, x, y, w, h, flags)))
        if not flags & winapi.SWP_NOSIZE and not flags & winapi.SWP_NOMOVE:
            self.rects[hwnd] = (x, y, x + w, y + h)
        return True

    def show_window(self, hwnd: int, cmd: int) -> None:
        self.calls.append(("show_window", (hwnd, cmd)))

    def get_window_rect(self, hwnd: int) -> Rect:
        return self.rects.get(hwnd, (0, 0, 0, 0))

    def set_window_region(self, hwnd: int, rect: Rect | None) -> bool:
        self.calls.append(("set_window_region", (hwnd, rect)))
        return True

    def find_window(self, title: str) -> int:
        return 0

    def set_foreground(self, hwnd: int) -> bool:
        self.foreground.append(hwnd)
        return True

    def window_dpi(self, hwnd: int) -> int:
        return 120

    def monitor_info(self, hint: MonitorHint) -> winapi.MonitorInfo:
        self.calls.append(("monitor_info", (hint,)))
        return self.monitor

    def dwm_set(self, hwnd: int, attr: int, value: int) -> bool:
        self.dwm.append((hwnd, attr, value))
        return self.dwm_ok

    def extend_frame(self, hwnd: int) -> bool:
        self.calls.append(("extend_frame", (hwnd,)))
        return True

    def client_area_animation(self) -> bool:
        return self.animation

    def notification_state(self) -> int | None:
        return self.qstate

    def read_hkcu_dword(self, key: str, name: str) -> int | None:
        return self.hkcu.get((key, name))

    def windows_build(self) -> int:
        return self.build

    def places(self, hwnd: int) -> list[tuple[int, int, int, int]]:
        """Rectangulos ``(x, y, w, h)`` de las llamadas a ``SetWindowPos`` con tamano."""
        return [
            (c[1][2], c[1][3], c[1][4], c[1][5])  # type: ignore[misc]
            for c in self.calls
            if c[0] == "set_window_pos" and c[1][0] == hwnd and not int(c[1][6]) & winapi.SWP_NOSIZE  # type: ignore[call-overload]
        ]
