"""Cliente de Odoo por XML-RPC (``/xmlrpc/2/common`` + ``/xmlrpc/2/object``) con clave de API.

* Vale para Odoo 14 → 19 (XML-RPC se retira en Odoo 22 / Online 21.1; JSON-2 queda para mas
  adelante, ver design §3.2). Transporte con tiempo maximo.
* **Lectura** (lo usan las herramientas MCP): ``search_read``, ``read``, ``search_count``,
  ``fields_get``. Los campos de cada modelo salen de ``micnaga/brain/odoo_schema.json`` si existe
  (lo genera la adaptacion a vuestro Odoo, ``docs/ONBOARDING.md`` §6.2) o de los valores por
  defecto de Odoo 19 estandar (``DEFAULT_SCHEMA``).
* **Escritura**: solo ``create``/``write``/``unlink``/``message_post`` desde
  ``micnaga.brain.executor``, tras la aprobacion del usuario. El modelo nunca llega aqui.
* La clave nunca se registra ni se devuelve; ``OdooError`` lleva mensajes sin secretos.
"""

from __future__ import annotations

import contextlib
import html
import json
import logging
import re
import unicodedata
import xmlrpc.client
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TIMEOUT_S = 20.0
READ_METHODS = frozenset({"search_read", "read", "search_count", "fields_get", "name_search"})
WRITE_METHODS = frozenset({"create", "write", "unlink", "message_post"})

DEFAULT_SCHEMA: dict[str, Any] = {
    "partner": {
        "model": "res.partner",
        "fields": ["id", "name", "is_company", "parent_id", "email", "phone", "function"],
    },
    "lead": {
        "model": "crm.lead",
        "fields": [
            "id",
            "name",
            "partner_id",
            "stage_id",
            "expected_revenue",
            "probability",
            "date_deadline",
            "user_id",
            "type",
        ],
    },
    "stage": {"model": "crm.stage", "fields": ["id", "name", "sequence"]},
    "activity": {
        "model": "mail.activity",
        "fields": ["id", "summary", "date_deadline", "activity_type_id", "user_id", "res_id"],
    },
    "message": {
        "model": "mail.message",
        "fields": ["id", "date", "body", "author_id", "message_type", "subtype_id"],
    },
}
SCHEMA_FILE = Path(__file__).resolve().parent / "odoo_schema.json"
_COMPANY_SUFFIXES = re.compile(
    r"\b(s\.?\s?l\.?u?|s\.?\s?a\.?u?|s\.?\s?c\.?|sociedad limitada|slp|sll|ltd|inc|gmbh)\b\.?",
    re.IGNORECASE,
)


class OdooError(RuntimeError):
    """Error de Odoo (``kind``: ``auth`` | ``offline`` | ``access`` | ``failed``)."""

    def __init__(self, message: str, kind: str = "failed") -> None:
        super().__init__(message)
        self.kind = kind


def load_schema() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(json.dumps(DEFAULT_SCHEMA))
    if SCHEMA_FILE.is_file():
        try:
            custom = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
            for key, spec in custom.items() if isinstance(custom, dict) else []:
                if isinstance(spec, dict) and key in schema:
                    schema[key].update(spec)
        except ValueError:
            log.warning("odoo_schema.json is not valid JSON; using defaults")
    return schema


class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float) -> None:
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host: Any) -> Any:
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float) -> None:
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host: Any) -> Any:
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def normalize_name(text: str) -> str:
    """Minusculas, sin acentos ni sufijos societarios (``SL``, ``S.A.``...)."""
    t = unicodedata.normalize("NFKD", text.casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = _COMPANY_SUFFIXES.sub(" ", t)
    return " ".join(re.sub(r"[^a-z0-9@.+ ]+", " ", t).split())


def strip_html(text: object, max_len: int = 400) -> str:
    if not isinstance(text, str):
        return ""
    plain = html.unescape(re.sub(r"<[^>]+>", " ", text))
    plain = " ".join(plain.split())
    return plain[:max_len]


def m2o(value: object) -> dict[str, Any] | None:
    """``[id, "Nombre"]`` (many2one de XML-RPC) -> ``{"id", "nombre"}``."""
    if isinstance(value, list | tuple) and len(value) == 2:
        return {"id": value[0], "nombre": value[1]}
    return None


@dataclass
class OdooConfig:
    url: str
    db: str
    login: str
    key: str


class OdooClient:
    def __init__(self, cfg: OdooConfig, *, timeout_s: float = TIMEOUT_S) -> None:
        url = cfg.url.strip().rstrip("/")
        if not re.match(r"^https?://", url):
            raise OdooError("the Odoo URL must start with http:// or https://", "failed")
        self.cfg = OdooConfig(url, cfg.db.strip(), cfg.login.strip(), cfg.key)
        self.schema = load_schema()
        transport: xmlrpc.client.Transport = (
            _TimeoutSafeTransport(timeout_s)
            if url.startswith("https://")
            else _TimeoutTransport(timeout_s)
        )
        self._common = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/common", transport=transport, allow_none=True
        )
        self._object = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/object", transport=transport, allow_none=True
        )
        self.uid: int | None = None

    # ------------------------------------------------------------------ bajo nivel

    def _wrap(self, exc: Exception) -> OdooError:
        if isinstance(exc, xmlrpc.client.Fault):
            text = str(exc.faultString)
            low = text.lower()
            if "accesserror" in low or "access denied" in low or "not allowed" in low:
                return OdooError("Odoo denied access to that record or model", "access")
            if "accessdenied" in low or "session expired" in low or "invalid" in low:
                return OdooError("Odoo rejected the login or API key", "auth")
            return OdooError(f"Odoo error: {text.splitlines()[-1][:200] if text else ''}")
        if isinstance(exc, OSError | xmlrpc.client.ProtocolError | TimeoutError):
            return OdooError("Odoo is not responding", "offline")
        return OdooError(f"{type(exc).__name__}", "failed")

    def authenticate(self) -> int:
        try:
            uid = self._common.authenticate(self.cfg.db, self.cfg.login, self.cfg.key, {})
        except Exception as exc:
            raise self._wrap(exc) from exc
        if not uid:
            raise OdooError("Odoo rejected the login or API key", "auth")
        self.uid = int(uid)  # type: ignore[arg-type]
        return self.uid

    def version(self) -> dict[str, Any]:
        try:
            v = self._common.version()
        except Exception as exc:
            raise self._wrap(exc) from exc
        return v if isinstance(v, dict) else {}

    def call(
        self, model: str, method: str, args: Sequence[Any], kwargs: dict[str, Any] | None = None
    ) -> Any:
        if method not in READ_METHODS | WRITE_METHODS | {"check_access_rights", "has_access"}:
            raise OdooError(f"method {method!r} is not allowed")
        if self.uid is None:
            self.authenticate()
        try:
            return self._object.execute_kw(
                self.cfg.db, self.uid, self.cfg.key, model, method, list(args), kwargs or {}
            )
        except Exception as exc:
            raise self._wrap(exc) from exc

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        *,
        limit: int = 10,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        kw: dict[str, Any] = {"fields": fields, "limit": limit}
        if order:
            kw["order"] = order
        out = self.call(model, "search_read", [domain], kw)
        return list(out) if isinstance(out, list) else []

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        out = self.call(model, "read", [ids], {"fields": fields})
        return list(out) if isinstance(out, list) else []

    def fields(self, key: str) -> list[str]:
        return list(self.schema[key]["fields"])

    def model(self, key: str) -> str:
        return str(self.schema[key]["model"])

    # ------------------------------------------------------------------ comprobacion

    def can_read(self, model: str) -> bool:
        """Permiso de lectura. ``check_access_rights`` (<= 17/18) o, si no existe, un
        ``search_count`` (Odoo 19 lo renombro; **sin verificar** contra un 19 real)."""
        try:
            return bool(
                self.call(model, "check_access_rights", ["read"], {"raise_exception": False})
            )
        except OdooError as exc:
            if exc.kind in ("auth", "offline"):
                raise
        try:
            self.call(model, "search_count", [[]])
            return True
        except OdooError as exc:
            if exc.kind in ("auth", "offline"):
                raise
            return False

    def probe(self) -> dict[str, Any]:
        """«Probar» de Ajustes: usuario, version y modelos legibles."""
        uid = self.authenticate()
        name = ""
        with contextlib.suppress(OdooError):
            rows = self.read("res.users", [uid], ["name"])
            name = str(rows[0]["name"]) if rows else ""
        version = ""
        with contextlib.suppress(OdooError):
            version = str(self.version().get("server_version", ""))
        readable = {}
        for key in ("partner", "lead", "stage", "activity", "message"):
            model = self.model(key)
            readable[model] = self.can_read(model)
        return {"uid": uid, "user": name, "version": version, "readable": readable}

    # ------------------------------------------------------------------ lecturas de alto nivel

    def _rank(self, rows: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
        from rapidfuzz import fuzz

        q = normalize_name(text)
        scored = []
        for r in rows:
            best = 0.0
            for f in ("name", "email", "display_name"):
                v = r.get(f)
                if isinstance(v, str) and v:
                    best = max(best, fuzz.WRatio(q, normalize_name(v)))
            scored.append((best, r))
        scored.sort(key=lambda x: -x[0])
        out = []
        for score, r in scored:
            r = dict(r)
            r["parecido"] = round(score / 100, 2)
            out.append(r)
        return out

    def _chunks(self, text: str) -> list[str]:
        """El texto entero y sus trozos significativos (``ilike`` por trozos)."""
        norm = normalize_name(text)
        words = [w for w in norm.split() if len(w) >= 3]
        chunks = [text.strip()]
        if norm and norm != text.strip().lower():
            chunks.append(norm)
        chunks += sorted(words, key=len, reverse=True)[:3]
        seen: list[str] = []
        for c in chunks:
            if c and c not in seen:
                seen.append(c)
        return seen

    def search_partners(
        self, text: str, *, companies: bool | None = None, limit: int = 8
    ) -> list[dict[str, Any]]:
        fields = self.fields("partner")
        model = self.model("partner")
        found: dict[int, dict[str, Any]] = {}
        for chunk in self._chunks(text):
            domain: list[Any] = ["|", "|", ("name", "ilike", chunk)]
            domain += [("email", "ilike", chunk), ("phone", "ilike", chunk)]
            if companies is not None:
                domain = [("is_company", "=", companies), *domain]
            for r in self.search_read(model, domain, fields, limit=limit):
                found.setdefault(int(r["id"]), r)
            if len(found) >= limit:
                break
        return [_partner(r) for r in self._rank(list(found.values()), text)[:limit]]

    def search_leads(
        self, text: str | None = None, *, partner_id: int | None = None, limit: int = 8
    ) -> list[dict[str, Any]]:
        fields = self.fields("lead")
        model = self.model("lead")
        found: dict[int, dict[str, Any]] = {}
        if partner_id is not None:
            domain: list[Any] = [
                "|",
                ("partner_id", "=", partner_id),
                ("partner_id", "child_of", partner_id),
            ]
            for r in self.search_read(model, domain, fields, limit=limit):
                found.setdefault(int(r["id"]), r)
        if text:
            for chunk in self._chunks(text):
                domain = ["|", ("name", "ilike", chunk), ("partner_id", "ilike", chunk)]
                for r in self.search_read(model, domain, fields, limit=limit):
                    found.setdefault(int(r["id"]), r)
                if len(found) >= limit:
                    break
        rows = self._rank(list(found.values()), text) if text else list(found.values())
        return [_lead(r) for r in rows[:limit]]

    def read_lead(self, lead_id: int) -> dict[str, Any] | None:
        rows = self.read(self.model("lead"), [lead_id], self.fields("lead"))
        if not rows:
            return None
        lead = _lead(rows[0])
        lead["actividades_pendientes"] = self.pending_activities(lead_id)
        lead["historial"] = self.history(self.model("lead"), lead_id, limit=5)
        return lead

    def stages(self) -> list[dict[str, Any]]:
        rows = self.search_read(
            self.model("stage"), [], self.fields("stage"), limit=50, order="sequence"
        )
        return [{"id": r["id"], "nombre": r.get("name"), "orden": r.get("sequence")} for r in rows]

    def history(self, model: str, res_id: int, *, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.search_read(
            self.model("message"),
            [("model", "=", model), ("res_id", "=", res_id)],
            self.fields("message"),
            limit=limit,
            order="date desc",
        )
        return [
            {
                "fecha": r.get("date"),
                "autor": (m2o(r.get("author_id")) or {}).get("nombre"),
                "tipo": r.get("message_type"),
                "texto": strip_html(r.get("body")),
            }
            for r in rows
            if strip_html(r.get("body"))
        ]

    def pending_activities(
        self, res_id: int, *, model: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        rows = self.search_read(
            self.model("activity"),
            [("res_model", "=", model or self.model("lead")), ("res_id", "=", res_id)],
            self.fields("activity"),
            limit=limit,
            order="date_deadline",
        )
        return [
            {
                "id": r["id"],
                "resumen": r.get("summary") or (m2o(r.get("activity_type_id")) or {}).get("nombre"),
                "fecha": r.get("date_deadline"),
                "tipo": (m2o(r.get("activity_type_id")) or {}).get("nombre"),
                "responsable": (m2o(r.get("user_id")) or {}).get("nombre"),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ escrituras (ejecutor)

    def create(self, model: str, values: dict[str, Any]) -> int:
        out = self.call(model, "create", [values])
        if isinstance(out, list):
            out = out[0] if out else 0
        return int(out)

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        return bool(self.call(model, "write", [ids, values]))

    def unlink(self, model: str, ids: list[int]) -> bool:
        return bool(self.call(model, "unlink", [ids]))

    def message_post(self, model: str, res_id: int, body_html: str) -> int:
        """Nota interna (subtipo nota: no envia correo a los seguidores)."""
        out = self.call(
            model,
            "message_post",
            [[res_id]],
            {"body": body_html, "message_type": "comment", "subtype_xmlid": "mail.mt_note"},
        )
        if isinstance(out, list):
            out = out[0] if out else 0
        return int(out or 0)

    def model_id(self, model: str) -> int | None:
        rows = self.search_read("ir.model", [("model", "=", model)], ["id"], limit=1)
        return int(rows[0]["id"]) if rows else None


def _partner(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r.get("id"),
        "nombre": r.get("name"),
        "es_empresa": bool(r.get("is_company")),
        "empresa": m2o(r.get("parent_id")),
        "email": r.get("email") or None,
        "telefono": r.get("phone") or None,
        "cargo": r.get("function") or None,
        "parecido": r.get("parecido"),
    }


def _lead(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r.get("id"),
        "nombre": r.get("name"),
        "cliente": m2o(r.get("partner_id")),
        "etapa": m2o(r.get("stage_id")),
        "importe": r.get("expected_revenue"),
        "probabilidad": r.get("probability"),
        "fecha_cierre": r.get("date_deadline") or None,
        "comercial": m2o(r.get("user_id")),
        "parecido": r.get("parecido"),
    }


def client_from_config(
    url: str, db: str, login: str, key: str | None = None, *, timeout_s: float = TIMEOUT_S
) -> OdooClient | None:
    """Cliente con la clave del Administrador de credenciales (o ``None`` si falta algo)."""
    if key is None:
        from micnaga.brain import secrets

        key = secrets.get(secrets.ODOO_KEY)
    if not (url.strip() and db.strip() and login.strip() and key):
        return None
    return OdooClient(OdooConfig(url, db, login, key), timeout_s=timeout_s)
