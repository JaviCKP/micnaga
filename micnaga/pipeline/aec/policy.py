"""Cuando se cancela el eco: una sola regla para el lote (etapa 3) y el vivo (DECISIONS §35).

Modos de ``processing.aec`` (Ajustes, «Cancelacion de eco»):

* ``"auto"`` («Automatica», por defecto): solo donde se **mide** eco. Con auriculares nunca (no
  hay camino del altavoz al micro). Con altavoces o salida desconocida («unknown» no es
  «altavoces»: un escritorio remoto o un dispositivo sin nombre claro) decide la medida.
* ``"always"`` («Siempre»): se cancela todo tramo en el que suene el sistema, mida lo que mida
  (tambien con auriculares). Sin audio del sistema no hay nada que cancelar.
* ``"off"`` («Desactivada»): nunca.

Valores antiguos: ``"webrtc"`` y ``"speex"`` (el motor elegido a mano) equivalen a ``"always"``
con el motor que haya (``config._migrate`` los reescribe; ``aec_mode`` los acepta igual).

**Por tramos, igual en lote y en vivo.** La pista alineada se recorre en trozos de
``SLICE_S`` (60 s, la rejilla del vivo). Para el trozo ``[S, E)`` se mide el eco
(``echo_check.measure_echo``) en los ultimos ``LOOKBACK_S`` hasta ``E`` (un minuto suelto da una
decision que oscila) y, si toca cancelar, se cancela ``[S - PREROLL_S, E)`` con un cancelador
nuevo (el estado adaptativo no se puede guardar) y se queda ``[S, E)``. El vivo hace exactamente
lo mismo con cada trozo que llega, asi que lo calculado al grabar coincide con lo que haria el
lote y se reutiliza al colgar. Un cambio a altavoces a mitad de reunion se cancela solo desde
ahi; antes no se toca el micro.

Por que no cancelar «por si acaso»: en la reunion real de 47 min con auriculares (DECISIONS §32)
el AEC sin eco borro palabras (WER del micro 13,4 % -> Speex 19,6 %, WebRTC 24,5 %).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

from micnaga.types import AudioArray, CaptureMeta

if TYPE_CHECKING:
    from micnaga.pipeline.aec.base import EchoCanceller
    from micnaga.pipeline.aec.echo_check import WindowCache

log = logging.getLogger(__name__)

AEC_MODES: tuple[str, ...] = ("auto", "always", "off")
LEGACY_ALWAYS: frozenset[str] = frozenset({"webrtc", "speex"})
"""Motores que antes se elegian a mano: ahora son el modo «Siempre» con el motor que haya."""
SLICE_S = 60.0
LOOKBACK_S = 300.0
PREROLL_S = 8.0

REASON_OFF = "off"
REASON_HEADPHONES = "headphones"
REASON_NO_SYSTEM = "no system audio"
REASON_ECHO = "echo"
REASON_NO_ECHO = "no echo"
REASON_FORCED = "forced"


def aec_mode(value: str) -> str:
    """Modo efectivo (``auto``/``always``/``off``) de un valor de ``processing.aec``."""
    if value in ("off", "always"):
        return value
    if value in LEGACY_ALWAYS:
        return "always"
    return "auto"


def headphones_only(meta: CaptureMeta) -> bool:
    """``True`` si la salida fue de auriculares toda la reunion (ningun cambio a otra cosa)."""
    if meta.output_kind != "headphones":
        return False
    return not any(
        c.track == "system" and c.output_kind != "headphones" for c in meta.device_changes
    )


def output_kinds_during(meta: CaptureMeta, t0: float, t1: float) -> set[str]:
    """Tipos de salida (``headphones``/``speakers``/``unknown``) en ``[t0, t1)`` s de la reunion.

    Parte de ``initial_output_kind`` (lo escribe el grabador) y sigue los cambios de
    dispositivo de la pista del sistema. Sin ese dato y con cambios no se sabe: ``unknown``."""
    changes = sorted((c for c in meta.device_changes if c.track == "system"), key=lambda c: c.t)
    initial = meta.extra.get("initial_output_kind")
    if not isinstance(initial, str):
        initial = meta.output_kind if not changes else "unknown"
    kind = initial
    for c in changes:
        if c.t > t0:
            break
        kind = c.output_kind
    kinds = {kind}
    kinds.update(c.output_kind for c in changes if t0 < c.t < t1)
    return kinds


@dataclass(frozen=True)
class SliceDecision:
    apply: bool
    reason: str
    echo: dict[str, Any] | None = None


def precheck(mode: str, meta: CaptureMeta, span: tuple[float, float] | None = None) -> str | None:
    """Motivo para no cancelar sin mirar el audio (toda la reunion o el tramo ``span`` en s), o
    ``None``. En «Automatica» con auriculares en todo el tramo no se cancela nunca."""
    if mode == "off":
        return REASON_OFF
    if mode == "auto":
        if span is None:
            if headphones_only(meta):
                return REASON_HEADPHONES
        elif output_kinds_during(meta, *span) == {"headphones"}:
            return REASON_HEADPHONES
    return None


def decide(
    mode: str,
    mic: AudioArray,
    ref: AudioArray,
    sr: int,
    *,
    offset: int = 0,
    cache: WindowCache | None = None,
) -> SliceDecision:
    """Decision para un trozo a partir de su ventana de medida (``LOOKBACK_S`` hasta su fin)."""
    from micnaga.pipeline.aec.echo_check import measure_echo

    check = measure_echo(mic, ref, sr, offset=offset, cache=cache)
    info = check.to_dict()
    if check.reason == "no system audio" or check.reason == "no audio":
        return SliceDecision(False, REASON_NO_SYSTEM, info)
    if mode == "always":
        return SliceDecision(True, REASON_FORCED, info)
    if check.echo:
        return SliceDecision(True, REASON_ECHO, info)
    return SliceDecision(False, REASON_NO_ECHO, info)


def decide_slice(
    mode: str,
    meta: CaptureMeta,
    mic: AudioArray,
    ref: AudioArray,
    sr: int,
    bounds: tuple[int, int],
    *,
    offset: int | None = None,
    cache: WindowCache | None = None,
) -> SliceDecision:
    """Decision del trozo ``[bounds)`` (muestras) con su ventana de medida ``mic``/``ref``: la
    misma funcion en el lote y en el vivo (``offset``/``cache``: solo ahorran calculo)."""
    skip = precheck(mode, meta, (bounds[0] / sr, bounds[1] / sr))
    if skip is not None:
        return SliceDecision(False, skip)
    from micnaga.pipeline.aec.echo_check import reference_active_s

    if offset is None:
        offset = max(0, bounds[1] - len(ref))
    start, end = bounds[0] - offset, bounds[1] - offset
    active_s = reference_active_s(ref[max(0, start) : max(0, end)], sr)
    if not active_s:
        return SliceDecision(False, REASON_NO_SYSTEM)
    if mode == "always":
        return SliceDecision(True, REASON_FORCED, {"active_s": active_s})
    return decide(mode, mic, ref, sr, offset=offset, cache=cache)


def slice_bounds(k: int, length: int, sr: int) -> tuple[int, int, int, int]:
    """``(inicio, fin, ini_medida, ini_cancelado)`` del trozo ``k`` (muestras alineadas)."""
    slice_len = round(SLICE_S * sr)
    start = k * slice_len
    end = min(start + slice_len, length)
    return start, end, max(0, end - round(LOOKBACK_S * sr)), max(0, start - round(PREROLL_S * sr))


def cancel_span(
    canceller: EchoCanceller, mic: AudioArray, ref: AudioArray, sr: int
) -> NDArray[np.float32]:
    out = np.asarray(canceller.process(mic, ref, sr), dtype=np.float32)
    if len(out) != len(mic):
        raise ValueError(f"echo canceller returned {len(out)} samples, expected {len(mic)}")
    return out


@dataclass
class AecSummary:
    """Lo que se decidio en una reunion (marca de la etapa 3 y log)."""

    mode: str
    slices: list[dict[str, Any]] = field(default_factory=list)
    engine: str | None = None

    def add(self, k: int, decision: SliceDecision) -> None:
        item: dict[str, Any] = {"k": k, "apply": decision.apply, "reason": decision.reason}
        if decision.echo is not None:
            item["coherence"] = decision.echo.get("coherence")
            item["echo_db"] = decision.echo.get("echo_db")
            item["delay_ms"] = decision.echo.get("delay_ms")
        self.slices.append(item)

    @property
    def applied(self) -> list[int]:
        return [s["k"] for s in self.slices if s["apply"]]

    def info(self) -> dict[str, Any]:
        """Marca de la etapa 3: ``aec`` = motor si se cancelo algun trozo; si no ``skipped``."""
        applied = self.applied
        reasons: dict[str, int] = {}
        for s in self.slices:
            reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
        coh = [s["coherence"] for s in self.slices if s.get("coherence") is not None]
        out: dict[str, Any] = {
            "mode": self.mode,
            "slices": len(self.slices),
            "applied_slices": applied,
            "reasons": reasons,
            "max_coherence": round(max(coh), 3) if coh else None,
        }
        if applied and self.engine:
            out["aec"] = self.engine
        else:
            out["skipped"] = _main_reason(reasons)
        return out

    def describe(self) -> str:
        info = self.info()
        applied = info["applied_slices"]
        if applied:
            spans = ", ".join(f"{k}-{k + 1} min" for k in applied[:8])
            more = "" if len(applied) <= 8 else f" (+{len(applied) - 8})"
            n = f"{len(applied)}/{len(self.slices)}"
            what = f"applied with {self.engine} in {n} min: {spans}{more}"
        else:
            what = f"not applied ({info['skipped']})"
        return (
            f"echo canceller (mode {self.mode}): {what}; reasons {info['reasons']}, "
            f"max coherence {info['max_coherence']}"
        )


def _main_reason(reasons: dict[str, int]) -> str:
    if not reasons:
        return REASON_NO_SYSTEM
    for r in (REASON_NO_ECHO, REASON_NO_SYSTEM, "unavailable"):
        if r in reasons:
            return r
    return max(reasons, key=lambda r: reasons[r])


def cancel_track(
    mode: str,
    meta: CaptureMeta,
    mic: AudioArray,
    ref: AudioArray,
    sr: int,
    canceller: EchoCanceller,
    *,
    progress: Callable[[float], None] | None = None,
) -> tuple[NDArray[np.float32] | None, AecSummary]:
    """Lote: aplica la regla trozo a trozo sobre la pista alineada entera.

    Devuelve el micro limpio (``None`` si no se cancelo ningun trozo) y el resumen."""
    n = min(len(mic), len(ref))
    mic = np.asarray(mic[:n], dtype=np.float32)
    ref = np.asarray(ref[:n], dtype=np.float32)
    summary = AecSummary(mode, engine=getattr(canceller, "name", None))
    slice_len = round(SLICE_S * sr)
    n_slices = -(-n // slice_len) if n else 0
    out: NDArray[np.float32] | None = None
    cache: WindowCache = {}  # las ventanas de 5 min se solapan: cada trozo de 20 s se mide 1 vez
    for k in range(n_slices):
        start, end, lo_echo, lo_aec = slice_bounds(k, n, sr)
        decision = decide_slice(
            mode,
            meta,
            mic[lo_echo:end],
            ref[lo_echo:end],
            sr,
            (start, end),
            offset=lo_echo,
            cache=cache,
        )
        summary.add(k, decision)
        if decision.apply:
            cleaned = cancel_span(canceller, mic[lo_aec:end], ref[lo_aec:end], sr)
            if out is None:
                out = mic.copy()
            out[start:end] = cleaned[start - lo_aec :]
        if progress is not None:
            progress((k + 1) / n_slices)
    return out, summary


__all__ = [
    "AEC_MODES",
    "LOOKBACK_S",
    "PREROLL_S",
    "SLICE_S",
    "AecSummary",
    "SliceDecision",
    "aec_mode",
    "cancel_track",
    "decide",
    "decide_slice",
    "headphones_only",
    "output_kinds_during",
    "precheck",
    "slice_bounds",
]
