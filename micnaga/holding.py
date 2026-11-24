"""«Descartadas»: sesiones apartadas (audio sin transcribir) que se pueden recuperar N dias.

Regla del usuario (DECISIONS §18): nada se borra sin un «No»/«Descartar» explicito, y ni siquiera
entonces al instante: un «Ahora no» por error tras hora y media de reunion debe poder deshacerse.
Toda sesion que no se va a transcribir pasa por aqui:

* «Ahora no» (popup) o «Descartar» (popup de fin), cuando expira el «Deshacer»;
* una sesion sin confirmar cuyo popup de fin nadie contesto;
* una sesion que la etapa 1 del pipeline no deja pasar (compuertas o sin confirmar);
* al arrancar, las sesiones huerfanas marcadas como descartadas o sin confirmar.

Disposicion en disco (todo local, nunca se transcribe salvo que el usuario la recupere)::

    <raiz de reuniones>/_descartadas/<carpeta original>/
        capture.json, audio_*.flac|wav, held.json   {"reason", "held_at", "original"}

La marca ``.micnaga-discard.json`` se escribe (con ``fsync``) en la carpeta de la sesion **en el
mismo instante** del «No», antes de cerrar el popup: si la app muere antes de apartarla, el
arranque la aparta en vez de encolarla (hallazgo 1 de la revision).

Modulo ligero (biblioteca estandar): lo usan la app, el worker y el arranque.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from micnaga import paths

log = logging.getLogger(__name__)

HELD_DIRNAME = "_descartadas"
HELD_FILE = "held.json"
DISCARD_MARKER = ".micnaga-discard.json"

# Motivos (claves estables; la UI los traduce)
REASON_DECLINED = "declined"  # «Ahora no» / «Descartar»
REASON_UNANSWERED = "unanswered"  # sin respuesta a la pregunta de fin
REASON_UNCONFIRMED = "unconfirmed"  # la etapa 1 no transcribe sesiones sin «Si»
REASON_GATES = "gates"  # una compuerta (p. ej. demasiado corta)
REASON_INTERRUPTED = "interrupted"  # la app se cerro con una sesion sin confirmar


def held_root(meetings_root: Path) -> Path:
    return meetings_root / HELD_DIRNAME


def _now() -> datetime:
    return datetime.now().astimezone()


def _write_json_sync(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False))
        fh.flush()
        os.fsync(fh.fileno())
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.02)


# ----------------------------------------------------------------------------- marca


def mark_discarded(session_dir: Path, reason: str = REASON_DECLINED) -> bool:
    """Marca (persistente, sincrona) «esta sesion NO se transcribe». ``False`` si no se pudo."""
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        _write_json_sync(
            session_dir / DISCARD_MARKER,
            {"reason": reason, "at": _now().isoformat(timespec="seconds")},
        )
        return True
    except OSError:
        log.exception("could not persist the discard marker")
        return False


def unmark_discarded(session_dir: Path) -> None:
    with contextlib.suppress(OSError):
        (session_dir / DISCARD_MARKER).unlink(missing_ok=True)


def discard_reason(session_dir: Path) -> str | None:
    """Motivo de la marca, o ``None`` si la sesion no esta marcada."""
    marker = session_dir / DISCARD_MARKER
    if not marker.is_file():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return REASON_DECLINED
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason if isinstance(reason, str) else REASON_DECLINED


# ----------------------------------------------------------------------------- apartar


def _move(src: Path, dst: Path) -> None:
    last: OSError | None = None
    for _ in range(10):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:  # antivirus / indexador con la carpeta abierta
            last = exc
            time.sleep(0.2)
    try:
        shutil.move(str(src), str(dst))
    except OSError:
        assert last is not None
        raise last from None


def move_to_held(
    session_dir: Path,
    reason: str,
    *,
    meetings_root: Path | None = None,
    detail: str = "",
    retain: bool = False,
) -> Path:
    """Aparta la carpeta en ``_descartadas`` (anota motivo y fecha). Devuelve la ruta nueva.

    Idempotente: una carpeta que ya esta dentro de ``_descartadas`` solo actualiza ``held.json``.
    Lo transcrito en vivo mientras se grababa (``work/live/``) se borra: una sesion apartada
    conserva solo el audio, como siempre (DECISIONS §23).
    """
    _drop_live_work(session_dir)
    if meetings_root is not None:
        root = meetings_root
    elif session_dir.parent.name == HELD_DIRNAME:
        root = session_dir.parent.parent  # ya apartada: no anidar ``_descartadas``
    else:
        root = session_dir.parent
    target_root = held_root(root)
    if session_dir.parent == target_root:
        dest = session_dir
    else:
        target_root.mkdir(parents=True, exist_ok=True)
        dest = paths.unique_folder(target_root, session_dir.name)
    _write_json_sync(
        session_dir / HELD_FILE,
        {
            "reason": reason,
            "held_at": _now().isoformat(timespec="seconds"),
            "original": session_dir.name,
            "detail": detail,
            "retain": retain,
        },
    )
    if dest != session_dir:
        _move(session_dir, dest)
    unmark_discarded(dest)
    log.info("session held (%s)", reason)  # sin titulo ni ruta
    return dest


def _drop_live_work(session_dir: Path) -> None:
    live = paths.work_dir(session_dir) / "live"
    for _ in range(10):
        try:
            shutil.rmtree(live)
        except FileNotFoundError:
            break
        except OSError:  # el proceso en vivo aun lo tiene abierto: se reintenta un momento
            time.sleep(0.2)
            continue
        break
    with contextlib.suppress(OSError):
        paths.work_dir(session_dir).rmdir()  # solo si quedo vacia
    if live.exists():
        log.warning("could not delete the live transcription of a held session")


@dataclass(frozen=True)
class HeldSession:
    name: str
    """Nombre de la carpeta dentro de ``_descartadas``."""
    title: str | None
    started_at: str
    duration_s: float
    reason: str
    held_at: str
    expires_at: str
    has_audio: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "started_at": self.started_at,
            "duration_s": self.duration_s,
            "reason": self.reason,
            "held_at": self.held_at,
            "expires_at": self.expires_at,
            "has_audio": self.has_audio,
            "detail": self.detail,
        }


def _held_info(d: Path) -> dict[str, Any]:
    try:
        data = json.loads((d / HELD_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _held_at(d: Path, info: dict[str, Any]) -> datetime:
    raw = info.get("held_at")
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(raw)
    return datetime.fromtimestamp(d.stat().st_mtime).astimezone()


def list_held(meetings_root: Path, keep_days: int = 7) -> list[HeldSession]:
    """Sesiones apartadas, la mas reciente primero."""
    base = held_root(meetings_root)
    if not base.is_dir():
        return []
    out: list[tuple[datetime, HeldSession]] = []
    for d in base.iterdir():
        if not d.is_dir():
            continue
        info = _held_info(d)
        try:
            meta = json.loads((d / paths.CAPTURE_JSON).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        held_at = _held_at(d, info)
        title = meta.get("meet_title")
        duration = meta.get("duration_s")
        if not isinstance(duration, int | float):
            mic = meta.get("mic") if isinstance(meta.get("mic"), dict) else {}
            rate = (mic or {}).get("sample_rate") or 16000
            duration = float((mic or {}).get("samples") or 0) / float(rate)
        audio = any(
            (d / n).is_file()
            for n in (
                paths.AUDIO_ME_FLAC,
                paths.AUDIO_REMOTE_FLAC,
                paths.AUDIO_ME_WAV,
                paths.AUDIO_REMOTE_WAV,
            )
        )
        out.append(
            (
                held_at,
                HeldSession(
                    name=d.name,
                    title=title if isinstance(title, str) and title.strip() else None,
                    started_at=str(meta.get("started_at") or ""),
                    duration_s=round(float(duration), 1),
                    reason=str(info.get("reason") or REASON_DECLINED),
                    held_at=held_at.isoformat(timespec="seconds"),
                    expires_at=(
                        ""
                        if info.get("retain") is True
                        else (held_at + timedelta(days=keep_days)).isoformat(timespec="seconds")
                    ),
                    has_audio=audio,
                    detail=str(info.get("detail") or ""),
                ),
            )
        )
    out.sort(key=lambda p: p[0], reverse=True)
    return [h for _, h in out]


def held_dir(meetings_root: Path, name: str) -> Path | None:
    """Carpeta apartada ``name`` (validada: sin rutas) o ``None``."""
    if not name or name != Path(name).name or name in (".", ".."):
        return None
    d = held_root(meetings_root) / name
    return d if d.is_dir() else None


def restore_held(meetings_root: Path, name: str) -> Path:
    """Devuelve una sesion apartada a la raiz de reuniones (confirmada: el usuario la quiere).

    No encola: quien llama la cierra (FLAC) y la encola.
    """
    src = held_dir(meetings_root, name)
    if src is None:
        raise FileNotFoundError(name)
    info = _held_info(src)
    original = str(info.get("original") or name)
    dest = paths.unique_folder(meetings_root, original)
    _move(src, dest)
    with contextlib.suppress(OSError):
        (dest / HELD_FILE).unlink()
    unmark_discarded(dest)
    _set_confirmed(dest)
    log.info("held session recovered")
    return dest


def _set_confirmed(session_dir: Path) -> None:
    """Confirmada y marcada como recuperada por el usuario (salta las compuertas automaticas:
    una sesion corta ya confirmada volvia a «Descartadas» en bucle, hallazgo P2-b)."""
    import dataclasses

    from micnaga.capture.session import read_capture_json, write_capture_json
    from micnaga.gates import USER_RESTORED_KEY

    try:
        meta = read_capture_json(session_dir)
    except (OSError, ValueError, KeyError):
        log.warning("recovered session has no readable capture.json")
        return
    extra = {**meta.extra, USER_RESTORED_KEY: True}
    write_capture_json(session_dir, dataclasses.replace(meta, confirmed=True, extra=extra))


def delete_held(meetings_root: Path, name: str) -> bool:
    d = held_dir(meetings_root, name)
    if d is None:
        return False
    shutil.rmtree(d, ignore_errors=True)
    return not d.exists()


def purge_held(meetings_root: Path, keep_days: int, *, now: datetime | None = None) -> int:
    """Borra las sesiones apartadas hace mas de ``keep_days`` dias. Devuelve cuantas."""
    base = held_root(meetings_root)
    if not base.is_dir():
        return 0
    limit = (now or _now()) - timedelta(days=keep_days)
    n = 0
    for d in list(base.iterdir()):
        if not d.is_dir():
            continue
        try:
            info = _held_info(d)
            if info.get("retain") is True:
                continue
            when = _held_at(d, info)
        except OSError:
            continue
        if when.tzinfo is None:
            when = when.astimezone()
        if when < limit:
            shutil.rmtree(d, ignore_errors=True)
            if not d.exists():
                n += 1
    if n:
        log.info("purged %d held session(s) older than %d days", n, keep_days)
    return n


__all__ = [
    "DISCARD_MARKER",
    "HELD_DIRNAME",
    "HELD_FILE",
    "REASON_DECLINED",
    "REASON_GATES",
    "REASON_INTERRUPTED",
    "REASON_UNANSWERED",
    "REASON_UNCONFIRMED",
    "HeldSession",
    "delete_held",
    "discard_reason",
    "held_dir",
    "held_root",
    "list_held",
    "mark_discarded",
    "move_to_held",
    "purge_held",
    "restore_held",
    "unmark_discarded",
]
