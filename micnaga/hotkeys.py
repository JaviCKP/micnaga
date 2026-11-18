"""Atajos globales con ``RegisterHotKey`` (spec §11): sin administrador y sin hooks de teclado.

``RegisterHotKey`` es afin al hilo: registrar, bombear mensajes (``WM_HOTKEY``) y des-registrar
ocurren en un unico hilo dedicado con su bucle de mensajes. ``start()`` espera a que el hilo
haya intentado registrar todas las combinaciones y devuelve el resultado de cada una (p. ej. si
otra aplicacion ya tiene ``ctrl+alt+m``: ``ERROR_HOTKEY_ALREADY_REGISTERED`` = 1409).

Los callbacks se ejecutan en el hilo de los atajos: deben volver rapido.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from micnaga.config import HotkeysConfig

log = logging.getLogger("micnaga.hotkeys")

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
}

_NAMED_KEYS = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "ins": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "plus": 0xBB,
    "comma": 0xBC,
    "minus": 0xBD,
    "period": 0xBE,
}


class HotkeyParseError(ValueError):
    """Cadena de atajo invalida (``"ctrl+alt+m"``)."""


@dataclass(frozen=True)
class Hotkey:
    modifiers: int  # combinacion de MOD_*
    vk: int  # codigo de tecla virtual
    text: str  # forma normalizada: "ctrl+alt+m"


def parse_hotkey(spec: str) -> Hotkey:
    """``"ctrl+alt+m"`` -> ``Hotkey``. Exige >= 1 modificador y exactamente una tecla."""
    parts = [p.strip().lower() for p in spec.split("+")]
    if not spec.strip() or any(not p for p in parts):
        raise HotkeyParseError(f"invalid hotkey {spec!r}")
    modifiers = 0
    mod_names: list[str] = []
    key_part: str | None = None
    for part in parts:
        if part in _MODIFIERS:
            bit = _MODIFIERS[part]
            if modifiers & bit:
                raise HotkeyParseError(f"duplicate modifier in {spec!r}")
            modifiers |= bit
            mod_names.append(part)
        elif key_part is None:
            key_part = part
        else:
            raise HotkeyParseError(f"more than one key in {spec!r}")
    if key_part is None:
        raise HotkeyParseError(f"no key in {spec!r}")
    if modifiers == 0:
        raise HotkeyParseError(f"hotkey needs at least one modifier: {spec!r}")
    vk = _key_to_vk(key_part, spec)
    return Hotkey(modifiers, vk, "+".join([*mod_names, key_part]))


def _key_to_vk(key: str, spec: str) -> int:
    if len(key) == 1 and "a" <= key <= "z":
        return ord(key.upper())
    if len(key) == 1 and "0" <= key <= "9":
        return ord(key)
    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x70 + int(key[1:]) - 1
    raise HotkeyParseError(f"unknown key {key!r} in {spec!r}")


@dataclass(frozen=True)
class HotkeyResult:
    name: str
    spec: str
    registered: bool
    error: str | None = None
    taken: bool = False  # otra aplicacion ya tiene la combinacion


class HotkeyManager:
    """Registra atajos globales en un hilo dedicado.

    ``bindings``: ``nombre -> (cadena de atajo, callback)``.
    """

    _ID_BASE = 0x4D00

    def __init__(self, bindings: Mapping[str, tuple[str, Callable[[], None]]]) -> None:
        self._bindings = dict(bindings)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._tid = 0
        self._results: dict[str, HotkeyResult] = {}
        self._ids: dict[int, str] = {}

    @classmethod
    def from_config(
        cls,
        config: HotkeysConfig,
        *,
        marker: Callable[[], None],
        private_pause: Callable[[], None],
    ) -> HotkeyManager:
        return cls(
            {
                "marker": (config.marker, marker),
                "private_pause": (config.private_pause, private_pause),
            }
        )

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def results(self) -> dict[str, HotkeyResult]:
        return dict(self._results)

    def start(self, timeout_s: float = 5.0) -> dict[str, HotkeyResult]:
        """Arranca el hilo y devuelve el resultado del registro de cada atajo."""
        if self.running:
            return self.results
        self._ready.clear()
        self._results = {}
        self._thread = threading.Thread(target=self._run, name="micnaga-hotkeys", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout_s):
            log.error("hotkey thread did not become ready")
        return self.results

    def stop(self, timeout_s: float = 5.0) -> None:
        """Des-registra todo y para el hilo (idempotente)."""
        thread = self._thread
        if thread is None:
            return
        if thread.is_alive():
            self._post(WM_QUIT, 0)
            thread.join(timeout_s)
        self._thread = None

    def __enter__(self) -> HotkeyManager:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ internos

    def _post(self, message: int, wparam: int) -> None:
        import win32api

        try:
            win32api.PostThreadMessage(self._tid, message, wparam, 0)
        except Exception:
            log.debug("PostThreadMessage failed", exc_info=True)

    def _simulate(self, name: str) -> None:
        """Solo pruebas: entrega un ``WM_HOTKEY`` como si se hubiera pulsado ``name``."""
        for hid, n in self._ids.items():
            if n == name:
                self._post(WM_HOTKEY, hid)

    def _run(self) -> None:
        import pywintypes
        import win32api
        import win32gui

        self._tid = win32api.GetCurrentThreadId()
        registered: list[int] = []
        try:
            win32gui.PeekMessage(0, 0, 0, PM_NOREMOVE)  # fuerza la creacion de la cola
            callbacks: dict[int, Callable[[], None]] = {}
            for index, (name, (spec, callback)) in enumerate(self._bindings.items()):
                hid = self._ID_BASE + index
                try:
                    hotkey = parse_hotkey(spec)
                except HotkeyParseError as exc:
                    self._results[name] = HotkeyResult(name, spec, False, str(exc))
                    continue
                try:
                    win32gui.RegisterHotKey(None, hid, hotkey.modifiers | MOD_NOREPEAT, hotkey.vk)
                except pywintypes.error as exc:
                    taken = exc.winerror == ERROR_HOTKEY_ALREADY_REGISTERED
                    reason = "already registered by another application" if taken else str(exc)
                    self._results[name] = HotkeyResult(name, spec, False, reason, taken)
                    log.warning("hotkey %s (%s) not registered: %s", name, spec, reason)
                    continue
                registered.append(hid)
                callbacks[hid] = callback
                self._ids[hid] = name
                self._results[name] = HotkeyResult(name, spec, True)
            self._ready.set()
            self._pump(win32gui, callbacks)
        finally:
            for hid in registered:
                try:
                    win32gui.UnregisterHotKey(None, hid)
                except Exception:
                    log.debug("UnregisterHotKey failed", exc_info=True)
            self._ids.clear()
            self._ready.set()

    def _pump(self, win32gui: Any, callbacks: Mapping[int, Callable[[], None]]) -> None:
        while True:
            ret, msg = win32gui.GetMessage(None, 0, 0)
            if ret in (0, -1):  # WM_QUIT o error
                return
            if msg[1] == WM_HOTKEY:
                callback = callbacks.get(int(msg[2]))
                if callback is not None:
                    try:
                        callback()
                    except Exception:
                        log.exception("hotkey callback failed")
