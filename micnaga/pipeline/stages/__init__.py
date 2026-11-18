"""Las 11 etapas del pipeline (spec seccion 8), una por fichero ``sNN_<nombre>.py``.

Este paquete es ligero al importarse: ``get_stages()`` importa los modulos de las etapas (y con
ellos numpy, scipy...) solo cuando alguien los pide. Logica pura reutilizable sin dependencias
pesadas: ``phrases``. Los nombres siguen ``micnaga.types.PIPELINE_STAGES``.
"""

from __future__ import annotations

import importlib

from micnaga.pipeline.stages.base import Stage
from micnaga.types import PIPELINE_STAGES

_MODULES = (
    "s01_gates",
    "s02_align",
    "s03_aec",
    "s04_condition",
    "s05_vad",
    "s06_asr",
    "s07_diarize",
    "s08_assign",
    "s09_echo_dedup",
    "s10_names",
    "s11_assemble",
)


def get_stages() -> tuple[Stage, ...]:
    """Las etapas en orden; ``Stage.index`` es 1-based y casa con ``jobs.stage``."""
    stages: list[Stage] = []
    for index, (name, module_name) in enumerate(zip(PIPELINE_STAGES, _MODULES, strict=True), 1):
        module = importlib.import_module(f"{__name__}.{module_name}")
        stages.append(Stage(index, name, module.run))
    return tuple(stages)


__all__ = ["Stage", "get_stages"]
