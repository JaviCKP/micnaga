"""Adaptador de la libreria ``diarize`` (Apache-2.0, CPU) + extractor de huellas (MIC-11).

Hechos verificados con ``diarize 0.1.2``:

* ``diarize.diarize(ruta_wav, min_speakers, max_speakers, num_speakers)`` solo acepta **ruta a
  fichero**: se escribe un WAV temporal (borrado siempre al terminar, aunque falle).
* ``import diarize`` arrastra **torch** (~340 MB de RSS solo por importar): por eso la libreria
  se importa **a la primera llamada** y no al crear el motor ni al importar este modulo.
  ``close()`` suelta las referencias y hace ``gc.collect()``, pero torch no se puede descargar de
  un proceso; si hace falta devolver esa memoria hay que ejecutar la etapa en un subproceso.
* ``diarize`` usa Silero VAD (pip ``silero_vad`` con torch, modelo incluido en el paquete) y
  WeSpeaker ResNet34-LM via ``wespeakerruntime``, que **descarga el ONNX por su cuenta** a
  ``~/.wespeaker`` desde un servidor ajeno (``myqcloud.com``). Aqui se evita: durante la llamada
  se sustituye ``wespeakerruntime.Speaker`` por una fabrica que le pasa ``onnx_path=`` apuntando
  al fichero del manifiesto (``<models_dir>/wespeaker/voxceleb_resnet34_LM.onnx``, byte a byte
  identico al del servidor original: mismo SHA-256). Sin red y sin tocar ``~``.
* La estimacion automatica de numero de hablantes (GMM-BIC) sobre-segmenta voz sintetica limpia
  (TTS: 13 hablantes para 2 voces con ``max_speakers=20``, 6 con 8). Con ``n_speakers=None`` el
  adaptador limita a ``DEFAULT_MAX_SPEAKERS`` y ademas **funde** los hablantes cuyos centroides
  WeSpeaker tienen coseno >= ``merge_threshold`` (0,55; 0,45 para hablantes con < 10 s; sin
  calibrar con voces reales); con ``n_speakers`` conocido no se toca nada.
* Etiquetas ``SPEAKER_00`` -> ``"S1"``... por orden de **primera aparicion**.
"""

from __future__ import annotations

import contextlib
import gc
import importlib.util
import logging
import shutil
import tempfile
import threading
import warnings
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from numpy.typing import NDArray

from micnaga import models
from micnaga.config import Config
from micnaga.pipeline.engines.asr._common import MODEL_SR, to_mono_16k
from micnaga.registry import EngineUnavailableError
from micnaga.types import AudioArray, Turn

log = logging.getLogger(__name__)

DEFAULT_MAX_SPEAKERS = 8
DEFAULT_MERGE_THRESHOLD = 0.55
SMALL_SPEAKER_S = 10.0
SMALL_MERGE_THRESHOLD = 0.45
MIN_DIARIZE_S = 0.5
EMBED_WINDOW_S = 3.0
EMBED_MAX_WINDOWS = 20
EMBED_MIN_S = 0.8
WESPEAKER_FILE = "voxceleb_resnet34_LM.onnx"

_patch_lock = threading.Lock()


def _label_map(labels_in_order: list[str]) -> dict[str, str]:
    """``SPEAKER_07`` -> ``S1`` por primera aparicion."""
    mapping: dict[str, str] = {}
    for lab in labels_in_order:
        if lab not in mapping:
            mapping[lab] = f"S{len(mapping) + 1}"
    return mapping


def turns_from_segments(segments: list[tuple[float, float, str]]) -> list[Turn]:
    """Convierte ``(start, end, etiqueta)`` de la libreria en ``Turn`` normalizados (puro)."""
    ordered = sorted(segments, key=lambda s: (s[0], s[1]))
    mapping = _label_map([s[2] for s in ordered])
    return [Turn(speaker=mapping[lab], start=float(a), end=float(b)) for a, b, lab in ordered]


def merge_similar_speakers(
    turns: list[Turn],
    embeddings: dict[str, NDArray[np.float32]],
    threshold: float = DEFAULT_MERGE_THRESHOLD,
    small_s: float = SMALL_SPEAKER_S,
    small_threshold: float = SMALL_MERGE_THRESHOLD,
) -> list[Turn]:
    """Funde hablantes que son en realidad la misma voz (post-proceso de la estimacion automatica).

    1. Clustering aglomerativo sobre los centroides WeSpeaker: se funden los pares con coseno
       >= ``threshold`` (centroides ponderados por tiempo de habla).
    2. Los hablantes pequenos (< ``small_s`` s de voz) se absorben en el mas parecido si el
       coseno es >= ``small_threshold`` (los clusters pequenos salen con centroides ruidosos).
    3. Los hablantes sin embedding (casi sin voz) pasan al hablante del turno mas cercano.

    Contrarresta la sobre-segmentacion de ``diarize`` (una voz partida en varios hablantes). Los
    umbrales NO estan calibrados con reuniones reales (voz sintetica limpia: misma voz ~0,8-0,9,
    voces distintas ~0,2-0,3): afinar en F2 con el banco de pruebas. Riesgo conocido: un
    participante real que casi no habla puede quedar absorbido por otro.
    El resultado se renumera por primera aparicion.
    """
    dur: dict[str, float] = {}
    for tn in turns:
        dur[tn.speaker] = dur.get(tn.speaker, 0.0) + (tn.end - tn.start)
    cent = {s: np.asarray(v, dtype=np.float64) for s, v in embeddings.items() if s in dur}
    for s, v in cent.items():
        cent[s] = v / (float(np.linalg.norm(v)) or 1.0)
    group = {s: s for s in dur}  # etiqueta original -> etiqueta fusionada
    weight = {s: dur[s] for s in cent}

    def absorb(a: str, b: str) -> None:  # b entra en a
        merged = weight[a] * cent[a] + weight[b] * cent[b]
        cent[a] = merged / (float(np.linalg.norm(merged)) or 1.0)
        weight[a] += weight[b]
        del cent[b], weight[b]
        for s, g in group.items():
            if g == b:
                group[s] = a

    def best_pair(min_sim: float, only_small: bool) -> tuple[str, str] | None:
        best: tuple[float, str, str] | None = None
        for a in cent:
            for b in cent:
                if a == b or (only_small and weight[b] >= small_s):
                    continue
                sim = float(cent[a] @ cent[b])
                if sim >= min_sim and (best is None or sim > best[0]):
                    best = (sim, a, b)
        return None if best is None else (best[1], best[2])

    while len(cent) > 1 and (pair := best_pair(threshold, False)) is not None:
        absorb(*pair)
    while len(cent) > 1 and (pair := best_pair(small_threshold, True)) is not None:
        absorb(*pair)

    # hablantes sin embedding: se pegan al hablante del turno (con embedding) mas cercano
    anchors = [tn for tn in turns if tn.speaker in cent or group[tn.speaker] in cent]
    for tn in turns:
        if tn.speaker in embeddings or not anchors:
            continue
        mid = (tn.start + tn.end) / 2
        near = min(anchors, key=lambda o: abs((o.start + o.end) / 2 - mid))
        group[tn.speaker] = group[near.speaker]
    return turns_from_segments([(tn.start, tn.end, group[tn.speaker]) for tn in turns])


@contextlib.contextmanager
def _local_wespeaker(onnx_path: Path, threads: int) -> Iterator[None]:
    """Hace que ``wespeakerruntime.Speaker`` cargue el ONNX local (nunca descarga nada)."""
    import wespeakerruntime

    original = wespeakerruntime.Speaker

    def factory(*_args: Any, **_kwargs: Any) -> Any:
        return original(onnx_path=str(onnx_path), intra_op_num_threads=threads)

    with _patch_lock:
        wespeakerruntime.Speaker = factory
        try:
            yield
        finally:
            wespeakerruntime.Speaker = original


class DiarizeDiarizer:
    """``Diarizer`` + ``SpeakerEmbedder`` sobre la libreria ``diarize``."""

    def __init__(
        self,
        onnx_path: Path,
        threads: int = 2,
        max_speakers: int = DEFAULT_MAX_SPEAKERS,
        work_dir: Path | None = None,
        merge_threshold: float | None = DEFAULT_MERGE_THRESHOLD,
    ) -> None:
        self._onnx = onnx_path
        self._threads = max(1, threads)
        self._max_speakers = max(1, max_speakers)
        self._work_dir = work_dir
        self._merge_threshold = merge_threshold
        self._closed = False
        try:
            version = metadata.version("diarize")
        except metadata.PackageNotFoundError:  # pragma: no cover
            version = "unknown"
        self.name = f"diarize-{version}"

    # ---------------------------------------------------------------- Diarizer

    def diarize(self, audio: AudioArray, sr: int, n_speakers: int | None) -> list[Turn]:
        self._check_open()
        x = to_mono_16k(audio, sr)
        if len(x) < MIN_DIARIZE_S * MODEL_SR:
            return []
        if n_speakers is not None and n_speakers < 1:
            raise ValueError("n_speakers must be >= 1 or None")
        tmp = Path(tempfile.mkdtemp(prefix="micnaga-diar-", dir=self._work_dir))
        try:
            wav = tmp / "remote.wav"
            sf.write(str(wav), x, MODEL_SR, subtype="PCM_16")
            with warnings.catch_warnings(), _local_wespeaker(self._onnx, self._threads):
                warnings.simplefilter("ignore")
                import diarize as diarize_lib

                if n_speakers is not None:
                    result = diarize_lib.diarize(str(wav), num_speakers=n_speakers)
                else:
                    result = diarize_lib.diarize(
                        str(wav), min_speakers=1, max_speakers=self._max_speakers
                    )
            segs = [(s.start, s.end, s.speaker) for s in result.segments]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        turns = turns_from_segments(segs)
        multi = len({tn.speaker for tn in turns}) > 1
        if n_speakers is None and self._merge_threshold is not None and multi:
            turns = merge_similar_speakers(
                turns, self.embed(x, MODEL_SR, turns), self._merge_threshold
            )
        gc.collect()
        return turns

    # --------------------------------------------------------- SpeakerEmbedder

    def embed(
        self, audio: AudioArray, sr: int, turns: list[Turn]
    ) -> dict[str, NDArray[np.float32]]:
        """Embedding WeSpeaker de 256 dim por hablante (media de ventanas, L2-normalizada).

        Usa solo el audio de los turnos de cada hablante, en ventanas de 3 s (hasta 20), con
        las mismas características fbank que ``diarize``. Hablantes con < 0,8 s de voz se omiten.
        """
        self._check_open()
        x = to_mono_16k(audio, sr)
        by_speaker: dict[str, list[tuple[int, int]]] = {}
        for t in turns:
            a, b = max(0, int(t.start * MODEL_SR)), min(len(x), int(t.end * MODEL_SR))
            if b > a:
                by_speaker.setdefault(t.speaker, []).append((a, b))
        out: dict[str, NDArray[np.float32]] = {}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import torch
            import torchaudio.compliance.kaldi as kaldi
            import wespeakerruntime

            model = wespeakerruntime.Speaker(
                onnx_path=str(self._onnx), intra_op_num_threads=self._threads
            )
            for spk, spans in by_speaker.items():
                clip = np.concatenate([x[a:b] for a, b in spans])
                if len(clip) < EMBED_MIN_S * MODEL_SR:
                    continue
                win = int(EMBED_WINDOW_S * MODEL_SR)
                starts = list(range(0, max(1, len(clip) - win // 2), win))
                if len(starts) > EMBED_MAX_WINDOWS:
                    pick = np.linspace(0, len(starts) - 1, EMBED_MAX_WINDOWS).astype(int)
                    starts = [starts[i] for i in pick]
                vecs: list[NDArray[np.float32]] = []
                for s in starts:
                    piece = clip[s : s + win]
                    if len(piece) < EMBED_MIN_S * MODEL_SR:
                        continue
                    wave = torch.from_numpy(piece.copy()).unsqueeze(0) * (1 << 15)
                    feats = kaldi.fbank(
                        wave,
                        num_mel_bins=80,
                        frame_length=25,
                        frame_shift=10,
                        dither=0.0,
                        sample_frequency=MODEL_SR,
                        window_type="hamming",
                        use_energy=False,
                    ).numpy()
                    feats = feats - feats.mean(axis=0)
                    emb = np.asarray(
                        model.extract_embedding_feat(feats[None], cmn=False), dtype=np.float32
                    )[0]
                    n = float(np.linalg.norm(emb))
                    if n > 0:
                        vecs.append(emb / n)
                if vecs:
                    mean = np.mean(vecs, axis=0)
                    out[spk] = (mean / (float(np.linalg.norm(mean)) or 1.0)).astype(np.float32)
        gc.collect()
        return out

    # ------------------------------------------------------------------ vida

    def close(self) -> None:
        self._closed = True
        gc.collect()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("diarizer is closed")


def create(config: Config) -> DiarizeDiarizer:
    for mod in ("diarize", "wespeakerruntime"):
        if importlib.util.find_spec(mod) is None:
            raise EngineUnavailableError(
                f"python package {mod!r} is not installed (pip install micnaga[diar])"
            )
    folder = models.require_models(config, models.WESPEAKER_GROUP)
    return DiarizeDiarizer(folder / WESPEAKER_FILE, threads=min(config.processing.threads, 4))
