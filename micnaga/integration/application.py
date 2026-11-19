"""``Application``: une UI, vigilante, captura, cola, tuberia de eventos y atajos.

Todo lo que toca Windows o el exterior es inyectable (``ui_factory``, ``watcher_factory``,
``hotkeys_factory``, ``recorder_factory``, ``spawn``...) para poder arrancar la app entera con
dobles en los tests. ``micnaga.app.run`` la construye con los valores reales.

Ciclo de vida: ``UiHost.run(app.start)`` -> ``start`` se llama en el hilo auxiliar de la UI cuando
las ventanas y la bandeja ya existen; ``shutdown`` (idempotente) para el vigilante, cierra la
grabacion en curso SIN descartarla, espera al cierre/encolado y para los hilos.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from micnaga import paths
from micnaga.config import Config
from micnaga.events import (
    Event,
    JobDiscarded,
    JobDone,
    JobFailed,
    JobProgress,
    JobStarted,
)
from micnaga.integration.controller import AppController
from micnaga.integration.events_server import EventsServer
from micnaga.integration.jobs import (
    DbFactory,
    Finisher,
    WorkerSupervisor,
    finalize_and_enqueue,
)
from micnaga.integration.levels import LevelMonitor
from micnaga.integration.library_ops import AppLibraryOps
from micnaga.integration.ports import RecorderFactory, UiPort
from micnaga.store.db import Database, open_database
from micnaga.ui.contracts import (
    Diagnostic,
    LibraryOps,
    ModelProgress,
    UiCallbacks,
)

if TYPE_CHECKING:
    from micnaga.hotkeys import HotkeyManager
    from micnaga.watcher import Watcher, WatcherController, WatcherState

log = logging.getLogger(__name__)

UiFactory = Callable[
    [
        UiCallbacks,
        LibraryOps,
        Callable[[Callable[[ModelProgress], None]], None],
        Callable[[], list[Diagnostic]],
    ],
    UiPort,
]
WatcherFactory = Callable[
    [Config, "WatcherController", Callable[["WatcherState"], None]], "Watcher"
]
HotkeysFactory = Callable[[Config, Callable[[], None], Callable[[], None]], "HotkeyManager"]

ENV_NO_AUTOSTART = "MICNAGA_NO_AUTOSTART"


def default_watcher_factory(
    config: Config, controller: WatcherController, on_state: Callable[[WatcherState], None]
) -> Watcher:
    from micnaga.watcher import Watcher

    return Watcher(config, controller, on_state_change=on_state)


def default_hotkeys_factory(
    config: Config, marker: Callable[[], None], private_pause: Callable[[], None]
) -> HotkeyManager:
    from micnaga.hotkeys import HotkeyManager

    return HotkeyManager.from_config(config.hotkeys, marker=marker, private_pause=private_pause)


def default_ui_factory(
    config: Config,
) -> UiFactory:
    def build(
        callbacks: UiCallbacks,
        ops: LibraryOps,
        download: Callable[[Callable[[ModelProgress], None]], None],
        diagnostics: Callable[[], list[Diagnostic]],
    ) -> UiPort:
        from micnaga.ui.host import UiHost

        return UiHost(
            config=config,
            callbacks=callbacks,
            library_ops=ops,
            download_models=download,
            diagnostics=diagnostics,
        )

    return build


def make_model_downloader(
    config: Callable[[], Config],
) -> Callable[[Callable[[ModelProgress], None]], None]:
    """Adapta ``micnaga.models.download_models`` al contrato del asistente (progreso por modelo)."""

    def download(callback: Callable[[ModelProgress], None]) -> None:
        from micnaga import models

        cfg = config()
        required = models.required_groups(cfg)
        got: dict[str, int] = {}

        def emit(gid: str, state: str) -> None:
            group = models.MANIFEST[gid]
            done = sum(min(got.get(f.rel_path, 0), f.size) for f in group.files)
            callback(
                ModelProgress(
                    name=group.title,
                    state=state,  # type: ignore[arg-type]
                    done=done,
                    total=group.total_size,
                )
            )

        for gid in required:
            emit(gid, "pending")

        def on_progress(p: models.DownloadProgress) -> None:
            gid = p.file.split("/", 1)[0]
            if gid not in models.MANIFEST:
                return
            got[p.file] = p.file_done
            group = models.MANIFEST[gid]
            complete = all(got.get(f.rel_path, -1) >= f.size for f in group.files)
            if p.phase == "verify":
                state = "verifying"
            elif complete and p.phase in ("done", "skip"):
                state = "verified"
            else:
                state = "downloading"
            emit(gid, state)

        models.download_models(cfg, on_progress)

    return download


class AppHeldOps:
    """``HeldOps`` de la biblioteca sobre ``micnaga.holding`` (raiz de la config de ahora)."""

    def __init__(self, app: Application) -> None:
        self._app = app

    def list_held(self) -> list[dict[str, Any]]:
        from micnaga import holding

        cfg = self._app.config
        root = paths.meetings_root(cfg)
        return [h.to_dict() for h in holding.list_held(root, cfg.storage.discarded_keep_days)]

    def recover_held(self, name: str) -> None:
        self._app.recover_held(name)

    def delete_held(self, name: str) -> None:
        from micnaga import holding

        if not holding.delete_held(paths.meetings_root(self._app.config), name):
            raise FileNotFoundError(name)


class Application:
    def __init__(
        self,
        config: Config,
        *,
        ui_factory: UiFactory,
        watcher_factory: WatcherFactory = default_watcher_factory,
        hotkeys_factory: HotkeysFactory = default_hotkeys_factory,
        recorder_factory: RecorderFactory | None = None,
        db_factory: DbFactory | None = None,
        spawn: Callable[[], Any] | None = None,
        is_worker_running: Callable[[], bool] | None = None,
        models_ready: Callable[[], bool] | None = None,
        events_name: str | None = None,
        level_monitor: LevelMonitor | None = None,
        supervisor_interval_s: float = 15.0,
        recover_at_boot: bool = True,
        start_events_server: bool = True,
        warmup: bool = True,
        start_watcher: bool = True,
        foreground: Callable[[], int | None] | None = None,
        process_start: float | None = None,
        exit_after_s: float = 0.0,
        brain: Any = None,
        live_title: Any = None,
    ) -> None:
        self.config = config
        try:  # solo congelada: el AUMID debe coincidir con el acceso directo del instalador
            from micnaga.ui.toast import set_process_aumid

            set_process_aumid()
        except Exception:
            log.exception("could not set the process AppUserModelID")
        self._db_factory: DbFactory = db_factory or self._open_db
        self._recover_at_boot = recover_at_boot
        self._start_events_server = start_events_server
        self._warmup = warmup
        self._start_watcher = start_watcher
        self._models_ready_fn = models_ready or self._default_models_ready
        self._process_start = process_start
        self._exit_after_s = exit_after_s
        self._stopped = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._active_jobs: set[int] = set()
        self._db_busy = False
        self._monitor = level_monitor or LevelMonitor()
        self.library_ops: LibraryOps = AppLibraryOps(lambda: self.config)
        self.hotkey_results: dict[str, Any] = {}
        self.tray_ready_s: float | None = None
        self.last_job_error: str | None = None
        self.boot_queued: list[Path] = []

        self.callbacks = self._build_callbacks()
        self.ui: UiPort = ui_factory(
            self.callbacks,
            self.library_ops,
            make_model_downloader(lambda: self.config),
            self.diagnostics,
        )
        self.finisher = Finisher(self._db_factory, db_key=self._db_key)
        self.supervisor = WorkerSupervisor(
            config,
            db_factory=self._db_factory,
            db_key=self._db_key,
            spawn=spawn,
            is_running=is_worker_running,
            models_ready=self._models_ready_fn,
            on_busy=self._on_busy,
            interval_s=supervisor_interval_s,
        )
        ctl_kwargs: dict[str, Any] = {}
        if foreground is not None:
            ctl_kwargs["foreground"] = foreground
        self.controller = AppController(
            config,
            self.ui,
            self.finisher,
            recorder_factory=recorder_factory,
            on_job_queued=self.supervisor.kick,
            **ctl_kwargs,
        )
        self.ui.set_levels_provider(self._levels)
        self.watcher = watcher_factory(config, self.controller, self._on_watcher_state)
        self.controller.bind_watcher(self.watcher)
        self.held_ops = AppHeldOps(self)
        set_held = getattr(self.ui, "set_held_ops", None)
        if callable(set_held):
            set_held(self.held_ops)
        self.hotkeys = hotkeys_factory(config, self._marker, self.watcher.toggle_private_pause)
        self.events_server = EventsServer(self.handle_event, events_name)
        # Secretaria (capa de IA, opcional: inerte con secretaria.enabled = false).
        self.brain = brain if brain is not None else self._make_brain()
        setter = getattr(self.ui, "set_brain", None)
        if self.brain is not None and setter is not None:
            setter(self.brain)
        self.live_title = live_title if live_title is not None else self._make_live_title()
        self.google_context = self._make_google_context()

    def _make_brain(self) -> Any:
        try:
            from micnaga.brain.service import BrainService

            return BrainService(lambda: self.config, db_factory=self._db_factory)
        except Exception:
            log.exception("secretaria unavailable")
            return None

    def _make_live_title(self) -> Any:
        try:
            from micnaga.brain.live_title import LiveTitler

            def on_title(title: str) -> None:
                setter = getattr(self.ui, "set_pill_title", None)
                if setter is not None:
                    setter(title)

            return LiveTitler(lambda: self.config, lambda: self.controller.session_dir, on_title)
        except Exception:
            log.exception("live title unavailable")
            return None

    def _make_google_context(self) -> Any:
        """Evento de Google Calendar en curso al grabar (inerte si Calendar no esta activado)."""
        try:
            from micnaga.brain.google.context import GoogleContextWatcher

            def on_title(title: str) -> None:
                setter = getattr(self.ui, "set_pill_title", None)
                if setter is not None:
                    setter(title)

            return GoogleContextWatcher(
                lambda: self.config, lambda: self.controller.session_dir, on_title
            )
        except Exception:
            log.exception("google context unavailable")
            return None

    def _db_key(self) -> object:
        """Ruta del indice segun la config actual: si cambia, Finisher y supervisor reabren."""
        try:
            return str(paths.db_path(self.config))
        except Exception:
            return None

    def _open_db(self) -> Database:
        """Abre el indice con reintentos: la primera apertura (crear esquema / pasar a WAL) de
        dos hilos a la vez puede dar ``database is locked``."""
        for attempt in range(5):
            try:
                return open_database(self.config)
            except sqlite3.OperationalError:
                if attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ ciclo de vida

    def start(self, _host: object = None) -> None:
        """Arranca todo. Se llama (hilo auxiliar de la UI) con la bandeja ya visible."""
        if self._process_start is not None:
            self.tray_ready_s = time.time() - self._process_start
            log.info("tray ready %.2f s after process start", self.tray_ready_s)
        try:
            self._db_factory().close()  # crea esquema/WAL antes de que lo abran varios hilos
        except Exception:
            log.exception("could not open the jobs database")
        self._ensure_autostart()
        if self._warmup:
            threading.Thread(target=self._warm_up, name="micnaga-warmup", daemon=True).start()
        if self._start_events_server:
            self.events_server.start()
        self.supervisor.start()
        if self._recover_at_boot:
            threading.Thread(target=self._boot_recovery, name="micnaga-boot", daemon=True).start()
        self.hotkey_results = self.hotkeys.start()
        for name, result in self.hotkey_results.items():
            if not getattr(result, "registered", True):
                log.warning("hotkey %s unavailable: %s", name, getattr(result, "error", ""))
        if self._start_watcher:
            self.watcher.start()
        for extra in (self.brain, self.live_title, self.google_context):
            if extra is not None:
                try:
                    extra.start()
                except Exception:
                    log.exception("could not start %s", type(extra).__name__)
        if self._exit_after_s > 0:
            timer = threading.Timer(self._exit_after_s, self.request_quit)
            timer.daemon = True
            timer.start()
        if self._models_missing():
            log.info("models missing: opening the first-run wizard")
            self.ui.open_wizard()

    def shutdown(self, *, wait_s: float = 120.0, reason: str = "unspecified") -> None:
        """Para el vigilante, cierra la grabacion en curso (sin descartarla) y los hilos.

        ``reason`` queda en el log (``tray_quit``, ``exit_after``, ``console_close``,
        ``gui_loop_ended``...): toda salida dice por que salio."""
        with self._shutdown_lock:
            if self._stopped.is_set():
                return
            self._stopped.set()
        log.info("shutting down (reason: %s)", reason)
        for step in (
            self.hotkeys.stop,
            self.watcher.stop,
            lambda: self.controller.shutdown(wait_s=wait_s),
            self._monitor.stop,
            lambda: self.finisher.stop(5.0),
            self.supervisor.stop,
            self.events_server.stop,
            *(x.stop for x in (self.google_context, self.live_title, self.brain) if x is not None),
        ):
            try:
                step()
            except Exception:
                log.exception("shutdown step failed")

    def request_quit(self, reason: str = "exit_after") -> None:
        """Salir como el «Salir» de la bandeja (para ``--exit-after`` y pruebas)."""
        self.shutdown(reason=reason)
        self.ui.quit(reason)

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    # ------------------------------------------------------------------ UI -> app

    def _build_callbacks(self) -> UiCallbacks:
        return UiCallbacks(
            on_popup_decision=self._on_popup_decision,
            on_popup_hover=lambda h: self.watcher.popup_hover(h),
            on_end_decision=lambda c: self.watcher.end_answer(c),
            on_undo=lambda: self.watcher.undo(),
            on_marker=self._marker,
            on_pause_toggle=lambda p: self.watcher.set_private_pause(p),
            on_stop=lambda: self.watcher.user_stop(),
            on_record_now=lambda: self.watcher.record_now(),
            on_pause_watching=lambda minutes: self.watcher.suspend(minutes * 60.0),
            on_resume_watching=lambda: self.watcher.resume_watching(),
            on_quit=lambda: self.shutdown(reason="tray_quit"),
            on_settings_saved=self._on_settings_saved,
            on_first_run_done=self._on_first_run_done,
            on_levels_monitor=self._on_levels_monitor,
        )

    def _marker(self) -> None:
        self.controller.add_marker()

    def _on_popup_decision(self, decision: str) -> None:
        # La cuenta atras es la del vigilante: el "timeout" de la UI se ignora.
        if decision == "record":
            self.watcher.popup_answer(True)
        elif decision == "dismiss":
            self.watcher.popup_answer(False)

    def _on_settings_saved(self, cfg: Any) -> None:
        if not isinstance(cfg, Config):
            return
        self.config = cfg
        self.controller.set_config(cfg)
        # Los atajos se vuelven a registrar; el resto (deteccion...) rige al reiniciar la app.
        try:
            self.hotkeys.stop()
            self.hotkeys = default_hotkeys_factory(
                cfg, self._marker, self.watcher.toggle_private_pause
            )
            self.hotkeys.start()
        except Exception:
            log.exception("could not re-register hotkeys")

    def _on_first_run_done(self) -> None:
        # El arranque con Windows ya lo deja activo el instalador y el asistente no lo pregunta:
        # aqui NO se toca el registro (ver ``_ensure_autostart``).
        self.supervisor.kick()

    def _ensure_autostart(self) -> None:
        """Arranque con Windows por defecto ACTIVO. Solo en la app instalada (congelada): si la
        config lo quiere (``startup.run_at_login``, que cambia Ajustes) y el valor ``Run`` falta o
        apunta a otro sitio (instalacion movida, limpiador de registro...), se reescribe con el
        mismo nombre y comando que el instalador."""
        if os.environ.get(ENV_NO_AUTOSTART) or not self.config.startup.run_at_login:
            return
        try:
            from micnaga.ui import startup
            from micnaga.ui.toast import is_frozen

            if not is_frozen():
                return
            if startup.ensure_run_at_login():
                log.info("run-at-login value was missing or stale; rewritten")
        except Exception:
            log.exception("could not ensure run-at-login")

    def _on_levels_monitor(self, active: bool) -> None:
        if active and not self.controller.recording:
            self._monitor.start()
        else:
            self._monitor.stop()

    def _levels(self) -> tuple[float, float]:
        if self.controller.recording:
            return self.controller.levels()
        return self._monitor.levels()

    def _on_watcher_state(self, state: WatcherState) -> None:
        self.ui.set_watching_paused(state.value == "suspended")

    # ------------------------------------------------------------------ worker -> UI

    def _on_busy(self, busy: bool) -> None:
        self._db_busy = busy
        self._refresh_processing()

    def _refresh_processing(self) -> None:
        self.ui.set_processing(self._db_busy or bool(self._active_jobs))

    def handle_event(self, event: Event) -> None:
        """Evento de la tuberia del worker (hilo del servidor)."""
        if isinstance(event, JobStarted):
            self._active_jobs.add(event.job_id)
            self._refresh_processing()
        elif isinstance(event, JobProgress):
            return
        elif isinstance(event, JobDone):
            self._active_jobs.discard(event.job_id)
            self._refresh_processing()
            folder = Path(event.session_dir).name
            if self.brain is not None:
                # La Secretaria decide cuando sale el aviso (con «· N propuestas»).
                self.brain.on_job_done(
                    folder, event.title, event.duration_s, event.n_speakers, self._notify_ready
                )
            else:
                self.ui.notify_ready(folder, event.title, event.duration_s, event.n_speakers)
            self.supervisor.kick()
        elif isinstance(event, JobFailed):
            self._active_jobs.discard(event.job_id)
            self._refresh_processing()
            self.last_job_error = event.error
            log.warning("job %d failed: %s", event.job_id, event.error)
            self.supervisor.kick()
        elif isinstance(event, JobDiscarded):
            self._active_jobs.discard(event.job_id)
            self._refresh_processing()
            log.info("job %d discarded: %s", event.job_id, event.reason)
            self.supervisor.kick()

    def _notify_ready(
        self, folder: str, title: str, duration_s: float, speakers: int, proposals: int
    ) -> None:
        if proposals:
            self.ui.notify_ready(folder, title, duration_s, speakers, proposals)
        else:
            self.ui.notify_ready(folder, title, duration_s, speakers)

    # ------------------------------------------------------------------ hilos de arranque

    def _warm_up(self) -> None:
        """Precarga lo pesado fuera del camino critico de la bandeja (el primer popup < 3 s)."""
        try:
            import numpy  # noqa: F401

            import micnaga.capture  # noqa: F401
            from micnaga.capture import wasapi  # noqa: F401
        except Exception:
            log.debug("warm-up import failed", exc_info=True)

    def recover_held(self, name: str) -> None:
        """«Recuperar» de la biblioteca: vuelve a la raiz, confirmada, y se encola."""
        from micnaga import holding

        root = paths.meetings_root(self.config)
        session = holding.restore_held(root, name)
        self.finisher.submit(
            "recover",
            finalize_and_enqueue(self.config, session, on_enqueued=self.supervisor.kick),
        )

    def _boot_recovery(self) -> None:
        try:
            from micnaga.integration.recovery import recover_and_enqueue

            db = self._db_factory()
            try:
                self.boot_queued = recover_and_enqueue(self.config, db)
            finally:
                db.close()
            if self.boot_queued:
                self.supervisor.kick()
        except Exception:
            log.exception("boot recovery failed")

    def _default_models_ready(self) -> bool:
        try:
            from micnaga.models import missing_models

            return not missing_models(self.config)
        except Exception:
            log.exception("could not check models")
            return True

    def _models_missing(self) -> bool:
        return not self._models_ready_fn()

    # ------------------------------------------------------------------ diagnostico

    def diagnostics(self) -> list[Diagnostic]:
        items: list[Diagnostic] = []
        try:
            info = self.watcher.diagnostics()
            items.append(Diagnostic("Vigilante", "ok", str(info.get("state", ""))))
            if info.get("last_error"):
                items.append(Diagnostic("Sondeo", "warn", str(info["last_error"])))
        except Exception:
            items.append(Diagnostic("Vigilante", "fail", ""))
        for name, result in self.hotkey_results.items():
            ok = bool(getattr(result, "registered", False))
            items.append(
                Diagnostic(
                    f"Atajo {name}",
                    "ok" if ok else "warn",
                    str(getattr(result, "spec", "")) if ok else str(getattr(result, "error", "")),
                )
            )
        missing = self._models_missing()
        items.append(Diagnostic("Modelos", "warn" if missing else "ok", ""))
        items.append(
            Diagnostic(
                "Procesado", "warn" if self.last_job_error else "ok", self.last_job_error or ""
            )
        )
        items.append(Diagnostic("Carpeta", "ok", str(paths.meetings_root(self.config))))
        sec = self.config.secretaria
        items.append(
            Diagnostic(
                "Secretaria", "ok" if sec.enabled else "off", sec.model_acta if sec.enabled else ""
            )
        )
        return items
