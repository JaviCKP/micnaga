"""Atajos globales: parseo (unitario) + registro real con una combinacion obscura."""

from __future__ import annotations

import threading
import time

import pytest

from micnaga.config import HotkeysConfig
from micnaga.hotkeys import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    Hotkey,
    HotkeyManager,
    HotkeyParseError,
    HotkeyResult,
    parse_hotkey,
)

# Combinacion improbable (F23 no existe en teclados): si estuviera tomada, el test se salta.
OBSCURE = "ctrl+alt+shift+f23"


@pytest.mark.parametrize(
    ("spec", "mods", "vk"),
    [
        ("ctrl+alt+m", MOD_CONTROL | MOD_ALT, ord("M")),
        ("ctrl+alt+p", MOD_CONTROL | MOD_ALT, ord("P")),
        ("Ctrl+Shift+5", MOD_CONTROL | MOD_SHIFT, ord("5")),
        (" win + alt + F12 ", MOD_WIN | MOD_ALT, 0x7B),
        ("control+space", MOD_CONTROL, 0x20),
        ("alt+esc", MOD_ALT, 0x1B),
        ("ctrl+alt+shift+f23", MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x86),
        ("super+left", MOD_WIN, 0x25),
    ],
)
def test_parse_valid(spec: str, mods: int, vk: int) -> None:
    hk = parse_hotkey(spec)
    assert isinstance(hk, Hotkey) and hk.modifiers == mods and hk.vk == vk


def test_parse_normalises_text() -> None:
    assert parse_hotkey("Ctrl + Alt + M").text == "ctrl+alt+m"


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "m",  # sin modificador
        "f5",
        "ctrl+alt",  # sin tecla
        "ctrl+alt+m+p",  # dos teclas
        "ctrl+ctrl+m",  # modificador repetido
        "ctrl++m",
        "ctrl+alt+nonsense",
        "ctrl+f25",
        "ctrl+",
    ],
)
def test_parse_invalid(spec: str) -> None:
    with pytest.raises(HotkeyParseError):
        parse_hotkey(spec)


def test_default_config_strings_parse() -> None:
    cfg = HotkeysConfig()
    assert parse_hotkey(cfg.marker).vk == ord("M")
    assert parse_hotkey(cfg.private_pause).vk == ord("P")


def test_invalid_spec_is_reported_not_raised() -> None:
    m = HotkeyManager({"x": ("nonsense", lambda: None)})
    try:
        res = m.start()
    finally:
        m.stop()
    assert not res["x"].registered and res["x"].error


def _skip_if_taken(res: dict[str, HotkeyResult]) -> None:
    if not res["a"].registered:
        pytest.skip(f"{OBSCURE} is taken by another application on this machine")


def test_real_register_callback_and_clean_unregister() -> None:
    hits: list[str] = []
    done = threading.Event()

    def cb() -> None:
        hits.append("a")
        done.set()

    m = HotkeyManager({"a": (OBSCURE, cb)})
    try:
        res = m.start()
        _skip_if_taken(res)
        assert m.running
        m._simulate("a")  # WM_HOTKEY entregado al hilo, como si se pulsara
        assert done.wait(2.0) and hits == ["a"]
    finally:
        m.stop()
    assert not m.running
    assert not [t for t in threading.enumerate() if t.name == "micnaga-hotkeys"]
    # Des-registrado de verdad: otra instancia puede registrar la misma combinacion.
    again = HotkeyManager({"a": (OBSCURE, lambda: None)})
    try:
        assert again.start()["a"].registered
    finally:
        again.stop()


def test_real_conflict_is_reported_as_taken() -> None:
    first = HotkeyManager({"a": (OBSCURE, lambda: None)})
    second = HotkeyManager({"a": (OBSCURE, lambda: None)})
    try:
        res = first.start()
        _skip_if_taken(res)
        clash = second.start()["a"]
        assert not clash.registered and clash.taken
        assert "already registered" in (clash.error or "")
    finally:
        second.stop()
        first.stop()


def test_callback_exception_does_not_stop_the_loop() -> None:
    calls: list[int] = []
    done = threading.Event()

    def cb() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        done.set()

    m = HotkeyManager({"a": (OBSCURE, cb)})
    try:
        _skip_if_taken(m.start())
        m._simulate("a")
        time.sleep(0.2)
        m._simulate("a")
        assert done.wait(2.0)
    finally:
        m.stop()


def test_stop_without_start_and_double_stop_are_safe() -> None:
    m = HotkeyManager({})
    m.stop()
    m.start()
    m.stop()
    m.stop()
    assert not m.running


def test_from_config_wires_both_hotkeys() -> None:
    m = HotkeyManager.from_config(HotkeysConfig(), marker=lambda: None, private_pause=lambda: None)
    assert set(m._bindings) == {"marker", "private_pause"}
