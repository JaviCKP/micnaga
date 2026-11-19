"""«Rellenar partes de horas»: el panel de revision fuera de las reuniones (y su estado).

* ``plan``: lee de Odoo los partes de las ultimas semanas, calcula los dias que faltan o estan
  incompletos (``timesheets.analyze_days``), el patron habitual y, para cada dia, las lineas
  propuestas con la evidencia que haya (reuniones grabadas en Micnaga y eventos del calendario
  de Odoo). Solo salen marcadas las lineas con evidencia real.
* ``apply``: valida cada linea editada por el usuario **en Python** (``HoursBook``: proyecto,
  tarea, fecha no futura, 0,25-12 h, <= 10 h/dia contando lo que ya hay en Odoo), salta una linea
  identica ya registrada y crea ``account.analytic.line`` sin ``employee_id`` (Odoo pone el
  empleado del usuario). Cada aplicacion es un **lote** que se puede deshacer.
* Estado en ``<datos>/secretaria/horas.json``: dias marcados como libres, ultimo aviso semanal y
  los ultimos lotes (ids creados).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from micnaga.brain import timesheets as ts

log = logging.getLogger(__name__)

STATE_FILE = "horas.json"
LOOKBACK_DAYS = 35
HISTORY_DAYS = 63
MAX_BATCHES = 20
MAX_LINES = 120
TIMESHEET_MODEL = "account.analytic.line"


# ============================================================================== estado


def _path() -> Path:
    from micnaga.brain.learn import secretaria_dir

    return secretaria_dir() / STATE_FILE


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("libres", [])
    data.setdefault("ultimo_aviso", None)
    data.setdefault("lotes", [])
    return data


def save_state(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def off_days() -> list[str]:
    return [str(d) for d in load_state()["libres"]]


def set_off(day: str, off: bool) -> list[str]:
    if ts.parse_day(day) is None:
        raise ValueError("fecha")
    data = load_state()
    days = set(data["libres"])
    if off:
        days.add(day)
    else:
        days.discard(day)
    data["libres"] = sorted(days)[-400:]
    save_state(data)
    return list(data["libres"])


# ============================================================================== validacion


class HoursBook:
    """Valida lineas contra Odoo con cache (proyectos, tareas, horas por dia + lo aceptado)."""

    def __init__(self, odoo: Any, today: date) -> None:
        self.odoo = odoo
        self.today = today
        self._projects: dict[int, dict[str, Any] | None] = {}
        self._tasks: dict[int, dict[str, Any] | None] = {}
        self._days: dict[str, list[dict[str, Any]]] = {}
        self._accepted: dict[str, float] = {}

    def project(self, pid: int) -> dict[str, Any] | None:
        if pid not in self._projects:
            self._projects[pid] = self.odoo.read_project(pid)
        return self._projects[pid]

    def task(self, tid: int) -> dict[str, Any] | None:
        if tid not in self._tasks:
            self._tasks[tid] = self.odoo.read_task(tid)
        return self._tasks[tid]

    def day_lines(self, day: str) -> list[dict[str, Any]]:
        if day not in self._days:
            self._days[day] = list(self.odoo.timesheet_lines(day, day))
        return self._days[day]

    def day_total(self, day: str) -> float:
        existing = sum(float(ln.get("horas") or 0) for ln in self.day_lines(day))
        return round(existing + self._accepted.get(day, 0.0), 2)

    def validate(self, raw: dict[str, Any], *, explicit: bool = False) -> dict[str, Any]:
        day = ts.parse_day(raw.get("fecha"))
        total = self.day_total(day.isoformat()) if day is not None else 0.0
        return ts.validate_line(
            raw,
            today=self.today,
            project=self.project,
            task=self.task,
            day_total=total,
            explicit=explicit,
        )

    def exists(self, line: dict[str, Any]) -> bool:
        return any(ts.same_line(line, ln) for ln in self.day_lines(line["fecha"]))

    def accept(self, line: dict[str, Any]) -> None:
        self._accepted[line["fecha"]] = self._accepted.get(line["fecha"], 0.0) + line["horas"]


def create_values(line: dict[str, Any]) -> dict[str, Any]:
    """Valores de ``account.analytic.line``. Sin ``employee_id`` ni ``user_id``: Odoo pone los
    del usuario conectado (un empleado normal no puede leer ``hr.employee``)."""
    values: dict[str, Any] = {
        "project_id": line["proyecto_id"],
        "date": line["fecha"],
        "unit_amount": line["horas"],
        "name": line["descripcion"],
    }
    if line.get("tarea_id"):
        values["task_id"] = line["tarea_id"]
    return values


# ============================================================================== plan


def plan(
    odoo: Any,
    *,
    today: date,
    meetings: list[ts.Evidence] | None = None,
    calendar: bool = True,
) -> dict[str, Any]:
    """Dias por rellenar con sus lineas propuestas (ver modulo)."""
    history = odoo.timesheet_lines(
        (today - timedelta(days=HISTORY_DAYS)).isoformat(), today.isoformat()
    )
    start = today - timedelta(days=LOOKBACK_DAYS)
    window = [ln for ln in history if str(ln["fecha"]) >= start.isoformat()]
    off = off_days()
    days = ts.analyze_days(window, start, today, today=today, off_days=off, history=history)
    todo = [d for d in days if d.estado in ("falta", "incompleto")]
    pattern = ts.learn_pattern(history)
    projects = [p for p in odoo.projects() if p.get("horas", True)]
    evidence: dict[str, list[ts.Evidence]] = {}
    for ev in meetings or []:
        evidence.setdefault(ev.fecha, []).append(ev)
    if calendar and todo:
        try:
            for e in odoo.calendar_events(todo[0].fecha, todo[-1].fecha):
                if e.get("fecha") and not e.get("todo_el_dia"):
                    evidence.setdefault(e["fecha"], []).append(
                        ts.Evidence(
                            e["fecha"],
                            str(e.get("nombre") or ""),
                            e.get("inicio"),
                            float(e.get("horas") or 0.0),
                            "calendario",
                        )
                    )
        except Exception as exc:  # sin calendario o sin permiso: se sigue sin el
            log.info("calendar not available for the hours plan: %s", getattr(exc, "kind", exc))
    by_day: dict[str, list[dict[str, Any]]] = {}
    for ln in window:
        by_day.setdefault(str(ln["fecha"]), []).append(ln)
    out_days = []
    for d in todo:
        existing = by_day.get(d.fecha, [])
        lines = ts.propose_day(d.fecha, pattern, existing, evidence.get(d.fecha, []), projects)
        for i, ln in enumerate(lines):
            ln["key"] = f"{d.fecha}#{i}"
        out_days.append(
            {
                **d.to_dict(),
                "existentes": [
                    {
                        "proyecto": (ln.get("proyecto") or {}).get("nombre"),
                        "horas": ln.get("horas"),
                        "descripcion": ln.get("descripcion"),
                    }
                    for ln in existing
                ],
                "lineas": lines,
            }
        )
    learned = _remember_pattern(pattern, today)
    return {
        "hoy": today.isoformat(),
        "jornada": ts.typical_day_hours(history),
        "patron": ts.pattern_summary(pattern),
        "aprendido": learned,
        "dias": out_days,
        "posibles_libres": [d.fecha for d in days if d.estado == "libre_probable"],
        "libres": [d.fecha for d in days if d.estado == "libre"],
        "proyectos": [{"id": p["id"], "nombre": p["nombre"]} for p in projects],
    }


def _remember_pattern(pattern: list[dict[str, Any]], today: date) -> bool:
    """El patron va a ``learned.md`` (solo nombres de proyecto y franjas)."""
    text = ts.pattern_summary(pattern)
    if not text:
        return False
    try:
        from micnaga.brain import learn

        return learn.upsert_prefixed(ts.LEARN_PREFIX, text, source="crm", today=today.isoformat())
    except Exception:
        log.exception("could not store the hours pattern")
        return False


# ============================================================================== aplicar


def apply(odoo: Any, lines: list[dict[str, Any]], *, today: date) -> dict[str, Any]:
    """Crea las lineas (validadas una a una). Devuelve resultados por ``key`` y el lote."""
    book = HoursBook(odoo, today)
    results: list[dict[str, Any]] = []
    created: list[dict[str, Any]] = []
    for raw in lines[:MAX_LINES]:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "")
        try:
            line = book.validate(raw)
        except ts.LineError as exc:
            results.append({"key": key, "ok": False, "error": str(exc)})
            continue
        if book.exists(line):
            results.append({"key": key, "ok": True, "error": "ya_existe", "id": None})
            continue
        try:
            new_id = odoo.create(TIMESHEET_MODEL, create_values(line))
        except Exception as exc:
            kind = getattr(exc, "kind", "failed")
            results.append({"key": key, "ok": False, "error": f"odoo_{kind}"})
            continue
        book.accept(line)
        created.append({**line, "odoo_id": new_id})
        results.append({"key": key, "ok": True, "error": None, "id": new_id})
    batch = None
    if created:
        batch = uuid.uuid4().hex[:12]
        data = load_state()
        data["lotes"] = [
            *data["lotes"][-(MAX_BATCHES - 1) :],
            {
                "id": batch,
                "creado": datetime.now().isoformat(timespec="seconds"),
                "lineas": created,
                "deshecho": False,
            },
        ]
        save_state(data)
    return {"resultados": results, "lote": batch, "creadas": len(created)}


def undo(odoo: Any, batch: str) -> dict[str, Any]:
    data = load_state()
    found = next((b for b in data["lotes"] if b.get("id") == batch), None)
    if found is None or found.get("deshecho"):
        return {"ok": False, "error": "no_batch"}
    ids = [int(ln["odoo_id"]) for ln in found.get("lineas") or [] if ln.get("odoo_id")]
    if ids:
        odoo.unlink(TIMESHEET_MODEL, ids)
    found["deshecho"] = True
    save_state(data)
    return {"ok": True, "deshechas": len(ids)}


# ============================================================================== aviso semanal


def week_gaps(odoo: Any, today: date) -> list[str]:
    """Dias laborables de esta semana (lunes..hoy) que faltan o estan incompletos."""
    monday = today - timedelta(days=today.weekday())
    history = odoo.timesheet_lines(
        (today - timedelta(days=HISTORY_DAYS)).isoformat(), today.isoformat()
    )
    week = [ln for ln in history if str(ln["fecha"]) >= monday.isoformat()]
    days = ts.analyze_days(week, monday, today, today=today, off_days=off_days(), history=history)
    return [d.fecha for d in days if d.estado in ("falta", "incompleto")]


def should_nudge(now: datetime, *, enabled: bool, last: str | None, hour: int = 15) -> bool:
    """Viernes por la tarde, una vez por semana, si esta activado."""
    if not enabled or now.weekday() != 4 or now.hour < hour:
        return False
    return last != now.date().isoformat()
