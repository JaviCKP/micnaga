"""Autocomprobacion de presupuesto: CPU y RAM del proceso mientras graba (spec seccion 2).

::

    python -m micnaga.capture.measure [--seconds 30] [--real]

Graba ``--seconds`` s en una carpeta temporal (que se borra) y escribe CPU medio, RAM maxima y
cuantas veces se muestreo. Por defecto usa audio sintetico (48 kHz estereo, igual que un
loopback tipico), lo que mide mezcla + soxr + escritura pero **no** el coste de PortAudio;
``--real`` usa WASAPI (solo en un PC con dispositivos). Presupuesto: CPU < 3 % (del total de la
maquina, como el Administrador de tareas) y RAM privada < 150 MB.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from micnaga.capture.recorder import Recorder
from micnaga.capture.sources import CaptureBackend
from micnaga.config import CaptureConfig

CPU_BUDGET_PCT = 3.0
RAM_BUDGET_MB = 150.0


@dataclass(frozen=True)
class Measurement:
    seconds: float
    cpu_pct_total: float
    """CPU medio del proceso como % de **toda** la maquina (como el Administrador de tareas)."""
    cpu_pct_core: float
    """CPU medio como % de un nucleo."""
    ram_private_mb: float
    """Conjunto de trabajo privado (USS): lo que muestra el Administrador de tareas."""
    ram_rss_mb: float
    commit_mb: float
    """Memoria comprometida (bytes privados). En Windows ``import numpy`` reserva ~50 MB por
    nucleo (OpenBLAS) sin usarlos; con ``OPENBLAS_NUM_THREADS=1`` antes de importar numpy baja."""
    samples: int

    @property
    def within_budget(self) -> bool:
        return self.cpu_pct_total < CPU_BUDGET_PCT and self.ram_private_mb < RAM_BUDGET_MB

    def report(self) -> str:
        return (
            f"{self.seconds:.0f} s: CPU {self.cpu_pct_total:.2f} % of machine "
            f"({self.cpu_pct_core:.2f} % of one core), private working set "
            f"{self.ram_private_mb:.0f} MB, RSS {self.ram_rss_mb:.0f} MB, "
            f"commit {self.commit_mb:.0f} MB -> "
            f"{'within' if self.within_budget else 'OVER'} budget "
            f"(< {CPU_BUDGET_PCT:.0f} % CPU, < {RAM_BUDGET_MB:.0f} MB)"
        )


def measure(
    seconds: float,
    backend: CaptureBackend | None,
    session_dir: Path,
    config: CaptureConfig | None = None,
) -> Measurement:
    """Graba ``seconds`` s y mide el proceso actual (incluye el hilo de este llamador)."""
    proc = psutil.Process()
    rec = Recorder(config or CaptureConfig(), backend=backend)
    rec.start(session_dir)
    cpu0 = sum(proc.cpu_times()[:2])
    t0 = time.perf_counter()
    peak_private = peak_rss = peak_commit = 0.0
    n = 0
    while time.perf_counter() - t0 < seconds:
        time.sleep(0.5)
        mi = proc.memory_info()
        try:
            uss = float(proc.memory_full_info().uss)
        except psutil.Error:  # pragma: no cover - sin permisos: se usa RSS
            uss = float(mi.rss)
        peak_private = max(peak_private, uss)
        peak_rss = max(peak_rss, float(mi.rss))
        peak_commit = max(peak_commit, float(getattr(mi, "private", mi.rss)))
        n += 1
    wall = time.perf_counter() - t0
    cpu = sum(proc.cpu_times()[:2]) - cpu0
    rec.stop()
    core = 100.0 * cpu / wall
    return Measurement(
        seconds=wall,
        cpu_pct_total=core / (psutil.cpu_count() or 1),
        cpu_pct_core=core,
        ram_private_mb=peak_private / 1e6,
        ram_rss_mb=peak_rss / 1e6,
        commit_mb=peak_commit / 1e6,
        samples=n,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m micnaga.capture.measure")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--real", action="store_true", help="usar WASAPI en vez de audio sintetico")
    args = ap.parse_args(argv)
    backend: CaptureBackend | None = None
    if not args.real:
        from micnaga.capture.synthetic import demo_backend

        backend = demo_backend()
    tmp = Path(tempfile.mkdtemp(prefix="micnaga-measure-"))
    try:
        print(measure(args.seconds, backend, tmp / "session").report())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
