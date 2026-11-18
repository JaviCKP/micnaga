"""Maquina de estados del vigilante: pura, sin hilos, sin reloj propio, sin imports de UI/captura.

``WatcherStateMachine.step(event, now) -> list[Action]``: recibe un evento (un tick con la
instantanea de senales, una respuesta del popup, hover, tray, hotkeys...) y el instante ``now``
(segundos, reloj monotono inyectado por quien llama) y devuelve las acciones que debe ejecutar el
controlador. No llama a nada: el ``Watcher`` toma un lock, hace ``step`` y despacha las acciones
fuera del lock (ver ``watcher.py``), asi un controlador que reentre no provoca interbloqueos.

Diagrama y tabla de transiciones: ``docs/state_machine.md``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Literal, TypeAlias

from micnaga.config import DetectionConfig
from micnaga.watcher.model import MeetingInfo, MeetTab, MicApp, MonitorHint, SignalSnapshot

PopupCloseReason: TypeAlias = Literal[
    "confirmed",  # «Grabar» (o «Grabar ahora»): la tarjeta vuela a la pastilla
    "declined",  # «Ahora no» / Esc / parar desde el popup
    "timeout",  # sin respuesta: se convierte en pastilla «provisional» (o se descarta)
    "meeting_ended",  # la reunion acabo antes de responder: se desvanece
    "suspended",  # «Pausar vigilancia 1 h» con el popup abierto
]


class WatcherState(Enum):
    """Estado observable (tray, diagnostico). ``PAUSED`` y ``ENDING`` son superposiciones."""

    IDLE = "idle"  # vigilando
    CANDIDATE = "candidate"  # senales estables: Meet < confirm_after_s o micro < mic_grace_s
    RECORDING_PRE_POPUP = "recording_pre_popup"  # ruta generica: grabando, aun sin popup
    PROMPTING = "prompting"  # popup visible, grabacion provisional en marcha
    RECORDING_CONFIRMED = "recording_confirmed"
    RECORDING_PROVISIONAL = "recording_provisional"
    PAUSED = "paused"  # pausa privada (sobre PROMPTING / RECORDING_*)
    ENDING = "ending"  # una senal obligatoria cayo; cuenta atras de end_after_s
    IGNORED = "ignored"  # reunion(es) ignoradas («Ahora no») hasta colgar
    SUSPENDED = "suspended"  # «Pausar vigilancia 1 h»


class Phase(Enum):
    """Fase base interna; ``WatcherState`` se deriva de ella mas los solapamientos."""

    IDLE = "idle"
    CANDIDATE = "candidate"
    RECORDING_PRE_POPUP = "recording_pre_popup"
    PROMPTING = "prompting"
    RECORDING_CONFIRMED = "recording_confirmed"
    RECORDING_PROVISIONAL = "recording_provisional"
    SUSPENDED = "suspended"


# --------------------------------------------------------------------------- eventos


@dataclass(frozen=True)
class Tick:
    """Paso de tiempo. ``snapshot=None`` = solo temporizadores (cuenta atras del popup)."""

    snapshot: SignalSnapshot | None = None


@dataclass(frozen=True)
class PopupAnswer:
    accept: bool  # True = «Grabar», False = «Ahora no»


@dataclass(frozen=True)
class PopupHover:
    hovering: bool


@dataclass(frozen=True)
class RecordNow:
    """Tray «Grabar ahora» (o confirmar una grabacion provisional desde la pastilla)."""


@dataclass(frozen=True)
class UserStop:
    """Parar desde la pastilla / tray (vale en cualquier estado de grabacion)."""


@dataclass(frozen=True)
class PrivatePause:
    paused: bool | None = None  # None = conmutar (atajo global)


@dataclass(frozen=True)
class SuspendWatching:
    duration_s: float = 3600.0


@dataclass(frozen=True)
class ResumeWatching:
    pass


Event: TypeAlias = (
    Tick
    | PopupAnswer
    | PopupHover
    | RecordNow
    | UserStop
    | PrivatePause
    | SuspendWatching
    | ResumeWatching
)


# --------------------------------------------------------------------------- acciones


@dataclass(frozen=True)
class StartRecording:
    provisional: bool
    meeting: MeetingInfo


@dataclass(frozen=True)
class ShowPopup:
    title: str | None
    code: str | None
    monitor_hint: MonitorHint | None
    timeout_s: float


@dataclass(frozen=True)
class ClosePopup:
    reason: PopupCloseReason


@dataclass(frozen=True)
class ConfirmRecording:
    pass


@dataclass(frozen=True)
class DiscardRecording:
    """Parar y borrar todo lo grabado de esta sesion (sin compuertas, sin cola)."""


@dataclass(frozen=True)
class StopAndFinalize:
    confirmed: bool  # False = provisional: las compuertas decidiran
    meeting: MeetingInfo  # titulo/codigo definitivos (nombre de carpeta, meet_code)


@dataclass(frozen=True)
class ShowPill:
    provisional: bool
    paused: bool = False


@dataclass(frozen=True)
class HidePill:
    pass


@dataclass(frozen=True)
class PauseRecording:
    pass


@dataclass(frozen=True)
class ResumeRecording:
    pass


Action: TypeAlias = (
    StartRecording
    | ShowPopup
    | ClosePopup
    | ConfirmRecording
    | DiscardRecording
    | StopAndFinalize
    | ShowPill
    | HidePill
    | PauseRecording
    | ResumeRecording
)

_RECORDING_PHASES = (
    Phase.RECORDING_PRE_POPUP,
    Phase.PROMPTING,
    Phase.RECORDING_CONFIRMED,
    Phase.RECORDING_PROVISIONAL,
)


def pick_app(apps: Iterable[MicApp], since: dict[str, float]) -> MicApp:
    """App que lleva mas tiempo con el micro; a igualdad, la de clave menor (orden estable)."""
    return min(apps, key=lambda a: (since.get(a.key, 0.0), a.key))


def pick_tab(tabs: tuple[MeetTab, ...]) -> MeetTab:
    """Pestana preferida: la activa de su ventana; si no, la primera (orden estable)."""
    for tab in tabs:
        if tab.active:
            return tab
    return tabs[0]


class WatcherStateMachine:
    """Ver ``docs/state_machine.md``."""

    def __init__(self, config: DetectionConfig) -> None:
        self._confirm_after_s = config.confirm_after_s
        self._end_after_s = config.end_after_s
        self._popup_timeout_s = config.popup_timeout_s
        self._on_timeout = config.on_timeout
        self._generic = config.generic_apps
        self._mic_grace_s = config.mic_grace_s
        self._popup_after_s = config.popup_after_recording_s

        self._phase = Phase.IDLE
        self._paused = False
        self._lost_since: float | None = None
        self._stable_since: float | None = None
        self._session: MeetingInfo | None = None
        self._ignored: dict[
            str, float | None
        ] = {}  # clave -> desde cuando no se ve (None = visible)
        self._suspended_until: float | None = None
        self._popup_elapsed = 0.0
        self._pre_popup_elapsed = 0.0
        self._last_step: float | None = None
        self._hover = False
        self._mic_since: dict[str, float] = {}  # app generica -> inicio del uso continuo
        self._generic_origin = False  # la sesion nacio de la ruta generica
        self._popup_shown = False
        self._pill_visible = False
        self._last: SignalSnapshot = SignalSnapshot()

    # ------------------------------------------------------------------ consulta

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def session(self) -> MeetingInfo | None:
        return self._session

    @property
    def ignored_keys(self) -> frozenset[str]:
        return frozenset(self._ignored)

    @property
    def popup_remaining_s(self) -> float | None:
        if self._phase is not Phase.PROMPTING:
            return None
        return max(0.0, self._popup_timeout_s - self._popup_elapsed)

    @property
    def popup_frozen(self) -> bool:
        """``True`` si la cuenta atras del popup esta congelada (raton, pausa o senal caida)."""
        return self._phase is Phase.PROMPTING and (
            self._hover or self._paused or self._lost_since is not None
        )

    @property
    def state(self) -> WatcherState:
        if self._phase is Phase.SUSPENDED:
            return WatcherState.SUSPENDED
        if self._phase in _RECORDING_PHASES:
            if self._paused:
                return WatcherState.PAUSED
            if self._lost_since is not None:
                return WatcherState.ENDING
            return WatcherState(self._phase.value)
        if self._phase is Phase.IDLE and self._ignored:
            return WatcherState.IGNORED
        return WatcherState(self._phase.value)

    # ------------------------------------------------------------------ entrada

    def step(self, event: Event, now: float) -> list[Action]:
        actions: list[Action] = []
        self._advance_clocks(now)
        if isinstance(event, Tick):
            self._on_tick(event.snapshot, now, actions)
        elif isinstance(event, PopupAnswer):
            self._on_popup_answer(event.accept, now, actions)
        elif isinstance(event, PopupHover):
            if self._phase is Phase.PROMPTING:
                self._hover = event.hovering
        elif isinstance(event, RecordNow):
            self._on_record_now(now, actions)
        elif isinstance(event, UserStop):
            self._on_user_stop(now, actions)
        elif isinstance(event, PrivatePause):
            self._on_private_pause(event.paused, actions)
        elif isinstance(event, SuspendWatching):
            self._on_suspend(event.duration_s, now, actions)
        else:
            self._on_resume_watching(actions)
        return actions

    # ------------------------------------------------------------------ tick

    def _on_tick(self, snap: SignalSnapshot | None, now: float, actions: list[Action]) -> None:
        if snap is not None:
            self._last = snap
        if self._suspended_until is not None and now >= self._suspended_until:
            self._suspended_until = None
            if self._phase is Phase.SUSPENDED:
                self._phase = Phase.IDLE  # los 5 s de estabilidad empiezan de cero
        if self._phase is Phase.SUSPENDED:
            return
        if snap is not None:
            self._age_ignored(snap, now)
            if self._phase in (Phase.IDLE, Phase.CANDIDATE):
                self._tick_watching(snap, now, actions)
            else:
                self._tick_session(snap, now, actions)
        if (
            self._phase is Phase.RECORDING_PRE_POPUP
            and self._pre_popup_elapsed >= self._popup_after_s
        ):
            self._open_popup(actions)
        if self._phase is Phase.PROMPTING and self._popup_elapsed >= self._popup_timeout_s:
            self._on_popup_timeout(now, actions)

    def _eligible(self, snap: SignalSnapshot) -> tuple[MeetTab, ...]:
        return tuple(t for t in snap.live_tabs() if t.key not in self._ignored)

    def _eligible_apps(self, snap: SignalSnapshot) -> list[MicApp]:
        if not self._generic:
            return []
        return [a for a in snap.mic_apps if a.key not in self._ignored]

    def _tick_watching(self, snap: SignalSnapshot, now: float, actions: list[Action]) -> None:
        tabs = self._eligible(snap)
        apps = self._eligible_apps(snap)
        # Ruta generica: uso CONTINUO del micro por cada app (cualquier corte reinicia la cuenta).
        present = {a.key for a in apps}
        for key in list(self._mic_since):
            if key not in present:
                del self._mic_since[key]
        for app in apps:
            self._mic_since.setdefault(app.key, now)
        # Ruta rapida de Meet: micro + pestana estables.
        if tabs:
            if self._stable_since is None:
                self._stable_since = now
        else:
            self._stable_since = None
        if self._stable_since is not None and now - self._stable_since >= self._confirm_after_s:
            self._start_meet(pick_tab(tabs), actions)
            return
        ready = [a for a in apps if now - self._mic_since[a.key] >= self._mic_grace_s]
        if ready:
            self._start_generic(pick_app(ready, self._mic_since), actions)
            return
        busy = self._stable_since is not None or bool(self._mic_since)
        self._phase = Phase.CANDIDATE if busy else Phase.IDLE

    def _begin_session(self, meeting: MeetingInfo, *, generic: bool) -> None:
        self._session = meeting
        self._generic_origin = generic
        self._stable_since = None
        self._mic_since.clear()
        self._lost_since = None
        self._paused = False
        self._hover = False
        self._popup_elapsed = 0.0
        self._pre_popup_elapsed = 0.0
        self._popup_shown = False

    def _start_meet(self, tab: MeetTab, actions: list[Action]) -> None:
        meeting = MeetingInfo.from_tab(tab)
        self._begin_session(meeting, generic=False)
        # La grabacion provisional arranca en el mismo instante en que sale el popup (spec §6).
        actions.append(StartRecording(provisional=True, meeting=meeting))
        self._open_popup(actions)

    def _start_generic(self, app: MicApp, actions: list[Action]) -> None:
        meeting = MeetingInfo(
            title=app.label,
            browser=app.exe if app.is_browser else None,
            generic_app=app.key,
        )
        self._begin_session(meeting, generic=True)
        self._phase = Phase.RECORDING_PRE_POPUP
        self._pill_visible = True
        actions.append(StartRecording(provisional=True, meeting=meeting))
        actions.append(ShowPill(provisional=True, paused=False))

    def _open_popup(self, actions: list[Action]) -> None:
        session = self._session or MeetingInfo()
        self._phase = Phase.PROMPTING
        self._popup_shown = True
        self._popup_elapsed = 0.0
        self._hover = False
        actions.append(
            ShowPopup(session.title, session.code, session.monitor, self._popup_timeout_s)
        )

    def _session_tabs(self, snap: SignalSnapshot) -> list[MeetTab]:
        """Pestanas vivas que son la de la sesion (identidad pegajosa por codigo)."""
        session = self._session
        if session is None:
            return []
        live = [
            t for t in snap.live_tabs() if session.browser is None or t.browser == session.browser
        ]
        if session.code:
            return [t for t in live if t.code == session.code]
        # Identidad debil (titulo sin codigo): cualquier Meet sin codigo; si solo queda alguno
        # con codigo, es la misma reunion cuyo codigo aparecio despues (lobby -> llamada).
        return [t for t in live if t.code is None] or live

    def _maybe_upgrade_to_meet(self, snap: SignalSnapshot, actions: list[Action]) -> None:
        """Sesion generica de un navegador + aparece su pestana de Meet: pasa a la ruta Meet."""
        session = self._session
        if session is None or session.generic_app is None or session.browser is None:
            return
        tabs = [
            t
            for t in snap.live_tabs()
            if t.browser == session.browser and t.key not in self._ignored
        ]
        if not tabs:
            return
        tab = pick_tab(tuple(tabs))
        self._session = dataclasses.replace(
            session, title=tab.title, code=tab.code, monitor=tab.monitor, generic_app=None
        )
        if self._phase is Phase.RECORDING_PRE_POPUP:
            self._open_popup(actions)  # el popup que aun no habia salido, ahora

    def _tick_session(self, snap: SignalSnapshot, now: float, actions: list[Action]) -> None:
        session = self._session
        if session is None or session.manual:
            return  # una grabacion manual solo la termina el usuario
        self._maybe_upgrade_to_meet(snap, actions)
        session = self._session
        if session is None:
            return
        alive = False
        if session.generic_app is not None:
            alive = any(a.key == session.generic_app for a in snap.mic_apps)
        else:
            mine = self._session_tabs(snap)
            if mine:
                alive = True
                tab = pick_tab(tuple(mine))
                updated = session
                if tab.title and tab.title != session.title:
                    updated = dataclasses.replace(updated, title=tab.title)
                if tab.code and not session.code:
                    updated = dataclasses.replace(updated, code=tab.code)
                if updated is not session:
                    self._session = updated
        if alive:
            self._lost_since = None
            return
        if self._lost_since is None:
            self._lost_since = now
        if now - self._lost_since >= self._end_after_s:
            self._end_session(now, actions)

    def _age_ignored(self, snap: SignalSnapshot, now: float) -> None:
        live_keys = {t.key for t in snap.live_tabs()} | {a.key for a in snap.mic_apps}
        for key in list(self._ignored):
            if key in live_keys:
                self._ignored[key] = None
                continue
            since = self._ignored[key]
            if since is None:
                self._ignored[key] = now
            elif now - since >= self._end_after_s:
                del self._ignored[key]  # colgaste: la proxima vez con ese codigo es otra reunion

    # ------------------------------------------------------------------ fin de sesion

    def _end_session(self, now: float, actions: list[Action]) -> None:
        """La senal de la sesion cayo ``end_after_s`` seguidos."""
        session = self._session or MeetingInfo()
        if self._phase is Phase.PROMPTING:
            actions.append(ClosePopup("meeting_ended"))
            if self._generic_origin:
                # Ya hay >= popup_after_recording_s de llamada grabada: las compuertas deciden.
                actions.append(StopAndFinalize(confirmed=False, meeting=session))
                actions.append(HidePill())
            else:
                actions.append(DiscardRecording())  # ruta Meet: acabo antes de responder
                self._hide_pill_if_visible(actions)
        elif self._phase in (Phase.RECORDING_PRE_POPUP, Phase.RECORDING_PROVISIONAL):
            actions.append(StopAndFinalize(confirmed=False, meeting=session))
            actions.append(HidePill())
        else:
            actions.append(StopAndFinalize(confirmed=True, meeting=session))
            actions.append(HidePill())
        self._after_session(now)

    def _hide_pill_if_visible(self, actions: list[Action]) -> None:
        if self._pill_visible:
            actions.append(HidePill())

    def _after_session(self, now: float) -> None:
        self._session = None
        self._paused = False
        self._lost_since = None
        self._stable_since = None
        self._mic_since.clear()
        self._hover = False
        self._generic_origin = False
        self._popup_shown = False
        self._pill_visible = False
        self._popup_elapsed = 0.0
        self._pre_popup_elapsed = 0.0
        if self._suspended_until is not None and now < self._suspended_until:
            self._phase = Phase.SUSPENDED
            self._ignored.clear()
        else:
            self._suspended_until = None
            self._phase = Phase.IDLE

    def _ignore_current(self) -> None:
        session = self._session
        if session is None or session.manual:
            return
        self._ignored[session.key] = None
        if session.browser:  # el navegador tambien cuenta como app generica
            self._ignored[f"app:{session.browser}"] = None

    # ------------------------------------------------------------------ relojes y popup

    def _advance_clocks(self, now: float) -> None:
        last, self._last_step = self._last_step, now
        if last is None:
            return
        dt = max(0.0, now - last)
        if self._phase is Phase.PROMPTING:
            # La cuenta atras se congela con el raton encima, en pausa privada y mientras la
            # senal esta caida: si no, el timeout (15 s) siempre ganaria al fin de reunion
            # (15 s) y «la reunion acaba antes de responder» seria inalcanzable.
            if not self._hover and not self._paused and self._lost_since is None:
                self._popup_elapsed += dt
        elif (
            self._phase is Phase.RECORDING_PRE_POPUP
            and not self._paused
            and self._lost_since is None  # no se abre un popup para una llamada que ya cuelga
        ):
            self._pre_popup_elapsed += dt

    def _on_popup_answer(self, accept: bool, now: float, actions: list[Action]) -> None:
        if self._phase is not Phase.PROMPTING:
            return  # respuesta tardia (el popup ya se cerro): se ignora
        if accept:
            self._confirm_from_popup(actions)
            return
        actions.append(DiscardRecording())
        actions.append(ClosePopup("declined"))
        self._hide_pill_if_visible(actions)  # la ruta generica ya mostraba la pastilla
        self._ignore_current()  # «Ahora no»: ignora esa reunion/app hasta colgar
        self._after_session(now)

    def _confirm_from_popup(self, actions: list[Action]) -> None:
        actions.append(ConfirmRecording())
        actions.append(ClosePopup("confirmed"))
        actions.append(ShowPill(provisional=False, paused=self._paused))
        self._pill_visible = True
        self._phase = Phase.RECORDING_CONFIRMED
        self._hover = False

    def _on_popup_timeout(self, now: float, actions: list[Action]) -> None:
        if self._on_timeout == "discard":
            actions.append(DiscardRecording())
            actions.append(ClosePopup("timeout"))
            self._hide_pill_if_visible(actions)
            self._ignore_current()  # si no, volveria a preguntar
            self._after_session(now)
            return
        actions.append(ClosePopup("timeout"))
        if not self._pill_visible:  # la ruta generica ya la mostraba desde el principio
            actions.append(ShowPill(provisional=True, paused=self._paused))
            self._pill_visible = True
        self._phase = Phase.RECORDING_PROVISIONAL
        self._hover = False

    # ------------------------------------------------------------------ tray / usuario

    def _on_record_now(self, now: float, actions: list[Action]) -> None:
        if self._phase is Phase.PROMPTING:
            self._confirm_from_popup(actions)
        elif self._phase in (Phase.RECORDING_PRE_POPUP, Phase.RECORDING_PROVISIONAL):
            actions.append(ConfirmRecording())
            actions.append(ShowPill(provisional=False, paused=self._paused))
            self._phase = Phase.RECORDING_CONFIRMED
        elif self._phase is Phase.RECORDING_CONFIRMED:
            return
        else:  # IDLE / CANDIDATE / SUSPENDED
            live = self._last.live_tabs()
            if live:
                free = tuple(t for t in live if t.key not in self._ignored)
                tab = pick_tab(free or live)
                self._ignored.pop(tab.key, None)
                meeting = MeetingInfo.from_tab(tab)
            else:
                meeting = MeetingInfo(manual=True)
            self._begin_session(meeting, generic=False)
            self._phase = Phase.RECORDING_CONFIRMED
            self._pill_visible = True
            actions.append(StartRecording(provisional=False, meeting=meeting))
            actions.append(ShowPill(provisional=False, paused=False))

    def _on_user_stop(self, now: float, actions: list[Action]) -> None:
        if self._phase is Phase.PROMPTING:
            self._on_popup_answer(False, now, actions)
            return
        if self._phase not in (
            Phase.RECORDING_CONFIRMED,
            Phase.RECORDING_PROVISIONAL,
            Phase.RECORDING_PRE_POPUP,
        ):
            return
        session = self._session or MeetingInfo()
        confirmed = self._phase is Phase.RECORDING_CONFIRMED
        actions.append(StopAndFinalize(confirmed=confirmed, meeting=session))
        actions.append(HidePill())
        # Sin esto, con las senales aun vivas volveria a preguntar.
        self._ignore_current()
        for tab in self._last.live_tabs():
            self._ignored[tab.key] = None
        for app in self._last.mic_apps:
            self._ignored[app.key] = None
        self._after_session(now)

    def _on_private_pause(self, paused: bool | None, actions: list[Action]) -> None:
        if self._phase not in _RECORDING_PHASES:
            return
        target = (not self._paused) if paused is None else paused
        if target == self._paused:
            return
        self._paused = target
        actions.append(PauseRecording() if target else ResumeRecording())

    def _on_suspend(self, duration_s: float, now: float, actions: list[Action]) -> None:
        self._suspended_until = now + duration_s
        if self._phase in (Phase.IDLE, Phase.CANDIDATE, Phase.SUSPENDED):
            self._phase = Phase.SUSPENDED
            self._stable_since = None
            self._mic_since.clear()
            self._ignored.clear()
        elif self._phase is Phase.PROMPTING:
            actions.append(DiscardRecording())
            actions.append(ClosePopup("suspended"))
            self._hide_pill_if_visible(actions)
            self._after_session(now)
        # Grabando: no se interrumpe; la suspension rige cuando acabe la sesion.

    def _on_resume_watching(self, actions: list[Action]) -> None:
        self._suspended_until = None
        if self._phase is Phase.SUSPENDED:
            self._phase = Phase.IDLE
            self._stable_since = None
            self._mic_since.clear()
