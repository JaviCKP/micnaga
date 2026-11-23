"""Utilidades Win32 de la UI (ctypes puro, inyectables para los tests).

Todo lo que toca la API de Windows pasa por ``Win32``; los tests usan ``FakeWin32`` y asi se
comprueba la logica (estilos, geometria, DND, tema) sin ventanas reales. Solo se **lee** el
registro (tema); nunca se escribe aqui (``startup.py`` es el unico que escribe, y es inyectable).

Verificado en este PC (Windows 11 26200): ``pywebview 6.2.1`` con ``focus=False`` ya aplica
``WS_EX_NOACTIVATE``; aun asi ``apply_no_activate`` lo fija de forma explicita e idempotente
(y anade ``WS_EX_TOOLWINDOW`` para que no salga en la barra de tareas ni en Alt+Tab).
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Literal

from micnaga.ui.contracts import MonitorHint, Rect

log = logging.getLogger(__name__)

# --- constantes (win32con no trae todas; valores de winuser.h / dwmapi.h) ---------------------
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
MONITOR_DEFAULTTONEAREST = 2
SPI_GETCLIENTAREAANIMATION = 0x1042

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_NONE = 1  # sin fondo de sistema
DWMSBT_MAINWINDOW = 2  # Mica
DWMSBT_TRANSIENTWINDOW = 3  # acrilico
MICA_MIN_BUILD = 22621  # Windows 11 22H2

# SHQueryUserNotificationState
QUNS_NOT_PRESENT = 1
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5
QUNS_QUIET_TIME = 6
QUNS_APP = 7
_QUNS_SILENT = frozenset(
    {QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE, QUNS_QUIET_TIME}
)

PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
NOTIFICATIONS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"


@dataclass(frozen=True)
class MonitorInfo:
    work: Rect
    """Area de trabajo (sin barra de tareas) en pixeles fisicos."""
    scale: float = 1.0
    """Factor DPI (1.0 = 96 dpi)."""


class Win32:
    """Implementacion real (ctypes). Importar este modulo no toca la API: solo al llamar."""

    # -- estilos y ventanas
    def get_ex_style(self, hwnd: int) -> int:
        return (
            int(ctypes.windll.user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)) & 0xFFFFFFFF
        )

    def set_ex_style(self, hwnd: int, style: int) -> None:
        ctypes.windll.user32.SetWindowLongW(
            wintypes.HWND(hwnd), GWL_EXSTYLE, ctypes.c_long(ctypes.c_int32(style).value)
        )

    def set_window_pos(
        self, hwnd: int, after: int, x: int, y: int, w: int, h: int, flags: int
    ) -> bool:
        return bool(
            ctypes.windll.user32.SetWindowPos(
                wintypes.HWND(hwnd), wintypes.HWND(after), x, y, w, h, flags
            )
        )

    def show_window(self, hwnd: int, cmd: int) -> None:
        ctypes.windll.user32.ShowWindow(wintypes.HWND(hwnd), cmd)

    def is_visible(self, hwnd: int) -> bool:
        return bool(ctypes.windll.user32.IsWindowVisible(wintypes.HWND(hwnd)))

    def get_window_rect(self, hwnd: int) -> Rect:
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
        return (r.left, r.top, r.right, r.bottom)

    def find_window(self, title: str) -> int:
        return int(ctypes.windll.user32.FindWindowW(None, title) or 0)

    def set_foreground(self, hwnd: int) -> bool:
        return bool(ctypes.windll.user32.SetForegroundWindow(wintypes.HWND(hwnd)))

    def set_window_region(self, hwnd: int, rect: Rect | None, radius: int = 0) -> bool:
        """``SetWindowRgn`` con un rectangulo (coordenadas de la ventana, esquinas redondeadas de
        ``radius`` px fisicos) o ``None`` (sin recorte).

        Recorta pintado Y clics: fuera de la region los clics llegan a la ventana de debajo.
        OJO: NO recorta el fondo de sistema de DWM (Mica/acrilico), ver ``clear_backdrop``."""
        user32, gdi32 = _user32(), _gdi32()
        if rect is None:
            return bool(user32.SetWindowRgn(hwnd, None, True))
        rgn = create_region(rect, radius)
        if not rgn:
            return False
        if user32.SetWindowRgn(hwnd, rgn, True):
            return True  # el sistema es ahora el dueno de la region
        gdi32.DeleteObject(rgn)
        return False

    def window_dpi(self, hwnd: int) -> int:
        try:
            return int(ctypes.windll.user32.GetDpiForWindow(wintypes.HWND(hwnd))) or 96
        except (AttributeError, OSError):
            return 96

    # -- monitores
    def monitor_info(self, hint: MonitorHint) -> MonitorInfo:
        user32 = ctypes.windll.user32
        if isinstance(hint, tuple):
            r = wintypes.RECT(*hint)
            hmon = user32.MonitorFromRect(ctypes.byref(r), MONITOR_DEFAULTTONEAREST)
        elif isinstance(hint, int) and hint:
            hmon = user32.MonitorFromWindow(wintypes.HWND(hint), MONITOR_DEFAULTTONEAREST)
        else:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)

        class MONITORINFO(ctypes.Structure):
            _fields_ = (
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            )

        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        dpi_x, dpi_y = ctypes.c_uint(96), ctypes.c_uint(96)
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        w = mi.rcWork
        return MonitorInfo((w.left, w.top, w.right, w.bottom), (dpi_x.value or 96) / 96.0)

    # -- sistema
    def dwm_set(self, hwnd: int, attr: int, value: int) -> bool:
        v = ctypes.c_int(value)
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v)
        )
        return bool(hr == 0)

    def extend_frame(self, hwnd: int) -> bool:
        """``DwmExtendFrameIntoClientArea`` con margenes -1: todo el cliente es «cristal»
        (sin esto pywebview pinta un recuadro gris detras de una ventana transparente)."""

        class MARGINS(ctypes.Structure):
            _fields_ = (
                ("left", ctypes.c_int),
                ("right", ctypes.c_int),
                ("top", ctypes.c_int),
                ("bottom", ctypes.c_int),
            )

        m = MARGINS(-1, -1, -1, -1)
        hr = ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(wintypes.HWND(hwnd), ctypes.byref(m))
        return bool(hr == 0)

    def client_area_animation(self) -> bool:
        flag = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(flag), 0
        )
        return bool(flag.value) if ok else True

    def notification_state(self) -> int | None:
        state = ctypes.c_int(0)
        hr = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        return int(state.value) if hr == 0 else None

    def read_hkcu_dword(self, key: str, name: str) -> int | None:
        """Lectura (NUNCA escritura) de un DWORD de HKCU."""
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ) as k:
                value, kind = winreg.QueryValueEx(k, name)
        except OSError:
            return None
        return int(value) if kind == winreg.REG_DWORD else None

    def windows_build(self) -> int:
        return int(sys.getwindowsversion().build)  # type: ignore[attr-defined,unused-ignore]


_libs: dict[str, ctypes.WinDLL] = {}


def _lib(name: str) -> ctypes.WinDLL:
    """Copia propia de la DLL: los ``argtypes`` de aqui no tocan ``ctypes.windll`` (global)."""
    lib = _libs.get(name)
    if lib is None:
        lib = _libs[name] = ctypes.WinDLL(name, use_last_error=True)
        if name == "gdi32":
            lib.CreateRectRgn.argtypes = (ctypes.c_int,) * 4
            lib.CreateRectRgn.restype = wintypes.HRGN
            lib.CreateRoundRectRgn.argtypes = (ctypes.c_int,) * 6
            lib.CreateRoundRectRgn.restype = wintypes.HRGN
            lib.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
            lib.DeleteObject.restype = wintypes.BOOL
            lib.PtInRegion.argtypes = (wintypes.HRGN, ctypes.c_int, ctypes.c_int)
            lib.PtInRegion.restype = wintypes.BOOL
        elif name == "user32":
            lib.SetWindowRgn.argtypes = (wintypes.HWND, wintypes.HRGN, wintypes.BOOL)
            lib.SetWindowRgn.restype = ctypes.c_int
    return lib


def _gdi32() -> ctypes.WinDLL:
    return _lib("gdi32")


def _user32() -> ctypes.WinDLL:
    return _lib("user32")


def create_region(rect: Rect, radius: int = 0) -> int | None:
    """HRGN de ``rect`` (``x, y, ancho, alto``). Con esquinas redondeadas ``CreateRoundRectRgn``
    se queda 1 px corto por la derecha y por abajo (medido): se compensa con +1."""
    x, y, w, h = rect
    gdi32 = _gdi32()
    if radius > 0:
        d = 2 * radius
        rgn = gdi32.CreateRoundRectRgn(x, y, x + w + 1, y + h + 1, d, d)
    else:
        rgn = gdi32.CreateRectRgn(x, y, x + w, y + h)
    return int(rgn) if rgn else None


_default: Win32 | None = None


def default_api() -> Win32:
    global _default
    if _default is None:
        _default = Win32()
    return _default


# ------------------------------------------------------------------------------- estilos


def apply_no_activate(hwnd: int, api: Win32 | None = None, *, tool_window: bool = True) -> int:
    """``WS_EX_NOACTIVATE`` (+ ``TOOLWINDOW``): nunca roba el foco. Devuelve el estilo."""
    w = api or default_api()
    style = w.get_ex_style(hwnd) | WS_EX_NOACTIVATE
    if tool_window:
        style |= WS_EX_TOOLWINDOW
    w.set_ex_style(hwnd, style)
    return style


def clear_no_activate(hwnd: int, api: Win32 | None = None) -> int:
    """Permite activar la ventana (teclado en el popup tras un clic deliberado)."""
    w = api or default_api()
    style = w.get_ex_style(hwnd) & ~WS_EX_NOACTIVATE
    w.set_ex_style(hwnd, style)
    return style


def show_no_activate(hwnd: int, api: Win32 | None = None) -> None:
    """Muestra sin activar y por encima de todo."""
    w = api or default_api()
    w.show_window(hwnd, SW_SHOWNOACTIVATE)
    w.set_window_pos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def hide(hwnd: int, api: Win32 | None = None) -> None:
    (api or default_api()).show_window(hwnd, SW_HIDE)


def place(hwnd: int, rect: Rect, api: Win32 | None = None, *, topmost: bool = True) -> bool:
    """Coloca/dimensiona (pixeles fisicos ``izq, arriba, ancho, alto``) sin activar."""
    x, y, w, h = rect
    return (api or default_api()).set_window_pos(
        hwnd, HWND_TOPMOST if topmost else 0, x, y, w, h, SWP_NOACTIVATE
    )


def set_region(hwnd: int, rect: Rect | None, api: Win32 | None = None, *, radius: int = 0) -> bool:
    """Zona visible y clicable de la ventana (``None`` = toda), con esquinas de ``radius`` px."""
    return (api or default_api()).set_window_region(hwnd, rect, radius)


def is_topmost(hwnd: int, api: Win32 | None = None) -> bool:
    return bool((api or default_api()).get_ex_style(hwnd) & WS_EX_TOPMOST)


# ------------------------------------------------------------------------------ geometria

POPUP_SIZE = (372, 112)
PILL_SIZE = (200, 40)
PILL_HOVER_WIDTH = 296
CARD_SIZE = POPUP_SIZE
UNDO_WIDTH = 236
SURFACE_RADIUS = 6
"""``border-radius`` (px CSS) de la pastilla y de la tarjeta (``pill.css``): la region de la
ventana (que mide la forma) lleva las mismas esquinas, escaladas al DPI."""
POPUP_MARGIN = 24
PILL_TOP_MARGIN = 12


def _px(v: float, scale: float) -> int:
    return round(v * scale)


def popup_rect(
    mon: MonitorInfo, size: tuple[int, int] = POPUP_SIZE, margin: int = POPUP_MARGIN
) -> Rect:
    """Esquina superior derecha del area de trabajo. Devuelve ``(x, y, w, h)`` fisicos."""
    _left, top, right, _bottom = mon.work
    w, h = _px(size[0], mon.scale), _px(size[1], mon.scale)
    m = _px(margin, mon.scale)
    return (right - w - m, top + m, w, h)


def pill_rect(
    mon: MonitorInfo,
    width: int = PILL_SIZE[0],
    height: int = PILL_SIZE[1],
    top_margin: int = PILL_TOP_MARGIN,
) -> Rect:
    """Centrada arriba. ``(x, y, w, h)`` fisicos."""
    left, top, right, _bottom = mon.work
    w, h = _px(width, mon.scale), _px(height, mon.scale)
    return (left + (right - left - w) // 2, top + _px(top_margin, mon.scale), w, h)


def shape_rect(
    pill: Rect,
    size: tuple[int, int],
    mon: MonitorInfo,
    *,
    anchor: Literal["center", "left"] = "center",
) -> Rect:
    """Ventana de la superficie con la forma ``size`` (px CSS: pastilla, pastilla ancha, aviso o
    tarjeta): centrada en la pastilla (o desde su borde izquierdo), pegada a su borde superior y
    dentro del area de trabajo. ``(x, y, w, h)`` fisicos."""
    w, h = _px(size[0], mon.scale), _px(size[1], mon.scale)
    x = pill[0] if anchor == "left" else pill[0] + pill[2] // 2 - w // 2
    return clamp_to_work_area((x, pill[1], w, h), mon.work)


def region_radius(scale: float) -> int:
    """Radio (px fisicos) de las esquinas de la region para ``SURFACE_RADIUS`` CSS."""
    return max(0, _px(SURFACE_RADIUS, scale))


def clamp_to_work_area(rect: Rect, work: Rect) -> Rect:
    """Mantiene una ventana (posicion recordada) dentro de un area visible."""
    x, y, w, h = rect
    left, top, right, bottom = work
    x = max(left, min(x, right - w))
    y = max(top, min(y, bottom - h))
    return (x, y, w, h)


# --------------------------------------------------------------------------- animacion


def cubic_bezier(x1: float, y1: float, x2: float, y2: float) -> Callable[[float], float]:
    """Curva CSS ``cubic-bezier`` (por biseccion): ``f(t) -> progreso``."""

    def bez(a: float, b: float, s: float) -> float:
        return 3 * a * s * (1 - s) ** 2 + 3 * b * s**2 * (1 - s) + s**3

    def f(t: float) -> float:
        if t <= 0:
            return 0.0
        if t >= 1:
            return 1.0
        lo, hi = 0.0, 1.0
        for _ in range(32):
            mid = (lo + hi) / 2
            if bez(x1, x2, mid) < t:
                lo = mid
            else:
                hi = mid
        return bez(y1, y2, (lo + hi) / 2)

    return f


EASE = cubic_bezier(0.2, 0.8, 0.2, 1.0)


def lerp_rect(a: Rect, b: Rect, p: float) -> Rect:
    return (
        round(a[0] + (b[0] - a[0]) * p),
        round(a[1] + (b[1] - a[1]) * p),
        round(a[2] + (b[2] - a[2]) * p),
        round(a[3] + (b[3] - a[3]) * p),
    )


def animate_rect(
    hwnd: int,
    start: Rect,
    end: Rect,
    duration_s: float,
    api: Win32 | None = None,
    *,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
    easing: Callable[[float], float] = EASE,
    fps: int = 120,
) -> int:
    """Mueve/redimensiona la ventana de ``start`` a ``end`` (vuelo del popup a la pastilla).

    Bloquea ``duration_s``; pensado para un hilo auxiliar. Devuelve los fotogramas dibujados.
    ``duration_s <= 0`` coloca el destino directamente.
    """
    w = api or default_api()
    if duration_s <= 0:
        place(hwnd, end, w)
        return 1
    t0 = clock()
    frames = 0
    while True:
        p = (clock() - t0) / duration_s
        if p >= 1:
            break
        place(hwnd, lerp_rect(start, end, easing(p)), w)
        frames += 1
        sleep(1.0 / fps)
    place(hwnd, end, w)
    return frames + 1


# ------------------------------------------------------------------------------ sistema

Theme = Literal["light", "dark"]


def system_theme(api: Win32 | None = None) -> Theme:
    """Tema de aplicaciones de Windows (``AppsUseLightTheme``, solo lectura). Defecto: claro."""
    value = (api or default_api()).read_hkcu_dword(PERSONALIZE_KEY, "AppsUseLightTheme")
    return "dark" if value == 0 else "light"


def resolve_theme(setting: str, api: Win32 | None = None) -> Theme:
    """``ui.theme`` (``system`` | ``light`` | ``dark``) -> tema efectivo."""
    if setting in ("light", "dark"):
        return setting  # type: ignore[return-value]
    return system_theme(api)


def animations_enabled(api: Win32 | None = None) -> bool:
    """``False`` si «Efectos de animacion» esta apagado (``SPI_GETCLIENTAREAANIMATION``)."""
    try:
        return (api or default_api()).client_area_animation()
    except (OSError, AttributeError):  # pragma: no cover - defensivo
        return True


def do_not_disturb(api: Win32 | None = None) -> bool:
    """Mejor esfuerzo: ``True`` si Windows pide no sonar (sonido del popup en silencio).

    Combina ``SHQueryUserNotificationState`` (pantalla completa, presentacion, horas
    silenciosas) con ``NOC_GLOBAL_SETTING_TOASTS_ENABLED = 0`` (que Windows 10/11 escribe al
    activar «No molestar»/asistente de concentracion). NO hay una API documentada y fiable para
    el asistente de concentracion: ver DECISIONS en el informe de la UI.
    """
    w = api or default_api()
    try:
        state = w.notification_state()
        if state in _QUNS_SILENT:
            return True
        toasts = w.read_hkcu_dword(NOTIFICATIONS_KEY, "NOC_GLOBAL_SETTING_TOASTS_ENABLED")
    except (OSError, AttributeError):  # pragma: no cover - defensivo
        return False
    return toasts == 0


def set_backdrop(
    hwnd: int,
    kind: Literal["mica", "acrylic"] = "mica",
    *,
    dark: bool = False,
    api: Win32 | None = None,
) -> bool:
    """Mica/acrilico con ``DWMWA_SYSTEMBACKDROP_TYPE`` (Windows 11 22H2+). ``False`` si no aplica.

    Nota: para verse, la ventana web debe tener fondo transparente y el marco extendido al area
    cliente; **sin verificar visualmente** en esta maquina (sin escritorio interactivo). Windows
    10 (build < 22621) devuelve ``False`` y la UI se queda con el color solido.
    """
    w = api or default_api()
    if w.windows_build() < MICA_MIN_BUILD:
        return False
    w.dwm_set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
    w.dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, 2)
    return w.dwm_set(
        hwnd,
        DWMWA_SYSTEMBACKDROP_TYPE,
        DWMSBT_MAINWINDOW if kind == "mica" else DWMSBT_TRANSIENTWINDOW,
    )


def acrylic_supported(api: Win32 | None = None) -> bool:
    """Acrilico real de DWM (``DWMSBT_TRANSIENTWINDOW``): Windows 11 22H2 (22621) o posterior."""
    return (api or default_api()).windows_build() >= MICA_MIN_BUILD


def clear_backdrop(hwnd: int, api: Win32 | None = None) -> bool:
    """Quita el fondo de sistema de DWM (``DWMWA_SYSTEMBACKDROP_TYPE = DWMSBT_NONE``).

    pywebview (``BrowserForm.update_title_bar_theme``) pone Mica (``DWMSBT_MAINWINDOW``) en TODA
    ventana cuando las apps usan el tema oscuro. Con el marco extendido al cliente (la antigua
    superficie transparente) DWM pintaba ese Mica (gris liso: la ventana nunca esta activa) en el
    rectangulo ENTERO de la ventana sin que la region lo recortase: era el recuadro gris (medido,
    DECISIONS §29). La superficie ya es opaca y sin marco extendido; se quita igualmente. Antes
    de Windows 11 22H2 el atributo no existe (no hay Mica que quitar): ``True``.
    """
    w = api or default_api()
    if w.windows_build() < MICA_MIN_BUILD:
        return True
    return w.dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE)


def ensure_dpi_aware() -> None:
    """Igual que pywebview (``SetProcessDPIAware``): geometria en pixeles fisicos coherente.

    Idempotente y silencioso si ya esta fijado.
    """
    with contextlib.suppress(AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()
