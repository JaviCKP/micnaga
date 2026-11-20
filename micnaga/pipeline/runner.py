"""Ejecutor del pipeline: las 11 etapas en orden, con reanudacion, descarte y fallo (spec 8).

``PipelineRunner.run(session, job)`` procesa una carpeta de sesion:

* Cada etapa deja su resultado en ``work/`` y una marca ``work/NN_nombre.done.json`` (la ultima
  escritura). Una etapa con marca y todas sus salidas se **salta**: tras un cierre inesperado el
  trabajo se reanuda en la primera etapa sin marca. Al re-ejecutar una etapa se borran las marcas
  de las posteriores.
* ``jobs.stage`` se actualiza con ``Database.set_stage`` (indice 1-based de la etapa en curso) y se
  emiten eventos ``JobStarted`` / ``JobProgress`` / ``JobDone`` / ``JobFailed`` / ``JobDiscarded``.
* **Descarte** (la etapa 1 lanza ``SessionDiscarded``): ``Database.discard_job`` (que ya anota
  solo fecha + motivo y borra la ruta de ``jobs``), despues la carpeta se aparta en
  «Descartadas» (``micnaga.holding``: recuperable ``storage.discarded_keep_days`` dias). Sin
  trabajo (modo en primer plano) se usa ``log_discard``. Ni el motivo ni el log llevan titulo o
  ruta.
* **Fallo**: ``fail_job`` con el error; los ficheros se conservan para reintentar. Las
  excepciones que no son ``Exception`` (``KeyboardInterrupt``, cierre brusco) no se capturan: el
  trabajo queda ``processing`` y ``requeue_stale`` lo recupera al arrancar el worker.
* **Huellas en ``work/``**: los embeddings de hablante (etapa 7) solo se guardan cifrados con
  DPAPI. Al arrancar se borran el ``07_embeddings.json`` en claro de versiones antiguas (la etapa
  7 se repite) y los temporales ``.tmp-*.json`` huerfanos de un cierre brusco.
* **Cierre** (tras la etapa 11, lo ultimo que se toca del sistema de ficheros): renombra la
  carpeta a ``YYYY-MM-DD_HHMM_slug`` (``jobs.session_dir`` se actualiza *antes* del renombrado; si
  el proceso muere entremedias, ``_locate`` encuentra la carpeta antigua gracias a
  ``work/finalize.json``), asegura los FLAC, indexa en SQLite, marca el trabajo ``done`` y por
  ultimo borra ``work/`` (``keep_work=False``).

Memoria: cada etapa libera su modelo antes de la siguiente (``stages.base.engine_scope``).
"""

from __future__ import annotations

import contextlib
import gc
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from micnaga import paths
from micnaga.events import Event, JobDiscarded, JobDone, JobFailed, JobProgress, JobStarted
from micnaga.pipeline import output
from micnaga.pipeline.stages import get_stages
from micnaga.pipeline.stages.base import (
    Deps,
    SessionDiscarded,
    Stage,
    StageContext,
    StageError,
    atomic_write_json,
    clear_marker,
    default_deps,
    is_stage_done,
    write_marker,
)
from micnaga.types import CaptureMeta, Transcript

if TYPE_CHECKING:
    from micnaga.config import Config
    from micnaga.store.db import Database, Job

log = logging.getLogger(__name__)

ENV_FAKE_ENGINES = "MICNAGA_FAKE_ENGINES"
"""Solo para pruebas: ``1`` hace que el runner use los motores falsos de ``micnaga.bench.fakes``."""

FINALIZE_FILE = "finalize.json"
STAGE_WEIGHTS = (1, 1, 3, 1, 2, 12, 4, 1, 1, 1, 1)

Status = Literal["done", "discarded", "failed"]
DepsSource = Deps | Callable[[Path], Deps] | None


@dataclass
class RunResult:
    status: Status
    session_dir: Path
    """Carpeta final (``done``), la de entrada (``failed``) o la ya borrada (``discarded``)."""
    reason: str | None = None
    """Motivo del descarte o texto del error."""
    transcript: Transcript | None = None
    stage_times: dict[str, float] = field(default_factory=dict)
    """Segundos por etapa (0 si se salto por estar ya hecha)."""
    skipped: list[str] = field(default_factory=list)


def deps_for_session(session: Path) -> Deps:
    """Dependencias por defecto; con ``MICNAGA_FAKE_ENGINES=1`` (solo tests) motores falsos."""
    if os.environ.get(ENV_FAKE_ENGINES, "").strip() not in ("", "0"):
        from micnaga.bench.fakes import fake_deps_for_session

        return fake_deps_for_session(session)
    return default_deps()


class PipelineRunner:
    def __init__(
        self,
        config: Config,
        *,
        deps: DepsSource = None,
        db: Database | None = None,
        emit: Callable[[Event], None] | None = None,
        keep_work: bool = False,
        rename: bool = True,
        on_stage_start: Callable[[Stage], None] | None = None,
    ) -> None:
        self.config = config
        self._deps_source = deps
        self.db = db
        self._emit_cb = emit
        self.keep_work = keep_work
        self.rename = rename
        self._on_stage_start = on_stage_start
        self._last_fraction = -1.0

    # ------------------------------------------------------------------ utilidades

    def _deps(self, session: Path) -> Deps:
        if isinstance(self._deps_source, Deps):
            return self._deps_source
        if self._deps_source is not None:
            return self._deps_source(session)
        return deps_for_session(session)

    def _emit(self, event: Event) -> None:
        if self._emit_cb is None:
            return
        try:
            self._emit_cb(event)
        except Exception:
            log.debug("event sink failed", exc_info=True)

    @staticmethod
    def _locate(session: Path) -> Path | None:
        """La carpeta de la sesion, o la antigua si el renombrado final quedo a medias."""
        if session.is_dir():
            return session
        parent = session.parent
        if parent.is_dir():
            for cand in parent.iterdir():
                marker = paths.work_dir(cand) / FINALIZE_FILE
                if marker.is_file():
                    try:
                        data = json.loads(marker.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if data.get("final_name") == session.name:
                        return cand
        return None

    # ------------------------------------------------------------------ ejecucion

    def run(self, session: Path, job: Job | None = None) -> RunResult:
        located = self._locate(session)
        if located is None:
            return self._fail(job, session, StageError("session folder is missing"))
        session = located
        job_id = job.id if job is not None else None
        self._last_fraction = -1.0
        if job is not None:
            self._emit(JobStarted(job.id, str(session)))
        times: dict[str, float] = {}
        skipped: list[str] = []
        try:
            work = paths.work_dir(session)
            if not work.is_dir() and (session / paths.TRANSCRIPT_JSON).is_file():
                # procesada del todo y ``work/`` ya limpiado: solo falta cerrar
                return self._finalize(session, job, times, skipped)
            meta = self._load_meta(session)
            work.mkdir(parents=True, exist_ok=True)
            deps = self._deps(session)
            stages = get_stages()
            # embeddings en claro de versiones antiguas: se borran antes de nada (la marca de la
            # etapa 7 los lista como salida, asi que la etapa 7 se repite y los escribe cifrados)
            from micnaga.pipeline.stages.s07_diarize import remove_legacy_embeddings

            remove_legacy_embeddings(work)
            # temporales JSON huerfanos de un cierre brusco (podian llevar esos embeddings)
            for orphan in work.glob(".tmp-*.json"):
                with contextlib.suppress(OSError):
                    orphan.unlink()
            total = float(sum(STAGE_WEIGHTS))
            for stage in stages:
                before = sum(STAGE_WEIGHTS[: stage.index - 1]) / total
                span = STAGE_WEIGHTS[stage.index - 1] / total
                self._set_stage(job, stage, before)
                if stage.index == 2 and not is_stage_done(session, stages[5]):
                    self._finish_live(session, deps, meta, stages, job_id)
                if is_stage_done(session, stage):
                    times[stage.name] = 0.0
                    skipped.append(stage.name)
                    continue
                for later in stages[stage.index :]:
                    clear_marker(session, later)
                if self._on_stage_start is not None:
                    self._on_stage_start(stage)

                def progress(
                    frac: float, _b: float = before, _s: float = span, _st: Stage = stage
                ) -> None:
                    self._progress(job_id, _st, _b + _s * frac)

                ctx = StageContext(session, self.config, deps, meta, progress)
                t0 = time.perf_counter()
                result = stage.run(ctx)
                elapsed = time.perf_counter() - t0
                write_marker(session, stage, result, elapsed)
                times[stage.name] = elapsed
                del ctx
                gc.collect()
            return self._finalize(session, job, times, skipped)
        except SessionDiscarded as discard:
            return self._discard(job, session, discard.reason, times)
        except Exception as exc:
            return self._fail(job, session, exc, times)

    def _finish_live(
        self,
        session: Path,
        deps: Deps,
        meta: CaptureMeta,
        stages: tuple[Stage, ...],
        job_id: int | None,
    ) -> None:
        """Si hubo procesado en vivo, completa la cola y deja hechas las etapas 2-6.

        Cualquier fallo borra ``work/live/`` y el pipeline sigue por lotes (nunca se pierde una
        reunion por el procesado en vivo)."""
        from micnaga.pipeline import live

        if not live.has_cache(session):
            return
        engines = live.engines_from_deps(deps)
        if engines is None or not self.config.processing.live:
            live.discard_cache(session)
            return
        total = float(sum(STAGE_WEIGHTS))
        before = sum(STAGE_WEIGHTS[:1]) / total
        span = sum(STAGE_WEIGHTS[1:6]) / total

        def progress(frac: float) -> None:
            self._progress(job_id, stages[1], before + span * frac)

        ctx = StageContext(session, self.config, deps, meta, progress)
        t0 = time.perf_counter()
        try:
            done = live.finish(ctx, stages[1:6], engines, on_progress=progress)
        except Exception:
            log.exception("live processing could not be completed; processing in batch")
            done = False
        if not done:
            for later in stages[1:]:
                clear_marker(session, later)
            live.discard_cache(session)
            return
        log.info("live processing completed in %.1f s", time.perf_counter() - t0)

    @staticmethod
    def _load_meta(session: Path) -> CaptureMeta:
        file = session / paths.CAPTURE_JSON
        if not file.is_file():
            raise StageError("capture.json is missing")
        return CaptureMeta.from_json(file.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ progreso

    def _set_stage(self, job: Job | None, stage: Stage, fraction: float) -> None:
        if job is not None and self.db is not None:
            self.db.set_stage(job.id, stage.index)
        self._progress(job.id if job is not None else None, stage, fraction, force=True)

    def _progress(
        self, job_id: int | None, stage: Stage, fraction: float, *, force: bool = False
    ) -> None:
        if job_id is None:
            return
        if not force and abs(fraction - self._last_fraction) < 0.01:
            return
        self._last_fraction = fraction
        self._emit(JobProgress(job_id, stage.index, stage.name, round(min(1.0, fraction), 4)))

    # ------------------------------------------------------------------ descarte / fallo

    def _discard(
        self, job: Job | None, session: Path, reason: str, times: dict[str, float]
    ) -> RunResult:
        log.info("session discarded: %s", reason)  # solo el motivo: nada de titulo ni ruta
        label = f"discarded:{job.id}" if job is not None else "discarded"
        if self.db is not None:
            try:
                if job is not None:
                    self.db.discard_job(job.id, reason)  # tambien anota fecha + motivo
                else:
                    self.db.log_discard(reason)
            except Exception:
                log.exception("could not record the discard")
        hold_session(session, reason)
        if job is not None:
            self._emit(JobDiscarded(job.id, label, reason))
        return RunResult("discarded", session, reason, stage_times=times)

    def _fail(
        self, job: Job | None, session: Path, exc: Exception, times: dict[str, float] | None = None
    ) -> RunResult:
        message = f"{type(exc).__name__}: {exc}"
        log.error("processing failed: %s", message, exc_info=not isinstance(exc, StageError))
        if job is not None:
            if self.db is not None:
                try:
                    self.db.fail_job(job.id, message)
                except Exception:
                    log.exception("could not mark job %d as failed", job.id)
            self._emit(JobFailed(job.id, str(session), message))
        return RunResult("failed", session, message, stage_times=times or {})

    # ------------------------------------------------------------------ cierre

    def _finalize(
        self, session: Path, job: Job | None, times: dict[str, float], skipped: list[str]
    ) -> RunResult:
        transcript = output.read_transcript(session)
        final = self._plan_final(session, transcript)
        if final != session:
            if job is not None and self.db is not None:
                self._set_job_dir(job.id, final)
            self._rename(session, final)
            session = final
        ensure_flac(session)
        if job is not None:
            self._mark_analysis_pending(session)
        if self.db is not None:
            output.index_meeting(self.db, session, transcript, job.id if job is not None else None)
            if job is not None:
                self.db.finish_job(job.id)
        if not self.keep_work:
            shutil.rmtree(paths.work_dir(session), ignore_errors=True)
        if job is not None:
            self._emit(
                JobDone(
                    job.id,
                    str(session),
                    transcript.meeting.title,
                    transcript.meeting.duration_s,
                    len(transcript.speakers),
                )
            )
        return RunResult("done", session, transcript=transcript, stage_times=times, skipped=skipped)

    def _mark_analysis_pending(self, session: Path) -> None:
        """Acta pendiente, en disco antes de dar el trabajo por hecho (el aviso puede perderse)."""
        try:
            if not self.config.secretaria.enabled:
                return
            (session / paths.ANALYSIS_PENDING).write_text("", encoding="utf-8")
        except Exception:
            log.warning("could not mark the meeting as pending analysis", exc_info=True)

    def _plan_final(self, session: Path, transcript: Transcript) -> Path:
        if not self.rename:
            return session
        plan = paths.work_dir(session) / FINALIZE_FILE
        if plan.is_file():
            try:
                name = str(json.loads(plan.read_text(encoding="utf-8"))["final_name"])
                return session.parent / name
            except (OSError, ValueError, KeyError):
                pass
        try:
            start = datetime.fromisoformat(transcript.meeting.start)
        except ValueError:
            return session
        name = paths.meeting_folder_name(start, transcript.meeting.title)
        if session.name == name or re.fullmatch(re.escape(name) + r"-\d+", session.name):
            return session
        final = paths.unique_folder(session.parent, name)
        if paths.work_dir(session).is_dir():
            atomic_write_json(plan, {"final_name": final.name})
        return final

    def _set_job_dir(self, job_id: int, path: Path) -> None:
        assert self.db is not None
        self.db.set_session_dir(job_id, path)

    def _rename(self, src: Path, dst: Path) -> None:
        last: OSError | None = None
        for _ in range(5):
            try:
                os.replace(src, dst)
                return
            except OSError as exc:  # antivirus / indexador con la carpeta abierta
                last = exc
                time.sleep(0.2)
        assert last is not None
        raise last


# ----------------------------------------------------------------------------- ficheros


def hold_session(session: Path, reason: str) -> None:
    """Aparta una sesion que no se transcribe en «Descartadas» (recuperable N dias; nunca se
    borra sin un «No» del usuario, DECISIONS §18). Si no se puede mover, se deja donde esta."""
    from micnaga.holding import REASON_GATES, REASON_UNCONFIRMED, move_to_held
    from micnaga.pipeline.stages.s01_gates import UNCONFIRMED

    key = REASON_UNCONFIRMED if reason == UNCONFIRMED else REASON_GATES
    try:
        move_to_held(session, key, detail=reason)
    except OSError:
        log.exception("could not move a discarded session to the held area")


def delete_session(session: Path) -> None:
    """Borra la carpeta de una sesion descartada (reintenta si Windows la tiene bloqueada)."""
    for _ in range(5):
        try:
            shutil.rmtree(session)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.2)
    shutil.rmtree(session, ignore_errors=True)
    if session.exists():
        log.warning("could not fully delete a discarded session folder")


def ensure_flac(session: Path) -> None:
    """Si quedan WAV de la grabacion, los convierte a FLAC (el destino final es FLAC)."""
    import soundfile as sf

    for wav_name, flac_name in (
        (paths.AUDIO_ME_WAV, paths.AUDIO_ME_FLAC),
        (paths.AUDIO_REMOTE_WAV, paths.AUDIO_REMOTE_FLAC),
    ):
        wav, flac = session / wav_name, session / flac_name
        if wav.is_file() and not flac.is_file():
            data, sr = sf.read(str(wav), dtype="int16")
            tmp = session / f".{flac_name}.tmp"
            sf.write(str(tmp), data, sr, subtype="PCM_16", format="FLAC")
            os.replace(tmp, flac)
        if wav.is_file() and flac.is_file():
            wav.unlink()
