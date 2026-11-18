"""Ejecucion del banco de pruebas: procesa cada caso y mide (spec 12).

**Nunca se procesa el set de prueba en el sitio**: el pipeline borra las sesiones descartadas y
renombra las terminadas. Cada caso se copia a una carpeta temporal (con su propia base de datos
SQLite temporal) y se procesa alli; la carpeta temporal se borra al terminar.

Formato de un caso: ``docs/testset_format.md``.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from micnaga import paths
from micnaga.bench import metrics
from micnaga.bench.measure import ResourceMonitor, machine_info
from micnaga.types import CaptureMeta, Transcript

if TYPE_CHECKING:
    from micnaga.config import Config
    from micnaga.pipeline.runner import DepsSource

REFERENCE_FILE = "reference.json"
EXPECT_FILE = "expect.json"


@dataclass
class CaseResult:
    name: str
    status: str
    """``done`` | ``discarded`` | ``failed``."""
    reason: str | None = None
    audio_s: float = 0.0
    wall_s: float = 0.0
    cpu_s: float = 0.0
    cpu_percent_machine: float = 0.0
    peak_rss_mb: float = 0.0
    factor: float | None = None
    """``wall_s / audio_s`` (factor de procesado; objetivo <= 0,4)."""
    stage_times: dict[str, float] = field(default_factory=dict)
    wer: float | None = None
    der: float | None = None
    echo_as_me: float | None = None
    expected_discard: bool | None = None
    false_positive: bool = False
    """Caso trampa que NO se descarto."""
    false_negative: bool = False
    """Reunion real que se descarto."""
    engines: dict[str, str | None] = field(default_factory=dict)
    echo_dropped: int | None = None
    n_segments: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BenchRun:
    mode: str
    """``testset`` | ``synthetic``."""
    engines: str
    """``real`` | ``fake``."""
    timestamp: str
    machine: dict[str, Any]
    cases: list[CaseResult]
    aggregate: dict[str, float | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "engines": self.engines,
            "timestamp": self.timestamp,
            "machine": self.machine,
            "aggregate": self.aggregate,
            "cases": {c.name: c.to_dict() for c in self.cases},
        }


def discover_cases(testset: Path, only: Sequence[str] | None = None) -> list[Path]:
    """Subcarpetas con ``capture.json`` (ordenadas); ``only`` filtra por nombre."""
    cases = sorted(
        p for p in testset.iterdir() if p.is_dir() and (p / paths.CAPTURE_JSON).is_file()
    )
    if only:
        wanted = set(only)
        cases = [p for p in cases if p.name in wanted]
    return cases


def _read_transcript(file: Path) -> Transcript | None:
    if not file.is_file():
        return None
    return Transcript.from_json(file.read_text(encoding="utf-8"))


def _audio_seconds(session: Path) -> float:
    import soundfile as sf

    longest = 0.0
    meta = CaptureMeta.from_json((session / paths.CAPTURE_JSON).read_text(encoding="utf-8"))
    for info in (meta.mic, meta.system):
        for name in (info.file, paths.AUDIO_ME_FLAC, paths.AUDIO_REMOTE_FLAC):
            f = session / name
            if f.is_file():
                i = sf.info(str(f))
                longest = max(longest, i.frames / i.samplerate)
                break
    return longest


def run_case(
    case: Path,
    scratch: Path,
    config: Config,
    deps: DepsSource,
    *,
    db_path: Path | None = None,
) -> CaseResult:
    """Procesa una copia del caso en ``scratch`` y devuelve sus mediciones."""
    from micnaga.pipeline.runner import PipelineRunner
    from micnaga.store.db import Database

    session = scratch / case.name
    shutil.copytree(case, session, ignore=shutil.ignore_patterns(REFERENCE_FILE, EXPECT_FILE))
    audio_s = _audio_seconds(session)
    expect_file = case / EXPECT_FILE
    expected_discard: bool | None = None
    if expect_file.is_file():
        expected_discard = bool(json.loads(expect_file.read_text(encoding="utf-8")).get("discard"))
    reference = _read_transcript(case / REFERENCE_FILE)

    with Database(db_path or scratch / "bench.db") as db:
        runner = PipelineRunner(config, deps=deps, db=db, keep_work=False, rename=False)
        with ResourceMonitor() as monitor:
            result = runner.run(session)
    res = monitor.result
    assert res is not None
    out = CaseResult(
        name=case.name,
        status=result.status,
        reason=result.reason,
        audio_s=audio_s,
        wall_s=res.wall_s,
        cpu_s=res.cpu_s,
        cpu_percent_machine=res.cpu_percent_machine,
        peak_rss_mb=res.peak_rss_mb,
        factor=(res.wall_s / audio_s) if audio_s > 0 else None,
        stage_times={k: round(v, 3) for k, v in result.stage_times.items()},
        expected_discard=expected_discard,
        false_positive=bool(expected_discard) and result.status != "discarded",
        false_negative=expected_discard is False and result.status == "discarded",
    )
    transcript = result.transcript
    if transcript is not None:
        out.n_segments = len(transcript.segments)
        out.engines = {
            "asr": transcript.engines.asr,
            "diarization": transcript.engines.diarization,
            "aec": transcript.engines.aec,
        }
        if reference is not None:
            out.wer = metrics.wer(reference, transcript)
            out.der = metrics.der(reference, transcript)
            out.echo_as_me = metrics.echo_as_me_rate(reference, transcript)
    return out


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def aggregate(cases: list[CaseResult]) -> dict[str, float | None]:
    done = [c for c in cases if c.status == "done"]
    audio = sum(c.audio_s for c in done)
    return {
        "cases": float(len(cases)),
        "done": float(len(done)),
        "failed": float(sum(1 for c in cases if c.status == "failed")),
        "wer": _mean([c.wer for c in done if c.wer is not None]),
        "der": _mean([c.der for c in done if c.der is not None]),
        "echo_as_me": _mean([c.echo_as_me for c in done if c.echo_as_me is not None]),
        "factor": (sum(c.wall_s for c in done) / audio) if audio > 0 else None,
        "peak_rss_mb": max((c.peak_rss_mb for c in cases), default=0.0) or None,
        "cpu_percent_machine": _mean([c.cpu_percent_machine for c in done]),
        "false_positives": float(sum(1 for c in cases if c.false_positive)),
        "false_negatives": float(sum(1 for c in cases if c.false_negative)),
    }


def run_bench(
    cases: Sequence[Path],
    config: Config,
    *,
    mode: str,
    engines: str,
    deps_for: Callable[[Path], DepsSource],
    progress: Callable[[str], None] | None = None,
) -> BenchRun:
    """Procesa todos los casos (copias temporales) y agrega los resultados."""
    scratch = Path(tempfile.mkdtemp(prefix="micnaga-bench-"))
    results: list[CaseResult] = []
    try:
        for case in cases:
            if progress is not None:
                progress(f"[{len(results) + 1}/{len(cases)}] {case.name}")
            t0 = time.perf_counter()
            try:
                results.append(run_case(case, scratch, config, deps_for(case)))
            except Exception as exc:  # un caso roto no debe tirar el informe entero
                results.append(
                    CaseResult(
                        case.name,
                        "failed",
                        f"{type(exc).__name__}: {exc}",
                        wall_s=time.perf_counter() - t0,
                    )
                )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return BenchRun(
        mode=mode,
        engines=engines,
        timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        machine=machine_info(),
        cases=results,
        aggregate=aggregate(results),
    )
