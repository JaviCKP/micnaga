"""Atomic ASR block checkpoints for the batch pipeline and HQ."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from micnaga.config import Config
from micnaga.pipeline.stages.base import atomic_write_json
from micnaga.types import Word


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def asr_key(config: Config, *, whisper_model: str | None = None) -> str:
    """Resolve the options that affect words, including glossary.txt."""
    processing = config.processing
    if whisper_model is None and processing.asr_engine != "whisper":
        return _digest(processing.asr_engine)
    from micnaga.pipeline.engines.asr.whisper import engine_options

    return _digest([whisper_model or processing.whisper_model, engine_options(config)])


class BlockCheckpoint:
    def __init__(self, directory: Path, options: Any, audio: Iterable[Path]) -> None:
        stamps = []
        for path in audio:
            info = path.stat()
            stamps.append((path.name, info.st_size, info.st_mtime_ns))
        self._dir = directory / _digest([1, options, stamps])

    def words(self, track: str, index: int, transcribe: Callable[[], list[Word]]) -> list[Word]:
        path = self._dir / f"{track}-{index}.json"
        try:
            return [Word.from_dict(w) for w in json.loads(path.read_text(encoding="utf-8"))]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            words = transcribe()
            atomic_write_json(path, [word.to_dict() for word in words])
            return words
