"""Un Odoo falso por XML-RPC (``/xmlrpc/2/common`` y ``/xmlrpc/2/object``) en 127.0.0.1.

Implementa lo poco que usa Micnaga: ``version``, ``authenticate``, ``execute_kw`` con
``search_read``, ``read``, ``search_count``, ``check_access_rights``, ``create``, ``write``,
``unlink`` y ``message_post``. Dominios: ``=``, ``!=``, ``ilike``, ``in``, ``not in``, ``>=``,
``<=``, ``child_of`` y los prefijos ``|``/``&``. Cada llamada queda en ``calls`` (modelo, metodo)
para comprobar que el analisis solo lee; los ``context`` de ``create`` en ``contexts``.

Negocio (datos inventados): partes de horas con un patron diario hasta el 2026-11-04 (el
2026-11-02 sin horas: «probablemente libre»), proyectos, tareas, calendario, gastos y modulos.
``access`` (modelo -> operaciones permitidas) imita ``check_access_rights``; un ``create`` o
``write`` sin ese permiso falla como en Odoo.
"""

from __future__ import annotations

import copy
import socketserver
import threading
import unicodedata
from typing import Any
from xmlrpc.server import (
    MultiPathXMLRPCServer,
    SimpleXMLRPCDispatcher,
    SimpleXMLRPCRequestHandler,
)

DB, LOGIN, KEY, UID = "bitnaga_test", "javier@example.com", "test-odoo-key", 7

PROJECT_TRAINING, PROJECT_ATLAS, PROJECT_INTERNAL, PROJECT_CLOSED = 5, 6, 4, 9
PARTNER_ME = 3


def _seed_timesheets() -> list[dict[str, Any]]:
    """Patron diario L-V del 2026-10-05 al 2026-11-04 (sin el 12-10 ni el 02-11)."""
    from datetime import date, timedelta

    rows: list[dict[str, Any]] = []
    day = date(2026, 10, 5)
    n = 0
    topics = ["Mejora de materiales", "Preparación de sesiones", "Documentación"]
    while day <= date(2026, 11, 4):
        if day.weekday() < 5 and day not in (date(2026, 10, 12), date(2026, 11, 2)):
            for proj, hours, name in (
                (PROJECT_TRAINING, 4.5, "Formación a técnicos (9:00-13:30)"),
                (PROJECT_TRAINING, 0.5, "Reunión de seguimiento (13:30-14:00)"),
                (PROJECT_TRAINING, 2.0, f"{topics[n % 3]} (15:00-17:00)"),
                (PROJECT_ATLAS, 1.0, ["Desarrollo de Atlas", "Revisión de propuesta"][n % 2]),
            ):
                rows.append(
                    {
                        "id": 50_000 + len(rows),
                        "date": day.isoformat(),
                        "project_id": proj,
                        "task_id": False,
                        "employee_id": 70,
                        "user_id": UID,
                        "unit_amount": hours,
                        "name": name,
                    }
                )
            n += 1
        day += timedelta(days=1)
    return rows


SEED: dict[str, list[dict[str, Any]]] = {
    "res.users": [{"id": 7, "name": "Javier", "partner_id": PARTNER_ME, "tz": "Europe/Madrid"}],
    "hr.employee": [{"id": 70, "name": "Javier"}],
    "account.analytic.line": _seed_timesheets(),
    "project.project": [
        {
            "id": PROJECT_TRAINING,
            "name": "Formación Ayuntamiento",
            "active": True,
            "allow_timesheets": True,
            "partner_id": False,
        },
        {
            "id": PROJECT_ATLAS,
            "name": "Plataforma Atlas",
            "active": True,
            "allow_timesheets": True,
            "partner_id": 41,
        },
        {
            "id": PROJECT_INTERNAL,
            "name": "Interno",
            "active": True,
            "allow_timesheets": True,
            "partner_id": False,
        },
        {
            "id": PROJECT_CLOSED,
            "name": "Proyecto archivado",
            "active": False,
            "allow_timesheets": True,
            "partner_id": False,
        },
    ],
    "project.task": [
        {
            "id": 300,
            "name": "Migrar informes de Atlas",
            "project_id": PROJECT_ATLAS,
            "stage_id": False,
            "state": "01_in_progress",
            "date_deadline": False,
            "user_ids": [7],
        },
        {
            "id": 301,
            "name": "Tarea cerrada",
            "project_id": PROJECT_ATLAS,
            "stage_id": False,
            "state": "1_done",
            "date_deadline": False,
            "user_ids": [7],
        },
    ],
    "calendar.event": [],
    "hr.expense": [],
    "ir.module.module": [
        {"id": n, "name": m, "state": "installed"}
        for n, m in enumerate(
            ["hr_timesheet", "project", "calendar", "hr_expense", "crm", "contacts", "mail"], 1
        )
    ],
    "res.partner": [
        {
            "id": 41,
            "name": "Clínica Dental Triana",
            "is_company": True,
            "parent_id": False,
            "email": "info@dentaltriana.es",
            "phone": "954000000",
            "function": False,
        },
        {
            "id": 57,
            "name": "Carlos Ruiz Ortega",
            "is_company": False,
            "parent_id": 41,
            "email": "carlos@dentaltriana.es",
            "phone": False,
            "function": "Gerente",
        },
        {
            "id": PARTNER_ME,
            "name": "Javier",
            "is_company": False,
            "parent_id": False,
            "email": "javier@example.com",
            "phone": False,
            "function": False,
        },
        {
            "id": 58,
            "name": "Lucía Pérez",
            "is_company": False,
            "parent_id": 41,
            "email": False,
            "phone": False,
            "function": "Higienista",
        },
    ],
    "crm.stage": [
        {"id": 1, "name": "Nuevo", "sequence": 1},
        {"id": 2, "name": "Propuesta", "sequence": 2},
        {"id": 3, "name": "Negociación", "sequence": 3},
        {"id": 4, "name": "Ganado", "sequence": 4},
    ],
    "crm.lead": [
        {
            "id": 205,
            "name": "CRM + recordatorios WhatsApp · Dental Triana",
            "partner_id": 41,
            "stage_id": 2,
            "expected_revenue": 5400.0,
            "probability": 40.0,
            "date_deadline": False,
            "user_id": 7,
            "type": "opportunity",
        },
    ],
    "mail.activity": [
        {
            "id": 900,
            "summary": "Llamar a Carlos",
            "date_deadline": "2026-11-17",
            "activity_type_id": 4,
            "user_id": 7,
            "res_model": "crm.lead",
            "res_id": 205,
        },
    ],
    "mail.message": [
        {
            "id": 3000,
            "date": "2026-11-10 10:00:00",
            "body": "<p>Enviada propuesta 5.400 €</p>",
            "author_id": 7,
            "message_type": "comment",
            "subtype_id": 2,
            "model": "crm.lead",
            "res_id": 205,
        },
    ],
    "ir.model": [
        {"id": 300, "model": "crm.lead"},
        {"id": 301, "model": "res.partner"},
        {"id": 302, "model": "project.task"},
        {"id": 303, "model": "project.project"},
    ],
    "mail.activity.type": [{"id": 4, "name": "Por hacer"}],
}
M2O = {
    "parent_id": "res.partner",
    "partner_id": "res.partner",
    "stage_id": "crm.stage",
    "user_id": "res.users",
    "author_id": "res.users",
    "activity_type_id": "mail.activity.type",
    "res_model_id": "ir.model",
    "project_id": "project.project",
    "task_id": "project.task",
    "employee_id": "hr.employee",
}
M2M = {"user_ids", "partner_ids"}
WRITE_METHODS = {"create", "write", "unlink", "message_post"}


def _norm(v: object) -> str:
    t = unicodedata.normalize("NFKD", str(v).casefold())
    return "".join(c for c in t if not unicodedata.combining(c))


class FakeOdoo:
    def __init__(self) -> None:
        self.data = copy.deepcopy(SEED)
        self.calls: list[tuple[str, str]] = []
        self.denied: set[str] = set()
        self.access: dict[str, set[str]] = {}
        """modelo -> operaciones permitidas (sin entrada: todas)."""
        self.contexts: list[tuple[str, dict[str, Any]]] = []
        self.lock = threading.Lock()
        self._next = 10_000
        self.server: MultiPathXMLRPCServer | None = None
        self.thread: threading.Thread | None = None

    # ------------------------------------------------------------------ servidor

    def start(self) -> FakeOdoo:
        common = SimpleXMLRPCDispatcher(allow_none=True)
        common.register_function(lambda: {"server_version": "19.0-fake"}, "version")
        common.register_function(self.authenticate, "authenticate")
        obj = SimpleXMLRPCDispatcher(allow_none=True)
        obj.register_function(self.execute_kw, "execute_kw")

        class Server(socketserver.ThreadingMixIn, MultiPathXMLRPCServer):
            daemon_threads = True

        class Handler(SimpleXMLRPCRequestHandler):
            rpc_paths = ("/xmlrpc/2/common", "/xmlrpc/2/object")

        self.server = Server(
            ("127.0.0.1", 0), requestHandler=Handler, logRequests=False, allow_none=True
        )
        self.server.add_dispatcher("/xmlrpc/2/common", common)
        self.server.add_dispatcher("/xmlrpc/2/object", obj)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    # ------------------------------------------------------------------ rpc

    def authenticate(self, db: str, login: str, key: str, _env: Any) -> Any:
        return UID if (db, login, key) == (DB, LOGIN, KEY) else False

    def execute_kw(
        self,
        db: str,
        uid: int,
        key: str,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        from xmlrpc.client import Fault

        if (db, uid, key) != (DB, UID, KEY):
            raise Fault(1, "odoo.exceptions.AccessDenied: Access Denied")
        kwargs = kwargs or {}
        with self.lock:
            self.calls.append((model, method))
            if model in self.denied:
                raise Fault(2, "odoo.exceptions.AccessError: You are not allowed")
            rows = self.data.setdefault(model, [])
            if method == "check_access_rights":
                return self._allowed(model, str(args[0]))
            if method in ("create", "write", "unlink") and not self._allowed(
                model, "create" if method == "create" else "write"
            ):
                raise Fault(2, "odoo.exceptions.AccessError: You are not allowed to modify")
            if method == "search_count":
                return len(self._filter(rows, args[0] if args else []))
            if method == "search_read":
                found = self._filter(rows, args[0] if args else [])
                order = kwargs.get("order")
                if order:
                    field, *rest = str(order).split()
                    found.sort(key=lambda r: str(r.get(field) or ""), reverse=bool(rest))
                found = found[: int(kwargs.get("limit") or 80)]
                return [self._render(model, r, kwargs.get("fields")) for r in found]
            if method == "read":
                ids = set(args[0])
                return [
                    self._render(model, r, kwargs.get("fields")) for r in rows if r["id"] in ids
                ]
            if method == "create":
                vals = {k: self._command(v) for k, v in dict(args[0]).items()}
                if kwargs.get("context"):
                    self.contexts.append((model, dict(kwargs["context"])))
                if model == "account.analytic.line":
                    if "employee_id" in vals:  # un empleado sin acceso a hr.employee
                        raise Fault(2, "odoo.exceptions.AccessError: hr.employee")
                    vals.setdefault("employee_id", 70)
                    vals.setdefault("user_id", UID)
                self._next += 1
                vals["id"] = self._next
                rows.append(vals)
                return self._next
            if method == "write":
                if kwargs.get("context"):
                    self.contexts.append((model, dict(kwargs["context"])))
                for r in rows:
                    if r["id"] in args[0]:
                        r.update(args[1])
                return True
            if method == "unlink":
                self.data[model] = [r for r in rows if r["id"] not in args[0]]
                return True
            if method == "message_post":
                self._next += 1
                self.data["mail.message"].append(
                    {
                        "id": self._next,
                        "body": kwargs.get("body"),
                        "model": model,
                        "res_id": args[0][0],
                        "message_type": "comment",
                        "author_id": UID,
                        "date": "2026-11-19 12:00:00",
                        "subtype_id": 2,
                    }
                )
                return self._next
        raise Fault(3, f"unknown method {method}")

    def _allowed(self, model: str, op: str) -> bool:
        if model in self.denied:
            return False
        ops = self.access.get(model)
        return ops is None or op in ops

    @staticmethod
    def _command(v: Any) -> Any:
        """``[(6, 0, ids)]`` (orden x2many) -> ``ids``."""
        if isinstance(v, list) and v and isinstance(v[0], list | tuple) and v[0][0] == 6:
            return list(v[0][2])
        return v

    def _render(self, model: str, r: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
        out = {}
        for k, v in r.items():
            if fields and k not in fields and k != "id":
                continue
            if k in M2O and isinstance(v, int) and not isinstance(v, bool):
                target = next((x for x in self.data.get(M2O[k], []) if x["id"] == v), None)
                v = [v, (target or {}).get("name", "?")] if target else False
            out[k] = v
        return out

    def _match(self, r: dict[str, Any], term: Any) -> bool:
        field, op, value = term
        v = r.get(field)
        if field in M2O and op == "ilike":
            target = next((x for x in self.data.get(M2O[field], []) if x["id"] == v), None)
            v = (target or {}).get("name", "")
        if op == "=":
            return bool(v == value or (value is False and not v))
        if op == "!=":
            return bool(v != value)
        if op == "ilike":
            return bool(v) and _norm(value) in _norm(v)
        if op == "in":
            if field in M2M:
                return bool(set(v or []) & set(value))
            return v in value
        if op == "not in":
            return v not in value
        if op == ">=":
            return v is not None and v is not False and str(v) >= str(value)
        if op == "<=":
            return v is not None and v is not False and str(v) <= str(value)
        if op == "child_of":
            return bool(v == value or r.get("parent_id") == value)
        raise ValueError(op)

    def _filter(self, rows: list[dict[str, Any]], domain: list[Any]) -> list[dict[str, Any]]:
        return [r for r in rows if self._eval(r, list(domain))]

    def _eval(self, r: dict[str, Any], domain: list[Any]) -> bool:
        def parse(items: list[Any]) -> tuple[bool, list[Any]]:
            head, rest = items[0], items[1:]
            if head == "|":
                a, rest = parse(rest)
                b, rest = parse(rest)
                return a or b, rest
            if head == "&":
                a, rest = parse(rest)
                b, rest = parse(rest)
                return a and b, rest
            return self._match(r, head), rest

        ok = True
        items = domain
        while items:
            val, items = parse(items)
            ok = ok and val
        return ok

    @property
    def write_calls(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[1] in WRITE_METHODS]
