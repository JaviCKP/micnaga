"""Icono de bandeja (pystray): color por estado + menu (spec §10).

pystray se importa de forma perezosa. En Windows ``Icon.run_detached()`` corre su bucle en un hilo
propio, asi que convive con pywebview (que ocupa el hilo principal). El menu y los textos salen de
``i18n``. ``Tray`` es testeable sin pystray: ``menu_spec`` devuelve la descripcion pura del menu.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from micnaga import __version__
from micnaga.ui import i18n
from micnaga.ui.contracts import ACTION_RECORD_IN_PERSON, TrayState, UiCallbacks, app_action
from micnaga.ui.icons import make_pystray_icon, tray_icon

log = logging.getLogger(__name__)

PAUSE_MINUTES = 60


@dataclass(frozen=True)
class MenuEntry:
    key: str
    label: str
    enabled: bool = True
    default: bool = False
    separator_before: bool = False
    visible: bool = True


STATE_LABEL_KEYS: dict[str, str] = {
    "watching": "tray.state.watching",
    "recording": "tray.state.recording",
    "paused": "tray.state.paused",
    "processing": "tray.state.processing",
}


def menu_spec(
    state: TrayState,
    *,
    watching_paused: bool,
    language: str = "es",
    timesheets: bool = False,
    in_person: bool = True,
    update: str = "",
) -> list[MenuEntry]:
    """Entradas del menu en orden. «Grabar ahora» y «Grabar reunión presencial» se desactivan
    mientras se graba (la segunda tambien si no hay app detras, ``in_person``).

    «Rellenar partes de horas» esta siempre (el menu se construye por posicion) y solo se activa
    si la Secretaria tiene Odoo con permiso para registrar horas (``timesheets``).

    «Actualizar a la versión X» tambien esta siempre, oculta salvo que haya una version nueva
    (``update``, de ``micnaga.updates``)."""
    recording = state in ("recording", "paused")
    return [
        MenuEntry("record_now", i18n.t("tray.record_now", language), enabled=not recording),
        MenuEntry(
            "record_in_person",
            i18n.t("tray.record_in_person", language),
            enabled=in_person and not recording,
        ),
        MenuEntry(
            "resume_watching" if watching_paused else "pause_watching",
            i18n.t("tray.resume_watching" if watching_paused else "tray.pause_watching", language),
            enabled=not recording,
        ),
        MenuEntry(
            "open_library",
            i18n.t("tray.open_library", language),
            default=True,
            separator_before=True,
        ),
        MenuEntry("fill_timesheets", i18n.t("tray.fill_timesheets", language), enabled=timesheets),
        MenuEntry("open_settings", i18n.t("tray.settings", language)),
        MenuEntry(
            "install_update",
            i18n.t("tray.update", language, version=update),
            visible=bool(update),
        ),
        MenuEntry("quit", i18n.t("tray.quit", language), separator_before=True),
    ]


class Tray:
    """Icono de bandeja. ``set_state`` cambia color/glifo y el tooltip."""

    def __init__(
        self,
        callbacks: UiCallbacks,
        *,
        language: str = "es",
        timesheets: Callable[[], bool] | None = None,
    ) -> None:
        self._cb = callbacks
        self._language = language
        self._timesheets = timesheets or (lambda: False)
        self._state: TrayState = "watching"
        self._watching_paused = False
        self._icon: Any = None
        self._update_listener: Any = None

    # -- estado
    @property
    def state(self) -> TrayState:
        return self._state

    def title(self) -> str:
        """«Micnaga 0.1.1 · vigilando» (la version siempre a la vista)."""
        return i18n.t(
            "tray.tooltip",
            self._language,
            version=__version__,
            state=i18n.t(STATE_LABEL_KEYS[self._state], self._language),
        )

    def set_state(self, state: TrayState) -> None:
        # El supervisor del worker lo llama en cada vuelta (cada 15 s, 3 s con un trabajo) aunque
        # nada cambie: redibujar el icono (Pillow + ICO + LoadImage) solo si cambia el estado. El
        # menu se sigue reconstruyendo (sus entradas dinamicas, p. ej. la de reunion presencial,
        # solo se evaluan en ``update_menu``).
        self._watch_updates()
        if state == self._state:
            if self._icon is not None:
                self._icon.update_menu()
            return
        self._state = state
        self._refresh()

    def set_watching_paused(self, paused: bool) -> None:
        self._watching_paused = paused
        self._refresh()

    def _refresh(self) -> None:
        icon = self._icon
        if icon is None:
            return
        icon.icon = tray_icon(self._state)
        icon.title = self.title()
        icon.update_menu()

    @staticmethod
    def _in_person_on() -> bool:
        return app_action(ACTION_RECORD_IN_PERSON) is not None

    def _record_in_person(self) -> None:
        action = app_action(ACTION_RECORD_IN_PERSON)
        if action is not None:
            action()

    @staticmethod
    def _update_version() -> str:
        """Version nueva que se ofrece (no omitida) o ``""``."""
        from micnaga import updates

        svc = updates.service()
        rel = svc.available() if svc is not None else None
        return rel.version if rel is not None else ""

    def _install_update(self) -> None:
        from micnaga import updates

        svc = updates.service()
        if svc is not None:
            svc.request_install(open_ui=True)

    def _watch_updates(self) -> None:
        """Se suscribe al servicio de actualizaciones (lo registra la app despues de la bandeja):
        al aparecer una version nueva, el menu se rehace al momento."""
        if self._update_listener is not None:
            return
        from micnaga import updates

        svc = updates.service()
        if svc is None:
            return

        def changed() -> None:
            icon = self._icon
            if icon is not None:
                icon.update_menu()

        self._update_listener = changed
        svc.add_listener(changed)

    def _timesheets_on(self) -> bool:
        try:
            return bool(self._timesheets())
        except Exception:
            return False

    # -- acciones
    def dispatch(self, key: str) -> None:
        cb = self._cb
        actions = {
            "record_now": cb.on_record_now,
            "record_in_person": self._record_in_person,
            "pause_watching": lambda: cb.on_pause_watching(PAUSE_MINUTES),
            "resume_watching": cb.on_resume_watching,
            "open_library": cb.on_open_library,
            "open_settings": cb.on_open_settings,
            "fill_timesheets": cb.on_open_timesheets,
            "install_update": self._install_update,
            "quit": cb.on_quit,
        }
        action = actions.get(key)
        if action is None:
            return
        if key == "pause_watching":
            self._watching_paused = True
        elif key == "resume_watching":
            self._watching_paused = False
        try:
            action()
        except Exception:
            log.exception("tray action %s failed", key)
        self._refresh()

    # -- ciclo de vida
    def _build_menu(self) -> Any:
        import pystray

        items: list[Any] = []
        # El menu se evalua cada vez que se abre: los textos y `enabled` son callables.
        spec_keys = [e.key for e in menu_spec("watching", watching_paused=False)]
        for idx, _ in enumerate(spec_keys):

            def entry(i: int = idx) -> MenuEntry:
                return menu_spec(
                    self._state,
                    watching_paused=self._watching_paused,
                    language=self._language,
                    timesheets=self._timesheets_on(),
                    in_person=self._in_person_on(),
                    update=self._update_version(),
                )[i]

            def action(i: int = idx) -> Callable[[Any, Any], None]:
                # pystray exige exactamente 0, 1 o 2 parametros (sin valores por defecto)
                return lambda _icon, _item: self.dispatch(entry(i).key)

            if entry().separator_before:
                items.append(pystray.Menu.SEPARATOR)
            items.append(
                pystray.MenuItem(
                    lambda _item, i=idx: entry(i).label,
                    action(),
                    enabled=lambda _item, i=idx: entry(i).enabled,
                    default=entry().default,
                    visible=lambda _item, i=idx: entry(i).visible,
                )
            )
        return pystray.Menu(*items)

    def start(self) -> None:
        """Muestra el icono (hilo propio). Idempotente."""
        if self._icon is not None:
            return
        # fotograma del tamano exacto de la bandeja (``tray-<estado>.ico``), no uno de 32 reducido
        self._icon = make_pystray_icon(
            "micnaga", tray_icon(self._state), self.title(), self._build_menu()
        )
        self._icon.run_detached()
        self._watch_updates()

    def stop(self) -> None:
        listener, self._update_listener = self._update_listener, None
        if listener is not None:
            from micnaga import updates

            svc = updates.service()
            if svc is not None:
                svc.remove_listener(listener)
        icon, self._icon = self._icon, None
        if icon is not None:
            icon.stop()
