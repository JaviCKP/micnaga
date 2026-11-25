"""La Secretaria dentro del proceso de la app: cola serie, reintentos y API para la UI.

* **Disparo:** ``on_job_done`` (evento ``job_done`` del worker). El aviso «Lista» sale al
  guardar la transcripcion. La Secretaria encola el acta y las propuestas por separado.
* **Cola:** tabla ``secretary_runs`` (``micnaga.brain.store``); un hilo serie (E/S pura: un
  subproceso y HTTP). Al arrancar: lo que quedo ``running`` vuelve a ``pending`` y se recogen las
  reuniones de las ultimas 24 h sin acta. Los fallos se reintentan con espera creciente (sin red,
  sin Claude, limite de uso hasta su hora de reinicio).
* **Si la pasada falla** y hay titulo provisional (``titulo_provisional.json``), se usa ese si el
  titulo es generico.
* ``api(action, args)``: lo que llaman la biblioteca y Ajustes (``LibraryApi.secretaria`` /
  ``SettingsApi.secretaria``). Los secretos entran por aqui y **nunca** se devuelven.
* **Odoo**: perfil de capacidades (``capabilities``) al pulsar «Probar» y una vez al dia en el
  tic; panel «Rellenar partes de horas» (``horas_plan`` / ``horas_apply`` / ``horas_undo`` /
  ``horas_off``) y el aviso del viernes por la tarde si la semana tiene huecos
  (``secretaria.horas_nudge``, apagado por defecto).
* **«Conocer tu Odoo»** (``micnaga.brain.odoo_learn``): un escaneo de solo lectura al conectar
  (o al «Probar» una conexion nueva), una vez por semana en segundo plano (solo con la cola vacia
  y nunca grabando: se para si empieza una grabacion) y con «Actualizar» (``odoo_learn*``).
* **«Corregir con un mensaje»** (``refine`` / ``refine_plan`` / ``refine_undo`` /
  ``refine_status``, ``micnaga.brain.refine``): un ``claude -p`` corto de solo lectura revisa una
  propuesta o la lista; el progreso se consulta mientras dura.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from micnaga import paths
from micnaga.brain import acta as acta_io
from micnaga.brain import learn, secrets
from micnaga.brain import store as bstore
from micnaga.brain.google.service_api import GoogleApiMixin
from micnaga.config import Config

log = logging.getLogger(__name__)

# respuestas asincronas de la revision: llevan la reunion a la que pertenecen (la vista ignora
# la respuesta de una reunion que ya no es la que se mira)
_ECHO_FOLDER = frozenset(
    {
        "acta",
        "apply",
        "undo",
        "edit",
        "dismiss",
        "refine",
        "refine_plan",
        "refine_undo",
        "refine_status",
        "analyze",
    }
)

TICK_S = 60.0
BOOT_WINDOW_H = 24
ODOO_RETRY_S = 3600.0

Notify = Callable[[str, str, float, int, int], None]
"""``notify(carpeta, titulo, duracion_s, hablantes, propuestas)``."""
Nudge = Callable[[int], None]
"""``nudge(dias_con_huecos)``: aviso «Te faltan horas esta semana»."""


class BrainService(GoogleApiMixin):
    def __init__(
        self,
        config: Callable[[], Config],
        *,
        db_factory: Callable[[], Any] | None = None,
        secretary_factory: Callable[[Config], Any] | None = None,
        runner_factory: Callable[[Config], Any] | None = None,
        odoo_factory: Callable[..., Any] | None = None,
        tick_s: float = TICK_S,
    ) -> None:
        self._config = config
        self._db_factory = db_factory
        self._secretary_factory = secretary_factory
        self._runner_factory = runner_factory
        self._odoo_factory = odoo_factory
        self.tick_s = tick_s
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status: dict[str, dict[str, Any]] = {}
        self._renames: dict[str, str] = {}
        self._queued: set[str] = set()
        self._started_at = time.time()
        self._nudge: Nudge | None = None
        self.now: Callable[[], datetime] = datetime.now
        self._odoo_failed_at = float("-inf")  # monotonic cuenta desde el arranque de Windows
        self._acta_lock = threading.RLock()
        """Escrituras de ``acta.json`` desde la UI (aplicar, editar, revisar): una a la vez."""
        self._refining: dict[str, dict[str, Any]] = {}
        self._full: dict[str, dict[str, Any]] = {}
        """«Acta completa» en curso por carpeta (texto en streaming) o su ultimo resultado."""
        self._open_file: Callable[[Path], None] = _startfile
        self._busy: Callable[[], bool] = lambda: False
        """¿Se esta grabando? (lo pone la app con ``set_busy``)."""
        self._learn_thread: threading.Thread | None = None
        self._learn_lock = threading.Lock()
        self._learn_failed_at = 0.0
        self._learn_error: str | None = None
        self._spawn: Callable[[Callable[[], None]], Any] = _spawn

    def set_nudge(self, nudge: Nudge | None) -> None:
        """La UI pone aqui como ensenar el aviso semanal de horas (toast)."""
        self._nudge = nudge

    def set_busy(self, busy: Callable[[], bool] | None) -> None:
        """La app dice aqui si esta grabando: entonces no se explora Odoo en segundo plano."""
        self._busy = busy or (lambda: False)

    def _recording(self) -> bool:
        try:
            return bool(self._busy())
        except Exception:
            return True  # ante la duda, no molestar

    # ------------------------------------------------------------------ ciclo de vida

    @property
    def config(self) -> Config:
        return self._config()

    @property
    def enabled(self) -> bool:
        return bool(self.config.secretaria.enabled)

    def _db(self) -> Any:
        if self._db_factory is not None:
            db = self._db_factory()
        else:
            from micnaga.store.db import open_database

            db = open_database(self.config)
        bstore.ensure_schema(db)
        return db

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._run, name="micnaga-secretaria", daemon=True)
        self._thread.start()
        threading.Thread(target=self._boot, name="micnaga-secretaria-boot", daemon=True).start()

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout_s)

    def _boot(self) -> None:
        if not self.enabled:
            return
        try:
            db = self._db()
            try:
                bstore.requeue_running(db)
                since = (datetime.now() - timedelta(hours=BOOT_WINDOW_H)).isoformat()
                root = paths.meetings_root(self.config)
                for rec in db.list_meetings(200):
                    if rec.start[:19] < since[:19]:
                        continue
                    folder = root / rec.folder
                    has = (folder / paths.TRANSCRIPT_JSON).is_file()
                    if has and not (folder / acta_io.ACTA_JSON).is_file():
                        # lo que fallo antes (sin sesion de Claude, sin red...) vuelve a probar
                        bstore.enqueue(db, rec.folder, failed_before=self._started_at)
                # P2-c: actas pendientes marcadas por el worker, de cualquier fecha y numero
                for mark in sorted(root.glob(f"*/{paths.ANALYSIS_PENDING}")):
                    if (mark.parent / acta_io.ACTA_JSON).is_file():
                        mark.unlink(missing_ok=True)
                    elif (mark.parent / paths.TRANSCRIPT_JSON).is_file():
                        bstore.enqueue(db, mark.parent.name, failed_before=self._started_at)
                due = bstore.due(db)
            finally:
                db.close()
            for f in due:
                self._submit(f)
        except Exception:
            log.exception("secretaria boot scan failed")

    # ------------------------------------------------------------------ disparo

    def on_job_done(
        self, folder: str, title: str, duration_s: float, speakers: int, notify: Notify
    ) -> None:
        """Avisa de que el texto se puede abrir y encola el acta en segundo plano."""
        try:
            notify(folder, title, duration_s, speakers, 0)
        except Exception:
            log.exception("toast notify failed")
        if not self.enabled:
            return
        try:
            self.analyze(folder, force=False)
        except Exception:
            log.exception("could not enqueue the secretaria run")

    def analyze(self, folder: str, *, force: bool = True) -> None:
        """«Analizar» / «Rehacer» desde la biblioteca."""
        db = self._db()
        try:
            bstore.enqueue(db, folder, force=force)
        finally:
            db.close()
        self._submit(folder)

    def _submit(self, folder: str) -> None:
        with self._lock:
            if folder in self._queued:
                return
            self._queued.add(folder)
            self._status[folder] = {"fase": "en_cola"}
        self._queue.put(folder)

    # ------------------------------------------------------------------ hilo

    def _run(self) -> None:
        last_tick = time.monotonic()
        while not self._stop.is_set():
            try:
                folder = self._queue.get(timeout=1.0)
            except queue.Empty:
                folder = ""
            if folder is None:
                return
            if folder:
                with self._lock:
                    self._queued.discard(folder)
                try:
                    self._process(folder)
                except Exception:
                    log.exception("secretaria run crashed")
            if time.monotonic() - last_tick >= self.tick_s:
                last_tick = time.monotonic()
                self._tick()

    def _tick(self) -> None:
        if not self.enabled:
            return
        try:
            db = self._db()
            try:
                due = bstore.due(db)
            finally:
                db.close()
            for f in due:
                self._submit(f)
        except Exception:
            log.debug("secretaria tick failed", exc_info=True)
        self._odoo_daily()
        self._odoo_learn_weekly()

    def _odoo_daily(self) -> None:
        """Perfil de capacidades una vez al dia y, el viernes por la tarde, el aviso de horas."""
        cfg = self.config.secretaria
        if not cfg.odoo_url or time.monotonic() - self._odoo_failed_at < ODOO_RETRY_S:
            return  # sin Odoo o fallo reciente: no se reintenta el login en cada tic
        try:
            from micnaga.brain import capabilities, horas

            now = self.now()
            state = horas.load_state()
            nudge = horas.should_nudge(now, enabled=cfg.horas_nudge, last=state["ultimo_aviso"])
            client = None
            key = capabilities.connection_key(cfg.odoo_url, cfg.odoo_db, cfg.odoo_login)
            if capabilities.is_stale(capabilities.load(key), now=now) or nudge:
                client = self._odoo_client()
            if client is None:
                return
            profile = capabilities.ensure(client, now=now)
            if not nudge or capabilities.features(profile).get("horas") != "escritura":
                return
            gaps = horas.week_gaps(client, now.date())
            state = horas.load_state()
            state["ultimo_aviso"] = now.date().isoformat()
            horas.save_state(state)
            if gaps and self._nudge is not None:
                self._nudge(len(gaps))
        except Exception as exc:
            self._odoo_failed_at = time.monotonic()
            log.info("odoo daily check skipped: %s", getattr(exc, "kind", type(exc).__name__))

    # ------------------------------------------------------------------ «Conocer tu Odoo»

    def _odoo_learn_weekly(self) -> None:
        """Al conectar y luego cada semana: con la cola vacia, sin grabar y sin fallo reciente."""
        from micnaga.brain import odoo_learn

        key = odoo_learn.connection_key_for(self.config.secretaria)
        if key is None or self._learning() or self._recording() or not self._queue.empty():
            return
        if time.monotonic() - self._learn_failed_at < ODOO_RETRY_S and self._learn_failed_at:
            return
        if not odoo_learn.is_stale(odoo_learn.load_state(key), now=self.now()):
            return
        self._start_learn(self._odoo_client, background=True)

    def _learning(self) -> bool:
        t = self._learn_thread
        return t is not None and t.is_alive()

    def _start_learn(self, make_client: Callable[[], Any], *, background: bool) -> bool:
        """Lanza el escaneo en su hilo (uno a la vez). ``background``: se para si se graba."""
        with self._learn_lock:
            if self._learning():
                return False
            self._learn_error = None

            def work() -> None:
                self._learn_run(make_client, background=background)

            self._learn_thread = self._spawn(work)
        return True

    def _learn_run(self, make_client: Callable[[], Any], *, background: bool) -> None:
        from micnaga.brain import odoo_learn

        def stop() -> bool:
            return self._stop.is_set() or (background and self._recording())

        try:
            client = make_client()
            if client is None:
                return
            odoo_learn.refresh(client, now=self.now(), stop=stop)
            self._learn_failed_at = 0.0
        except odoo_learn.ScanStopped:
            log.info("odoo scan paused (recording)")
        except Exception as exc:
            self._learn_failed_at = time.monotonic()
            self._learn_error = str(getattr(exc, "kind", "failed"))
            log.info("odoo scan failed: %s", self._learn_error)

    def _api_odoo_learn(self, args: dict[str, Any]) -> dict[str, Any]:
        """«Lo que sabe de tu Odoo»: ultima actualizacion y cuantas cosas (sin red)."""
        from micnaga.brain import odoo_learn

        key = odoo_learn.connection_key_for(self.config.secretaria)
        return {
            "ok": True,
            **odoo_learn.status(key),
            "escaneando": self._learning(),
            "error": self._learn_error,
            "hay": bool(key and odoo_learn.context_text(key)),
            "conectado": key is not None,
        }

    def _api_odoo_learn_refresh(self, args: dict[str, Any]) -> dict[str, Any]:
        """«Actualizar»: escanea ya (aunque no toque)."""
        from micnaga.brain import odoo_learn

        if odoo_learn.connection_key_for(self.config.secretaria) is None:
            return {"ok": False, "error": "secretaria.err.odoo_incomplete"}
        self._start_learn(self._odoo_client, background=False)
        return self._api_odoo_learn({})

    def _api_odoo_learn_open(self, args: dict[str, Any]) -> dict[str, Any]:
        """«Ver»: abre el ``contexto.md`` de la conexion configurada."""
        from micnaga.brain import odoo_learn

        key = odoo_learn.connection_key_for(self.config.secretaria)
        target = odoo_learn.context_path(key) if key else None
        if target is None or not target.is_file():
            return {"ok": False, "error": "library.err.not_found"}
        self._open_file(target)
        return {"ok": True}

    def _secretary(self) -> Any:
        cfg = self.config
        if self._secretary_factory is not None:
            return self._secretary_factory(cfg)
        from micnaga.brain.secretary import Secretary

        kwargs: dict[str, Any] = {"db_factory": self._db}
        if self._runner_factory is not None:
            kwargs["runner"] = self._runner_factory(cfg)
        if self._odoo_factory is not None:
            kwargs["odoo_factory"] = self._odoo_factory
        return Secretary(cfg, **kwargs)

    def _process(self, folder: str) -> None:
        root = paths.meetings_root(self.config)
        path = root / folder
        db = self._db()
        try:
            if not (path / paths.TRANSCRIPT_JSON).is_file():
                bstore.finish(db, folder, proposals=0, state="skipped")
                return
            if not self.enabled or not bstore.claim(db, folder):
                return
        finally:
            db.close()

        def on_status(s: dict[str, Any]) -> None:
            with self._lock:
                self._status[folder] = s

        final = folder
        try:
            secretary = self._secretary()
            if hasattr(secretary, "clean"):  # transcripcion limpia antes del acta (§23)
                secretary.clean(path, on_status=on_status)
            result = secretary.analyze(path, on_status=on_status)
            final = result.folder.name
            db = self._db()
            try:
                bstore.finish(db, final, proposals=result.proposals)
            finally:
                db.close()
            with self._lock:
                self._status.pop(folder, None)
                self._status[final] = {"fase": "lista"}
            self._maybe_consolidate()
        except Exception as exc:
            kind = getattr(exc, "kind", "failed")
            resets = getattr(exc, "resets_at", None)
            log.warning("secretaria failed for a meeting: %s (%s)", kind, type(exc).__name__)
            db = self._db()
            try:
                bstore.fail(db, folder, f"{type(exc).__name__}: {exc}", kind, retry_at=resets)
            finally:
                db.close()
            with self._lock:
                self._status[folder] = {"fase": "error", "error": kind}
            final = self._apply_live_title(path) or folder
        if final != folder:
            with self._lock:
                self._renames[folder] = final

    def _apply_live_title(self, path: Path) -> str | None:
        title = acta_io.read_live_title(path)
        if not title:
            return None
        try:
            from micnaga.brain.secretary import is_generic_title, retitle_meeting
            from micnaga.pipeline import output

            if not is_generic_title(output.read_transcript(path).meeting.title):
                return None

            def with_db(fn: Callable[[Any], Any]) -> Any:
                db = self._db()
                try:
                    return fn(db)
                finally:
                    db.close()

            final, _ = retitle_meeting(path, title, with_db)
            return final.name
        except Exception:
            log.exception("could not apply the provisional title")
            return None

    def _maybe_consolidate(self) -> None:
        if not learn.needs_consolidation():
            return
        try:
            from micnaga.brain.claude import ClaudeRequest

            runner = self._runner()

            def ask(text: str) -> dict[str, Any] | None:
                res = runner.run(
                    ClaudeRequest(
                        prompt=text,
                        system_prompt=(
                            "Reescribe esta lista de hechos de negocio de forma compacta: fusiona"
                            " duplicados, quita lo obsoleto, sin datos personales ni importes."
                        ),
                        model="sonnet",
                        schema=learn.CONSOLIDATE_SCHEMA,
                        max_turns=3,
                        timeout_s=120,
                    )
                )
                return res.structured  # type: ignore[no-any-return]

            learn.consolidate(ask)
        except Exception:
            log.exception("learned.md consolidation failed")

    def _runner(self) -> Any:
        if self._runner_factory is not None:
            return self._runner_factory(self.config)
        from micnaga.brain.claude import ClaudeRunner

        return ClaudeRunner(auth=self.config.secretaria.auth)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ estado

    def resolve_folder(self, folder: str) -> str:
        """Nombre actual de una carpeta que la Secretaria renombro (por su titulo)."""
        with self._lock:
            seen: set[str] = set()
            while folder in self._renames and folder not in seen:
                seen.add(folder)
                folder = self._renames[folder]
        return folder

    def status(self, folder: str) -> dict[str, Any] | None:
        with self._lock:
            s = self._status.get(folder)
            return dict(s) if s else None

    # ================================================================== API de la UI

    def api(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, "_api_" + action, None)
        if handler is None:
            return {"ok": False, "error": "secretaria.err.unknown"}
        try:
            out: dict[str, Any] = handler(args)
            if (
                action in _ECHO_FOLDER
                and isinstance(out.get("acta"), dict)
                and isinstance(args.get("folder"), str)
            ):
                # cada respuesta dice de que reunion es: la vista ignora la de otra reunion
                out.setdefault("folder", args["folder"])
            if isinstance(out.get("acta"), dict):  # items con su esquema editable
                from micnaga.brain.plan import decorate, fingerprint

                decorate(out["acta"])
                from micnaga.brain.google.items import decorate as decorate_google

                decorate_google(out["acta"])  # items de Google (su propio esquema)
                self._decorate_links(out["acta"])
                for p in out["acta"].get("propuestas") or []:
                    # la version exacta que ve el usuario: «Aplicar» solo vale para ella
                    if isinstance(p, dict) and p.get("uid"):
                        p["huella"] = fingerprint(p)
            return out
        except Exception as exc:
            log.exception("secretaria api %s failed", action)
            return {"ok": False, "error": "secretaria.err." + str(getattr(exc, "kind", "failed"))}

    # ---- biblioteca

    def _folder_path(self, args: dict[str, Any]) -> Path | None:
        from micnaga.ui.media import valid_folder_name

        folder = args.get("folder")
        if not isinstance(folder, str) or not valid_folder_name(folder):
            return None
        root = paths.meetings_root(self.config).resolve()
        path = (root / folder).resolve()
        if not path.is_relative_to(root) or not path.is_dir():
            return None
        return path

    def _api_acta(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._folder_path(args)
        if path is None:
            with self._lock:  # renombrada por el titulo del acta mientras se miraba
                moved = self._renames.get(str(args.get("folder")))
            return {"ok": False, "error": "library.err.not_found", "moved": moved}
        acta = acta_io.read_acta(path)
        if acta is not None and any(not p.get("uid") for p in acta.get("propuestas") or []):
            from micnaga.brain.plan import ensure_uids

            with self._acta_lock:  # actas antiguas: cada propuesta recibe su identidad
                acta = acta_io.read_acta(path) or acta
                if ensure_uids(acta.get("propuestas") or []):
                    acta_io.write_acta(path, acta)
        if acta is not None:
            from micnaga.brain.plan import relabel
            from micnaga.pipeline import output

            with contextlib.suppress(OSError, ValueError, KeyError):
                relabel(acta, output.read_transcript(path))
        db = self._db()
        try:
            run = bstore.get_run(db, path.name)
        finally:
            db.close()
        state = run.state if run else None
        return {
            "ok": True,
            "enabled": self.enabled,
            "acta": acta,
            "run": None
            if run is None
            else {"state": state, "error": run.error_kind, "attempts": run.attempts},
            "status": self.status(path.name),
        }

    def _odoo_client(self) -> Any:
        if self._odoo_factory is not None:
            return self._odoo_factory(self.config)
        from micnaga.brain.secretary import default_odoo_factory

        return default_odoo_factory(self.config)

    def _executor(self, path: Path) -> Any:
        from micnaga.brain.executor import Executor

        return Executor(
            path,
            odoo=self._odoo_client,
            today=self.now().date(),
            google=lambda: self._google_applier(path),
        )

    # ---- partes de horas (panel fuera de las reuniones)

    def _connected(self) -> Any:
        client = self._odoo_client()
        if client is None:
            from micnaga.brain.odoo import OdooError

            raise OdooError("odoo not configured", "incomplete")
        client.authenticate()
        return client

    def _meeting_evidence(self, since: str) -> list[Any]:
        """Reuniones grabadas en Micnaga desde ``since`` (titulo, hora local, duracion)."""
        from micnaga.brain.timesheets import Evidence

        out: list[Evidence] = []
        db = self._db()
        try:
            records = db.list_meetings(500)
        finally:
            db.close()
        for rec in records:
            try:
                start = datetime.fromisoformat(rec.start)
            except ValueError:
                continue
            if start.tzinfo is not None:
                start = start.astimezone()
            day = start.date().isoformat()
            if day < since or rec.duration_s < 300:
                continue
            out.append(
                Evidence(day, rec.title, start.strftime("%H:%M"), rec.duration_s / 3600, "reunion")
            )
        return out

    def _api_horas_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        from micnaga.brain import capabilities, horas

        client = self._connected()
        client.read_only = True  # preparar el plan solo lee
        profile = capabilities.ensure(client)
        if capabilities.features(profile).get("horas") != "escritura":
            return {"ok": False, "error": "secretaria.horas.err.no_access"}
        today = self.now().date()
        since = (today - timedelta(days=horas.LOOKBACK_DAYS)).isoformat()
        out = horas.plan(
            client,
            today=today,
            meetings=self._meeting_evidence(since),
            calendar=capabilities.can(profile, "calendar.event"),
        )
        return {"ok": True, **out}

    def _api_horas_apply(self, args: dict[str, Any]) -> dict[str, Any]:
        from micnaga.brain import horas

        lines = args.get("lineas")
        if not isinstance(lines, list) or not lines:
            return {"ok": False, "error": "secretaria.err.invalid"}
        batch = args.get("lote")  # reintento de un lote parcial
        out = horas.apply(
            self._connected(),
            lines,
            today=self.now().date(),
            batch=batch if isinstance(batch, str) else None,
        )
        return {"ok": True, **out}

    def _api_horas_undo(self, args: dict[str, Any]) -> dict[str, Any]:
        from micnaga.brain import horas

        batch = args.get("lote")
        if not isinstance(batch, str):
            return {"ok": False, "error": "secretaria.err.invalid"}
        return horas.undo(self._connected(), batch)

    def _api_horas_off(self, args: dict[str, Any]) -> dict[str, Any]:
        from micnaga.brain import horas

        day = args.get("fecha")
        if not isinstance(day, str):
            return {"ok": False, "error": "secretaria.err.invalid"}
        try:
            return {"ok": True, "libres": horas.set_off(day, args.get("libre") is not False)}
        except ValueError:
            return {"ok": False, "error": "secretaria.err.invalid"}

    def _ids(self, args: dict[str, Any]) -> list[str]:
        ids = args.get("ids")
        return [str(i) for i in ids if isinstance(i, str)][:200] if isinstance(ids, list) else []

    def _api_apply(self, args: dict[str, Any]) -> dict[str, Any]:
        """``items``: ``[{id, huella, confirmar?}]``. La aprobacion vale para la version que vio el
        usuario en ESTA reunion; si alguna cambio (otra generacion del acta, una revision...), no
        se aplica nada. ``confirmar``: repetir un «resultado incierto» ya comprobado a mano."""
        from micnaga.brain.executor import StaleError

        path = self._folder_path(args)
        if path is None:
            return {"ok": False, "error": "library.err.not_found"}
        items = args.get("items")
        if not isinstance(items, list) or not items:
            return {"ok": False, "error": "secretaria.err.stale"}
        expect: dict[str, str] = {}
        confirm: list[str] = []
        for it in items[:200]:
            if not isinstance(it, dict) or not isinstance(it.get("id"), str):
                continue
            expect[it["id"]] = str(it.get("huella") or "")
            if it.get("confirmar") is True:
                confirm.append(it["id"])
        if not expect:
            return {"ok": False, "error": "secretaria.err.stale"}
        # «Acta a Drive» sube el acta COMPLETA: se escribe antes (fuera del cerrojo del acta, tarda
        # minutos) y, si no se puede, no se aplica nada en vez de subir la breve
        if self._wants_drive(path, list(expect)) and not self._ensure_full_acta(path):
            return {"ok": False, "error": "secretaria.err.full_acta"}
        try:
            with self._acta_lock:
                acta = self._executor(path).apply(list(expect), expect=expect, confirm=confirm)
        except StaleError as exc:
            return {"ok": False, "error": "secretaria.err.stale", "stale": exc.ids}
        db = self._db()
        try:
            bstore.set_proposals(db, path.name, acta_io.pending_count(acta))
        finally:
            db.close()
        return {"ok": True, "acta": acta}

    def _api_edit(self, args: dict[str, Any]) -> dict[str, Any]:
        """Cambios en una propuesta pendiente (horas, proyecto, descripcion, invitados...)."""
        from micnaga.brain.executor import ApplyError
        from micnaga.brain.timesheets import LineError

        path = self._folder_path(args)
        pid = args.get("id")
        changes = args.get("cambios")
        if path is None or not isinstance(pid, str) or not isinstance(changes, dict):
            return {"ok": False, "error": "secretaria.err.invalid"}
        try:
            with self._acta_lock:
                acta = self._executor(path).edit(pid, changes)
        except LineError as exc:
            return {"ok": False, "error": f"secretaria.horas.err.{exc}"}
        except ApplyError as exc:
            key = str(exc)
            if key.startswith("horas_"):
                return {"ok": False, "error": f"secretaria.horas.err.{key[6:]}"}
            return {"ok": False, "error": f"secretaria.err.{key}"}
        return {"ok": True, "acta": acta}

    def _api_undo(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._folder_path(args)
        if path is None:
            return {"ok": False, "error": "library.err.not_found"}
        with self._acta_lock:
            resolve = args.get("resolver")  # «forzar» / «omitir» un conflicto (un item)
            acta = self._executor(path).undo(
                self._ids(args), resolve=resolve if isinstance(resolve, str) else None
            )
        return {"ok": True, "acta": acta}

    def _api_dismiss(self, args: dict[str, Any]) -> dict[str, Any]:
        """El usuario cerro la revision sin aplicar lo desmarcado: queda «descartada»."""
        path = self._folder_path(args)
        if path is None:
            return {"ok": False, "error": "library.err.not_found"}
        with self._acta_lock:
            acta = acta_io.read_acta(path) or {}
            wanted = set(self._ids(args))
            for p in acta.get("propuestas") or []:
                if p.get("id") in wanted and p.get("estado") == "pendiente":
                    p["estado"] = "descartada"
            acta_io.write_acta(path, acta)
        return {"ok": True, "acta": acta}

    # ---- «corregir con un mensaje»

    def _refiner(self, path: Path) -> Any:
        from micnaga.brain.refine import Refiner

        return Refiner(
            self.config,
            self._executor(path),
            runner=self._runner(),
            today=self.now().date(),
            lock=self._acta_lock,
        )

    def _refine(self, args: dict[str, Any], work: Callable[[Any, Any], Any]) -> dict[str, Any]:
        from micnaga.brain.refine import RefineError

        path = self._folder_path(args)
        if path is None or not isinstance(args.get("texto"), str):
            return {"ok": False, "error": "secretaria.err.invalid"}
        key = path.name
        target = args.get("id") if isinstance(args.get("id"), str) else None

        def on_status(s: dict[str, Any]) -> None:
            with self._lock:
                self._refining[key] = {**s, "id": target}

        on_status({"fase": "pensando"})
        try:
            out = work(self._refiner(path), on_status)
        except RefineError as exc:
            kind = exc.kind
            error = (
                f"secretaria.horas.err.{kind[6:]}"
                if kind.startswith("horas_")
                else f"secretaria.refine.err.{kind}"
            )
            return {"ok": False, "error": error, "nota": exc.note}
        finally:
            with self._lock:
                self._refining.pop(key, None)
        db = self._db()
        try:
            bstore.set_proposals(db, key, acta_io.pending_count(out.acta))
        finally:
            db.close()
        return {
            "ok": True,
            "acta": out.acta,
            "cambiados": out.changed,
            "anadidos": out.added,
            "quitados": out.removed,
            "nota": out.note,
            "s": round(out.duration_s, 1),
        }

    def _api_refine(self, args: dict[str, Any]) -> dict[str, Any]:
        """Una propuesta revisada por la IA a partir de un mensaje del usuario."""
        pid = args.get("id")
        if not isinstance(pid, str):
            return {"ok": False, "error": "secretaria.err.invalid"}
        text = str(args.get("texto") or "")
        return self._refine(args, lambda r, st: r.item(pid, text, on_status=st))

    def _api_refine_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        """Un mensaje sobre la lista entera (añadir, quitar, cambiar varias)."""
        text = str(args.get("texto") or "")
        return self._refine(args, lambda r, st: r.plan(text, on_status=st))

    def _api_refine_undo(self, args: dict[str, Any]) -> dict[str, Any]:
        """«Deshacer»: la version anterior de un item (``id``) o la lista de antes (sin ``id``)."""
        from micnaga.brain.executor import ApplyError
        from micnaga.brain.refine import RefineError

        path = self._folder_path(args)
        if path is None:
            return {"ok": False, "error": "library.err.not_found"}
        pid = args.get("id")
        try:
            with self._acta_lock:
                if isinstance(pid, str):
                    acta = self._executor(path).revert(pid)
                else:
                    acta = self._refiner(path).undo_plan()
        except (ApplyError, RefineError) as exc:
            return {"ok": False, "error": f"secretaria.err.{getattr(exc, 'kind', exc)}"}
        db = self._db()
        try:
            bstore.set_proposals(db, path.name, acta_io.pending_count(acta))
        finally:
            db.close()
        return {"ok": True, "acta": acta}

    def _api_refine_status(self, args: dict[str, Any]) -> dict[str, Any]:
        folder = args.get("folder")
        with self._lock:
            st = self._refining.get(folder) if isinstance(folder, str) else None
            return {"ok": True, "status": dict(st) if st else None}

    def _decorate_links(self, acta: dict[str, Any]) -> None:
        """«Ver en Odoo» de lo ya aplicado: ``{odoo}/web#id=…&model=…`` (lo abre ``open_link``)."""
        base = self.config.secretaria.odoo_url.rstrip("/")
        for p in acta.get("propuestas") or []:
            p.pop("enlace", None)
            if not base.startswith("http") or p.get("estado") != "aplicada":
                continue
            if p.get("grupo") in ("google", "correcciones"):
                continue
            u = p.get("deshacer") or {}
            model, rid = u.get("modelo"), u.get("creado") or u.get("id")
            if model == "mail.message" and isinstance(u.get("registro"), list):
                model, rid = u["registro"][0], u["registro"][1]
            if isinstance(model, str) and isinstance(rid, int) and not isinstance(rid, bool):
                p["enlace"] = f"{base}/web#id={rid}&model={model}&view_type=form"

    # ---- «Acta completa» (bajo demanda, en streaming)

    def _with_db(self, fn: Callable[[Any], Any]) -> Any:
        db = self._db()
        try:
            return fn(db)
        finally:
            db.close()

    def _api_full_acta(self, args: dict[str, Any]) -> dict[str, Any]:
        """Lo guardado (al instante) y, si se esta escribiendo, lo que lleva."""
        from micnaga.brain import full_acta

        path = self._folder_path(args)
        if path is None:
            return {"ok": False, "error": "library.err.not_found"}
        with self._lock:
            live = dict(self._full.get(path.name) or {}) or None
        return {"ok": True, "doc": full_acta.read(path), "live": live}

    def _api_full_acta_make(self, args: dict[str, Any]) -> dict[str, Any]:
        """Empieza a escribirla en un hilo; la vista consulta ``full_acta`` mientras tanto."""
        path = self._folder_path(args)
        if path is None:
            return {"ok": False, "error": "library.err.not_found"}
        if not self.enabled:
            return {"ok": False, "error": "secretaria.err.disabled"}
        key = path.name
        with self._lock:
            if (self._full.get(key) or {}).get("fase") == "escribiendo":
                return {"ok": True}
            self._full[key] = {"fase": "escribiendo", "texto": "", "inicio": time.time()}
        threading.Thread(
            target=self._write_full, args=(path,), name="micnaga-acta-completa", daemon=True
        ).start()
        return {"ok": True}

    def _write_full(self, path: Path) -> None:
        from micnaga.brain.full_acta import FullActa

        key = path.name

        def on_text(chunk: str) -> None:
            with self._lock:
                st = self._full.get(key)
                if st is not None:
                    st["texto"] = (st.get("texto") or "") + chunk

        try:
            FullActa(self.config, self._runner(), with_db=self._with_db).make(path, on_text=on_text)
            state: dict[str, Any] = {"fase": "lista"}
        except Exception as exc:
            log.warning("full acta failed: %s", getattr(exc, "kind", type(exc).__name__))
            state = {"fase": "error", "error": str(getattr(exc, "kind", "failed"))}
        with self._lock:
            self._full[key] = state

    def _full_acta_md(self, path: Path) -> str | None:
        """El Markdown del «Acta completa» guardada (``None`` si no la hay)."""
        from micnaga.brain import full_acta

        doc = full_acta.read(path)
        return doc["md"] if doc else None

    def _ensure_full_acta(self, path: Path) -> bool:
        """Que exista el «Acta completa» (la que se sube a Drive): si no la hay, se escribe ahora,
        con el mismo estado «escribiendo» que la vista (un solo escritor; si ya se esta
        escribiendo, se espera). ``True`` si al final la hay."""
        from micnaga.brain import full_acta

        if full_acta.read(path) is not None:
            return True
        key = path.name
        with self._lock:
            busy = (self._full.get(key) or {}).get("fase") == "escribiendo"
            if not busy:
                self._full[key] = {"fase": "escribiendo", "texto": "", "inicio": time.time()}
        if busy:
            deadline = time.monotonic() + full_acta.TIMEOUT_S + 60
            while time.monotonic() < deadline:
                with self._lock:
                    if (self._full.get(key) or {}).get("fase") != "escribiendo":
                        break
                time.sleep(0.5)
        else:
            self._write_full(path)
        return full_acta.read(path) is not None

    def _wants_drive(self, path: Path, ids: list[str]) -> bool:
        acta = acta_io.read_acta(path) or {}
        return any(
            p.get("tipo") == "acta_drive" and p.get("id") in ids
            for p in acta.get("propuestas") or []
        )

    def _api_full_acta_open(self, args: dict[str, Any]) -> dict[str, Any]:
        from micnaga.brain import full_acta

        path = self._folder_path(args)
        target = path / full_acta.FILE if path is not None else None
        if target is None or not target.is_file():
            return {"ok": False, "error": "library.err.not_found"}
        self._open_file(target)
        return {"ok": True}

    def _api_analyze(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._folder_path(args)
        if path is None:
            return {"ok": False, "error": "library.err.not_found"}
        if not self.enabled:
            return {"ok": False, "error": "secretaria.err.disabled"}
        self.analyze(path.name, force=True)
        return {"ok": True}

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        db = self._db()
        try:
            hits = bstore.search_actas(db, query, limit)
        finally:
            db.close()
        return [{"folder": h.folder, "title": h.title, "snippet": h.snippet} for h in hits]

    # ---- ajustes

    def _api_status(self, args: dict[str, Any]) -> dict[str, Any]:
        cfg = self.config
        from micnaga.brain.claude import find_claude

        installed = find_claude() is not None
        return {
            "ok": True,
            "enabled": cfg.secretaria.enabled,
            "auth": cfg.secretaria.auth,
            "claude_installed": installed,
            "has_token": secrets.has(secrets.CLAUDE_TOKEN),
            "has_api_key": secrets.has(secrets.ANTHROPIC_KEY),
            "has_odoo_key": secrets.has(secrets.ODOO_KEY),
        }

    def _api_claude_detect(self, args: dict[str, Any]) -> dict[str, Any]:
        runner = self._runner()
        info = runner.auth_status()
        return {
            "ok": True,
            "logged_in": bool(info.get("loggedIn")),
            "method": info.get("authMethod"),
            "plan": info.get("subscriptionType"),
        }

    def _api_claude_test(self, args: dict[str, Any]) -> dict[str, Any]:
        """«Probar»: un titulo de ejemplo con Haiku (2-4 s)."""
        from micnaga.brain.claude import ClaudeRequest
        from micnaga.brain.prompt import build_title_input, system_prompt
        from micnaga.brain.schema import TITLE_SCHEMA, clean_title

        runner = self._runner()
        t0 = time.monotonic()
        res = runner.run(
            ClaudeRequest(
                prompt=build_title_input(
                    "Yo: Hola Carlos, ¿revisamos el presupuesto del CRM para la clínica?\n"
                    "S1: Sí, y la demo con los datos de Triana."
                ),
                system_prompt=system_prompt("titulo"),
                model=self.config.secretaria.model_title,
                schema=TITLE_SCHEMA,
                max_turns=3,
                timeout_s=60,
            )
        )
        title = clean_title((res.structured or {}).get("titulo"))
        return {"ok": bool(title), "title": title, "ms": round((time.monotonic() - t0) * 1000)}

    def _api_set_secret(self, args: dict[str, Any]) -> dict[str, Any]:
        targets = {
            "token": secrets.CLAUDE_TOKEN,
            "api_key": secrets.ANTHROPIC_KEY,
            "odoo": secrets.ODOO_KEY,
        }
        kind = args.get("kind")
        value = args.get("value")
        if kind not in targets or not (value is None or isinstance(value, str)):
            return {"ok": False, "error": "secretaria.err.invalid"}
        if isinstance(value, str) and len(value) > 4096:
            return {"ok": False, "error": "secretaria.err.invalid"}
        secrets.put(targets[str(kind)], value)
        return {"ok": True, "stored": secrets.has(targets[str(kind)])}

    def _api_odoo_test(self, args: dict[str, Any]) -> dict[str, Any]:
        from micnaga.brain.odoo import OdooClient, OdooConfig, OdooError

        url = str(args.get("url") or self.config.secretaria.odoo_url)
        db = str(args.get("db") or self.config.secretaria.odoo_db)
        login = str(args.get("login") or self.config.secretaria.odoo_login)
        key = args.get("key") if isinstance(args.get("key"), str) and args.get("key") else None
        key = key or secrets.get(secrets.ODOO_KEY)
        if not (url and db and login and key):
            return {"ok": False, "error": "secretaria.err.odoo_incomplete"}
        from micnaga.brain import capabilities

        try:
            client = OdooClient(OdooConfig(url, db, login, str(key)), timeout_s=15, read_only=True)
            info = client.probe()
            profile = capabilities.ensure(client, refresh=True)
        except OdooError as exc:
            return {"ok": False, "error": f"secretaria.err.odoo_{exc.kind}"}
        from micnaga.brain import odoo_learn

        if odoo_learn.load_state(str(profile["key"])) is None:  # conexion nueva: conocerla
            self._start_learn(lambda: client, background=False)
        return {
            "ok": True,
            **info,
            "capacidades": capabilities.summary(profile),
            "capacidades_texto": capabilities.summary_text(profile),
        }

    def _api_caps(self, args: dict[str, Any]) -> dict[str, Any]:
        """Lo ultimo que se sabe de este Odoo (sin red): para Ajustes y la biblioteca."""
        from micnaga.brain import capabilities

        s = self.config.secretaria
        if not (s.odoo_url and s.odoo_db and s.odoo_login):
            return {"ok": True, "capacidades": [], "horas": False}
        prof = capabilities.load(capabilities.connection_key(s.odoo_url, s.odoo_db, s.odoo_login))
        feats = capabilities.features(prof)
        return {
            "ok": True,
            "capacidades": capabilities.summary(prof),
            "capacidades_texto": capabilities.summary_text(prof),
            "comprobado": (prof or {}).get("checked_at"),
            "horas": feats.get("horas") == "escritura",
        }

    def _api_learned(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "items": [i.to_dict() for i in learn.list_learned()],
            "versions": learn.history_versions()[:20],
        }

    def _api_forget(self, args: dict[str, Any]) -> dict[str, Any]:
        idx = args.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool):
            return {"ok": False, "error": "secretaria.err.invalid"}
        return {"ok": learn.forget(idx), **self._api_learned({})}

    def _api_restore(self, args: dict[str, Any]) -> dict[str, Any]:
        version = args.get("version")
        if not isinstance(version, str):
            return {"ok": False, "error": "secretaria.err.invalid"}
        return {"ok": learn.restore(version), **self._api_learned({})}


def _spawn(fn: Callable[[], None]) -> threading.Thread:
    t = threading.Thread(target=fn, name="micnaga-odoo-learn", daemon=True)
    t.start()
    return t


def _startfile(path: Path) -> None:  # pragma: no cover - abre el editor de Markdown real
    import os

    os.startfile(path)  # type: ignore[attr-defined,unused-ignore]
