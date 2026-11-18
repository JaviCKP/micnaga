"""Capa de ventanas: ``WindowFactory`` (pywebview real o falsa para tests).

``UiHost`` solo habla con estas interfaces; asi su orquestacion (popup -> pastilla, hover,
cola de aperturas...) se prueba sin abrir ninguna ventana. El import de ``webview`` es perezoso
(``tests/test_import_hygiene.py`` prohibe que el nucleo lo cargue).

Hilos: ``webview.start`` ocupa el hilo principal; ``create_window`` puede llamarse despues desde
cualquier hilo (pywebview 6). Los metodos de ``Window`` (``evaluate_js``, ``destroy``...) son
seguros entre hilos; mostrar/ocultar/colocar usan Win32 sobre el ``hwnd`` nativo.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from micnaga.ui import winapi
from micnaga.ui.contracts import Rect
from micnaga.ui.media import LocalServer

log = logging.getLogger(__name__)


@dataclass
class WindowSpec:
    key: str
    page: str
    title: str
    width: int
    height: int
    """Tamano logico (CSS px). Las ventanas sin marco se recolocan en pixeles fisicos aparte."""
    params: dict[str, Any] = field(default_factory=dict)
    js_api: object | None = None
    transparent: bool = False
    frameless: bool = False
    on_top: bool = False
    focus: bool = True
    hidden: bool = False
    resizable: bool = True
    min_size: tuple[int, int] = (200, 100)
    background: str = "#ffffff"
    overlay: bool = False
    """Popup/pastilla: sin foco, sin barra de tareas, siempre encima."""


class UiWindow(Protocol):
    key: str

    @property
    def hwnd(self) -> int: ...

    def wait_loaded(self, timeout: float = 10.0) -> bool: ...

    def run_js(self, script: str) -> None: ...

    def show(self, *, activate: bool) -> None: ...

    def hide(self) -> None: ...

    def place(self, rect: Rect) -> None: ...

    def rect(self) -> Rect: ...

    def destroy(self) -> None: ...

    def on_closed(self, callback: Callable[[], None]) -> None: ...

    def on_moved(self, callback: Callable[[int, int], None]) -> None: ...

    def set_title(self, title: str) -> None: ...

    def pick_folder(self, current: str) -> str | None: ...

    def make_transparent(self) -> bool:
        """Transparencia real de una ventana sin marco (popup/pastilla): formulario negro +
        marco DWM extendido al cliente. ``False`` si no se pudo."""
        ...


class WindowFactory(Protocol):
    def create(self, spec: WindowSpec) -> UiWindow: ...

    def start(self, on_started: Callable[[], None], *, icon: Path | None) -> None:
        """Arranca el bucle de la GUI (bloquea hasta cerrar la ultima ventana)."""
        ...


# --------------------------------------------------------------------------- pywebview


class PywebviewWindow:
    def __init__(self, win: Any, spec: WindowSpec, w32: winapi.Win32) -> None:
        self.key = spec.key
        self._win = win
        self._spec = spec
        self._w32 = w32
        self._hwnd = 0

    @property
    def hwnd(self) -> int:
        if not self._hwnd:
            try:
                self._hwnd = int(self._win.native.Handle.ToInt32())
            except Exception:
                self._hwnd = self._w32.find_window(self._spec.title)
        return self._hwnd

    def wait_loaded(self, timeout: float = 10.0) -> bool:
        return bool(self._win.events.loaded.wait(timeout))

    def run_js(self, script: str) -> None:
        try:
            self._win.evaluate_js(script)
        except Exception:
            log.debug("run_js failed on %s", self.key, exc_info=True)

    def show(self, *, activate: bool) -> None:
        if activate:
            self._win.show()
            with contextlib.suppress(Exception):
                self._win.restore()
        else:
            winapi.show_no_activate(self.hwnd, self._w32)

    def hide(self) -> None:
        winapi.hide(self.hwnd, self._w32)

    def place(self, rect: Rect) -> None:
        winapi.place(self.hwnd, rect, self._w32, topmost=self._spec.on_top)

    def rect(self) -> Rect:
        return self._w32.get_window_rect(self.hwnd)

    def destroy(self) -> None:
        try:
            self._win.destroy()
        except Exception:
            log.debug("destroy failed on %s", self.key, exc_info=True)

    def on_closed(self, callback: Callable[[], None]) -> None:
        self._win.events.closed += callback

    def on_moved(self, callback: Callable[[int, int], None]) -> None:
        self._win.events.moved += callback

    def set_title(self, title: str) -> None:
        with contextlib.suppress(Exception):
            self._win.set_title(title)

    def make_transparent(self) -> bool:  # pragma: no cover - necesita una ventana real
        """``BackColor = Black`` en el formulario WinForms + ``DwmExtendFrameIntoClientArea``.

        Sin esto pywebview deja el recuadro gris del formulario detras del WebView2 transparente.
        """
        ok = False
        try:
            native = self._win.native
            from System import Action  # type: ignore[import-not-found,unused-ignore]
            from System.Drawing import Color  # type: ignore[import-not-found,unused-ignore]

            def set_back() -> None:
                native.BackColor = Color.Black

            if getattr(native, "InvokeRequired", False):
                native.Invoke(Action(set_back))
            else:
                set_back()
            ok = True
        except Exception:
            log.debug("BackColor not set on %s", self.key, exc_info=True)
        hwnd = self.hwnd
        if hwnd:
            ok = winapi.make_overlay_transparent(hwnd, self._w32) and ok
        return ok

    def pick_folder(self, current: str) -> str | None:  # pragma: no cover - dialogo nativo
        import webview

        try:
            result = self._win.create_file_dialog(webview.FOLDER_DIALOG, directory=current or "")
        except Exception:
            log.exception("folder dialog failed")
            return None
        return str(result[0]) if result else None


class PywebviewFactory:
    def __init__(self, server: LocalServer, w32: winapi.Win32 | None = None) -> None:
        self._server = server
        self._w32 = w32 or winapi.default_api()

    def create(self, spec: WindowSpec) -> UiWindow:
        import webview

        url = self._server.page_url(spec.page, **spec.params)
        win = webview.create_window(
            spec.title,
            url=url,
            js_api=spec.js_api,
            width=spec.width,
            height=spec.height,
            resizable=spec.resizable,
            frameless=spec.frameless,
            easy_drag=False,  # la pastilla usa .pywebview-drag-region
            shadow=not spec.transparent,
            focus=spec.focus,  # focus=False => WS_EX_NOACTIVATE (pywebview 6)
            hidden=spec.hidden,
            on_top=spec.on_top,
            transparent=spec.transparent,
            background_color=spec.background,
            min_size=spec.min_size,
            text_select=False,
            zoomable=False,
            draggable=False,
        )
        assert win is not None
        return PywebviewWindow(win, spec, self._w32)

    def start(self, on_started: Callable[[], None], *, icon: Path | None) -> None:
        import webview

        webview.start(
            on_started,
            gui="edgechromium",
            debug=False,
            private_mode=True,
            icon=str(icon) if icon is not None and icon.is_file() else None,
        )


# ------------------------------------------------------------------------------- falsa


class FakeWindow:
    """Ventana de pruebas: registra las llamadas en ``log``."""

    def __init__(self, spec: WindowSpec, hwnd: int) -> None:
        self.key = spec.key
        self.spec = spec
        self._hwnd = hwnd
        self.log: list[tuple[str, Any]] = []
        self.visible = False
        self.alive = True
        self.current_rect: Rect = (0, 0, spec.width, spec.height)
        self._closed: list[Callable[[], None]] = []
        self._moved: list[Callable[[int, int], None]] = []

    @property
    def hwnd(self) -> int:
        return self._hwnd

    def wait_loaded(self, timeout: float = 10.0) -> bool:
        return True

    def run_js(self, script: str) -> None:
        self.log.append(("js", script))

    def show(self, *, activate: bool) -> None:
        self.visible = True
        self.log.append(("show", activate))

    def hide(self) -> None:
        self.visible = False
        self.log.append(("hide", None))

    def place(self, rect: Rect) -> None:
        self.current_rect = rect
        self.log.append(("place", rect))

    def rect(self) -> Rect:
        x, y, w, h = self.current_rect
        return (x, y, x + w, y + h)

    def destroy(self) -> None:
        self.alive = False
        self.visible = False
        self.log.append(("destroy", None))
        for cb in self._closed:
            cb()

    def on_closed(self, callback: Callable[[], None]) -> None:
        self._closed.append(callback)

    def on_moved(self, callback: Callable[[int, int], None]) -> None:
        self._moved.append(callback)

    def set_title(self, title: str) -> None:
        self.log.append(("title", title))

    def make_transparent(self) -> bool:
        self.log.append(("make_transparent", None))
        return True

    def pick_folder(self, current: str) -> str | None:
        self.log.append(("pick_folder", current))
        return None

    # -- ayudas de test
    def js_calls(self) -> list[str]:
        return [str(a) for k, a in self.log if k == "js"]

    def fire_moved(self, x: int, y: int) -> None:
        for cb in self._moved:
            cb(x, y)


class FakeFactory:
    def __init__(self) -> None:
        self.windows: dict[str, FakeWindow] = {}
        self.created: list[WindowSpec] = []
        self._next = 1000
        self._lock = threading.Lock()

    def create(self, spec: WindowSpec) -> UiWindow:
        with self._lock:
            self._next += 1
            win = FakeWindow(spec, self._next)
            self.windows[spec.key] = win
            self.created.append(spec)
        return win

    def start(self, on_started: Callable[[], None], *, icon: Path | None) -> None:
        on_started()
