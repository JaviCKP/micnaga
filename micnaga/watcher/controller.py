"""Interfaz que debe implementar la app (UI + captura) y despacho de acciones.

El vigilante NO importa ``capture`` ni ``ui``: emite acciones y el agente que cablee la app
implementa ``WatcherController``. Todos los metodos se llaman desde el hilo del vigilante (o desde
el hilo que invoco el evento: popup, tray, hotkey) FUERA del lock de la maquina, en orden.
Deben volver rapido (encolar trabajo en el hilo de UI / captura) y pueden llamar de vuelta a
``Watcher.popup_answer`` / ``popup_hover`` / ``record_now``... sin riesgo de interbloqueo.

Sesion en disco: la crea y es dueno de ella el controlador (``start_recording`` la abre,
``stop_and_finalize`` / ``discard_recording`` la cierran). El vigilante solo pasa ``confirmed`` y
el ``MeetingInfo`` definitivo (titulo/codigo para el nombre de carpeta y ``meet_code``); la
decision de compuertas la toma la etapa 1 del pipeline (``micnaga.gates``), no el vigilante.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Protocol

from micnaga.watcher.machine import (
    Action,
    ClosePopup,
    ConfirmRecording,
    DiscardRecording,
    HidePill,
    PauseRecording,
    PopupCloseReason,
    ResumeRecording,
    ShowPill,
    ShowPopup,
    StartRecording,
    StopAndFinalize,
)
from micnaga.watcher.model import MeetingInfo, MonitorHint

log = logging.getLogger("micnaga.watcher")


class WatcherController(Protocol):
    def start_recording(self, *, provisional: bool, meeting: MeetingInfo) -> None:
        """Abre la sesion y empieza a grabar (provisional=True: grabacion hasta que se confirme)."""
        ...

    def show_popup(
        self,
        *,
        title: str | None,
        code: str | None,
        monitor_hint: MonitorHint | None,
        timeout_s: float,
    ) -> None:
        """Muestra «¿Estas en una reu?». La cuenta atras autoritativa es la del vigilante."""
        ...

    def close_popup(self, *, reason: PopupCloseReason) -> None:
        """Cierra el popup (animacion segun el motivo; ``timeout`` lo convierte en pastilla)."""
        ...

    def confirm_recording(self) -> None:
        """La sesion pasa a confirmada (marcar en capture.json; solo exigira la compuerta 1)."""
        ...

    def discard_recording(self) -> None:
        """Parar y borrar todo lo grabado de la sesion actual; no entra en la cola."""
        ...

    def stop_and_finalize(self, *, confirmed: bool, meeting: MeetingInfo) -> None:
        """Parar, cerrar (WAV->FLAC) y encolar el job con ``confirmed``; las compuertas deciden.

        Puede llegar con la pausa privada activa: cerrar igualmente.
        """
        ...

    def show_pill(self, *, provisional: bool, paused: bool) -> None:
        """Muestra (o actualiza) la pastilla de grabacion."""
        ...

    def hide_pill(self) -> None: ...

    def pause_recording(self) -> None:
        """Pausa privada: no se escribe nada a disco; la pastilla pasa a ambar."""
        ...

    def resume_recording(self) -> None: ...


def dispatch_action(controller: WatcherController, action: Action) -> None:
    """Traduce una accion de la maquina a la llamada del controlador."""
    if isinstance(action, StartRecording):
        controller.start_recording(provisional=action.provisional, meeting=action.meeting)
    elif isinstance(action, ShowPopup):
        controller.show_popup(
            title=action.title,
            code=action.code,
            monitor_hint=action.monitor_hint,
            timeout_s=action.timeout_s,
        )
    elif isinstance(action, ClosePopup):
        controller.close_popup(reason=action.reason)
    elif isinstance(action, ConfirmRecording):
        controller.confirm_recording()
    elif isinstance(action, DiscardRecording):
        controller.discard_recording()
    elif isinstance(action, StopAndFinalize):
        controller.stop_and_finalize(confirmed=action.confirmed, meeting=action.meeting)
    elif isinstance(action, ShowPill):
        controller.show_pill(provisional=action.provisional, paused=action.paused)
    elif isinstance(action, HidePill):
        controller.hide_pill()
    elif isinstance(action, PauseRecording):
        controller.pause_recording()
    elif isinstance(action, ResumeRecording):
        controller.resume_recording()


def dispatch_actions(controller: WatcherController, actions: Iterable[Action]) -> None:
    """Despacha en orden; la excepcion de una accion se registra y no corta las demas."""
    for action in actions:
        try:
            dispatch_action(controller, action)
        except Exception:
            log.exception("watcher controller failed on %s", type(action).__name__)
