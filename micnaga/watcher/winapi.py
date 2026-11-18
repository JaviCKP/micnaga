"""Implementaciones reales (Windows) de los proveedores del escaneo de pestanas.

Todo import de ``win32*``, ``psutil`` y ``comtypes`` es perezoso (dentro de funciones/metodos):
importar el modulo no toca COM ni arrastra dependencias.

* ``Win32BrowserWindows``: ``EnumWindows`` + filtro por clase ``Chrome_WidgetWin_1`` (Chrome, Edge
  y Brave la comparten) + cache pid -> nombre de exe.
* ``UiaTabNameReader``: UI Automation con ``comtypes.gen.UIAutomationClient`` (sin dependencias
  nuevas). Se cachea el elemento ``TabControl`` de cada ventana y se piden solo los ``TabItem``
  de ese subarbol con una ``CacheRequest`` (una llamada entre procesos). Medido en este PC:
  ~0.5 ms por ventana con el control cacheado frente a 6-60 ms buscando desde la ventana.
  Debe usarse siempre desde el mismo hilo (COM apartamento del hilo del vigilante).
* ``monitor_hint_for_window``: ``MonitorFromWindow`` + ``GetMonitorInfo``.
"""

from __future__ import annotations

import time
from typing import Any

from micnaga.watcher.meet_tabs import BrowserWindow
from micnaga.watcher.model import MonitorHint

CHROMIUM_WINDOW_CLASS = "Chrome_WidgetWin_1"


def _rect(raw: Any) -> tuple[int, int, int, int]:
    left, top, right, bottom = (int(v) for v in raw)
    return left, top, right, bottom


def monitor_hint_for_window(hwnd: int) -> MonitorHint | None:
    """Monitor en el que esta (o mas cerca esta) la ventana ``hwnd``."""
    import win32api
    import win32con

    handle = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
    if not handle:
        return None
    info = win32api.GetMonitorInfo(handle)
    return MonitorHint(
        handle=int(handle),
        rect=_rect(info["Monitor"]),
        work_area=_rect(info["Work"]),
        device=str(info.get("Device", "")),
        primary=bool(info.get("Flags", 0) & 1),
    )


class Win32BrowserWindows:
    """Lista las ventanas de navegador visibles (``BrowserWindowLister``)."""

    def __init__(self, pid_cache_ttl_s: float = 60.0) -> None:
        self._pid_names: dict[int, str] = {}
        self._pid_cache_at = 0.0
        self._ttl = pid_cache_ttl_s

    def _exe_name(self, pid: int) -> str:
        name = self._pid_names.get(pid)
        if name is None:
            import psutil

            try:
                name = psutil.Process(pid).name().lower()
            except (psutil.Error, OSError):
                name = ""
            self._pid_names[pid] = name
        return name

    def windows(self, browsers: frozenset[str]) -> list[BrowserWindow]:
        import win32gui
        import win32process

        now = time.monotonic()
        if now - self._pid_cache_at > self._ttl:  # los pid se reutilizan
            self._pid_names.clear()
            self._pid_cache_at = now
        foreground = win32gui.GetForegroundWindow()
        found: list[BrowserWindow] = []

        def visit(hwnd: int, _: Any) -> bool:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if win32gui.GetClassName(hwnd) != CHROMIUM_WINDOW_CLASS:
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return True
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            exe = self._exe_name(pid)
            if exe in browsers:
                found.append(BrowserWindow(hwnd, exe, title, hwnd == foreground))
            return True

        win32gui.EnumWindows(visit, None)
        return found


class UiaTabNameReader:
    """Nombres de pestana por UI Automation (``TabNameReader``)."""

    NO_TABCONTROL_BACKOFF_S = 5.0

    def __init__(self) -> None:
        self._uia: Any = None
        self._module: Any = None
        self._cond_tab_item: Any = None
        self._cond_tab_control: Any = None
        self._cache_request: Any = None
        self._tab_controls: dict[int, Any] = {}
        self._no_tabcontrol_until: dict[int, float] = {}

    def _init(self) -> None:
        import comtypes
        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as uia_mod

        self._module = uia_mod
        self._uia = comtypes.client.CreateObject(
            uia_mod.CUIAutomation, interface=uia_mod.IUIAutomation
        )
        self._cond_tab_item = self._uia.CreatePropertyCondition(
            uia_mod.UIA_ControlTypePropertyId, uia_mod.UIA_TabItemControlTypeId
        )
        self._cond_tab_control = self._uia.CreatePropertyCondition(
            uia_mod.UIA_ControlTypePropertyId, uia_mod.UIA_TabControlTypeId
        )
        self._cache_request = self._uia.CreateCacheRequest()
        self._cache_request.AddProperty(uia_mod.UIA_NamePropertyId)

    def _query(self, control: Any) -> list[str]:
        arr = control.FindAllBuildCache(
            self._module.TreeScope_Descendants, self._cond_tab_item, self._cache_request
        )
        return [str(arr.GetElement(i).CachedName) for i in range(arr.Length)]

    def tab_names(self, hwnd: int) -> list[str]:
        if self._uia is None:
            self._init()
        now = time.monotonic()
        if self._no_tabcontrol_until.get(hwnd, 0.0) > now:
            return []
        control = self._tab_controls.get(hwnd)
        if control is not None:
            try:
                names = self._query(control)
                if names:
                    return names
            except Exception:  # elemento obsoleto (la barra se reconstruyo)
                pass
            self._tab_controls.pop(hwnd, None)
        window = self._uia.ElementFromHandle(hwnd)
        control = window.FindFirst(self._module.TreeScope_Descendants, self._cond_tab_control)
        if control is None:
            self._no_tabcontrol_until[hwnd] = now + self.NO_TABCONTROL_BACKOFF_S
            return []
        names = self._query(control)
        if names:
            self._tab_controls[hwnd] = control
        else:
            self._no_tabcontrol_until[hwnd] = now + self.NO_TABCONTROL_BACKOFF_S
        return names

    def release(self) -> None:
        self._tab_controls.clear()
        self._no_tabcontrol_until.clear()
        self._cache_request = None
        self._cond_tab_item = None
        self._cond_tab_control = None
        self._uia = None
        self._module = None
