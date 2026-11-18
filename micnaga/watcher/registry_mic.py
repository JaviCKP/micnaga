r"""Senal «alguna app usa el micro» a partir del registro de privacidad de Windows (SOLO LECTURA).

Layout verificado en este PC (Windows 11)::

    HKCU\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\
        NonPackaged\<ruta del exe con '#' en lugar de '\'>     apps de escritorio
            LastUsedTimeStart  REG_QWORD  (FILETIME; 0 = nunca)
            LastUsedTimeStop   REG_QWORD  (FILETIME; 0 = en uso ahora)
        <PackageFamilyName>                                     apps de la Store / empaquetadas
            Value (REG_SZ "Allow"/"Prompt"), LastSetTime, LastUsedTimeStart, LastUsedTimeStop, ...

``Start > 0 and Stop == 0`` significa «en uso». Ojo: un proceso que muere sin cerrar el micro
(cuelgue, kill) deja ``Stop == 0`` para siempre (en este PC hay una entrada de Discord asi). Por
eso una entrada «en uso» solo cuenta si hay un proceso vivo que ya existia cuando empezo el uso
(``create_time <= LastUsedTimeStart``): un proceso lanzado DESPUES no puede ser quien tiene el
micro, de modo que una entrada obsoleta de una app que se reinicio tampoco cuenta.

Navegadores (``detection.browsers``) → ``browsers`` (ruta rapida de Meet). Todas las apps no
excluidas → ``apps`` (ruta generica, ``detection.generic_apps``). Se excluyen siempre el propio
Micnaga (el interprete de Python que lo ejecuta y ``micnaga.exe``): su grabacion mantendria la
senal.

Coste: ``QueryInfoKey`` de dos claves por tick para detectar altas; las listas de subclaves se
refrescan al cambiar su numero o cada ``refresh_s``; la comprobacion de procesos (la parte cara)
se cachea y solo ocurre para entradas «en uso».
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from micnaga.watcher.model import MicApp, SignalReading
from micnaga.watcher.sources import MicReading

MIC_CONSENT_SUBKEY = (
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager"
    r"\ConsentStore\microphone"
)
NON_PACKAGED = "NonPackaged"
FILETIME_EPOCH_OFFSET_S = 11_644_473_600  # 1601-01-01 -> 1970-01-01
START_TOLERANCE_S = 10.0
OWN_EXE_NAMES = frozenset({"micnaga.exe", "micnaga-cli.exe", "micnaga-host.exe"})

_FRIENDLY = (  # (subcadena en minusculas, nombre amigable); el primero que case gana
    ("teams", "Teams"),
    ("whatsapp", "WhatsApp"),
    ("zoom", "Zoom"),
    ("discord", "Discord"),
    ("slack", "Slack"),
    ("skype", "Skype"),
    ("webex", "Webex"),
    ("chrome", "navegador"),
    ("msedge", "navegador"),
    ("brave", "navegador"),
    ("firefox", "navegador"),
)


def exe_basename(subkey: str) -> str:
    """``C:#Program Files#Google#Chrome#Application#chrome.exe`` -> ``chrome.exe``."""
    return subkey.rsplit("#", 1)[-1].strip().lower()


def subkey_to_path(subkey: str) -> str:
    return subkey.replace("#", "\\")


def friendly_app_name(exe_or_package: str) -> str:
    """Nombre para el popup/carpeta: tabla pequena; si no, el nombre del exe / del paquete."""
    stem = exe_or_package.split("_", 1)[0] if "_" in exe_or_package else exe_or_package
    low = stem.lower()
    for needle, name in _FRIENDLY:
        if needle in low:
            return name
    return exe_or_package.split("_", 1)[0] if "_" in exe_or_package else exe_or_package


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def own_exe_paths() -> frozenset[str]:
    """Rutas (normalizadas) de los ejecutables que pueden ser Micnaga en esta maquina.

    El registro apunta al exe que abre el micro: con un venv puede ser el lanzador del venv o
    el interprete base, asi que se incluyen ambos y las variantes ``python.exe``/``pythonw.exe``.
    """
    candidates: list[str] = [sys.executable, getattr(sys, "_base_executable", "") or ""]
    candidates += [os.path.join(sys.base_prefix, n) for n in ("python.exe", "pythonw.exe")]
    paths: set[str] = set()
    for c in candidates:
        if not c:
            continue
        folder, name = os.path.split(c)
        if name.lower() in ("python.exe", "pythonw.exe"):
            paths.add(_norm_path(os.path.join(folder, "python.exe")))
            paths.add(_norm_path(os.path.join(folder, "pythonw.exe")))
        else:
            paths.add(_norm_path(c))
    return frozenset(paths)


class MicRegistryReader(Protocol):
    """Acceso de solo lectura a ``ConsentStore\\microphone`` (inyectable)."""

    def subkey_count(self) -> int:
        """Numero de subclaves de ``NonPackaged``."""
        ...

    def subkey_names(self) -> list[str]: ...

    def read_times(self, subkey: str) -> tuple[int, int] | None:
        """``(LastUsedTimeStart, LastUsedTimeStop)`` o ``None`` si falta alguno."""
        ...

    def package_count(self) -> int:
        """Numero de subclaves de ``microphone`` (incluye ``NonPackaged``; sirve para altas)."""
        ...

    def package_names(self) -> list[str]:
        """``PackageFamilyName`` de las apps empaquetadas (sin ``NonPackaged``)."""
        ...

    def read_package_times(self, name: str) -> tuple[int, int] | None: ...


class WinregMicReader:
    """Lector real con ``winreg`` (``KEY_READ``: jamas se escribe en el registro)."""

    def __init__(self) -> None:
        import winreg

        self._winreg = winreg

    def _open(self, sub: str) -> Any:
        w = self._winreg
        try:
            return w.OpenKey(w.HKEY_CURRENT_USER, sub, 0, w.KEY_READ)
        except OSError:
            return None

    def _count(self, sub: str) -> int:
        key = self._open(sub)
        if key is None:
            return 0
        with key as k:
            return int(self._winreg.QueryInfoKey(k)[0])

    def _names(self, sub: str) -> list[str]:
        key = self._open(sub)
        if key is None:
            return []
        names: list[str] = []
        with key as k:
            i = 0
            while True:
                try:
                    names.append(self._winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
        return names

    def _times(self, sub: str) -> tuple[int, int] | None:
        key = self._open(sub)
        if key is None:
            return None
        with key as k:
            try:
                start = int(self._winreg.QueryValueEx(k, "LastUsedTimeStart")[0])
                stop = int(self._winreg.QueryValueEx(k, "LastUsedTimeStop")[0])
            except (OSError, TypeError, ValueError):
                return None
        return start, stop

    def subkey_count(self) -> int:
        return self._count(MIC_CONSENT_SUBKEY + "\\" + NON_PACKAGED)

    def subkey_names(self) -> list[str]:
        return self._names(MIC_CONSENT_SUBKEY + "\\" + NON_PACKAGED)

    def read_times(self, subkey: str) -> tuple[int, int] | None:
        return self._times(MIC_CONSENT_SUBKEY + "\\" + NON_PACKAGED + "\\" + subkey)

    def package_count(self) -> int:
        return self._count(MIC_CONSENT_SUBKEY)

    def package_names(self) -> list[str]:
        return [n for n in self._names(MIC_CONSENT_SUBKEY) if n.lower() != NON_PACKAGED.lower()]

    def read_package_times(self, name: str) -> tuple[int, int] | None:
        return self._times(MIC_CONSENT_SUBKEY + "\\" + name)


def psutil_exe_running(exe_path: str, started_before: float) -> bool:
    """``True`` si hay un proceso vivo de ese exe creado antes de ``started_before`` (unix s).

    Se compara el nombre y, cuando psutil deja leerla, la ruta completa (distingue versiones).
    """
    import psutil

    name = os.path.basename(exe_path).lower()
    want = _norm_path(exe_path)
    for p in psutil.process_iter(["name", "exe", "create_time"]):
        info = p.info
        if (info["name"] or "").lower() != name:
            continue
        if info["exe"] and _norm_path(info["exe"]) != want:
            continue
        if (info["create_time"] or 0.0) <= started_before:
            return True
    return False


def psutil_package_running(family: str, started_before: float) -> bool:
    """Como ``psutil_exe_running`` para una app empaquetada (el nombre del paquete en la ruta)."""
    import psutil

    stem = family.split("_", 1)[0].lower()
    for p in psutil.process_iter(["name", "exe", "create_time"]):
        info = p.info
        hay = f"{info['exe'] or ''}|{info['name'] or ''}".lower()
        if stem in hay and (info["create_time"] or 0.0) <= started_before:
            return True
    return False


def filetime_to_unix(filetime: int) -> float:
    return filetime / 10_000_000 - FILETIME_EPOCH_OFFSET_S


@dataclass(frozen=True)
class MicEntry:
    subkey: str  # subclave de NonPackaged, o PackageFamilyName
    exe: str  # basename (apps no empaquetadas); "" en las empaquetadas
    start: int
    stop: int
    in_use_raw: bool  # Start > 0 y Stop == 0
    process_alive: bool | None  # None = no comprobado (no estaba en uso)
    package: str | None = None

    @property
    def ident(self) -> str:
        return self.package or self.exe


class RegistryMicSource:
    """Implementa ``MicSource`` sobre un ``MicRegistryReader``."""

    def __init__(
        self,
        browsers: Iterable[str],
        reader: MicRegistryReader | None = None,
        process_running: Callable[[str, float], bool] = psutil_exe_running,
        *,
        generic: bool = True,
        exclude: Iterable[str] = (),
        own_paths: Iterable[str] | None = None,
        package_running: Callable[[str, float], bool] = psutil_package_running,
        clock: Callable[[], float] = time.monotonic,
        refresh_s: float = 60.0,
        alive_cache_s: float = 5.0,
        stale_cache_s: float = 60.0,
    ) -> None:
        self._browsers = frozenset(b.lower() for b in browsers)
        self._reader = reader if reader is not None else WinregMicReader()
        self._process_running = process_running
        self._package_running = package_running
        self._generic = generic
        self._exclude = frozenset(e.strip().lower() for e in exclude if e.strip())
        self._own_paths = frozenset(own_paths) if own_paths is not None else own_exe_paths()
        self._clock = clock
        self._refresh_s = refresh_s
        self._alive_cache_s = alive_cache_s
        self._stale_cache_s = stale_cache_s
        self._counts = (-1, -1)
        self._enumerated_at = float("-inf")
        self._matched: dict[str, str] = {}  # subclave NonPackaged -> exe
        self._packages: list[str] = []
        # (clase, ident) -> (start, instante, vivo)
        self._alive_cache: dict[tuple[str, str], tuple[int, float, bool]] = {}

    # ------------------------------------------------------------------ enumeracion

    def _refresh_matches(self, now: float) -> None:
        counts = (
            self._reader.subkey_count(),
            self._reader.package_count() if self._generic else 0,
        )
        if counts == self._counts and now - self._enumerated_at < self._refresh_s:
            return
        self._counts = counts
        self._enumerated_at = now
        self._matched = {
            name: exe_basename(name)
            for name in self._reader.subkey_names()
            if self._generic or exe_basename(name) in self._browsers
        }
        self._packages = self._reader.package_names() if self._generic else []

    def _alive(self, kind: str, ident: str, path: str, start: int, now: float) -> bool:
        cached = self._alive_cache.get((kind, ident))
        if cached is not None and cached[0] == start:
            ttl = self._alive_cache_s if cached[2] else self._stale_cache_s
            if now - cached[1] < ttl:
                return cached[2]
        started_before = filetime_to_unix(start) + START_TOLERANCE_S
        check = self._package_running if kind == "pkg" else self._process_running
        alive = check(path, started_before)
        self._alive_cache[(kind, ident)] = (start, now, alive)
        return alive

    def entries(self) -> list[MicEntry]:
        now = self._clock()
        self._refresh_matches(now)
        out: list[MicEntry] = []
        for subkey, exe in self._matched.items():
            times = self._reader.read_times(subkey)
            if times is None:
                continue
            start, stop = times
            raw = start > 0 and stop == 0
            alive = self._alive("exe", subkey, subkey_to_path(subkey), start, now) if raw else None
            out.append(MicEntry(subkey, exe, start, stop, raw, alive))
        for name in self._packages:
            times = self._reader.read_package_times(name)
            if times is None:
                continue
            start, stop = times
            raw = start > 0 and stop == 0
            alive = self._alive("pkg", name, name, start, now) if raw else None
            out.append(MicEntry(name, "", start, stop, raw, alive, package=name))
        return out

    # ------------------------------------------------------------------ exclusiones

    def _is_excluded(self, e: MicEntry) -> bool:
        if e.package:
            fam = e.package.lower()
            return fam in self._exclude or fam.split("_", 1)[0] in self._exclude
        path = _norm_path(subkey_to_path(e.subkey))
        return (
            e.exe in OWN_EXE_NAMES
            or path in self._own_paths
            or e.exe in self._exclude
            or path in self._exclude
        )

    def _to_app(self, e: MicEntry) -> MicApp:
        ident = e.ident
        return MicApp(
            key=f"app:{ident.lower()}",
            label=friendly_app_name(ident),
            exe=e.exe or None,
            package=e.package,
            is_browser=e.exe in self._browsers,
        )

    # ------------------------------------------------------------------ lectura

    def read(self) -> MicReading:
        t0 = time.perf_counter()
        try:
            entries = self.entries()
        except Exception as exc:  # el registro no debe tumbar el hilo del vigilante
            return MicReading(
                frozenset(),
                SignalReading("mic", False, error=f"{type(exc).__name__}: {exc}"),
            )
        active = [e for e in entries if e.in_use_raw and e.process_alive]
        browsers = frozenset(e.exe for e in active if e.exe in self._browsers)
        apps = frozenset(
            self._to_app(e) for e in active if self._generic and not self._is_excluded(e)
        )
        stale = sorted({e.ident for e in entries if e.in_use_raw and not e.process_alive})
        detail = {
            "browsers_in_use": sorted(browsers),
            "apps_in_use": sorted(a.key for a in apps),
            "excluded_in_use": sorted(
                e.ident for e in active if self._generic and self._is_excluded(e)
            ),
            "stale_entries_ignored": stale,
            "matched_subkeys": len(entries),
        }
        cost = (time.perf_counter() - t0) * 1000
        return MicReading(browsers, SignalReading("mic", bool(active), detail, None, cost), apps)
