"""Parakeet TDT 0.6B v3 (int8) con ``onnx-asr``.

Verificado (onnx-asr 0.12): el modelo ``nemo-conformer-tdt`` devuelve **tokens** con marca de
tiempo (resolucion 80 ms) y log-probabilidad; las palabras se reconstruyen aqui agrupando los
tokens SentencePiece (los que empiezan por espacio abren palabra; los demas, incluida la
puntuacion, se pegan a la anterior). El vocabulario no tiene digitos con marca de palabra
(``▁8`` no existe): un numero llega como un token ``▁`` suelto (``" "`` tras decodificar) seguido
de los digitos (``" ", "5", "0"``), asi que un token solo de espacio abre la palabra siguiente
(si no, salia «a las8 y50»). El modelo v3 es multilingue (incluye espanol) y emite puntuacion y
mayusculas por si mismo.

* Ficheros: ``<models_dir>/parakeet-tdt-0.6b-v3-int8/{encoder-model.int8.onnx,
  decoder_joint-model.int8.onnx, vocab.txt, config.json}``. El preprocesador de mel va dentro
  del paquete ``onnx_asr``. Nunca se descarga nada desde aqui (carga con ``path=``).
* ``processing.threads`` -> ``intra_op_num_threads`` de todas las sesiones ONNX.
* Fin de palabra: inicio de la siguiente palabra, con tope de 0,32 s tras el ultimo token (el
  modelo no da duraciones); en pausas largas la palabra no se estira.
* ``p`` de la palabra: media geometrica de las probabilidades de sus tokens.
"""

from __future__ import annotations

import gc
import logging
import math
from typing import Any

from micnaga import models
from micnaga.config import Config
from micnaga.pipeline.engines.asr._common import MODEL_SR, pad_min, split_points, to_mono_16k
from micnaga.registry import EngineUnavailableError
from micnaga.types import AudioArray, Word

log = logging.getLogger(__name__)

NAME = "parakeet-tdt-0.6b-v3-int8"
FRAME_STEP_S = 0.08  # 10 ms * submuestreo 8
MAX_WORD_TAIL_S = 0.32
OPENING_MARKS = "¿¡([{“«"
CLOSING_MARKS = ".,;:!?…)]}”»%"


def words_from_tokens(
    tokens: list[str],
    timestamps: list[float],
    logprobs: list[float] | None,
    *,
    offset: float = 0.0,
    duration: float | None = None,
) -> list[Word]:
    """Agrupa tokens SentencePiece en ``Word`` (funcion pura, probada sin modelo)."""
    groups: list[dict[str, Any]] = []
    prefix = ""
    prefix_ts: float | None = None
    boundary = False  # hubo un token solo de espacio (``▁``): el siguiente abre palabra
    for i, tok in enumerate(tokens):
        ts = timestamps[i]
        lp = logprobs[i] if logprobs is not None else 0.0
        stripped = tok.strip()
        if not stripped:
            boundary = boundary or bool(tok)
            continue
        starts_word = tok[:1].isspace() or boundary or not groups or bool(prefix)
        boundary = False
        if starts_word and all(c in OPENING_MARKS for c in stripped):
            prefix += stripped  # «¿» suelto: se pega a la palabra siguiente
            prefix_ts = ts if prefix_ts is None else prefix_ts
            continue
        if starts_word and groups and all(c in CLOSING_MARKS for c in stripped):
            g = groups[-1]  # puntuacion suelta tras espacio: se pega a la palabra anterior
            g["text"] += stripped
            g["last"] = ts
            g["lps"].append(lp)
        elif starts_word:
            start = prefix_ts if prefix_ts is not None else ts
            groups.append({"text": prefix + stripped, "s": start, "last": ts, "lps": [lp]})
            prefix, prefix_ts = "", None
        else:
            g = groups[-1]
            g["text"] += tok
            g["last"] = ts
            g["lps"].append(lp)
    if prefix and groups:  # prefijo huerfano al final: pegarlo a la ultima palabra
        groups[-1]["text"] += prefix
    words: list[Word] = []
    for k, g in enumerate(groups):
        end = g["last"] + MAX_WORD_TAIL_S
        if k + 1 < len(groups):
            end = min(end, groups[k + 1]["s"])
        if duration is not None:
            end = min(end, duration)
        end = max(end, g["s"] + FRAME_STEP_S / 2)
        p = math.exp(sum(g["lps"]) / len(g["lps"])) if logprobs is not None else 1.0
        words.append(
            Word(w=g["text"], s=offset + g["s"], e=offset + end, p=float(min(1.0, max(0.0, p))))
        )
    return words


class ParakeetEngine:
    """Un modelo Parakeet cargado en memoria (``close()`` lo libera)."""

    name = NAME

    def __init__(self, model_dir: str, threads: int) -> None:
        try:
            import onnx_asr
            import onnxruntime as ort
        except ImportError as exc:
            raise EngineUnavailableError(
                f"onnx-asr is not installed: {exc} (pip install micnaga[asr])"
            ) from exc
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, threads)
        so.inter_op_num_threads = 1
        so.log_severity_level = 3
        try:
            self._model: Any = onnx_asr.load_model(
                "nemo-conformer-tdt",
                model_dir,
                quantization="int8",
                sess_options=so,
                providers=["CPUExecutionProvider"],
            ).with_timestamps()
        except Exception as exc:
            raise EngineUnavailableError(f"cannot load Parakeet from {model_dir}: {exc}") from exc

    def transcribe(self, audio: AudioArray, sr: int) -> list[Word]:
        if self._model is None:
            raise RuntimeError("engine is closed")
        x = to_mono_16k(audio, sr)
        words: list[Word] = []
        for a, b in split_points(x):
            chunk = x[a:b]
            if len(chunk) < 0.05 * MODEL_SR:
                continue
            res = self._model.recognize(pad_min(chunk), sample_rate=MODEL_SR)
            if not res.tokens:
                continue
            words.extend(
                words_from_tokens(
                    list(res.tokens),
                    [float(t) for t in (res.timestamps or [])],
                    [float(v) for v in res.logprobs] if res.logprobs is not None else None,
                    offset=a / MODEL_SR,
                    duration=b / MODEL_SR,
                )
            )
        return words

    def close(self) -> None:
        self._model = None
        gc.collect()


def create(config: Config) -> ParakeetEngine:
    model_dir = models.require_models(config, models.PARAKEET_GROUP)
    return ParakeetEngine(str(model_dir), config.processing.threads)
