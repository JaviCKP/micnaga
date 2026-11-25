"""Senal «app usando el micro» leida de las sesiones de captura WASAPI.

El registro de privacidad de Windows (``CapabilityAccessManager``) no siempre se actualiza: en
una sesion de Escritorio remoto (micro «Audio remoto») Chrome puede estar capturando sin que su
entrada pase a ``LastUsedTimeStop == 0``. Las sesiones de audio de los dispositivos de captura si
lo reflejan: una sesion en estado ``AudioSessionStateActive`` (1) es un proceso con el micro
abierto ahora mismo. Esta fuente se combina con la del registro (``CombinedMicSource``).

COM se usa desde el hilo del vigilante (que hace ``CoInitialize``); pycaw/comtypes/psutil se
importan de forma perezosa.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable

from micnaga.watcher.model import MicApp, SignalReading
from micnaga.watcher.registry_mic import (
    OWN_EXE_NAMES,
    _norm_path,
    friendly_app_name,
    own_exe_paths,
)
from micnaga.watcher.sources import MicReading, MicSource

log = logging.getLogger(__name__)

CaptureLister = Callable[[], Iterable[tuple[int, str, str]]]
"""Devuelve ``(pid, exe_en_minusculas, ruta_del_exe)`` por cada sesion de captura ACTIVA."""


ENDPOINTS_REFRESH_S = 10.0
"""Cada cuanto se vuelven a enumerar los micros (``EnumAudioEndpoints`` + ``Activate``): era el
~70 % de los ~5 ms que costaba cada tick de 1 s. Las sesiones se leen en cada tick."""


def _session_managers() -> list[object]:
    from _ctypes import COMError

    import comtypes
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import CLSID_MMDeviceEnumerator
    from pycaw.pycaw import IAudioSessionManager2

    enumerator = comtypes.CoCreateInstance(
        CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER
    )
    devices = enumerator.EnumAudioEndpoints(1, 1)  # eCapture, DEVICE_STATE_ACTIVE
    managers: list[object] = []
    last_error: COMError | None = None
    for i in range(devices.GetCount()):
        try:
            manager = (
                devices.Item(i)
                .Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None)
                .QueryInterface(IAudioSessionManager2)
            )
        except COMError as exc:
            last_error = exc
            log.debug("could not activate WASAPI capture endpoint %d: %s", i, exc)
        else:
            managers.append(manager)
    if not managers and last_error is not None:
        raise last_error
    return managers


class CachedCaptureLister:
    """``CaptureLister`` que reutiliza los gestores de sesion de cada micro durante
    ``refresh_s`` (un micro enchufado a mitad aparece como mucho ``refresh_s`` despues en ESTA
    senal secundaria; el registro de privacidad sigue en cada tick). Si fallan todos los gestores
    se reintenta con una enumeracion nueva. Solo desde el hilo del vigilante (objetos COM STA).
    """

    def __init__(
        self, refresh_s: float = ENDPOINTS_REFRESH_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._refresh_s = refresh_s
        self._clock = clock
        self._managers: list[object] | None = None
        self._at = 0.0

    def __call__(self) -> list[tuple[int, str, str]]:
        now = self._clock()
        if self._managers is None or now - self._at >= self._refresh_s:
            self._managers, self._at = _session_managers(), now
        try:
            return _active_sessions(self._managers)
        except Exception:
            self._managers, self._at = _session_managers(), now
            return _active_sessions(self._managers)

    def close(self) -> None:
        """Suelta los objetos COM (antes del ``CoUninitialize`` del hilo del vigilante)."""
        self._managers = None


def wasapi_capture_sessions() -> list[tuple[int, str, str]]:
    """Enumeracion completa (sin cache)."""
    return _active_sessions(_session_managers())


def _active_sessions(managers: Iterable[object]) -> list[tuple[int, str, str]]:
    from _ctypes import COMError

    import psutil
    from pycaw.pycaw import IAudioSessionControl2

    out: list[tuple[int, str, str]] = []
    last_error: COMError | None = None
    enumerated = False
    for manager in managers:
        try:
            sessions = manager.GetSessionEnumerator()  # type: ignore[attr-defined]
            count = sessions.GetCount()
        except COMError as exc:
            last_error = exc
            log.debug("could not enumerate sessions for a WASAPI capture endpoint: %s", exc)
            continue
        enumerated = True
        for j in range(count):
            try:
                control = sessions.GetSession(j).QueryInterface(IAudioSessionControl2)
                if int(control.GetState()) != 1:  # AudioSessionStateActive
                    continue
                pid = int(control.GetProcessId())
            except COMError as exc:
                log.debug("could not read WASAPI capture session %d: %s", j, exc)
                continue
            if pid == 0:
                continue
            try:
                proc = psutil.Process(pid)
                name = proc.name().lower()
                path = proc.exe()
            except (psutil.Error, OSError):
                continue
            out.append((pid, name, path))
    if not enumerated and last_error is not None:
        raise last_error
    return out


class CaptureSessionMicSource:
    """Implementa ``MicSource`` con las sesiones de captura activas."""

    def __init__(
        self,
        browsers: Iterable[str],
        lister: CaptureLister | None = None,
        *,
        generic: bool = True,
        exclude: Iterable[str] = (),
        own_paths: Iterable[str] | None = None,
        own_pid: int | None = None,
    ) -> None:
        self._browsers = frozenset(b.lower() for b in browsers)
        self._lister: CaptureLister = lister if lister is not None else CachedCaptureLister()
        self._generic = generic
        self._exclude = frozenset(e.strip().lower() for e in exclude if e.strip())
        self._own_paths = frozenset(own_paths) if own_paths is not None else own_exe_paths()
        self._own_pid = os.getpid() if own_pid is None else own_pid

    def _is_excluded(self, pid: int, exe: str, path: str) -> bool:
        norm = _norm_path(path) if path else ""
        return (
            pid == self._own_pid
            or exe in OWN_EXE_NAMES
            or (bool(norm) and norm in self._own_paths)
            or exe in self._exclude
            or (bool(norm) and norm in self._exclude)
        )

    def read(self) -> MicReading:
        t0 = time.perf_counter()
        try:
            sessions = list(self._lister())
        except Exception as exc:  # sin dispositivo de captura, COM no inicializado...
            return MicReading(
                frozenset(),
                SignalReading("mic_sessions", False, error=f"{type(exc).__name__}: {exc}"),
            )
        browsers: set[str] = set()
        apps: set[MicApp] = set()
        excluded: set[str] = set()
        for pid, exe, path in sessions:
            if exe in self._browsers:
                browsers.add(exe)
            if not self._generic:
                continue
            if self._is_excluded(pid, exe, path):
                excluded.add(exe)
                continue
            apps.add(
                MicApp(
                    key=f"app:{exe}",
                    label=friendly_app_name(exe),
                    exe=exe,
                    is_browser=exe in self._browsers,
                )
            )
        detail = {
            "browsers_in_use": sorted(browsers),
            "apps_in_use": sorted(a.key for a in apps),
            "excluded_in_use": sorted(excluded),
            "active_sessions": len(sessions),
        }
        cost = (time.perf_counter() - t0) * 1000
        active = bool(browsers or apps)
        return MicReading(
            frozenset(browsers),
            SignalReading("mic_sessions", active, detail, None, cost),
            frozenset(apps),
        )

    def close(self) -> None:
        close = getattr(self._lister, "close", None)
        if callable(close):
            close()


class CombinedMicSource:
    """Une dos fuentes de micro: una app cuenta como «en uso» si cualquiera de ellas lo dice.

    Si ambas ven la misma app con la misma clave, se queda la del registro (primaria).
    """

    def __init__(self, primary: MicSource, secondary: MicSource) -> None:
        self._primary = primary
        self._secondary = secondary

    def read(self) -> MicReading:
        a = self._primary.read()
        b = self._secondary.read()
        keys = {app.key for app in a.apps}
        apps = set(a.apps) | {app for app in b.apps if app.key not in keys}
        browsers = a.browsers | b.browsers
        errors = [r.error for r in (a.reading, b.reading) if r.error]
        reading = SignalReading(
            "mic",
            bool(browsers or apps),
            {"registry": a.reading.detail, "sessions": b.reading.detail},
            "; ".join(errors) if errors and not (browsers or apps) else None,
            (a.reading.cost_ms or 0.0) + (b.reading.cost_ms or 0.0),
        )
        return MicReading(browsers, reading, frozenset(apps))

    def close(self) -> None:
        for source in (self._primary, self._secondary):
            close = getattr(source, "close", None)
            if callable(close):
                close()
