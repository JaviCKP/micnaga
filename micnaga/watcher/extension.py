"""Senal por extension de navegador (F4, spec §5 ultima fila): host de *native messaging* + lector.

Flujo::

    Meet (content script) --runtime.sendMessage--> service worker --connectNative--> micnaga-host
        {state, title, code, tab}                                     (stdio, longitud + JSON)
    micnaga-host  --escribe-->  <datos>/extension/host-<pid>.json  --lee-->  FileExtensionSource
                                (atomico, con latido)                        (ExtensionSignalSource)

* **Host** (``host_main``): lo lanza el navegador (``micnaga-host.exe`` o ``python -m
  micnaga.watcher.extension``). Lee mensajes ``uint32 little-endian + JSON UTF-8`` de stdin,
  mantiene
  el estado por pestana y lo vuelca a ``host-<pid>.json`` (escritura atomica con reintentos)
  cada vez
  que cambia y cada ``heartbeat_s``. Al cerrarse stdin (el navegador se fue) borra su fichero.
  El host **no usa la red** ni escribe nada fuera de ``<datos de la app>/extension``.
* **Lector** (``FileExtensionSource``): implementa ``ExtensionSignalSource``. ``available()`` es
  ``True`` **solo mientras hay al menos una llamada en curso** en un host VIVO (proceso existente y
  fichero refrescado hace menos de ``stale_after_s``) con una pestana no caducada. Diseno
  *fail-open*: la extension solo puede **anadir** una llamada positiva; si no esta instalada, el
  host no corre, el navegador se cierra o las heuristicas del DOM de Meet dejan de funcionar
  (nunca detecta llamada), ``available()`` es ``False`` y el vigilante sigue con sus senales
  nativas. Cada llamada expira a los ``tab_ttl_s`` sin refresco (la extension manda un latido cada
  5 s mientras dura la llamada).

Privacidad: el codigo de reunion y el titulo solo viven en ese fichero mientras dura la llamada (se
borra al colgar o al cerrar el navegador); ``DECISIONS.md`` registra esta desviacion de «no se
persiste el codigo de reunion».

Como activarlo (la app lo construye, este modulo NO se engancha solo)::

    from micnaga.watcher.extension import create_extension_source
    Watcher(config, controller, extension=create_extension_source())

Registro del host en el navegador: ver ``extension/README.md`` y
``scripts/register_native_host.ps1``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import struct
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, BinaryIO

from micnaga.watcher.sources import ExtensionSignalSource, ExtensionState

log = logging.getLogger(__name__)

HOST_NAME = "es.micnaga.host"
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 1 << 20  # 1 MiB: tope de Chrome para mensajes del host al navegador
HEARTBEAT_S = 5.0  # el host refresca su fichero; la extension manda latidos con el mismo periodo
STALE_AFTER_S = 20.0  # un fichero sin refrescar mas tiempo = host muerto
TAB_TTL_S = 90.0  # una pestana en llamada sin refrescar mas tiempo = ya no esta
STATE_GLOB = "host-*.json"
MAX_TITLE_CHARS = 200
KNOWN_BROWSERS = ("chrome.exe", "msedge.exe", "brave.exe", "vivaldi.exe", "opera.exe")
STATES = ("in_call", "not_in_call")

CODE_RE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$")

# Claves del registro de cada navegador (HKCU) donde se registra el host. Solo se documentan: el
# registro lo escribe ``scripts/register_native_host.ps1`` a peticion del usuario.
NATIVE_HOST_REGISTRY_KEYS = {
    "chrome": r"Software\Google\Chrome\NativeMessagingHosts\es.micnaga.host",
    "edge": r"Software\Microsoft\Edge\NativeMessagingHosts\es.micnaga.host",
    "brave": r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\es.micnaga.host",
}


class ProtocolError(ValueError):
    """Trama de native messaging invalida (longitud absurda, JSON roto, truncada)."""


# ----------------------------------------------------------------------------- framing


def encode_message(message: object) -> bytes:
    """``uint32`` little-endian (orden nativo en Windows) con la longitud + JSON UTF-8 compacto."""
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"message too large: {len(payload)} bytes")
    return struct.pack("<I", len(payload)) + payload


def _read_exactly(stream: BinaryIO, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            break
        data += chunk
    return data


def decode_message(stream: BinaryIO) -> Any | None:
    """Lee un mensaje; ``None`` en EOF limpio (el navegador cerro la tuberia).

    Lanza ``ProtocolError`` si la trama esta truncada, supera ``MAX_MESSAGE_BYTES`` o no es JSON.
    """
    header = _read_exactly(stream, 4)
    if not header:
        return None
    if len(header) < 4:
        raise ProtocolError("truncated length header")
    (length,) = struct.unpack("<I", header)
    if length > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"message too large: {length} bytes")
    payload = _read_exactly(stream, length)
    if len(payload) < length:
        raise ProtocolError("truncated message body")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid JSON payload: {exc}") from exc


def iter_messages(stream: BinaryIO) -> Iterator[Any]:
    """Mensajes hasta EOF. Una trama corrupta termina la iteracion con ``ProtocolError``."""
    while (message := decode_message(stream)) is not None:
        yield message


def write_message(stream: BinaryIO, message: object) -> None:
    stream.write(encode_message(message))
    stream.flush()


# ----------------------------------------------------------------------------- estado


def state_dir(base: Path | None = None) -> Path:
    """Carpeta de los ficheros de estado: ``<datos de la app>/extension``."""
    if base is not None:
        return Path(base)
    from micnaga import paths

    return paths.app_data_dir() / "extension"


def state_file(directory: Path, pid: int) -> Path:
    return directory / f"host-{pid}.json"


def _clean_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:MAX_TITLE_CHARS] or None


def _clean_code(value: object) -> str | None:
    if isinstance(value, str) and CODE_RE.match(value.strip().lower()):
        return value.strip().lower()
    return None


def _clean_tab(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return "0"
    text = str(value)[:32]
    return text or "0"


class HostState:
    """Estado en memoria del host: pestanas en llamada + metadatos del proceso."""

    def __init__(
        self,
        *,
        pid: int,
        browser: str,
        created: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.pid = pid
        self.browser = browser
        self.created = created
        self._clock = clock
        self.tabs: dict[str, dict[str, Any]] = {}

    def apply(self, message: object) -> bool:
        """Aplica un mensaje de la extension. ``False`` si no es valido (se ignora)."""
        if not isinstance(message, dict):
            return False
        state = message.get("state")
        if state not in STATES:
            return False
        tab = _clean_tab(message.get("tab"))
        if state == "not_in_call":
            self.tabs.pop(tab, None)
            return True
        self.tabs[tab] = {
            "title": _clean_title(message.get("title")),
            "code": _clean_code(message.get("code")),
            "seen_at": self._clock(),
        }
        return True

    def to_json(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION,
            "pid": self.pid,
            "created": self.created,
            "browser": self.browser,
            "updated_at": self._clock(),
            "tabs": self.tabs,
        }


def write_state_atomic(
    path: Path, data: dict[str, Any], *, retries: int = 8, delay_s: float = 0.02
) -> None:
    """Escribe ``data`` en ``path`` sin que un lector vea nunca un fichero a medias.

    En Windows ``os.replace`` falla con ``PermissionError`` si el lector lo tiene abierto en ese
    instante: se reintenta unos milisegundos.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    try:
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(delay_s)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _process_created(pid: int) -> float | None:
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def default_pid_alive(pid: int, created: float | None) -> bool:
    """``True`` si ``pid`` existe y, si se conoce, lo creo la misma hora de arranque."""
    try:
        import psutil
    except ImportError:  # sin psutil no se puede comprobar: se confia en el latido
        return True
    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            return False
        return not (created and abs(proc.create_time() - created) > 2.0)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


# ----------------------------------------------------------------------------- lector


class FileExtensionSource:
    """``ExtensionSignalSource`` que lee los ficheros ``host-<pid>.json`` del host.

    * ``available()``: hay una llamada en curso en un host vivo y reciente (``host_alive()`` solo
      mira el host). Un fichero ausente, ilegible, de otra version del protocolo, con
      ``updated_at`` antiguo o de un proceso muerto **no cuenta**.
    * ``read()``: una entrada por pestana en llamada no caducada, con ``browser`` del host.
    """

    def __init__(
        self,
        directory: Path | None = None,
        *,
        stale_after_s: float = STALE_AFTER_S,
        tab_ttl_s: float = TAB_TTL_S,
        clock: Callable[[], float] = time.time,
        pid_alive: Callable[[int, float | None], bool] = default_pid_alive,
        cache_s: float = 0.25,
    ) -> None:
        self._dir = directory
        self._stale_after_s = stale_after_s
        self._tab_ttl_s = tab_ttl_s
        self._clock = clock
        self._pid_alive = pid_alive
        self._cache_s = cache_s
        self._cached: list[dict[str, Any]] = []
        self._cached_at = float("-inf")

    @property
    def directory(self) -> Path:
        return state_dir(self._dir)

    def _live_hosts(self) -> list[dict[str, Any]]:
        mono = time.monotonic()
        if mono - self._cached_at < self._cache_s:
            return self._cached
        now = self._clock()
        hosts: list[dict[str, Any]] = []
        try:
            files = sorted(self.directory.glob(STATE_GLOB))
        except OSError:
            files = []
        for path in files:
            data = self._load(path)
            if data is None:
                continue
            updated = data["updated_at"]
            # ``updated_at`` en el futuro (reloj cambiado) tambien se trata como caducado
            if not (0 <= now - updated <= self._stale_after_s):
                continue
            if not self._pid_alive(data["pid"], data.get("created")):
                continue
            hosts.append(data)
        self._cached, self._cached_at = hosts, mono
        return hosts

    @staticmethod
    def _load(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("v") != PROTOCOL_VERSION:
            return None
        pid, updated, tabs = data.get("pid"), data.get("updated_at"), data.get("tabs")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or isinstance(updated, bool)
            or not isinstance(updated, (int, float))
            or not isinstance(tabs, dict)
            or not isinstance(data.get("browser"), str)
        ):
            return None
        return data

    def host_alive(self) -> bool:
        """Hay un host vivo y reciente (con o sin llamadas). Solo diagnostico."""
        return bool(self._live_hosts())

    def available(self) -> bool:
        return bool(self.read())

    def read(self) -> list[ExtensionState]:
        now = self._clock()
        calls: list[ExtensionState] = []
        for host in self._live_hosts():
            for tab_id, tab in host["tabs"].items():
                if not isinstance(tab, dict):
                    continue
                seen = tab.get("seen_at")
                if isinstance(seen, bool) or not isinstance(seen, (int, float)):
                    continue
                if not (0 <= now - seen <= self._tab_ttl_s):
                    continue
                calls.append(
                    ExtensionState(
                        in_call=True,
                        browser=str(host["browser"]).lower(),
                        title=_clean_title(tab.get("title")),
                        code=_clean_code(tab.get("code")),
                        extra={"tab": str(tab_id), "host_pid": host["pid"]},
                    )
                )
        return calls

    def diagnostics(self) -> dict[str, Any]:
        hosts = self._live_hosts()
        return {
            "directory": str(self.directory),
            "live_hosts": len(hosts),
            "in_call": len(self.read()),
        }


def create_extension_source(directory: Path | None = None, **kwargs: Any) -> ExtensionSignalSource:
    """Fabrica para el vigilante: ``Watcher(..., extension=create_extension_source())``."""
    return FileExtensionSource(directory, **kwargs)


def cleanup_dead_state_files(directory: Path | None = None) -> int:
    """Borra ficheros de hosts que ya no existen (crash). Devuelve cuantos."""
    removed = 0
    base = state_dir(directory)
    try:
        files = list(base.glob(STATE_GLOB))
    except OSError:
        return 0
    for path in files:
        data = FileExtensionSource._load(path)
        alive = data is not None and default_pid_alive(data["pid"], data.get("created"))
        if not alive:
            with contextlib.suppress(OSError):
                path.unlink()
                removed += 1
    return removed


# ----------------------------------------------------------------------------- host


def detect_browser(pid: int | None = None) -> str:
    """Nombre del navegador que lanzo el host (``chrome.exe``...), subiendo por los ancestros."""
    try:
        import psutil

        proc = psutil.Process(pid or os.getpid())
        parents = proc.parents()
    except Exception:
        return "unknown"
    for ancestor in parents:
        with contextlib.suppress(Exception):
            name = ancestor.name().lower()
            if name in KNOWN_BROWSERS:
                return name
    with contextlib.suppress(Exception):
        if parents:
            return str(parents[0].name()).lower()
    return "unknown"


def run_host(
    stdin: BinaryIO,
    *,
    directory: Path | None = None,
    browser: str | None = None,
    heartbeat_s: float = HEARTBEAT_S,
    clock: Callable[[], float] = time.time,
    pid: int | None = None,
) -> int:
    """Bucle del host sobre ``stdin`` hasta EOF. Devuelve 0 (EOF limpio) o 1 (trama corrupta)."""
    my_pid = pid if pid is not None else os.getpid()
    base = state_dir(directory)
    path = state_file(base, my_pid)
    created = _process_created(my_pid) or clock()
    state = HostState(
        pid=my_pid, browser=browser or detect_browser(my_pid), created=created, clock=clock
    )
    lock = threading.Lock()
    stop = threading.Event()

    def flush() -> None:
        with lock:
            snapshot = state.to_json()
        try:
            write_state_atomic(path, snapshot)
        except OSError as exc:
            log.warning("could not write %s: %s", path, exc)

    def beat() -> None:
        while not stop.wait(heartbeat_s):
            flush()

    with contextlib.suppress(Exception):
        cleanup_dead_state_files(base)
    flush()  # el host ya esta vivo (host_alive()); available() espera a una llamada
    thread = threading.Thread(target=beat, name="micnaga-host-beat", daemon=True)
    thread.start()
    code = 0
    try:
        while True:
            try:
                message = decode_message(stdin)
            except ProtocolError as exc:
                log.warning("protocol error, closing: %s", exc)
                code = 1
                break
            if message is None:
                break
            with lock:
                changed = state.apply(message)
            if changed:
                flush()
    finally:
        stop.set()
        thread.join(1.0)
        with contextlib.suppress(OSError):
            path.unlink()  # sin host no hay estado: nada de codigos de reunion en disco
    return code


def host_main(argv: list[str] | None = None) -> int:
    """Entrada de ``micnaga-host.exe`` / ``python -m micnaga.watcher.extension``.

    El navegador pasa como argumento el origen de la extension (``chrome-extension://<id>/``) y en
    Windows ademas ``--parent-window=<hwnd>``; se ignoran.
    """
    del argv
    try:
        from micnaga.logging_setup import setup_logging

        setup_logging("host")
    except Exception:  # los logs nunca deben impedir que el host arranque
        pass
    stdin = sys.stdin.buffer
    return run_host(stdin)


# ----------------------------------------------------------------------------- manifiestos


def extension_id_from_key(key_b64: str) -> str:
    """ID de una extension a partir del campo ``key`` de su ``manifest.json`` (SPKI DER en base64).

    Algoritmo de Chrome: los 16 primeros bytes del SHA-256 de la clave publica, cada nibble
    mapeado a la letra ``a``..``p``.
    """
    digest = hashlib.sha256(base64.b64decode(key_b64)).digest()[:16]
    return "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0x0F)) for b in digest)


def build_host_manifest(host_path: str, extension_id: str) -> dict[str, Any]:
    """Manifiesto del host de native messaging (se guarda como JSON y se registra en HKCU)."""
    if not re.fullmatch(r"[a-p]{32}", extension_id):
        raise ValueError("extension_id must be 32 characters in a-p")
    return {
        "name": HOST_NAME,
        "description": "Micnaga: senal de llamada de Google Meet para el vigilante",
        "path": host_path,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }


if __name__ == "__main__":
    sys.exit(host_main())
