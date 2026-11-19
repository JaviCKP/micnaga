"""Etapa 3: eliminacion de eco, solo con altavoces (spec 8.3).

Se aplica si ``output_kind != "headphones"`` (altavoces o desconocido) **o** si algun cambio de
dispositivo de la pista del sistema paso a algo que no son auriculares (caso trampa «cambio de
auriculares a altavoces a mitad»). El cancelador (``registry``/``processing.aec``) limpia la pista
del micro usando la del sistema como referencia; la del sistema no cambia.

Antes de cancelar se **mide** si hay eco (``aec.echo_check.measure_echo``: pico de GCC-PHAT +
coherencia entre pistas, ~1 s): con auriculares o un altavoz que no llega al micro el
cancelador solo gasta tiempo (2 min en una reunion de 47 min), asi que se salta y la decision
queda en el log y en la marca (``info.echo``). Si se oye el sistema en el micro se cancela como
siempre. La segunda capa (etapa 9) sigue siendo la red de seguridad.

Degradacion: con ``aec = "off"`` o si no hay ninguna implementacion instalada
(``EngineUnavailableError``) la etapa termina sin salida y deja constancia en su marca; la segunda
capa (etapa 9) es la red de seguridad.

Salida: ``work/03_mic_aec.wav`` (si se aplico).
"""

from __future__ import annotations

import gc
import logging
from typing import Any

import numpy as np

from micnaga.pipeline.stages.base import StageContext, StageResult
from micnaga.registry import EngineUnavailableError
from micnaga.types import AudioArray, CaptureMeta

log = logging.getLogger(__name__)

OUTPUT = "03_mic_aec.wav"


def needs_aec(meta: CaptureMeta) -> bool:
    if meta.output_kind != "headphones":
        return True
    return any(c.track == "system" and c.output_kind != "headphones" for c in meta.device_changes)


def check_echo(mic: AudioArray, ref: AudioArray, sr: int) -> dict[str, Any]:
    """Mide el eco y lo deja en el log (solo metricas). ``info["echo"]`` dice si cancelar."""
    from micnaga.pipeline.aec.echo_check import measure_echo

    result = measure_echo(mic, ref, sr).to_dict()
    log.info(
        "echo check: %s (coherence=%.3f, echo=%.1f dB, delay=%.0f ms, system speech=%.0f s)",
        "echo, cancelling" if result["echo"] else "negligible, AEC skipped",
        result["coherence"],
        result["echo_db"],
        result["delay_ms"],
        result["active_s"],
    )
    return result


def _close(canceller: Any) -> None:
    close = getattr(canceller, "close", None)
    if callable(close):
        close()


def run(ctx: StageContext) -> StageResult:
    if not needs_aec(ctx.meta):
        return StageResult([], {"skipped": "headphones"})
    if ctx.config.processing.aec == "off":
        return StageResult([], {"skipped": "off"})
    try:
        canceller = ctx.deps.create_aec(ctx.config)
    except EngineUnavailableError as exc:
        log.warning("no echo canceller available, relying on text de-duplication: %s", exc)
        return StageResult([], {"skipped": "unavailable", "detail": str(exc)})
    if canceller is None:
        return StageResult([], {"skipped": "off"})
    try:
        mic = ctx.read_work_audio("02_mic.wav")
        ref = ctx.read_work_audio("02_system.wav")
        echo = check_echo(mic, ref, ctx.sr)
        if not echo["echo"]:
            return StageResult([], {"skipped": "no echo", "echo": echo})
        ctx.progress(0.3)
        cleaned = np.asarray(canceller.process(mic, ref, ctx.sr), dtype=np.float32)
        if len(cleaned) != len(mic):
            raise ValueError(f"echo canceller returned {len(cleaned)} samples, expected {len(mic)}")
        engine_name = canceller.name
    finally:
        _close(canceller)
        del canceller
        gc.collect()
    ctx.write_audio(OUTPUT, cleaned)
    return StageResult([OUTPUT], {"aec": engine_name, "echo": echo})
