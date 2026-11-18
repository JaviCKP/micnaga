"""Sincronia entre pistas: estimacion de deriva y alineacion (seccion 7, trampa 2).

Funciones **puras** (numpy dentro, numpy fuera; sin hilos, sin disco, sin Windows). La etapa 2
del pipeline llama a :func:`align_tracks`.

Convencion de ``SyncMark``
--------------------------
``SyncMark(samples, clock_s)`` empareja un indice de muestra de la pista **tal como esta en
disco** (16 kHz, relleno de silencios incluido) con el **reloj comun** (QueryPerformanceCounter)
en segundos desde el inicio de la sesion, **sin contar las pausas privadas**. Es la misma linea de
tiempo que ``Marker.t`` y ``PrivatePause.at_s``. Por construccion la muestra 0 de las dos pistas
corresponde a ``clock_s = 0`` (el grabador rellena con ceros hasta el primer paquete).

Si la tarjeta de sonido va a la misma velocidad que el reloj comun, ``clock_s == samples / sr``.
Si va lenta, ``clock_s`` crece mas deprisa que ``samples / sr`` (la pista "se queda corta").
Las marcas se toman cada 10 s y en cada corte (pausa, parada). Con ellas se construye una
correspondencia lineal a trozos ``muestra -> reloj``; :func:`align_tracks` remuestrea cada pista
sobre el reloj comun, de modo que tras la alineacion el indice ``k`` de las dos pistas es el
instante ``k / sr`` de la linea de tiempo de la reunion.

El desfase constante entre las dos pistas debido a la latencia de captura de cada dispositivo
(decenas de ms como mucho) no se puede medir desde software y no se corrige.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import soxr
from numpy.typing import NDArray
from scipy.ndimage import map_coordinates

from micnaga.types import CaptureMeta, SyncMark

DEFAULT_SAMPLE_RATE = 16000
SHIFT_ONLY_TOLERANCE_SAMPLES = 1.0
"""Si la correccion varia menos que esto en toda la pista, basta un desplazamiento entero."""
_CHUNK = 1 << 20
_MARGIN = 64
"""Muestras de contexto a cada lado de un trozo (la sobremuestra de soxr tiene transitorios)."""

F32 = NDArray[np.float32]
F64 = NDArray[np.float64]


@dataclass(frozen=True)
class DriftEstimate:
    """Resultado de :func:`estimate_drift` (ajuste lineal robusto de la correspondencia)."""

    rate: float
    """Segundos de reloj comun por segundo de pista. ``> 1``: el reloj de la pista va lento."""
    ppm: float
    """``(rate - 1) * 1e6``."""
    offset_s: float
    """``clock - samples / sr`` extrapolado a la muestra 0 (≈ 0 si el anclaje es bueno)."""
    drift_s: float
    """Deriva acumulada en todo el tramo cubierto por las marcas, en segundos."""
    residual_s: float
    """Mayor ``|marca - recta|`` en segundos (la parte no lineal: saltos, huecos sin rellenar)."""
    n_marks: int


def _marks_arrays(marks: Sequence[SyncMark]) -> tuple[F64, F64]:
    if not marks:
        return np.empty(0), np.empty(0)
    s = np.array([m.samples for m in marks], dtype=np.float64)
    c = np.array([m.clock_s for m in marks], dtype=np.float64)
    return s, c


def estimate_drift(
    marks: Sequence[SyncMark], sample_rate: int = DEFAULT_SAMPLE_RATE
) -> DriftEstimate:
    """Ajusta ``clock_s = a + rate * samples / sr`` (pendiente robusta a saltos y atipicos).

    Con menos de dos marcas devuelve ``rate = 1``. Pensado para informar (CLI, logs, tests) y
    para decidir si hace falta remuestrear; :func:`align_tracks` usa la correspondencia a
    trozos, que ademas absorbe saltos puntuales.
    """
    s, c = _marks_arrays(marks)
    if len(s) < 2:
        return DriftEstimate(1.0, 0.0, 0.0, 0.0, 0.0, len(s))
    t = s / sample_rate
    y = c - t
    # Pendientes entre marcas separadas ~1/6 del tramo: baseline largo (poco ruido) y un salto
    # puntual solo afecta a ~1/5 de las parejas, asi que la mediana lo ignora.
    gap = max(1, len(t) // 6)
    dt = t[gap:] - t[:-gap]
    ok = dt > 1e-9
    slope = float(np.median(((y[gap:] - y[:-gap])[ok]) / dt[ok])) if ok.any() else 0.0
    intercept = float(np.median(y - slope * t))
    resid = float(np.max(np.abs(y - (intercept + slope * t))))
    span = float(t[-1] - t[0])
    return DriftEstimate(
        rate=1.0 + slope,
        ppm=slope * 1e6,
        offset_s=intercept,
        drift_s=slope * span,
        residual_s=resid,
        n_marks=len(s),
    )


def clock_knots(
    marks: Sequence[SyncMark], sample_rate: int = DEFAULT_SAMPLE_RATE, n_samples: int | None = None
) -> tuple[F64, F64]:
    """Nudos ``(muestras, reloj_s)`` estrictamente crecientes de la correspondencia a trozos.

    * Se ancla en ``(0, 0)`` salvo que haya una marca en la muestra 0 (entonces manda esa).
    * Se descartan marcas que no crecen en muestras o en reloj (jitter, duplicados).
    * Si ``n_samples`` pasa de la ultima marca, se prolonga con la pendiente global (o 1).
    """
    s, c = _marks_arrays(marks)
    order = np.argsort(s, kind="stable")
    s, c = s[order], c[order]
    ks: list[float] = [0.0]
    kc: list[float] = [0.0]
    for si, ci in zip(s, c, strict=True):
        if si <= 0:
            kc[0] = float(ci)
            continue
        if si > ks[-1] and ci > kc[-1]:
            ks.append(float(si))
            kc.append(float(ci))
    if n_samples is not None and n_samples > ks[-1]:
        rate = estimate_drift(marks, sample_rate).rate if len(marks) >= 2 else 1.0
        ks.append(float(n_samples))
        kc.append(kc[-1] + (n_samples - ks[-2]) / sample_rate * rate)
    return np.array(ks), np.array(kc)


def _warp_chunk(x: F32, p: F64, sample_rate: int, oversample: int) -> F32:
    """``y[j] = x(p[j])`` con ``p`` fraccionario (spline cubico sobre senal sobremuestreada)."""
    n = len(x)
    lo = max(math.floor(float(p.min())) - _MARGIN, 0)
    hi = min(math.ceil(float(p.max())) + _MARGIN, n)
    if hi <= lo or p.max() < 0 or p.min() > n:
        return np.zeros(len(p), dtype=np.float32)
    seg = np.ascontiguousarray(x[lo:hi], dtype=np.float32)
    if oversample > 1:
        seg = soxr.resample(seg, sample_rate, sample_rate * oversample, quality="HQ")
    coords = ((p - lo) * oversample).astype(np.float64)
    y = map_coordinates(
        seg.astype(np.float64), [coords], order=3, mode="constant", cval=0.0, prefilter=True
    )
    # Fuera de la senal real (antes de 0 o despues de n) debe ser silencio exacto.
    y[(p < 0) | (p > n - 1)] = 0.0
    out: F32 = y.astype(np.float32)
    return out


def resample_to_clock(
    x: F32,
    marks: Sequence[SyncMark],
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    *,
    n_out: int | None = None,
    oversample: int = 4,
) -> F32:
    """Remuestrea ``x`` para que el indice ``k`` sea el instante ``k / sr`` del reloj comun.

    La correspondencia ``muestra -> reloj`` sale de ``marks`` (lineal a trozos). Si la correccion
    es despreciable (menos de 1 muestra de variacion) solo se desplaza un numero entero de
    muestras. En otro caso se interpola con un spline cubico sobre la senal sobremuestreada
    x``oversample`` con soxr HQ (error < 1 % hasta 6 kHz); se procesa por trozos de ~1 M
    muestras, asi que la memoria no crece con la duracion.

    ``n_out`` por defecto es la duracion de ``x`` en el reloj comun.
    """
    x = np.asarray(x, dtype=np.float32)
    n = len(x)
    ks, kc = clock_knots(marks, sample_rate, n_samples=n)
    c_end = float(np.interp(n, ks, kc)) if n else 0.0
    if n_out is None:
        n_out = round(c_end * sample_rate)
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    out = np.zeros(n_out, dtype=np.float32)
    if n == 0:
        return out

    def positions_at(tau: F64) -> F64:
        p = np.interp(tau, kc, ks)
        # Fuera de los nudos: pendiente 1 (una muestra por muestra).
        before = tau < kc[0]
        after = tau > kc[-1]
        if before.any():
            p[before] = ks[0] + (tau[before] - kc[0]) * sample_rate
        if after.any():
            p[after] = ks[-1] + (tau[after] - kc[-1]) * sample_rate
        return p

    def positions(j0: int, j1: int) -> F64:
        return positions_at(np.arange(j0, j1, dtype=np.float64) / sample_rate)

    # Camino rapido: ``p - j`` es lineal a trozos en ``j``, asi que sus extremos estan en los
    # nudos (y en los bordes de la salida).
    tau_probe = np.concatenate([kc, [0.0, (n_out - 1) / sample_rate]])
    j_probe = tau_probe * sample_rate
    d = positions_at(tau_probe) - j_probe
    if float(np.ptp(d)) <= SHIFT_ONLY_TOLERANCE_SAMPLES:
        k = round(float(np.mean(d)))
        src0, src1 = max(k, 0), min(n, n_out + k)
        dst0 = max(-k, 0)
        if src1 > src0:
            out[dst0 : dst0 + (src1 - src0)] = x[src0:src1]
        return out

    for j0 in range(0, n_out, _CHUNK):
        j1 = min(j0 + _CHUNK, n_out)
        out[j0:j1] = _warp_chunk(x, positions(j0, j1), sample_rate, oversample)
    return out


def align_tracks(
    mic: F32, system: F32, meta: CaptureMeta, sample_rate: int | None = None
) -> tuple[F32, F32]:
    """Alinea las dos pistas sobre el reloj comun (etapa 2 del pipeline).

    Parametros
    ----------
    mic, system:
        Audio mono ``float32`` en [-1, 1] tal como esta en disco (16 kHz).
    meta:
        ``CaptureMeta`` leido de ``capture.json``; se usan ``meta.mic.sync`` y
        ``meta.system.sync`` (y ``meta.mic.sample_rate`` si no se da ``sample_rate``).
    sample_rate:
        Para pruebas con otra frecuencia; por defecto la de la pista.

    Devuelve
    --------
    ``(mic, system)`` ``float32`` con **la misma longitud**; el indice ``k`` de ambas es el
    instante ``k / sr`` de la linea de tiempo de la reunion (sin pausas privadas). La pista mas
    corta se completa con silencio. Sin marcas (sesion muy corta, ficheros antiguos) solo se
    igualan las longitudes. Deriva objetivo: < 50 ms tras 1 h.
    """
    sr = sample_rate if sample_rate is not None else (meta.mic.sample_rate or DEFAULT_SAMPLE_RATE)
    mic_c = resample_to_clock(mic, meta.mic.sync, sr)
    sys_c = resample_to_clock(system, meta.system.sync, sr)
    n = max(len(mic_c), len(sys_c))
    if len(mic_c) < n:
        mic_c = np.concatenate([mic_c, np.zeros(n - len(mic_c), dtype=np.float32)])
    if len(sys_c) < n:
        sys_c = np.concatenate([sys_c, np.zeros(n - len(sys_c), dtype=np.float32)])
    return mic_c, sys_c


def track_duration_s(
    marks: Sequence[SyncMark], n_samples: int, sample_rate: int = DEFAULT_SAMPLE_RATE
) -> float:
    """Duracion de una pista sobre el reloj comun (segundos)."""
    ks, kc = clock_knots(marks, sample_rate, n_samples=n_samples)
    return float(np.interp(n_samples, ks, kc))
