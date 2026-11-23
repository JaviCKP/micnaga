"""Iconos nuevos: un fotograma propio por tamano, nitidos a 16-24 px y los ficheros al dia."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from PIL import Image, ImageChops

from micnaga.ui import icons


def _px(img: Image.Image, xy: tuple[int, int]) -> tuple[int, ...]:
    return cast(tuple[int, ...], img.getpixel(xy))


def _pixels(img: Image.Image) -> list[tuple[int, ...]]:
    w, h = img.size
    return [_px(img, (x, y)) for y in range(h) for x in range(w)]


def _frame(path: Path, size: int) -> Image.Image:
    im = Image.open(path)
    im.size = (size, size)  # type: ignore[misc]
    return im.convert("RGBA")


def test_app_ico_has_every_size_hand_drawn() -> None:
    im = Image.open(icons.ICO_PATH)
    assert set(im.info["sizes"]) == {(n, n) for n in icons.ICO_SIZES}
    for n in (16, 24, 32, 48):
        got = _frame(icons.ICO_PATH, n)
        assert got.tobytes() == icons.app_icon(n).tobytes(), n  # al dia con la geometria


@pytest.mark.parametrize("state", icons.TRAY_STATES)
@pytest.mark.parametrize("theme", icons.THEMES)
def test_tray_ico_files_exist_with_small_sizes(state: str, theme: str) -> None:
    path = icons.tray_ico_path(state, theme)
    sizes = set(Image.open(path).info["sizes"])
    assert {(16, 16), (20, 20), (24, 24), (32, 32)} <= sizes
    assert _frame(path, 16).tobytes() == icons.render_state(state, 16, theme).tobytes()


@pytest.mark.parametrize("size", [16, 20, 24, 32, 48, 64, 256])
def test_rings_are_concentric_evenly_spaced_and_pixel_snapped(size: int) -> None:
    """Huecos iguales (punto -> anillo -> anillo) y, en los ejes, el trazo cae en pixeles
    enteros: la columna central del anillo interior es tinta pura, sin mezcla."""
    geo = icons.app_rings(size)
    edges = [geo.dot] + [e for r in geo.radii for e in (r - geo.stroke / 2, r + geo.stroke / 2)]
    gaps = [edges[i + 1] - edges[i] for i in range(0, len(edges) - 1, 2)]
    assert max(gaps) - min(gaps) <= (1 if size <= 24 else 0), (size, gaps)
    img = icons.app_icon(size)
    c = size // 2
    top = int(c - geo.radii[0] - geo.stroke / 2)
    for y in range(top, top + int(geo.stroke)):
        px = _px(img, (c, y))
        assert min(px) >= 240, (size, y, px)  # blanco opaco (sin gris de antialiasing)
    # concentricos: simetria perfecta en los dos ejes y en la diagonal
    for t in (
        Image.Transpose.FLIP_LEFT_RIGHT,
        Image.Transpose.FLIP_TOP_BOTTOM,
        Image.Transpose.TRANSPOSE,
    ):
        diff = cast(
            tuple[tuple[int, int], ...], ImageChops.difference(img, img.transpose(t)).getextrema()
        )
        assert max(hi for _lo, hi in diff) <= 1, (size, t)


def test_sixteen_and_twenty_px_keep_two_rings_larger_sizes_three() -> None:
    assert len(icons.app_rings(16).radii) == 2 and len(icons.app_rings(20).radii) == 2
    assert all(len(icons.app_rings(n).radii) == 3 for n in (32, 48, 256))


def test_provisional_and_recording_differ_by_glyph() -> None:
    a = icons.tray_icon("provisional", 16).convert("L").tobytes()
    b = icons.tray_icon("recording", 16).convert("L").tobytes()
    assert a != b


def test_tray_icon_carries_its_state_for_the_exact_frame_loader() -> None:
    assert icons.tray_icon("paused").info["micnaga_state"] == "paused"
    assert icons.load_tray_hicon(Image.new("RGBA", (16, 16))) is None  # sin estado: pystray
