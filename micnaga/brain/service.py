"""La Secretaria dentro del proceso de la app: cola serie, reintentos y API para la UI.

* **Disparo:** ``on_job_done`` (evento ``job_done`` del worker). Con la Secretaria apagada el
  aviso «Lista» sale al momento, como siempre. Encendida, el aviso espera a la pasada hasta
  ``TOAST_WAIT_S`` y sale con «· N propuestas»; si tarda mas, sale sin el numero y las propuestas
  aparecen despues en la biblioteca.
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
from micnaga.config import Config

log = logging.getLogger(__name__)

TOAST_WAIT_S = 120.0
TICK_S = 60.0
BOOT_WINDOW_H = 24
ODOO_RETRY_S = 3600.0

Notify = Callable[[str, str, float, int, int], None]
"""``notify(carpeta, titulo, duracion_s, hablantes, propuestas)``."""
Nudge = Callable[[int], None]
"""``nudge(dias_con_huecos)``: aviso «Te faltan horas esta semana»."""


class BrainService:
    def __init__(
        self,
        config: Callable[[], Config],
        *,
        db_factory: Callable[[], Any] | None = None,
        secretary_factory: Callable[[Config], Any] | None = None,
        runner_factory: Callable[[Config], Any] | None = None,
        odoo_factory: Callable[..., Any] | None = None,
        toast_wait_s: float = TOAST_WAIT_S,
        tick_s: float = TICK_S,
    ) -> None:
        self._config = config
        self._db_factory = db_factory
        self._secretary_factory = secretary_factory
        self._runner_factory = runner_factory
        self._odoo_factory = odoo_factory
        self.toast_wait_s = toast_wait_s
        self.tick_s = tick_s
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status: dict[str, dict[str, Any]] = {}
        self._waiters: dict[str, list[Callable[[str, int], None]]] = {}
        self._renames: dict[str, str] = {}
        self._queued: set[str] = set()
        self._started_at = time.time()
        self._nudge: Nudge | None = None
        self.now: Callable[[], datetime] = datetime.now
        self._odoo_failed_at = 0.0

    def set_nudge(self, nudge: Nudge | None) -> None:
        """La UI pone aqui como ensenar el aviso semanal de horas (toast)."""
        self._nudge = nudge

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
        """Encola la pasada; el aviso «Lista» sale al terminar (o a los ``toast_wait_s``)."""
        if not self.enabled:
            notify(folder, title, duration_s, speakers, 0)
            return
        done = threading.Event()
        state = {"sent": False}
        lock = threading.Lock()

        def finish(final_folder: str, proposals: int) -> None:
            with lock:
                if state["sent"]:
                    return
                state["sent"] = True
            final_title = title
            tr = paths.meetings_root(self.config) / final_folder / paths.TRANSCRIPT_JSON
            try:
                from micnaga.types import Transcript

                final_title = Transcript.from_json(tr.read_text(encoding="utf-8")).meeting.title
            except (OSError, ValueError, KeyError):
                pass
            notify(final_folder, final_title, duration_s, speakers, proposals)
            done.set()

        with self._lock:
            self._waiters.setdefault(folder, []).append(finish)

        def fallback() -> None:
            if not done.wait(self.toast_wait_s):
                with self._lock:
                    current = self._renames.get(folder, folder)
                finish(current, 0)

        threading.Thread(target=fallback, name="micnaga-toast-wait", daemon=True).start()
        try:
            db = self._db()
            try:
                bstore.enqueue(db, folder)
            finally:
                db.close()
        except Exception:
            log.exception("could not enqueue the secretaria run")
            finish(folder, 0)
            return
        self._submit(folder)

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
                self._release(folder, folder, 0)
                return
            if not self.enabled or not bstore.claim(db, folder):
                run = bstore.get_run(db, folder)
                self._release(folder, folder, run.proposals if run else 0)
                return
        finally:
            db.close()

        def on_status(s: dict[str, Any]) -> None:
            with self._lock:
                self._status[folder] = s

        final = folder
        proposals = 0
        try:
            result = self._secretary().analyze(path, on_status=on_status)
            final = result.folder.name
            proposals = result.proposals
            db = self._db()
            try:
                bstore.finish(db, final, proposals=proposals)
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
        self._release(folder, final, proposals)

    def _release(self, folder: str, final: str, proposals: int) -> None:
        with self._lock:
            waiters = self._waiters.pop(folder, [])
            if final != folder:
                self._renames[folder] = final
        for w in waiters:
            try:
                w(final, proposals)
            except Exception:
                log.exception("toast notify failed")

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
            if isinstance(out.get("acta"), dict):  # items con su esquema editable
                from micnaga.brain.plan import decorate

                decorate(out["acta"])
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

        return Executor(path, odoo=self._odoo_client, today=self.now().date())

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
        out = horas.apply(self._connected(), lines, today=self.now().date())
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
        path = self._folder_path(args)
        if path is None:
            return {"ok": False, "error": "library.err.not_found"}
        acta = self._executor(path).apply(self._ids(args))
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
        acta = self._executor(path).undo(self._ids(args))
        return {"ok": True, "acta": acta}

    def _api_dismiss(self, args: dict[str, Any]) -> dict[str, Any]:
        """El usuario cerro la revision sin aplicar lo desmarcado: queda «descartada»."""
        path = self._folder_path(args)
        if path is None:
            return {"ok": False, "error": "library.err.not_found"}
        acta = acta_io.read_acta(path) or {}
        wanted = set(self._ids(args))
        for p in acta.get("propuestas") or []:
            if p.get("id") in wanted and p.get("estado") == "pendiente":
                p["estado"] = "descartada"
        acta_io.write_acta(path, acta)
        return {"ok": True, "acta": acta}

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
