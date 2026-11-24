"""Maquina de estados del vigilante: pura, sin hilos, sin reloj propio, sin imports de UI/captura.

``WatcherStateMachine.step(event, now) -> list[Action]``: recibe un evento (un tick con la
instantanea de senales, una respuesta del popup, hover, tray, hotkeys...) y el instante ``now``
(segundos, reloj monotono inyectado por quien llama) y devuelve las acciones que debe ejecutar el
controlador. No llama a nada: el ``Watcher`` toma un lock, hace ``step`` y despacha las acciones
fuera del lock (ver ``watcher.py``), asi un controlador que reentre no provoca interbloqueos.

Flujo (decide el usuario; DECISIONS §18, ``docs/state_machine.md``):

* **Deteccion**: cualquier app con el micro abierto ``mic_grace_s`` seguidos -> grabacion
  provisional + pastilla; ``popup_after_recording_s`` despues, «¿Estas en una reu?». Meet es
  una ruta rapida (~``confirm_after_s``) con las mismas reglas.
* **Popup**: «Si» -> confirmada (se transcribira). «Ahora no» -> la grabacion se pausa, se marca
  como descartada en disco al instante y sale «Descartada · Deshacer» ``undo_s``; al expirar se
  aparta en «Descartadas» (recuperable N dias, nunca se borra al momento). Sin respuesta -> el
  popup se convierte en la pastilla provisional (con «¿Guardar?») y se decide al final.
* **Fin**: la app suelta el micro ``end_confirm_s`` seguidos -> se anota el punto de recorte y
  sale el popup de fin: confirmada «Reunión terminada» (Terminar / Seguir grabando; sin respuesta
  en ``end_popup_timeout_s`` termina), sin confirmar «¿La guardo?» (Guardar / Descartar; sin
  respuesta se aparta sin transcribir). Si el micro vuelve antes de contestar, el popup de fin se
  retira y la grabacion sigue como estaba.
* **Reunion presencial** (``RecordInPerson``, tray / biblioteca): sesion manual confirmada al
  momento, sin popup; no la cierran las senales (no hay llamada) ni la molesta un Meet que
  empiece mientras tanto. Solo la para el usuario.
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
    "declined",  # «Ahora no» / Esc
    "timeout",  # sin respuesta: se convierte en pastilla «provisional»
    "meeting_ended",  # la reunion acabo antes de responder: se desvanece
    "suspended",  # «Pausar vigilancia 1 h» con el popup abierto
]
EndCloseReason: TypeAlias = Literal["answered", "resumed", "timeout"]
EndChoice: TypeAlias = Literal["finish", "keep", "save", "discard"]
HoldReason: TypeAlias = Literal["declined", "unanswered", "suspended", "failed"]


class WatcherState(Enum):
    """Estado observable (tray, diagnostico). ``PAUSED`` y ``ENDING`` son superposiciones."""

    IDLE = "idle"  # vigilando
    CANDIDATE = "candidate"  # senales estables: Meet < confirm_after_s o micro < mic_grace_s
    RECORDING_PRE_POPUP = "recording_pre_popup"  # ruta generica: grabando, aun sin popup
    PROMPTING = "prompting"  # popup visible, grabacion provisional en marcha
    RECORDING_CONFIRMED = "recording_confirmed"
    RECORDING_PROVISIONAL = "recording_provisional"
    PAUSED = "paused"  # pausa privada (sobre PROMPTING / RECORDING_*)
    ENDING = "ending"  # el micro se solto: esperando ``end_confirm_s``
    END_PROMPT = "end_prompt"  # popup de fin visible
    DISCARD_PENDING = "discard_pending"  # «Descartada · Deshacer»
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
    END_PROMPT = "end_prompt"
    DISCARD_PENDING = "discard_pending"
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
    """Tray «Grabar ahora» / «¿Guardar?» de la pastilla (confirma una grabacion provisional)."""


@dataclass(frozen=True)
class RecordInPerson:
    """Tray / biblioteca «Grabar reunion presencial»: sesion manual confirmada, sin llamada.

    No adopta una pestana de Meet visible ni pregunta nada; solo la termina el usuario. Grabando
    ya (cualquier fase de grabacion) no hace nada."""


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


@dataclass(frozen=True)
class EndAnswer:
    """Respuesta al popup de fin: ``finish``/``keep`` (confirmada), ``save``/``discard``."""

    choice: EndChoice


@dataclass(frozen=True)
class Undo:
    """«Deshacer» del aviso «Descartada»."""


@dataclass(frozen=True)
class RecordingFailed:
    """El controlador no pudo abrir la captura para la ultima ``StartRecording``."""


Event: TypeAlias = (
    Tick
    | PopupAnswer
    | PopupHover
    | RecordNow
    | RecordInPerson
    | UserStop
    | PrivatePause
    | SuspendWatching
    | ResumeWatching
    | EndAnswer
    | Undo
    | RecordingFailed
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
class MarkEnd:
    """El micro se solto: el controlador anota AHORA el punto de recorte (tiempo de grabacion)."""


@dataclass(frozen=True)
class ClearEnd:
    """El micro volvio o el usuario sigue grabando: se olvida el punto de recorte."""


@dataclass(frozen=True)
class ShowEndPopup:
    confirmed: bool  # True: «Reunión terminada»; False: «¿La guardo?»
    title: str | None
    monitor_hint: MonitorHint | None
    timeout_s: float


@dataclass(frozen=True)
class CloseEndPopup:
    reason: EndCloseReason


@dataclass(frozen=True)
class BeginDiscard:
    """«Ahora no»/«Descartar»: marcar en disco YA, pausar la captura y ofrecer «Deshacer»."""

    undo_s: float


@dataclass(frozen=True)
class CancelDiscard:
    """«Deshacer»: quitar la marca y reanudar la captura."""


@dataclass(frozen=True)
class HoldRecording:
    """Parar y apartar en «Descartadas» (no se transcribe; recuperable N dias)."""

    reason: HoldReason
    trim: bool = False  # recortar en el punto de ``MarkEnd``


@dataclass(frozen=True)
class StopAndFinalize:
    confirmed: bool  # siempre True en el flujo normal; False solo en compatibilidad
    meeting: MeetingInfo  # titulo/codigo definitivos (nombre de carpeta, meet_code)
    trim: bool = False  # recortar en el punto de ``MarkEnd``


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
    | MarkEnd
    | ClearEnd
    | ShowEndPopup
    | CloseEndPopup
    | BeginDiscard
    | CancelDiscard
    | HoldRecording
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
    Phase.END_PROMPT,
    Phase.DISCARD_PENDING,
)
_LIVE_PHASES = (
    Phase.RECORDING_PRE_POPUP,
    Phase.PROMPTING,
    Phase.RECORDING_CONFIRMED,
    Phase.RECORDING_PROVISIONAL,
)
"""Fases en las que la senal de la sesion se vigila (fin de reunion)."""


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
    """Ver el docstring del modulo y ``docs/state_machine.md``."""

    def __init__(self, config: DetectionConfig) -> None:
        self._confirm_after_s = config.confirm_after_s
        self._end_after_s = config.end_after_s
        self._end_confirm_s = config.end_confirm_s
        self._end_timeout_s = config.end_popup_timeout_s
        self._undo_s = config.undo_s
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
        self._ignored: dict[str, float | None] = {}  # clave -> desde cuando no se ve
        self._suspended_until: float | None = None
        self._popup_elapsed = 0.0
        self._pre_popup_elapsed = 0.0
        self._end_elapsed = 0.0
        self._undo_deadline: float | None = None
        self._last_step: float | None = None
        self._hover = False
        self._mic_since: dict[str, float] = {}  # app generica -> inicio del uso continuo
        self._generic_origin = False  # la sesion nacio de la ruta generica
        self._popup_shown = False
        self._pill_visible = False
        self._confirmed = False
        self._keep_going = False  # «Seguir grabando»: solo el usuario la para
        self._user_stopped = False  # el popup de fin lo abrio el boton parar
        self._end_phase: Phase | None = None  # fase a la que vuelve si el micro regresa
        self._undo_kind: Literal["popup", "end"] = "popup"
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
    def confirmed(self) -> bool:
        return self._confirmed

    @property
    def ignored_keys(self) -> frozenset[str]:
        return frozenset(self._ignored)

    @property
    def popup_remaining_s(self) -> float | None:
        if self._phase is Phase.PROMPTING:
            return max(0.0, self._popup_timeout_s - self._popup_elapsed)
        if self._phase is Phase.END_PROMPT:
            return max(0.0, self._end_timeout_s - self._end_elapsed)
        return None

    @property
    def popup_total_s(self) -> float | None:
        if self._phase is Phase.PROMPTING:
            return self._popup_timeout_s
        if self._phase is Phase.END_PROMPT:
            return self._end_timeout_s
        return None

    @property
    def popup_frozen(self) -> bool:
        """``True`` si la cuenta atras del popup esta congelada (raton, pausa o senal caida)."""
        if self._phase is Phase.END_PROMPT:
            return self._hover
        return self._phase is Phase.PROMPTING and (
            self._hover or self._paused or self._lost_since is not None
        )

    @property
    def needs_fast_ticks(self) -> bool:
        """Hay un temporizador corto en marcha (popup, popup de fin, «Deshacer», fin)."""
        return self._phase in (Phase.PROMPTING, Phase.END_PROMPT, Phase.DISCARD_PENDING) or (
            self._phase in _LIVE_PHASES and self._lost_since is not None
        )

    @property
    def state(self) -> WatcherState:
        if self._phase is Phase.SUSPENDED:
            return WatcherState.SUSPENDED
        if self._phase is Phase.END_PROMPT:
            return WatcherState.END_PROMPT
        if self._phase is Phase.DISCARD_PENDING:
            return WatcherState.DISCARD_PENDING
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
            if self._phase in (Phase.PROMPTING, Phase.END_PROMPT):
                self._hover = event.hovering
        elif isinstance(event, RecordNow):
            self._on_record_now(now, actions)
        elif isinstance(event, RecordInPerson):
            self._on_record_in_person(actions)
        elif isinstance(event, UserStop):
            self._on_user_stop(now, actions)
        elif isinstance(event, PrivatePause):
            self._on_private_pause(event.paused, actions)
        elif isinstance(event, SuspendWatching):
            self._on_suspend(event.duration_s, now, actions)
        elif isinstance(event, EndAnswer):
            self._on_end_answer(event.choice, now, actions)
        elif isinstance(event, Undo):
            self._on_undo(now, actions)
        elif isinstance(event, RecordingFailed):
            self._on_recording_failed(now, actions)
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
        if self._phase is Phase.DISCARD_PENDING:
            if self._undo_deadline is not None and now >= self._undo_deadline:
                self._finish_discard(now, actions)
            return
        if snap is not None:
            self._age_ignored(snap, now)
            if self._phase in (Phase.IDLE, Phase.CANDIDATE):
                self._tick_watching(snap, now, actions)
            else:
                self._tick_session(snap, now, actions)
        elif self._phase in _LIVE_PHASES and self._lost_since is not None:
            self._check_end(now, actions)
        if (
            self._phase is Phase.RECORDING_PRE_POPUP
            and self._pre_popup_elapsed >= self._popup_after_s
        ):
            self._open_popup(actions)
        if self._phase is Phase.PROMPTING and self._popup_elapsed >= self._popup_timeout_s:
            self._on_popup_timeout(now, actions)
        if self._phase is Phase.END_PROMPT and self._end_elapsed >= self._end_timeout_s:
            self._on_end_timeout(now, actions)

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
        self._end_elapsed = 0.0
        self._popup_shown = False
        self._confirmed = False
        self._keep_going = False
        self._user_stopped = False
        self._end_phase = None
        self._undo_deadline = None

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
            session,
            title=tab.title or session.title,
            code=tab.code,
            monitor=tab.monitor,
            generic_app=None,
        )
        if self._phase is Phase.RECORDING_PRE_POPUP:
            self._open_popup(actions)  # el popup que aun no habia salido, ahora

    def _alive(self, snap: SignalSnapshot) -> bool:
        session = self._session
        if session is None:
            return False
        if session.generic_app is not None:
            return any(a.key == session.generic_app for a in snap.mic_apps)
        mine = self._session_tabs(snap)
        if not mine:
            return False
        tab = pick_tab(tuple(mine))
        updated = session
        if tab.title and tab.title != session.title:
            updated = dataclasses.replace(updated, title=tab.title)
        if tab.code and not session.code:
            updated = dataclasses.replace(updated, code=tab.code)
        if updated is not session:
            self._session = updated
        return True

    def _tick_session(self, snap: SignalSnapshot, now: float, actions: list[Action]) -> None:
        session = self._session
        if session is None or session.manual:
            return  # una grabacion manual solo la termina el usuario
        if self._phase is Phase.END_PROMPT:
            if self._user_stopped or self._end_phase is None:
                return
            if self._alive(snap):  # el micro volvio antes de contestar: se sigue como estaba
                self._resume_from_end(actions)
            return
        if self._keep_going:
            return  # «Seguir grabando»: solo el usuario la para
        self._maybe_upgrade_to_meet(snap, actions)
        if self._alive(snap):
            self._lost_since = None
            return
        if self._lost_since is None:
            self._lost_since = now
        self._check_end(now, actions)

    def _check_end(self, now: float, actions: list[Action]) -> None:
        if self._lost_since is not None and now - self._lost_since >= self._end_confirm_s:
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
        """El micro de la sesion se solto ``end_confirm_s`` seguidos."""
        if self._phase is Phase.RECORDING_PRE_POPUP and not self._confirmed:
            # Nunca se pregunto: no se molesta con un popup; se aparta sin transcribir
            # (recuperable desde la biblioteca).
            actions.append(MarkEnd())
            actions.append(HoldRecording("unanswered", trim=True))
            self._hide_pill_if_visible(actions)
            self._after_session(now)
            return
        if self._phase is Phase.PROMPTING:
            actions.append(ClosePopup("meeting_ended"))
        actions.append(MarkEnd())
        self._open_end_popup(actions, back_to=self._phase)

    def _open_end_popup(self, actions: list[Action], *, back_to: Phase | None) -> None:
        session = self._session or MeetingInfo()
        self._end_phase = back_to
        self._phase = Phase.END_PROMPT
        self._end_elapsed = 0.0
        self._hover = False
        actions.append(
            ShowEndPopup(self._confirmed, session.title, session.monitor, self._end_timeout_s)
        )

    def _resume_from_end(self, actions: list[Action]) -> None:
        back = self._end_phase or Phase.RECORDING_PROVISIONAL
        actions.append(CloseEndPopup("resumed"))
        actions.append(ClearEnd())
        self._lost_since = None
        self._end_phase = None
        self._hover = False
        if back is Phase.PROMPTING:
            self._open_popup(actions)  # se habia retirado: vuelve a preguntar
            return
        self._phase = back

    def _on_end_answer(self, choice: EndChoice, now: float, actions: list[Action]) -> None:
        if self._phase is not Phase.END_PROMPT:
            return  # respuesta tardia
        session = self._session or MeetingInfo()
        if choice == "keep":
            actions.append(CloseEndPopup("answered"))
            actions.append(ClearEnd())
            if not self._confirmed:
                actions.append(ConfirmRecording())
                self._confirmed = True
            self._keep_going = True
            self._lost_since = None
            self._end_phase = None
            self._user_stopped = False
            self._phase = Phase.RECORDING_CONFIRMED
            actions.append(ShowPill(provisional=False, paused=self._paused))
            self._pill_visible = True
            return
        if choice == "discard":
            actions.append(CloseEndPopup("answered"))
            self._begin_discard(actions, kind="end")
            return
        # finish / save: se guarda (confirmada) recortada al momento en que se solto el micro
        actions.append(CloseEndPopup("answered"))
        if not self._confirmed:
            actions.append(ConfirmRecording())
        actions.append(StopAndFinalize(confirmed=True, meeting=session, trim=True))
        self._hide_pill_if_visible(actions)
        self._ignore_after_end()
        self._after_session(now)

    def _on_end_timeout(self, now: float, actions: list[Action]) -> None:
        session = self._session or MeetingInfo()
        actions.append(CloseEndPopup("timeout"))
        if self._confirmed:
            actions.append(StopAndFinalize(confirmed=True, meeting=session, trim=True))
        else:
            actions.append(HoldRecording("unanswered", trim=True))
        self._hide_pill_if_visible(actions)
        self._ignore_after_end()
        self._after_session(now)

    def _hide_pill_if_visible(self, actions: list[Action]) -> None:
        if self._pill_visible:
            actions.append(HidePill())
            self._pill_visible = False

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
        self._end_elapsed = 0.0
        self._confirmed = False
        self._keep_going = False
        self._user_stopped = False
        self._end_phase = None
        self._undo_deadline = None
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

    def _ignore_after_end(self) -> None:
        """Cierre tras el popup de fin. Si lo abrio el boton parar (senales aun vivas) no se
        vuelve a preguntar por ellas; si lo abrio la perdida de senal, solo se ignora la propia
        sesion: otra reunion/app viva en ese momento (reuniones seguidas) es una sesion nueva."""
        if self._user_stopped:
            self._ignore_live()
        else:
            self._ignore_current()

    def _ignore_live(self) -> None:
        """Tras cerrar una sesion con las senales aun vivas no se vuelve a preguntar."""
        self._ignore_current()
        for tab in self._last.live_tabs():
            self._ignored[tab.key] = None
        for app in self._last.mic_apps:
            self._ignored[app.key] = None

    # ------------------------------------------------------------------ «Descartada · Deshacer»

    def _begin_discard(self, actions: list[Action], *, kind: Literal["popup", "end"]) -> None:
        self._undo_kind = kind
        self._phase = Phase.DISCARD_PENDING
        self._undo_deadline = (self._last_step or 0.0) + self._undo_s
        self._hover = False
        self._hide_pill_if_visible(actions)
        actions.append(BeginDiscard(self._undo_s))

    def _finish_discard(self, now: float, actions: list[Action]) -> None:
        actions.append(HoldRecording("declined", trim=self._undo_kind == "end"))
        self._ignore_after_end()
        self._after_session(now)

    def _on_undo(self, now: float, actions: list[Action]) -> None:
        if self._phase is not Phase.DISCARD_PENDING:
            return
        session = self._session or MeetingInfo()
        actions.append(CancelDiscard())
        self._undo_deadline = None
        if self._undo_kind == "end":
            # Deshacer «Descartar» del final = «Guardar».
            actions.append(ConfirmRecording())
            actions.append(StopAndFinalize(confirmed=True, meeting=session, trim=True))
            self._ignore_after_end()
            self._after_session(now)
            return
        # Deshacer «Ahora no» = «Si»: se confirma y la grabacion sigue (la pausa no se grabo).
        self._ignored.pop(session.key, None)
        if session.browser:
            self._ignored.pop(f"app:{session.browser}", None)
        actions.append(ConfirmRecording())
        self._confirmed = True
        self._lost_since = None  # el reloj de fin no corre durante «Deshacer»: empieza de cero
        self._phase = Phase.RECORDING_CONFIRMED
        actions.append(ShowPill(provisional=False, paused=self._paused))
        self._pill_visible = True

    # ------------------------------------------------------------------ relojes y popup

    def _advance_clocks(self, now: float) -> None:
        last, self._last_step = self._last_step, now
        if last is None:
            return
        dt = max(0.0, now - last)
        if self._phase is Phase.PROMPTING:
            # La cuenta atras se congela con el raton encima, en pausa privada y mientras la
            # senal esta caida (el popup de fin decide que pasa entonces).
            if not self._hover and not self._paused and self._lost_since is None:
                self._popup_elapsed += dt
        elif self._phase is Phase.END_PROMPT:
            if not self._hover:
                self._end_elapsed += dt
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
        actions.append(ClosePopup("declined"))
        self._ignore_current()  # «Ahora no»: ignora esa reunion/app hasta colgar
        self._begin_discard(actions, kind="popup")

    def _confirm_from_popup(self, actions: list[Action]) -> None:
        actions.append(ConfirmRecording())
        actions.append(ClosePopup("confirmed"))
        actions.append(ShowPill(provisional=False, paused=self._paused))
        self._pill_visible = True
        self._confirmed = True
        self._phase = Phase.RECORDING_CONFIRMED
        self._hover = False

    def _on_popup_timeout(self, now: float, actions: list[Action]) -> None:
        if self._on_timeout == "discard":
            # Nada se borra sin un «No»: sin respuesta se aparta (recuperable).
            actions.append(ClosePopup("timeout"))
            actions.append(HoldRecording("unanswered"))
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
            self._pill_visible = True
            self._confirmed = True
            self._phase = Phase.RECORDING_CONFIRMED
        elif self._phase is Phase.END_PROMPT:
            self._on_end_answer("keep" if self._confirmed else "save", now, actions)
        elif self._phase is Phase.DISCARD_PENDING:
            self._on_undo(now, actions)
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
            self._start_confirmed(meeting, actions)

    def _start_confirmed(self, meeting: MeetingInfo, actions: list[Action]) -> None:
        self._begin_session(meeting, generic=False)
        self._phase = Phase.RECORDING_CONFIRMED
        self._confirmed = True
        self._pill_visible = True
        actions.append(StartRecording(provisional=False, meeting=meeting))
        actions.append(ShowPill(provisional=False, paused=False))

    def _on_record_in_person(self, actions: list[Action]) -> None:
        """Reunion presencial: manual (ni senales que la cierren ni popups de Meet), confirmada
        desde el principio y sin adoptar ninguna pestana de Meet. Grabando ya, no hace nada."""
        if self._phase not in (Phase.IDLE, Phase.CANDIDATE, Phase.SUSPENDED):
            return
        self._start_confirmed(MeetingInfo(manual=True, in_person=True), actions)

    def _on_user_stop(self, now: float, actions: list[Action]) -> None:
        if self._phase is Phase.PROMPTING:
            self._on_popup_answer(False, now, actions)
            return
        if self._phase is Phase.END_PROMPT:
            self._on_end_answer("finish" if self._confirmed else "save", now, actions)
            return
        if self._phase not in (
            Phase.RECORDING_CONFIRMED,
            Phase.RECORDING_PROVISIONAL,
            Phase.RECORDING_PRE_POPUP,
        ):
            return
        session = self._session or MeetingInfo()
        if self._confirmed:
            actions.append(StopAndFinalize(confirmed=True, meeting=session))
            self._hide_pill_if_visible(actions)
            self._ignore_live()
            self._after_session(now)
            return
        # Parar una grabacion sin confirmar: se pregunta «¿La guardo?» (nada se pierde sin un no).
        actions.append(MarkEnd())
        self._user_stopped = True
        self._open_end_popup(actions, back_to=None)

    def _on_private_pause(self, paused: bool | None, actions: list[Action]) -> None:
        if self._phase not in _LIVE_PHASES:
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
            actions.append(ClosePopup("suspended"))
            actions.append(HoldRecording("suspended"))
            self._hide_pill_if_visible(actions)
            self._after_session(now)
        # Grabando: no se interrumpe; la suspension rige cuando acabe la sesion.

    def _on_resume_watching(self, actions: list[Action]) -> None:
        self._suspended_until = None
        if self._phase is Phase.SUSPENDED:
            self._phase = Phase.IDLE
            self._stable_since = None
            self._mic_since.clear()

    def _on_recording_failed(self, now: float, actions: list[Action]) -> None:
        """La captura no se pudo abrir: no hay nada grabando, se vuelve a vigilar.

        Una sesion detectada sola se ignora hasta colgar (sin esto, un dispositivo roto daria un
        aviso cada pocos segundos); «Grabar ahora» siempre puede reintentar al momento.
        """
        if self._phase not in _RECORDING_PHASES:
            return
        if self._phase is Phase.PROMPTING:
            actions.append(ClosePopup("meeting_ended"))
        elif self._phase is Phase.END_PROMPT:
            actions.append(CloseEndPopup("timeout"))
        self._hide_pill_if_visible(actions)
        self._ignore_current()
        self._after_session(now)
