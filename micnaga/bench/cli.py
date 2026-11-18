"""``micnaga bench`` / ``python -m micnaga.bench`` (spec 12).

Procesa el set de prueba (``MICNAGA_TESTSET`` o ``--testset``) o, con ``--synthetic``, sesiones
sinteticas generadas al vuelo, y escribe un informe Markdown comparado con la ejecucion anterior.
Formato del set: ``docs/testset_format.md``.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from micnaga.pipeline.runner import DepsSource

ENV_TESTSET = "MICNAGA_TESTSET"
EXIT_OK = 0
EXIT_USAGE = 2


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="micnaga bench", description="Banco de pruebas de Micnaga.")
    p.add_argument(
        "--testset", type=Path, help=f"carpeta del set de prueba (por defecto ${ENV_TESTSET})"
    )
    p.add_argument("--out", type=Path, help="informe Markdown (por defecto <datos de app>/bench/)")
    p.add_argument(
        "--synthetic", action="store_true", help="sesiones sinteticas, sin set de prueba"
    )
    p.add_argument(
        "--engines",
        choices=("auto", "real", "fake"),
        default="auto",
        help="auto = reales con el set de prueba, falsos con --synthetic",
    )
    p.add_argument("--cases", nargs="*", help="solo estos casos (nombres de carpeta)")
    p.add_argument("--last-run", type=Path, help="fichero last_run.json alternativo")
    p.add_argument("--no-save", action="store_true", help="no actualizar last_run.json")
    return p


def run(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")

    from micnaga.config import ConfigError, load_config

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"micnaga bench: config invalida: {exc}", file=sys.stderr)
        return EXIT_USAGE
    from micnaga.pipeline.worker import apply_thread_limits

    apply_thread_limits(config.processing.threads)

    from micnaga import paths
    from micnaga.bench import report
    from micnaga.bench.runner import discover_cases, run_bench

    engines = args.engines
    if engines == "auto":
        engines = "fake" if args.synthetic else "real"

    tmp_root: Path | None = None
    try:
        if args.synthetic:
            from micnaga.bench.synthetic import SYNTHETIC_CASES, generate_session, spec_for_case

            tmp_root = Path(tempfile.mkdtemp(prefix="micnaga-synthetic-"))
            names = args.cases or list(SYNTHETIC_CASES)
            unknown = [n for n in names if n not in SYNTHETIC_CASES]
            if unknown:
                print(f"micnaga bench: casos sinteticos desconocidos: {unknown}", file=sys.stderr)
                return EXIT_USAGE
            for name in names:
                generate_session(tmp_root / name, spec_for_case(name))
            testset = tmp_root
            mode = "synthetic"
        else:
            raw = args.testset or (
                Path(os.environ[ENV_TESTSET]) if os.environ.get(ENV_TESTSET) else None
            )
            if raw is None:
                print(
                    f"micnaga bench: no hay set de prueba. Define {ENV_TESTSET} o usa --testset "
                    "(o prueba --synthetic). Formato: docs/testset_format.md",
                    file=sys.stderr,
                )
                return EXIT_USAGE
            testset = raw.expanduser()
            if not testset.is_dir():
                print(f"micnaga bench: {testset} no existe o no es una carpeta", file=sys.stderr)
                return EXIT_USAGE
            mode = "testset"
        cases = discover_cases(testset, None if args.synthetic else args.cases)
        if not cases:
            print(
                f"micnaga bench: no hay casos (carpetas con capture.json) en {testset}",
                file=sys.stderr,
            )
            return EXIT_USAGE

        def deps_for(case: Path) -> DepsSource:
            if engines == "fake":
                from micnaga.bench.fakes import fake_deps_for_session

                return fake_deps_for_session(case)
            return None

        bench_run = run_bench(
            cases,
            config,
            mode=mode,
            engines=engines,
            deps_for=deps_for,
            progress=lambda msg: print(msg, flush=True),
        )
    finally:
        if tmp_root is not None:
            import shutil

            shutil.rmtree(tmp_root, ignore_errors=True)

    previous = report.load_previous(mode, args.last_run)
    text = report.render_report(bench_run, previous)
    out = args.out or (paths.app_data_dir() / "bench" / f"report-{mode}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    if not args.no_save:
        report.save_last_run(bench_run, args.last_run)
    print(text)
    print(f"informe: {out}")
    return EXIT_OK
