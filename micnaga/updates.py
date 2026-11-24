"""Actualizaciones automaticas desde GitHub Releases (``JaviCKP/micnaga``). Solo stdlib.

Que hace
--------
* Comprueba ``/releases/latest`` (la API ya excluye borradores y prereleases; se vuelve a mirar)
  un minuto despues de arrancar, cada 12 h y cuando se pulsa «Buscar actualizaciones». Compara la
  version (``X.Y.Z``) con ``micnaga.__version__`` y respeta «Omitir esta version».
* La peticion no lleva datos del usuario: solo ``User-Agent: Micnaga/<version>`` y, si la hay, la
  ``ETag`` de la ultima respuesta (``If-None-Match``: un 304 no gasta cuota). Sin red o con el
  limite de la API agotado no se avisa de nada (solo una comprobacion pedida a mano dice «no se
  pudo comprobar»).
* Descarga del instalador ``Micnaga-Setup-<version>.exe`` a ``%TEMP%\\Micnaga-update\\<version>``
  via ``.part`` (reanuda con ``Range``, reintenta con espera creciente), solo por ``https`` y solo
  hacia GitHub (``github.com``, ``api.github.com``, ``*.githubusercontent.com``; cualquier
  redireccion a otro host aborta).
* Verificacion antes de ejecutar nada:
  1. tamano igual al publicado;
  2. SHA-256 igual al de ``SHA256SUMS.txt`` (activo de la release, formato ``sha256sum``) y/o al
     ``digest`` que GitHub calcula para cada activo. Si estan los dos deben coincidir; si no hay
     ninguno, no se instala;
  3. firma Authenticode (``Get-AuthenticodeSignature``): una firma presente debe ser valida (y del
     editor esperado, ``EXPECTED_PUBLISHER``); un instalador sin firmar se rechaza si
     ``REQUIRE_SIGNATURE`` o si la app que corre ahora esta firmada (no se baja de firmado a sin
     firmar, ``REFUSE_SIGNED_TO_UNSIGNED``).
* Instalar solo en reposo (sin grabar ni procesar): si no, queda pendiente y se instala al quedar
  libre. ``/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS``; el ``[Run]`` del
  instalador vuelve a abrir Micnaga. La version nueva muestra una vez «Novedades de la X».

Estado (no es configuracion): ``<datos de app>/updates.json`` (version omitida, ETag, ultima
comprobacion, release en cache, novedades pendientes). La configuracion solo tiene
``[updates] auto_check``.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from micnaga import __version__

log = logging.getLogger(__name__)

REPO = "JaviCKP/micnaga"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
ASSET_TEMPLATE = "Micnaga-Setup-{version}.exe"
CHECKSUMS_ASSET = "SHA256SUMS.txt"
ALLOWED_HOSTS: tuple[str, ...] = ("github.com", "api.github.com")
ALLOWED_SUFFIXES: tuple[str, ...] = (".githubusercontent.com",)
"""Activos de las releases: ``objects.githubusercontent.com`` / ``release-assets...``."""

FIRST_CHECK_DELAY_S = 60.0
CHECK_INTERVAL_S = 12 * 3600.0
IDLE_POLL_S = 20.0
"""Instalacion pendiente: cada cuanto se mira si la app ya esta en reposo."""
HTTP_TIMEOUT_S = 30.0
DOWNLOAD_ATTEMPTS = 5
BACKOFF_S = (2.0, 5.0, 15.0, 30.0)
MAX_INSTALLER_BYTES = 1 << 30
MAX_JSON_BYTES = 2 << 20
MAX_CHECKSUMS_BYTES = 64 << 10
CHUNK = 1 << 16
NOTES_MAX_CHARS = 1200
NOTES_MAX_LINES = 14

INSTALLER_ARGS: tuple[str, ...] = (
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/CLOSEAPPLICATIONS",
    "/SP-",
)

# --------------------------------------------------------------------------- firma
REQUIRE_SIGNATURE = False
"""``True`` cuando TODAS las releases salgan firmadas: un instalador sin firma no se ejecuta.
Hoy no hay certificado (DECISIONS §31.2), asi que esta apagado."""
EXPECTED_PUBLISHER = ""
"""CN del certificado de firma (p. ej. ``"Bitnaga S.L."``). Vacio = cualquier firma valida
(pero siempre la misma que la de la app instalada, si esta esta firmada)."""
REFUSE_SIGNED_TO_UNSIGNED = True
"""Si la app que corre esta firmada, nunca se instala una version sin firmar."""

STATE_FILE = "updates.json"
ENV_DISABLE = "MICNAGA_NO_UPDATES"
"""Con esta variable no se comprueba nada (ni a mano): despliegues gestionados, pruebas."""


class UpdateError(RuntimeError):
    """Fallo con una clave i18n (``updates.err.*``) que la UI traduce."""

    def __init__(self, key: str, detail: str = "") -> None:
        super().__init__(f"{key}: {detail}" if detail else key)
        self.key = key
        self.detail = detail


# ===================================================================== versiones

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(text: object) -> tuple[int, int, int] | None:
    """``"v0.1.2"`` / ``"0.1.2"`` -> ``(0, 1, 2)``. Prereleases (``-beta``) y basura -> ``None``."""
    if not isinstance(text, str):
        return None
    m = _VERSION_RE.match(text.strip())
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


# ===================================================================== releases


@dataclass(frozen=True)
class ReleaseInfo:
    """Lo que interesa de una release publicada (de la API de GitHub)."""

    version: str
    tag: str
    notes: str
    """Texto ya saneado (``sanitize_notes``): se pinta con ``textContent``."""
    published_at: str
    asset_name: str
    asset_url: str
    asset_size: int
    digest: str = ""
    """SHA-256 que GitHub calcula para el activo (``"sha256:<hex>"`` -> ``<hex>``), si lo hay."""
    checksums_url: str = ""
    """``SHA256SUMS.txt`` de la release, si se publico."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReleaseInfo | None:
        try:
            info = cls(
                version=str(data["version"]),
                tag=str(data["tag"]),
                notes=str(data.get("notes", "")),
                published_at=str(data.get("published_at", "")),
                asset_name=str(data["asset_name"]),
                asset_url=str(data["asset_url"]),
                asset_size=int(data["asset_size"]),
                digest=str(data.get("digest", "")),
                checksums_url=str(data.get("checksums_url", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return info if parse_version(info.version) else None


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def parse_release(data: object) -> ReleaseInfo | None:
    """Release de la API -> ``ReleaseInfo``; ``None`` si es borrador, prerelease, la etiqueta no
    es ``vX.Y.Z`` o no trae el instalador con el nombre exacto."""
    if not isinstance(data, Mapping) or data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name")
    ver = parse_version(tag)
    if ver is None or not isinstance(tag, str):
        return None
    version = ".".join(str(n) for n in ver)
    want = ASSET_TEMPLATE.format(version=version)
    installer: Mapping[str, Any] | None = None
    checksums = ""
    assets = data.get("assets")
    for asset in assets if isinstance(assets, list) else []:
        if not isinstance(asset, Mapping):
            continue
        name = asset.get("name")
        url = asset.get("browser_download_url")
        if not isinstance(url, str) or not url_allowed(url):
            continue
        if name == want and asset.get("state", "uploaded") == "uploaded":
            installer = asset
        elif name == CHECKSUMS_ASSET:
            checksums = url
    if installer is None:
        return None
    size = installer.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_INSTALLER_BYTES:
        return None
    digest = ""
    raw = installer.get("digest")
    if isinstance(raw, str) and raw.lower().startswith("sha256:"):
        hexd = raw.split(":", 1)[1].strip().lower()
        digest = hexd if _HEX64.match(hexd) else ""
    body = data.get("body")
    published = data.get("published_at")
    return ReleaseInfo(
        version=version,
        tag=tag,
        notes=sanitize_notes(body if isinstance(body, str) else ""),
        published_at=published if isinstance(published, str) else "",
        asset_name=want,
        asset_url=str(installer["browser_download_url"]),
        asset_size=size,
        digest=digest,
        checksums_url=checksums,
    )


def parse_checksums(text: str, name: str) -> str | None:
    """SHA-256 de ``name`` en un ``SHA256SUMS.txt`` (``<hex>  <nombre>`` o ``<hex> *<nombre>``)."""
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        hexd, fname = parts[0].lower(), parts[1].strip().lstrip("*").strip()
        if fname == name and _HEX64.match(hexd):
            return hexd
    return None


_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]{0,200}>")
_URL = re.compile(r"https?://\S+")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def sanitize_notes(
    body: str, *, max_chars: int = NOTES_MAX_CHARS, max_lines: int = NOTES_MAX_LINES
) -> str:
    """Notas de la release (Markdown de GitHub) -> texto plano corto y seguro.

    Fuera HTML, imagenes, URLs, enlaces (queda su texto), marcas de enfasis y de titulo, caracteres
    de control y de direccion (spoofing bidi). Las vinetas pasan a «• ». Se recorta por lineas y
    por caracteres (con «…»). La UI lo pinta con ``textContent``: esto es para que se lea bien,
    la seguridad la da no interpretar HTML."""
    text = _CONTROL.sub("", body.replace("\r\n", "\n").replace("\r", "\n"))
    text = _HTML_TAG.sub("", text)
    text = _MD_LINK.sub(lambda m: m.group(1), text)
    text = _URL.sub("", text)
    lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if line.startswith("```"):
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        bullet = re.match(r"^(?:[-*+]|\d+[.)])\s+", line)
        if bullet:
            line = "• " + line[bullet.end() :]
        line = re.sub(r"(\*\*|__|`)", "", line)
        line = re.sub(r"(?<!\w)[*_](\S[^*_]*\S|\S)[*_](?!\w)", r"\1", line)
        line = " ".join(line.split())
        if line or (lines and lines[-1]):
            lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    truncated = len(lines) > max_lines
    out = "\n".join(lines[:max_lines])
    if len(out) > max_chars:
        out, truncated = out[:max_chars].rstrip(), True
    return out + ("…" if truncated else "")


# ===================================================================== red


def url_allowed(url: str) -> bool:
    """Solo ``https`` hacia GitHub (API, web y CDN de activos)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return False
    return host in ALLOWED_HOSTS or any(host.endswith(s) for s in ALLOWED_SUFFIXES)


class _StrictRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> urllib.request.Request | None:
        if not url_allowed(newurl):
            raise UpdateError("updates.err.network", f"redirect blocked: {newurl[:80]}")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and req.has_header("Range"):
            new.add_header("Range", req.get_header("Range"))  # la CDN tambien reanuda
        return new


def default_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_StrictRedirects())


def user_agent(version: str = __version__) -> str:
    return f"Micnaga/{version}"


def _read_limited(resp: Any, limit: int) -> bytes:
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("updates.err.network", "response too large")
    return bytes(data)


# ===================================================================== firma


@dataclass(frozen=True)
class SignatureInfo:
    """Resultado de ``Get-AuthenticodeSignature``: ``status`` = ``Valid`` | ``NotSigned`` |
    ``HashMismatch`` | ``NotTrusted`` | ... | ``Unknown`` (no se pudo comprobar)."""

    status: str
    subject: str = ""
    thumbprint: str = ""

    @property
    def signed(self) -> bool:
        return self.status not in ("NotSigned", "Unknown")

    @property
    def common_name(self) -> str:
        m = re.search(r"(?:^|,\s*)CN=(\"[^\"]*\"|[^,]*)", self.subject)
        return m.group(1).strip().strip('"') if m else ""


_PS_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$s=Get-AuthenticodeSignature -LiteralPath $env:MICNAGA_VERIFY_PATH;"
    "$c=$s.SignerCertificate;"
    "[pscustomobject]@{status=[string]$s.Status;"
    "subject=$(if($c){$c.Subject}else{''});"
    "thumbprint=$(if($c){$c.Thumbprint}else{''})}|ConvertTo-Json -Compress"
)


def authenticode(path: Path, *, run: Callable[..., Any] = subprocess.run) -> SignatureInfo:
    """Firma Authenticode de ``path`` con PowerShell. La ruta va por variable de entorno (nunca
    dentro de la orden). Cualquier fallo -> ``Unknown``."""
    if sys.platform != "win32":
        return SignatureInfo("Unknown")
    env = dict(os.environ, MICNAGA_VERIFY_PATH=str(path))
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    exe = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    try:
        out = run(
            [
                str(exe),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _PS_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        log.warning("authenticode check failed", exc_info=True)
        return SignatureInfo("Unknown")
    if not isinstance(data, dict) or not isinstance(data.get("status"), str):
        return SignatureInfo("Unknown")
    return SignatureInfo(
        status=data["status"],
        subject=str(data.get("subject") or ""),
        thumbprint=str(data.get("thumbprint") or ""),
    )


def signature_verdict(
    new: SignatureInfo,
    current: SignatureInfo | None,
    *,
    require: bool = REQUIRE_SIGNATURE,
    publisher: str = EXPECTED_PUBLISHER,
    refuse_downgrade: bool = REFUSE_SIGNED_TO_UNSIGNED,
) -> str | None:
    """``None`` si se puede instalar; si no, la clave del error (``updates.err.*``)."""
    current_valid = current is not None and current.status == "Valid"
    if new.status == "Valid":
        if publisher and new.common_name != publisher:
            return "updates.err.publisher"
        if current_valid and current is not None and current.common_name != new.common_name:
            return "updates.err.publisher"
        return None
    if new.status in ("NotSigned", "Unknown"):
        if require or (refuse_downgrade and current_valid):
            return "updates.err.unsigned"
        if new.status == "Unknown" and current_valid:
            return "updates.err.signature"
        return None
    return "updates.err.signature"  # firma presente pero rota (HashMismatch, NotTrusted...)


# ===================================================================== datos de la build


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


_BUILD_CACHE: dict[str, str] | None = None


def build_info() -> dict[str, str]:
    """``{"version", "commit", "date"}``. La build congelada lee ``micnaga/_build_info.py`` (lo
    escribe ``scripts\\build.ps1``); en desarrollo, ``git`` (una vez). Vacios si no hay nada."""
    global _BUILD_CACHE
    if _BUILD_CACHE is None:
        info = {"version": __version__, "commit": "", "date": ""}
        try:
            from micnaga import _build_info  # type: ignore[attr-defined,unused-ignore]

            info["commit"] = str(getattr(_build_info, "COMMIT", ""))[:12]
            info["date"] = str(getattr(_build_info, "DATE", ""))[:32]
        except ImportError:
            if not is_frozen():
                info.update(_git_info())
        _BUILD_CACHE = info
    return dict(_BUILD_CACHE)


def _git_info() -> dict[str, str]:
    root = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%h|%cs"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    parts = out.stdout.strip().split("|")
    if out.returncode != 0 or len(parts) != 2:
        return {}
    return {"commit": parts[0][:12], "date": parts[1][:32]}


# ===================================================================== estado persistente


class _State:
    """``updates.json``: pequeno, escrito de forma atomica, tolerante a basura."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self._path is None:
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.data.get(key, default)

    def update(self, **values: Any) -> None:
        with self._lock:
            for k, v in values.items():
                if v is None:
                    self.data.pop(k, None)
                else:
                    self.data[k] = v
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=1)
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(snapshot, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            log.warning("cannot write %s", self._path, exc_info=True)


# ===================================================================== servicio


@dataclass
class _Progress:
    done: int = 0
    total: int = 0
    started: float = 0.0


@dataclass
class UpdateHooks:
    """Lo que el servicio necesita de la app (todo opcional)."""

    auto_check: Callable[[], bool] = lambda: True
    is_busy: Callable[[], bool] = lambda: False
    """Grabando, procesando o descargando modelos: no se instala."""
    on_available: Callable[[ReleaseInfo], None] = lambda _r: None
    """Aviso discreto (toast) la primera vez que se ve una version (nunca grabando)."""
    on_updated: Callable[[str], None] = lambda _v: None
    """Primer arranque tras actualizar: aviso «Novedades»."""
    before_install: Callable[[], None] = lambda: None
    """Parada ordenada (vigilante, cola...) justo antes de lanzar el instalador."""
    quit_app: Callable[[], None] = lambda: None
    open_ui: Callable[[], None] = lambda: None
    """Abre Ajustes > Acerca de (la bandeja «Actualizar a la version X»)."""


@dataclass
class _Snapshot:
    state: str = "idle"
    """idle | checking | uptodate | available | downloading | verifying | waiting | installing |
    error | disabled"""
    error: str = ""
    manual: bool = False
    progress: _Progress = field(default_factory=_Progress)


class UpdateService:
    """Comprobacion periodica, descarga verificada e instalacion en reposo. Seguro entre hilos."""

    def __init__(
        self,
        hooks: UpdateHooks | None = None,
        *,
        current: str = __version__,
        opener: urllib.request.OpenerDirector | None = None,
        state_path: Path | None = None,
        download_dir: Path | None = None,
        can_install: bool | None = None,
        launch: Callable[[list[str]], None] | None = None,
        signature: Callable[[Path], SignatureInfo] = authenticode,
        current_exe: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        first_delay_s: float = FIRST_CHECK_DELAY_S,
        interval_s: float = CHECK_INTERVAL_S,
    ) -> None:
        self.hooks = hooks or UpdateHooks()
        self.current = current
        self._opener = opener or default_opener()
        self._state = _State(state_path)
        self._dir = download_dir or Path(tempfile.gettempdir()) / "Micnaga-update"
        self._can_install = is_frozen() if can_install is None else can_install
        self._launch = launch or _launch_detached
        self._signature = signature
        self._current_exe = current_exe or Path(sys.executable)
        self._sleep = sleep
        self._clock = clock
        self._first_delay = first_delay_s
        self._interval = interval_s
        self._lock = threading.RLock()
        self._snap = _Snapshot()
        self._release: ReleaseInfo | None = ReleaseInfo.from_dict(self._state.get("release") or {})
        self._ready: Path | None = None
        self._expected: dict[str, str] = {}
        self._current_sig: SignatureInfo | None = None
        self._install_pending = False
        self._installing = False
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._listeners: list[Callable[[], None]] = []
        if os.environ.get(ENV_DISABLE):
            self._snap.state = "disabled"

    # ------------------------------------------------------------------ oyentes
    def add_listener(self, fn: Callable[[], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[], None]) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self._listeners.remove(fn)

    def _changed(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn()
            except Exception:
                log.exception("update listener failed")

    def _set(self, state: str, error: str = "") -> None:
        with self._lock:
            self._snap.state, self._snap.error = state, error
        self._changed()

    # ------------------------------------------------------------------ ciclo de vida
    def start(self) -> None:
        """Hilo de fondo: novedades tras actualizar, primera comprobacion y cada 12 h."""
        self._note_version_change()
        self._prune_downloads()
        if self._snap.state == "disabled" or self._worker is not None:
            return
        self._worker = threading.Thread(target=self._loop, name="micnaga-updates", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _prune_downloads(self) -> None:
        """Borra instaladores ya usados (version igual o anterior a la que corre)."""
        import shutil

        try:
            folders = list(self._dir.iterdir()) if self._dir.is_dir() else []
        except OSError:
            return
        for folder in folders:
            ver = parse_version(folder.name)
            if ver is not None and not is_newer(folder.name, self.current):
                shutil.rmtree(folder, ignore_errors=True)

    def _loop(self) -> None:
        if self._stop.wait(self._first_delay):
            return
        while not self._stop.is_set():
            last = float(self._state.get("last_check", 0) or 0)
            due = self._clock() - last >= self._interval * 0.98
            if due and self._auto_on():
                try:
                    self.check(manual=False)
                except Exception:
                    log.exception("update check failed")
            self._maybe_notify()
            self._maybe_install_pending()
            wait = IDLE_POLL_S if (self._install_pending or self._notify_deferred()) else 3600.0
            self._wake.wait(min(wait, self._interval))
            self._wake.clear()

    def _auto_on(self) -> bool:
        try:
            return bool(self.hooks.auto_check())
        except Exception:
            return True

    # ------------------------------------------------------------------ comprobar
    def check(self, *, manual: bool = True) -> dict[str, Any]:
        """Consulta ``/releases/latest``. A mano devuelve el estado (con error legible); en
        segundo plano los fallos de red se callan."""
        if self._snap.state == "disabled":
            return self.status()
        with self._lock:
            if self._snap.state in ("downloading", "verifying", "installing"):
                return self.status()
            self._snap.manual = manual
        self._set("checking")
        try:
            release = self._fetch_latest(manual=manual)
        except UpdateError as exc:
            log.info("update check: %s", exc)
            self._set("error" if manual else "idle", exc.key if manual else "")
            return self.status()
        self._state.update(last_check=self._clock())
        with self._lock:
            if release is not None:
                self._release = release
        if release is not None and release.version == self.current:
            wn = self.whats_new()
            if wn is not None and not wn["notes"] and release.notes:
                self._state.update(whats_new={**wn, "notes": release.notes})
        if release is not None and is_newer(release.version, self.current):
            self._set("available")
            self._maybe_notify()
        else:
            self._set("uptodate")
        return self.status()

    def _fetch_latest(self, *, manual: bool) -> ReleaseInfo | None:
        reset = float(self._state.get("rate_reset", 0) or 0)
        if not manual and self._clock() < reset:
            raise UpdateError("updates.err.rate_limited")
        headers = {
            "User-Agent": user_agent(self.current),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        etag = self._state.get("etag")
        cached = ReleaseInfo.from_dict(self._state.get("release") or {})
        if isinstance(etag, str) and etag and cached is not None:
            headers["If-None-Match"] = etag
        req = urllib.request.Request(API_LATEST, headers=headers)
        try:
            with self._opener.open(req, timeout=HTTP_TIMEOUT_S) as resp:
                body = _read_limited(resp, MAX_JSON_BYTES)
                new_etag = resp.headers.get("ETag") or ""
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and cached is not None:
                return cached
            if exc.code == 404:  # sin ninguna release publicada aun
                return None
            if exc.code in (403, 429):
                retry = _rate_reset(exc.headers, self._clock())
                self._state.update(rate_reset=retry)
                raise UpdateError("updates.err.rate_limited") from exc
            raise UpdateError("updates.err.network", f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise UpdateError("updates.err.offline", str(exc)[:120]) from exc
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise UpdateError("updates.err.network", "invalid JSON") from exc
        release = parse_release(data)
        self._state.update(
            etag=new_etag or None,
            release=release.to_dict() if release is not None else None,
            rate_reset=None,
        )
        return release

    # ------------------------------------------------------------------ omitir / estado
    def skip(self, version: str | None = None) -> dict[str, Any]:
        """«Omitir esta version»: no se vuelve a avisar de ella (si de una posterior)."""
        with self._lock:
            target = version or (self._release.version if self._release else "")
        if parse_version(target):
            self._state.update(skipped=target)
        self._changed()
        return self.status()

    def available(self) -> ReleaseInfo | None:
        """La release nueva que se ofrece (no omitida), o ``None``."""
        with self._lock:
            rel = self._release
        if rel is None or not is_newer(rel.version, self.current):
            return None
        if self._state.get("skipped") == rel.version:
            return None
        return rel

    def status(self) -> dict[str, Any]:
        """Todo lo que pinta Ajustes > Acerca de (y la bandeja)."""
        with self._lock:
            snap = self._snap
            rel = self._release
            prog = snap.progress
            state = snap.state
            pending = self._install_pending
        avail = self.available()
        newer = rel is not None and is_newer(rel.version, self.current)
        if state in ("available", "uptodate", "idle"):
            if avail is not None:
                state = "available"
            elif newer:
                state = "skipped"
            elif state != "idle":
                state = "uptodate"
        return {
            "current": self.current,
            "build": build_info(),
            "state": state,
            "error": snap.error,
            "latest": rel.version if newer and rel else "",
            "available": avail is not None,
            "skipped": bool(newer and rel and self._state.get("skipped") == rel.version),
            "notes": rel.notes if newer and rel else "",
            "size": rel.asset_size if newer and rel else 0,
            "done": prog.done,
            "total": prog.total,
            "pending": pending,
            "can_install": self._can_install,
            "auto": self._auto_on(),
            "checked_at": float(self._state.get("last_check", 0) or 0),
            "whats_new": self.whats_new(),
        }

    # ------------------------------------------------------------------ avisos
    def _notify_deferred(self) -> bool:
        rel = self.available()
        return rel is not None and self._state.get("notified") != rel.version

    def _maybe_notify(self) -> None:
        """Un solo aviso por version, nunca mientras se graba o procesa (se reintenta luego)."""
        rel = self.available()
        if rel is None or self._state.get("notified") == rel.version or self._busy():
            return
        self._state.update(notified=rel.version)
        try:
            self.hooks.on_available(rel)
        except Exception:
            log.exception("update notice failed")

    def _busy(self) -> bool:
        try:
            return bool(self.hooks.is_busy())
        except Exception:
            return True

    # ------------------------------------------------------------------ novedades
    def _note_version_change(self) -> None:
        """Primer arranque de una version distinta: deja «Novedades» para mostrar una vez."""
        last = self._state.get("last_run_version")
        if last == self.current:
            return
        updated = isinstance(last, str) and is_newer(self.current, last)
        notes = ""
        pending = self._state.get("pending_notes")
        if isinstance(pending, dict) and pending.get("version") == self.current:
            notes = str(pending.get("notes", ""))
        elif self._release is not None and self._release.version == self.current:
            notes = self._release.notes
        self._state.update(
            last_run_version=self.current,
            pending_notes=None,
            whats_new={"version": self.current, "notes": notes, "seen": False} if updated else None,
        )
        if updated:
            try:
                self.hooks.on_updated(self.current)
            except Exception:
                log.exception("updated notice failed")

    def whats_new(self) -> dict[str, Any] | None:
        """``{"version", "notes", "seen"}`` de la version que corre si se acaba de actualizar."""
        data = self._state.get("whats_new")
        if not isinstance(data, dict) or data.get("version") != self.current:
            return None
        return {
            "version": self.current,
            "notes": str(data.get("notes", "")),
            "seen": bool(data.get("seen")),
        }

    def mark_whats_new_seen(self) -> None:
        data = self.whats_new()
        if data is not None and not data["seen"]:
            self._state.update(whats_new={**data, "seen": True})

    # ------------------------------------------------------------------ instalar
    def request_install(self, *, open_ui: bool = False) -> dict[str, Any]:
        """«Instalar y reiniciar»: descarga y verifica en segundo plano y, en reposo, instala.

        Grabando o procesando queda pendiente (``pending``) y se instala al quedar libre."""
        if open_ui:
            with contextlib.suppress(Exception):
                self.hooks.open_ui()
        rel = self.available() or self._newer_release()
        if rel is None:
            return self.status()
        if not self._can_install:
            self._set("error", "updates.err.dev")
            return self.status()
        with self._lock:
            if self._snap.state in ("downloading", "verifying", "installing"):
                return self.status()
            self._install_pending = True
            self._snap.state = "downloading"
            self._snap.error = ""
        threading.Thread(
            target=self._download_and_install, args=(rel,), name="micnaga-update-dl", daemon=True
        ).start()
        self._changed()
        return self.status()

    def _newer_release(self) -> ReleaseInfo | None:
        """Tambien se puede instalar a mano una version omitida."""
        with self._lock:
            rel = self._release
        return rel if rel is not None and is_newer(rel.version, self.current) else None

    def _download_and_install(self, rel: ReleaseInfo) -> None:
        try:
            path = self.download(rel)
        except UpdateError as exc:
            log.warning("update download failed: %s", exc)
            with self._lock:
                self._install_pending = False
            self._set("error", exc.key)
            return
        except Exception:
            log.exception("update download failed")
            with self._lock:
                self._install_pending = False
            self._set("error", "updates.err.generic")
            return
        with self._lock:
            self._ready = path
        self._maybe_install_pending(from_loop=False)

    def _maybe_install_pending(self, *, from_loop: bool = True) -> None:
        with self._lock:
            path, pending = self._ready, self._install_pending
        if path is None or not pending:
            return
        if self._busy():
            if self._snap.state != "waiting":
                self._set("waiting")
            if not from_loop:
                self._wake.set()  # el hilo de fondo pasa a mirar cada IDLE_POLL_S
            return
        rel = self._newer_release()
        if rel is None:
            return
        with self._lock:  # el hilo de descarga y el sondeo de reposo no lanzan dos instaladores
            if self._installing:
                return
            self._installing = True
        try:
            self.install(rel, path)
        except UpdateError as exc:
            log.warning("update install refused: %s", exc)
            with self._lock:
                self._install_pending = False
                self._ready = None
            self._set("error", exc.key)
        finally:
            with self._lock:
                self._installing = False

    def download(self, rel: ReleaseInfo) -> Path:
        """Descarga (reanudable, con reintentos) y verifica el instalador. Devuelve su ruta."""
        expected = self._expected_sha256(rel)
        target_dir = self._dir / rel.version
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / rel.asset_name
        if dest.is_file() and dest.stat().st_size == rel.asset_size:
            self._set("verifying")
            if _sha256(dest) == expected:
                self._check_signature(dest)
                return dest
            dest.unlink(missing_ok=True)
        part = dest.with_name(dest.name + ".part")
        with self._lock:
            self._snap.progress = _Progress(0, rel.asset_size, self._clock())
        self._set("downloading")
        last_error: UpdateError | None = None
        for attempt in range(DOWNLOAD_ATTEMPTS):
            if self._stop.is_set():
                raise UpdateError("updates.err.cancelled")
            try:
                self._fetch_part(rel, part)
                break
            except UpdateError as exc:
                last_error = exc
                if exc.key in ("updates.err.size", "updates.err.disk", "updates.err.cancelled"):
                    raise
                wait = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
                log.info(
                    "update download attempt %d failed (%s); retry in %.0f s",
                    attempt + 1,
                    exc,
                    wait,
                )
                if attempt + 1 < DOWNLOAD_ATTEMPTS:
                    self._sleep(wait)
        else:
            raise last_error or UpdateError("updates.err.network")
        self._set("verifying")
        if part.stat().st_size != rel.asset_size or _sha256(part) != expected:
            part.unlink(missing_ok=True)
            raise UpdateError("updates.err.checksum")
        os.replace(part, dest)
        self._check_signature(dest)
        return dest

    def _expected_sha256(self, rel: ReleaseInfo) -> str:
        """SHA-256 publicado: ``SHA256SUMS.txt`` y/o el ``digest`` de GitHub (deben coincidir)."""
        with self._lock:
            known = self._expected.get(rel.version)
        if known:
            return known
        listed: str | None = None
        if rel.checksums_url:
            req = urllib.request.Request(
                rel.checksums_url, headers={"User-Agent": user_agent(self.current)}
            )
            try:
                with self._opener.open(req, timeout=HTTP_TIMEOUT_S) as resp:
                    text = _read_limited(resp, MAX_CHECKSUMS_BYTES).decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                raise UpdateError("updates.err.network", f"HTTP {exc.code}") from exc
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                raise UpdateError("updates.err.offline", str(exc)[:120]) from exc
            listed = parse_checksums(text, rel.asset_name)
            if listed is None:
                raise UpdateError("updates.err.checksum", "installer not in SHA256SUMS.txt")
        if listed and rel.digest and listed != rel.digest:
            raise UpdateError("updates.err.checksum", "SHA256SUMS.txt and digest disagree")
        expected = listed or rel.digest
        if not expected:
            raise UpdateError("updates.err.checksum", "no published checksum")
        with self._lock:
            self._expected[rel.version] = expected
        return expected

    def _fetch_part(self, rel: ReleaseInfo, part: Path) -> None:
        have = part.stat().st_size if part.exists() else 0
        if have > rel.asset_size:
            part.unlink()
            have = 0
        if have == rel.asset_size:
            return
        _check_disk(part.parent, rel.asset_size - have)
        headers = {"User-Agent": user_agent(self.current), "Accept": "application/octet-stream"}
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(rel.asset_url, headers=headers)
        try:
            resp = self._opener.open(req, timeout=HTTP_TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and have:
                part.unlink(missing_ok=True)
                raise UpdateError("updates.err.network", "range not satisfiable") from exc
            raise UpdateError("updates.err.network", f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise UpdateError("updates.err.offline", str(exc)[:120]) from exc
        with resp:
            status = getattr(resp, "status", 200)
            if have and status != 206:
                have = 0  # el servidor ignoro Range: de cero
            got = have
            self._progress(got)
            try:
                with part.open("ab" if have else "wb") as fh:
                    while block := resp.read(CHUNK):
                        got += len(block)
                        if got > rel.asset_size:
                            raise UpdateError("updates.err.size")
                        fh.write(block)
                        self._progress(got)
                        if self._stop.is_set():
                            raise UpdateError("updates.err.cancelled")
            except (OSError, http.client.HTTPException) as exc:
                raise UpdateError("updates.err.offline", str(exc)[:120]) from exc
        if got != rel.asset_size:
            raise UpdateError("updates.err.offline", f"incomplete: {got}/{rel.asset_size}")

    def _progress(self, done: int) -> None:
        with self._lock:
            self._snap.progress.done = done

    def _check_signature(self, path: Path) -> None:
        new = self._signature(path)
        current: SignatureInfo | None = None
        if is_frozen():
            if self._current_sig is None:
                self._current_sig = self._signature(self._current_exe)
            current = self._current_sig
        verdict = signature_verdict(new, current)
        if verdict is not None:
            raise UpdateError(verdict, f"{new.status} {new.common_name}")

    def install(self, rel: ReleaseInfo, path: Path) -> None:
        """Ultima verificacion, instalador en marcha (separado de este proceso) y salida."""
        if not self._can_install:
            raise UpdateError("updates.err.dev")
        self._set("verifying")
        if path.stat().st_size != rel.asset_size or _sha256(path) != self._expected_sha256(rel):
            path.unlink(missing_ok=True)
            raise UpdateError("updates.err.checksum")
        self._check_signature(path)
        if self._busy():  # pudo empezar una grabacion mientras se verificaba
            self._set("waiting")
            return
        self._set("installing")
        self._state.update(pending_notes={"version": rel.version, "notes": rel.notes})
        log_path = path.with_name("install.log")
        try:
            self._launch([str(path), *INSTALLER_ARGS, f"/LOG={log_path}"])
        except OSError as exc:
            raise UpdateError("updates.err.launch", str(exc)) from exc
        log.info("installer %s launched; quitting for the update", rel.version)
        for step in (self.hooks.before_install, self.hooks.quit_app):
            try:
                step()
            except Exception:
                log.exception("update shutdown step failed")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _check_disk(folder: Path, need: int) -> None:
    import shutil

    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return
    if free < need + (64 << 20):
        raise UpdateError("updates.err.disk")


def _rate_reset(headers: Any, now: float) -> float:
    """Hasta cuando no volver a preguntar tras un 403/429 (``Retry-After`` o ``X-RateLimit-Reset``,
    como poco una hora)."""
    try:
        if headers is not None and headers.get("Retry-After"):
            return now + max(60.0, float(headers["Retry-After"]))
        if headers is not None and headers.get("X-RateLimit-Reset"):
            return max(now + 60.0, float(headers["X-RateLimit-Reset"]))
    except (TypeError, ValueError):
        pass
    return now + 3600.0


def _launch_detached(argv: list[str]) -> None:  # pragma: no cover - lanza el instalador real
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    breakaway = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB: que sobreviva a la salida de la app
    try:
        subprocess.Popen(argv, creationflags=flags | breakaway, close_fds=True)
    except OSError:
        subprocess.Popen(argv, creationflags=flags, close_fds=True)


# ===================================================================== servicio global

_SERVICE: UpdateService | None = None


def set_service(service: UpdateService | None) -> None:
    """La integracion registra el servicio; la bandeja y Ajustes lo leen con ``service()``."""
    global _SERVICE
    _SERVICE = service


def service() -> UpdateService | None:
    return _SERVICE


def default_state_path() -> Path:
    from micnaga import paths

    return paths.app_data_dir() / STATE_FILE
