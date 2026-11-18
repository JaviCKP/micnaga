"""Refuerzo: «audio saliendo del navegador» (sesion de audio activa, pycaw).

Chrome/Edge/Brave reproducen el audio desde un proceso utilidad (el servicio de audio), que NO es
el pid de la ventana, pero comparte el nombre del exe: las sesiones se asocian por nombre de
proceso, no por pid de ventana. Solo se consulta con el micro en uso (puerta barata). La senal es
informativa: la maquina de estados no depende de ella (spec §5: «refuerzo»); se expone en el
diagnostico y en ``SignalSnapshot.audio_out_browsers``.

pycaw/comtypes y COM se usan desde el hilo del vigilante (que hace ``CoInitialize``).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from micnaga.watcher.model import SignalReading
from micnaga.watcher.sources import AudioOutReading

SessionLister = Callable[[], Iterable[tuple[str, bool]]]
"""Devuelve ``(nombre_de_exe_en_minusculas, activa)`` por cada sesion de audio de render."""


def pycaw_sessions() -> list[tuple[str, bool]]:
    import psutil
    from pycaw.pycaw import AudioUtilities

    out: list[tuple[str, bool]] = []
    for session in AudioUtilities.GetAllSessions():
        pid = int(session.ProcessId)
        if pid == 0:
            continue
        try:
            name = psutil.Process(pid).name().lower()
        except (psutil.Error, OSError):
            continue
        out.append((name, int(session.State) == 1))  # AudioSessionState.Active == 1
    return out


class PycawAudioOutSource:
    """Implementa ``AudioOutSource``."""

    def __init__(self, browsers: Iterable[str], sessions: SessionLister = pycaw_sessions) -> None:
        self._browsers = frozenset(b.lower() for b in browsers)
        self._sessions = sessions

    def read(self, mic_browsers: frozenset[str]) -> AudioOutReading:
        if not mic_browsers:
            return AudioOutReading(
                frozenset(), SignalReading("audio_out", False, {"skipped": "mic off"})
            )
        t0 = time.perf_counter()
        try:
            active = frozenset(
                name for name, is_active in self._sessions() if is_active and name in self._browsers
            )
            error = None
        except Exception as exc:  # sin dispositivo de salida, COM no inicializado...
            active = frozenset()
            error = f"{type(exc).__name__}: {exc}"
        cost = (time.perf_counter() - t0) * 1000
        return AudioOutReading(
            active,
            SignalReading("audio_out", bool(active), {"browsers": sorted(active)}, error, cost),
        )
