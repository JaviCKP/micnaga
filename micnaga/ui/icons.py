"""Iconos de Micnaga (app y bandeja), dibujados desde geometria con Pillow.

Motivo: los circulos concentricos de la marca de Bitnaga (cada nodo del logotipo son anillos
finos alrededor de un centro). La app es una baldosa nocturna con anillos blancos perfectamente
concentricos, del mismo grosor y con el mismo hueco entre ellos (tambien entre el punto y el
primer anillo), y un punto central violeta: el unico acento.

Nitidez
-------
Cada tamano se dibuja a su resolucion con su propia tabla (``_APP``/``_TRAY``): grosor, radios y
punto en pixeles. Centro en la esquina de pixel central; con trazo de 1 px el radio es
``k + 0,5`` y con trazo par, entero: asi los cuatro extremos de cada anillo caen exactos en la
rejilla y solo las diagonales llevan antialiasing. A 16-24 px hay dos anillos (tres se
empastarian); desde 32, tres. Por debajo de 32 px el antialiasing se «endurece» un poco
(``_hint``) para que el trazo no se vea gris.

Bandeja
-------
Monocromo como los iconos del sistema, en dos tintas: blanca para barra de tareas oscura y casi
negra para la clara (``taskbar_theme`` lee ``SystemUsesLightTheme``). El violeta solo marca la
grabacion. Sin baldosa: el glifo usa todo el cuadro.

============ =========================================================
estado       glifo
============ =========================================================
watching     dos anillos y un punto (en reposo, escuchando)
recording    anillo exterior + punto violeta grande (grabando)
provisional  anillo exterior + anillo violeta (grabando sin confirmar)
paused       anillo exterior + dos barras de pausa
processing   anillo exterior + tres puntos (trabajando)
============ =========================================================

``scripts/make_icons.py`` regenera ``web/assets/micnaga.ico`` y ``tray-<estado>-<tema>.ico``; la
bandeja carga el fotograma del tamano exacto del sistema (``SM_CXSMICON``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from micnaga.ui.contracts import TrayState

if TYPE_CHECKING:
    from PIL import Image

NIGHT = (11, 11, 16)
WHITE = (255, 255, 255)
BRAND_VIOLET = (167, 139, 250)
"""Acento sobre oscuro (``--accent`` de tokens.css en oscuro)."""
BRAND_VIOLET_DEEP = (94, 52, 214)
"""Acento sobre claro (mas profundo: contraste con la barra de tareas clara)."""
INK_LIGHT_BAR = (26, 26, 31)
APP_COLOR = NIGHT
THEMES: tuple[str, ...] = ("dark", "light")
"""Tema de la BARRA DE TAREAS: ``dark`` = tinta blanca; ``light`` = tinta oscura."""
INK = {"dark": WHITE, "light": INK_LIGHT_BAR}
ACCENT = {"dark": BRAND_VIOLET, "light": BRAND_VIOLET_DEEP}
STATE_COLORS: dict[str, tuple[int, int, int]] = {
    "watching": WHITE,
    "recording": BRAND_VIOLET,
    "provisional": BRAND_VIOLET,
    "paused": WHITE,
    "processing": WHITE,
}
"""Color del glifo que distingue cada estado en barra oscura (el violeta solo al grabar)."""
TRAY_STATES: tuple[str, ...] = ("watching", "recording", "provisional", "paused", "processing")
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
TRAY_SIZES = (16, 20, 24, 32, 40, 48, 64)
ASSETS_DIR = Path(__file__).resolve().parent / "web" / "assets"
ICO_PATH = ASSETS_DIR / "micnaga.ico"
_SS = 8  # supermuestreo


def tray_ico_path(state: str, theme: str = "dark") -> Path:
    return ASSETS_DIR / f"tray-{state}-{theme}.ico"


# =============================================================================== geometria


@dataclass(frozen=True)
class Rings:
    """Anillos concentricos en pixeles del tamano final (centro en ``size / 2``)."""

    stroke: float
    radii: tuple[float, ...]
    """Radio de la linea media de cada anillo, de dentro afuera."""
    dot: float
    """Radio del punto central."""


# Tablas a mano (16-32). Huecos iguales: punto -> anillo 1 -> anillo 2 (-> anillo 3).
_APP: dict[int, Rings] = {
    16: Rings(1, (2.5, 5.5), 1),
    20: Rings(1, (3.5, 6.5), 1.5),
    24: Rings(1, (4.5, 7.5), 2),
    32: Rings(1, (5.5, 8.5, 11.5), 3),
}

# Geometria de referencia (fracciones del lado) para 40 px en adelante: trazo, punto y hueco
# iguales entre el punto y el primer anillo y entre anillos.
_REF_STROKE = 0.024
_REF_DOT = 0.07
_REF_GAP = 0.06
RING_FADE: dict[int, tuple[float, ...]] = {2: (1.0, 0.55), 3: (1.0, 0.66, 0.36)}
"""Opacidad de cada anillo, de dentro afuera: mismo grosor, la onda se apaga al alejarse."""


def _snap_radius(r: float, stroke: float) -> float:
    """Radio que deja los bordes del trazo en pixeles enteros en los extremos de los ejes."""
    if int(stroke) % 2 == 1:
        return int(r) + 0.5
    return float(round(r))


def rings_from(stroke: int, dot: int, gap: int, count: int = 3) -> Rings:
    """Anillos con huecos identicos; con ``dot`` y ``gap`` enteros los bordes caen en la rejilla."""
    first = dot + gap + stroke / 2
    return Rings(stroke, tuple(first + i * (gap + stroke) for i in range(count)), dot)


def app_rings(size: int) -> Rings:
    if size in _APP:
        return _APP[size]
    stroke = max(1, round(size * _REF_STROKE))
    return rings_from(stroke, round(size * _REF_DOT), round(size * _REF_GAP))


# =============================================================================== dibujo


def _mask(size: int) -> Image.Image:
    from PIL import Image

    return Image.new("L", (size * _SS, size * _SS), 0)


def _disc(m: Image.Image, size: int, r: float, value: int = 255) -> None:
    from PIL import ImageDraw

    c = size / 2 * _SS
    rr = r * _SS
    ImageDraw.Draw(m).ellipse((c - rr, c - rr, c + rr - 1, c + rr - 1), fill=value)


def _ring(m: Image.Image, size: int, r: float, stroke: float) -> None:
    _disc(m, size, r + stroke / 2, 255)
    _disc(m, size, r - stroke / 2, 0)


def _rect(m: Image.Image, x0: float, y0: float, x1: float, y1: float) -> None:
    from PIL import ImageDraw

    ImageDraw.Draw(m).rectangle(
        (round(x0 * _SS), round(y0 * _SS), round(x1 * _SS) - 1, round(y1 * _SS) - 1), fill=255
    )


def _hint(alpha: Image.Image, size: int) -> Image.Image:
    """A 16-24 px, endurece el antialiasing de los anillos de 1 px (menos gris, mas linea)."""
    if size >= 32:
        return alpha

    def curve(v: int) -> int:
        x = v / 255
        y = min(1.0, max(0.0, (x - 0.12) / 0.7))
        return round(255 * y)

    return alpha.point([curve(v) for v in range(256)])


def _reduce(m: Image.Image, size: int) -> Image.Image:
    """Supermuestreo -> tamano final. Antes se promedia con sus reflejos: el rasterizado de
    elipses de Pillow no es simetrico al ultimo nivel y los anillos deben serlo exactamente."""
    from PIL import Image

    return _hint(_sym(m).resize((size, size), Image.Resampling.BOX), size)


def _sym(m: Image.Image) -> Image.Image:
    from PIL import Image

    m = Image.blend(m, m.transpose(Image.Transpose.FLIP_LEFT_RIGHT), 0.5)
    return Image.blend(m, m.transpose(Image.Transpose.FLIP_TOP_BOTTOM), 0.5)


def _paint(base: Image.Image, alpha: Image.Image, color: tuple[int, int, int]) -> None:
    from PIL import Image

    layer = Image.new("RGBA", base.size, (*color, 255))
    layer.putalpha(alpha)
    base.alpha_composite(layer)


def _tile_radius(size: int) -> float:
    return {16: 3.5, 20: 4.5, 24: 5.5, 32: 7.0}.get(size, size * 0.225)


def _tile(size: int) -> Image.Image:
    """Baldosa nocturna lisa con un filo interior de 1 px (define el borde en fondos oscuros)."""
    from PIL import Image, ImageDraw

    s = size * _SS
    r = _tile_radius(size) * _SS
    shape = Image.new("L", (s, s), 0)
    ImageDraw.Draw(shape).rounded_rectangle((0, 0, s - 1, s - 1), radius=r, fill=255)
    inner = Image.new("L", (s, s), 0)
    w = _SS
    ImageDraw.Draw(inner).rounded_rectangle((w, w, s - 1 - w, s - 1 - w), radius=r - w, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shape, inner = _sym(shape), _sym(inner)
    _paint(out, shape.resize((size, size), Image.Resampling.BOX), NIGHT)
    rim = Image.new("L", (s, s), 0)
    rim.paste(shape, (0, 0))
    rim.paste(0, (0, 0), inner)
    level = 0.16 if size < 32 else 0.11
    rim_a = rim.resize((size, size), Image.Resampling.BOX).point(lambda v: round(v * level))
    _paint(out, rim_a, WHITE)
    return out


def app_icon(size: int = 256) -> Image.Image:
    """Icono de la app: baldosa nocturna, anillos blancos concentricos y punto violeta."""
    geo = app_rings(size)
    img = _tile(size)
    fade = RING_FADE.get(len(geo.radii), (1.0,) * len(geo.radii))
    for r, alpha in zip(geo.radii, fade, strict=True):
        ring = _mask(size)
        _ring(ring, size, r, geo.stroke)
        _paint(img, _reduce(ring, size).point(lambda v, a=alpha: round(v * a)), WHITE)
    dot = _mask(size)
    _disc(dot, size, geo.dot)
    _paint(img, _reduce(dot, size), BRAND_VIOLET)
    return img


# ----------------------------------------------------------------------------- bandeja


@dataclass(frozen=True)
class TrayGeo:
    stroke: float
    outer: float
    inner: float
    """Segundo anillo (reposo)."""
    dot: float
    """Punto pequeno del reposo."""
    rec: float
    """Punto de grabar."""
    pause: tuple[float, float, float]
    """Barra: ancho, hueco, alto."""
    work: tuple[float, float]
    """Tres puntos de «procesando»: lado, hueco."""


_TRAY: dict[int, TrayGeo] = {
    16: TrayGeo(1, 6.5, 3.5, 1.5, 3.5, (2, 2, 6), (2, 1)),
    20: TrayGeo(1, 8.5, 4.5, 2, 4.5, (2, 2, 8), (2, 1)),
    24: TrayGeo(2, 10, 5, 2, 5, (3, 2, 10), (2, 2)),
    32: TrayGeo(2, 14, 8, 3, 7, (4, 4, 12), (4, 2)),
}


def tray_geo(size: int) -> TrayGeo:
    if size in _TRAY:
        return _TRAY[size]
    k = size / 32
    g = _TRAY[32]
    stroke = max(2, round(g.stroke * k))
    outer = size / 2 - stroke / 2 - max(1, round(k))  # 1 px de aire, como a 16
    return TrayGeo(
        stroke,
        outer,
        _snap_radius(g.inner * k, stroke),
        round(g.dot * k * 2) / 2,
        round(g.rec * k * 2) / 2,
        (round(g.pause[0] * k), round(g.pause[1] * k), round(g.pause[2] * k / 2) * 2),
        (round(g.work[0] * k), round(g.work[1] * k)),
    )


def render_state(state: str, size: int, theme: str = "dark") -> Image.Image:
    """Icono de bandeja de ``state`` a ``size`` px para una barra de tareas ``theme``."""
    from PIL import Image

    if state not in STATE_COLORS:
        raise ValueError(f"unknown tray state: {state!r}")
    if theme not in THEMES:
        raise ValueError(f"unknown taskbar theme: {theme!r}")
    ink, accent = INK[theme], ACCENT[theme]
    g = tray_geo(size)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    base = _mask(size)
    _ring(base, size, g.outer, g.stroke)
    extra = _mask(size)
    violet = _mask(size)
    c = size / 2
    if state == "watching":
        _ring(base, size, g.inner, g.stroke)
        _disc(base, size, g.dot)
    elif state == "recording":
        _disc(violet, size, g.rec)
    elif state == "provisional":
        _ring(violet, size, g.inner, g.stroke)  # el punto de grabar, aun hueco
    elif state == "paused":
        w, gap, h = g.pause
        x0 = c - (2 * w + gap) / 2
        _rect(extra, x0, c - h / 2, x0 + w, c + h / 2)
        _rect(extra, x0 + w + gap, c - h / 2, x0 + 2 * w + gap, c + h / 2)
    else:  # processing
        side, gap = g.work
        x0 = c - (3 * side + 2 * gap) / 2
        for i in range(3):
            x = x0 + i * (side + gap)
            _rect(extra, x, c - side / 2, x + side, c + side / 2)
    _paint(img, _reduce(base, size), ink)
    _paint(img, _reduce(extra, size), ink)
    _paint(img, _reduce(violet, size), accent)
    return img


def tray_icon(state: TrayState | str, size: int = 64, theme: str | None = None) -> Image.Image:
    """Icono de bandeja para ``state`` (``watching`` | ``recording`` | ``provisional`` |
    ``paused`` | ``processing``) en el tema de la barra de tareas actual. Lleva
    ``info["micnaga_state"]`` para que la bandeja cargue el fotograma exacto del ``.ico``."""
    th = theme or taskbar_theme()
    img = render_state(str(state), size, th)
    img.info["micnaga_state"] = str(state)
    return img


def taskbar_theme() -> str:
    """``light`` si la barra de tareas es clara (``SystemUsesLightTheme`` = 1), si no ``dark``."""
    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value, _ = winreg.QueryValueEx(k, "SystemUsesLightTheme")
        return "light" if int(value) == 1 else "dark"
    except (ImportError, OSError, ValueError, TypeError):
        return "dark"


# =============================================================================== ficheros


def write_multi_ico(path: Path, frames: dict[int, Image.Image]) -> Path:
    """``.ico`` con un fotograma propio por tamano (nunca uno reducido de otro)."""
    order = sorted(frames)
    big = frames[order[-1]]
    path.parent.mkdir(parents=True, exist_ok=True)
    big.save(
        path,
        format="ICO",
        sizes=[(n, n) for n in order],
        append_images=[frames[n] for n in order[:-1]],
    )
    return path


def write_ico(path: Path = ICO_PATH, sizes: tuple[int, ...] = ICO_SIZES) -> Path:
    """Escribe el ``.ico`` de la app (cada tamano dibujado a su resolucion)."""
    return write_multi_ico(path, {n: app_icon(n) for n in sizes})


def write_tray_icos(folder: Path = ASSETS_DIR, sizes: tuple[int, ...] = TRAY_SIZES) -> list[Path]:
    return [
        write_multi_ico(folder / f"tray-{s}-{t}.ico", {n: render_state(s, n, t) for n in sizes})
        for s in TRAY_STATES
        for t in THEMES
    ]


# =============================================================================== bandeja Win32


def small_icon_size() -> int:
    """Tamano de icono pequeno del sistema (``SM_CXSMICON``: 16 a 100 %, 24 a 150 %...)."""
    try:
        import ctypes

        value = int(ctypes.windll.user32.GetSystemMetrics(49))
    except (AttributeError, OSError):
        return 16
    return value if 8 <= value <= 256 else 16


def load_tray_hicon(image: Any) -> int | None:
    """HICON del fotograma exacto (``SM_CXSMICON``) de ``tray-<estado>-<tema>.ico`` para
    ``image`` (salida de ``tray_icon``), o ``None`` si no hay fichero (que pystray haga lo
    suyo)."""
    state = getattr(image, "info", {}).get("micnaga_state")
    if not isinstance(state, str):
        return None
    path = tray_ico_path(state, taskbar_theme())
    if not path.is_file():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        cx = small_icon_size()
        handle = user32.LoadImageW(None, str(path), 1, cx, cx, 0x10)  # IMAGE_ICON, FROMFILE
    except (AttributeError, OSError):
        return None
    return int(handle) if handle else None


def make_pystray_icon(name: str, image: Any, title: str, menu: Any) -> Any:
    """``pystray.Icon`` que, en Windows, carga el fotograma del tamano exacto de la bandeja.

    pystray guarda la imagen como ICO y la carga con ``LR_DEFAULTSIZE`` (32 px): Windows la
    reduce a 16 y queda borrosa. Aqui se sustituye ``_assert_icon_handle`` (privado de pystray,
    con respaldo: si algo falla se usa el suyo)."""
    import pystray

    base: Any = pystray.Icon
    if not hasattr(base, "_assert_icon_handle"):  # otro backend (no Win32)
        return base(name, image, title, menu)

    class _Icon(base):  # type: ignore[misc]
        def _assert_icon_handle(self) -> None:
            if getattr(self, "_icon_handle", None):
                return
            handle = load_tray_hicon(self.icon)
            if handle:
                self._icon_handle = handle
                return
            super()._assert_icon_handle()

    return _Icon(name, image, title, menu)


if __name__ == "__main__":  # pragma: no cover
    print(write_ico())
    for p in write_tray_icos():
        print(p)
