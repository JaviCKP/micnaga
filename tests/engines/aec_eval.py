"""Banco de pruebas sintetico de cancelacion de eco (MIC-05, spec F1).

Escenario de 60 s a 16 kHz:

    0-15 s   solo lejano (far-end) hablando         -> ERLE (convergencia)
    15-25 s  doble conversacion (far + near)        -> distorsion del hablante cercano
    25-35 s  solo cercano (near-end)                -> el AEC no debe dañar la voz limpia
    35-50 s  solo lejano                            -> ERLE ya convergido
    50-60 s  doble conversacion

El eco es la voz lejana (opcionalmente con alinealidad ``tanh`` del altavoz) convolucionada con
una respuesta al impulso de sala SINTETICA (impulso directo con retardo ``delay_ms`` + cola de
ruido con decaimiento exponencial segun ``rt60``). Una RIR sintetica NO es una sala real: las
cifras sirven para comparar motores entre si, no para prometer rendimiento en una oficina.

Metricas:

* ``erle_db``: 10*log10(potencia del micro / potencia de la salida) en los tramos solo-lejano
  (``erle_conv_db`` = 5-15 s, ya convergiendo; ``erle_late_db`` = 35-50 s; ``erle_first5_db`` =
  0-5 s).
* ``dt_sdr_db`` (doble conversacion): 10*log10(|near|^2 / |salida - g*near|^2) con g la ganancia
  optima; incluye eco residual + distorsion. ``dt_gain_db`` = 20*log10|g| (atenuacion media del
  hablante cercano; 0 dB = intacto).
* ``near_only_sdr_db``: lo mismo en 25-35 s (solo cercano, referencia en silencio).
* ``xrt``: segundos de audio por segundo de CPU (veces tiempo real).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from micnaga.types import AudioArray
from tests.synth import SR, speech_like

SEGMENTS: dict[str, list[tuple[float, float]]] = {
    "far_only": [(0, 15), (35, 50)],
    "double": [(15, 25), (50, 60)],
    "near_only": [(25, 35)],
}
DURATION = 60.0


@dataclass
class Scenario:
    name: str
    far: AudioArray
    near: AudioArray
    echo: AudioArray
    mic: AudioArray
    params: dict[str, float | str] = field(default_factory=dict)


def _mask(sig: AudioArray, spans: list[tuple[float, float]], sr: int = SR) -> AudioArray:
    out = np.zeros(int(DURATION * sr), dtype=np.float32)
    for a, b in spans:
        i, j = int(a * sr), int(b * sr)
        out[i:j] = sig[i:j]
    return out


def _tile(sig: AudioArray, n: int) -> AudioArray:
    reps = -(-n // len(sig))
    return np.tile(sig, reps)[:n].astype(np.float32)


def synthetic_voice(seed: int, f0: float, duration: float = DURATION) -> AudioArray:
    """Voz sintetica de 60 s: f0 que deriva lentamente, silabas y pausas aleatorias."""
    rng = np.random.default_rng(seed)
    n = int(duration * SR)
    out = np.zeros(n, dtype=np.float32)
    pos = 0
    while pos < n:
        seg = int(rng.uniform(0.6, 2.2) * SR)
        f = f0 * rng.uniform(0.8, 1.25)
        piece = speech_like(
            seg / SR,
            f0=f,
            amp=rng.uniform(0.15, 0.35),
            syllable_hz=rng.uniform(3, 6),
            seed=seed + pos,
        )
        end = min(n, pos + len(piece))
        out[pos:end] = piece[: end - pos]
        pos = end + int(rng.uniform(0.1, 0.5) * SR)
    return out


def synthetic_rir(
    delay_ms: float, rt60: float, sr: int = SR, seed: int = 1, length_s: float = 0.5
) -> AudioArray:
    """RIR sintetica: pico directo + 4 reflexiones tempranas + cola exponencial."""
    rng = np.random.default_rng(seed)
    n = int(length_s * sr)
    h = np.zeros(n, dtype=np.float64)
    d = int(delay_ms / 1000 * sr)
    h[d] = 1.0
    for dt_ms, g in [(3, 0.5), (7, -0.35), (12, 0.3), (19, -0.2)]:
        j = d + int(dt_ms / 1000 * sr)
        if j < n:
            h[j] += g
    t = np.arange(n - d) / sr
    tail = rng.standard_normal(n - d) * np.exp(-6.91 * t / max(rt60, 0.05))
    tail[: int(0.002 * sr)] = 0.0
    h[d:] += 0.12 * tail
    return h.astype(np.float32)


def build_scenario(
    far_src: AudioArray,
    near_src: AudioArray,
    *,
    delay_ms: float = 90.0,
    rt60: float = 0.3,
    nonlinear: bool = False,
    echo_db: float = 0.0,
    noise_db: float = -75.0,
    seed: int = 0,
    name: str = "",
) -> Scenario:
    """Mezcla el micro: near + eco(far) + ruido (``echo_db``: eco sobre la voz cercana)."""
    from scipy.signal import fftconvolve

    n = int(DURATION * SR)
    far = _mask(_tile(far_src, n), SEGMENTS["far_only"] + SEGMENTS["double"])
    near = _mask(_tile(near_src, n), SEGMENTS["double"] + SEGMENTS["near_only"])
    drive = np.tanh(2.5 * far) / 2.5 * 1.0 if nonlinear else far  # altavoz saturado
    h = synthetic_rir(delay_ms, rt60, seed=seed + 1)
    echo = fftconvolve(drive, h)[:n].astype(np.float32)
    active_near = near[np.abs(near) > 1e-4]
    active_echo = echo[np.abs(echo) > 1e-4]
    near_rms = float(np.sqrt(np.mean(active_near**2))) if len(active_near) else 0.1
    echo_rms = float(np.sqrt(np.mean(active_echo**2))) if len(active_echo) else 0.1
    echo = echo * (near_rms / echo_rms * 10 ** (echo_db / 20))
    rng = np.random.default_rng(seed + 7)
    noise = (10 ** (noise_db / 20) * rng.standard_normal(n)).astype(np.float32)
    mic = np.clip(near + echo + noise, -1, 1).astype(np.float32)
    return Scenario(
        name=name or f"d{int(delay_ms)}_rt{rt60}",
        far=far,
        near=near,
        echo=echo,
        mic=mic,
        params={
            "delay_ms": delay_ms,
            "rt60": rt60,
            "nonlinear": str(nonlinear),
            "echo_db": echo_db,
        },
    )


def _power(x: AudioArray) -> float:
    return float(np.mean(np.square(x.astype(np.float64)))) + 1e-20


def _gather(sig: AudioArray, spans: list[tuple[float, float]], sr: int = SR) -> AudioArray:
    return np.concatenate([sig[int(a * sr) : int(b * sr)] for a, b in spans])


def _sdr_gain(out: AudioArray, near: AudioArray, taps: int = 32) -> tuple[float, float]:
    """SDR "invariante al filtro" y ganancia lineal del hablante cercano.

    Se ajusta por minimos cuadrados un FIR no causal ``h`` (``2*taps+1`` coeficientes) tal que
    ``salida ~ h * cercano``. Asi un retardo de 1-2 muestras o un filtrado de color (p. ej. el
    paso alto de Speex) no cuentan como "distorsion": solo lo que NO se explica linealmente por
    la voz cercana (eco residual, artefactos no lineales).

    * ``sdr`` = 10*log10(|h*near|^2 / |out - h*near|^2)
    * ``gain`` = 10*log10(|h*near|^2 / |near|^2)  (0 dB = la voz cercana pasa intacta)
    """
    from scipy.linalg import solve_toeplitz

    pad = 2 * taps
    o = np.pad(out.astype(np.float64), pad)
    n = np.pad(near.astype(np.float64), pad)
    lo, hi = pad, len(n) - pad
    lags = range(-taps, taps + 1)

    def dot_shift(x: np.ndarray, y: np.ndarray, d: int) -> float:  # sum x[n] * y[n - d]
        return float(np.dot(x[lo:hi], y[lo - d : hi - d]))

    r = np.array([dot_shift(n, n, d) for d in range(0, 2 * taps + 1)])
    r[0] *= 1.0 + 1e-9  # regularizacion minima
    p = np.array([dot_shift(o, n, j) for j in lags])
    h = solve_toeplitz(r, p)  # incognitas h_k, k = -taps..taps
    fit = np.zeros(hi - lo)
    for k, hk in zip(lags, h, strict=True):
        fit += hk * n[lo - k : hi - k]
    out_c, near_c = o[lo:hi], n[lo:hi]
    sig = float(np.dot(fit, fit))
    err = float(np.dot(out_c - fit, out_c - fit))
    sdr = 10 * np.log10(sig / (err + 1e-20) + 1e-20)
    return sdr, 10 * np.log10(sig / (np.dot(near_c, near_c) + 1e-20) + 1e-20)


def evaluate(sc: Scenario, out: AudioArray) -> dict[str, float]:
    far5 = [(0, 5)]
    conv = [(5, 15)]
    late = [(35, 50)]
    res: dict[str, float] = {}
    for key, spans in (("erle_first5_db", far5), ("erle_conv_db", conv), ("erle_late_db", late)):
        res[key] = 10 * np.log10(_power(_gather(sc.mic, spans)) / _power(_gather(out, spans)))
    dt = SEGMENTS["double"]
    res["dt_sdr_db"], res["dt_gain_db"] = _sdr_gain(_gather(out, dt), _gather(sc.near, dt))
    no = [(26, 35)]  # se salta 1 s: la cola de la RIR aun suena tras callar el lejano
    res["near_only_sdr_db"], _ = _sdr_gain(_gather(out, no), _gather(sc.near, no))
    return res


def run_canceller(
    process: Callable[[AudioArray, AudioArray, int], AudioArray], sc: Scenario
) -> tuple[AudioArray, float]:
    """Ejecuta ``process`` sobre el escenario y devuelve (salida, x_tiempo_real)."""
    t0 = time.perf_counter()
    out = process(sc.mic, sc.far, SR)
    dt = time.perf_counter() - t0
    assert len(out) == len(sc.mic)
    return out, DURATION / dt


def tts_sources(tmp_dir: Path) -> tuple[AudioArray, AudioArray] | None:
    """(lejano, cercano) con voces SAPI (Zira en ingles, Helena en espanol), ~60 s cada uno."""
    from tests.engines import tts

    es, en = tts.find_voice("C0A"), tts.find_voice("409")
    if es is None or en is None:
        return None
    near_p = tts.synth((tts.SPANISH_TEXT + " ") * 3, tmp_dir / "near_es.wav", es)
    far_p = tts.synth((tts.ENGLISH_TEXT + " ") * 5, tmp_dir / "far_en.wav", en)
    if near_p is None or far_p is None:
        return None
    return tts.load(far_p), tts.load(near_p)


def _norm_words(text: str) -> str:
    import re

    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def asr_metrics(
    sc: Scenario, out: AudioArray, transcribe: Callable[[AudioArray], str]
) -> dict[str, float]:
    """Metricas a nivel de ASR (lo que realmente importa para «Yo»).

    * ``leak_words``: palabras transcritas en los tramos solo-lejano (el micro solo tiene eco:
      lo ideal es 0). ``leak_words_mic``: lo mismo sin AEC (referencia).
    * ``dt_wer_rel``: WER de la salida en doble conversacion frente a la transcripcion de la voz
      cercana limpia en los mismos tramos (incluye las palabras del lejano que se cuelan como
      inserciones y las del cercano que el AEC se come como borrados).
    """
    import jiwer

    far_spans = [(35, 50)]
    dt_spans = SEGMENTS["double"]
    leak = len(_norm_words(transcribe(_gather(out, far_spans))).split())
    leak_mic = len(_norm_words(transcribe(_gather(sc.mic, far_spans))).split())
    ref = _norm_words(" ".join(transcribe(s) for s in _spans(sc.near, dt_spans)))
    hyp = _norm_words(" ".join(transcribe(s) for s in _spans(out, dt_spans)))
    wer = float(jiwer.wer(ref, hyp)) if ref else float("nan")
    return {"leak_words": float(leak), "leak_words_mic": float(leak_mic), "dt_wer_rel": wer}


def _spans(sig: AudioArray, spans: list[tuple[float, float]]) -> list[AudioArray]:
    return [sig[int(a * SR) : int(b * SR)] for a, b in spans]
