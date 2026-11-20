"""``LibraryOps`` de la UI sobre ``pipeline.output`` y ``store.voiceprints``.

Las importaciones pesadas (numpy via huellas de voz) son perezosas. ``folder`` es el nombre de la
carpeta de la reunion (la UI ya lo valida; aqui se vuelve a comprobar que no sale de la raiz).
Las huellas solo se guardan con ``privacy.voiceprints_enabled`` y nunca para «Yo».
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from micnaga import paths
from micnaga.config import Config, load_config
from micnaga.types import SPEAKER_ME
from micnaga.ui.contracts import Voiceprint

log = logging.getLogger(__name__)


class VoiceprintsDisabledError(RuntimeError):
    """Las huellas de voz estan desactivadas en Ajustes."""


class AppLibraryOps:
    def __init__(self, config: Callable[[], Config] | None = None) -> None:
        self._config = config or load_config

    def _folder(self, folder: str) -> Path:
        """Bajo la raiz de ahora o por la ruta del indice (``micnaga.store.locate``, P1-4)."""
        from micnaga.store.locate import existing_db_factory, meeting_dir

        cfg = self._config()
        path = meeting_dir(
            paths.meetings_root(cfg), folder, existing_db_factory(paths.db_path(cfg))
        )
        if path is None:
            raise FileNotFoundError(folder)
        return path

    def rename_speaker(self, folder: str, speaker_id: str, name: str) -> None:
        from micnaga.pipeline import output

        output.rename_speaker(self._folder(folder), speaker_id, name, learn_voiceprint=False)

    def merge_speakers(self, folder: str, keep_id: str, merge_id: str) -> None:
        from micnaga.pipeline import output

        output.merge_speakers(self._folder(folder), keep_id, merge_id)

    def regenerate_from_json(self, folder: str) -> None:
        from micnaga.pipeline import output

        output.regenerate_from_json(self._folder(folder))

    def list_voiceprints(self) -> list[Voiceprint]:
        from micnaga.store.voiceprints import VoiceprintStore

        return [Voiceprint(name=n) for n in VoiceprintStore().list()]

    def save_voiceprint(self, folder: str, speaker_id: str) -> None:
        if not self._config().privacy.voiceprints_enabled:
            raise VoiceprintsDisabledError("voiceprints are disabled")
        if speaker_id == SPEAKER_ME:
            raise ValueError("the me speaker never gets a voiceprint")
        from micnaga.pipeline import output
        from micnaga.store.voiceprints import VoiceprintStore, load_meeting_embeddings

        path = self._folder(folder)
        transcript = output.read_transcript(path)
        name = next((s.name for s in transcript.speakers if s.id == speaker_id), "")
        if not name.strip():
            raise KeyError(speaker_id)
        embeddings, engine = load_meeting_embeddings(path)
        vec = embeddings.get(speaker_id)
        if vec is None:
            raise LookupError("this meeting has no stored embedding for that speaker")
        VoiceprintStore().save(name, vec, engine=engine)

    def delete_voiceprint(self, name: str) -> None:
        from micnaga.store.voiceprints import VoiceprintStore

        VoiceprintStore().delete(name)
