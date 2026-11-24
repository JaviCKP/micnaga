"""Iconos de bandeja generados con Pillow, .ico de la app y logica del menu."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from PIL import Image

from micnaga.ui import icons, tray
from micnaga.ui.contracts import UiCallbacks


def _px(img: Image.Image, xy: tuple[int, int]) -> tuple[int, ...]:
    return cast(tuple[int, ...], img.getpixel(xy))


def _pixels(img: Image.Image) -> list[tuple[int, ...]]:
    w, h = img.size
    return [_px(img, (x, y)) for y in range(h) for x in range(w)]


STATES = ("watching", "recording", "paused", "processing")


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_tray_icon_is_a_ring_in_the_taskbar_ink(state: str, theme: str) -> None:
    img = icons.tray_icon(state, 64, theme=theme)
    assert img.size == (64, 64) and img.mode == "RGBA"
    assert _px(img, (0, 0))[3] == 0  # sin baldosa: esquinas transparentes
    # el anillo exterior, en el eje vertical, es tinta opaca del tema de la barra
    g = icons.tray_geo(64)
    y = round(32 - g.outer)
    r, gg, b, a = _px(img, (32, y))
    assert a == 255 and (r, gg, b) == icons.INK[theme]


def test_only_recording_states_use_the_violet_accent() -> None:
    def has_violet(state: str) -> bool:
        img = icons.tray_icon(state, 32, theme="dark")
        return any(px[:3] == icons.BRAND_VIOLET for px in _pixels(img))

    assert has_violet("recording") and has_violet("provisional")
    assert not any(has_violet(s) for s in ("watching", "paused", "processing"))


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


def test_menu_has_the_spec_entries_in_order() -> None:
    spec = tray.menu_spec("watching", watching_paused=False)
    assert [e.label for e in spec if e.visible] == [
        "Grabar ahora",
        "Grabar reunión presencial",
        "Pausar vigilancia 1 h",
        "Abrir biblioteca",
        "Rellenar partes de horas",
        "Ajustes",
        "Salir",
    ]
    assert [e.key for e in spec if e.default] == ["open_library"]
    # siempre presente (el menu se arma por posicion); activo solo con Odoo y permiso de horas
    assert not spec[4].enabled
    assert tray.menu_spec("watching", watching_paused=False, timesheets=True)[4].enabled


def test_fill_timesheets_entry_opens_the_panel() -> None:
    seen: list[str] = []
    t = tray.Tray(UiCallbacks(on_open_timesheets=lambda: seen.append("horas")))
    t.dispatch("fill_timesheets")
    assert seen == ["horas"]


def test_menu_disables_record_and_pause_while_recording_and_flips_resume() -> None:
    rec = tray.menu_spec("recording", watching_paused=False)
    assert not rec[0].enabled and not rec[1].enabled and not rec[2].enabled
    paused = tray.menu_spec("watching", watching_paused=True)
    assert paused[2].key == "resume_watching" and paused[2].label == "Reanudar vigilancia"


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


def test_tray_title_reflects_state_and_shows_the_version() -> None:
    from micnaga import __version__

    t = tray.Tray(UiCallbacks())
    assert t.title() == f"Micnaga {__version__} · vigilando"
    t.set_state("paused")
    assert "en pausa" in t.title()


def test_update_entry_is_hidden_until_a_new_version_exists() -> None:
    hidden = tray.menu_spec("watching", watching_paused=False)
    entry = next(e for e in hidden if e.key == "install_update")
    assert not entry.visible
    # siempre en la misma posicion (el menu se arma por posicion), justo antes de «Salir»
    shown = tray.menu_spec("watching", watching_paused=False, update="0.2.0")
    assert [e.key for e in shown] == [e.key for e in hidden]
    entry = next(e for e in shown if e.key == "install_update")
    assert entry.visible and entry.label == "Actualizar a la versión 0.2.0"
    assert shown[-1].key == "quit" and shown[-2] is entry


def test_update_entry_starts_the_install(monkeypatch: pytest.MonkeyPatch) -> None:
    from micnaga import updates

    calls: list[bool] = []

    class Svc:
        def available(self) -> None:
            return None

        def request_install(self, *, open_ui: bool = False) -> None:
            calls.append(open_ui)

        def add_listener(self, fn: object) -> None:
            pass

    monkeypatch.setattr(updates, "_SERVICE", Svc())
    tray.Tray(UiCallbacks()).dispatch("install_update")
    assert calls == [True]
