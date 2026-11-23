"""``AppController``: implementa ``WatcherController`` uniendo vigilante, captura, UI y cola.

Reglas de hilos (los metodos los llama el hilo del vigilante bajo su lock de despacho, o el
hilo de la UI/hotkeys, y **deben volver rapido**):

* ``start_recording`` abre la sesion y el ``Recorder`` en linea (la grabacion empieza ya). Si la
  captura no abre, avisa al usuario y se lo dice al vigilante (``recording_failed``): la maquina
  vuelve a vigilar y «Grabar ahora» puede reintentar (hallazgo 2 de la revision).
* ``begin_discard`` («Ahora no») escribe la marca de descartada en disco **antes de volver**
  (y por tanto antes de que se cierre el popup): si la app muere en ese momento, el arranque
  aparta la sesion en vez de encolarla (hallazgo 1). La captura se pausa (nada de despues del
  «No» se graba) y la UI ofrece «Deshacer».
* ``stop_and_finalize`` / ``hold_recording`` fijan los metadatos y sueltan el ``Recorder`` en
  linea, pero ``Recorder.stop()`` (puede esperar hasta 5 s a sus hilos), el recorte, la
  conversion a FLAC, el apartado y el encolado corren en hilos aparte: un hilo corto por parada
  y el ``Finisher`` serie para el resto.
* Los metadatos (``set_meeting_info``, ``set_confirmed``) se fijan **antes** de ``stop()``:
  el grabador solo los persiste mientras esta grabando.
* Sin grabador (``start`` fallo por falta de dispositivos), nada se muestra: ni pastilla ni popup
  sin grabacion detras.
* Procesado en vivo (``integration.live``): se lanza al empezar a grabar, se pide parar con el
  «No» (y se relanza con «Deshacer») y, al parar, el hilo de parada espera a que salga antes del
  FLAC. Apartar la sesion borra lo transcrito en vivo (``holding``).

La cuenta atras de los popups es la del vigilante: la UI la pinta con ``popup_state`` (se la da
``popup_countdown()`` de este controlador) y el vigilante los cierra.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from micnaga import holding, paths
from micnaga.config import Config
from micnaga.integration.jobs import (
    Finisher,
    finalize_and_enqueue,
    finalize_and_hold,
    write_trim_marker,
)
from micnaga.integration.live import LivePort, NoLive
from micnaga.integration.ports import RecorderFactory, RecorderLike, UiPort
from micnaga.watcher import (
    EndCloseReason,
    HoldReason,
    MeetingInfo,
    MonitorHint,
    PopupCloseReason,
)

if TYPE_CHECKING:
    from micnaga.watcher import Watcher

log = logging.getLogger(__name__)

DEFAULT_POPUP_TITLE = "Reunión"
IN_PERSON_TITLE = "Reunión presencial"
"""Titulo por defecto de «Grabar reunión presencial» (la IA puede renombrarla despues)."""
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
    def __init__(
        self,
        directory: Path,
        meeting: MeetingInfo,
        recorder: RecorderLike,
        *,
        db_path: Path | None = None,
    ) -> None:
        self.directory = directory
        self.db_path = db_path  # indice de la carpeta del arranque (no el de la config de ahora)
        self.meeting = meeting
        self.recorder = recorder
        self.paused = False
        self.confirmed = False
        self.end_mark_s: float | None = None
        self.discard_paused = False  # la pausa la puso «Ahora no» (la quita «Deshacer»)
        self.discarding = False


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
        on_job_queued_db: Callable[[Path], None] | None = None,
        foreground: Callable[[], int | None] = foreground_hwnd,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        ticker: bool = True,
        sync_s: float = 30.0,
        live: LivePort | None = None,
    ) -> None:
        self._config = config
        self._ui = ui
        self._finisher = finisher
        self._factory = recorder_factory or default_recorder_factory
        self._on_job_queued = on_job_queued or (lambda: None)
        self._on_job_queued_db = on_job_queued_db
        self._foreground = foreground
        self._live: LivePort = live if live is not None else NoLive()
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
        self.last_start_error: str | None = None
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
        if s is None or s.discarding:
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
            if title is None and meeting.in_person:
                title = IN_PERSON_TITLE
            when = self._now()
            error_key = "capture_failed"
            try:
                # La carpeta (y su indice) se fijan aqui y la sesion los conserva hasta el final
                # del procesado aunque se cambie la carpeta de reuniones (hallazgo P1-4). Un
                # fallo resolviendola (permisos...) es un fallo de arranque mas (P2-d).
                try:
                    root = paths.meetings_root(self._config)
                    db_path = root / paths.db_path(self._config).name
                    directory = paths.unique_folder(
                        root, paths.meeting_folder_name(when, title or paths.DEFAULT_SLUG)
                    )
                except OSError:
                    error_key = "storage_failed"
                    raise
                recorder = self._factory(self._config.capture)
                recorder.start(
                    directory,
                    confirmed=not provisional,
                    meet_title=title,
                    meet_code=meeting.code,
                )
            except Exception as exc:
                log.exception("could not start recording")
                self.last_start_error = f"{type(exc).__name__}: {exc}"
                failed = True
            else:
                failed = False
                self.last_start_error = None
                if meeting.in_person:  # en disco ya: sobrevive a un cierre brusco
                    from micnaga.capture.session import mark_in_person

                    mark_in_person(directory)
                session = _Session(directory, meeting, recorder, db_path=db_path)
                session.confirmed = not provisional
                self._session = session
        if failed:
            self._report_start_failure(error_key)
            return
        log.info("recording started (provisional=%s, in_person=%s)", provisional, meeting.in_person)
        self._start_live(directory)
        self._wake_ticker()

    def _report_start_failure(self, key: str = "capture_failed") -> None:
        try:
            self._ui.notify_error(key)
        except Exception:
            log.exception("could not show the capture error")
        w = self._watcher
        if w is not None:
            # Reentrante: el vigilante encola las acciones resultantes tras las pendientes.
            w.recording_failed()

    def show_popup(
        self,
        *,
        title: str | None,
        code: str | None,
        monitor_hint: MonitorHint | None,
        timeout_s: float,
    ) -> None:
        if self._session is None:
            log.warning("show_popup without an active recording; not shown")
            return
        hint = self._ui_hint(monitor_hint)
        self._popup_open = True
        log.info("popup shown")
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
        s.confirmed = True
        try:
            s.recorder.set_confirmed(True)
        except Exception:
            log.exception("set_confirmed failed")

    def mark_end(self) -> None:
        with self._lock:
            s = self._session
        if s is not None and s.end_mark_s is None:
            s.end_mark_s = _elapsed(s.recorder)
            log.info("meeting end marked at %.1f s", s.end_mark_s)

    def clear_end(self) -> None:
        with self._lock:
            s = self._session
        if s is not None:
            s.end_mark_s = None

    def show_end_popup(
        self,
        *,
        confirmed: bool,
        title: str | None,
        monitor_hint: MonitorHint | None,
        timeout_s: float,
    ) -> None:
        with self._lock:
            s = self._session
        if s is None:
            return
        duration = s.end_mark_s if s.end_mark_s is not None else _elapsed(s.recorder)
        self._popup_open = True
        log.info("end popup shown (confirmed=%s)", confirmed)
        self._ui.show_end_popup(
            title or DEFAULT_POPUP_TITLE,
            duration_s=duration,
            confirmed=confirmed,
            monitor_hint=self._ui_hint(monitor_hint),
            countdown_total_s=timeout_s,
        )

    def close_end_popup(self, *, reason: EndCloseReason) -> None:
        self._popup_open = False
        self._ui.hide_end_popup(reason)

    def begin_discard(self, *, undo_s: float) -> None:
        with self._lock:
            s = self._session
        if s is None:
            return
        # 1) persistente y sincrono: un cierre brusco desde aqui nunca transcribe este «No»
        holding.mark_discarded(s.directory, holding.REASON_DECLINED)
        s.discarding = True
        # 2) nada de despues del «No» se graba (la pausa privada no llega a disco)
        if not s.paused:
            try:
                s.recorder.pause()
                s.discard_paused = True
            except Exception:
                log.exception("pause on discard failed")
        self._ui.hide_pill()
        self._ui.show_undo(undo_s)
        try:
            self._live.signal_stop(s.directory)
        except Exception:
            log.exception("could not stop live processing")
        log.info("recording discarded (undo for %.0f s)", undo_s)

    def cancel_discard(self) -> None:
        with self._lock:
            s = self._session
        self._ui.hide_undo()
        if s is None:
            return
        holding.unmark_discarded(s.directory)
        s.discarding = False
        if s.discard_paused:
            s.discard_paused = False
            try:
                s.recorder.resume()
            except Exception:
                log.exception("resume on undo failed")
        self._start_live(s.directory)
        log.info("discard undone")

    def hold_recording(self, *, reason: HoldReason, trim: bool) -> None:
        s = self._take_session()
        self._ui.hide_undo()
        if s is None:
            return
        directory = s.directory
        holding.mark_discarded(directory, reason)  # por si la app muere antes de apartarla
        end_s = s.end_mark_s if trim else None
        if end_s is not None:  # el corte, en disco ANTES de parar (P2-a)
            write_trim_marker(directory, end_s)
        self._prepare_stop(s, confirmed=False, meeting=s.meeting)

        def stop_then_hold() -> None:
            self._safe_stop(s)
            self._stop_live(directory)
            self._finisher.submit(
                "hold",
                finalize_and_hold(self._config, directory, reason=reason, end_s=end_s),
            )

        self._spawn_stopper(stop_then_hold)
        log.info("recording held (%s)", reason)

    def stop_and_finalize(
        self, *, confirmed: bool, meeting: MeetingInfo, trim: bool = False
    ) -> None:
        s = self._take_session()
        if s is None:
            return
        self._ui.hide_undo()
        self._stop_session(s, confirmed=confirmed, meeting=meeting, trim=trim)

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
        if s is None or s.discarding:
            return False
        try:
            s.recorder.add_marker(label)
        except Exception:
            log.warning("marker not added", exc_info=True)
            return False
        self.marker_count += 1
        return True

    def shutdown(self, *, wait_s: float = 120.0) -> None:
        """Salir: si se esta grabando, parar. Confirmada -> se encola; sin confirmar o con un
        «Ahora no» pendiente -> se aparta (nunca se transcribe sin «Si», nunca se borra)."""
        s = self._take_session()
        if s is not None:
            self._ui.hide_pill()
            self._ui.hide_undo()
            if s.confirmed and not s.discarding:
                self._stop_session(s, confirmed=True, meeting=s.meeting, trim=False)
            else:
                reason = "declined" if s.discarding else "unanswered"
                holding.mark_discarded(s.directory, reason)
                self._prepare_stop(s, confirmed=False, meeting=s.meeting)
                directory = s.directory

                def stop_then_hold() -> None:
                    self._safe_stop(s)
                    self._stop_live(directory)
                    self._finisher.submit(
                        "hold",
                        finalize_and_hold(self._config, directory, reason=reason, end_s=None),
                    )

                self._spawn_stopper(stop_then_hold)
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

    def _prepare_stop(self, s: _Session, *, confirmed: bool, meeting: MeetingInfo) -> None:
        # Metadatos ANTES de parar: el grabador solo los guarda mientras graba.
        try:
            if confirmed:
                s.recorder.set_confirmed(True)
            s.recorder.set_meeting_info(
                title=(meeting.title or "").strip() or None, code=meeting.code
            )
        except Exception:
            log.exception("could not persist the final meeting info")

    def _stop_session(
        self, s: _Session, *, confirmed: bool, meeting: MeetingInfo, trim: bool
    ) -> None:
        self._prepare_stop(s, confirmed=confirmed, meeting=meeting)
        directory = s.directory
        db_path = s.db_path
        end_s = s.end_mark_s if trim else None
        if end_s is not None:  # el corte, en disco ANTES de parar el grabador y el vivo (P2-a)
            write_trim_marker(directory, end_s)

        def enqueued() -> None:
            if db_path is not None and self._on_job_queued_db is not None:
                self._on_job_queued_db(db_path)
            self._on_job_queued()

        def stop_then_queue() -> None:
            stopped = self._safe_stop(s)
            self._stop_live(directory)
            if not stopped:
                return
            # el indice de la carpeta donde se grabo, aunque la config ya apunte a otra (P1-4)
            self._finisher.submit(
                "finalize",
                finalize_and_enqueue(self._config, directory, on_enqueued=enqueued, end_s=end_s),
                db_path=db_path,
            )

        self._spawn_stopper(stop_then_queue)

    def _start_live(self, directory: Path) -> None:
        try:
            self._live.start(directory)
        except Exception:
            log.exception("could not start live processing")

    def _stop_live(self, directory: Path) -> None:
        """Espera a que el proceso en vivo salga (antes del recorte y el FLAC)."""
        try:
            self._live.stop(directory)
        except Exception:
            log.exception("could not stop live processing")

    @staticmethod
    def _safe_stop(s: _Session) -> bool:
        try:
            s.recorder.stop()
        except Exception:
            log.exception("stopping the recorder failed")
            return False
        if s.meeting.in_person:  # con el grabador parado ya nadie reescribe capture.json
            from micnaga.capture.session import apply_in_person

            apply_in_person(s.directory)
        return True

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
            if s is not None and not s.paused and not s.discarding:
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


__all__ = ["AppController", "default_recorder_factory", "foreground_hwnd"]
