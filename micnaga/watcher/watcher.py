"""Fachada ``Watcher``: hilo de sondeo + maquina de estados + despacho al controlador.

Hilos: un hilo de sondeo (``micnaga-watcher``, duerme con ``Event.wait``; nada de bucle activo)
mas los hilos que llamen a los metodos de entrada (UI del popup, tray, hotkeys). Todo evento pasa
por ``_submit``: lock -> ``machine.step`` -> encolar acciones -> soltar el lock -> despachar al
controlador en orden. Un controlador que vuelva a llamar a ``popup_hover``/``record_now`` desde
dentro de ``show_popup`` no provoca interbloqueo.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from micnaga.config import Config, DetectionConfig
from micnaga.watcher.controller import WatcherController, dispatch_actions
from micnaga.watcher.machine import (
    Action,
    EndAnswer,
    EndChoice,
    Event,
    Phase,
    PopupAnswer,
    PopupHover,
    PrivatePause,
    RecordingFailed,
    RecordInPerson,
    RecordNow,
    ResumeWatching,
    StartRecording,
    SuspendWatching,
    Tick,
    Undo,
    UserStop,
    WatcherState,
    WatcherStateMachine,
)
from micnaga.watcher.model import SignalSnapshot
from micnaga.watcher.sampler import SignalSampler, build_default_sampler
from micnaga.watcher.sources import ExtensionSignalSource

log = logging.getLogger("micnaga.watcher")

POPUP_TIMER_STEP_S = 0.25
"""Con un temporizador corto en marcha (popup, popup de fin, «Deshacer», fin de reunion) los
temporizadores se evaluan cada 0.25 s (las senales siguen a 1 s)."""


class Watcher:
    def __init__(
        self,
        config: Config | DetectionConfig,
        controller: WatcherController,
        *,
        sampler: SignalSampler | None = None,
        extension: ExtensionSignalSource | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_state_change: Callable[[WatcherState], None] | None = None,
        init_com: bool | None = None,
    ) -> None:
        self._cfg = config.detection if isinstance(config, Config) else config
        self._controller = controller
        self._sampler = sampler
        self._extension = extension
        self._clock = clock
        self._on_state_change = on_state_change
        self._init_com = (sampler is None) if init_com is None else init_com

        self._machine = WatcherStateMachine(self._cfg)
        self._lock = threading.RLock()
        self._dispatch_lock = threading.RLock()
        self._pending: deque[Action] = deque()
        self._last_notified: WatcherState = self._machine.state

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_errors = 0
        self._last_error: str | None = None

    # ------------------------------------------------------------------ ciclo de vida

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="micnaga-watcher", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        """Para el hilo de sondeo. No cierra una grabacion en curso (lo decide la app)."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ estado

    @property
    def state(self) -> WatcherState:
        with self._lock:
            return self._machine.state

    @property
    def phase(self) -> Phase:
        with self._lock:
            return self._machine.phase

    @property
    def popup_remaining_s(self) -> float | None:
        """Segundos que le quedan al popup (``None`` sin popup). La cuenta atras del vigilante es
        la autoritativa: se congela con el raton encima, en pausa privada o con una senal caida."""
        with self._lock:
            return self._machine.popup_remaining_s

    @property
    def popup_total_s(self) -> float | None:
        """Duracion total de la cuenta atras del popup visible (normal o de fin)."""
        with self._lock:
            return self._machine.popup_total_s

    @property
    def needs_fast_ticks(self) -> bool:
        with self._lock:
            return self._machine.needs_fast_ticks

    @property
    def popup_frozen(self) -> bool:
        """La cuenta atras esta congelada (raton encima, pausa privada o senal caida)."""
        with self._lock:
            return self._machine.popup_frozen

    def diagnostics(self) -> dict[str, Any]:
        """Datos para la vista «diagnostico» de ajustes (estado + lectura de cada senal)."""
        with self._lock:
            m = self._machine
            session = m.session
            info: dict[str, Any] = {
                "state": m.state.value,
                "confirmed": m.confirmed,
                "phase": m.phase.value,
                "paused": m.paused,
                "popup_remaining_s": m.popup_remaining_s,
                "ignored": sorted(m.ignored_keys),
                "session": None
                if session is None
                else {
                    "title": session.title,
                    "code": session.code,
                    "manual": session.manual,
                    "in_person": session.in_person,
                },
            }
        info["poll_errors"] = self._poll_errors
        info["last_error"] = self._last_error
        sampler = self._sampler
        info["sampler"] = sampler.diagnostics() if sampler is not None else None
        return info

    # ------------------------------------------------------------------ eventos de entrada

    def popup_answer(self, accept: bool) -> None:
        """Clic en «Grabar» (True) o «Ahora no» (False)."""
        self._submit(PopupAnswer(accept))

    def popup_hover(self, hovering: bool) -> None:
        """El raton entra/sale del popup: pausa/reanuda la cuenta atras."""
        self._submit(PopupHover(hovering))

    def record_now(self) -> None:
        """Tray «Grabar ahora» (tambien confirma una grabacion provisional)."""
        self._submit(RecordNow())

    def record_in_person(self) -> bool:
        """«Grabar reunion presencial» (tray / biblioteca). ``True`` si empezo a grabar;
        ``False`` si ya habia una grabacion en marcha (no se hace nada)."""
        actions = self._submit(RecordInPerson())
        return any(isinstance(a, StartRecording) for a in actions)

    def user_stop(self) -> None:
        """Boton parar de la pastilla / tray."""
        self._submit(UserStop())

    def toggle_private_pause(self) -> None:
        """Atajo ``hotkeys.private_pause`` (MIC-13)."""
        self._submit(PrivatePause(None))

    def set_private_pause(self, paused: bool) -> None:
        self._submit(PrivatePause(paused))

    def suspend(self, duration_s: float = 3600.0) -> None:
        """Tray «Pausar vigilancia 1 h»."""
        self._submit(SuspendWatching(duration_s))

    def resume_watching(self) -> None:
        self._submit(ResumeWatching())

    def end_answer(self, choice: EndChoice) -> None:
        """Popup de fin: ``finish`` | ``keep`` (confirmada) o ``save`` | ``discard``."""
        self._submit(EndAnswer(choice))

    def undo(self) -> None:
        """«Deshacer» del aviso «Descartada»."""
        self._submit(Undo())

    def recording_failed(self) -> None:
        """El controlador no pudo abrir la captura: la maquina vuelve a vigilar."""
        self._submit(RecordingFailed())

    def feed(self, snapshot: SignalSnapshot | None) -> None:
        """Inyecta una instantanea (pruebas / fuentes externas); ``None`` = solo temporizadores."""
        self._submit(Tick(snapshot))

    # ------------------------------------------------------------------ nucleo

    def _submit(self, event: Event) -> list[Action]:
        """Paso de la maquina + despacho. Devuelve las acciones que produjo este evento."""
        with self._lock:
            actions = self._machine.step(event, self._clock())
            self._pending.extend(actions)
            state = self._machine.state
            changed = state != self._last_notified
            self._last_notified = state
        self._drain()
        if changed and self._on_state_change is not None:
            try:
                self._on_state_change(state)
            except Exception:
                log.exception("watcher state listener failed")
        return actions

    def _drain(self) -> None:
        with self._dispatch_lock:
            while True:
                with self._lock:
                    if not self._pending:
                        return
                    action = self._pending.popleft()
                dispatch_actions(self._controller, [action])

    # ------------------------------------------------------------------ hilo de sondeo

    def _run(self) -> None:
        com_ready = False
        if self._init_com:
            try:
                import comtypes

                comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)  # type: ignore[no-untyped-call]
                com_ready = True
            except Exception:
                log.exception("COM init failed in watcher thread")
        sampler = self._sampler
        try:
            if sampler is None:
                sampler = build_default_sampler(self._cfg, self._extension)
                self._sampler = sampler
            self._poll_loop(sampler)
        except Exception:
            log.exception("watcher thread crashed")
        finally:
            try:
                if sampler is not None:
                    sampler.close()
            except Exception:
                log.exception("sampler close failed")
            if com_ready:
                import comtypes

                comtypes.CoUninitialize()  # type: ignore[no-untyped-call]

    def _poll_loop(self, sampler: SignalSampler) -> None:
        interval = max(0.05, self._cfg.poll_interval_s)
        next_poll = self._clock()
        while not self._stop.is_set():
            now = self._clock()
            if now >= next_poll:
                next_poll = now + interval
                try:
                    snap: SignalSnapshot | None = sampler.sample()
                except Exception as exc:
                    snap = None
                    self._poll_errors += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    if self._poll_errors <= 3 or self._poll_errors % 60 == 0:
                        log.exception("signal sampling failed (%d)", self._poll_errors)
                self._submit(Tick(snap))
            elif self.needs_fast_ticks:
                self._submit(Tick(None))
            wait = max(0.0, next_poll - self._clock())
            if self.needs_fast_ticks:
                wait = min(wait, POPUP_TIMER_STEP_S)
            self._stop.wait(wait)
