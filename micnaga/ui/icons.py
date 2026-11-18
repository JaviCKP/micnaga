"""Iconos generados en tiempo de ejecucion con Pillow (bandeja) y el ``.ico`` de la aplicacion.

Identidad Bitnaga reinterpretada para 16 px: un disco por estado (color *y* glifo distintos,
para que no dependa solo del color) y, como marca, los anillos de «escucha» del logotipo de
Bitnaga alrededor del punto de grabar.

=========== ======================== ===============================
estado      color                    glifo
=========== ======================== ===============================
watching    grafito (gris violaceo)  anillo + punto (escuchando)
recording   rojo coral               punto central
paused      ambar                    dos barras de pausa
processing  violeta Bitnaga          ondas (tres barras)
=========== ======================== ===============================

El estado «procesando» es violeta (el acento de la marca) en vez del azul de la spec §10: sigue
siendo frio y distinto de los otros tres a 16 px.

``python -m micnaga.ui.icons`` regenera ``web/assets/micnaga.ico`` (icono del instalador).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from micnaga.ui.contracts import TrayState

if TYPE_CHECKING:
    from PIL import Image

STATE_COLORS: dict[str, tuple[int, int, int]] = {
    "watching": (88, 86, 104),
    "recording": (229, 62, 70),
    "paused": (240, 158, 22),
    "processing": (124, 77, 232),
}
NIGHT = (11, 11, 16)
BRAND_VIOLET = (167, 139, 250)
REC_DOT = (255, 77, 85)
APP_COLOR = NIGHT
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
ASSETS_DIR = Path(__file__).resolve().parent / "web" / "assets"
ICO_PATH = ASSETS_DIR / "micnaga.ico"
_SS = 4  # supersampling

_WHITE = (255, 255, 255, 255)


def _draw_glyph(d: object, glyph: str, s: int) -> None:
    from PIL import ImageDraw

    assert isinstance(d, ImageDraw.ImageDraw)
    c = s / 2
    if glyph == "ring":
        ring = max(2, round(s * 0.075))
        r = s * 0.25
        d.ellipse((c - r, c - r, c + r, c + r), outline=_WHITE, width=ring)
        r = s * 0.095
        d.ellipse((c - r, c - r, c + r, c + r), fill=_WHITE)
    elif glyph == "dot":
        r = s * 0.18
        d.ellipse((c - r, c - r, c + r, c + r), fill=_WHITE)
    elif glyph == "bars":
        w, h = s * 0.1, s * 0.36
        for dx in (-s * 0.11, s * 0.11):
            d.rounded_rectangle(
                (c + dx - w / 2, c - h / 2, c + dx + w / 2, c + h / 2), radius=w / 2, fill=_WHITE
            )
    elif glyph == "wave":
        w = s * 0.085
        for dx, hh in ((-s * 0.18, 0.18), (0, 0.38), (s * 0.18, 0.25)):
            h = s * hh
            d.rounded_rectangle(
                (c + dx - w / 2, c - h / 2, c + dx + w / 2, c + h / 2), radius=w / 2, fill=_WHITE
            )


_GLYPH = {"watching": "ring", "recording": "dot", "paused": "bars", "processing": "wave"}


def _disc(color: tuple[int, int, int], s: int) -> Image.Image:
    """Disco de ``color`` con un brillo superior muy sutil (una sola capa) y borde nitido."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    pad = s * 0.04
    ImageDraw.Draw(img).ellipse((pad, pad, s - pad, s - pad), fill=(*color, 255))
    hi = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(hi).ellipse((pad, pad, s - pad, s * 0.6), fill=(255, 255, 255, 20))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).ellipse((pad, pad, s - pad, s - pad), fill=255)
    return Image.composite(Image.alpha_composite(img, hi), img, mask)


def render(color: tuple[int, int, int], glyph: str, size: int) -> Image.Image:
    """Disco de ``color`` con un glifo blanco, antialiasing por supermuestreo."""
    from PIL import Image, ImageDraw

    s = size * _SS
    img = _disc(color, s)
    _draw_glyph(ImageDraw.Draw(img), glyph, s)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def tray_icon(state: TrayState | str, size: int = 64) -> Image.Image:
    """Icono de bandeja para ``state`` (watching | recording | paused | processing)."""
    if state not in STATE_COLORS:
        raise ValueError(f"unknown tray state: {state!r}")
    return render(STATE_COLORS[state], _GLYPH[state], size)


def app_icon(size: int = 256) -> Image.Image:
    """Icono de la aplicacion: disco nocturno, anillos violeta de Bitnaga y el punto de grabar.

    Por debajo de 32 px se queda con un solo anillo (los finos desaparecerian).
    """
    from PIL import Image, ImageDraw

    s = size * _SS
    img = _disc(NIGHT, s)
    d = ImageDraw.Draw(img)
    c = s / 2
    pad = s * 0.04
    d.ellipse(
        (pad, pad, s - pad, s - pad), outline=(255, 255, 255, 34), width=max(1, round(s * 0.012))
    )
    rings = ((0.34, 0.45), (0.25, 0.75)) if size >= 32 else ((0.27, 0.95),)
    w = max(_SS, round(s * (0.03 if size >= 32 else 0.07)))
    for r_frac, alpha in rings:
        r = s * r_frac
        d.ellipse(
            (c - r, c - r, c + r, c + r), outline=(*BRAND_VIOLET, round(255 * alpha)), width=w
        )
    r = s * (0.13 if size >= 32 else 0.15)
    d.ellipse((c - r, c - r, c + r, c + r), fill=(*REC_DOT, 255))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def write_ico(path: Path = ICO_PATH, sizes: tuple[int, ...] = ICO_SIZES) -> Path:
    """Escribe un ``.ico`` multi-tamano (cada tamano renderizado a su resolucion, no escalado)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    big = app_icon(max(sizes))
    big.save(
        path,
        format="ICO",
        sizes=[(n, n) for n in sizes],
        append_images=[app_icon(n) for n in sizes if n != max(sizes)],
    )
    return path


if __name__ == "__main__":  # pragma: no cover
    print(write_ico())
