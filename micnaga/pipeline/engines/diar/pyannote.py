"""Adaptador de pyannote.audio (community-1) como diarizador alternativo.

ESTADO: escrito contra la API documentada de pyannote.audio 4.x y **no verificado contra la
libreria real** (no esta instalada en este PC a proposito: arrastra pytorch-lightning,
torchmetrics, torchcodec...). Sin ella, ``create`` lanza ``EngineUnavailableError``.

API usada (documentada para ``pyannote/speaker-diarization-community-1``):

* ``Pipeline.from_pretrained(<carpeta con config.yaml>)`` -> pipeline (offline si se le pasa una
  ruta local; nunca se le pasa un id de Hugging Face, asi no hay red ni token).
* ``pipeline({"waveform": tensor(1, n), "sample_rate": sr}, num_speakers=n)`` -> en 4.x un
  ``DiarizeOutput`` con ``.speaker_diarization`` (``Annotation``); en 3.x directamente la
  ``Annotation``. Ambos casos se aceptan.
* ``annotation.itertracks(yield_label=True)`` -> ``(Segment, pista, etiqueta)``.

La carpeta del modelo es ``<models_dir>/pyannote-community-1/`` (no esta en el manifiesto: es un
repositorio con acceso restringido, requiere aceptar condiciones y token; el usuario avanzado la
coloca a mano). Este adaptador no implementa ``SpeakerEmbedder``.
"""

from __future__ import annotations

import gc
import importlib.util
import logging
from typing import Any

import numpy as np

from micnaga import paths
from micnaga.config import Config
from micnaga.pipeline.engines.asr._common import MODEL_SR, to_mono_16k
from micnaga.pipeline.engines.diar.diarize import turns_from_segments
from micnaga.registry import EngineUnavailableError
from micnaga.types import AudioArray, Turn

log = logging.getLogger(__name__)

GROUP = "pyannote-community-1"
MIN_DIARIZE_S = 0.5


class PyannoteDiarizer:
    """Diarizador pyannote community-1 (carga perezosa del pipeline)."""

    name = "pyannote-community-1"

    def __init__(self, model_dir: str, threads: int) -> None:
        self._dir = model_dir
        self._threads = max(1, threads)
        self._pipeline: Any = None

    def _load(self) -> Any:
        if self._pipeline is None:
            try:
                import torch
                from pyannote.audio import Pipeline

                torch.set_num_threads(self._threads)
                self._pipeline = Pipeline.from_pretrained(self._dir)
            except Exception as exc:
                raise EngineUnavailableError(
                    f"cannot load pyannote pipeline from {self._dir}: {exc}"
                ) from exc
            self._pipeline.to(torch.device("cpu"))
        return self._pipeline

    def diarize(self, audio: AudioArray, sr: int, n_speakers: int | None) -> list[Turn]:
        x = to_mono_16k(audio, sr)
        if len(x) < MIN_DIARIZE_S * MODEL_SR:
            return []
        pipeline = self._load()
        import torch

        waveform = torch.from_numpy(np.ascontiguousarray(x)).unsqueeze(0)
        kwargs: dict[str, Any] = {}
        if n_speakers is not None:
            kwargs["num_speakers"] = n_speakers
        out = pipeline({"waveform": waveform, "sample_rate": MODEL_SR}, **kwargs)
        annotation = getattr(out, "speaker_diarization", out)
        segs = [
            (seg.start, seg.end, str(label))
            for seg, _, label in annotation.itertracks(yield_label=True)
        ]
        return turns_from_segments(segs)

    def close(self) -> None:
        self._pipeline = None
        gc.collect()


def create(config: Config) -> PyannoteDiarizer:
    if importlib.util.find_spec("pyannote.audio") is None:
        raise EngineUnavailableError(
            "pyannote.audio is not installed (pip install micnaga[pyannote]); "
            "the default diarizer is 'diarize'"
        )
    folder = paths.models_dir(config) / GROUP
    if not (folder / "config.yaml").is_file():
        raise EngineUnavailableError(
            f"pyannote community-1 files not found in {folder} (expected config.yaml and weights); "
            "the model is gated on Hugging Face and must be placed there manually."
        )
    return PyannoteDiarizer(str(folder), config.processing.threads)
