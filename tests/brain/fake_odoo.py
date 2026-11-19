"""Un Odoo falso por XML-RPC (``/xmlrpc/2/common`` y ``/xmlrpc/2/object``) en 127.0.0.1.

Implementa lo poco que usa Micnaga: ``version``, ``authenticate``, ``execute_kw`` con
``search_read``, ``read``, ``search_count``, ``check_access_rights``, ``create``, ``write``,
``unlink`` y ``message_post``. Dominios: ``=``, ``!=``, ``ilike``, ``in``, ``child_of`` y los
prefijos ``|``/``&``. Cada llamada queda en ``calls`` (modelo, metodo) para comprobar que el
analisis solo lee.
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

SEED: dict[str, list[dict[str, Any]]] = {
    "res.users": [{"id": 7, "name": "Javier"}],
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
    "ir.model": [{"id": 300, "model": "crm.lead"}, {"id": 301, "model": "res.partner"}],
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
}
WRITE_METHODS = {"create", "write", "unlink", "message_post"}


def _norm(v: object) -> str:
    t = unicodedata.normalize("NFKD", str(v).casefold())
    return "".join(c for c in t if not unicodedata.combining(c))


class FakeOdoo:
    def __init__(self) -> None:
        self.data = copy.deepcopy(SEED)
        self.calls: list[tuple[str, str]] = []
        self.denied: set[str] = set()
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
                return True
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
                vals = dict(args[0])
                self._next += 1
                vals["id"] = self._next
                rows.append(vals)
                return self._next
            if method == "write":
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
            return v in value
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
