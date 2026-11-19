"""Proceso de trabajo (MIC-08, spec 4 y 8): consume la cola de procesado en segundo plano.

Es un proceso **aparte** de la app (``micnaga worker``), de modo que un fallo en el procesado nunca
tumba la grabacion. Este modulo es ligero al importarse (nada de numpy/onnxruntime a nivel de
modulo): las variables de hilos (``OMP_NUM_THREADS``...) deben fijarse **antes** de importar el
runner y los motores (``configure_process``).

* ``configure_process(config)``: limita hilos y baja la prioridad a ``BELOW_NORMAL_PRIORITY_CLASS``.
* ``WorkerLock``: mutex con nombre (derivado de ``MICNAGA_HOME``, como la tuberia):
  ``requeue_stale`` supone un unico worker, asi que un segundo ``micnaga worker`` sale sin hacer
  nada.
* ``PipeEventSink``: cliente de la tuberia de eventos (``events.pipe_name()``). Escribe desde un
  hilo propio con cola acotada: si la UI no escucha, esta bloqueada o la escritura falla, los
  eventos se **descartan en silencio** y el worker nunca espera.
* ``run_worker_loop``: ``requeue_stale`` al empezar, y despues reclama trabajos de uno en uno y los
  ejecuta con ``PipelineRunner``; con ``once=True`` sale cuando no quedan pendientes.
* ``spawn_worker()``: lanza el worker como proceso hijo separado (sin ventana, prioridad baja).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from micnaga import paths
from micnaga.events import Event, encode_line, pipe_name

if TYPE_CHECKING:
    from micnaga.config import Config
    from micnaga.pipeline.runner import DepsSource
    from micnaga.store.db import Database

log = logging.getLogger(__name__)

THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
ERROR_ALREADY_EXISTS = 183


# ----------------------------------------------------------------------------- proceso


def apply_thread_limits(threads: int) -> None:
    """Fija las variables de entorno de hilos (llamar antes de importar numpy/onnxruntime)."""
    value = str(max(1, threads))
    for var in THREAD_ENV_VARS:
        os.environ[var] = value
    os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")


def set_low_priority() -> bool:
    """``BELOW_NORMAL_PRIORITY_CLASS`` para este proceso. ``False`` si no se pudo."""
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        return True
    except Exception as exc:
        log.warning("could not lower process priority: %s", exc)
        return False


LIVE_MAX_THREADS = 2


def live_threads(config: Config) -> int:
    """Hilos del proceso en vivo: pocos (corre durante la reunion, al lado de la captura)."""
    return max(1, min(LIVE_MAX_THREADS, config.processing.threads // 2))


def configure_process(config: Config) -> None:
    apply_thread_limits(config.processing.threads)
    if config.processing.low_priority:
        set_low_priority()


# ----------------------------------------------------------------------------- unico worker


def _mutex_name() -> str:
    digest = hashlib.sha1(str(paths.app_data_dir()).lower().encode("utf-8")).hexdigest()[:10]
    return f"Local\\micnaga-worker-{digest}"


class NamedMutex:
    """Mutex con nombre: ``acquire()`` es ``False`` si otro proceso ya lo tiene."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._handle: Any = None

    def acquire(self) -> bool:
        import win32api
        import win32event

        handle = win32event.CreateMutex(None, True, self._name)  # type: ignore[arg-type]
        if win32api.GetLastError() == ERROR_ALREADY_EXISTS:
            handle.Close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(Exception):
                self._handle.Close()
            self._handle = None

    def __enter__(self) -> NamedMutex:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class WorkerLock(NamedMutex):
    """Mutex del worker: ``acquire()`` es ``False`` si ya hay otro worker."""

    def __init__(self) -> None:
        super().__init__(_mutex_name())


def mutex_exists(name: str) -> bool:
    try:
        import win32api
        import win32event

        handle = win32event.OpenMutex(win32event.SYNCHRONIZE, False, name)
    except Exception:
        return False
    win32api.CloseHandle(handle)
    return True


def is_worker_running() -> bool:
    """``True`` si existe el mutex del worker (otro proceso lo tiene)."""
    try:
        import win32api
        import win32event

        handle = win32event.OpenMutex(win32event.SYNCHRONIZE, False, _mutex_name())
    except Exception:
        return False
    win32api.CloseHandle(handle)
    return True


# ----------------------------------------------------------------------------- eventos


class PipeEventSink:
    """Envia eventos a la UI por la tuberia con nombre; los descarta si nadie escucha."""

    def __init__(self, name: str | None = None, *, max_queue: int = 256) -> None:
        self._name = name or pipe_name()
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=max_queue)
        self._thread = threading.Thread(target=self._run, name="micnaga-events", daemon=True)
        self._thread.start()

    def __call__(self, event: Event) -> None:
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(encode_line(event))

    def close(self, timeout: float = 1.0) -> None:
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        handle: Any = None
        next_try = 0.0
        while True:
            data = self._queue.get()
            if data is None:
                break
            if handle is None:
                if time.monotonic() < next_try:
                    continue
                try:
                    handle = open(self._name, "wb", buffering=0)  # noqa: SIM115
                except OSError:
                    next_try = time.monotonic() + 2.0
                    continue
            try:
                handle.write(data)
            except OSError:
                with contextlib.suppress(OSError):
                    handle.close()
                handle = None
                next_try = time.monotonic() + 2.0
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()


# ----------------------------------------------------------------------------- bucle


def run_worker_loop(
    config: Config,
    db: Database,
    *,
    once: bool = False,
    poll_interval_s: float = 2.0,
    deps: DepsSource = None,
    emit: Callable[[Event], None] | None = None,
    stop: threading.Event | None = None,
    recover: bool = True,
    keep_work: bool = False,
) -> int:
    """Procesa la cola. Devuelve cuantos trabajos se atendieron (``done``/``discarded``/``failed``).

    ``stop`` permite pedir la salida entre trabajos (el trabajo en curso termina).
    """
    from micnaga.pipeline.runner import PipelineRunner

    stop = stop or threading.Event()
    if recover:
        for stale in db.requeue_stale():
            log.info(
                "recovered job %d (state=%s, stage=%d)", stale.id, stale.state.value, stale.stage
            )
    runner = PipelineRunner(config, deps=deps, db=db, emit=emit, keep_work=keep_work)
    handled = 0
    while not stop.is_set():
        job = db.claim_next_job()
        if job is None:
            if once:
                break
            stop.wait(poll_interval_s)
            continue
        log.info("processing job %d (attempt %d)", job.id, job.attempts)
        try:
            result = runner.run(Path(job.session_dir), job)
            log.info("job %d finished: %s", job.id, result.status)
        except Exception as exc:  # un fallo imprevisto no debe parar la cola
            log.exception("unexpected error in job %d", job.id)
            with contextlib.suppress(Exception):
                db.fail_job(job.id, f"{type(exc).__name__}: {exc}")
        handled += 1
    return handled


# ----------------------------------------------------------------------------- lanzador


def worker_command(extra_args: tuple[str, ...] = ()) -> list[str]:
    """Linea de comandos del worker (en un ejecutable congelado: ``<exe> worker``)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "worker", *extra_args]
    return [sys.executable, "-m", "micnaga", "worker", *extra_args]


def spawn_live(session: Path) -> subprocess.Popen[bytes]:
    """Lanza ``micnaga live <carpeta>`` (sin ventana, prioridad baja, grupo propio)."""
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | BELOW_NORMAL_PRIORITY_CLASS
    cmd = worker_command(())
    cmd[-1] = "live"  # ``... worker`` -> ``... live``
    return subprocess.Popen(
        [*cmd, str(session)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


def spawn_worker(
    extra_args: tuple[str, ...] = (), *, env: dict[str, str] | None = None
) -> subprocess.Popen[bytes] | None:
    """Lanza ``micnaga worker`` como proceso hijo independiente (sin ventana, prioridad baja).

    Devuelve ``None`` si ya hay un worker en marcha. Sin ``DETACHED_PROCESS`` (``CREATE_NO_WINDOW``
    se ignora con el): el hijo tiene su propio grupo de procesos y sus tres flujos en ``DEVNULL``.
    """
    if is_worker_running():
        return None
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | BELOW_NORMAL_PRIORITY_CLASS
    return subprocess.Popen(
        worker_command(extra_args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
        env={**os.environ, **(env or {})},
    )
