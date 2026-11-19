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
    guards: bool = False

    def __init__(
        self,
        model_dir: str,
        model_size: str,
        threads: int,
        language: str | None = "es",
        *,
        guards: bool = False,
    ) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise EngineUnavailableError(
                f"faster-whisper is not installed: {exc} (pip install micnaga[asr])"
            ) from exc
        self.name = f"faster-whisper-{model_size}-int8"
        self.language = language
        self.guards = guards
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
        extra: dict[str, Any] = {}
        if self.guards:  # «Mejorar transcripcion» (pipeline/hq.py): umbrales anti-alucinacion
            from micnaga.pipeline import hq

            extra = {
                "no_speech_threshold": hq.NO_SPEECH_THRESHOLD,
                "log_prob_threshold": hq.LOG_PROB_THRESHOLD,
                "compression_ratio_threshold": hq.COMPRESSION_RATIO_THRESHOLD,
            }
        segments, _info = self._model.transcribe(
            pad_min(x),
            language=self.language,
            word_timestamps=True,
            beam_size=5,
            condition_on_previous_text=False,
            vad_filter=False,
            **extra,
        )
        words: list[Word] = []
        for seg in segments:  # generador perezoso: aqui se hace la inferencia
            if self.guards and _hallucinated(seg):
                continue
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


def _hallucinated(seg: Any) -> bool:
    """Segmento que faster-whisper deja pasar pero que huele a alucinacion (silencio con texto
    seguro de si mismo, o texto muy repetitivo)."""
    from micnaga.pipeline import hq

    no_speech = float(getattr(seg, "no_speech_prob", 0.0))
    logprob = float(getattr(seg, "avg_logprob", 0.0))
    ratio = float(getattr(seg, "compression_ratio", 0.0))
    if ratio > hq.COMPRESSION_RATIO_THRESHOLD:
        return True
    return no_speech > hq.NO_SPEECH_THRESHOLD and logprob < hq.LOG_PROB_THRESHOLD


def create(config: Config) -> WhisperEngine:
    size = config.processing.whisper_model
    try:
        group = models.whisper_group(size)
    except models.ModelError as exc:
        raise EngineUnavailableError(str(exc)) from exc
    model_dir = models.require_models(config, group)
    return WhisperEngine(str(model_dir), size, config.processing.threads)
