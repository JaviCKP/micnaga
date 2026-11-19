"""Notificacion «Lista» (spec §10): «Revision semanal · 47 min · 3 hablantes» + boton «Abrir».

``windows-toasts`` se importa de forma perezosa y el *toaster* es inyectable (tests/demo).

Activacion: ``InteractableWindowsToaster`` sin AUMID propio usa el de ``cmd.exe`` (asi los botones
funcionan sin registrar nada, pero Windows rotula el origen con el texto de atribucion). Para que
el toast salga firmado como «Micnaga», el instalador debe crear un acceso directo en el menu
Inicio con ``System.AppUserModel.ID`` y pasar ese AUMID aqui (``aumid=``).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any, Protocol

from micnaga.ui import i18n

log = logging.getLogger(__name__)

ARG_PREFIX = "open:"

# AppUserModelID de Micnaga. DEBE coincidir con el del acceso directo del menu Inicio que crea el
# instalador (packaging/micnaga.iss, ``AppUserModelID``): Windows solo atribuye y activa los toasts
# de una app sin paquete si existe ese acceso directo.
AUMID = "es.micnaga.Micnaga"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def default_aumid() -> str | None:
    """``AUMID`` solo en la app instalada (congelada): en desarrollo no hay acceso directo y un
    AUMID sin registrar haria que los toasts no salieran; ahi se usa el de ``cmd.exe``."""
    return AUMID if is_frozen() else None


def set_process_aumid(aumid: str | None = None) -> bool:
    """``SetCurrentProcessExplicitAppUserModelID`` (solo si se pasa un AUMID o la app esta
    congelada). Debe llamarse antes de crear ventanas. ``True`` si se aplico."""
    value = aumid or default_aumid()
    if not value:
        return False
    try:
        import ctypes

        result = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p(value)
        )
    except (AttributeError, OSError):
        log.exception("could not set the process AppUserModelID")
        return False
    return int(result) == 0


class ToasterLike(Protocol):
    def show_toast(self, toast: Any) -> None: ...


def format_ready_text(
    title: str, duration_s: float, speakers: int, language: str = "es", proposals: int = 0
) -> str:
    """«Revision semanal · 47 min · 3 hablantes» (+ «· 5 propuestas» con la Secretaria)."""
    minutes = max(1, round(duration_s / 60))
    key = "toast.speakers_one" if speakers == 1 else "toast.speakers_other"
    parts = [title, i18n.t("toast.minutes", language, n=minutes), i18n.t(key, language, n=speakers)]
    if proposals > 0:
        pkey = "toast.proposals_one" if proposals == 1 else "toast.proposals_other"
        parts.append(i18n.t(pkey, language, n=proposals))
    return " · ".join(parts)


def parse_arguments(arguments: str) -> str | None:
    """Carpeta contenida en los argumentos de un toast (``open:<carpeta>``) o ``None``."""
    if arguments.startswith(ARG_PREFIX):
        return arguments[len(ARG_PREFIX) :] or None
    return None


class ToastNotifier:
    """Muestra el toast «Lista». ``on_open(folder)`` se llama al pulsar «Abrir» (o el cuerpo)."""

    def __init__(
        self,
        on_open: Callable[[str], None],
        *,
        language: str = "es",
        aumid: str | None = None,
        toaster: ToasterLike | None = None,
    ) -> None:
        self._on_open = on_open
        self._language = language
        self._aumid = aumid
        self._toaster = toaster

    def _get_toaster(self) -> ToasterLike:
        if self._toaster is None:
            from windows_toasts import InteractableWindowsToaster

            self._toaster = InteractableWindowsToaster("Micnaga", self._aumid)
        return self._toaster

    def show_ready(
        self, folder: str, title: str, duration_s: float, speakers: int, proposals: int = 0
    ) -> None:
        from windows_toasts import Toast, ToastButton

        toast = Toast(
            text_fields=[
                i18n.t("toast.ready", self._language),
                format_ready_text(title, duration_s, speakers, self._language, proposals),
            ],
            launch_action=ARG_PREFIX + folder,
            on_activated=self._activated,
        )
        toast.AddAction(ToastButton(i18n.t("toast.open", self._language), ARG_PREFIX + folder))
        self._get_toaster().show_toast(toast)

    def _activated(self, args: Any) -> None:
        folder = parse_arguments(str(getattr(args, "arguments", "") or ""))
        if folder is None:
            return
        try:
            self._on_open(folder)
        except Exception:
            log.exception("toast open callback failed")
