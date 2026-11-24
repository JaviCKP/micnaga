"""Regenera los iconos de Micnaga desde la geometria de ``micnaga/ui/icons.py``.

Salidas (se versionan en ``micnaga/ui/web/assets/``):

* ``micnaga.ico``: app, instalador y ventanas; 16, 20, 24, 32, 40, 48, 64, 96, 128 y 256 px,
  cada uno dibujado a su tamano (tablas ajustadas a mano hasta 64 px).
* ``tray-<estado>.ico``: bandeja (watching, recording, provisional, paused, processing);
  16-64 px. La bandeja carga el fotograma de ``SM_CXSMICON`` (16 a 100 %, 24 a 150 %...).
* ``micnaga-256.png``: la marca en PNG (README, documentacion).

Con ``--preview RUTA.png`` dibuja ademas una hoja de prueba: todos los tamanos a 1:1 y
ampliados x4 (pixel a pixel) sobre una barra de tareas clara y otra oscura.

    python scripts/make_icons.py
    python scripts/make_icons.py --preview %TEMP%\\icons_preview.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from micnaga.ui import icons  # noqa: E402

LIGHT_BAR = (243, 243, 243)
DARK_BAR = (32, 32, 32)


def write_assets(folder: Path = icons.ASSETS_DIR) -> list[Path]:
    out = [icons.write_ico(folder / "micnaga.ico")]
    out += icons.write_tray_icos(folder)
    png = folder / "micnaga-256.png"
    icons.app_icon(256).save(png, format="PNG", optimize=True)
    out.append(png)
    return out


def preview(path: Path) -> Path:
    """Hoja de prueba: la app a 1:1 (256 -> 16) y ampliada x4 (16-64), y la bandeja a 1:1 y x4,
    sobre una barra de tareas clara y otra oscura (cada una con su tinta)."""
    from PIL import Image, ImageDraw

    zoom, pad = 4, 16
    width = 1800
    band = 900
    sheet = Image.new("RGB", (width, band * 2), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    for k, (bar, theme) in enumerate(((LIGHT_BAR, "light"), (DARK_BAR, "dark"))):
        y0 = k * band
        d.rectangle((0, y0, width, y0 + band), fill=bar)
        x = pad
        for n in (256, 128, 96, 64, 48, 40, 32, 24, 20, 16):  # app 1:1
            im = icons.app_icon(n)
            sheet.paste(im, (x, y0 + pad + (256 - n)), im)
            x += n + pad
        y = y0 + 256 + 2 * pad
        x = pad
        for n in (16, 20, 24, 32, 40, 48, 64):  # app x4, pixel a pixel
            im = icons.app_icon(n)
            z = im.resize((n * zoom, n * zoom), Image.Resampling.NEAREST)
            sheet.paste(z, (x, y + 64 * zoom - n * zoom), z)
            x += n * zoom + pad
        y += 64 * zoom + 2 * pad
        x = pad
        for s in icons.TRAY_STATES:  # bandeja 1:1 (16, 20, 24) y x4 (16, 24)
            for n in (16, 20, 24):
                im = icons.render_state(s, n, theme)
                sheet.paste(im, (x, y), im)
                x += n + 6
            x += 4
            for n in (16, 24):
                im = icons.render_state(s, n, theme)
                z = im.resize((n * zoom, n * zoom), Image.Resampling.NEAREST)
                sheet.paste(z, (x, y), z)
                x += n * zoom + 8
            x += 18
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Regenera los iconos de Micnaga")
    ap.add_argument("--preview", type=Path, help="hoja de prueba PNG (claro y oscuro)")
    ap.add_argument("--no-write", action="store_true", help="solo la hoja de prueba")
    args = ap.parse_args(argv)
    if not args.no_write:
        for p in write_assets():
            print(p.relative_to(ROOT))
    if args.preview:
        print(preview(args.preview))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
