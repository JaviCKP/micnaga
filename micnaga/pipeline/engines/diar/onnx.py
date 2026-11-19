"""Diarizador sin torch: Silero VAD + WeSpeaker ResNet34-LM (ONNX) + clustering aglomerativo.

Solo ``numpy``, ``scipy`` y ``onnxruntime`` (nada de torch, torchaudio, scikit-learn,
``diarize``, ``wespeakerruntime`` ni ``kaldiio``). Pipeline:

1. Tramos de voz con nuestro Silero VAD (``micnaga.pipeline.vad.detect_speech``).
2. Ventanas deslizantes de ``WINDOW_S`` s con salto ``HOP_S`` s dentro de cada tramo; el fbank
   kaldi de 80 dim (25/10 ms, hamming, preenfasis 0,97, FFT 512, 20 Hz-Nyquist, log) se calcula
   **una vez por tramo** en numpy y cada ventana es un corte de esas tramas (con CMN propio),
   identico a calcularlo sobre el audio de la ventana (las ventanas empiezan en multiplos de 10 ms).
3. Embeddings WeSpeaker por lotes (mismo ONNX y mismo espacio que el adaptador ``diarize``).
4. Clustering aglomerativo (enlace medio, coseno, ``scipy.cluster.hierarchy``) con umbral de
   distancia, o cortado a ``n_speakers``. Si hay demasiadas ventanas se agrupa una submuestra y el
   resto se asigna al centroide mas cercano. Despues: reasignacion al centroide, fusion de
   clusters pequenos parecidos y suavizado de ventanas aisladas.
5. Etiquetas por ventana -> turnos (cada ventana "posee" desde el punto medio con la anterior
   hasta el punto medio con la siguiente = resolucion de solapes por punto medio; en un cambio de
   hablante la frontera se mueve al instante mas silencioso entre los dos centros), fusion de
   turnos contiguos del mismo hablante y renumeracion ``S1..Sn`` por primera aparicion.

``embed`` (``SpeakerEmbedder``, MIC-11) devuelve por hablante la media L2-normalizada de embeddings
de ventanas de 3 s, igual que el adaptador ``diarize``: las huellas son intercambiables.

Los umbrales se calibraron SOLO con voz sintetica (ver ``docs/diarization_onnx.md``).
"""

from __future__ import annotations

import gc
import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

from micnaga import models
from micnaga.pipeline import vad
from micnaga.pipeline.engines.asr._common import MODEL_SR, to_mono_16k
from micnaga.registry import EngineUnavailableError
from micnaga.types import AudioArray, Turn

if TYPE_CHECKING:
    from micnaga.config import Config

log = logging.getLogger(__name__)

WESPEAKER_FILE = "voxceleb_resnet34_LM.onnx"
EMBED_DIM = 256

# ---- fbank (kaldi) ----
N_MELS = 80
FRAME_LEN = 400  # 25 ms @ 16 kHz
FRAME_SHIFT = 160  # 10 ms
N_FFT = 512
PREEMPH = 0.97
LOW_FREQ = 20.0
_EPS = float(np.finfo(np.float32).eps)

# ---- ventanas / clustering (calibrado con TTS; reevaluar con voces reales) ----
WINDOW_S = 1.5
HOP_S = 0.75
MIN_WINDOW_S = 0.5  # un tramo mas corto no genera embedding (se pega al vecino)
DEFAULT_MAX_SPEAKERS = 8
DEFAULT_THRESHOLD = 0.60  # distancia coseno media (enlace medio) a la que se dejan de fundir
CENTROID_MERGE_SIM = (
    0.55  # tras el corte, se funden clusters cuyos centroides tienen coseno >= esto
)
SMALL_CLUSTER_S = 6.0  # un cluster con menos voz se funde en el mas parecido si coseno >= SMALL_SIM
SMALL_SIM = 0.30
MERGE_GAP_S = 0.5
MAX_CLUSTER_POINTS = 3000  # por encima se agrupa una submuestra (memoria O(n^2))
BATCH = 32
EMBED_WINDOW_S = 3.0
EMBED_MAX_WINDOWS = 20
EMBED_MIN_S = 0.8
ENERGY_FRAME_S = 0.01  # tramas para colocar las fronteras de turno en la pausa mas cercana


# ======================================================================== fbank


def _mel(freq: NDArray[np.float64]) -> NDArray[np.float64]:
    return 1127.0 * np.log(1.0 + freq / 700.0)


_BANK: NDArray[np.float32] | None = None
_WINDOW: NDArray[np.float64] | None = None


def mel_banks() -> NDArray[np.float32]:
    """Banco de filtros triangulares de kaldi, ``(80, 257)`` (la columna 256 = Nyquist es 0)."""
    global _BANK
    if _BANK is None:
        nyquist = MODEL_SR / 2
        mel_low = float(_mel(np.array(LOW_FREQ)))
        mel_high = float(_mel(np.array(nyquist)))
        delta = (mel_high - mel_low) / (N_MELS + 1)
        b = np.arange(N_MELS, dtype=np.float64)[:, None]
        left, center, right = (mel_low + (b + k) * delta for k in (0.0, 1.0, 2.0))
        mel = _mel(np.arange(N_FFT // 2, dtype=np.float64) * (MODEL_SR / N_FFT))[None, :]
        bank = np.maximum(
            0.0, np.minimum((mel - left) / (center - left), (right - mel) / (right - center))
        )
        _BANK = np.pad(bank, ((0, 0), (0, 1))).astype(np.float32)
    return _BANK


def fbank(audio: AudioArray) -> NDArray[np.float32]:
    """Log-fbank kaldi de 80 dim sobre ``audio`` mono 16 kHz float [-1, 1] (sin CMN).

    Equivale a ``torchaudio.compliance.kaldi.fbank(x * 32768, num_mel_bins=80, dither=0,
    window_type="hamming", use_energy=False)`` (snip_edges, preenfasis 0,97, quita DC por trama).
    Devuelve ``(n_frames, 80)``; vacio si hay menos de 400 muestras.
    """
    global _WINDOW
    x = np.asarray(audio, dtype=np.float32) * np.float32(1 << 15)
    if len(x) < FRAME_LEN:
        return np.zeros((0, N_MELS), dtype=np.float32)
    n = 1 + (len(x) - FRAME_LEN) // FRAME_SHIFT
    frames = np.lib.stride_tricks.sliding_window_view(x, FRAME_LEN)[::FRAME_SHIFT][:n]
    frames = frames - frames.mean(axis=1, keepdims=True)
    pre = np.empty_like(frames)
    pre[:, 0] = frames[:, 0] * (1.0 - PREEMPH)  # replicate padding del primer elemento
    pre[:, 1:] = frames[:, 1:] - PREEMPH * frames[:, :-1]
    if _WINDOW is None:
        _WINDOW = np.hamming(FRAME_LEN)  # hamming simetrico = alpha .54, beta .46, periodic=False
    pre *= _WINDOW.astype(np.float32)
    spec = np.fft.rfft(pre, n=N_FFT, axis=1)
    power = (spec.real**2 + spec.imag**2).astype(np.float32)
    out = np.log(np.maximum(power @ mel_banks().T, np.float32(_EPS)))
    return np.ascontiguousarray(out, dtype=np.float32)


# ===================================================================== clustering


def _unit(x: NDArray[Any]) -> NDArray[np.float32]:
    a = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(a, axis=-1, keepdims=True)
    return np.asarray(a / np.maximum(n, 1e-12), dtype=np.float32)


def _centroids(emb: NDArray[np.float32], labels: NDArray[np.int64], k: int) -> NDArray[np.float32]:
    cent = np.zeros((k, emb.shape[1]), dtype=np.float32)
    for c in range(k):
        m = labels == c
        if m.any():
            cent[c] = emb[m].mean(axis=0)
    return _unit(cent)


def _relabel(labels: NDArray[np.int64]) -> NDArray[np.int64]:
    """Etiquetas contiguas ``0..k-1`` por primera aparicion."""
    _, first, inv = np.unique(labels, return_index=True, return_inverse=True)
    rank = np.empty(len(first), dtype=np.int64)
    rank[np.argsort(first)] = np.arange(len(first))
    return np.asarray(rank[inv], dtype=np.int64)


def cluster_embeddings(
    emb: NDArray[np.float32],
    n_speakers: int | None = None,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_speakers: int = DEFAULT_MAX_SPEAKERS,
    weights_s: float = HOP_S,
    centroid_merge_sim: float = CENTROID_MERGE_SIM,
    small_cluster_s: float = SMALL_CLUSTER_S,
    small_sim: float = SMALL_SIM,
    max_points: int = MAX_CLUSTER_POINTS,
    refine_iters: int = 3,
) -> NDArray[np.int64]:
    """Etiqueta cada fila de ``emb`` (``(n, d)``, se normaliza aqui) con un cluster ``0..k-1``.

    * ``n_speakers=None``: enlace medio + coseno cortado a distancia ``threshold`` (con tope
      ``max_speakers``); fusion de clusters cuyos centroides tienen coseno >=
      ``centroid_merge_sim`` (el enlace medio deja sueltas variantes de una misma voz); y fusion de
      clusters con menos de ``small_cluster_s`` s de voz
      (``weights_s`` s por ventana) en el mas parecido si su coseno >= ``small_sim``.
    * ``n_speakers=k``: se corta el arbol a ``min(k, n)`` clusters (sin fusiones).
    Las etiquetas salen renumeradas por primera aparicion.
    """
    from scipy.cluster.hierarchy import fcluster, linkage

    x = _unit(emb)
    n = len(x)
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    if n == 1 or (n_speakers is not None and n_speakers <= 1):
        return np.zeros(n, dtype=np.int64)

    # submuestra uniforme si hay demasiadas ventanas (pdist es O(n^2) en memoria)
    sub = np.arange(n) if n <= max_points else np.linspace(0, n - 1, max_points).astype(int)
    z = linkage(x[sub].astype(np.float64), method="average", metric="cosine")
    if n_speakers is not None:
        lab_sub = fcluster(z, t=min(n_speakers, len(sub)), criterion="maxclust")
    else:
        lab_sub = fcluster(z, t=threshold, criterion="distance")
        if lab_sub.max() > max_speakers:
            lab_sub = fcluster(z, t=max_speakers, criterion="maxclust")
    lab_sub = _relabel(np.asarray(lab_sub - 1, dtype=np.int64))
    k = int(lab_sub.max()) + 1
    cent = _centroids(x[sub], lab_sub, k)
    labels = np.argmax(x @ cent.T, axis=1).astype(np.int64)

    for _ in range(refine_iters if k > 1 else 0):  # k-means coseno corto: limpia ventanas ruidosas
        present = np.unique(labels)
        cent = _centroids(x, labels, k)[present]
        new = present[np.argmax(x @ cent.T, axis=1)]
        if np.array_equal(new, labels):
            break
        labels = np.asarray(new, dtype=np.int64)

    if n_speakers is None:
        labels = _merge_close(x, labels, centroid_merge_sim)
        labels = _merge_small(x, labels, weights_s, small_cluster_s, small_sim)
    return _relabel(labels)


def _merge_close(
    x: NDArray[np.float32], labels: NDArray[np.int64], min_sim: float
) -> NDArray[np.int64]:
    """Funde el par de clusters mas parecidos (centroides) mientras su coseno sea >= ``min_sim``."""
    labels = labels.copy()
    while True:
        ids = [int(c) for c in np.unique(labels)]
        if len(ids) < 2:
            return labels
        cent = _unit(np.stack([x[labels == c].mean(axis=0) for c in ids]))
        sim = cent @ cent.T
        np.fill_diagonal(sim, -1.0)
        i, j = np.unravel_index(int(np.argmax(sim)), sim.shape)
        if sim[i, j] < min_sim:
            return labels
        labels[labels == ids[int(j)]] = ids[int(i)]


def _merge_small(
    x: NDArray[np.float32],
    labels: NDArray[np.int64],
    window_s: float,
    small_s: float,
    small_sim: float,
) -> NDArray[np.int64]:
    labels = labels.copy()
    while True:
        ids = [int(c) for c in np.unique(labels)]
        if len(ids) < 2:
            return labels
        counts = {c: int((labels == c).sum()) * window_s for c in ids}
        cent = {c: _unit(x[labels == c].mean(axis=0)) for c in ids}
        merged = False
        for c in sorted((c for c in ids if counts[c] < small_s), key=lambda c: counts[c]):
            sims = {o: float(cent[c] @ cent[o]) for o in ids if o != c}
            best = max(sims, key=lambda o: sims[o])
            if (
                sims[best] >= small_sim
            ):  # si no se parece a nadie, es un hablante real que habla poco
                labels[labels == c] = best
                merged = True
                break
        if not merged:
            return labels


# ============================================================ ventanas -> turnos


def window_starts(length_s: float, window_s: float = WINDOW_S, hop_s: float = HOP_S) -> list[float]:
    """Inicios (s, multiplos de 10 ms) de las ventanas de un tramo de ``length_s`` s.

    Un tramo mas corto que la ventana da una sola ventana de todo el tramo (inicio 0). Si el
    ultimo salto deja mas de 0,2 s sin cubrir se anade una ventana alineada al final.
    """
    if length_s <= window_s + 1e-9:
        return [0.0]
    last = length_s - window_s
    starts = [round(i * hop_s, 2) for i in range(int(last / hop_s + 1e-9) + 1)]
    if last - starts[-1] > 0.2:
        starts.append(round(np.floor(last * 100) / 100, 2))
    return starts


def frame_energy(audio: AudioArray) -> NDArray[np.float32]:
    """Energia en dB por trama de 10 ms (``ENERGY_FRAME_S``) de ``audio`` mono 16 kHz."""
    step = int(ENERGY_FRAME_S * MODEL_SR)
    n = len(audio) // step
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    x = np.asarray(audio[: n * step], dtype=np.float32).reshape(n, step)
    power = np.mean(np.square(x, dtype=np.float32), axis=1)
    return np.asarray(10.0 * np.log10(power + 1e-10), dtype=np.float32)


def quietest_point(energy: NDArray[np.float32], a: float, b: float) -> float:
    """Instante (s) de menor energia (media movil de 50 ms) entre ``a`` y ``b``.

    Si no hay tramas en el intervalo devuelve el punto medio.
    """
    f0 = max(0, int(np.ceil(a / ENERGY_FRAME_S)))
    f1 = min(len(energy), int(b / ENERGY_FRAME_S))
    if f1 - f0 < 1:
        return (a + b) / 2
    k = 5
    pad = np.pad(energy, (k // 2, k // 2), mode="edge")
    smooth = np.convolve(pad, np.ones(k, dtype=np.float32) / k, mode="valid")
    return (f0 + int(np.argmin(smooth[f0:f1])) + 0.5) * ENERGY_FRAME_S


def turns_from_windows(
    spans: Sequence[tuple[float, float]],
    labels: Sequence[int],
    regions: Sequence[tuple[float, float]],
    region_of: Sequence[int],
    merge_gap_s: float = MERGE_GAP_S,
    energy: NDArray[np.float32] | None = None,
) -> list[tuple[float, float, int]]:
    """Convierte etiquetas por ventana en segmentos ``(inicio, fin, etiqueta)`` sin solapes.

    Cada ventana abarca desde el punto medio entre su centro y el de la anterior hasta el punto
    medio con la siguiente (solapes resueltos por punto medio); la primera y la ultima de cada
    tramo llegan al borde del tramo. Con ``energy`` (``frame_energy`` del audio), la frontera
    entre dos ventanas de **distinta** etiqueta se mueve al instante mas silencioso entre sus
    centros: la gente cambia de turno en una pausa, y el punto medio puede caer hasta ~0,4 s
    dentro de la frase de uno de los dos. Los segmentos contiguos con la misma etiqueta (hueco <=
    ``merge_gap_s``) se funden. ``region_of[i]`` = tramo de la ventana ``i``.
    """
    segs: list[tuple[float, float, int]] = []
    n = len(spans)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and region_of[j + 1] == region_of[i]:
            j += 1
        r0, r1 = regions[region_of[i]]
        centers = [(spans[k][0] + spans[k][1]) / 2 for k in range(i, j + 1)]
        bounds = [r0]
        for idx in range(len(centers) - 1):
            c0, c1 = centers[idx], centers[idx + 1]
            if energy is not None and labels[i + idx] != labels[i + idx + 1]:
                bounds.append(quietest_point(energy, c0, c1))
            else:
                bounds.append((c0 + c1) / 2)
        bounds.append(r1)
        for idx, k in enumerate(range(i, j + 1)):
            a, b = bounds[idx], bounds[idx + 1]
            if b > a:
                segs.append((a, b, int(labels[k])))
        i = j + 1
    segs.sort()
    merged: list[tuple[float, float, int]] = []
    for a, b, lab in segs:
        if merged and merged[-1][2] == lab and a - merged[-1][1] <= merge_gap_s:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b), lab)
        else:
            merged.append((a, b, lab))
    return merged


def smooth_isolated(labels: NDArray[np.int64], region_of: Sequence[int]) -> NDArray[np.int64]:
    """Una ventana cuyas dos vecinas (del mismo tramo) coinciden entre si adopta su etiqueta."""
    out = labels.copy()
    for i in range(1, len(labels) - 1):
        if (
            region_of[i - 1] == region_of[i] == region_of[i + 1]
            and labels[i - 1] == labels[i + 1] != labels[i]
        ):
            out[i] = labels[i - 1]
    return out


def to_turns(segs: Sequence[tuple[float, float, int]]) -> list[Turn]:
    """``(inicio, fin, etiqueta)`` -> ``Turn`` con ``S1..Sn`` por primera aparicion."""
    mapping: dict[int, str] = {}
    out: list[Turn] = []
    for a, b, lab in sorted(segs, key=lambda s: (s[0], s[1])):
        sp = mapping.setdefault(lab, f"S{len(mapping) + 1}")
        out.append(Turn(speaker=sp, start=float(a), end=float(b)))
    return out


# ========================================================================= motor


class OnnxDiarizer:
    """``Diarizer`` + ``SpeakerEmbedder`` sin torch."""

    name = "onnx-wespeaker-resnet34-lm"

    def __init__(
        self,
        onnx_path: Path,
        config: Config | None = None,
        threads: int = 2,
        threshold: float = DEFAULT_THRESHOLD,
        max_speakers: int = DEFAULT_MAX_SPEAKERS,
        vad_model: vad.ProbModel | None = None,
    ) -> None:
        import onnxruntime as ort

        if not onnx_path.is_file():
            raise EngineUnavailableError(
                f"WeSpeaker model not found at {onnx_path}; run the first-start model download "
                "(micnaga.models.download_models)."
            )
        from micnaga.pipeline.engines.ort_options import session_options

        so = session_options(threads)
        try:
            self._sess: Any = ort.InferenceSession(
                str(onnx_path), sess_options=so, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:
            raise EngineUnavailableError(f"cannot load WeSpeaker ({onnx_path}): {exc}") from exc
        self._config = config
        self._threshold = threshold
        self._max_speakers = max(1, max_speakers)
        self._vad_model = vad_model
        self._closed = False

    # -------------------------------------------------------------- embeddings

    def _run(self, feats: NDArray[np.float32]) -> NDArray[np.float32]:
        """``(B, T, 80)`` ya con CMN -> ``(B, 256)`` L2-normalizados."""
        out = self._sess.run(["embs"], {"feats": feats})[0]
        return _unit(np.asarray(out, dtype=np.float32))

    def embed_windows(
        self, audio: AudioArray, regions: Sequence[tuple[float, float]], offset: int = 0
    ) -> tuple[NDArray[np.float32], list[tuple[float, float]], list[int]]:
        """Embeddings de las ventanas de todos los tramos (fbank una vez por tramo).

        ``audio`` empieza en la muestra ``offset`` de la pista (los tiempos de ``regions`` y de
        las ventanas son de la pista): el procesado en vivo pasa solo el trozo de cada tramo.
        """
        spans: list[tuple[float, float]] = []
        region_of: list[int] = []
        vecs: list[NDArray[np.float32]] = []
        pending: list[NDArray[np.float32]] = []
        win_frames = 1 + (int(WINDOW_S * MODEL_SR) - FRAME_LEN) // FRAME_SHIFT

        def flush() -> None:
            if pending:
                vecs.append(self._run(np.stack(pending)))
                pending.clear()

        for ri, (r0, r1) in enumerate(regions):
            a = round(r0 * 100) * FRAME_SHIFT  # muestra, alineada a 10 ms
            seg = audio[a - offset : min(len(audio), int(r1 * MODEL_SR) - offset)]
            if len(seg) < MIN_WINDOW_S * MODEL_SR:
                continue
            ft = fbank(seg)
            length_s = len(seg) / MODEL_SR
            for st in window_starts(length_s):
                f0 = round(st * 100)
                chunk = ft[f0 : f0 + win_frames]
                if len(chunk) < 50:
                    continue
                t0 = a / MODEL_SR + st
                spans.append((t0, t0 + (len(chunk) * FRAME_SHIFT + 240) / MODEL_SR))
                region_of.append(ri)
                chunk = chunk - chunk.mean(axis=0)
                if len(chunk) == win_frames:
                    pending.append(chunk)
                    if len(pending) >= BATCH:
                        flush()
                else:  # tramo corto: lote propio (longitud distinta)
                    flush()
                    vecs.append(self._run(chunk[None]))
            flush()
        emb = np.concatenate(vecs) if vecs else np.zeros((0, EMBED_DIM), dtype=np.float32)
        return emb, spans, region_of

    # ---------------------------------------------------------------- Diarizer

    def diarize(self, audio: AudioArray, sr: int, n_speakers: int | None) -> list[Turn]:
        return self.diarize_with_probs(audio, sr, n_speakers, None)

    def speech_regions(
        self, probs: NDArray[np.float32], duration_s: float, *, final: bool = True
    ) -> list[tuple[float, float]]:
        """Tramos para las ventanas a partir de las probabilidades de Silero del audio.

        Con ``final=False`` (audio aun creciendo, procesado en vivo) solo se devuelven los tramos
        que ya no pueden cambiar: los que acaban antes de ``duration_s - guarda``.
        """
        params = self.region_params()
        found = vad.regions_with(probs, duration_s, params)
        if not final:
            limit = duration_s - params.stable_guard_s
            found = [r for r in found if r.end <= limit]
        return [(r.start, r.end) for r in found if r.end - r.start >= MIN_WINDOW_S]

    def region_params(self) -> vad.RegionParams:
        return vad.region_params(self._config, min_silence_ms=300, pad_ms=100)

    def diarize_with_probs(
        self,
        audio: AudioArray,
        sr: int,
        n_speakers: int | None,
        probs: NDArray[np.float32] | None,
    ) -> list[Turn]:
        """``diarize`` reutilizando las probabilidades de Silero ya calculadas sobre ``audio``
        (etapa 5): mismo resultado sin volver a pasar el VAD."""
        self._check_open()
        if n_speakers is not None and n_speakers < 1:
            raise ValueError("n_speakers must be >= 1 or None")
        x = to_mono_16k(audio, sr)
        if len(x) < MIN_WINDOW_S * MODEL_SR:
            return []
        if probs is None or sr != MODEL_SR:
            probs = vad.speech_probs(x, MODEL_SR, model=self._vad_model, config=self._config)
        regions = self.speech_regions(probs, len(x) / MODEL_SR)
        if not regions:
            return []
        emb, spans, region_of = self.embed_windows(x, regions)
        return self.turns_from_embeddings(x, regions, emb, spans, region_of, n_speakers)

    def turns_from_embeddings(
        self,
        x: AudioArray,
        regions: Sequence[tuple[float, float]],
        emb: NDArray[np.float32],
        spans: Sequence[tuple[float, float]],
        region_of: Sequence[int],
        n_speakers: int | None,
    ) -> list[Turn]:
        """Clustering + turnos a partir de los embeddings de las ventanas (``embed_windows``)."""
        if len(emb) == 0:
            return []
        labels = cluster_embeddings(
            emb, n_speakers, threshold=self._threshold, max_speakers=self._max_speakers
        )
        labels = smooth_isolated(labels, region_of)
        segs = turns_from_windows(
            spans, labels.tolist(), regions, region_of, energy=frame_energy(x)
        )
        gc.collect()
        return to_turns(segs)

    # --------------------------------------------------------- SpeakerEmbedder

    def embed(
        self, audio: AudioArray, sr: int, turns: list[Turn]
    ) -> dict[str, NDArray[np.float32]]:
        """Embedding WeSpeaker de 256 dim por hablante (media de ventanas de 3 s, L2-normalizada).

        Mismo esquema que el adaptador ``diarize`` (ventanas de 3 s sobre el audio concatenado de
        los turnos, hasta 20): las huellas guardadas con uno sirven con el otro. Hablantes con
        < 0,8 s de voz se omiten.
        """
        self._check_open()
        x = to_mono_16k(audio, sr)
        by_speaker: dict[str, list[tuple[int, int]]] = {}
        for t in turns:
            a, b = max(0, int(t.start * MODEL_SR)), min(len(x), int(t.end * MODEL_SR))
            if b > a:
                by_speaker.setdefault(t.speaker, []).append((a, b))
        out: dict[str, NDArray[np.float32]] = {}
        win = int(EMBED_WINDOW_S * MODEL_SR)
        for spk, spans in by_speaker.items():
            clip = np.concatenate([x[a:b] for a, b in spans])
            if len(clip) < EMBED_MIN_S * MODEL_SR:
                continue
            starts = list(range(0, max(1, len(clip) - win // 2), win))
            if len(starts) > EMBED_MAX_WINDOWS:
                pick = np.linspace(0, len(starts) - 1, EMBED_MAX_WINDOWS).astype(int)
                starts = [starts[i] for i in pick]
            vecs: list[NDArray[np.float32]] = []
            for s in starts:
                piece = clip[s : s + win]
                if len(piece) < EMBED_MIN_S * MODEL_SR:
                    continue
                f = fbank(piece)
                if len(f) == 0:
                    continue
                vecs.append(self._run((f - f.mean(axis=0))[None])[0])
            if vecs:
                out[spk] = _unit(np.mean(vecs, axis=0)).astype(np.float32)
        return out

    # ------------------------------------------------------------------ vida

    def close(self) -> None:
        self._closed = True
        self._sess = None
        gc.collect()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("diarizer is closed")


def create(config: Config) -> OnnxDiarizer:
    folder = models.require_models(config, models.WESPEAKER_GROUP)
    models.require_models(config, models.SILERO_GROUP)  # VAD
    threads = max(1, min(config.processing.threads, os.cpu_count() or 4))
    return OnnxDiarizer(folder / WESPEAKER_FILE, config=config, threads=threads)
