"""Interfaz que debe implementar la app (UI + captura) y despacho de acciones.

El vigilante NO importa ``capture`` ni ``ui``: emite acciones y el agente que cablee la app
implementa ``WatcherController``. Todos los metodos se llaman desde el hilo del vigilante (o desde
el hilo que invoco el evento: popup, tray, hotkey) FUERA del lock de la maquina, en orden.
Deben volver rapido (encolar trabajo en el hilo de UI / captura) y pueden llamar de vuelta a
``Watcher.popup_answer`` / ``popup_hover`` / ``record_now``... sin riesgo de interbloqueo.

Sesion en disco: la crea y es dueno de ella el controlador (``start_recording`` la abre,
``stop_and_finalize`` / ``hold_recording`` la cierran). El vigilante solo pasa ``confirmed`` y
el ``MeetingInfo`` definitivo (titulo/codigo para el nombre de carpeta y ``meet_code``); la
decision de compuertas la toma la etapa 1 del pipeline (``micnaga.gates``), no el vigilante.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Protocol

from micnaga.watcher.machine import (
    Action,
    BeginDiscard,
    CancelDiscard,
    ClearEnd,
    CloseEndPopup,
    ClosePopup,
    ConfirmRecording,
    EndCloseReason,
    HidePill,
    HoldReason,
    HoldRecording,
    MarkEnd,
    PauseRecording,
    PopupCloseReason,
    ResumeRecording,
    ShowEndPopup,
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

    def begin_discard(self, *, undo_s: float) -> None:
        """«Ahora no»/«Descartar»: marcar la sesion como descartada en disco (sincrono, antes de
        volver), pausar la captura, ocultar la pastilla y ofrecer «Deshacer» ``undo_s``."""
        ...

    def cancel_discard(self) -> None:
        """«Deshacer»: quitar la marca y reanudar la captura."""
        ...

    def hold_recording(self, *, reason: HoldReason, trim: bool) -> None:
        """Parar y apartar en «Descartadas» (no se transcribe; recuperable N dias)."""
        ...

    def stop_and_finalize(self, *, confirmed: bool, meeting: MeetingInfo, trim: bool) -> None:
        """Parar, cerrar (WAV->FLAC, recortada si ``trim``) y encolar el job.

        Puede llegar con la pausa privada activa: cerrar igualmente.
        """
        ...

    def mark_end(self) -> None:
        """El micro se solto: anotar ya el punto de recorte (tiempo de grabacion)."""
        ...

    def clear_end(self) -> None:
        """Olvidar el punto de recorte (el micro volvio o «Seguir grabando»)."""
        ...

    def show_end_popup(
        self,
        *,
        confirmed: bool,
        title: str | None,
        monitor_hint: MonitorHint | None,
        timeout_s: float,
    ) -> None:
        """Popup de fin: «Reunión terminada» (confirmada) o «¿La guardo?» (sin confirmar)."""
        ...

    def close_end_popup(self, *, reason: EndCloseReason) -> None: ...

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
    elif isinstance(action, BeginDiscard):
        controller.begin_discard(undo_s=action.undo_s)
    elif isinstance(action, CancelDiscard):
        controller.cancel_discard()
    elif isinstance(action, HoldRecording):
        controller.hold_recording(reason=action.reason, trim=action.trim)
    elif isinstance(action, StopAndFinalize):
        controller.stop_and_finalize(
            confirmed=action.confirmed, meeting=action.meeting, trim=action.trim
        )
    elif isinstance(action, MarkEnd):
        controller.mark_end()
    elif isinstance(action, ClearEnd):
        controller.clear_end()
    elif isinstance(action, ShowEndPopup):
        controller.show_end_popup(
            confirmed=action.confirmed,
            title=action.title,
            monitor_hint=action.monitor_hint,
            timeout_s=action.timeout_s,
        )
    elif isinstance(action, CloseEndPopup):
        controller.close_end_popup(reason=action.reason)
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
