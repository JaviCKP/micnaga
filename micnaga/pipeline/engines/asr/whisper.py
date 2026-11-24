"""faster-whisper (CTranslate2): motor ASR por defecto (``large-v3-turbo``; DECISIONS §32).

* Modelo: ``<models_dir>/faster-whisper-<tamano>/`` (``config.json``, ``model.bin``,
  ``tokenizer.json``, vocabulario); se pasa la **ruta** a ``WhisperModel`` con
  ``local_files_only=True``, asi que ``huggingface_hub`` nunca se usa ni escribe en
  ``~/.cache/huggingface``. Tamanos del manifiesto: ``small``, ``large-v3-turbo`` (defecto) y
  ``large-v3``; otro tamano da ``EngineUnavailableError``.
* ``compute_type="int8"``, ``device="cpu"``, ``cpu_threads=processing.threads``,
  ``word_timestamps=True``, ``beam_size`` 5.
* **Idioma forzado** (``asr_language``): el de ``ui.language`` si es un codigo de Whisper, si no
  ``es``. Sin forzarlo Whisper detecta el idioma con los primeros 30 s de **cada** bloque, y un
  bloque corto o con un anglicismo («Claude», «wrapper») podia salir en ingles.
* **Glosario** personal y de Bitnaga como ``hotwords`` de faster-whisper:
  van en el prompt de **cada** pasada de decodificacion (un ``initial_prompt`` solo llega a la
  primera pasada del bloque cuando ``condition_on_previous_text`` es ``False``).
* ``condition_on_previous_text=False`` (evita bucles de repeticion) y ``vad_filter=False`` (el VAD
  ya se hizo en la etapa 5).
* **Guardas contra alucinaciones** (``guards``, encendidas en el pipeline y en «Mejorar
  transcripcion»): umbrales de faster-whisper (no_speech / logprob / compression ratio), segmentos
  descartados con esos mismos criterios, segmentos que solo repiten el glosario del prompt,
  frases de cierre de video que Whisper inventa en el silencio (``KNOWN_HALLUCINATIONS``) y
  ``drop_loops`` (n-gramas repetidos en bucle). Los silencios largos dentro de un bloque se
  acortan antes de llegar aqui (``s06_asr.compact``), que es donde nacian esas frases.
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

DEFAULT_LANGUAGE = "es"
BEAM_SIZE = 5
MIN_PROMPT_ECHO_TERMS = 3
"""Un segmento hecho solo de palabras del glosario se descarta si trae «Glosario» o al menos
estas palabras distintas (Whisper repite su prompt cuando el bloque apenas tiene voz); asi un
«San Francisco.» dicho de verdad se conserva."""
KNOWN_HALLUCINATIONS: frozenset[str] = frozenset(
    {
        "graciasporverelvideo",
        "graciasporver",
        "graciasporverestevideo",
        "suscribete",
        "suscribeteamicanal",
        "noolvidessuscribirte",
        "subtitulosrealizadosporlacomunidaddeamaraorg",
        "subtitulosporlacomunidaddeamaraorg",
        "subtituladoporlacomunidaddeamaraorg",
        "amaraorg",
        "thankyouforwatching",
        "thanksforwatching",
    }
)
"""Segmentos enteros (normalizados: minusculas, sin tildes ni signos) que Whisper inventa en
silencios: cierres de video de YouTube y creditos de subtitulos de sus datos de entrenamiento.
En una reunion de trabajo no se dicen como frase suelta."""


class WhisperEngine:
    """Un modelo faster-whisper cargado (``close()`` lo libera)."""

    name: str
    guards: bool = False
    hotwords: str | None = None
    beam_size: int = BEAM_SIZE
    _prompt_terms: frozenset[str] = frozenset()

    def __init__(
        self,
        model_dir: str,
        model_size: str,
        threads: int,
        language: str | None = DEFAULT_LANGUAGE,
        *,
        guards: bool = False,
        hotwords: str | None = None,
        beam_size: int = BEAM_SIZE,
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
        self.hotwords = hotwords.strip() if hotwords else None
        self.beam_size = max(1, beam_size)
        self._prompt_terms = frozenset(_norm_tokens(self.hotwords or ""))
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
        if self.guards:
            from micnaga.pipeline import hq

            extra = {
                "no_speech_threshold": hq.NO_SPEECH_THRESHOLD,
                "log_prob_threshold": hq.LOG_PROB_THRESHOLD,
                "compression_ratio_threshold": hq.COMPRESSION_RATIO_THRESHOLD,
            }
        if self.hotwords:
            extra["hotwords"] = self.hotwords
        segments, _info = self._model.transcribe(
            pad_min(x),
            language=self.language,
            word_timestamps=True,
            beam_size=self.beam_size,
            condition_on_previous_text=False,
            vad_filter=False,
            **extra,
        )
        words: list[Word] = []
        for seg in segments:  # generador perezoso: aqui se hace la inferencia
            if self.guards and (
                _hallucinated(seg) or _known_hallucination(seg) or self._prompt_echo(seg)
            ):
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
        if self.guards:
            from micnaga.pipeline import hq

            words = hq.drop_loops(words)
        return words

    def _prompt_echo(self, seg: Any) -> bool:
        """``True`` si el segmento solo repite terminos del glosario (``hotwords``)."""
        if not self._prompt_terms:
            return False
        tokens = set(_norm_tokens(str(getattr(seg, "text", ""))))
        if not tokens or not tokens <= self._prompt_terms:
            return False
        return "glosario" in tokens or len(tokens) >= MIN_PROMPT_ECHO_TERMS

    def close(self) -> None:
        self._model = None
        gc.collect()


def _norm_tokens(text: str) -> list[str]:
    from micnaga.glossary import normalize

    return [t for t in (normalize(tok) for tok in text.split()) if t]


def _known_hallucination(seg: Any) -> bool:
    from micnaga.glossary import normalize

    return normalize(str(getattr(seg, "text", ""))) in KNOWN_HALLUCINATIONS


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


def asr_language(config: Config) -> str:
    """Idioma que se fuerza en Whisper: ``ui.language`` si Whisper lo conoce, si no ``es``."""
    lang = (config.ui.language or "").strip().lower().split("-")[0]
    try:
        from faster_whisper.tokenizer import _LANGUAGE_CODES
    except ImportError:  # pragma: no cover - sin faster-whisper no hay motor que crear
        return lang if lang == DEFAULT_LANGUAGE else DEFAULT_LANGUAGE
    return lang if lang in _LANGUAGE_CODES else DEFAULT_LANGUAGE


def glossary_prompt(config: Config) -> str | None:
    """Terminos personales y de Bitnaga para ``hotwords``."""
    from micnaga.glossary import Glossary

    try:
        glossary = Glossary.load(config=config)
    except OSError as exc:
        log.warning("cannot read the glossary for the ASR prompt: %s", exc)
        return None
    return glossary.prompt() or None


def engine_options(config: Config) -> dict[str, Any]:
    """Argumentos comunes de ``WhisperEngine`` para el pipeline y «Mejorar transcripcion»."""
    return {
        "language": asr_language(config),
        "guards": True,
        "hotwords": glossary_prompt(config),
    }


def create(config: Config) -> WhisperEngine:
    size = config.processing.whisper_model
    try:
        group = models.whisper_group(size)
    except models.ModelError as exc:
        raise EngineUnavailableError(str(exc)) from exc
    model_dir = models.require_models(config, group)
    return WhisperEngine(str(model_dir), size, config.processing.threads, **engine_options(config))
