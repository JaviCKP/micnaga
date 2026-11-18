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
            job = db.enqueue_job(session)
            print(f"en cola: trabajo {job.id}")
            return EXIT_OK
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
