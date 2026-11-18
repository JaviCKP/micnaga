"""``AppController``: implementa ``WatcherController`` uniendo vigilante, captura, UI y cola.

Reglas de hilos (los metodos los llama el hilo del vigilante bajo su lock de despacho, o el
hilo de la UI/hotkeys, y **deben volver rapido**):

* ``start_recording`` abre la sesion y el ``Recorder`` en linea (la grabacion empieza ya).
* ``stop_and_finalize`` / ``discard_recording`` fijan los metadatos y sueltan el ``Recorder`` en
  linea, pero ``Recorder.stop()`` (puede esperar hasta 5 s a sus hilos), la conversion a FLAC, el
  borrado y el encolado corren en hilos aparte: un hilo corto por parada (para sellar el fin de la
  grabacion sin esperar a nadie) y el ``Finisher`` serie para el resto.
* Los metadatos (``set_meeting_info``, ``set_confirmed``) se fijan **antes** de ``stop()``:
  el grabador solo los persiste mientras esta grabando.
* Sin grabador (``start`` fallo por falta de dispositivos), confirmar/parar/descartar no hacen
  nada y no se encola nada; la pastilla no se muestra (nunca una pastilla sin grabacion).

La cuenta atras del popup es la del vigilante: la UI la pinta con ``popup_state`` (se la da
``popup_state()`` de este controlador) y el vigilante la cierra con ``close_popup``.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from micnaga import paths
from micnaga.config import Config
from micnaga.integration.jobs import Finisher, delete_session_dir, finalize_and_enqueue
from micnaga.integration.ports import RecorderFactory, RecorderLike, UiPort
from micnaga.watcher import MeetingInfo, MonitorHint, PopupCloseReason

if TYPE_CHECKING:
    from micnaga.watcher import Watcher

log = logging.getLogger(__name__)

DEFAULT_POPUP_TITLE = "Reunión"
LONG_TIMEOUT_S = 86400.0
"""Timeout que se pasa al JS del popup: la cuenta atras real es la del vigilante."""


def default_recorder_factory(capture: object) -> RecorderLike:
    from micnaga.capture import Recorder
    from micnaga.config import CaptureConfig

    assert isinstance(capture, CaptureConfig)
    return Recorder(capture)


def foreground_hwnd() -> int | None:
    """Ventana en primer plano (para colocar el popup de una app generica)."""
    try:
        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return None
    return hwnd or None


class _Session:
    def __init__(self, directory: Path, meeting: MeetingInfo, recorder: RecorderLike) -> None:
        self.directory = directory
        self.meeting = meeting
        self.recorder = recorder
        self.paused = False


class AppController:
    """Ver el docstring del modulo."""

    def __init__(
        self,
        config: Config,
        ui: UiPort,
        finisher: Finisher,
        *,
        recorder_factory: RecorderFactory | None = None,
        on_job_queued: Callable[[], None] | None = None,
        foreground: Callable[[], int | None] = foreground_hwnd,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        ticker: bool = True,
        sync_s: float = 30.0,
    ) -> None:
        self._config = config
        self._ui = ui
        self._finisher = finisher
        self._factory = recorder_factory or default_recorder_factory
        self._on_job_queued = on_job_queued or (lambda: None)
        self._foreground = foreground
        self._now = now
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._watcher: Watcher | None = None
        self._stoppers: list[threading.Thread] = []
        self._popup_open = False
        # ticker (pastilla: sincroniza el cronometro cada 30 s)
        self._ticker_enabled = ticker
        self._sync_s = sync_s
        self._ticker: threading.Thread | None = None
        self._ticker_stop = threading.Event()
        self.marker_count = 0
        ui.set_levels_provider(self.levels)
        ui.set_popup_state_provider(self.popup_countdown)

    # ------------------------------------------------------------------ enlaces

    def bind_watcher(self, watcher: Watcher) -> None:
        self._watcher = watcher

    @property
    def config(self) -> Config:
        return self._config

    def set_config(self, config: Config) -> None:
        self._config = config

    @property
    def recording(self) -> bool:
        return self._session is not None

    @property
    def session_dir(self) -> Path | None:
        s = self._session
        return None if s is None else s.directory

    # ------------------------------------------------------------------ consulta para la UI

    def levels(self) -> tuple[float, float]:
        s = self._session
        if s is None:
            return (0.0, 0.0)
        try:
            return s.recorder.levels()
        except Exception:
            return (0.0, 0.0)

    def popup_countdown(self) -> tuple[float, bool] | None:
        """``(restante, congelada)`` del vigilante, o ``None`` sin popup."""
        w = self._watcher
        if w is None or not self._popup_open:
            return None
        remaining = w.popup_remaining_s
        if remaining is None:
            return None
        return remaining, w.popup_frozen

    # ------------------------------------------------------------------ WatcherController

    def start_recording(self, *, provisional: bool, meeting: MeetingInfo) -> None:
        with self._lock:
            if self._session is not None:
                log.warning("start_recording while a session is open; ignored")
                return
            title = (meeting.title or "").strip() or None
            when = self._now()
            root = paths.meetings_root(self._config)
            directory = paths.unique_folder(
                root, paths.meeting_folder_name(when, title or paths.DEFAULT_SLUG)
            )
            try:
                recorder = self._factory(self._config.capture)
                recorder.start(
                    directory,
                    confirmed=not provisional,
                    meet_title=title,
                    meet_code=meeting.code,
                )
            except Exception:
                log.exception("could not start recording")
                return
            self._session = _Session(directory, meeting, recorder)
        log.info("recording started (provisional=%s)", provisional)
        self._wake_ticker()

    def show_popup(
        self,
        *,
        title: str | None,
        code: str | None,
        monitor_hint: MonitorHint | None,
        timeout_s: float,
    ) -> None:
        hint = self._ui_hint(monitor_hint)
        self._popup_open = True
        self._ui.show_popup(
            title or DEFAULT_POPUP_TITLE,
            code,
            hint,
            timeout_s=LONG_TIMEOUT_S,
            on_timeout=self._config.detection.on_timeout,
            countdown_total_s=timeout_s,
        )

    def close_popup(self, *, reason: PopupCloseReason) -> None:
        self._popup_open = False
        if reason == "confirmed":
            self._ui.accept_popup()
        elif reason == "declined":
            self._ui.dismiss_popup()
        elif reason == "timeout":
            self._ui.expire_popup()
        else:  # meeting_ended | suspended
            self._ui.hide_popup(reason)

    def confirm_recording(self) -> None:
        with self._lock:
            s = self._session
        if s is None:
            return
        try:
            s.recorder.set_confirmed(True)
        except Exception:
            log.exception("set_confirmed failed")

    def discard_recording(self) -> None:
        s = self._take_session()
        if s is None:
            return
        directory = s.directory

        def stop_then_delete() -> None:
            self._safe_stop(s)
            self._finisher.submit("discard", lambda _db: delete_session_dir(directory))

        self._spawn_stopper(stop_then_delete)
        log.info("recording discarded")

    def stop_and_finalize(self, *, confirmed: bool, meeting: MeetingInfo) -> None:
        s = self._take_session()
        if s is None:
            return
        self._stop_session(s, confirmed=confirmed, meeting=meeting)

    def show_pill(self, *, provisional: bool, paused: bool) -> None:
        with self._lock:
            s = self._session
        if s is None:
            log.warning("show_pill without an active recording; not shown")
            return
        s.paused = paused
        self._ui.set_pill_paused(paused)
        self._ui.show_pill(
            elapsed_s=_elapsed(s.recorder),
            provisional=provisional,
            monitor_hint=self._foreground(),
        )

    def hide_pill(self) -> None:
        self._ui.hide_pill()

    def pause_recording(self) -> None:
        with self._lock:
            s = self._session
        if s is not None:
            try:
                s.recorder.pause()
            except Exception:
                log.exception("pause failed")
            s.paused = True
        self._ui.set_pill_paused(True)

    def resume_recording(self) -> None:
        with self._lock:
            s = self._session
        if s is not None:
            try:
                s.recorder.resume()
            except Exception:
                log.exception("resume failed")
            s.paused = False
        self._ui.set_pill_paused(False)
        if s is not None:
            self._ui.sync_pill(_elapsed(s.recorder))

    # ------------------------------------------------------------------ acciones del usuario

    def add_marker(self, label: str = "") -> bool:
        """Atajo global (MIC-12) o boton de la pastilla. ``False`` si no se esta grabando."""
        with self._lock:
            s = self._session
        if s is None:
            return False
        try:
            s.recorder.add_marker(label)
        except Exception:
            log.warning("marker not added", exc_info=True)
            return False
        self.marker_count += 1
        return True

    def shutdown(self, *, wait_s: float = 120.0) -> None:
        """Salir: si se esta grabando, parar y encolar (nunca descartar), y esperar al cierre."""
        s = self._take_session()
        if s is not None:
            confirmed = _is_confirmed(s)
            self._ui.hide_pill()
            self._stop_session(s, confirmed=confirmed, meeting=s.meeting)
        self._ticker_stop.set()
        self.wait_idle(wait_s)

    def wait_idle(self, timeout_s: float = 60.0) -> bool:
        """Espera a que terminen las paradas y el cierre/encolado en curso (pruebas y salida)."""
        for t in list(self._stoppers):
            t.join(timeout_s)
        return self._finisher.wait_idle(timeout_s)

    # ------------------------------------------------------------------ internos

    def _take_session(self) -> _Session | None:
        with self._lock:
            s, self._session = self._session, None
        return s

    def _stop_session(self, s: _Session, *, confirmed: bool, meeting: MeetingInfo) -> None:
        # Metadatos ANTES de parar: el grabador solo los guarda mientras graba.
        try:
            if confirmed:
                s.recorder.set_confirmed(True)
            s.recorder.set_meeting_info(
                title=(meeting.title or "").strip() or None, code=meeting.code
            )
        except Exception:
            log.exception("could not persist the final meeting info")
        directory = s.directory

        def stop_then_queue() -> None:
            if not self._safe_stop(s):
                return
            self._finisher.submit(
                "finalize",
                finalize_and_enqueue(self._config, directory, on_enqueued=self._on_job_queued),
            )

        self._spawn_stopper(stop_then_queue)

    @staticmethod
    def _safe_stop(s: _Session) -> bool:
        try:
            s.recorder.stop()
            return True
        except Exception:
            log.exception("stopping the recorder failed")
            return False

    def _spawn_stopper(self, fn: Callable[[], None]) -> None:
        thread = threading.Thread(target=fn, name="micnaga-stop", daemon=True)
        with self._lock:
            self._stoppers = [t for t in self._stoppers if t.is_alive()]
            self._stoppers.append(thread)
        thread.start()

    def _ui_hint(self, hint: MonitorHint | None) -> int | tuple[int, int, int, int] | None:
        if hint is not None:
            return hint.rect
        return self._foreground()

    # -- ticker (cronometro de la pastilla: se corrige cada ``sync_s`` si deriva)
    def _wake_ticker(self) -> None:
        if not self._ticker_enabled:
            return
        if self._ticker is None or not self._ticker.is_alive():
            self._ticker_stop.clear()
            self._ticker = threading.Thread(
                target=self._tick_loop, name="micnaga-ticker", daemon=True
            )
            self._ticker.start()

    def _tick_loop(self) -> None:
        while not self._ticker_stop.wait(self._sync_s):
            s = self._session
            if s is not None and not s.paused:
                self.tick_sync(s)

    def tick_sync(self, s: _Session | None = None) -> None:
        s = s or self._session
        if s is not None:
            self._ui.sync_pill(_elapsed(s.recorder))


def _elapsed(recorder: RecorderLike) -> float:
    try:
        return float(recorder.elapsed_s())
    except Exception:
        return 0.0


def _is_confirmed(s: _Session) -> bool:
    status = getattr(s.recorder, "status", None)
    return bool(getattr(status, "confirmed", False))


__all__ = ["AppController", "default_recorder_factory", "foreground_hwnd"]
