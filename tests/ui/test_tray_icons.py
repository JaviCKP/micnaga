"""Iconos de bandeja generados con Pillow, .ico de la app y logica del menu."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from micnaga.ui import icons, tray
from micnaga.ui.contracts import UiCallbacks

STATES = ("watching", "recording", "paused", "processing")


@pytest.mark.parametrize("state", STATES)
def test_tray_icon_has_state_color_and_transparent_corners(state: str) -> None:
    img = icons.tray_icon(state, 64)
    assert img.size == (64, 64) and img.mode == "RGBA"
    assert img.getpixel((0, 0))[3] == 0  # esquina transparente: es un circulo
    # el color de estado domina cerca del borde del circulo (lejos del glifo blanco)
    r, g, b, a = img.getpixel((32, 6))
    want = icons.STATE_COLORS[state]
    assert a == 255
    assert max(abs(r - want[0]), abs(g - want[1]), abs(b - want[2])) < 40


def test_states_are_distinguishable_by_color() -> None:
    colors = {icons.STATE_COLORS[s] for s in STATES}
    assert len(colors) == 4
    assert icons.STATE_COLORS["recording"][0] > icons.STATE_COLORS["recording"][2]  # rojo
    assert icons.STATE_COLORS["processing"][2] > icons.STATE_COLORS["processing"][0]  # azul


def test_glyph_differs_per_state_not_only_color() -> None:
    a = icons.tray_icon("paused", 64).convert("L")
    b = icons.tray_icon("recording", 64).convert("L")
    assert a.tobytes() != b.tobytes()


def test_unknown_state_is_rejected() -> None:
    with pytest.raises(ValueError):
        icons.tray_icon("exploding")


def test_write_ico_is_a_valid_multi_size_icon(tmp_path: Path) -> None:
    path = icons.write_ico(tmp_path / "micnaga.ico")
    im = Image.open(path)
    assert im.format == "ICO"
    sizes = im.info["sizes"]
    assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= set(sizes)
    assert path.read_bytes()[:4] == b"\x00\x00\x01\x00"  # cabecera ICO


def test_shipped_ico_exists_and_is_valid() -> None:
    assert icons.ICO_PATH.is_file()
    assert Image.open(icons.ICO_PATH).format == "ICO"


def test_menu_has_the_five_spec_entries_in_order() -> None:
    spec = tray.menu_spec("watching", watching_paused=False)
    assert [e.label for e in spec] == [
        "Grabar ahora",
        "Pausar vigilancia 1 h",
        "Abrir biblioteca",
        "Ajustes",
        "Salir",
    ]
    assert [e.key for e in spec if e.default] == ["open_library"]


def test_menu_disables_record_and_pause_while_recording_and_flips_resume() -> None:
    rec = tray.menu_spec("recording", watching_paused=False)
    assert not rec[0].enabled and not rec[1].enabled
    paused = tray.menu_spec("watching", watching_paused=True)
    assert paused[1].key == "resume_watching" and paused[1].label == "Reanudar vigilancia"


def test_dispatch_calls_the_right_callbacks_and_survives_errors() -> None:
    seen: list[str] = []

    def boom() -> None:
        raise RuntimeError("callback roto")

    cb = UiCallbacks(
        on_record_now=lambda: seen.append("record"),
        on_pause_watching=lambda m: seen.append(f"pause{m}"),
        on_resume_watching=lambda: seen.append("resume"),
        on_open_library=lambda: seen.append("lib"),
        on_open_settings=boom,
        on_quit=lambda: seen.append("quit"),
    )
    t = tray.Tray(cb)
    for key in (
        "record_now",
        "pause_watching",
        "resume_watching",
        "open_library",
        "open_settings",
        "quit",
        "nope",
    ):
        t.dispatch(key)
    assert seen == ["record", "pause60", "resume", "lib", "quit"]


def test_tray_title_reflects_state() -> None:
    t = tray.Tray(UiCallbacks())
    assert t.title() == "Micnaga · vigilando"
    t.set_state("paused")
    assert "en pausa" in t.title()
