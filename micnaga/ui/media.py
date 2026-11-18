"""Servidor HTTP local (solo 127.0.0.1) para las paginas de la UI y el audio de las reuniones.

Por que no ``file://`` ni el servidor de pywebview:

* ``fetch('i18n/es.json')`` no funciona desde ``file://`` en WebView2.
* El audio vive bajo la raiz de reuniones (``Documentos\\Micnaga``), fuera del arbol web; los
  ``<audio>`` necesitan peticiones ``Range`` (206) para poder saltar a una frase.

Seguridad (cualquier pagina web abierta en el navegador puede hacer ``fetch`` a 127.0.0.1):

* Todas las rutas llevan un **token aleatorio** por proceso (``/<token>/...``): sin el no se
  puede ni adivinar la URL de un FLAC.
* Se valida la cabecera ``Host`` (frena el DNS rebinding) y no se envia ninguna cabecera CORS.
* Solo se sirven los dos FLAC de cada reunion (lista blanca) y los ficheros bajo ``web/``;
  nada de recorrer directorios.
"""

from __future__ import annotations

import contextlib
import json
import logging
import mimetypes
import re
import secrets
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit

from micnaga import paths

log = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).resolve().parent / "web"
HOST = "127.0.0.1"

#: pista -> nombre de fichero dentro de la carpeta de la reunion
AUDIO_TRACKS: dict[str, str] = {"yo": paths.AUDIO_ME_FLAC, "remoto": paths.AUDIO_REMOTE_FLAC}

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self'; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("image/x-icon", ".ico")


def valid_folder_name(name: str) -> bool:
    """Nombre de carpeta de reunion seguro (un solo componente, sin ``..`` ni separadores)."""
    if not name or name in {".", ".."} or name.startswith("."):
        return False
    return not any(c in name for c in '/\\:*?"<>|\x00')


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Interpreta ``Range: bytes=a-b`` -> ``(inicio, fin)`` inclusivos; ``None`` = invalido.

    Una cabecera ausente devuelve el fichero entero ``(0, size - 1)``.
    """
    if size <= 0:
        return None
    if not header:
        return 0, size - 1
    m = _RANGE_RE.match(header.strip())
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    if a == "" and b == "":
        return None
    if a == "":  # sufijo: los ultimos N bytes
        n = int(b)
        if n == 0:
            return None
        return max(0, size - n), size - 1
    start = int(a)
    end = int(b) if b else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


class LocalServer:
    """Servidor de assets + audio. ``start()`` lo lanza en un hilo demonio."""

    def __init__(
        self,
        *,
        web_root: Path = WEB_ROOT,
        meetings_root: Callable[[], Path] | None = None,
        token: str | None = None,
        dev_api: dict[str, object] | None = None,
    ) -> None:
        self.web_root = web_root.resolve()
        self._meetings_root = meetings_root
        #: SOLO desarrollo/pruebas/capturas: expone objetos ``js_api`` por HTTP (``bridge=http``).
        #: La app real nunca lo activa (``None``): ahi el puente es el de pywebview.
        self.dev_api = dev_api
        self.token = token or secrets.token_urlsafe(18)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ ciclo de vida

    def start(self) -> LocalServer:
        if self._httpd is not None:
            return self
        server = self

        class Handler(_Handler):
            owner = server

        httpd = _QuietServer((HOST, 0), Handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(
            target=lambda: httpd.serve_forever(poll_interval=0.05),
            name="micnaga-ui-http",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> LocalServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("server not started")
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self.port}/{self.token}/"

    # ------------------------------------------------------------------ URLs

    def page_url(self, page: str, **params: Any) -> str:
        """URL de una pagina (``popup.html``) con parametros (``theme=dark``...)."""
        q = {k: str(v) for k, v in params.items() if v is not None and v != ""}
        return self.base_url + page + (("?" + urlencode(q)) if q else "")

    def audio_url(self, folder: str, track: str) -> str:
        if track not in AUDIO_TRACKS or not valid_folder_name(folder):
            raise ValueError("invalid audio reference")
        return f"{self.base_url}m/{quote(folder)}/{track}"

    # ------------------------------------------------------------------ resolucion

    def resolve_static(self, rel: str) -> Path | None:
        rel = unquote(rel).lstrip("/")
        if not rel or "\x00" in rel:
            return None
        target = (self.web_root / rel).resolve()
        if not target.is_relative_to(self.web_root) or not target.is_file():
            return None
        return target

    def resolve_audio(self, folder: str, track: str) -> Path | None:
        if self._meetings_root is None or track not in AUDIO_TRACKS:
            return None
        folder = unquote(folder)
        if not valid_folder_name(folder):
            return None
        root = self._meetings_root().resolve()
        target = (root / folder / AUDIO_TRACKS[track]).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return None
        return target


class _QuietServer(ThreadingHTTPServer):
    """El navegador cierra conexiones keep-alive a su aire: no es un error."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.debug("http connection error from %s", client_address, exc_info=True)


class _Handler(BaseHTTPRequestHandler):
    owner: LocalServer
    protocol_version = "HTTP/1.1"
    server_version = "Micnaga"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("http: " + format, *args)

    # -- helpers
    def _fail(self, code: HTTPStatus, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        port = self.owner.port
        return host in {f"{HOST}:{port}", f"localhost:{port}"}

    def do_POST(self) -> None:
        """``/<token>/api/<superficie>/<metodo>``: solo si ``dev_api`` esta activo."""
        try:
            registry = self.owner.dev_api
            path = urlsplit(self.path).path
            prefix = f"/{self.owner.token}/api/"
            if registry is None or not self._host_ok() or not path.startswith(prefix):
                self._fail(HTTPStatus.NOT_FOUND)
                return
            parts = path[len(prefix) :].split("/")
            if len(parts) != 2:
                self._fail(HTTPStatus.NOT_FOUND)
                return
            surface, method = parts
            obj = registry.get(surface)
            fn = (
                getattr(obj, method, None)
                if obj is not None and not method.startswith("_")
                else None
            )
            if not callable(fn):
                self._fail(HTTPStatus.NOT_FOUND)
                return
            length = int(self.headers.get("Content-Length") or 0)
            args = json.loads(self.rfile.read(length) or b"[]")
            body = json.dumps(fn(*args), ensure_ascii=False, default=str).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            return
        except Exception:
            log.exception("dev api failed")
            self._fail(HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def do_GET(self) -> None:
        self._serve(head=False)

    def _serve(self, *, head: bool) -> None:
        try:
            if not self._host_ok():
                self._fail(HTTPStatus.FORBIDDEN)
                return
            path = urlsplit(self.path).path
            prefix = f"/{self.owner.token}/"
            if not path.startswith(prefix):
                self._fail(HTTPStatus.NOT_FOUND)
                return
            rel = path[len(prefix) :]
            parts = rel.split("/")
            if len(parts) == 3 and parts[0] == "m":
                file = self.owner.resolve_audio(parts[1], parts[2])
                is_page = False
            else:
                file = self.owner.resolve_static(rel or "index.html")
                is_page = True
            if file is None:
                self._fail(HTTPStatus.NOT_FOUND)
                return
            self._send_file(file, head=head, is_page=is_page)
        except ConnectionError:  # el navegador corto la peticion
            return
        except Exception:  # pragma: no cover - defensivo
            log.exception("http handler failed")
            with contextlib.suppress(OSError):
                self._fail(HTTPStatus.INTERNAL_SERVER_ERROR)

    def _send_file(self, file: Path, *, head: bool, is_page: bool) -> None:
        size = file.stat().st_size
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in {"application/json", "text/javascript"}:
            ctype += "; charset=utf-8"
        rng = parse_range(self.headers.get("Range") if not is_page else None, size)
        if rng is None:
            if size == 0 and not self.headers.get("Range"):
                rng = (0, -1)
            else:
                self._fail(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, {"Content-Range": f"bytes */{size}"}
                )
                return
        start, end = rng
        length = end - start + 1
        partial = bool(self.headers.get("Range")) and not is_page
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if is_page and file.suffix == ".html":
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if head or length <= 0:
            return
        with file.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
