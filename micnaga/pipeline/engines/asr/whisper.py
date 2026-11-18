"""faster-whisper (CTranslate2) como motor ASR alternativo.

* Modelo: ``<models_dir>/faster-whisper-<small>/`` (``config.json``, ``model.bin``,
  ``tokenizer.json``, ``vocabulary.txt``); se pasa la **ruta** a ``WhisperModel`` con
  ``local_files_only=True``, asi que ``huggingface_hub`` nunca se usa ni escribe en
  ``~/.cache/huggingface``.
* ``compute_type="int8"``, ``device="cpu"``, ``cpu_threads=processing.threads``,
  ``language="es"``, ``word_timestamps=True``. Solo ``small`` esta en el manifiesto
  (``medium`` pesa ~1,5 GB y no esta descargado; pedirlo da ``EngineUnavailableError``).
* ``condition_on_previous_text=False`` (evita bucles de repeticion en bloques independientes de
  VAD) y ``vad_filter=False`` (el VAD ya se hizo en la etapa 5).
* ``p`` de cada palabra = ``probability`` que da faster-whisper.
"""

from __future__ import annotations

import gc
import logging
from typing import Any

from micnaga import models
from micnaga.config import Config
from micnaga.pipeline.engines.asr._common import MODEL_SR, pad_min, to_mono_16k
from micnaga.registry import EngineUnavailableError
from micnaga.types import AudioArray, Word

log = logging.getLogger(__name__)


class WhisperEngine:
    """Un modelo faster-whisper cargado (``close()`` lo libera)."""

    name: str

    def __init__(
        self, model_dir: str, model_size: str, threads: int, language: str | None = "es"
    ) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise EngineUnavailableError(
                f"faster-whisper is not installed: {exc} (pip install micnaga[asr])"
            ) from exc
        self.name = f"faster-whisper-{model_size}-int8"
        self.language = language
        try:
            self._model: Any = WhisperModel(
                model_dir,
                device="cpu",
                compute_type="int8",
                cpu_threads=max(1, threads),
                local_files_only=True,
            )
        except Exception as exc:
            raise EngineUnavailableError(
                f"cannot load faster-whisper from {model_dir}: {exc}"
            ) from exc

    def transcribe(self, audio: AudioArray, sr: int) -> list[Word]:
        if self._model is None:
            raise RuntimeError("engine is closed")
        x = to_mono_16k(audio, sr)
        if len(x) < 0.05 * MODEL_SR:
            return []
        segments, _info = self._model.transcribe(
            pad_min(x),
            language=self.language,
            word_timestamps=True,
            beam_size=5,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        words: list[Word] = []
        for seg in segments:  # generador perezoso: aqui se hace la inferencia
            for w in seg.words or []:
                text = str(w.word).strip()
                if not text:
                    continue
                words.append(
                    Word(
                        w=text,
                        s=float(w.start),
                        e=max(float(w.end), float(w.start)),
                        p=float(min(1.0, max(0.0, w.probability))),
                    )
                )
        return words

    def close(self) -> None:
        self._model = None
        gc.collect()


def create(config: Config) -> WhisperEngine:
    size = config.processing.whisper_model
    try:
        group = models.whisper_group(size)
    except models.ModelError as exc:
        raise EngineUnavailableError(str(exc)) from exc
    model_dir = models.require_models(config, group)
    return WhisperEngine(str(model_dir), size, config.processing.threads)
