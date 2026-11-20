"""Cola de trabajos desde la app: cierre de sesiones (FLAC + SQLite) y supervisor del worker.

* ``Finisher``: un hilo serie. Recibe tareas ``fn(db)`` (convertir a FLAC, encolar, apartar
  sesiones descartadas) con una ``Database`` abierta **dentro de su hilo** (SQLite no comparte
  conexiones entre hilos). Los controladores del vigilante deben volver rapido, asi que nada de
  esto corre en su hilo.
* Cambio de carpeta de reuniones (hallazgo 3): ``Finisher`` y ``WorkerSupervisor`` reciben
  ``db_key()`` (la ruta del indice segun la config de ahora) y reabren la conexion cuando cambia;
  si no, seguirian encolando/vigilando el indice de la carpeta antigua mientras el worker nuevo
  lee el de la nueva y las transcripciones se quedarian paradas. Los trabajos que quedaran
  pendientes en el indice antiguo no se mueven (la carpeta antigua sigue siendo valida si se
  vuelve a ella).
* Una sesion conserva su carpeta **y su indice** hasta terminar el procesado (hallazgo P1-4):
  ``submit(..., db_path=)`` ejecuta la tarea contra el indice de la carpeta donde se grabo, no
  contra el de la config de ahora, y ``WorkerSupervisor.watch`` vigila ese indice y lanza el
  worker sobre esa carpeta (``MICNAGA_STORAGE``) hasta vaciarlo. Antes el audio quedaba en A y el
  trabajo/indice en B: la biblioteca de B la listaba pero no la abria.
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
DbKey = Callable[[], object]
Task = Callable[[Database], None]
PathDbFactory = Callable[[Path], Database]


def open_database_at(path: Path) -> Database:
    """Abre el indice de una carpeta concreta (creandola); reintenta si esta bloqueado."""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(5):
        try:
            return Database(path)
        except sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))
    raise AssertionError("unreachable")  # pragma: no cover


class Finisher:
    """Hilo serie de tareas de cierre."""

    def __init__(
        self,
        db_factory: DbFactory,
        *,
        db_key: DbKey | None = None,
        path_factory: PathDbFactory | None = None,
    ) -> None:
        self._db_factory = db_factory
        self._db_key = db_key or (lambda: None)
        self._path_factory = path_factory or open_database_at
        self._queue: queue.Queue[tuple[str, Task, Path | None] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._pending = 0

    def submit(self, label: str, task: Task, *, db_path: Path | None = None) -> None:
        """Encola ``task``. Con ``db_path`` corre contra ese indice (el de la carpeta de la
        sesion) aunque la config apunte ya a otra carpeta."""
        with self._lock:
            self._pending += 1
            self._idle.clear()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="micnaga-finisher", daemon=True
                )
                self._thread.start()
        self._queue.put((label, task, db_path))

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
        key: object = None
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                label, task, db_path = item
                try:
                    wanted = self._db_key()
                    if db_path is not None and str(db_path) != wanted:
                        own = self._path_factory(db_path)  # indice de la carpeta de la sesion
                        try:
                            task(own)
                        finally:
                            with contextlib.suppress(Exception):
                                own.close()
                        continue
                    if db is not None and wanted != key:
                        log.info("meetings folder changed: reopening the jobs index")
                        with contextlib.suppress(Exception):
                            db.close()
                        db = None
                    if db is None:
                        db = self._db_factory()
                        key = wanted
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


# ----------------------------------------------------------------------------- recorte

TRIM_MARKER = ".micnaga-trim.json"
"""Punto de corte (fin real de la reunion) escrito con ``fsync`` **antes** de parar el grabador
(hallazgo P2-a). ``capture.json`` no sirve hasta que el grabador para: lo reescribe entero en cada
escritura. Si la app muere entre la parada y el cierre, el arranque lo aplica antes del FLAC (sin
el, encolaba tambien la cola grabada tras el fin de la reunion)."""


def write_trim_marker(session_dir: Path, end_s: float) -> bool:
    import json
    import os

    path = session_dir / TRIM_MARKER
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"end_s": float(end_s)}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        log.exception("could not persist the trim point")
        return False


def read_trim_marker(session_dir: Path) -> float | None:
    import json

    try:
        data = json.loads((session_dir / TRIM_MARKER).read_text(encoding="utf-8"))
        value = data.get("end_s") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)


def apply_trim_marker(session_dir: Path) -> float | None:
    """Pasa el corte persistido a ``capture.json`` (lo recorta ``finalize_session``)."""
    end_s = read_trim_marker(session_dir)
    if end_s is None:
        return None
    from micnaga.capture.recovery import set_trim

    set_trim(session_dir, end_s)
    return end_s


def drop_trim_marker(session_dir: Path) -> None:
    with contextlib.suppress(OSError):
        (session_dir / TRIM_MARKER).unlink()


# ----------------------------------------------------------------------------- tareas


def finalize_and_enqueue(
    config: Config,
    session_dir: Path,
    *,
    on_enqueued: Callable[[], None],
    end_s: float | None = None,
) -> Task:
    """Tarea: (recorte en ``end_s``) WAV -> FLAC y trabajo ``pending``. Si la conversion falla
    se encola igualmente (el pipeline sabe convertir los WAV): una grabacion nunca se pierde."""

    def task(db: Database) -> None:
        from micnaga.capture import finalize_session

        if end_s is not None:
            from micnaga.capture.recovery import set_trim

            set_trim(session_dir, end_s)
        try:
            finalize_session(session_dir, flac_level=config.capture.flac_level)
            drop_trim_marker(session_dir)
        except Exception:
            log.exception("finalize failed for a session; queueing it anyway")
        job = db.enqueue_job(session_dir)
        log.info("job %d queued", job.id)
        on_enqueued()

    return task


def finalize_and_hold(
    config: Config, session_dir: Path, *, reason: str, end_s: float | None = None
) -> Task:
    """Tarea: (recorte) WAV -> FLAC y apartar en «Descartadas» (no se encola). Si algo falla la
    carpeta queda marcada y el siguiente arranque la aparta: nunca se transcribe ni se pierde."""

    def task(_db: Database) -> None:
        from micnaga import holding
        from micnaga.capture import finalize_session

        if end_s is not None:
            from micnaga.capture.recovery import set_trim

            set_trim(session_dir, end_s)
        try:
            finalize_session(session_dir, flac_level=config.capture.flac_level)
            drop_trim_marker(session_dir)
        except Exception:
            log.exception("finalize failed for a held session; holding the WAV files")
        holding.move_to_held(session_dir, reason)

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
        db_key: DbKey | None = None,
        spawn: Callable[[], subprocess.Popen[bytes] | None] | None = None,
        spawn_for: Callable[[Path], subprocess.Popen[bytes] | None] | None = None,
        path_factory: PathDbFactory | None = None,
        is_running: Callable[[], bool] | None = None,
        models_ready: Callable[[], bool] | None = None,
        on_busy: Callable[[bool], None] | None = None,
        interval_s: float = 15.0,
        min_respawn_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._db_factory = db_factory or (lambda: open_database(config))
        self._db_key = db_key or (lambda: None)
        self._spawn = spawn or _default_spawn
        self._spawn_for = spawn_for or _default_spawn_for
        self._path_factory = path_factory or open_database_at
        self._watched: dict[str, Path] = {}  # indice de otra carpeta con trabajos -> su raiz
        self._watch_lock = threading.Lock()
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

    def watch(self, db_path: Path) -> None:
        """Vigila tambien el indice de otra carpeta (una sesion grabada antes de cambiarla)."""
        if str(db_path) != self._db_key():
            with self._watch_lock:
                self._watched[str(db_path)] = db_path.parent
        self._wake.set()

    def _watched_busy_root(self) -> Path | None:
        """Raiz de la primera carpeta vigilada con trabajos pendientes (las vacias se olvidan)."""
        current = self._db_key()
        with self._watch_lock:
            items = list(self._watched.items())
        for key, root in items:
            busy = False
            if key != current:
                try:
                    other = self._path_factory(Path(key))
                    try:
                        counts = other.count_jobs()
                    finally:
                        other.close()
                    busy = counts[JobState.PENDING] + counts[JobState.PROCESSING] > 0
                except Exception:
                    log.warning("could not read the jobs of a previous meetings folder")
                    continue
            if busy:
                return root
            with self._watch_lock:
                self._watched.pop(key, None)
        return None

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(5.0)
        self._thread = None

    def _run(self) -> None:
        db: Database | None = None
        key: object = None
        try:
            while not self._stop.is_set():
                try:
                    wanted = self._db_key()
                    if db is not None and wanted != key:
                        with contextlib.suppress(Exception):
                            db.close()
                        db = None
                    if db is None:
                        db = self._db_factory()
                        key = wanted
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
        other = None if busy else self._watched_busy_root()
        if self._on_busy is not None:
            self._on_busy(busy or other is not None)
        if not (busy or other is not None) or not self._models_ready():
            return
        if self._is_running():
            return
        now = self._clock()
        if now < self._next_spawn:
            return
        try:
            proc = self._spawn() if other is None else self._spawn_for(other)
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


def _default_spawn_for(root: Path) -> subprocess.Popen[bytes] | None:
    """Worker sobre otra carpeta de reuniones (su indice y sus sesiones)."""
    from micnaga import paths
    from micnaga.pipeline.worker import spawn_worker

    return spawn_worker(("--once",), env={paths.ENV_STORAGE: str(root)})


def _default_is_running() -> bool:
    from micnaga.pipeline.worker import is_worker_running

    return is_worker_running()


def _models_ready(config: Config) -> bool:
    from micnaga.models import missing_models

    return not missing_models(config)
