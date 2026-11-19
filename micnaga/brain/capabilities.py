"""Que puede hacer la Secretaria en el Odoo conectado (perfil de capacidades).

Al pulsar «Probar» en Ajustes y una vez al dia (si se usa), Micnaga mira **sin escribir nada**:

* que modulos estan instalados (``ir.module.module``, si es legible);
* ``check_access_rights(read|create|write)`` de cada modelo conocido (verificado en Odoo 19.0).

El perfil se guarda en ``<datos>/secretaria/odoo_capacidades.json`` por conexion (url + base +
usuario: la cuenta del jefe tiene el suyo). De el salen:

* las **herramientas MCP** que ve el modelo (sin CRM si ``crm.lead`` no es legible...);
* los **tipos de propuesta** permitidos (sin «crear contacto» si ``res.partner`` es de solo
  lectura...);
* donde se puede dejar el acta como nota interna o programar una actividad: ``message_post`` y
  ``mail.activity`` exigen permiso de **escritura** sobre el registro (``_mail_post_access`` es
  ``write`` por defecto), asi que solo se ofrecen esos destinos.
* el resumen de Ajustes («Partes de horas · Tareas · Calendario · Contactos (lectura)»).

Se decide por permisos, no por modulos: un modulo instalado sin acceso no cuenta.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROFILE_VERSION = 1
PROFILE_FILE = "odoo_capacidades.json"
MAX_AGE = timedelta(hours=24)

#: clave -> modelo de Odoo
MODELS: dict[str, str] = {
    "horas": "account.analytic.line",
    "proyectos": "project.project",
    "tareas": "project.task",
    "calendario": "calendar.event",
    "actividades": "mail.activity",
    "gastos": "hr.expense",
    "contactos": "res.partner",
    "crm": "crm.lead",
    "etapas": "crm.stage",
    "mensajes": "mail.message",
}
MODULES = (
    "hr_timesheet",
    "project",
    "calendar",
    "hr_expense",
    "crm",
    "contacts",
    "mail",
    "sale_management",
    "hr_attendance",
)
OPERATIONS = ("read", "create", "write")

#: Lo que se ensena en Ajustes, en este orden (clave de funcion -> etiqueta).
FEATURES: tuple[tuple[str, str], ...] = (
    ("horas", "Partes de horas"),
    ("tareas", "Tareas"),
    ("calendario", "Calendario"),
    ("actividades", "Actividades"),
    ("gastos", "Gastos"),
    ("contactos", "Contactos"),
    ("crm", "CRM"),
)

#: Modelos donde puede ir el acta como nota interna / una actividad (orden de preferencia).
NOTE_TARGETS = ("project.task", "crm.lead", "project.project", "res.partner")


# ============================================================================== perfil


def connection_key(url: str, db: str, login: str) -> str:
    raw = f"{url.strip().rstrip('/').lower()}|{db.strip()}|{login.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def discover(client: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Perfil de capacidades del usuario conectado. Solo lecturas y ``check_access_rights``."""
    uid = client.uid if client.uid is not None else client.authenticate()
    user = ""
    version = ""
    try:
        user = str(client.user_info().get("nombre") or "")
    except Exception:
        log.debug("could not read the user name", exc_info=True)
    try:
        version = str(client.version().get("server_version", ""))
    except Exception:
        log.debug("could not read the Odoo version", exc_info=True)
    modules = client.installed_modules(MODULES)
    access: dict[str, dict[str, bool]] = {}
    for model in MODELS.values():
        ok_read = bool(client.can(model, "read"))
        row = {"read": ok_read, "create": False, "write": False}
        if ok_read:
            for op in ("create", "write"):
                row[op] = bool(client.can(model, op))
        access[model] = row
    cfg = client.cfg
    return {
        "version": PROFILE_VERSION,
        "key": connection_key(cfg.url, cfg.db, cfg.login),
        "checked_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "uid": uid,
        "user": user,
        "odoo_version": version,
        "modules": sorted(modules) if modules is not None else None,
        "access": access,
    }


def can(profile: dict[str, Any] | None, model: str, op: str = "read") -> bool:
    if not profile:
        return False
    return bool(((profile.get("access") or {}).get(model) or {}).get(op))


def features(profile: dict[str, Any] | None) -> dict[str, str]:
    """``{funcion: "escritura" | "lectura"}`` de lo que hay (lo que no hay no sale)."""
    out: dict[str, str] = {}
    if not profile:
        return out

    def level(model: str, writes: tuple[str, ...] = ("create",)) -> str | None:
        if not can(profile, model, "read"):
            return None
        return "escritura" if all(can(profile, model, w) for w in writes) else "lectura"

    horas = level(MODELS["horas"]) if can(profile, MODELS["proyectos"]) else None
    pairs = {
        "horas": horas,
        "tareas": level(MODELS["tareas"]),
        "calendario": level(MODELS["calendario"]),
        "actividades": level(MODELS["actividades"]),
        "gastos": level(MODELS["gastos"]),
        "contactos": level(MODELS["contactos"], ("create", "write")),
        "crm": level(MODELS["crm"], ("write",)),
    }
    for key, _label in FEATURES:
        if pairs.get(key):
            out[key] = str(pairs[key])
    return out


def summary(profile: dict[str, Any] | None) -> list[dict[str, str]]:
    """Para Ajustes: ``[{"clave", "etiqueta", "nivel"}]`` en orden."""
    feats = features(profile)
    return [
        {"clave": key, "etiqueta": label, "nivel": feats[key]}
        for key, label in FEATURES
        if key in feats
    ]


def summary_text(profile: dict[str, Any] | None) -> str:
    """«Partes de horas · Tareas · Calendario · Contactos (lectura)»."""
    return " · ".join(
        s["etiqueta"] + (" (lectura)" if s["nivel"] == "lectura" else "") for s in summary(profile)
    )


def note_targets(profile: dict[str, Any] | None) -> list[str]:
    """Modelos donde se puede publicar una nota interna o crear una actividad (escritura)."""
    return [m for m in NOTE_TARGETS if can(profile, m, "read") and can(profile, m, "write")]


# ============================================================================== herramientas


_TOOL_NEEDS: dict[str, tuple[str, ...]] = {
    "buscar_contactos": ("res.partner",),
    "buscar_empresas": ("res.partner",),
    "buscar_oportunidades": ("crm.lead",),
    "leer_oportunidad": ("crm.lead",),
    "listar_etapas": ("crm.lead", "crm.stage"),
    "historial_reciente": ("mail.message",),
    "actividades_pendientes": ("mail.activity",),
    "mis_partes_de_horas": ("account.analytic.line", "project.project"),
    "proyectos": ("project.project",),
    "tareas": ("project.task",),
    "calendario": ("calendar.event",),
    "mis_actividades": ("mail.activity",),
    "mis_gastos": ("hr.expense",),
}


def tools_for(profile: dict[str, Any] | None, candidates: tuple[str, ...]) -> list[str]:
    """Herramientas de Odoo que se ofrecen al modelo (sin perfil: las de CRM de siempre)."""
    if profile is None:
        return [t for t in candidates if t in LEGACY_TOOLS]
    return [t for t in candidates if all(can(profile, m) for m in _TOOL_NEEDS.get(t, ()))]


LEGACY_TOOLS = (
    "buscar_contactos",
    "buscar_empresas",
    "buscar_oportunidades",
    "leer_oportunidad",
    "listar_etapas",
    "historial_reciente",
    "actividades_pendientes",
)


def actions_for(profile: dict[str, Any] | None, all_types: tuple[str, ...]) -> list[str]:
    """Tipos de propuesta de Odoo permitidos para este usuario."""
    if profile is None:
        return [
            t
            for t in all_types
            if t
            in (
                "vincular_a_oportunidad",
                "actualizar_oportunidad",
                "crear_contacto",
                "programar_actividad",
                "nota_en_historial",
            )
        ]
    targets = note_targets(profile)
    lead_w = can(profile, "crm.lead", "write")
    rules = {
        "vincular_a_oportunidad": lead_w,
        "actualizar_oportunidad": lead_w and can(profile, "crm.stage"),
        "crear_contacto": can(profile, "res.partner", "create"),
        "programar_actividad": can(profile, "mail.activity", "create") and bool(targets),
        "nota_en_historial": bool(targets),
        "registrar_horas": can(profile, "account.analytic.line", "create")
        and can(profile, "project.project"),
        "crear_tarea": can(profile, "project.task", "create") and can(profile, "project.project"),
        "crear_evento": can(profile, "calendar.event", "create"),
        "crear_gasto": can(profile, "hr.expense", "create"),
    }
    return [t for t in all_types if rules.get(t, False)]


# ============================================================================== disco


def _path() -> Path:
    from micnaga.brain.learn import secretaria_dir

    return secretaria_dir() / PROFILE_FILE


def _read_all() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(key: str) -> dict[str, Any] | None:
    prof = _read_all().get(key)
    if not isinstance(prof, dict) or prof.get("version") != PROFILE_VERSION:
        return None
    return prof


def save(profile: dict[str, Any]) -> None:
    data = _read_all()
    data[str(profile["key"])] = profile
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


def is_stale(profile: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    if not profile:
        return True
    try:
        when = datetime.fromisoformat(str(profile.get("checked_at")))
    except ValueError:
        return True
    return (now or datetime.now()) - when > MAX_AGE


def ensure(client: Any, *, refresh: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """Perfil guardado si es de hoy; si no (o con ``refresh``), se descubre y se guarda."""
    cfg = client.cfg
    key = connection_key(cfg.url, cfg.db, cfg.login)
    prof = None if refresh else load(key)
    if prof is not None and not is_stale(prof, now=now):
        return prof
    prof = discover(client, now=now)
    try:
        save(prof)
    except OSError:
        log.warning("could not save the Odoo capability profile")
    return prof
