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

OVERLAY_OPAQUE_BG = "#0a090f"
"""Formulario de la superficie cuando no hay transparencia: el color de noche de ``.pill``."""

_overlay_hwnds: set[int] = set()
_theme_hooked: set[type] = set()
_hook_lock = threading.Lock()


def _keep_backdrop_clear(win: Any, hwnd: int, w32: winapi.Win32) -> None:
    """pywebview reaplica Mica en ``BrowserForm.update_title_bar_theme`` al crear la ventana y
    en cada ``SystemEvents.UserPreferenceChanged`` (cambio de tema, de colores...). Se envuelve
    ese metodo (una vez por clase) para que, justo despues, las superficies vuelvan a quedarse
    sin fondo de sistema: el recuadro gris no puede volver (DECISIONS §29)."""
    with _hook_lock:
        _overlay_hwnds.add(hwnd)
        try:
            cls = type(win.native)
        except Exception:
            return
        if cls in _theme_hooked:
            return
        original = getattr(cls, "update_title_bar_theme", None)
        if original is None:
            return

        def update_title_bar_theme(self: Any) -> None:
            original(self)
            try:
                handle = int(self.Handle.ToInt64())
            except Exception:
                return
            if handle in _overlay_hwnds:
                winapi.clear_backdrop(handle, w32)

        try:
            cls.update_title_bar_theme = update_title_bar_theme
            _theme_hooked.add(cls)
        except Exception:
            log.warning("cannot hook pywebview's theme update; backdrop cleared on show only")


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

    def post_message(self, text: str) -> None:
        """Mensaje de texto a la pagina (``chrome.webview`` «message»), sin esperar: lo mas
        barato para datos frecuentes (niveles). No compila ni evalua nada en la pagina."""
        ...

    def show(self, *, activate: bool) -> None: ...

    def hide(self) -> None: ...

    def is_visible(self) -> bool: ...

    def place(self, rect: Rect) -> None: ...

    def rect(self) -> Rect: ...

    def set_region(self, rect: Rect | None, radius: int = 0) -> bool:
        """Zona visible y clicable (coordenadas de la ventana, px fisicos, esquinas de
        ``radius`` px); ``None`` = toda. ``False`` si Windows no la aplico."""
        ...

    def destroy(self) -> None: ...

    def on_closed(self, callback: Callable[[], None]) -> None: ...

    def on_moved(self, callback: Callable[[int, int], None]) -> None: ...

    def set_title(self, title: str) -> None: ...

    def pick_folder(self, current: str) -> str | None: ...

    def prepare_overlay(self, transparent: bool = True) -> bool:
        """Ventana superpuesta (pastilla/tarjeta). ``transparent``: formulario negro + marco DWM
        extendido al cliente (transparencia real; el CSS dibuja la forma). Si no, formulario
        opaco del color de la pastilla. En los dos casos sin fondo de sistema de DWM (Mica, que
        pywebview pone con el tema oscuro), tambien cuando pywebview lo reaplique. Idempotente.
        ``False`` si no se pudo (con ``transparent``: no hay transparencia en este equipo)."""
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
        self._rendering: bool | None = None

    @property
    def hwnd(self) -> int:
        """El ``Handle`` ACTUAL del formulario (WinForms puede recrearlo: un hwnd guardado
        apuntaria a una ventana muerta). Nunca lo crea: ``Handle`` sin crear lo crearia en este
        hilo, que no es el de la interfaz."""
        try:
            native = self._win.native
            if native is not None and native.IsHandleCreated:
                handle = int(native.Handle.ToInt64())
                if handle:
                    self._hwnd = handle
                    return handle
        except Exception:
            log.debug("no native handle for %s", self.key, exc_info=True)
        if not self._hwnd:
            self._hwnd = self._w32.find_window(self._spec.title)
        return self._hwnd

    def wait_loaded(self, timeout: float = 10.0) -> bool:
        return bool(self._win.events.loaded.wait(timeout))

    def run_js(self, script: str) -> None:
        try:
            self._win.evaluate_js(script)
        except Exception:
            log.debug("run_js failed on %s", self.key, exc_info=True)

    def post_message(self, text: str) -> None:
        """``CoreWebView2.PostWebMessageAsString`` en el hilo de la interfaz (como hace pywebview
        con ``evaluate_js``, pero sin compilar script ni esperar respuesta). A ~10 veces por
        segundo, ``evaluate_js`` (envuelto, serializado y esperando respuesta) costaba ~2 % de un
        nucleo en Python (medido)."""
        try:
            native = self._win.native
            from System import Action  # type: ignore[import-not-found,unused-ignore]
        except Exception:
            log.debug("post_message unavailable on %s", self.key, exc_info=True)
            return

        def send() -> None:  # en el hilo de la interfaz: CoreWebView2 solo vive ahi
            try:
                native.browser.webview.CoreWebView2.PostWebMessageAsString(text)
            except Exception:
                log.debug("post_message failed on %s", self.key, exc_info=True)

        try:
            native.browser.webview.Invoke(Action(send))
        except Exception:
            log.debug("post_message not delivered on %s", self.key, exc_info=True)

    def show(self, *, activate: bool) -> None:
        self._set_rendering(True)
        if activate:
            self._win.show()
            with contextlib.suppress(Exception):
                self._win.restore()
        else:
            winapi.show_no_activate(self.hwnd, self._w32)

    def hide(self) -> None:
        winapi.hide(self.hwnd, self._w32)
        self._set_rendering(False)

    def _set_rendering(self, on: bool) -> None:
        """Las superposiciones se ocultan con Win32 (``ShowWindow``), que WinForms no ve: el
        control WebView2 seguia «visible» y Chromium pintaba fotogramas (GPU incluida) con la
        ventana oculta. ``Visible`` del control pone ``CoreWebView2Controller.IsVisible``: oculto,
        el navegador deja de dibujar y de dar fotogramas (DECISIONS §34)."""
        if not self._spec.overlay or self._rendering is on:
            return
        try:
            native = self._win.native
            view = native.browser.webview

            def apply() -> None:
                view.Visible = on

            if getattr(native, "InvokeRequired", False):
                from System import Action  # type: ignore[import-not-found,unused-ignore]

                native.Invoke(Action(apply))
            else:
                apply()
            self._rendering = on
        except Exception:
            log.debug("WebView2 visibility not set on %s", self.key, exc_info=True)

    def is_visible(self) -> bool:
        return bool(self.hwnd) and self._w32.is_visible(self.hwnd)

    def place(self, rect: Rect) -> None:
        winapi.place(self.hwnd, rect, self._w32, topmost=self._spec.on_top)

    def rect(self) -> Rect:
        return self._w32.get_window_rect(self.hwnd)

    def set_region(self, rect: Rect | None, radius: int = 0) -> bool:
        hwnd = self.hwnd
        return bool(hwnd) and winapi.set_region(hwnd, rect, self._w32, radius=radius)

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

    def prepare_overlay(self, transparent: bool = True) -> bool:
        hwnd = self.hwnd
        if not hwnd:
            return False
        _keep_backdrop_clear(self._win, hwnd, self._w32)
        ok = self._set_back_color("#000000" if transparent else OVERLAY_OPAQUE_BG)
        if transparent:
            ok = winapi.make_overlay_transparent(hwnd, self._w32) and ok
        winapi.clear_backdrop(hwnd, self._w32)
        return ok

    def _set_back_color(self, color: str) -> bool:  # pragma: no cover - necesita WinForms
        """``BackColor`` del formulario: negro = transparente con el marco DWM extendido; sin
        el, pywebview deja un recuadro gris detras del WebView2 transparente."""
        try:
            native = self._win.native
            from System import Action  # type: ignore[import-not-found,unused-ignore]
            from System.Drawing import (  # type: ignore[import-not-found,unused-ignore]
                ColorTranslator,
            )

            def set_back() -> None:
                native.BackColor = ColorTranslator.FromHtml(color)

            if getattr(native, "InvokeRequired", False):
                native.Invoke(Action(set_back))
            else:
                set_back()
            return True
        except Exception:
            log.debug("BackColor not set on %s", self.key, exc_info=True)
            return False

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
            # sin sombra en la pastilla/tarjeta: pywebview la hace extendiendo el marco de DWM
            shadow=not spec.transparent and not spec.overlay,
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
        self.region: Rect | None = None
        self.region_radius = 0
        self.region_ok = True
        self.overlay_ok = True
        """``prepare_overlay(transparent=True)`` funciona (hay transparencia)."""
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

    def is_visible(self) -> bool:
        return self.visible

    def post_message(self, text: str) -> None:
        self.log.append(("post", text))

    def place(self, rect: Rect) -> None:
        self.current_rect = rect
        self.log.append(("place", rect))

    def rect(self) -> Rect:
        x, y, w, h = self.current_rect
        return (x, y, x + w, y + h)

    def set_region(self, rect: Rect | None, radius: int = 0) -> bool:
        self.region = rect
        self.region_radius = radius
        self.log.append(("region", rect))
        return self.region_ok

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

    def prepare_overlay(self, transparent: bool = True) -> bool:
        self.log.append(("prepare_overlay", transparent))
        return self.overlay_ok or not transparent

    def pick_folder(self, current: str) -> str | None:
        self.log.append(("pick_folder", current))
        return None

    # -- ayudas de test
    def js_calls(self) -> list[str]:
        return [str(a) for k, a in self.log if k == "js"]

    def fire_moved(self, x: int, y: int) -> None:
        """El usuario arrastro la ventana: su esquina queda en ``x, y`` (px fisicos)."""
        self.current_rect = (x, y, *self.current_rect[2:])
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
