"""Vigilante de reuniones (spec §5-§6): senales, maquina de estados y fachada ``Watcher``.

Punto de entrada para la app::

    from micnaga.watcher import Watcher, WatcherController
    watcher = Watcher(config, controller)   # controller: implementa WatcherController
    watcher.start()

Los modulos de senales importan win32/comtypes/psutil de forma perezosa.
"""

from __future__ import annotations

from micnaga.watcher.controller import WatcherController, dispatch_actions
from micnaga.watcher.machine import (
    Action,
    Event,
    Phase,
    PopupCloseReason,
    WatcherState,
    WatcherStateMachine,
)
from micnaga.watcher.model import MeetingInfo, MeetTab, MonitorHint, SignalSnapshot
from micnaga.watcher.sampler import SignalSampler, build_default_sampler
from micnaga.watcher.sources import ExtensionSignalSource, ExtensionState, NullExtensionSource
from micnaga.watcher.watcher import Watcher

__all__ = [
    "Action",
    "Event",
    "ExtensionSignalSource",
    "ExtensionState",
    "MeetTab",
    "MeetingInfo",
    "MonitorHint",
    "NullExtensionSource",
    "Phase",
    "PopupCloseReason",
    "SignalSampler",
    "SignalSnapshot",
    "Watcher",
    "WatcherController",
    "WatcherState",
    "WatcherStateMachine",
    "build_default_sampler",
    "dispatch_actions",
]
