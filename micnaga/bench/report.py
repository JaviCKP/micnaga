"""Informe Markdown del banco de pruebas y comparativa con la ejecucion anterior.

``last_run.json`` guarda la ultima ejecucion **por modo** (``testset`` / ``synthetic``) en
``<datos de app>/bench/last_run.json`` (nunca dentro del paquete ni del repositorio).
Con motores falsos el informe lo advierte: son cifras de la tuberia, no mediciones de calidad.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from micnaga import paths
from micnaga.bench.runner import BenchRun, CaseResult

# metrica -> (etiqueta, formato, objetivo, ``True`` si menos es mejor, funcion de comprobacion)
TARGETS: dict[str, tuple[str, str, str]] = {
    "wer": ("WER", "pct", "<= 12 %"),
    "der": ("DER (remota)", "pct", "<= 20 %"),
    "echo_as_me": ("Frases de otros como «Yo»", "pct", "< 5 %"),
    "factor": ("Factor de procesado", "x", "<= 0,4"),
    "peak_rss_mb": ("Pico de RAM (MB)", "mb", "< 3072"),
    "cpu_percent_machine": ("CPU media (% del equipo)", "pct1", "-"),
    "false_positives": ("Falsos positivos (casos trampa)", "int", "0"),
    "false_negatives": ("Reuniones reales descartadas", "int", "0"),
    "failed": ("Casos con error", "int", "0"),
}


def last_run_path() -> Path:
    return paths.app_data_dir() / "bench" / "last_run.json"


def load_previous(mode: str, path: Path | None = None) -> dict[str, Any] | None:
    file = path or last_run_path()
    if not file.is_file():
        return None
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = data.get(mode) if isinstance(data, dict) else None
    return entry if isinstance(entry, dict) else None


def save_last_run(run: BenchRun, path: Path | None = None) -> Path:
    file = path or last_run_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if file.is_file():
        try:
            loaded = json.loads(file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    data[run.mode] = run.to_dict()
    file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return file


def _fmt(value: float | None, kind: str) -> str:
    if value is None:
        return "n/d"
    if kind == "pct":
        return f"{value * 100:.1f} %"
    if kind == "pct1":
        return f"{value:.1f} %"
    if kind == "x":
        return f"{value:.3f}"
    if kind == "mb":
        return f"{value:.0f}"
    if kind == "int":
        return f"{int(value)}"
    return f"{value:.3f}"


def _delta(cur: float | None, prev: float | None, kind: str) -> str:
    if cur is None or prev is None:
        return "-"
    d = cur - prev
    if abs(d) < 1e-9:
        return "="
    if kind == "pct":
        return f"{d * 100:+.1f} pp"
    if kind == "int":
        return f"{d:+.0f}"
    if kind == "mb":
        return f"{d:+.0f}"
    return f"{d:+.3f}"


def _case_row(c: CaseResult) -> str:
    stages = ", ".join(f"{k} {v:.1f}" for k, v in c.stage_times.items() if v > 0) or "-"
    return (
        f"| {c.name} | {c.status} | {c.audio_s / 60:.1f} | {c.wall_s:.1f} | "
        f"{_fmt(c.factor, 'x')} | {c.peak_rss_mb:.0f} | {_fmt(c.wer, 'pct')} | "
        f"{_fmt(c.der, 'pct')} | {_fmt(c.echo_as_me, 'pct')} | {stages} |"
    )


def render_report(run: BenchRun, previous: dict[str, Any] | None) -> str:
    lines: list[str] = [f"# Informe del banco de pruebas — {run.timestamp}", ""]
    fake = run.engines == "fake"
    if fake:
        lines += [
            "> **MOTORES FALSOS (modo sintetico).** Las cifras de WER/DER/eco/tiempos describen la",
            "> tuberia con audio sintetico y motores de prueba, **no** son mediciones de los",
            "> modelos reales ni se pueden comparar con los objetivos de la spec.",
            "",
        ]
    m = run.machine
    battery = {None: "sin bateria", True: "con bateria", False: "enchufado"}[m.get("on_battery")]
    lines += [
        f"- Modo: `{run.mode}` · motores: `{run.engines}`",
        f"- Equipo: {m.get('processor') or m.get('platform')} · {m.get('cpu_logical')} hilos · "
        f"{m.get('ram_gb')} GB · {battery}",
    ]
    used = {
        ", ".join(f"{k}={v}" for k, v in sorted(c.engines.items())) for c in run.cases if c.engines
    }
    lines += [f"- Motores usados: {u}" for u in sorted(used)]
    lines += ["", "## Resumen frente al objetivo y a la ejecucion anterior", ""]
    prev_agg = (previous or {}).get("aggregate", {})
    prev_when = (previous or {}).get("timestamp", "-")
    lines.append(f"Anterior: {prev_when if previous else 'no hay ejecucion previa'}")
    lines += [
        "",
        "| Metrica | Objetivo | Anterior | Actual | Cambio |",
        "| --- | --- | --- | --- | --- |",
    ]
    for key, (label, kind, target) in TARGETS.items():
        cur = run.aggregate.get(key)
        prev = prev_agg.get(key) if isinstance(prev_agg, dict) else None
        lines.append(
            f"| {label} | {target} | {_fmt(prev, kind)} | {_fmt(cur, kind)} | "
            f"{_delta(cur, prev, kind)} |"
        )
    lines += [
        "",
        "## Casos",
        "",
        "| Caso | Estado | Audio (min) | Tiempo (s) | Factor | RAM pico (MB) | WER | DER | "
        "Eco como Yo | Tiempo por etapa (s) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines += [_case_row(c) for c in run.cases]
    problems = [
        c for c in run.cases if c.status == "failed" or c.false_positive or c.false_negative
    ]
    if problems:
        lines += ["", "## Problemas", ""]
        for c in problems:
            what = (
                "falso positivo (caso trampa procesado)"
                if c.false_positive
                else "reunion real descartada"
                if c.false_negative
                else f"error: {c.reason}"
            )
            lines.append(f"- **{c.name}**: {what}")
    trap = [c for c in run.cases if c.expected_discard]
    if trap:
        ok = sum(1 for c in trap if not c.false_positive)
        lines += ["", f"Casos trampa descartados correctamente: {ok}/{len(trap)}."]
    return "\n".join(lines) + "\n"
