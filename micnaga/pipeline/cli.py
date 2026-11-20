"""``micnaga process <sesion>`` y ``micnaga worker`` (despacho de ``micnaga.cli``).

Este modulo no importa numpy ni onnxruntime a nivel de modulo: ``run_worker`` fija primero las
variables de hilos (``OMP_NUM_THREADS``...) y la prioridad, y solo despues importa el runner.

Codigos de salida de ``process``: 0 terminada, 3 descartada por las compuertas, 1 fallo, 2 uso.
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_DISCARDED = 3


def _process_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="micnaga process", description="Procesa una sesion grabada en primer plano."
    )
    p.add_argument("session_dir", type=Path, help="carpeta de la sesion (audio + capture.json)")
    p.add_argument("--no-rename", action="store_true", help="no renombrar la carpeta al terminar")
    p.add_argument("--keep-work", action="store_true", help="conservar work/ al terminar")
    p.add_argument(
        "--enqueue",
        action="store_true",
        help="solo anadir la sesion a la cola (la procesara el worker)",
    )
    return p


def run_process(argv: list[str]) -> int:
    args = _process_parser().parse_args(argv)
    session: Path = args.session_dir.resolve()
    if not session.is_dir():
        print(f"micnaga process: {session} no es una carpeta", file=sys.stderr)
        return EXIT_USAGE

    from micnaga.config import ConfigError, load_config

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"micnaga process: config invalida: {exc}", file=sys.stderr)
        return EXIT_USAGE
    # Hilos ANTES de importar numpy/onnxruntime (los importa el almacen y el runner).
    from micnaga.pipeline.worker import apply_thread_limits

    apply_thread_limits(config.processing.threads)
    from micnaga.logging_setup import setup_logging
    from micnaga.store.db import open_database

    setup_logging("process", console=True)
    db = open_database(config)
    try:
        if args.enqueue:
            return _enqueue(db, session)
        from micnaga.pipeline.runner import PipelineRunner

        runner = PipelineRunner(config, db=db, keep_work=args.keep_work, rename=not args.no_rename)
        result = runner.run(session)
    finally:
        db.close()
    if result.status == "done":
        print(f"listo: {result.session_dir}")
        return EXIT_OK
    if result.status == "discarded":
        print(f"descartada: {result.reason}")
        return EXIT_DISCARDED
    print(f"error: {result.reason}", file=sys.stderr)
    return EXIT_FAILED


def _enqueue(db: Any, session: Path) -> int:
    """``process --enqueue``: deja la sesion ``pending`` de verdad. Un trabajo ``failed`` se
    reintenta (antes se devolvia exito y seguia fallido: el worker nunca lo cogia)."""
    from micnaga.store.db import JobState

    job = db.enqueue_job(session)
    if job.state is JobState.FAILED:
        job = db.retry_job(job.id, reset_attempts=True)
        print(f"en cola de nuevo: trabajo {job.id} (habia fallado)")
        return EXIT_OK
    if job.state is JobState.PENDING:
        print(f"en cola: trabajo {job.id}")
        return EXIT_OK
    if job.state is JobState.PROCESSING:
        print(f"el trabajo {job.id} ya se esta procesando")
        return EXIT_OK
    print(
        f"micnaga process: el trabajo {job.id} ya esta {job.state.value}; no se vuelve a encolar",
        file=sys.stderr,
    )
    return EXIT_FAILED


def _worker_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="micnaga worker", description="Proceso de trabajo: consume la cola de procesado."
    )
    p.add_argument("--once", action="store_true", help="salir cuando no queden trabajos")
    p.add_argument("--poll", type=float, default=2.0, help="segundos entre consultas de la cola")
    p.add_argument("--keep-work", action="store_true", help="conservar work/ al terminar")
    return p


def run_worker(argv: list[str]) -> int:
    args = _worker_parser().parse_args(argv)

    from micnaga.config import ConfigError, load_config

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"micnaga worker: config invalida: {exc}", file=sys.stderr)
        return EXIT_USAGE

    # Hilos y prioridad ANTES de importar el runner (numpy, onnxruntime...).
    from micnaga.pipeline.worker import (
        PipeEventSink,
        WorkerLock,
        configure_process,
        run_worker_loop,
    )

    configure_process(config)

    from micnaga.logging_setup import setup_logging
    from micnaga.store.db import open_database

    setup_logging("worker")
    with WorkerLock() as lock:
        if not lock.acquire():
            print("micnaga worker: ya hay otro worker en marcha", file=sys.stderr)
            return EXIT_OK
        db = open_database(config)
        sink = PipeEventSink()
        stop = threading.Event()
        try:
            run_worker_loop(
                config,
                db,
                once=args.once,
                poll_interval_s=args.poll,
                emit=sink,
                stop=stop,
                keep_work=args.keep_work,
            )
        except KeyboardInterrupt:
            stop.set()
        finally:
            sink.close()
            db.close()
    return EXIT_OK


def _live_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="micnaga live",
        description="(interno) Procesa en vivo la sesion que se esta grabando.",
    )
    p.add_argument("session_dir", type=Path, help="carpeta de la sesion que se graba")
    return p


def run_live(argv: list[str]) -> int:
    """Proceso en vivo (lo lanza la app al empezar a grabar; sale al parar la grabacion)."""
    args = _live_parser().parse_args(argv)
    session: Path = args.session_dir.resolve()
    from micnaga.config import ConfigError, load_config

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"micnaga live: config invalida: {exc}", file=sys.stderr)
        return EXIT_USAGE
    # Pocos hilos y prioridad baja ANTES de importar numpy/onnxruntime: nunca molestar a la
    # captura ni a la reunion.
    from micnaga.pipeline.worker import apply_thread_limits, live_threads, set_low_priority

    threads = live_threads(config)
    apply_thread_limits(threads)
    set_low_priority()
    config.processing.threads = threads
    from micnaga.capture import sync

    sync.WARP_THREADS = threads

    from micnaga.logging_setup import setup_logging

    setup_logging("live")
    import logging

    log = logging.getLogger("micnaga.live")
    from micnaga.pipeline import live as live_mod
    from micnaga.pipeline.worker import NamedMutex

    with NamedMutex(live_mod.mutex_name(session)) as lock:
        if not lock.acquire():
            return EXIT_OK  # ya hay un proceso en vivo para esta sesion
        from micnaga.pipeline.stages.base import default_deps

        engines = live_mod.engines_from_deps(default_deps())
        if engines is None:
            return EXIT_OK
        log.info("live processing started (%d threads)", threads)
        try:
            reason = live_mod.run_live(session, config, engines)
        except Exception:
            log.exception("live processing failed; the worker will process in batch")
            return EXIT_FAILED
        log.info("live processing stopped: %s", reason)
    return EXIT_OK
