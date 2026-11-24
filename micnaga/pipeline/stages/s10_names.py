"""Etapa 10: nombres (huellas de voz, MIC-11) y glosario personal (MIC-16) (spec 8.10).

* Huellas: solo si ``privacy.voiceprints_enabled``. Se compara el embedding de cada hablante
  (``07_embeddings.dpapi``, cifrado con DPAPI) con las huellas guardadas
  (coseno >= ``privacy.voiceprint_threshold``); si coincide, el hablante toma el nombre y
  ``voiceprint_match`` la similitud. Si dos hablantes casan con la misma persona gana el de
  mayor similitud. Los embeddings de la reunion se guardan
  cifrados (DPAPI) en su carpeta para poder aprender la huella al renombrar (``output``). El
  ``07_embeddings.json`` en claro de versiones antiguas nunca se lee: se borra. Si el fichero
  cifrado no se puede descifrar se avisa y se sigue sin huellas.
* Glosario: ``Glossary.load`` aplicado a las palabras de cada frase; el texto se
  reconstruye cuando cambian las palabras.

Salidas: ``work/10_phrases.json`` y ``work/10_names.json``
(``{"S1": {"name": "Laura", "voiceprint_match": 0.84}}``).
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from micnaga.glossary import Glossary
from micnaga.pipeline.stages.base import StageContext, StageResult
from micnaga.pipeline.stages.phrases import apply_glossary
from micnaga.pipeline.stages.s07_diarize import EMBEDDINGS, remove_legacy_embeddings
from micnaga.types import Segment

log = logging.getLogger(__name__)

PHRASES = "10_phrases.json"
NAMES = "10_names.json"


def match_voiceprints(
    ctx: StageContext, speakers: set[str]
) -> tuple[dict[str, dict[str, Any]], int]:
    """``({speaker_id: {"name", "voiceprint_match"}}, n_embeddings)``."""
    remove_legacy_embeddings(ctx.work)  # en claro (versiones antiguas): nunca se lee
    if not ctx.config.privacy.voiceprints_enabled or not ctx.exists(EMBEDDINGS):
        return {}, 0
    from micnaga.store.voiceprints import (
        VoiceprintError,
        VoiceprintStore,
        read_protected_json,
        save_meeting_embeddings,
    )

    try:
        raw = read_protected_json(ctx.path(EMBEDDINGS))
        embeddings = {str(k): np.asarray(v, dtype=np.float32) for k, v in dict(raw).items()}
    except (VoiceprintError, OSError, TypeError, ValueError) as exc:
        log.warning("speaker embeddings unreadable, skipping voiceprints: %s", exc)
        return {}, 0
    info = ctx.stage_info("diarize")
    engine = str(info.get("embedder") or info.get("diarizer") or "")  # espacio de las huellas
    store = ctx.deps.voiceprints or VoiceprintStore()
    threshold = ctx.config.privacy.voiceprint_threshold
    best: dict[str, tuple[str, float]] = {}  # nombre -> (hablante, similitud)
    for sid, vec in embeddings.items():
        if sid not in speakers:
            continue
        try:
            found = store.match(vec, threshold=threshold, engine=engine)
        except (VoiceprintError, ValueError) as exc:
            log.warning("voiceprint match failed for %s: %s", sid, exc)
            continue
        if found is None:
            continue
        name, score = found
        if name not in best or score > best[name][1]:
            best[name] = (sid, score)
    names = {
        sid: {"name": name, "voiceprint_match": round(score, 4)}
        for name, (sid, score) in best.items()
    }
    try:
        save_meeting_embeddings(ctx.session, embeddings, engine=engine)
    except Exception as exc:  # no debe tumbar el procesado
        log.warning("could not store speaker embeddings: %s", exc)
    return names, len(embeddings)


def run(ctx: StageContext) -> StageResult:
    phrases = [Segment.from_dict(d) for d in ctx.read_json("09_phrases.json")]
    names, n_emb = match_voiceprints(ctx, {p.speaker for p in phrases})
    glossary = Glossary.load(config=ctx.config)
    out = [apply_glossary(p, glossary) for p in phrases]
    changed = sum(p.text != q.text for p, q in zip(phrases, out, strict=True))
    ctx.write_json(PHRASES, [p.to_dict() for p in out])
    ctx.write_json(NAMES, names)
    return StageResult(
        [PHRASES, NAMES],
        {"voiceprint_matches": len(names), "embeddings": n_emb, "glossary_changed": changed},
    )
