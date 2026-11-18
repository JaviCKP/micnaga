"""Cola de trabajos desde la app: cierre de sesiones (FLAC + SQLite) y supervisor del worker.

* ``Finisher``: un hilo serie. Recibe tareas ``fn(db)`` (convertir a FLAC, encolar, borrar
  sesiones descartadas) con una ``Database`` abierta **dentro de su hilo** (SQLite no comparte
  conexiones entre hilos). Los controladores del vigilante deben volver rapido, asi que nada de
  esto corre en su hilo.
* ``WorkerSupervisor``: mantiene vivo el proceso de trabajo mientras haya trabajos pendientes. El
  worker lanzado por la app sale al vaciar la cola (``--once``: cada tanda empieza en un proceso
  limpio, asi la memoria de torch/diarize se devuelve al sistema); si un trabajo se encola justo
  cuando el worker esta saliendo, ``spawn_worker`` devuelve ``None`` y el supervisor lo relanza
  cuando el proceso termina. Con modelos sin descargar no lanza nada.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from micnaga.config import Config
from micnaga.store.db import Database, JobState, open_database

log = logging.getLogger(__name__)

DbFactory = Callable[[], Database]
Task = Callable[[Database], None]


class Finisher:
    """Hilo serie de tareas de cierre."""

    def __init__(self, db_factory: DbFactory) -> None:
        self._db_factory = db_factory
        self._queue: queue.Queue[tuple[str, Task] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._pending = 0

    def submit(self, label: str, task: Task) -> None:
        with self._lock:
            self._pending += 1
            self._idle.clear()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="micnaga-finisher", daemon=True
                )
                self._thread.start()
        self._queue.put((label, task))

    def wait_idle(self, timeout_s: float | None = None) -> bool:
        return self._idle.wait(timeout_s)

    def stop(self, timeout_s: float = 60.0) -> bool:
        """Espera a las tareas pendientes y para el hilo. ``False`` si no terminaron a tiempo."""
        done = self._idle.wait(timeout_s)
        self._queue.put(None)
        thread = self._thread
        if thread is not None:
            thread.join(2.0)
        return done

    def _run(self) -> None:
        db: Database | None = None
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                label, task = item
                try:
                    if db is None:
                        db = self._db_factory()
                    task(db)
                except Exception:
                    log.exception("finisher task %r failed", label)
                finally:
                    with self._lock:
                        self._pending = max(0, self._pending - 1)
                        if self._pending == 0:
                            self._idle.set()
        finally:
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()


# ----------------------------------------------------------------------------- tareas


def finalize_and_enqueue(
    config: Config, session_dir: Path, *, on_enqueued: Callable[[], None]
) -> Task:
    """Tarea: WAV -> FLAC y trabajo ``pending``. Si la conversion falla se encola igualmente
    (el pipeline sabe convertir los WAV): una grabacion nunca se pierde por esto."""

    def task(db: Database) -> None:
        from micnaga.capture import finalize_session

        try:
            finalize_session(session_dir, flac_level=config.capture.flac_level)
        except Exception:
            log.exception("finalize failed for a session; queueing it anyway")
        job = db.enqueue_job(session_dir)
        log.info("job %d queued", job.id)
        on_enqueued()

    return task


def delete_session_dir(session_dir: Path) -> None:
    """Borra la carpeta de una sesion descartada (Windows puede tenerla bloqueada un instante)."""
    for _ in range(10):
        try:
            shutil.rmtree(session_dir)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.2)
    shutil.rmtree(session_dir, ignore_errors=True)
    if session_dir.exists():
        log.warning("could not fully delete a discarded session folder")


# ----------------------------------------------------------------------------- worker


class WorkerSupervisor:
    def __init__(
        self,
        config: Config,
        *,
        db_factory: DbFactory | None = None,
        spawn: Callable[[], subprocess.Popen[bytes] | None] | None = None,
        is_running: Callable[[], bool] | None = None,
        models_ready: Callable[[], bool] | None = None,
        on_busy: Callable[[bool], None] | None = None,
        interval_s: float = 15.0,
        min_respawn_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._db_factory = db_factory or (lambda: open_database(config))
        self._spawn = spawn or _default_spawn
        self._is_running = is_running or _default_is_running
        self._models_ready = models_ready or (lambda: _models_ready(config))
        self._on_busy = on_busy
        self._interval_s = interval_s
        self._min_respawn_s = min_respawn_s
        self._clock = clock
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._spawned_at = 0.0
        self._next_spawn = 0.0
        self._fail_streak = 0
        self.spawn_count = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="micnaga-worker-sup", daemon=True)
        self._thread.start()

    def kick(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(5.0)
        self._thread = None

    def _run(self) -> None:
        db: Database | None = None
        try:
            while not self._stop.is_set():
                try:
                    if db is None:
                        db = self._db_factory()
                    self.tick(db)
                except Exception:
                    log.exception("worker supervisor tick failed")
                # con un worker vivo se mira mas a menudo (para relanzarlo si salio con trabajo)
                self._wake.wait(3.0 if self._proc is not None else self._interval_s)
                self._wake.clear()
        finally:
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()

    def tick(self, db: Database) -> None:
        """Un paso (publico para los tests): ¿hay trabajo y no hay worker? Lanzarlo."""
        self._reap()
        counts = db.count_jobs()
        busy = counts[JobState.PENDING] + counts[JobState.PROCESSING] > 0
        if self._on_busy is not None:
            self._on_busy(busy)
        if not busy or not self._models_ready():
            return
        if self._is_running():
            return
        now = self._clock()
        if now < self._next_spawn:
            return
        try:
            proc = self._spawn()
        except Exception:
            log.exception("could not start the worker")
            self._next_spawn = now + 60.0
            return
        self._next_spawn = now + self._min_respawn_s
        if proc is not None:
            self._proc, self._spawned_at = proc, now
            self.spawn_count += 1
            log.info("worker started (pid %s)", proc.pid)

    def _reap(self) -> None:
        proc = self._proc
        if proc is None:
            return
        code = proc.poll()
        if code is None:
            return
        self._proc = None
        quick = self._clock() - self._spawned_at < 20.0
        if code != 0 and quick:
            self._fail_streak += 1
            delay = min(300.0, 15.0 * 2**self._fail_streak)
            self._next_spawn = self._clock() + delay
            log.warning(
                "worker exited with code %s right after start; retry in %.0f s", code, delay
            )
        else:
            self._fail_streak = 0
            if code != 0:
                log.warning("worker exited with code %s", code)
            # termino (--once): si quedan trabajos, el siguiente tick lo relanza
            self._wake.set()


def _default_spawn() -> subprocess.Popen[bytes] | None:
    from micnaga.pipeline.worker import spawn_worker

    return spawn_worker(("--once",))


def _default_is_running() -> bool:
    from micnaga.pipeline.worker import is_worker_running

    return is_worker_running()


def _models_ready(config: Config) -> bool:
    from micnaga.models import missing_models

    return not missing_models(config)
