"""Compuertas anti falsos positivos (MIC-04, spec seccion 5).

Modulo ligero (solo biblioteca estandar en tiempo de importacion): el vigilante lo llama al
cerrar una sesion y el pipeline (etapa 1) lo vuelve a llamar sobre el audio completo.

Una sesion pasa a la cola solo si cumple las tres compuertas (configurables en ``[gates]``):

1. duracion efectiva >= ``min_duration_s`` (las pausas privadas ya no estan en el audio, asi que
   la duracion efectiva es ``muestras / frecuencia``; no se restan otra vez);
2. voz remota >= ``min_remote_speech_s`` (VAD sobre la pista del sistema);
3. cambios de turno micro <-> sistema >= ``min_turns``.

Una sesion confirmada con «Grabar» solo exige la primera.

Cambio de turno (definicion precisa)
------------------------------------
Se parte de los tramos de voz (VAD) de cada pista, ya unidos por pista (``merge_regions``):

* ``yo``     = voz del micro que **no** esta cubierta por voz del sistema (el tramo del
  sistema se ensancha ``echo_tail_s`` = 0,3 s por detras, porque el eco llega despues que el
  sonido original);
* ``remoto`` = voz del sistema que no es ``yo``.

Con altavoces (y antes de la cancelacion de eco, etapa 3) el micro recoge el eco del sistema, y
un video de YouTube con Meet abierto no debe fabricar turnos: la voz del micro cubierta por voz del
sistema no cuenta como voz propia, y la doble habla se atribuye al sistema.

Los trozos ``yo``/``remoto`` de menos de ``min_spell_s`` (0,3 s) se ignoran: son restos de
solapes casi totales. Los trozos restantes se ordenan en el tiempo y un **cambio de turno**
es cada par de trozos consecutivos con etiqueta distinta (los trozos consecutivos de la misma
etiqueta cuentan como un solo turno; los silencios no cuentan como turno de nadie y no
rompen la secuencia).

Los solapes entre tramos (p. ej. por el margen ``vad.pad_ms`` que el VAD anade a cada lado, que
hace que un relevo limpio solape hasta ``2 * pad_ms``) se reparten asi sin crear turnos
espurios, de modo que el resultado no depende del valor de ``pad_ms``.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from micnaga.config import GatesConfig

MIN_SPELL_S = 0.3
ECHO_TAIL_S = 0.3

Span = tuple[float, float]


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str
    """``"ok"`` si pasa; si no, motivo legible (sin datos identificativos de la reunion)."""
    gate: str | None = None
    """Compuerta que fallo: ``"duration"``, ``"remote_speech"`` o ``"turns"``."""


@dataclass(frozen=True)
class GateMetrics:
    duration_s: float
    remote_speech_s: float
    turn_changes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_s": self.duration_s,
            "remote_speech_s": self.remote_speech_s,
            "turn_changes": self.turn_changes,
        }


def evaluate_gates(
    duration_s_effective: float,
    remote_speech_s: float,
    turn_changes: int,
    confirmed: bool,
    cfg: GatesConfig,
) -> GateResult:
    """Aplica las tres compuertas (solo la 1 si ``confirmed``)."""
    if duration_s_effective < cfg.min_duration_s:
        return GateResult(
            False,
            f"duracion {duration_s_effective:.0f} s < {cfg.min_duration_s:.0f} s",
            "duration",
        )
    if confirmed:
        return GateResult(True, "ok")
    if remote_speech_s < cfg.min_remote_speech_s:
        return GateResult(
            False,
            f"voz remota {remote_speech_s:.0f} s < {cfg.min_remote_speech_s:.0f} s",
            "remote_speech",
        )
    if turn_changes < cfg.min_turns:
        return GateResult(False, f"cambios de turno {turn_changes} < {cfg.min_turns}", "turns")
    return GateResult(True, "ok")


# ----------------------------------------------------------------------------- intervalos


def _spans(regions: Iterable[Any]) -> list[Span]:
    """Acepta ``SpeechRegion`` (``.start``/``.end``) o tuplas ``(inicio, fin)``."""
    out: list[Span] = []
    for r in regions:
        if isinstance(r, tuple):
            a, b = float(r[0]), float(r[1])
        else:
            a, b = float(r.start), float(r.end)
        if b > a:
            out.append((a, b))
    return out


def merge_regions(regions: Iterable[Any]) -> list[Span]:
    """Une tramos solapados o contiguos; devuelve una lista ordenada y disjunta."""
    merged: list[Span] = []
    for a, b in sorted(_spans(regions)):
        if merged and a <= merged[-1][1]:
            if b > merged[-1][1]:
                merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def remote_speech_seconds(regions: Iterable[Any]) -> float:
    """Segundos de voz de la union de los tramos (compuerta 2)."""
    return float(sum(b - a for a, b in merge_regions(regions)))


def _subtract(base: Sequence[Span], cut: Sequence[Span]) -> list[Span]:
    """``base \\ cut`` (ambas listas ordenadas y disjuntas)."""
    out: list[Span] = []
    j = 0
    for a, b in base:
        cur = a
        while j < len(cut) and cut[j][1] <= cur:
            j += 1
        k = j
        while k < len(cut) and cut[k][0] < b:
            c0, c1 = cut[k]
            if c0 > cur:
                out.append((cur, c0))
            cur = max(cur, c1)
            if cur >= b:
                break
            k += 1
        if cur < b:
            out.append((cur, b))
    return out


def turn_spells(
    me_regions: Iterable[Any],
    system_regions: Iterable[Any],
    *,
    min_spell_s: float = MIN_SPELL_S,
    echo_tail_s: float = ECHO_TAIL_S,
) -> list[tuple[float, float, str]]:
    """Trozos ``(inicio, fin, "me"|"remote")`` en orden temporal (ver la docstring del modulo)."""
    me = merge_regions(me_regions)
    remote = merge_regions(system_regions)
    shadow = merge_regions((a, b + echo_tail_s) for a, b in remote)
    me_only = _subtract(me, shadow)
    remote_only = _subtract(remote, me_only)
    spells = [(a, b, "me") for a, b in me_only if b - a >= min_spell_s]
    spells += [(a, b, "remote") for a, b in remote_only if b - a >= min_spell_s]
    spells.sort()
    return spells


def count_turn_changes(
    me_regions: Iterable[Any],
    system_regions: Iterable[Any],
    *,
    min_spell_s: float = MIN_SPELL_S,
    echo_tail_s: float = ECHO_TAIL_S,
) -> int:
    """Numero de cambios de turno micro <-> sistema (definicion en la docstring del modulo)."""
    spells = turn_spells(
        me_regions, system_regions, min_spell_s=min_spell_s, echo_tail_s=echo_tail_s
    )
    changes = 0
    for prev, cur in itertools.pairwise(spells):
        if prev[2] != cur[2]:
            changes += 1
    return changes


def compute_gate_metrics(
    duration_s: float, me_regions: Iterable[Any], system_regions: Iterable[Any]
) -> GateMetrics:
    system = merge_regions(system_regions)
    return GateMetrics(
        duration_s=float(duration_s),
        remote_speech_s=remote_speech_seconds(system),
        turn_changes=count_turn_changes(me_regions, system),
    )


def evaluate_regions(
    duration_s: float,
    me_regions: Iterable[Any],
    system_regions: Iterable[Any],
    confirmed: bool,
    cfg: GatesConfig,
) -> tuple[GateResult, GateMetrics]:
    """Atajo: metricas a partir de los tramos de voz de ambas pistas + evaluacion."""
    metrics = compute_gate_metrics(duration_s, me_regions, system_regions)
    result = evaluate_gates(
        metrics.duration_s, metrics.remote_speech_s, metrics.turn_changes, confirmed, cfg
    )
    return result, metrics
