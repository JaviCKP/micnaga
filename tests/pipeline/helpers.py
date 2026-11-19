"""Utilidades compartidas por los tests del pipeline (sesiones sinteticas + motores falsos)."""

from __future__ import annotations

from pathlib import Path

from micnaga import paths
from micnaga.bench.fakes import fake_deps_for_session
from micnaga.bench.synthetic import SyntheticSpec, generate_session
from micnaga.config import Config
from micnaga.pipeline.runner import PipelineRunner
from micnaga.pipeline.stages.base import Deps
from micnaga.store.db import Database


class SimulatedCrash(BaseException):
    """Cierre brusco simulado (no es ``Exception``: el runner no la captura)."""


def relaxed_config() -> Config:
    """Compuertas relajadas para poder usar sesiones sinteticas cortas (rapidas).

    Estos tests ejercitan el motor de compuertas sobre sesiones provisionales, asi que se
    desactiva ``require_confirmed`` (en la app, una sesion sin «Si» nunca llega a transcribirse:
    ver ``test_unconfirmed_session_is_held_not_transcribed``)."""
    cfg = Config()
    cfg.gates.min_duration_s = 20.0
    cfg.gates.min_remote_speech_s = 5.0
    cfg.gates.min_turns = 3
    cfg.gates.require_confirmed = False
    return cfg


def make_session(
    name: str = "tmp-session", spec: SyntheticSpec | None = None, root: Path | None = None
) -> Path:
    folder = (root or paths.meetings_root()) / name
    generate_session(folder, spec or SyntheticSpec(duration_s=45.0))
    return folder


def open_db() -> Database:
    path = paths.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return Database(path)


def runner_for(
    session: Path | None = None,
    *,
    config: Config | None = None,
    deps: Deps | None = None,
    db: Database | None = None,
    **kwargs: object,
) -> PipelineRunner:
    cfg = config or relaxed_config()
    return PipelineRunner(
        cfg,
        deps=deps if deps is not None else fake_deps_for_session,
        db=db,
        **kwargs,  # type: ignore[arg-type]
    )
