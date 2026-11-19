"""Proxy XML-RPC de **solo lectura** hacia un Odoo real (pruebas reales, nunca en la app).

La clave real vive solo en la memoria de este proceso (viene de ``ODOO_KEY``); el servidor MCP
hijo se conecta al proxy con una clave ficticia (``DUMMY_KEY``), asi la clave real no se escribe
en ningun fichero (ni en el almacen de secretos de prueba ni en el ``--mcp-config``).

Doble cerrojo: el proxy rechaza cualquier metodo que no sea de lectura **y** reenvia con un
``OdooClient(read_only=True)``. ``blocked`` registra cualquier intento de escritura.
"""

from __future__ import annotations

import socketserver
import threading
import xmlrpc.client
from typing import Any
from xmlrpc.server import MultiPathXMLRPCServer, SimpleXMLRPCDispatcher, SimpleXMLRPCRequestHandler

from micnaga.brain.odoo import CHECK_METHODS, READ_METHODS, OdooClient, OdooConfig, OdooError

DUMMY_KEY = "read-only-proxy"


class ReadOnlyOdooProxy:
    def __init__(self, url: str, db: str, login: str, key: str) -> None:
        self.db, self.login = db, login
        self._real = OdooClient(OdooConfig(url, db, login, key), read_only=True, timeout_s=30)
        self.calls: list[tuple[str, str]] = []
        self.blocked: list[tuple[str, str]] = []
        self.server: MultiPathXMLRPCServer | None = None
        self._lock = threading.Lock()

    def start(self) -> ReadOnlyOdooProxy:
        common = SimpleXMLRPCDispatcher(allow_none=True)
        common.register_function(lambda: self._real.version(), "version")
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
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def authenticate(self, db: str, login: str, key: str, _env: Any) -> Any:
        if key != DUMMY_KEY or db != self.db or login != self.login:
            return False
        with self._lock:
            return self._real.uid or self._real.authenticate()

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
        if key != DUMMY_KEY:
            raise xmlrpc.client.Fault(1, "odoo.exceptions.AccessDenied: Access Denied")
        if method not in READ_METHODS | CHECK_METHODS:
            self.blocked.append((model, method))
            raise xmlrpc.client.Fault(2, f"odoo.exceptions.AccessError: read-only proxy ({method})")
        self.calls.append((model, method))
        try:
            with self._lock:
                return self._real.call(model, method, args, kwargs)
        except OdooError as exc:
            text = "odoo.exceptions.AccessError" if exc.kind == "access" else str(exc)
            raise xmlrpc.client.Fault(3, text) from exc
