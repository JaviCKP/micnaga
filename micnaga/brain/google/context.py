"""Contexto de la reunion al empezar a grabar: el evento de Google Calendar que ocurre ahora.

En cuanto la grabacion esta confirmada (el «Sí» del popup, normalmente a los 20-40 s), un hilo
aparte pide a Calendar (``list_events`` de ahora ± 15 min, por ``claude -p`` y con guardian) los
eventos en curso y elige uno en Python: el que lleva el mismo codigo de Meet que la pestaña
grabada, o el que empieza mas cerca de ahora. De el salen:

* el **titulo** de la reunion (pastilla y titulo provisional),
* los **asistentes** (nombre y correo): candidatos para nombrar hablantes y para buscar
  contactos en Odoo en el analisis posterior.

Se guarda en ``<sesion>/contexto_google.json``. Nunca retrasa ni rompe la grabacion: hilo
propio, tiempo maximo y cualquier fallo (sin red, sin conector, lento) se ignora en silencio.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from micnaga import paths
from micnaga.brain import acta as acta_io
from micnaga.brain.google.analysis import CONTEXT_JSON

log = logging.getLogger(__name__)

WINDOW_MIN = 15
TIMEOUT_S = 45.0
POLL_S = 5.0


def meet_code(session: Path) -> str | None:
    try:
        data = json.loads((session / paths.CAPTURE_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    code = data.get("meet_code") if isinstance(data, dict) else None
    return str(code) if code else None


def _when(ev: dict[str, Any], key: str) -> datetime | None:
    raw = ev.get(key)
    value = raw.get("dateTime") if isinstance(raw, dict) else None
    if not isinstance(value, str):
        return None  # todo el dia: no es «la reunion de ahora»
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def _meet_url(ev: dict[str, Any]) -> str | None:
    url = ev.get("conferenceUrl") or ev.get("hangoutLink")
    conf = ev.get("conferenceData")
    if not url and isinstance(conf, dict):
        video = conf.get("videoEntryPoint")
        if isinstance(video, dict):
            url = video.get("uri")
    return str(url) if url else None


def pick(events: list[Any], now: datetime, code: str | None = None) -> dict[str, Any] | None:
    """El evento de ahora (± ``WINDOW_MIN``): mismo codigo de Meet o el mas cercano."""
    win = timedelta(minutes=WINDOW_MIN)
    best: tuple[float, dict[str, Any]] | None = None
    for ev in events:
        if not isinstance(ev, dict) or ev.get("status") == "cancelled":
            continue
        start, end = _when(ev, "start"), _when(ev, "end")
        if start is None or end is None or not (start - win <= now <= end + win):
            continue
        url = _meet_url(ev) or ""
        if code and code in url:
            return ev
        score = abs((start - now).total_seconds())
        if best is None or score < best[0]:
            best = (score, ev)
    return best[1] if best else None


def to_context(ev: dict[str, Any]) -> dict[str, Any]:
    people = []
    for a in ev.get("attendees") or []:
        if not isinstance(a, dict) or a.get("resource") or not a.get("email"):
            continue
        people.append(
            {
                "nombre": a.get("displayName") or None,
                "email": str(a["email"]),
                "self": bool(a.get("self")),
            }
        )
    start = _when(ev, "start")
    return {
        "titulo": " ".join(str(ev.get("summary") or "").split())[:120] or None,
        "event_id": ev.get("id"),
        "htmlLink": ev.get("htmlLink"),
        "meet_url": _meet_url(ev),
        "inicio": start.strftime("%Y-%m-%dT%H:%M") if start else None,
        "asistentes": people[:30],
    }


def lookup(config: Any, session: Path, *, runner: Any = None) -> dict[str, Any] | None:
    """Una consulta a Calendar (``list_events`` de la ventana actual)."""
    from micnaga.brain.claude import ClaudeRunner
    from micnaga.brain.google import calls, detect

    runner = runner or ClaudeRunner(auth=config.secretaria.auth, retries=0)
    now = datetime.now().replace(second=0, microsecond=0)
    win = timedelta(minutes=WINDOW_MIN)
    q: dict[str, Any] = {
        "startTime": (now - win).strftime("%Y-%m-%dT%H:%M:%S"),
        "endTime": (now + win).strftime("%Y-%m-%dT%H:%M:%S"),
        "pageSize": 10,
    }
    cal = config.secretaria.google_calendar_id
    if cal and cal != "primary":
        q["calendarId"] = cal
    out = calls.run_tool(
        runner, "calendar", "list_events", q, foreign=detect.foreign_servers(), timeout_s=TIMEOUT_S
    )
    if not out.ok:
        return None
    ev = pick(out.structured.get("events") or [], now, meet_code(session))
    return to_context(ev) if ev is not None else None


def write_context(session: Path, ctx: dict[str, Any]) -> None:
    tmp = session / (CONTEXT_JSON + ".tmp")
    tmp.write_text(json.dumps(ctx, ensure_ascii=False), encoding="utf-8")
    tmp.replace(session / CONTEXT_JSON)


class GoogleContextWatcher:
    """Hilo que vigila la grabacion en curso (solo su carpeta, sin tocar el controlador)."""

    def __init__(
        self,
        config: Callable[[], Any],
        session_dir: Callable[[], Path | None],
        on_title: Callable[[str], None],
        *,
        lookup_fn: Callable[[Any, Path], dict[str, Any] | None] | None = None,
        poll_s: float = POLL_S,
    ) -> None:
        self._config = config
        self._session_dir = session_dir
        self._on_title = on_title
        self._lookup = lookup_fn or lookup
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._done: set[str] = set()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, name="micnaga-google-context", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)

    def _enabled(self) -> bool:
        s = self._config().secretaria
        return bool(s.enabled and getattr(s, "google_calendar", False))

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            with contextlib.suppress(Exception):
                self.check()

    def check(self) -> dict[str, Any] | None:
        """Una comprobacion (los tests la llaman directamente)."""
        from micnaga.brain.live_title import is_confirmed

        session = self._session_dir()
        if session is None or not self._enabled() or session.name in self._done:
            return None
        if not is_confirmed(session):
            return None
        self._done.add(session.name)
        try:
            ctx = self._lookup(self._config(), session)
        except Exception as exc:
            log.info("calendar context skipped: %s", getattr(exc, "kind", type(exc).__name__))
            return None
        if not ctx:
            return None
        with contextlib.suppress(OSError):
            write_context(session, ctx)
        title = ctx.get("titulo")
        if title and self._session_dir() == session:
            self._on_title(str(title))
            if acta_io.read_live_title(session) is None:
                with contextlib.suppress(OSError):
                    acta_io.write_live_title(session, str(title), fuente="calendar")
        return ctx
