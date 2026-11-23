"""Puentes JS <-> Python (objetos ``js_api`` de pywebview). Sin importar ``webview``.

Reglas
------
* pywebview expone como API **todo atributo publico** del objeto: aqui los metodos publicos son
  exactamente lo que el JavaScript puede llamar; todo lo demas (ventanas, BD, callbacks) vive en
  atributos ``_privados``.
* Las llamadas JS llegan en hilos de pywebview: la biblioteca abre una ``Database`` por llamada
  (``sqlite3`` no comparte conexiones entre hilos).
* Toda entrada del JS se valida (nombres de carpeta, ids de hablante, longitudes).
* Las operaciones que cambian algo devuelven ``{"ok": bool, "error": "<clave i18n>"}`` y la UI
  traduce el error; las lecturas devuelven los datos.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from micnaga import paths
from micnaga.config import Config, ConfigError, config_from_dict
from micnaga.store.db import Database
from micnaga.store.locate import meeting_dir
from micnaga.types import SPEAKER_ME, Transcript
from micnaga.ui import i18n, settings_schema, sound, startup
from micnaga.ui.contracts import (
    BrainPort,
    ConfigStore,
    Diagnostic,
    DiagnosticsProvider,
    DownloadModels,
    HeldOps,
    LevelsProvider,
    LibraryOps,
    ModelProgress,
    UiCallbacks,
    Voiceprint,
    normalize_levels,
)
from micnaga.ui.media import valid_folder_name

log = logging.getLogger(__name__)

Result = dict[str, Any]


def ok(**extra: Any) -> Result:
    return {"ok": True, **extra}


def fail(error: str, **extra: Any) -> Result:
    return {"ok": False, "error": error, **extra}


# ===================================================================== popup + pastilla


class SurfaceController(Protocol):
    """Lo que ``UiHost`` ofrece a los puentes del popup y de la pastilla."""

    def levels(self) -> tuple[float, float]: ...

    def handle_popup_decide(self, decision: str) -> None: ...

    def handle_popup_hover(self, hovering: bool) -> None: ...

    def handle_popup_morph(self, provisional: bool) -> None: ...

    def handle_popup_closed(self, kind: str) -> None: ...

    def handle_notice_dismissed(self) -> None: ...

    def handle_popup_activate(self) -> None: ...

    def popup_state(self) -> dict[str, Any]: ...

    def handle_pill_hover(self, hovering: bool) -> None: ...

    def handle_pill_marker(self) -> None: ...

    def handle_pill_toggle_pause(self) -> None: ...

    def handle_pill_stop(self) -> None: ...

    def handle_end_decide(self, choice: str) -> None: ...

    def handle_pill_undo(self) -> None: ...

    def handle_pill_save(self) -> None: ...


class PopupApi:
    """``pywebview.api`` del popup."""

    _DECISIONS = frozenset({"record", "dismiss", "timeout"})
    _CLOSED = frozenset({"morphed", "leaving", "fading"})

    def __init__(self, controller: SurfaceController) -> None:
        self._c = controller

    def levels(self) -> list[float]:
        return list(self._c.levels())

    def popup_decide(self, decision: str) -> None:
        if decision in self._DECISIONS:
            self._c.handle_popup_decide(decision)

    def popup_hover(self, hovering: bool) -> None:
        self._c.handle_popup_hover(bool(hovering))

    def popup_morph(self, provisional: bool) -> None:
        self._c.handle_popup_morph(bool(provisional))

    def popup_closed(self, kind: str) -> None:
        if kind in self._CLOSED:
            self._c.handle_popup_closed(kind)

    def popup_end_decide(self, choice: str) -> None:
        """Popup de fin: ``finish`` | ``keep`` | ``save`` | ``discard``."""
        if choice in ("finish", "keep", "save", "discard"):
            self._c.handle_end_decide(choice)

    def popup_notice_dismissed(self) -> None:
        self._c.handle_notice_dismissed()

    def popup_activate(self) -> None:
        self._c.handle_popup_activate()

    def popup_state(self) -> dict[str, Any]:
        """``{"remaining_s", "total_s", "paused"}``: la cuenta atras autoritativa es la del
        vigilante (el JS solo la dibuja; ``paused`` = congelada por raton/pausa/senal)."""
        return self._c.popup_state()


class PillApi:
    """``pywebview.api`` de la pastilla de grabacion."""

    def __init__(self, controller: SurfaceController) -> None:
        self._c = controller

    def levels(self) -> list[float]:
        return list(self._c.levels())

    def pill_hover(self, hovering: bool) -> None:
        self._c.handle_pill_hover(bool(hovering))

    def pill_marker(self) -> None:
        self._c.handle_pill_marker()

    def pill_toggle_pause(self) -> None:
        self._c.handle_pill_toggle_pause()

    def pill_stop(self) -> None:
        self._c.handle_pill_stop()

    def pill_undo(self) -> None:
        """«Deshacer» del aviso «Descartada»."""
        self._c.handle_pill_undo()

    def pill_save(self) -> None:
        """«¿Guardar?» de la pastilla provisional: confirma la grabacion."""
        self._c.handle_pill_save()


class SurfaceApi(PillApi, PopupApi):
    """``pywebview.api`` de la superficie unica: la pastilla que crece hasta la tarjeta
    «¿Estás en una reu?» / «Reunión terminada» y vuelve a encogerse (``pill.html``)."""

    _PHASES = frozenset({"card", "collapsing"})

    def surface_phase(self, phase: str) -> None:
        """El JS termino de abrir la tarjeta (``card``) o empezo a encogerla (``collapsing``)."""
        if phase in self._PHASES:
            handler = getattr(self._c, "handle_surface_phase", None)
            if handler is not None:
                handler(phase)

    def surface_resize(self, kind: str) -> None:
        """El JS ya oculto el contenido: la ventana puede tomar la talla de ``kind``."""
        handler = getattr(self._c, "handle_surface_resize", None)
        if kind == "card" and handler is not None:
            handler(kind)

    def surface_metrics(self, width: float, height: float, dpr: float) -> None:
        """Tamano CSS de la pagina y ``devicePixelRatio`` (diagnostico de DPI, al log)."""
        handler = getattr(self._c, "handle_surface_metrics", None)
        try:
            values = (float(width), float(height), float(dpr))
        except (TypeError, ValueError):
            return
        if handler is not None:
            handler(*values)


# ============================================================================ secretaria

LIBRARY_BRAIN_ACTIONS = frozenset(
    {
        "acta",
        "apply",
        "undo",
        "dismiss",
        "analyze",
        "edit",
        "refine",
        "refine_plan",
        "refine_undo",
        "refine_status",
        "open_link",
        "full_acta",
        "full_acta_make",
        "full_acta_open",
        "caps",
        "horas_plan",
        "horas_apply",
        "horas_undo",
        "horas_off",
    }
)
SETTINGS_BRAIN_ACTIONS = frozenset(
    {
        "status",
        "claude_detect",
        "claude_test",
        "set_secret",
        "odoo_test",
        "caps",
        "odoo_learn",
        "odoo_learn_refresh",
        "odoo_learn_open",
        "learned",
        "forget",
        "restore",
        "google_status",
        "google_check",
        "google_open",
    }
)


def call_brain(
    brain: BrainPort | None, allowed: frozenset[str], action: object, args: object
) -> Result:
    """Puerta unica JS -> Secretaria: solo acciones de esa superficie, argumentos dict."""
    if brain is None:
        return fail("secretaria.err.disabled")
    if not isinstance(action, str) or action not in allowed:
        return fail("secretaria.err.unknown")
    return brain.api(action, args if isinstance(args, dict) else {})


# ============================================================================ biblioteca

_DEFAULT_SPEAKER_RE = re.compile(r"^Hablante \d+$")
MAX_NAME_LEN = 60


def clean_name(raw: object) -> str | None:
    """Nombre de persona valido (1..60 caracteres, sin controles) o ``None``."""
    if not isinstance(raw, str):
        return None
    name = " ".join(raw.split())
    if not name or len(name) > MAX_NAME_LEN or any(ord(c) < 32 for c in name):
        return None
    return name


def split_snippet(snippet: str) -> list[dict[str, Any]]:
    """``'a <mark>b</mark> c'`` -> partes ``[{t, hit}]``: el JS las pinta con ``textContent``."""
    parts: list[dict[str, Any]] = []
    for i, chunk in enumerate(re.split(r"</?mark>", snippet)):
        # los fragmentos pares quedan fuera de <mark>, los impares dentro (split alterna)
        if chunk:
            parts.append({"t": chunk, "hit": i % 2 == 1})
    return parts


class LibraryApi:
    """``pywebview.api`` de la biblioteca (MIC-14/15)."""

    def __init__(
        self,
        *,
        db_factory: Callable[[], Database],
        meetings_root: Callable[[], Path],
        audio_url: Callable[[str, str], str],
        config: Callable[[], Config],
        ops: LibraryOps | None = None,
        opener: Callable[[Path], None] | None = None,
        held: HeldOps | None = None,
        brain: BrainPort | None = None,
        open_settings: Callable[[], None] | None = None,
        settings: Callable[[], SettingsApi] | None = None,
    ) -> None:
        self._db_factory = db_factory
        self._open_settings = open_settings
        self._settings_factory = settings
        self._settings_api: SettingsApi | None = None
        self._root = meetings_root
        self._audio_url = audio_url
        self._config = config
        self._ops = ops
        self._held = held
        self._opener = opener or _startfile
        self._brain = brain
        self._hq_procs: dict[str, Any] = {}
        self._hq_lock = threading.Lock()

    # ---------------------------------------------------------------- lectura

    def bootstrap(self) -> Result:
        cfg = self._config()
        return ok(
            voiceprints_enabled=cfg.privacy.voiceprints_enabled,
            can_edit=self._ops is not None,
            has_held=self._held is not None,
            secretaria=self._brain is not None,
            hq=cfg.processing.hq_transcribe and self._ops is not None,
        )

    def open_settings(self) -> Result:
        """Abre (o trae al frente) la ventana de Ajustes desde la biblioteca."""
        if self._open_settings is None:
            return fail("library.err.not_found")
        try:
            self._open_settings()
        except Exception:
            log.exception("open_settings failed")
            return fail("library.err.not_found")
        return ok()

    def settings_call(self, name: str, args: list[Any] | None = None) -> Any:
        """Ajustes embebido en esta ventana: delega en ``SettingsApi`` (solo metodos publicos)."""
        if self._settings_factory is None or not isinstance(name, str) or name.startswith("_"):
            return fail("library.err.not_found")
        if self._settings_api is None:
            self._settings_api = self._settings_factory()
        fn = getattr(self._settings_api, name, None)
        if not callable(fn):
            return fail("library.err.not_found")
        return fn(*(args if isinstance(args, list) else []))

    # ------------------------------------------------------------- «Descartadas»

    def list_held(self) -> list[dict[str, Any]]:
        """Grabaciones apartadas sin transcribir (recuperables ``discarded_keep_days`` dias)."""
        if self._held is None:
            return []
        try:
            return self._held.list_held()
        except Exception:
            log.exception("list_held failed")
            return []

    def recover_held(self, name: str) -> Result:
        """La aparta de «Descartadas» y la encola como reunion confirmada."""
        if self._held is None or not _valid_held_name(name):
            return fail("library.err.not_found")
        try:
            self._held.recover_held(name)
        except FileNotFoundError:
            return fail("library.err.not_found")
        except Exception:
            log.exception("recover_held failed")
            return fail("library.err.op_failed")
        return ok(held=self.list_held())

    def delete_held(self, name: str) -> Result:
        if self._held is None or not _valid_held_name(name):
            return fail("library.err.not_found")
        try:
            self._held.delete_held(name)
        except Exception:
            log.exception("delete_held failed")
            return fail("library.err.op_failed")
        return ok(held=self.list_held())

    def list_meetings(self, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        with self._db_factory() as db:
            records = db.list_meetings(limit, offset)
        return [
            {
                "folder": r.folder,
                "title": r.title,
                "start": r.start,
                "duration_s": r.duration_s,
                "meet_code": r.meet_code,
                "speakers": [{"id": s.id, "name": s.name} for s in r.speakers],
            }
            for r in records
        ]

    def search(self, query: str, limit: int = 50) -> dict[str, Any]:
        """Busqueda de texto completo (FTS5). Devuelve fragmentos ya troceados y seguros."""
        started = time.perf_counter()
        query = str(query)[:200]
        limit = max(1, min(int(limit), 200))
        with self._db_factory() as db:
            hits = db.search(query, limit)
            originals = self._original_hits(
                db, query, {(h.folder, round(h.start, 2)) for h in hits}
            )
            names: dict[str, dict[str, str]] = {}
            for h in [*hits, *originals]:
                if h.folder not in names:
                    rec = db.get_meeting(h.folder)
                    names[h.folder] = {s.id: s.name for s in rec.speakers} if rec else {}
        out = [
            {
                "folder": h.folder,
                "title": h.title,
                "start": h.start,
                "speaker_id": h.speaker,
                "speaker": names.get(h.folder, {}).get(h.speaker, h.speaker),
                "parts": split_snippet(h.snippet),
            }
            for h in [*hits, *originals]
        ]
        if self._brain is not None:  # el texto de las actas tambien se encuentra
            try:
                label = i18n.t("library.acta_hit", self._config().ui.language)
                for a in self._brain.search(query, 10):
                    out.append(
                        {
                            "folder": a["folder"],
                            "title": a["title"],
                            "start": 0.0,
                            "speaker_id": "",
                            "speaker": label,
                            "parts": split_snippet(str(a["snippet"])),
                            "acta": True,
                        }
                    )
            except Exception:
                log.exception("acta search failed")
        return {
            "query": query,
            "hits": out,
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }

    @staticmethod
    def _original_hits(db: Any, query: str, have: set[tuple[str, float]]) -> list[Any]:
        """Lo que se oyo antes de la limpieza con IA (``original_fts``): la busqueda encuentra
        tambien la palabra original. Sin repetir un parrafo que ya salio por su texto nuevo."""
        try:
            from micnaga.brain import store as bstore
            from micnaga.store.db import SearchHit

            bstore.ensure_schema(db)
            out = []
            for o in bstore.search_originals(db, query, 10):
                rec = db.get_meeting(o.folder)
                if rec is None or (o.folder, round(o.start, 2)) in have:
                    continue
                out.append(SearchHit(0, o.folder, rec.title, 0, o.start, o.speaker, "", o.snippet))
            return out
        except Exception:
            log.debug("original wording search failed", exc_info=True)
            return []

    def get_meeting(self, folder: str) -> Result:
        """Transcripcion (fuente de verdad: ``transcripcion.json``) + URLs del audio."""
        path = self._meeting_json(folder)
        if path is None:
            return fail("library.err.not_found")
        try:
            tr = Transcript.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError):
            log.exception("cannot read %s", path)
            return fail("library.err.unreadable")
        d = tr.to_dict()
        from micnaga.brain.cleanup import marks

        for seg, s in zip(d["segments"], tr.segments, strict=True):
            seg.pop("words", None)  # el visor trabaja por frases; ahorra carga
            seg.pop("merged_from", None)
            if s.text_original is not None:  # limpieza con IA: palabras cambiadas y original
                seg["fix"] = marks(s)
        audio: dict[str, str | None] = {}
        for track, filename in (("yo", paths.AUDIO_ME_FLAC), ("remoto", paths.AUDIO_REMOTE_FLAC)):
            audio[track] = (
                self._audio_url(folder, track) if (path.parent / filename).is_file() else None
            )
        return ok(folder=folder, transcript=d, audio=audio)

    def list_voiceprints(self) -> list[dict[str, Any]]:
        if self._ops is None:
            return []
        return [dataclasses.asdict(v) for v in self._ops.list_voiceprints()]

    # ------------------------------------------------------------- operaciones

    def rename_speaker(self, folder: str, speaker_id: str, name: str) -> Result:
        cleaned = clean_name(name)
        if cleaned is None:
            return fail("library.err.invalid_name")
        speaker_id = str(speaker_id)
        guard = self._guard(folder, speaker_id)
        if guard is not None:
            return guard
        assert self._ops is not None
        try:
            self._ops.rename_speaker(folder, speaker_id, cleaned)
        except Exception:
            log.exception("rename_speaker failed")
            return fail("library.err.op_failed")
        return self.get_meeting(folder)

    def merge_speakers(self, folder: str, keep_id: str, merge_id: str) -> Result:
        keep_id, merge_id = str(keep_id), str(merge_id)
        if keep_id == merge_id or SPEAKER_ME in (keep_id, merge_id):
            return fail("library.err.cannot_merge")
        guard = self._guard(folder, keep_id) or self._guard(folder, merge_id)
        if guard is not None:
            return guard
        assert self._ops is not None
        try:
            self._ops.merge_speakers(folder, keep_id, merge_id)
        except Exception:
            log.exception("merge_speakers failed")
            return fail("library.err.op_failed")
        return self.get_meeting(folder)

    def save_voiceprint(self, folder: str, speaker_id: str, consent: bool) -> Result:
        """MIC-11. Exige el opt-in de Ajustes **y** el consentimiento del dialogo (§13)."""
        if not self._config().privacy.voiceprints_enabled:
            return fail("library.err.voiceprints_off")
        if consent is not True:
            return fail("library.err.consent_required")
        speaker_id = str(speaker_id)
        if speaker_id == SPEAKER_ME:
            return fail("library.err.cannot_merge")
        guard = self._guard(folder, speaker_id)
        if guard is not None:
            return guard
        path = self._meeting_json(folder)
        assert path is not None and self._ops is not None
        try:
            tr = Transcript.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError):
            return fail("library.err.unreadable")
        name = next((s.name for s in tr.speakers if s.id == speaker_id), "")
        if _DEFAULT_SPEAKER_RE.match(name):
            return fail("library.err.name_first")  # una huella sin nombre no sirve de nada
        try:
            self._ops.save_voiceprint(folder, speaker_id)
        except Exception:
            log.exception("save_voiceprint failed")
            return fail("library.err.op_failed")
        return ok(voiceprints=self.list_voiceprints())

    def delete_voiceprint(self, name: str) -> Result:
        cleaned = clean_name(name)
        if cleaned is None or self._ops is None:
            return fail("library.err.invalid_name")
        try:
            self._ops.delete_voiceprint(cleaned)
        except Exception:
            log.exception("delete_voiceprint failed")
            return fail("library.err.op_failed")
        return ok(voiceprints=self.list_voiceprints())

    def secretaria(self, action: str, args: dict[str, Any] | None = None) -> Result:
        """Acta, propuestas (aplicar / deshacer / descartar) y «Analizar» de una reunion."""
        return call_brain(self._brain, LIBRARY_BRAIN_ACTIONS, action, args)

    def hq(self, action: str, folder: str) -> Result:
        """«Mejorar transcripcion» (``pipeline.hq``): ``info`` | ``start`` | ``status`` |
        ``cancel`` | ``restore``. El trabajo es un proceso aparte (``micnaga hq``)."""
        from micnaga.pipeline import hq

        cfg = self._config()
        if not cfg.processing.hq_transcribe or self._ops is None:
            return fail("library.err.read_only")
        path = self._meeting_json(folder)
        if path is None:
            return fail("library.err.not_found")
        meeting = path.parent
        if action == "info":
            return ok(
                minutes=hq.estimate_minutes(meeting, hq.model_name(cfg)),
                download_mb=round(hq.download_bytes(cfg) / 1e6),
                model=hq.model_name(cfg),
                previa=hq.has_previa(meeting),
                status=self._hq_status(meeting),
            )
        if action == "status":
            return ok(status=self._hq_status(meeting), previa=hq.has_previa(meeting))
        if action == "start":
            if self._hq_alive(meeting):
                return ok(status=self._hq_status(meeting))
            try:
                proc = hq.spawn(meeting)
            except Exception:
                log.exception("could not start the hq transcription")
                return fail("library.err.op_failed")
            with self._hq_lock:
                self._hq_procs[meeting.name] = proc
            threading.Thread(
                target=self._hq_wait, args=(meeting, proc), name="micnaga-hq-wait", daemon=True
            ).start()
            return ok(status=self._hq_status(meeting))
        if action == "cancel":
            hq.request_cancel(meeting)
            return ok()
        if action == "restore":
            if self._hq_alive(meeting):
                return fail("library.err.op_failed")
            try:
                restored = hq.restore(meeting)
            except Exception:
                log.exception("hq restore failed")
                return fail("library.err.op_failed")
            if not restored:
                return fail("library.err.not_found")
            return self.get_meeting(folder)
        return fail("library.err.op_failed")

    def _hq_alive(self, meeting: Path) -> bool:
        from micnaga.pipeline import hq
        from micnaga.pipeline.worker import mutex_exists

        with self._hq_lock:
            proc = self._hq_procs.get(meeting.name)
        if proc is not None and proc.poll() is None:
            return True
        return mutex_exists(hq.mutex_name(meeting))  # lanzado por otra ventana o antes

    def _hq_status(self, meeting: Path) -> dict[str, Any] | None:
        from micnaga.pipeline import hq

        st = hq.read_status(meeting)
        if st and st.get("state") == "running" and not self._hq_alive(meeting):
            return None  # un estado «en marcha» huerfano (la app se reinicio, o lo mataron)
        return st

    def _hq_wait(self, meeting: Path, proc: Any) -> None:
        """Al terminar bien: limpieza con IA y acta otra vez (si la Secretaria esta activa)."""
        from micnaga.pipeline import hq

        proc.wait()
        st = hq.read_status(meeting) or {}
        if st.get("state") == "done" and self._brain is not None:
            try:
                self._brain.api("analyze", {"folder": meeting.name})
            except Exception:
                log.exception("secretaria re-run after hq failed")

    def open_folder(self, folder: str) -> Result:
        target = self._meeting_dir(folder)
        if target is None:
            return fail("library.err.not_found")
        self._opener(target)
        return ok()

    # ---------------------------------------------------------------- privados

    def _meeting_json(self, folder: object) -> Path | None:
        d = meeting_dir(self._root(), folder, self._db_factory, require=paths.TRANSCRIPT_JSON)
        return None if d is None else d / paths.TRANSCRIPT_JSON

    def _meeting_dir(self, folder: object) -> Path | None:
        """Bajo la raiz de ahora o por la ruta del indice (``micnaga.store.locate``, P1-4)."""
        return meeting_dir(self._root(), folder, self._db_factory)

    def _guard(self, folder: str, speaker_id: str) -> Result | None:
        """Comprobaciones comunes: operaciones disponibles, reunion y hablante existen."""
        if self._ops is None:
            return fail("library.err.read_only")
        path = self._meeting_json(folder)
        if path is None:
            return fail("library.err.not_found")
        try:
            tr = Transcript.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError):
            return fail("library.err.unreadable")
        if speaker_id not in {s.id for s in tr.speakers}:
            return fail("library.err.no_speaker")
        return None


def _valid_held_name(name: object) -> bool:
    return isinstance(name, str) and valid_folder_name(name)


def _startfile(path: Path) -> None:  # pragma: no cover - abre el Explorador real
    os.startfile(path)  # type: ignore[attr-defined,unused-ignore]


# ============================================================================== ajustes


class SettingsApi:
    """``pywebview.api`` de Ajustes: todo ``config.toml`` con un formulario."""

    def __init__(
        self,
        *,
        store: ConfigStore,
        callbacks: UiCallbacks,
        registry: startup.RegistryBackend | None = None,
        diagnostics: DiagnosticsProvider | None = None,
        ops: LibraryOps | None = None,
        folder_picker: Callable[[str], str | None] | None = None,
        on_applied: Callable[[Config], None] | None = None,
        brain: BrainPort | None = None,
    ) -> None:
        self._brain = brain
        self._store = store
        self._cb = callbacks
        self._registry = registry
        self._diagnostics = diagnostics
        self._ops = ops
        self._picker = folder_picker
        self._on_applied = on_applied

    def get_settings(self) -> dict[str, Any]:
        cfg: Config = self._store.load()
        values = cfg.to_dict()
        # el interruptor de arranque muestra el estado REAL del registro (puede haber cambiado
        # fuera de la app); config.toml guarda la intencion.
        values["startup"]["run_at_login"] = startup.is_run_at_login(backend=self._registry)
        return {
            "values": values,
            "schema": settings_schema.schema_for_js(),
            "has_diagnostics": self._diagnostics is not None,
            "has_voiceprints": self._ops is not None,
            "has_secretaria": self._brain is not None,
        }

    def save_settings(self, values: dict[str, Any]) -> Result:
        if not isinstance(values, dict):
            return fail("settings.err.invalid")
        try:
            current: Config = self._store.load()
            merged = settings_schema.deep_merge(current.to_dict(), _only_known(values))
            cfg = config_from_dict(merged)
        except ConfigError as exc:
            return fail("settings.err.invalid", detail=str(exc))
        try:
            self._store.save(cfg)
        except (OSError, ConfigError) as exc:
            log.exception("cannot save config")
            return fail("settings.err.save_failed", detail=str(exc))
        run = bool(cfg.startup.run_at_login)
        if run != startup.is_run_at_login(backend=self._registry):
            try:
                startup.set_run_at_login(run, backend=self._registry)
            except (OSError, startup.RegistryWriteBlockedError):
                log.exception("cannot update run-at-login")
                return fail("settings.err.startup_failed", saved=True)
        if self._on_applied is not None:
            self._on_applied(cfg)
        self._cb.on_settings_saved(cfg)
        return ok()

    def diagnostics(self) -> list[dict[str, str]]:
        if self._diagnostics is None:
            return []
        try:
            items: list[Diagnostic] = self._diagnostics()
        except Exception:
            log.exception("diagnostics provider failed")
            return []
        return [dataclasses.asdict(d) for d in items]

    def list_voiceprints(self) -> list[dict[str, Any]]:
        if self._ops is None:
            return []
        vps: list[Voiceprint] = self._ops.list_voiceprints()
        return [dataclasses.asdict(v) for v in vps]

    def delete_voiceprint(self, name: str) -> Result:
        cleaned = clean_name(name)
        if cleaned is None or self._ops is None:
            return fail("library.err.invalid_name")
        try:
            self._ops.delete_voiceprint(cleaned)
        except Exception:
            log.exception("delete_voiceprint failed")
            return fail("library.err.op_failed")
        return ok(voiceprints=self.list_voiceprints())

    def secretaria(self, action: str, args: dict[str, Any] | None = None) -> Result:
        """Conectar Claude / Odoo (los secretos entran y nunca salen) y «Lo que ha aprendido»."""
        return call_brain(self._brain, SETTINGS_BRAIN_ACTIONS, action, args)

    def pick_folder(self, current: str = "") -> str | None:
        return self._picker(str(current)) if self._picker is not None else None


def _only_known(values: dict[str, Any]) -> dict[str, Any]:
    """Descarta secciones/claves desconocidas (el JS no puede introducir campos nuevos)."""
    known = Config().to_dict()
    out: dict[str, Any] = {}
    for section, body in values.items():
        if section in known and isinstance(body, dict):
            out[section] = {k: v for k, v in body.items() if k in known[section]}
    return out


# ============================================================================== asistente


class WizardApi:
    """``pywebview.api`` del asistente de primer arranque (3 pasos, §11)."""

    def __init__(
        self,
        *,
        download_models: DownloadModels | None,
        levels: LevelsProvider,
        callbacks: UiCallbacks,
        play_test: Callable[[], None] | None = None,
        auto_finish: bool = False,
    ) -> None:
        self._auto_finish = auto_finish
        self._download = download_models
        self._levels = levels
        self._cb = callbacks
        self._play_test = play_test or (lambda: sound.play_chime(enabled=True, dnd=lambda: False))
        self._lock = threading.Lock()
        self._models: dict[str, ModelProgress] = {}
        self._state = "idle"  # idle | running | done | failed
        self._error = ""
        self._thread: threading.Thread | None = None

    # ---- paso 2
    def start_download(self) -> Result:
        if self._download is None:
            return fail("wizard.err.no_downloader")
        with self._lock:
            if self._state == "running":
                return ok()
            self._state, self._error, self._models = "running", "", {}
        self._thread = threading.Thread(
            target=self._run_download, name="micnaga-models", daemon=True
        )
        self._thread.start()
        return ok()

    def _run_download(self) -> None:
        assert self._download is not None
        try:
            self._download(self._on_progress)
        except Exception as exc:
            log.exception("model download failed")
            with self._lock:
                self._state, self._error = "failed", str(exc)
            return
        with self._lock:
            bad = [m for m in self._models.values() if m.state == "failed"]
            self._state = "failed" if bad else "done"
            if bad:
                self._error = bad[0].message or bad[0].name
            finished = self._state == "done"
        if finished and self._auto_finish:
            # Sin decisiones: terminada la descarga se cierra el asistente y la app pasa a la
            # bandeja (equivale a pulsar «Terminar»).
            self.finish()

    def _on_progress(self, ev: ModelProgress) -> None:
        with self._lock:
            self._models[ev.name] = ev

    def download_status(self) -> dict[str, Any]:
        with self._lock:
            models = [dataclasses.asdict(m) for m in self._models.values()]
            done = sum(m.done for m in self._models.values())
            total = sum(m.total for m in self._models.values())
            verified = sum(1 for m in self._models.values() if m.state in ("verified", "skipped"))
            overall = (
                1.0 if self._state == "done" else (min(1.0, done / total) if total > 0 else 0.0)
            )
            return {
                "state": self._state,
                "error": self._error,
                "models": models,
                "overall": overall,
                "verified": verified,
            }

    # ---- paso 3
    def levels(self) -> list[float]:
        return list(normalize_levels(self._levels()))

    def monitor(self, active: bool) -> None:
        """Pide/libera el monitor de niveles (el integrador abre los dispositivos)."""
        self._cb.on_levels_monitor(bool(active))

    def play_test_sound(self) -> None:
        self._play_test()

    def finish(self) -> Result:
        self._cb.on_first_run_done()
        return ok()
