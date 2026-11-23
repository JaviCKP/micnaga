"""Captura de audio (F0, seccion 7 de la spec).

API publica (los imports pesados/Windows son perezosos: importar este paquete no abre
dispositivos ni carga PyAudio/COM):

* :class:`Recorder` / :class:`RecorderState` / :class:`RecorderEvent` / :class:`RecorderStatus`
  -- grabador de dos pistas (``recorder.py``).
* :func:`recover_sessions` / :func:`finalize_session` / :func:`find_sessions` -- recuperacion
  tras cierre brusco y paso a FLAC (``recovery.py``).
* :func:`align_tracks` / :func:`estimate_drift` -- alineacion por deriva (``sync.py``, la usa la
  etapa 2 del pipeline).
* ``micnaga.capture.cli:run`` -- subcomando ``micnaga record``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from micnaga.capture.recorder import (
    CaptureError,
    Recorder,
    RecorderEvent,
    RecorderState,
    RecorderStatus,
)
from micnaga.capture.recovery import finalize_session, find_sessions, recover_sessions

if TYPE_CHECKING:
    from micnaga.capture.sync import DriftEstimate, align_tracks, estimate_drift

# ``sync`` (alineacion; la usan el pipeline y ``micnaga record``) importa scipy.ndimage: ~20 MiB de
# RSS que el proceso de la bandeja no necesita para grabar. Se carga al pedir el nombre.
_LAZY_SYNC = ("DriftEstimate", "align_tracks", "estimate_drift")


def __getattr__(name: str) -> Any:
    if name in _LAZY_SYNC:
        from micnaga.capture import sync

        return getattr(sync, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CaptureError",
    "DriftEstimate",
    "Recorder",
    "RecorderEvent",
    "RecorderState",
    "RecorderStatus",
    "align_tracks",
    "estimate_drift",
    "finalize_session",
    "find_sessions",
    "recover_sessions",
]
