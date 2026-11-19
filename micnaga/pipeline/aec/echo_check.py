"""Comprobacion rapida de eco: ¿la pista del sistema se oye en la del micro?

Con auriculares no hay camino acustico del altavoz al micro y el cancelador (Speex, ~2,3 s por
minuto de audio) solo gasta tiempo. Antes de ejecutarlo se mide, sobre las ``WINDOWS`` ventanas
de ``WINDOW_S`` con mas energia del sistema y para unos pocos retardos candidatos (picos de una
correlacion cruzada blanqueada, hasta 0,5 s):

* ``coherence``: **mediana** en 300-3400 Hz de la coherencia espectral (Welch, 256 ms) entre el
  micro y el sistema retrasado, solo en las tramas en las que suena el sistema. La mediana y no
  la media: un zumbido electrico comun a las dos pistas sube la coherencia en unas pocas
  frecuencias, un eco la sube en toda la banda. Sin eco ~0,01-0,05; con eco 0,2-0,9. Solo se
  usa la mitad de esas tramas con menos micro respecto al sistema (en doble habla la voz propia
  tapa el eco y hundiria la coherencia).
* ``echo_db``: potencia del micro que el sistema explica linealmente frente a la voz propia (el
  10 % de tramas mas fuertes del micro). Un eco por debajo de ``ECHO_MIN_DB`` no se oye.

Hay eco si ``coherence >= ECHO_MIN_COHERENCE`` y ``echo_db >= ECHO_MIN_DB``. Sin voz en el
sistema (``active_s`` < ``MIN_ACTIVE_S``) no hay nada que cancelar. Calibrado con la grabacion
real de 47 min con auriculares (coherencia 0,04) y con fugas simuladas sobre ella (0,7-0,9 hasta
-21 dB): ver ``tests/pipeline/test_echo_check.py`` y DECISIONS §23.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from micnaga.types import AudioArray

ECHO_MIN_COHERENCE = 0.1
ECHO_MIN_DB = -30.0
MAX_DELAY_S = 0.5
CANDIDATES = 3
MIN_ACTIVE_S = 3.0
WINDOW_S = 20.0
WINDOWS = 4
BAND_HZ = (300.0, 3400.0)
NPERSEG = 4096
ACTIVE_DBFS = -45.0


@dataclass(frozen=True)
class EchoCheck:
    echo: bool
    coherence: float
    echo_db: float
    """Potencia del eco estimado respecto a la voz propia (dB). Muy negativo = inaudible."""
    delay_ms: float
    active_s: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for k in ("coherence", "echo_db", "delay_ms", "active_s"):
            out[k] = round(float(out[k]), 3)
        return out


def _frames(x: AudioArray, hop: int) -> NDArray[np.float32]:
    n = (len(x) - NPERSEG) // hop + 1
    if n <= 0:
        return np.zeros((0, NPERSEG), dtype=np.float32)
    idx = np.arange(NPERSEG)[None, :] + hop * np.arange(n)[:, None]
    return np.asarray(x[idx], dtype=np.float32)


def _band(sr: int) -> NDArray[np.bool_]:
    freqs = np.fft.rfftfreq(NPERSEG, 1.0 / sr)
    return np.asarray((freqs >= BAND_HZ[0]) & (freqs <= BAND_HZ[1]))


def _coherence(mic: AudioArray, ref: AudioArray, sr: int) -> tuple[float, float]:
    """``(coherencia media, potencia de eco por trama)`` en ``BAND_HZ``, solo con el sistema
    activo. La potencia de eco es la parte del micro que explica linealmente el sistema."""
    n = min(len(mic), len(ref))
    r_frames = _frames(ref[:n], NPERSEG // 2)
    m_frames = _frames(mic[:n], NPERSEG // 2)
    if len(r_frames) < 8:
        return 0.0, 0.0
    level = np.sqrt(np.mean(np.square(r_frames, dtype=np.float64), axis=1))
    active = level > 10 ** (ACTIVE_DBFS / 20)
    if int(active.sum()) < 8:
        return 0.0, 0.0
    # Doble habla: en las tramas en las que el micro va muy por encima del sistema manda la voz
    # propia y la coherencia se hunde aunque haya eco; se usa la mitad con menos micro/sistema.
    m_level = np.mean(np.square(m_frames, dtype=np.float64), axis=1)
    ratio = m_level / np.maximum(level**2, 1e-20)
    active &= ratio <= np.median(ratio[active])
    k = int(active.sum())
    if k < 8:
        return 0.0, 0.0
    win = np.hanning(NPERSEG).astype(np.float32)
    band = _band(sr)
    m = np.fft.rfft(m_frames[active] * win, axis=1)[:, band]
    r = np.fft.rfft(r_frames[active] * win, axis=1)[:, band]
    sxy = np.sum(m * np.conj(r), axis=0)
    sxx = np.sum(np.abs(m) ** 2, axis=0)
    syy = np.sum(np.abs(r) ** 2, axis=0)
    msc = np.abs(sxy) ** 2 / np.maximum(sxx * syy, 1e-20)
    explained = float(np.sum(np.abs(sxy) ** 2 / np.maximum(syy, 1e-20))) / k
    return float(np.median(msc)), explained


def _near_power(mic: AudioArray, sr: int) -> float:
    """Potencia en banda de la voz propia: media del 10 % de tramas mas fuertes del micro."""
    frames = _frames(mic, NPERSEG)
    if len(frames) == 0:
        return 0.0
    win = np.hanning(NPERSEG).astype(np.float32)
    power = np.sum(np.abs(np.fft.rfft(frames * win, axis=1)[:, _band(sr)]) ** 2, axis=1)
    top = np.sort(power)[-max(1, len(power) // 10) :]
    return float(np.mean(top))


def _delay_candidates(mic: AudioArray, ref: AudioArray, sr: int, max_lag: int) -> list[int]:
    """Retardos candidatos (muestras >= 0): picos de una correlacion cruzada blanqueada con
    regularizacion (la PHAT pura amplifica los tramos en silencio y da picos falsos)."""
    nfft = 1 << int(np.ceil(np.log2(len(mic) + len(ref))))
    spec = np.fft.rfft(mic.astype(np.float64), nfft) * np.conj(
        np.fft.rfft(ref.astype(np.float64), nfft)
    )
    mag = np.abs(spec)
    spec /= mag + 0.1 * (float(np.mean(mag)) + 1e-20)
    cc = np.fft.irfft(spec, nfft)[: max_lag + 1]
    order = np.argsort(cc)[::-1]
    picks: list[int] = []
    for i in order:
        if all(abs(int(i) - p) > sr // 100 for p in picks):
            picks.append(int(i))
        if len(picks) == CANDIDATES:
            break
    return picks


def measure_echo(mic: AudioArray, ref: AudioArray, sr: int) -> EchoCheck:
    """Mide si ``ref`` (sistema) se filtra en ``mic``. Barato: unos 80 s de audio como mucho."""
    n = min(len(mic), len(ref))
    mic = np.asarray(mic[:n], dtype=np.float32)
    ref = np.asarray(ref[:n], dtype=np.float32)
    frame = max(1, int(0.05 * sr))
    k = n // frame
    if k == 0:
        return EchoCheck(False, 0.0, -99.0, 0.0, 0.0, "no audio")
    rms = np.sqrt(np.mean(np.square(ref[: k * frame].reshape(k, frame), dtype=np.float64), axis=1))
    active_s = float(np.count_nonzero(rms > 10 ** (ACTIVE_DBFS / 20)) * frame / sr)
    if active_s < MIN_ACTIVE_S:
        return EchoCheck(False, 0.0, -99.0, 0.0, active_s, "no system audio")
    win = int(WINDOW_S * sr)
    max_lag = int(MAX_DELAY_S * sr)
    starts = list(range(0, max(1, n - win + 1), max(1, win)))
    energies = [float(np.sum(np.square(ref[s : s + win], dtype=np.float64))) for s in starts]
    best, best_power, best_delay = 0.0, 0.0, 0
    for i in np.argsort(energies)[::-1][:WINDOWS]:
        s = starts[int(i)]
        m, r = mic[s : s + win], ref[s : s + win]
        if float(np.sum(np.square(r, dtype=np.float64))) < 1e-9:
            continue
        for d in sorted({0, *_delay_candidates(m, r, sr, max_lag)}):
            c, power = _coherence(m[d:], r[: len(r) - d], sr)
            if c > best:
                best, best_power, best_delay = c, power, d
    near = _near_power(mic, sr)
    echo_db = 10 * float(np.log10(max(best_power, 1e-20) / max(near, 1e-20)))
    echo = best >= ECHO_MIN_COHERENCE and echo_db >= ECHO_MIN_DB
    reason = "echo" if echo else "negligible echo"
    return EchoCheck(echo, best, echo_db, best_delay * 1000.0 / sr, active_s, reason)


__all__ = ["ECHO_MIN_COHERENCE", "EchoCheck", "measure_echo"]
