"""Adaptador de EVALUACION: ``sherpa_onnx.OfflineSpeakerDiarization`` como ``Diarizer``.

No es un motor de la app (no esta en el registro): se uso para comparar la receta de pyannote
empaquetada por sherpa-onnx (segmentacion neuronal pyannote segmentation-3.0 + embeddings +
clustering rapido, sin torch) con el diarizador ``onnx`` por defecto. Resultado y motivos en
``docs/diarization_onnx.md`` y DECISIONS.md: **no se adopto** con la evidencia actual.

Modelos: ``models.CANDIDATE_MANIFEST`` (segmentacion ``pyannote-segmentation-3.0`` y cuatro
modelos de embeddings con metadatos de sherpa-onnx). Parametros por defecto los recomendados por
sherpa-onnx: ``FastClusteringConfig(threshold=0.5)``, ``min_duration_on=0.3``,
``min_duration_off=0.5``. La salida puede tener solapes (se devuelven tal cual).

``embed`` delega en el embedder WeSpeaker ResNet34-LM del motor ``onnx`` (para que el post-filtro
de la etapa 7 se pueda evaluar igual que con los demas motores).
"""

from __future__ import annotations

import gc
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

from micnaga import models
from micnaga.pipeline.engines.asr._common import MODEL_SR, to_mono_16k
from micnaga.pipeline.engines.diar.diarize import turns_from_segments
from micnaga.registry import EngineUnavailableError
from micnaga.types import AudioArray, Turn

if TYPE_CHECKING:
    from micnaga.config import Config
    from micnaga.pipeline.engines.diar.onnx import OnnxDiarizer

SEGMENTATION_GROUP = models.PYANNOTE_SEG_GROUP
SEGMENTATION_FILE = "model.onnx"
DEFAULT_THRESHOLD = 0.5
MIN_DURATION_ON_S = 0.3
MIN_DURATION_OFF_S = 0.5
MIN_AUDIO_S = 0.5


class SherpaDiarizer:
    """``Diarizer`` + ``SpeakerEmbedder`` con ``sherpa_onnx.OfflineSpeakerDiarization``."""

    def __init__(
        self,
        segmentation: Path,
        embedding: Path,
        *,
        threads: int = 2,
        threshold: float = DEFAULT_THRESHOLD,
        wespeaker: Path | None = None,
        config: Config | None = None,
        name: str | None = None,
    ) -> None:
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise EngineUnavailableError(f"sherpa-onnx is not installed: {exc}") from exc
        for path in (segmentation, embedding):
            if not path.is_file():
                raise EngineUnavailableError(f"diarization model not found at {path}")
        self._so: Any = sherpa_onnx
        self._segmentation = str(segmentation)
        self._embedding = str(embedding)
        self._threads = max(1, threads)
        self._threshold = threshold
        self._wespeaker = wespeaker
        self._config = config
        self._embedder: OnnxDiarizer | None = None
        self.name = name or f"sherpa-onnx-pyannote-seg3+{embedding.stem}"
        self._sd: Any = self._build(None)

    def _build(self, n_speakers: int | None) -> Any:
        so = self._so
        config = so.OfflineSpeakerDiarizationConfig(
            segmentation=so.OfflineSpeakerSegmentationModelConfig(
                pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=self._segmentation),
                num_threads=self._threads,
            ),
            embedding=so.SpeakerEmbeddingExtractorConfig(
                model=self._embedding, num_threads=self._threads
            ),
            clustering=so.FastClusteringConfig(
                num_clusters=-1 if n_speakers is None else n_speakers, threshold=self._threshold
            ),
            min_duration_on=MIN_DURATION_ON_S,
            min_duration_off=MIN_DURATION_OFF_S,
        )
        if not config.validate():
            raise EngineUnavailableError("invalid sherpa-onnx diarization config")
        return so.OfflineSpeakerDiarization(config)

    def diarize(self, audio: AudioArray, sr: int, n_speakers: int | None) -> list[Turn]:
        x = to_mono_16k(audio, sr)
        if len(x) < MIN_AUDIO_S * MODEL_SR:
            return []
        sd = self._sd if n_speakers is None else self._build(n_speakers)
        result = sd.process(np.ascontiguousarray(x, dtype=np.float32)).sort_by_start_time()
        segs = [(float(s.start), float(s.end), str(s.speaker)) for s in result if s.end > s.start]
        gc.collect()
        return turns_from_segments(segs)

    def embed(
        self, audio: AudioArray, sr: int, turns: list[Turn]
    ) -> dict[str, NDArray[np.float32]]:
        if self._embedder is None:
            if self._wespeaker is None:
                return {}
            from micnaga.pipeline.engines.diar.onnx import OnnxDiarizer

            self._embedder = OnnxDiarizer(
                self._wespeaker, config=self._config, threads=self._threads
            )
        return self._embedder.embed(audio, sr, turns)

    def close(self) -> None:
        self._sd = None
        if self._embedder is not None:
            self._embedder.close()
            self._embedder = None
        gc.collect()
