"""«Conocer tu Odoo»: la Secretaria explora el Odoo conectado por su cuenta (solo lectura).

Cuando se conecta un Odoo (o «Probar» funciona con una conexion nueva), una vez por semana en
segundo plano (nunca grabando) y con «Actualizar» en Ajustes, Micnaga lee **sin escribir nada**
(cliente ``read_only``: las escrituras se rechazan antes de salir a la red) y solo de los modelos
que el perfil de capacidades (``capabilities``) o ``check_access_rights`` dicen legibles:

* CRM: etapas (en orden), equipos de venta, motivos de perdida y como se nombran las
  oportunidades (patron deducido de una muestra de titulos);
* clientes con mas actividad reciente (solo nombres), proyectos activos con su cliente, etapas de
  tareas y categorias de producto;
* el patron de partes de horas (``timesheets``) y el nombre y cargo del usuario.

Escribe por conexion ``<datos>/secretaria/odoo/<clave>/contexto.md`` (<= ``MAX_LINES`` lineas, lo
incluye ``learn.load_context``) y ``estado.json`` (lo ultimo visto + lo que ya se conocia). Sin
correos, telefonos, importes ni direcciones: cada nombre pasa el mismo filtro que ``learned.md``.

**Diferencias:** el primer escaneo es la base (solo una linea resumen en ``learned.md``). Despues,
etapas, proyectos y clientes que no se habian visto nunca entran en ``learned.md`` con fuente
``odoo`` (con historial y «Olvidar»). Lo que desaparece sale de ``contexto.md``. Una seccion que no
se pudo leer cuenta como «desconocida», no como «vacia».
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STATE_VERSION = 1
STATE_FILE = "estado.json"
CONTEXT_FILE = "contexto.md"
MAX_AGE = timedelta(days=7)
MAX_LINES = 60
RECENT_DAYS = 90
MAX_NAME = 80
CAPS = {
    "etapas": 15,
    "equipos": 10,
    "motivos": 10,
    "clientes": 12,
    "proyectos": 20,
    "etapas_tareas": 15,
    "categorias": 12,
    "ejemplos": 5,
}
MAX_SEEN = 400
MAX_NEW_PER_SCAN = 12
LEADS_SAMPLE = 200
STAGES_PREFIX = "Etapas del CRM en Odoo:"
NAMING_PREFIX = "Nombres de oportunidad en Odoo:"
BASELINE_PREFIX = "Conoce tu Odoo:"
SOURCE = "odoo"

Stop = Callable[[], bool]


# ============================================================================== rutas


def connection_key_for(cfg: Any) -> str | None:
    """Clave de la conexion configurada (``cfg`` = ``config.secretaria``) o ``None``."""
    from micnaga.brain.capabilities import connection_key

    if not (cfg.odoo_url and cfg.odoo_db and cfg.odoo_login):
        return None
    return connection_key(cfg.odoo_url, cfg.odoo_db, cfg.odoo_login)


def folder(key: str) -> Path:
    from micnaga.brain.learn import secretaria_dir

    if not re.fullmatch(r"[0-9a-f]{8,64}", key):
        raise ValueError("bad connection key")
    return secretaria_dir() / "odoo" / key


def context_path(key: str) -> Path:
    return folder(key) / CONTEXT_FILE


def load_state(key: str) -> dict[str, Any] | None:
    try:
        data = json.loads((folder(key) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return None
    return data


def context_text(key: str | None) -> str:
    """El ``contexto.md`` de esta conexion (vacio si no hay)."""
    if not key:
        return ""
    try:
        return context_path(key).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def is_stale(state: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    if not state:
        return True
    try:
        when = datetime.fromisoformat(str(state.get("scanned_at")))
    except ValueError:
        return True
    return (now or datetime.now()) - when > MAX_AGE


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ============================================================================== privacidad


def safe_name(value: object) -> str | None:
    """Un nombre apto para el contexto: sin correos, telefonos, importes ni secretos."""
    from micnaga.brain.learn import _AMOUNT, _EMAIL, _PHONE, _SECRET

    if isinstance(value, list | tuple) and len(value) == 2:  # many2one [id, nombre]
        value = value[1]
    if not isinstance(value, str):
        return None
    text = " ".join(value.replace("\n", " ").split()).strip(" -·#<>")
    if len(text) < 2:
        return None
    if len(text) > MAX_NAME:
        text = text[: MAX_NAME - 1].rstrip() + "…"
    for rx in (_EMAIL, _PHONE, _AMOUNT, _SECRET):
        if rx.search(text):
            return None
    return text


def _names(values: list[Any], cap: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        n = safe_name(v)
        if n is None or n.casefold() in seen:
            continue
        seen.add(n.casefold())
        out.append(n)
        if len(out) >= cap:
            break
    return out


# ============================================================================== escaneo


class _Scan:
    def __init__(self, client: Any, profile: dict[str, Any], today: datetime, stop: Stop):
        self.c = client
        self.profile = profile
        self.today = today
        self.stop = stop
        self.unknown: list[str] = []

    def readable(self, model: str) -> bool:
        """Modelos del perfil: lo que dice el perfil; el resto: ``check_access_rights``."""
        from micnaga.brain import capabilities

        if model in capabilities.MODELS.values():
            return capabilities.can(self.profile, model, "read")
        return bool(self.c.can(model, "read"))

    def rows(
        self, model: str, domain: list[Any], fields: list[str], *, limit: int, order: str | None
    ) -> list[dict[str, Any]]:
        if self.stop():
            raise ScanStopped
        return list(self.c.search_read(model, domain, fields, limit=limit, order=order))

    def section(self, name: str, fn: Callable[[], Any]) -> Any:
        """Cada seccion por separado: si falla (campo que no existe...) queda «desconocida»."""
        from micnaga.brain.odoo import OdooError

        try:
            return fn()
        except ScanStopped:
            raise
        except OdooError as exc:
            if exc.kind == "offline":
                raise
            log.info("odoo scan: %s unreadable (%s)", name, exc.kind)
        except Exception:
            log.info("odoo scan: %s failed", name, exc_info=True)
        self.unknown.append(name)
        return None

    # ---- CRM

    def stages(self) -> list[str] | None:
        if not self.readable("crm.stage"):
            return None
        rows = self.rows("crm.stage", [], ["name", "sequence"], limit=50, order="sequence")
        return _names([r.get("name") for r in rows], CAPS["etapas"])

    def teams(self) -> list[str] | None:
        if not self.readable("crm.team"):
            return None
        rows = self.rows("crm.team", [], ["name"], limit=30, order="sequence")
        return _names([r.get("name") for r in rows], CAPS["equipos"])

    def lost_reasons(self) -> list[str] | None:
        if not self.readable("crm.lost.reason"):
            return None
        rows = self.rows("crm.lost.reason", [], ["name"], limit=30, order="name")
        return _names([r.get("name") for r in rows], CAPS["motivos"])

    def leads(self) -> list[dict[str, Any]] | None:
        """Oportunidades recientes: solo titulo, cliente y fecha (nada de importes)."""
        if not self.readable("crm.lead"):
            return None
        since = (self.today - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        return self.rows(
            "crm.lead",
            [("type", "=", "opportunity"), ("write_date", ">=", since)],
            ["name", "partner_id", "write_date"],
            limit=LEADS_SAMPLE,
            order="write_date desc",
        )

    def sales(self) -> list[dict[str, Any]] | None:
        if not self.readable("sale.order"):
            return None
        since = (self.today - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        return self.rows(
            "sale.order",
            [("date_order", ">=", since)],
            ["partner_id"],
            limit=LEADS_SAMPLE,
            order="date_order desc",
        )

    # ---- proyectos

    def projects(self) -> list[dict[str, Any]] | None:
        if not self.readable("project.project"):
            return None
        rows = self.rows(
            "project.project",
            [("active", "=", True)],
            ["name", "partner_id"],
            limit=80,
            order="name",
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            name = safe_name(r.get("name"))
            if name is None:
                continue
            out.append({"nombre": name, "cliente": safe_name(r.get("partner_id"))})
        return out[: CAPS["proyectos"]]

    def task_stages(self) -> list[str] | None:
        if not self.readable("project.task.type"):
            return None
        rows = self.rows("project.task.type", [], ["name", "sequence"], limit=60, order="sequence")
        return _names([r.get("name") for r in rows], CAPS["etapas_tareas"])

    def categories(self) -> list[str] | None:
        if not self.readable("product.category"):
            return None
        rows = self.rows("product.category", [], ["complete_name"], limit=60, order="complete_name")
        names = [r.get("complete_name") for r in rows]
        names = [n for n in names if not (isinstance(n, str) and n.strip() in ("All", "Todos"))]
        return _names(names, CAPS["categorias"])

    def hours(self) -> str | None:
        from micnaga.brain import capabilities, timesheets

        if capabilities.features(self.profile).get("horas") is None:
            return None
        if self.stop():
            raise ScanStopped
        since = (self.today - timedelta(days=35)).date().isoformat()
        lines = self.c.timesheet_lines(since, self.today.date().isoformat())
        text = timesheets.pattern_summary(timesheets.learn_pattern(lines))
        return text[len(timesheets.LEARN_PREFIX) :].strip() or None if text else None

    def me(self) -> dict[str, str]:
        name = safe_name(str(self.profile.get("user") or "")) or ""
        role = ""
        if self.readable("hr.employee"):
            rows = self.rows(
                "hr.employee",
                [("user_id", "=", self.c.uid)],
                ["job_title", "department_id"],
                limit=1,
                order=None,
            )
            if rows:
                job = safe_name(rows[0].get("job_title")) or ""
                dept = safe_name(rows[0].get("department_id")) or ""
                role = " · ".join(x for x in (job, dept) if x)
        return {"nombre": name, "cargo": role}


class ScanStopped(Exception):
    """Empezo una grabacion: se deja para mas tarde (nada escrito)."""


def naming_pattern(titles: list[str], customers: list[str | None]) -> str | None:
    """Patron de nombres de oportunidad a partir de una muestra (determinista)."""
    if len(titles) < 3:
        return None
    seps = [" · ", " - ", " | ", " / ", ": "]
    counts = Counter(s for t in titles for s in seps if s in t)
    sep, n_sep = counts.most_common(1)[0] if counts else ("", 0)
    with_cust = 0
    cust_first = 0
    for t, c in zip(titles, customers, strict=False):
        if not c:
            continue
        key = c.casefold().split(",")[0].strip()[:20]
        low = t.casefold()
        if key and key in low:
            with_cust += 1
            if low.index(key) < max(1, len(low) // 3):
                cust_first += 1
    total = len(titles)
    parts: list[str] = []
    if n_sep >= total * 0.5:
        if with_cust >= total * 0.5:
            shape = (
                f"«cliente{sep}asunto»" if cust_first >= with_cust / 2 else f"«asunto{sep}cliente»"
            )
        else:
            shape = f"«…{sep}…»"
        parts.append(f"suelen ser {shape} ({n_sep} de {total})")
    elif with_cust >= total * 0.5:
        parts.append(f"suelen llevar el nombre del cliente ({with_cust} de {total})")
    else:
        parts.append(f"sin un patron fijo ({total} revisadas)")
    return "; ".join(parts)


def scan(
    client: Any,
    *,
    now: datetime | None = None,
    stop: Stop | None = None,
) -> dict[str, Any]:
    """Explora el Odoo conectado (solo lectura). Devuelve la instantanea (sin escribir disco)."""
    from micnaga.brain import capabilities

    client.read_only = True  # doble cerrojo: nada de escribir durante el escaneo
    when = now or datetime.now()
    halt: Stop = stop or (lambda: False)
    if client.uid is None:
        client.authenticate()  # un login malo o sin red: falla aqui, antes de nada
    profile = capabilities.ensure(client, now=when)
    s = _Scan(client, profile, when, halt)

    etapas = s.section("etapas", s.stages)
    equipos = s.section("equipos", s.teams)
    motivos = s.section("motivos", s.lost_reasons)
    leads = s.section("oportunidades", s.leads)
    sales = s.section("ventas", s.sales)
    proyectos = s.section("proyectos", s.projects)
    etapas_tareas = s.section("etapas_tareas", s.task_stages)
    categorias = s.section("categorias", s.categories)
    horas = s.section("horas", s.hours)
    me = s.section("usuario", s.me) or {"nombre": "", "cargo": ""}

    # clientes: actividad reciente en CRM y ventas; si no se pueden leer, los de los proyectos
    activity: Counter[str] = Counter()
    for r in [*(leads or []), *(sales or [])]:
        n = safe_name(r.get("partner_id"))
        if n:
            activity[n] += 1
    if not activity and proyectos:
        for p in proyectos:
            if p.get("cliente"):
                activity[str(p["cliente"])] += 1
    clientes: list[str] | None = [n for n, _c in activity.most_common(CAPS["clientes"])]
    if leads is None and sales is None and proyectos is None:
        clientes = None

    nombres: dict[str, Any] | None = None
    if leads:
        titles = [safe_name(r.get("name")) for r in leads]
        pairs = [(t, safe_name(r.get("partner_id"))) for t, r in zip(titles, leads, strict=True)]
        pairs = [(t, c) for t, c in pairs if t]
        pattern = naming_pattern([str(t) for t, _ in pairs], [c for _, c in pairs])
        nombres = {
            "patron": pattern,
            "ejemplos": _names([t for t, _ in pairs], CAPS["ejemplos"]),
        }

    snapshot = {
        "usuario": me,
        "etapas": etapas,
        "equipos": equipos,
        "motivos_perdida": motivos,
        "nombres_oportunidad": nombres,
        "clientes": clientes,
        "proyectos": proyectos,
        "etapas_tareas": etapas_tareas,
        "categorias": categorias,
        "horas": horas,
        "desconocido": sorted(set(s.unknown)),
        "crm": capabilities.can(profile, "crm.lead"),
    }
    return {
        "version": STATE_VERSION,
        "key": profile.get("key"),
        "scanned_at": when.isoformat(timespec="seconds"),
        "snapshot": snapshot,
        "counts": counts(snapshot),
    }


def counts(snapshot: dict[str, Any]) -> dict[str, int]:
    return {
        "etapas": len(snapshot.get("etapas") or []),
        "clientes": len(snapshot.get("clientes") or []),
        "proyectos": len(snapshot.get("proyectos") or []),
    }


# ============================================================================== contexto.md


def render(state: dict[str, Any]) -> str:
    """El ``contexto.md`` (<= ``MAX_LINES`` lineas)."""
    snap = state.get("snapshot") or {}
    day = str(state.get("scanned_at") or "")[:10]
    lines = [
        "## Tu Odoo (lo que sabe la Secretaria)",
        f"<!-- Lo escribe Micnaga leyendo Odoo (solo lectura), {day}. Sin correos, telefonos ni"
        " importes. Se rehace cada semana: no lo edites a mano. -->",
    ]
    me = snap.get("usuario") or {}
    if me.get("nombre"):
        who = me["nombre"] + (f" ({me['cargo']})" if me.get("cargo") else "")
        lines.append(f"- Usuario conectado: {who}")

    crm: list[str] = []
    if snap.get("etapas"):
        crm.append("- Etapas (en orden): " + " → ".join(snap["etapas"]))
    if snap.get("equipos"):
        crm.append("- Equipos de venta: " + ", ".join(snap["equipos"]))
    if snap.get("motivos_perdida"):
        crm.append("- Motivos de pérdida: " + ", ".join(snap["motivos_perdida"]))
    nom = snap.get("nombres_oportunidad") or {}
    if nom.get("patron"):
        crm.append("- Nombres de oportunidad: " + str(nom["patron"]))
    if nom.get("ejemplos"):
        crm.append("- Ejemplos: " + "; ".join(f"«{x}»" for x in nom["ejemplos"]))
    if crm:
        lines += ["", "### CRM", *crm]

    if snap.get("clientes"):
        lines += ["", "### Clientes con más actividad reciente", "- " + ", ".join(snap["clientes"])]

    if snap.get("proyectos"):
        lines += ["", "### Proyectos activos"]
        for p in snap["proyectos"]:
            lines.append(f"- {p['nombre']}" + (f" ({p['cliente']})" if p.get("cliente") else ""))
    extra: list[str] = []
    if snap.get("etapas_tareas"):
        extra.append("- Etapas de tareas: " + " → ".join(snap["etapas_tareas"]))
    if snap.get("categorias"):
        extra.append("- Categorías de producto: " + ", ".join(snap["categorias"]))
    if snap.get("horas"):
        extra.append("- Partes de horas habituales: " + str(snap["horas"]))
    if extra:
        lines += ["", "### Trabajo", *extra]
    if len(lines) > MAX_LINES:
        lines = [*lines[: MAX_LINES - 1], "- …"]
    return "\n".join(lines) + "\n"


# ============================================================================== diferencias


def _norm(text: str) -> str:
    from micnaga.brain.learn import _norm as norm

    return norm(text)


def _seen(prev: dict[str, Any] | None, kind: str) -> set[str] | None:
    """Lo que ya se conocia de ``kind`` (``None`` si nunca se pudo leer: es la base)."""
    if not prev:
        return None
    raw = (prev.get("conocido") or {}).get(kind)
    return {_norm(x) for x in raw} if isinstance(raw, list) else None


def diff(prev: dict[str, Any] | None, state: dict[str, Any]) -> list[str]:
    """Hechos nuevos para ``learned.md`` (vacio en la primera vez: es la base)."""
    snap = state["snapshot"]
    facts: list[str] = []
    stage_seen = _seen(prev, "etapas")
    if stage_seen is not None and snap.get("etapas"):
        new = [x for x in snap["etapas"] if _norm(x) not in stage_seen]
        if new:
            facts.append(f"Etapa nueva en el CRM de Odoo: {', '.join(new)}")
    proj_seen = _seen(prev, "proyectos")
    if proj_seen is not None and snap.get("proyectos"):
        for p in snap["proyectos"]:
            if _norm(p["nombre"]) in proj_seen:
                continue
            cust = f" (cliente {p['cliente']})" if p.get("cliente") else ""
            facts.append(f"{p['nombre']}{cust}: proyecto nuevo en Odoo")
    cust_seen = _seen(prev, "clientes")
    if cust_seen is not None and snap.get("clientes"):
        new = [x for x in snap["clientes"] if _norm(x) not in cust_seen]
        if new:
            facts.append("Clientes con actividad nueva en Odoo: " + ", ".join(new))
    return facts[:MAX_NEW_PER_SCAN]


def _known(prev: dict[str, Any] | None, snap: dict[str, Any]) -> dict[str, list[str]]:
    """Todo lo que se ha visto alguna vez (para no reaprender lo que entra y sale del top)."""
    old = (prev or {}).get("conocido") or {}
    out: dict[str, list[str]] = {}
    current = {
        "etapas": snap.get("etapas"),
        "proyectos": [p["nombre"] for p in snap["proyectos"]] if snap.get("proyectos") else None,
        "clientes": snap.get("clientes"),
    }
    for kind, now_list in current.items():
        before = old.get(kind) if isinstance(old.get(kind), list) else None
        if now_list is None and before is None:
            continue  # nunca legible: sigue sin base
        merged = list(before or [])
        have = {_norm(x) for x in merged}
        for x in now_list or []:
            if _norm(x) not in have:
                merged.append(x)
                have.add(_norm(x))
        out[kind] = merged[-MAX_SEEN:]
    return out


def baseline_facts(state: dict[str, Any]) -> list[tuple[str, str]]:
    """Lineas ``(prefijo, hecho)`` que se mantienen al dia en ``learned.md``."""
    snap = state["snapshot"]
    out: list[tuple[str, str]] = []
    c = state.get("counts") or {}
    bits = [
        f"{c.get('etapas', 0)} etapas" if snap.get("etapas") else "",
        f"{c.get('clientes', 0)} clientes activos" if snap.get("clientes") else "",
        f"{c.get('proyectos', 0)} proyectos" if snap.get("proyectos") else "",
    ]
    bits = [b for b in bits if b]
    if bits:
        out.append((BASELINE_PREFIX, f"{BASELINE_PREFIX} {', '.join(bits)} (ver contexto)"))
    if snap.get("etapas"):
        out.append((STAGES_PREFIX, f"{STAGES_PREFIX} " + " → ".join(snap["etapas"])))
    nom = snap.get("nombres_oportunidad") or {}
    if nom.get("patron"):
        out.append((NAMING_PREFIX, f"{NAMING_PREFIX} {nom['patron']}"))
    return out


# ============================================================================== todo junto


def refresh(
    client: Any,
    *,
    now: datetime | None = None,
    stop: Stop | None = None,
    learned_path: Path | None = None,
) -> dict[str, Any]:
    """Escanea, compara con lo anterior, guarda ``contexto.md``/``estado.json`` y aprende."""
    from micnaga.brain import learn

    state = scan(client, now=now, stop=stop)
    key = str(state["key"])
    prev = load_state(key)
    facts = diff(prev, state)
    state["conocido"] = _known(prev, state["snapshot"])
    state["nuevos"] = facts
    day = str(state["scanned_at"])[:10]
    _atomic(context_path(key), render(state))
    _atomic(folder(key) / STATE_FILE, json.dumps(state, ensure_ascii=False, indent=1))
    try:
        for prefix, fact in baseline_facts(state):
            learn.upsert_prefixed(prefix, fact, source=SOURCE, today=day, path=learned_path)
        if facts:
            learn.add_learnings(
                [{"hecho": f, "confianza": 1.0} for f in facts],
                source=SOURCE,
                exact=True,
                today=day,
                path=learned_path,
            )
    except Exception:
        log.exception("could not store what was learned from Odoo")
    log.info(
        "odoo scan done: %s, %d new facts, unknown=%s",
        state["counts"],
        len(facts),
        state["snapshot"].get("desconocido"),
    )
    return state


def status(key: str | None) -> dict[str, Any]:
    """Para Ajustes: cuando se actualizo y cuantas cosas sabe (sin red)."""
    st = load_state(key) if key else None
    if not st:
        return {"actualizado": None, "contadores": None, "crm": None}
    return {
        "actualizado": st.get("scanned_at"),
        "contadores": st.get("counts"),
        "crm": (st.get("snapshot") or {}).get("crm"),
    }
