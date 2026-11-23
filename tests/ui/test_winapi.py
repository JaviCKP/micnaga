"""Logica Win32 de la UI con un Win32 falso (estilos, geometria, DND, tema, Mica, animacion)."""

from __future__ import annotations

import pytest

from micnaga.ui import winapi
from tests.ui.fakes import FakeWin32


def test_apply_no_activate_sets_noactivate_and_toolwindow() -> None:
    w = FakeWin32()
    w.styles[7] = winapi.WS_EX_TOPMOST
    style = winapi.apply_no_activate(7, w)
    assert style & winapi.WS_EX_NOACTIVATE
    assert style & winapi.WS_EX_TOOLWINDOW
    assert style & winapi.WS_EX_TOPMOST  # no pisa los estilos existentes
    assert w.styles[7] == style
    assert winapi.apply_no_activate(7, w) == style  # idempotente


def test_clear_no_activate_keeps_other_bits() -> None:
    w = FakeWin32()
    winapi.apply_no_activate(3, w)
    style = winapi.clear_no_activate(3, w)
    assert not style & winapi.WS_EX_NOACTIVATE
    assert style & winapi.WS_EX_TOOLWINDOW


def test_show_no_activate_uses_shownoactivate_and_topmost_without_activation() -> None:
    w = FakeWin32()
    winapi.show_no_activate(5, w)
    assert ("show_window", (5, winapi.SW_SHOWNOACTIVATE)) in w.calls
    pos = next(c for c in w.calls if c[0] == "set_window_pos")[1]
    assert pos[1] == winapi.HWND_TOPMOST
    assert pos[6] & winapi.SWP_NOACTIVATE
    assert pos[6] & winapi.SWP_NOMOVE and pos[6] & winapi.SWP_NOSIZE


def test_place_never_activates() -> None:
    w = FakeWin32()
    winapi.place(1, (10, 20, 500, 290), w)
    assert w.places(1) == [(10, 20, 500, 290)]
    assert int(w.calls[-1][1][6]) & winapi.SWP_NOACTIVATE  # type: ignore[call-overload]


def test_popup_and_pill_geometry_scale_with_dpi() -> None:
    mon = winapi.MonitorInfo((0, 0, 1920, 1040), 1.25)
    px, py, pw, ph = winapi.popup_rect(mon)
    assert (pw, ph) == (465, 140)  # 372x112 CSS px at 125 %
    assert px + pw + 30 == 1920 and py == 30  # 24 px margin, top-right
    x, y, w, h = winapi.pill_rect(mon)
    assert (w, h) == (250, 50)
    assert x == (1920 - 250) // 2 and y == 15  # centred at the top


def test_geometry_on_secondary_monitor_with_offset() -> None:
    mon = winapi.MonitorInfo((-1920, 0, 0, 1040), 1.0)
    assert winapi.popup_rect(mon)[0] == -1920 + 1920 - 372 - 24
    assert winapi.pill_rect(mon)[0] == -1920 + (1920 - 200) // 2


def test_clamp_to_work_area_keeps_remembered_pill_visible() -> None:
    work = (0, 0, 1920, 1040)
    assert winapi.clamp_to_work_area((3000, -50, 250, 50), work) == (1670, 0, 250, 50)
    assert winapi.clamp_to_work_area((100, 100, 250, 50), work) == (100, 100, 250, 50)


def test_cubic_bezier_matches_css_ease_endpoints_and_is_monotonic() -> None:
    f = winapi.EASE
    assert f(0) == 0 and f(1) == 1
    vals = [f(i / 50) for i in range(51)]
    assert vals == sorted(vals)
    assert f(0.25) > 0.6  # curva (0.2, 0.8, 0.2, 1): sale rapido
    linear = winapi.cubic_bezier(0, 0, 1, 1)
    assert linear(0.37) == pytest.approx(0.37, abs=1e-3)


def test_animate_rect_interpolates_with_fake_clock() -> None:
    w = FakeWin32()
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(dt: float) -> None:
        now[0] += dt

    frames = winapi.animate_rect(
        9, (0, 0, 500, 290), (400, 100, 500, 290), 0.4, w, clock=clock, sleep=sleep, fps=50
    )
    rects = w.places(9)
    assert frames == len(rects) > 10
    assert rects[-1] == (400, 100, 500, 290)  # aterriza exacto
    xs = [r[0] for r in rects]
    assert xs == sorted(xs)


def test_animate_rect_zero_duration_places_target_once() -> None:
    w = FakeWin32()
    winapi.animate_rect(2, (0, 0, 10, 10), (5, 5, 10, 10), 0, w)
    assert w.places(2) == [(5, 5, 10, 10)]


def test_system_theme_and_resolution() -> None:
    w = FakeWin32()
    assert winapi.system_theme(w) == "light"  # sin valor => claro
    w.hkcu[(winapi.PERSONALIZE_KEY, "AppsUseLightTheme")] = 0
    assert winapi.system_theme(w) == "dark"
    assert winapi.resolve_theme("light", w) == "light"
    assert winapi.resolve_theme("system", w) == "dark"
    w.hkcu[(winapi.PERSONALIZE_KEY, "AppsUseLightTheme")] = 1
    assert winapi.resolve_theme("system", w) == "light"


def test_animations_enabled_follows_windows_setting() -> None:
    w = FakeWin32()
    assert winapi.animations_enabled(w)
    w.animation = False
    assert not winapi.animations_enabled(w)


@pytest.mark.parametrize(
    ("state", "toasts", "dnd"),
    [
        (winapi.QUNS_ACCEPTS_NOTIFICATIONS, None, False),
        (winapi.QUNS_QUIET_TIME, None, True),
        (winapi.QUNS_BUSY, None, True),
        (winapi.QUNS_PRESENTATION_MODE, None, True),
        (winapi.QUNS_RUNNING_D3D_FULL_SCREEN, None, True),
        (winapi.QUNS_NOT_PRESENT, None, False),  # usuario ausente: no es DND
        (winapi.QUNS_ACCEPTS_NOTIFICATIONS, 0, True),  # toasts desactivados globalmente
        (winapi.QUNS_ACCEPTS_NOTIFICATIONS, 1, False),
        (None, None, False),
    ],
)
def test_do_not_disturb_combines_shell_state_and_toast_flag(
    state: int | None, toasts: int | None, dnd: bool
) -> None:
    w = FakeWin32()
    w.qstate = state
    if toasts is not None:
        w.hkcu[(winapi.NOTIFICATIONS_KEY, "NOC_GLOBAL_SETTING_TOASTS_ENABLED")] = toasts
    assert winapi.do_not_disturb(w) is dnd


def test_set_backdrop_mica_on_windows_11_sets_system_backdrop_type() -> None:
    w = FakeWin32()
    assert winapi.set_backdrop(11, "mica", dark=True, api=w)
    assert (11, winapi.DWMWA_SYSTEMBACKDROP_TYPE, winapi.DWMSBT_MAINWINDOW) in w.dwm
    assert (11, winapi.DWMWA_USE_IMMERSIVE_DARK_MODE, 1) in w.dwm


def test_set_backdrop_falls_back_on_windows_10() -> None:
    w = FakeWin32()
    w.build = 19045
    assert winapi.set_backdrop(11, "mica", api=w) is False
    assert w.dwm == []  # no se toca nada: color solido


def test_set_backdrop_reports_dwm_failure() -> None:
    w = FakeWin32()
    w.dwm_ok = False
    assert winapi.set_backdrop(11, "acrylic", api=w) is False


def test_real_system_queries_are_read_only_and_sane() -> None:
    """Contra el Windows real de esta maquina (solo lectura)."""
    api = winapi.Win32()
    assert winapi.system_theme(api) in ("light", "dark")
    assert isinstance(winapi.animations_enabled(api), bool)
    assert isinstance(winapi.do_not_disturb(api), bool)
    mon = api.monitor_info(None)
    assert mon.scale > 0 and mon.work[2] > mon.work[0]


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5])
def test_rounded_region_covers_the_whole_window_minus_the_corners(scale: float) -> None:
    """``CreateRoundRectRgn`` se queda 1 px corto por la derecha y por abajo: ``create_region``
    lo compensa. La region llega justo hasta el borde de la ventana (ni falta ni sobra)."""
    import ctypes

    w, h = round(200 * scale), round(40 * scale)
    radius = winapi.region_radius(scale)
    rgn = winapi.create_region((0, 0, w, h), radius)
    assert rgn
    gdi = winapi._gdi32()
    try:
        inside = [(w - 1, h // 2), (w // 2, h - 1), (0, h // 2), (w // 2, 0), (radius, radius)]
        outside = [(w, h // 2), (w // 2, h), (0, 0), (w - 1, h - 1), (w - 1, 0), (0, h - 1)]
        assert all(gdi.PtInRegion(rgn, x, y) for x, y in inside)
        assert not any(gdi.PtInRegion(rgn, x, y) for x, y in outside)
    finally:
        gdi.DeleteObject(ctypes.c_void_p(rgn))


def test_clear_backdrop_removes_mica_on_windows_11_only() -> None:
    w = FakeWin32()
    assert winapi.clear_backdrop(9, w)
    assert w.dwm == [(9, winapi.DWMWA_SYSTEMBACKDROP_TYPE, winapi.DWMSBT_NONE)]
    w = FakeWin32()
    w.build = 19045  # Windows 10: no hay Mica que quitar
    assert winapi.clear_backdrop(9, w) and w.dwm == []
    w = FakeWin32()
    w.dwm_ok = False
    assert not winapi.clear_backdrop(9, w)


@pytest.mark.parametrize(
    ("scale", "pill", "stage", "layout"),
    [
        (1.0, (860, 12, 200, 40), (750, 12, 420, 112), {"px": 210.0, "py": 0.0, "cardL": 24.0}),
        (1.25, (835, 15, 250, 50), (698, 15, 525, 140), {"px": 209.6, "py": 0.0, "cardL": 23.6}),
        (1.5, (810, 18, 300, 60), (645, 18, 630, 168), {"px": 210.0, "py": 0.0, "cardL": 24.0}),
    ],
)
def test_stage_geometry_scales_with_dpi(
    scale: float,
    pill: tuple[int, int, int, int],
    stage: tuple[int, int, int, int],
    layout: dict[str, float],
) -> None:
    mon = winapi.MonitorInfo((0, 0, 1920, 1040), scale)
    assert winapi.pill_rect(mon) == pill
    assert winapi.stage_rect(pill, mon) == stage
    assert winapi.stage_layout(pill, stage, scale) == layout


def _rounded_distance(
    x: float, y: float, rect: tuple[float, float, float, float], r: float
) -> float:
    """Distancia (px) de un punto al rectangulo redondeado (negativa dentro)."""
    left, top, w, h = rect
    cx, cy = left + w / 2, top + h / 2
    qx, qy = abs(x - cx) - (w / 2 - r), abs(y - cy) - (h / 2 - r)
    outside = float((max(qx, 0.0) ** 2 + max(qy, 0.0) ** 2) ** 0.5)
    return outside + min(max(qx, qy), 0.0) - r


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5])
@pytest.mark.parametrize("box", [(109.6, 0.0, 200.0, 40.0), (23.6, 0.0, 372.0, 112.0)])
def test_region_never_cuts_the_antialiased_corners(
    scale: float, box: tuple[float, float, float, float]
) -> None:
    """El CSS dibuja las esquinas redondeadas con antialias; la region (GDI, borde duro) va un
    poco por fuera: contiene todo pixel que la forma toca y deja fuera lo que esta lejos."""
    import ctypes

    region = winapi.css_region(box, scale)
    radius = winapi.region_radius(scale)
    rgn = winapi.create_region(region, radius)
    assert rgn
    gdi = winapi._gdi32()
    shape = tuple(v * scale for v in box)
    r = winapi.SURFACE_RADIUS * scale
    try:
        x0, y0 = int(shape[0]) - 8, int(shape[1]) - 8
        for y in range(y0, int(shape[1] + shape[3]) + 8):
            for x in range(x0, int(shape[0] + shape[2]) + 8):
                d = _rounded_distance(x + 0.5, y + 0.5, shape, r)  # type: ignore[arg-type]
                inside = bool(gdi.PtInRegion(rgn, x, y))
                if d <= 0.75:  # la forma (o su antialias) toca este pixel
                    assert inside, (x, y, d)
                elif d >= winapi.REGION_OUTSET + 2.5:
                    assert not inside, (x, y, d)
    finally:
        gdi.DeleteObject(ctypes.c_void_p(rgn))


def test_pywebview_theme_updates_never_bring_mica_back_on_the_surface() -> None:
    """pywebview pone Mica en ``update_title_bar_theme`` (al crear la ventana y en cada cambio
    de preferencias del sistema): el gancho lo quita justo despues, solo en la superficie."""
    from micnaga.ui import windows

    w32 = FakeWin32()

    class Handle:
        def __init__(self, v: int) -> None:
            self.v = v

        def ToInt64(self) -> int:
            return self.v

    class Form:  # lo que hace pywebview con el tema oscuro
        def __init__(self, hwnd: int) -> None:
            self.Handle = Handle(hwnd)

        def update_title_bar_theme(self) -> None:
            w32.dwm_set(self.Handle.v, winapi.DWMWA_SYSTEMBACKDROP_TYPE, winapi.DWMSBT_MAINWINDOW)

    class Win:
        def __init__(self, hwnd: int) -> None:
            self.native = Form(hwnd)

    surface, other = Win(501), Win(502)
    windows._keep_backdrop_clear(surface, 501, w32)
    windows._keep_backdrop_clear(surface, 501, w32)  # idempotente: un solo gancho por clase
    w32.dwm.clear()
    surface.native.update_title_bar_theme()
    other.native.update_title_bar_theme()
    mica, none = winapi.DWMSBT_MAINWINDOW, winapi.DWMSBT_NONE
    attr = winapi.DWMWA_SYSTEMBACKDROP_TYPE
    assert w32.dwm == [(501, attr, mica), (501, attr, none), (502, attr, mica)]
