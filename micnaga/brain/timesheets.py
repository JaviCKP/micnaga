"""Partes de horas («fichar»): dias laborables, huecos, patron habitual y validacion.

Todo es local y puro (sin Odoo): recibe las lineas ya leidas (``OdooClient.timesheet_lines``) y
devuelve dias, patron y lineas propuestas. Lo usan la herramienta MCP ``mis_partes_de_horas``, el
panel «Rellenar partes de horas» y la validacion de ``registrar_horas`` antes de escribir.

* **Laborables**: lunes a viernes, menos los festivos **nacionales** de Espana que se pueden
  calcular (fijos + Viernes Santo) y los dias que el usuario marca como libres. Los festivos
  autonomicos/locales (p. ej. el lunes 2 de noviembre de 2026 en Andalucia) no se calculan: un
  dia sin horas **entre** dias con horas se trata como «probablemente libre» y no se propone.
* **Huecos**: ``falta`` (laborable sin horas despues del ultimo dia con horas) e ``incompleto``
  (con horas pero menos de ~3/4 de la jornada tipica).
* **Patron**: franjas que se repiten (proyecto + tarea + tramo horario de la descripcion
  «(9:00-13:30)» o, sin tramo, proyecto + horas) en las ultimas semanas.
* **Validacion**: fecha no futura, 0,25-12 h por linea (en cuartos de hora), <= 10 h por dia
  (contando lo que ya hay en Odoo) salvo peticion explicita, proyecto activo con partes de horas,
  tarea del proyecto y abierta.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

MIN_LINE_H = 0.25
MAX_LINE_H = 12.0
MAX_DAY_H = 10.0
DEFAULT_DAY_H = 8.0
PATTERN_WEEKS = 4
PATTERN_MIN_FREQ = 0.6
INCOMPLETE_RATIO = 0.75
WEEKDAY_LETTERS = ("L", "M", "X", "J", "V", "S", "D")
WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_RANGE = re.compile(r"\(?\s*(\d{1,2})[:.h](\d{2})\s*[-–a]\s*(\d{1,2})[:.h](\d{2})\s*\)?")
LEARN_PREFIX = "Partes de horas habituales:"


# ============================================================================== calendario


def easter(year: int) -> date:
    """Domingo de Pascua (algoritmo gregoriano anonimo)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def national_holidays(year: int) -> dict[date, str]:
    """Festivos nacionales comunes a toda Espana (no autonomicos ni locales)."""
    days = {
        date(year, 1, 1): "Año Nuevo",
        date(year, 1, 6): "Reyes",
        date(year, 5, 1): "Fiesta del Trabajo",
        date(year, 8, 15): "Asunción",
        date(year, 10, 12): "Fiesta Nacional",
        date(year, 11, 1): "Todos los Santos",
        date(year, 12, 6): "Constitución",
        date(year, 12, 8): "Inmaculada",
        date(year, 12, 25): "Navidad",
    }
    days[easter(year) - timedelta(days=2)] = "Viernes Santo"
    return days


def holiday_name(day: date) -> str | None:
    return national_holidays(day.year).get(day)


def is_working_day(day: date, off_days: Iterable[str] = ()) -> bool:
    return day.weekday() < 5 and holiday_name(day) is None and day.isoformat() not in off_days


def daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def parse_day(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


# ============================================================================== dias


def time_range(text: str) -> tuple[str, str] | None:
    """``"Formación (9:00-13:30)"`` -> ``("9:00", "13:30")``."""
    m = _RANGE.search(text or "")
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if not (0 <= h1 < 24 and 0 <= h2 < 24 and m1 < 60 and m2 < 60):
        return None
    return f"{h1}:{m1:02d}", f"{h2}:{m2:02d}"


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def round_quarter(hours: float) -> float:
    return round(round(float(hours) * 4) / 4, 2)


def totals_by_day(lines: Iterable[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for ln in lines:
        if ln.get("fecha"):
            out[str(ln["fecha"])] += float(ln.get("horas") or 0.0)
    return {k: round(v, 2) for k, v in out.items()}


def typical_day_hours(lines: list[dict[str, Any]]) -> float:
    """Jornada tipica (mediana de los dias con horas; 8 si no hay datos)."""
    vals = sorted(v for v in totals_by_day(lines).values() if v > 0)
    if not vals:
        return DEFAULT_DAY_H
    return round_quarter(vals[len(vals) // 2])


@dataclass(frozen=True)
class Day:
    fecha: str
    estado: str
    """``completo`` | ``incompleto`` | ``falta`` | ``libre_probable`` | ``libre`` | ``festivo``."""
    horas: float
    faltan: float
    festivo: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = date.fromisoformat(self.fecha)
        out: dict[str, Any] = {
            "fecha": self.fecha,
            "dia": WEEKDAYS[d.weekday()],
            "estado": self.estado,
            "horas": self.horas,
        }
        if self.faltan:
            out["faltan"] = self.faltan
        if self.festivo:
            out["festivo"] = self.festivo
        return out


def analyze_days(
    lines: list[dict[str, Any]],
    desde: date,
    hasta: date,
    *,
    today: date,
    off_days: Iterable[str] = (),
    jornada: float | None = None,
    history: list[dict[str, Any]] | None = None,
) -> list[Day]:
    """Estado de cada dia laborable (y de los festivos) entre ``desde`` y ``min(hasta, hoy)``.

    ``history`` (lineas de un periodo mas largo que INCLUYE ``lines``, opcional) solo sirve
    para saber cual fue el
    ultimo dia con horas y la jornada tipica.
    """
    off = set(off_days)
    every = history if history is not None else lines
    totals = totals_by_day(lines)
    all_totals = totals_by_day(every)
    logged = [d for d, h in all_totals.items() if h > 0]
    last = max(logged) if logged else None
    typical = jornada if jornada else typical_day_hours(every)
    out: list[Day] = []
    for day in daterange(desde, min(hasta, today)):
        iso = day.isoformat()
        h = totals.get(iso, 0.0)
        if day.weekday() >= 5:
            continue
        hol = holiday_name(day)
        if hol:
            out.append(Day(iso, "festivo", h, 0.0, hol))
            continue
        if iso in off:
            out.append(Day(iso, "libre", h, 0.0))
            continue
        if h <= 0:
            if last is not None and iso < last:
                out.append(Day(iso, "libre_probable", 0.0, 0.0))
            else:
                out.append(Day(iso, "falta", 0.0, typical))
        elif h < typical * INCOMPLETE_RATIO:
            out.append(Day(iso, "incompleto", h, round_quarter(typical - h)))
        else:
            out.append(Day(iso, "completo", h, 0.0))
    return out


# ============================================================================== patron


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", t).split())


def _strip_range(text: str) -> str:
    return " ".join(_RANGE.sub(" ", text or "").split()).strip(" -·")


def learn_pattern(
    lines: list[dict[str, Any]], *, weeks: int = PATTERN_WEEKS, until: date | None = None
) -> list[dict[str, Any]]:
    """Franjas habituales de las ultimas ``weeks`` semanas con horas (ordenadas por hora).

    Cada franja: ``proyecto``, ``tarea``, ``horas``, ``inicio``/``fin`` (si la descripcion los
    lleva), ``descripcion`` (la variante mas frecuente), ``variantes`` y ``frecuencia`` (fraccion
    de dias con horas en que aparece) y ``dias`` (letras L-V en que suele aparecer).
    """
    days = sorted({str(ln["fecha"]) for ln in lines if ln.get("fecha") and ln.get("proyecto")})
    if not days:
        return []
    end = parse_day(days[-1]) or date.today()
    if until is not None:
        end = min(end, until)
    start = end - timedelta(weeks=weeks)
    recent = [
        ln
        for ln in lines
        if ln.get("proyecto") and (d := parse_day(ln.get("fecha"))) and start < d <= end
    ]
    logged_days = sorted({str(ln["fecha"]) for ln in recent})
    if not logged_days:
        return []
    per_weekday = Counter(date.fromisoformat(d).weekday() for d in logged_days)
    slots: dict[tuple[Any, ...], dict[str, Any]] = {}
    for ln in sorted(recent, key=lambda x: str(x["fecha"]), reverse=True):
        proj = ln["proyecto"]
        task = ln.get("tarea")
        desc = str(ln.get("descripcion") or "")
        rng = time_range(desc)
        hours = round_quarter(float(ln.get("horas") or 0))
        key = (proj["id"], (task or {}).get("id"), rng if rng else ("~", hours))
        slot = slots.setdefault(
            key,
            {
                "proyecto": proj,
                "tarea": task,
                "rango": rng,
                "dias_set": set(),
                "horas": Counter(),
                "variantes": Counter(),
            },
        )
        slot["dias_set"].add(str(ln["fecha"]))
        slot["horas"][hours] += 1
        slot["variantes"][_strip_range(desc) or desc] += 1
    out = []
    n_days = len(logged_days)
    for slot in slots.values():
        freq = len(slot["dias_set"]) / n_days
        if freq < PATTERN_MIN_FREQ:
            continue
        hours = slot["horas"].most_common(1)[0][0]
        variants = [v for v, _n in slot["variantes"].most_common(6) if v]
        rng = slot["rango"]
        main = variants[0] if variants else str(slot["proyecto"].get("nombre") or "")
        wd = Counter(date.fromisoformat(d).weekday() for d in slot["dias_set"])
        letters = [
            WEEKDAY_LETTERS[i]
            for i in range(5)
            if per_weekday.get(i) and wd.get(i, 0) / per_weekday[i] >= 0.5
        ]
        out.append(
            {
                "proyecto": slot["proyecto"],
                "tarea": slot["tarea"],
                "horas": hours,
                "inicio": rng[0] if rng else None,
                "fin": rng[1] if rng else None,
                "descripcion": f"{main} ({rng[0]}-{rng[1]})" if rng else main,
                "variantes": variants,
                "frecuencia": round(freq, 2),
                "dias": letters,
            }
        )
    out.sort(key=lambda s: (s["inicio"] is None, minutes(s["inicio"]) if s["inicio"] else 0))
    return out


def _days_label(letters: list[str]) -> str:
    if letters == ["L", "M", "X", "J", "V"]:
        return "L–V"
    return ", ".join(letters)


def pattern_summary(pattern: list[dict[str, Any]]) -> str:
    """Una linea para ``learned.md`` (solo nombres de proyecto y franjas, sin personas)."""
    by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in pattern:
        by_project[str(s["proyecto"].get("nombre") or "?").strip()].append(s)
    parts = []
    for name, slots in by_project.items():
        ranged = [s for s in slots if s["inicio"]]
        other = [s for s in slots if not s["inicio"]]
        days = _days_label(sorted({x for s in slots for x in s["dias"]}, key="LMXJV".index))
        bits = [f"{s['inicio']}-{s['fin']}" for s in ranged]
        bits += [f"{_fmt_h(s['horas'])} h" for s in other]
        parts.append(f"{name} {days} {' + '.join(bits)}".strip())
    return (LEARN_PREFIX + " " + "; ".join(parts))[:240] if parts else ""


def _fmt_h(h: float) -> str:
    return f"{h:g}".replace(".", ",")


# ============================================================================== propuesta


@dataclass(frozen=True)
class Evidence:
    """Algo que paso ese dia: una reunion grabada en Micnaga o un evento del calendario."""

    fecha: str
    titulo: str
    inicio: str | None
    """``HH:MM`` local."""
    horas: float
    origen: str  # reunion | calendario


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


STRONG_MATCH = 85
WEAK_MATCH = 70


def guess_project(
    text: str, projects: list[dict[str, Any]], pattern: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Proyecto para un titulo (nombre del proyecto o descripciones habituales); si no, None."""
    project, _score = guess_project_scored(text, projects, pattern)
    return project


def guess_project_scored(
    text: str, projects: list[dict[str, Any]], pattern: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, float]:
    """Como ``guess_project`` pero con la puntuacion (0-100) del mejor parecido."""
    from rapidfuzz import fuzz

    q = _norm(text)
    if not q:
        return None, 0.0
    best: tuple[float, dict[str, Any] | None] = (0.0, None)
    for p in projects:
        score = fuzz.token_set_ratio(q, _norm(str(p.get("nombre") or "")))
        if score > best[0]:
            best = (score, {"id": p["id"], "nombre": p.get("nombre")})
    for s in pattern:
        for v in s["variantes"]:
            score = fuzz.token_set_ratio(q, _norm(v)) - 5
            if score > best[0]:
                best = (score, s["proyecto"])
    return (best[1], best[0]) if best[0] >= WEAK_MATCH else (None, best[0])


def propose_day(
    fecha: str,
    pattern: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    evidence: list[Evidence],
    projects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Lineas propuestas para un dia: lo que falte del patron + lo que respalda la evidencia.

    * Una evidencia (reunion grabada o evento) que **cubre** una franja del patron (al menos la
      mitad de la franja y con una duracion parecida) la sustituye, con el proyecto de la franja.
    * Una evidencia corta dentro de una franja larga no la toca (ese rato ya esta en la franja).
    * Una evidencia fuera de las franjas solo se propone si su titulo se parece a un proyecto.
    * Solo sale **marcada** una linea con evidencia cuyo proyecto es seguro: la franja que cubre
      o un parecido fuerte del titulo con un proyecto. El patron, nunca.
    """
    out: list[dict[str, Any]] = []
    covered = set()
    for i, s in enumerate(pattern):
        for ln in existing:
            same_project = (ln.get("proyecto") or {}).get("id") == s["proyecto"]["id"]
            rng = time_range(str(ln.get("descripcion") or ""))
            if same_project and (
                (s["inicio"] and rng == (s["inicio"], s["fin"]))
                or (not s["inicio"] and not rng and _close(ln.get("horas"), s["horas"]))
            ):
                covered.add(i)
    used_slots: set[int] = set()
    for ev in evidence:
        hours = max(MIN_LINE_H, round_quarter(ev.horas))
        span = None
        if ev.inicio:
            a = minutes(ev.inicio)
            span = (a, a + round(hours * 60))
        slot_idx = None
        inside_slot = False
        if span is not None:
            for i, s in enumerate(pattern):
                if not s["inicio"]:
                    continue
                srange = (minutes(s["inicio"]), minutes(s["fin"]))
                length = max(1, srange[1] - srange[0])
                ov = _overlap(span, srange)
                if not ov:
                    continue
                similar = 0.5 <= (span[1] - span[0]) / length <= 1.5
                if ov * 2 >= length and similar and i not in covered and i not in used_slots:
                    slot_idx = i
                    break
                inside_slot = True
        if slot_idx is None and inside_slot:
            continue  # rato dentro de una franja (o de algo ya registrado): ya contado
        project, score = guess_project_scored(ev.titulo, projects, pattern)
        sure = score >= STRONG_MATCH
        if slot_idx is not None:
            used_slots.add(slot_idx)
            if not sure:  # el titulo no dice otra cosa: el proyecto de la franja que cubre
                project = pattern[slot_idx]["proyecto"]
            sure = True
        if project is None:
            continue  # sin proyecto claro no se inventa uno
        span_txt = ""
        if span is not None:
            span_txt = f" ({_hhmm(span[0])}-{_hhmm(span[1])})"
        out.append(
            {
                "fecha": fecha,
                "proyecto_id": project["id"],
                "proyecto": project.get("nombre"),
                "tarea_id": None,
                "tarea": None,
                "horas": hours,
                "descripcion": f"{ev.titulo}{span_txt}"[:200],
                "origen": ev.origen,
                "checked": sure,
                "variantes": [],
            }
        )
    for i, s in enumerate(pattern):
        if i in covered or i in used_slots:
            continue
        out.append(
            {
                "fecha": fecha,
                "proyecto_id": s["proyecto"]["id"],
                "proyecto": s["proyecto"].get("nombre"),
                "tarea_id": (s["tarea"] or {}).get("id"),
                "tarea": (s["tarea"] or {}).get("nombre"),
                "horas": s["horas"],
                "descripcion": s["descripcion"],
                "origen": "patron",
                "checked": False,
                "variantes": s["variantes"],
            }
        )
    out.sort(key=lambda ln: _start_of(ln["descripcion"]))
    return out


def _close(a: object, b: float) -> bool:
    try:
        return abs(float(a) - b) < 0.01  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _hhmm(total: int) -> str:
    total %= 24 * 60
    return f"{total // 60}:{total % 60:02d}"


def _start_of(desc: str) -> int:
    rng = time_range(desc)
    return minutes(rng[0]) if rng else 24 * 60


# ============================================================================== validacion


class LineError(ValueError):
    """Linea de horas no valida. ``str(exc)`` es la clave del motivo (``horas``, ``futuro``...)."""


ProjectLookup = Callable[[int], dict[str, Any] | None]
TaskLookup = Callable[[int], dict[str, Any] | None]


def validate_line(
    raw: dict[str, Any],
    *,
    today: date,
    project: ProjectLookup,
    task: TaskLookup | None = None,
    day_total: float = 0.0,
    explicit: bool = False,
) -> dict[str, Any]:
    """Linea normalizada (``fecha``, ``proyecto_id``, ``tarea_id``, ``horas``, ``descripcion``)
    o ``LineError``. ``day_total`` = horas que ya hay ese dia (Odoo + lo aceptado antes)."""
    day = parse_day(raw.get("fecha"))
    if day is None:
        raise LineError("fecha")
    if day > today:
        raise LineError("futuro")
    try:
        hours = round_quarter(float(raw.get("horas")))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise LineError("horas") from exc
    if not MIN_LINE_H <= hours <= MAX_LINE_H:
        raise LineError("horas")
    if not explicit and day_total + hours > MAX_DAY_H + 1e-9:
        raise LineError("dia_lleno")
    pid = raw.get("proyecto_id")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise LineError("proyecto")
    proj = project(pid)
    if proj is None or not proj.get("activo", True) or not proj.get("horas", True):
        raise LineError("proyecto")
    tid = raw.get("tarea_id")
    task_name = None
    if tid not in (None, False, 0):
        if isinstance(tid, bool) or not isinstance(tid, int) or task is None:
            raise LineError("tarea")
        t = task(tid)
        if t is None or (t.get("proyecto") or {}).get("id") != pid or not t.get("abierta", True):
            raise LineError("tarea")
        task_name = t.get("nombre")
    else:
        tid = None
    desc = " ".join(str(raw.get("descripcion") or "").split())[:200]
    return {
        "fecha": day.isoformat(),
        "proyecto_id": pid,
        "proyecto": proj.get("nombre"),
        "tarea_id": tid,
        "tarea": task_name,
        "horas": hours,
        "descripcion": desc or str(proj.get("nombre") or "/"),
    }


def same_line(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Una linea igual ya registrada (mismo dia, proyecto, horas y descripcion)."""
    pa = a.get("proyecto_id") or (a.get("proyecto") or {}).get("id")
    pb = b.get("proyecto_id") or (b.get("proyecto") or {}).get("id")
    return (
        str(a.get("fecha")) == str(b.get("fecha"))
        and pa == pb
        and abs(float(a.get("horas") or 0) - float(b.get("horas") or 0)) < 0.01
        and _norm(str(a.get("descripcion") or "")) == _norm(str(b.get("descripcion") or ""))
    )
