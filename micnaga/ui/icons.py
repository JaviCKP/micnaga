"""Iconos generados en tiempo de ejecucion con Pillow (bandeja) y el ``.ico`` de la aplicacion.

Color por estado (spec §10) *y* un glifo distinto, para que no dependa solo del color:

=========== ========= ===============================
estado      color     glifo
=========== ========= ===============================
watching    gris      microfono
recording   rojo      punto central
paused      ambar     dos barras de pausa
processing  azul      ondas (tres barras)
=========== ========= ===============================

``python -m micnaga.ui.icons`` regenera ``web/assets/micnaga.ico`` (icono del instalador).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from micnaga.ui.contracts import TrayState

if TYPE_CHECKING:
    from PIL import Image

STATE_COLORS: dict[str, tuple[int, int, int]] = {
    "watching": (122, 122, 134),
    "recording": (212, 54, 60),
    "paused": (232, 148, 12),
    "processing": (46, 116, 224),
}
APP_COLOR = STATE_COLORS["recording"]
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
ASSETS_DIR = Path(__file__).resolve().parent / "web" / "assets"
ICO_PATH = ASSETS_DIR / "micnaga.ico"
_SS = 4  # supersampling


def _draw_glyph(d: object, glyph: str, s: int) -> None:
    from PIL import ImageDraw

    assert isinstance(d, ImageDraw.ImageDraw)
    white = (255, 255, 255, 255)
    c = s / 2
    if glyph == "mic":
        w, h = s * 0.20, s * 0.34
        d.rounded_rectangle(
            (c - w / 2, s * 0.22, c + w / 2, s * 0.22 + h), radius=w / 2, fill=white
        )
        t = max(2, round(s * 0.05))
        d.arc(
            (c - s * 0.20, s * 0.34, c + s * 0.20, s * 0.34 + s * 0.30), 0, 180, fill=white, width=t
        )
        d.line((c, s * 0.64, c, s * 0.76), fill=white, width=t)
        d.line((c - s * 0.10, s * 0.77, c + s * 0.10, s * 0.77), fill=white, width=t)
    elif glyph == "dot":
        r = s * 0.17
        d.ellipse((c - r, c - r, c + r, c + r), fill=white)
    elif glyph == "bars":
        w, h = s * 0.085, s * 0.34
        for dx in (-s * 0.105, s * 0.105):
            d.rounded_rectangle(
                (c + dx - w / 2, c - h / 2, c + dx + w / 2, c + h / 2), radius=w / 2, fill=white
            )
    elif glyph == "wave":
        w = s * 0.075
        for dx, hh in ((-s * 0.17, 0.16), (0, 0.34), (s * 0.17, 0.22)):
            h = s * hh
            d.rounded_rectangle(
                (c + dx - w / 2, c - h / 2, c + dx + w / 2, c + h / 2), radius=w / 2, fill=white
            )


_GLYPH = {"watching": "mic", "recording": "dot", "paused": "bars", "processing": "wave"}


def render(color: tuple[int, int, int], glyph: str, size: int) -> Image.Image:
    """Circulo de ``color`` con un glifo blanco, antialiasing por supermuestreo."""
    from PIL import Image, ImageDraw

    s = size * _SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = s * 0.04
    d.ellipse((pad, pad, s - pad, s - pad), fill=(*color, 255))
    # brillo superior muy sutil (una sola capa)
    hi = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(hi).ellipse((pad, pad, s - pad, s * 0.62), fill=(255, 255, 255, 22))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).ellipse((pad, pad, s - pad, s - pad), fill=255)
    img = Image.composite(Image.alpha_composite(img, hi), img, mask)
    _draw_glyph(ImageDraw.Draw(img), glyph, s)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def tray_icon(state: TrayState | str, size: int = 64) -> Image.Image:
    """Icono de bandeja para ``state`` (watching | recording | paused | processing)."""
    if state not in STATE_COLORS:
        raise ValueError(f"unknown tray state: {state!r}")
    return render(STATE_COLORS[state], _GLYPH[state], size)


def app_icon(size: int = 256) -> Image.Image:
    """Icono de la aplicacion (marca: coral con microfono)."""
    return render(APP_COLOR, "mic", size)


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
