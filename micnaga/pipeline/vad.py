"""Etapa 5: deteccion de voz con Silero VAD (ONNX con onnxruntime, sin torch).

El modelo ``silero_vad.onnx`` (v6, entradas ``input[batch, 64+512]``, ``state[2, batch, 128]``,
``sr`` int64; salidas ``output[batch, 1]`` y ``stateN``) se carga directamente: **no** se importa
el paquete ``silero_vad`` (arrastra torch). Se procesan tramas de 512 muestras a 16 kHz (32 ms)
con 64 muestras de contexto de la trama anterior, como hace la implementacion de referencia.

La logica de post-proceso (minimos de voz/silencio, margen, division de tramos largos y
agrupacion en bloques) esta separada del modelo (``regions_from_probs`` / ``group_regions``)
para poder probarla con probabilidades sinteticas.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import numpy as np
from numpy.typing import NDArray

from micnaga import models
from micnaga.registry import EngineUnavailableError
from micnaga.types import AudioArray, SpeechRegion

if TYPE_CHECKING:
    from micnaga.config import Config

log = logging.getLogger(__name__)

MODEL_SR = 16000
FRAME = 512  # muestras por trama a 16 kHz (32 ms)
CONTEXT = 64
FRAME_S = FRAME / MODEL_SR

DEFAULT_MIN_SPEECH_MS = 250
DEFAULT_MIN_SILENCE_MS = 500
DEFAULT_PAD_MS = 200
DEFAULT_MAX_BLOCK_S = 30.0
DEFAULT_THRESHOLD = 0.5
NEG_THRESHOLD_DELTA = 0.15
MAX_GAP_S = 1.0
"""Silencio maximo entre dos tramos de voz dentro de un bloque una vez acortado
(``s06_asr.compact``); ``group_regions`` cuenta cada silencio como mucho esto."""


@dataclass(frozen=True)
class VadCarry:
    """Estado de Silero al final de un trozo (para seguir en el siguiente)."""

    state: NDArray[np.float32]
    context: NDArray[np.float32]


class ProbModel(Protocol):
    """Cualquier cosa que devuelva una probabilidad de voz por trama de 32 ms."""

    def probs(self, audio_16k: AudioArray) -> NDArray[np.float32]: ...


class SileroVad:
    """Silero VAD ONNX con estado recurrente."""

    def __init__(self, model_path: Path, threads: int = 1) -> None:
        import onnxruntime as ort

        if not model_path.is_file():
            raise EngineUnavailableError(
                f"Silero VAD model not found at {model_path}; run the first-start model download "
                "(micnaga.models.download_models)."
            )
        from micnaga.pipeline.engines.ort_options import session_options

        so = session_options(threads)
        try:
            self._sess = ort.InferenceSession(
                str(model_path), sess_options=so, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:  # modelo corrupto, etc.
            raise EngineUnavailableError(f"cannot load Silero VAD ({model_path}): {exc}") from exc
        self._sr = np.array(MODEL_SR, dtype=np.int64)

    def probs(self, audio_16k: AudioArray) -> NDArray[np.float32]:
        """Probabilidad de voz de cada trama de 512 muestras (la ultima se rellena con ceros)."""
        return self.probs_from(audio_16k, None)[0]

    def probs_from(
        self, audio_16k: AudioArray, carry: VadCarry | None
    ) -> tuple[NDArray[np.float32], VadCarry]:
        """Como ``probs`` pero continuando el estado recurrente de un trozo anterior.

        Trocear el audio en multiplos de 512 muestras y encadenar ``carry`` da exactamente las
        mismas probabilidades que procesarlo de una vez (lo usa el procesado en vivo).
        """
        x = np.ascontiguousarray(audio_16k, dtype=np.float32)
        state = np.zeros((2, 1, 128), dtype=np.float32) if carry is None else carry.state.copy()
        context = np.zeros(CONTEXT, dtype=np.float32) if carry is None else carry.context.copy()
        n_frames = -(-len(x) // FRAME)
        if n_frames == 0:
            return np.zeros(0, dtype=np.float32), VadCarry(state, context)
        padded = np.zeros(n_frames * FRAME, dtype=np.float32)
        padded[: len(x)] = x
        out = np.empty(n_frames, dtype=np.float32)
        buf = np.empty((1, CONTEXT + FRAME), dtype=np.float32)
        for i in range(n_frames):
            chunk = padded[i * FRAME : (i + 1) * FRAME]
            buf[0, :CONTEXT] = context
            buf[0, CONTEXT:] = chunk
            prob, state = self._sess.run(None, {"input": buf, "state": state, "sr": self._sr})
            out[i] = float(prob[0, 0])
            context = chunk[-CONTEXT:].copy()
        return out, VadCarry(np.asarray(state, dtype=np.float32), context)

    def close(self) -> None:
        self._sess = None


_cache_lock = threading.Lock()
_cache: dict[str, SileroVad] = {}


def load_model(config: Config | None = None) -> SileroVad:
    """Instancia (cacheada por ruta) del Silero VAD de la carpeta de modelos."""
    path = models.group_dir(models.SILERO_GROUP, config) / "silero_vad.onnx"
    key = str(path)
    with _cache_lock:
        mdl = _cache.get(key)
        if mdl is None:
            mdl = SileroVad(path)
            _cache.clear()  # un solo modelo VAD a la vez
            _cache[key] = mdl
        return mdl


def release_model() -> None:
    """Libera el VAD cacheado (llamar entre etapas)."""
    with _cache_lock:
        for m in _cache.values():
            m.close()
        _cache.clear()


# ------------------------------------------------------------------ post-proceso (puro)


def regions_from_probs(
    probs: Sequence[float] | NDArray[np.float32],
    duration_s: float,
    *,
    frame_s: float = FRAME_S,
    min_speech_ms: int = DEFAULT_MIN_SPEECH_MS,
    min_silence_ms: int = DEFAULT_MIN_SILENCE_MS,
    pad_ms: int = DEFAULT_PAD_MS,
    max_speech_s: float | None = DEFAULT_MAX_BLOCK_S,
    threshold: float = DEFAULT_THRESHOLD,
    neg_threshold: float | None = None,
) -> list[SpeechRegion]:
    """Convierte probabilidades por trama en tramos de voz.

    * Se entra en voz con ``prob >= threshold`` y se sale tras ``min_silence_ms`` seguidos con
      ``prob < neg_threshold`` (``threshold - 0.15`` por defecto: histeresis).
    * Se descartan tramos con menos de ``min_speech_ms`` de voz.
    * Cada tramo se ensancha ``pad_ms`` por cada lado (acotado a ``[0, duration_s]``); los que
      llegan a solaparse se funden.
    * Los tramos mas largos que ``max_speech_s`` se parten por el valle de probabilidad mas
      profundo de su ultimo cuarto (o por la mitad si no hay valle).
    """
    p = np.asarray(probs, dtype=np.float32)
    neg = neg_threshold if neg_threshold is not None else max(threshold - NEG_THRESHOLD_DELTA, 0.01)
    min_speech = min_speech_ms / 1000.0
    min_silence = min_silence_ms / 1000.0

    raw: list[tuple[int, int]] = []  # [primera trama de voz, ultima trama de voz + 1)
    start: int | None = None
    silence_from: int | None = None
    for i, v in enumerate(p):
        if v >= threshold:
            if start is None:
                start = i
            silence_from = None
        elif start is not None and v < neg:
            if silence_from is None:
                silence_from = i
            if (i + 1 - silence_from) * frame_s >= min_silence:
                raw.append((start, silence_from))
                start, silence_from = None, None
    if start is not None:
        raw.append((start, silence_from if silence_from is not None else len(p)))

    padded: list[list[float]] = []
    pad = pad_ms / 1000.0
    for a, b in raw:
        s, e = a * frame_s, b * frame_s
        if e - s < min_speech:
            continue
        s = max(0.0, s - pad)
        e = min(duration_s, e + pad) if duration_s > 0 else e + pad
        if padded and s <= padded[-1][1]:
            padded[-1][1] = max(padded[-1][1], e)
        else:
            padded.append([s, e])

    out: list[SpeechRegion] = []
    for s, e in padded:
        pieces = _split_long(s, e, p, frame_s, max_speech_s) if max_speech_s else [(s, e)]
        out.extend(SpeechRegion(a, b) for a, b in pieces)
    return out


def _split_long(
    s: float, e: float, probs: NDArray[np.float32], frame_s: float, max_len: float
) -> list[tuple[float, float]]:
    """Parte ``[s, e]`` en trozos <= ``max_len`` cortando por valles de probabilidad."""
    pieces: list[tuple[float, float]] = []
    cur = s
    while e - cur > max_len + 1e-9:
        lo, hi = cur + 0.75 * max_len, cur + max_len
        i0, i1 = int(lo / frame_s), min(int(hi / frame_s), len(probs))
        valley = (i0 + int(np.argmin(probs[i0:i1]))) * frame_s if i1 > i0 else cur + max_len / 2
        cut = valley
        cut = min(max(cut, cur + 0.5 * max_len), cur + max_len)
        pieces.append((cur, cut))
        cur = cut
    pieces.append((cur, e))
    return pieces


def group_regions(
    regions: Sequence[SpeechRegion],
    max_len_s: float = DEFAULT_MAX_BLOCK_S,
    *,
    max_gap_s: float | None = MAX_GAP_S,
) -> list[tuple[float, float]]:
    """Agrupa tramos consecutivos en bloques ``(inicio, fin)`` de como mucho ``max_len_s``.

    Un bloque va desde el inicio del primer tramo hasta el fin del ultimo (incluye los
    silencios intermedios). Un tramo que por si solo supera ``max_len_s`` se parte en trozos
    iguales (defensa: ``regions_from_probs`` ya los parte por valles).

    Cada silencio intermedio cuenta como mucho ``max_gap_s`` (``None``: entero): es lo que dura
    el bloque una vez acortados sus silencios (``s06_asr.compact``, que usa el mismo valor), asi
    que el audio que llega al ASR sigue sin pasar de ``max_len_s`` aunque el bloque abarque mas
    tiempo de la pista. Whisper cobra cada ventana de 30 s entera, este o no llena: llenarlas de
    voz ahorra ventanas (DECISIONS §32).
    """
    if max_len_s <= 0:
        raise ValueError("max_len_s must be > 0")
    blocks: list[tuple[float, float]] = []
    cur: tuple[float, float] | None = None
    cost = 0.0  # duracion del bloque con los silencios acotados a ``max_gap_s``
    for r in sorted(regions, key=lambda r: r.start):
        pieces = [(r.start, r.end)]
        if r.end - r.start > max_len_s:
            n = int(np.ceil((r.end - r.start) / max_len_s))
            step = (r.end - r.start) / n
            pieces = [(r.start + k * step, r.start + (k + 1) * step) for k in range(n)]
        for a, b in pieces:
            if cur is None:
                cur, cost = (a, b), b - a
                continue
            gap = max(0.0, a - cur[1])
            if max_gap_s is not None:
                gap = min(gap, max_gap_s)
            extra = gap + max(0.0, b - max(a, cur[1]))
            if cost + extra <= max_len_s + 1e-9:
                cur, cost = (cur[0], max(cur[1], b)), cost + extra
            else:
                blocks.append(cur)
                cur, cost = (a, b), b - a
    if cur is not None:
        blocks.append(cur)
    return blocks


def _to_16k(audio: AudioArray, sr: int) -> AudioArray:
    if sr == MODEL_SR:
        return np.ascontiguousarray(audio, dtype=np.float32)
    import soxr

    return np.asarray(soxr.resample(np.asarray(audio, dtype=np.float32), sr, MODEL_SR), np.float32)


@dataclass(frozen=True)
class RegionParams:
    """Parametros del post-proceso (``regions_from_probs``) ya resueltos."""

    min_speech_ms: int
    min_silence_ms: int
    pad_ms: int
    max_speech_s: float | None
    threshold: float = DEFAULT_THRESHOLD

    @property
    def stable_guard_s(self) -> float:
        """Un tramo que acaba al menos esto antes del final del audio ya no puede cambiar."""
        return (self.min_silence_ms + 3 * self.pad_ms) / 1000.0 + 2 * FRAME_S + 0.5


def region_params(
    config: Config | None = None,
    min_speech_ms: int | None = None,
    min_silence_ms: int | None = None,
    pad_ms: int | None = None,
    max_speech_s: float | None = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> RegionParams:
    """Argumento explicito > ``config.vad`` > valores de la spec (250/500/200 ms, 30 s)."""
    cfg = config.vad if config is not None else None
    mv = (
        min_speech_ms
        if min_speech_ms is not None
        else (cfg.min_speech_ms if cfg else DEFAULT_MIN_SPEECH_MS)
    )
    ms = (
        min_silence_ms
        if min_silence_ms is not None
        else (cfg.min_silence_ms if cfg else DEFAULT_MIN_SILENCE_MS)
    )
    pd = pad_ms if pad_ms is not None else (cfg.pad_ms if cfg else DEFAULT_PAD_MS)
    mx = (
        max_speech_s
        if max_speech_s is not None
        else (cfg.max_block_s if cfg else DEFAULT_MAX_BLOCK_S)
    )
    return RegionParams(mv, ms, pd, mx, threshold)


def regions_with(
    probs: Sequence[float] | NDArray[np.float32], duration_s: float, params: RegionParams
) -> list[SpeechRegion]:
    return regions_from_probs(
        probs,
        duration_s,
        min_speech_ms=params.min_speech_ms,
        min_silence_ms=params.min_silence_ms,
        pad_ms=params.pad_ms,
        max_speech_s=params.max_speech_s,
        threshold=params.threshold,
    )


def speech_probs(
    audio: AudioArray, sr: int, *, model: ProbModel | None = None, config: Config | None = None
) -> NDArray[np.float32]:
    """Probabilidad de voz por trama de 32 ms (Silero, o ``model`` si se inyecta)."""
    if audio.ndim != 1:
        raise ValueError("audio must be mono (1-D)")
    x = _to_16k(audio, sr)
    if len(x) == 0:
        return np.zeros(0, dtype=np.float32)
    mdl: ProbModel = model if model is not None else load_model(config)
    return mdl.probs(x)


def detect_speech(
    audio: AudioArray,
    sr: int,
    min_speech_ms: int | None = None,
    min_silence_ms: int | None = None,
    pad_ms: int | None = None,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_speech_s: float | None = None,
    model: ProbModel | None = None,
    config: Config | None = None,
    probs: NDArray[np.float32] | None = None,
) -> list[SpeechRegion]:
    """Tramos de voz de ``audio`` (segundos relativos al inicio del array).

    Los parametros salen, por orden: argumento explicito > ``config.vad`` > valores de la
    spec (250 ms / 500 ms / 200 ms, bloques de 30 s). ``model`` permite inyectar un modelo
    falso en los tests; por defecto se carga (y cachea) el Silero ONNX de la carpeta de modelos.
    ``probs`` (ya calculadas sobre este mismo audio) evita volver a pasar el modelo.
    """
    params = region_params(config, min_speech_ms, min_silence_ms, pad_ms, max_speech_s, threshold)
    if audio.ndim != 1:
        raise ValueError("audio must be mono (1-D)")
    x = _to_16k(audio, sr)
    if len(x) == 0:
        return []
    if probs is None:
        mdl: ProbModel = model if model is not None else load_model(config)
        probs = mdl.probs(x)
    return regions_with(probs, len(x) / MODEL_SR, params)


def speech_seconds(regions: Sequence[SpeechRegion]) -> float:
    """Suma de duraciones (util para la compuerta 2: voz remota >= 30 s)."""
    return float(sum(r.duration for r in regions))


__all__ = [
    "RegionParams",
    "SileroVad",
    "VadCarry",
    "detect_speech",
    "group_regions",
    "load_model",
    "region_params",
    "regions_from_probs",
    "regions_with",
    "release_model",
    "speech_probs",
    "speech_seconds",
]
