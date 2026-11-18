"""Etapa 10: nombres (huellas de voz, MIC-11) y glosario personal (MIC-16) (spec 8.10).

* Huellas: solo si ``privacy.voiceprints_enabled``. Se compara el embedding de cada hablante
  (``07_embeddings.json``) con las huellas guardadas (coseno >= ``privacy.voiceprint_threshold``);
  si coincide, el hablante toma el nombre y ``voiceprint_match`` la similitud. Si dos hablantes
  casan con la misma persona gana el de mayor similitud. Los embeddings de la reunion se guardan
  cifrados (DPAPI) en su carpeta para poder aprender la huella al renombrar (``output``).
* Glosario: ``Glossary.load`` (``glossary.txt`` + ``processing.glossary``) aplicado a las palabras
  de cada frase; el texto de la frase se reconstruye con las palabras corregidas.

Salidas: ``work/10_phrases.json`` y ``work/10_names.json``
(``{"S1": {"name": "Laura", "voiceprint_match": 0.84}}``).
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from micnaga.glossary import Glossary
from micnaga.pipeline.stages.base import StageContext, StageResult
from micnaga.pipeline.stages.phrases import words_text
from micnaga.types import Segment

log = logging.getLogger(__name__)

PHRASES = "10_phrases.json"
NAMES = "10_names.json"


def match_voiceprints(
    ctx: StageContext, speakers: set[str]
) -> tuple[dict[str, dict[str, Any]], int]:
    """``({speaker_id: {"name", "voiceprint_match"}}, n_embeddings)``."""
    if not ctx.config.privacy.voiceprints_enabled or not ctx.exists("07_embeddings.json"):
        return {}, 0
    from micnaga.store.voiceprints import VoiceprintError, VoiceprintStore, save_meeting_embeddings

    embeddings = {
        k: np.asarray(v, dtype=np.float32) for k, v in ctx.read_json("07_embeddings.json").items()
    }
    engine = str(ctx.stage_info("diarize").get("diarizer") or "")
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
    changed = 0
    out: list[Segment] = []
    for p in phrases:
        if len(glossary) and p.words:
            words = glossary.apply_words(p.words)
            text = words_text(words)
            if text != p.text:
                changed += 1
            out.append(Segment(p.start, p.end, p.speaker, text, words))
        elif len(glossary):
            text = glossary.apply_text(p.text)
            changed += text != p.text
            out.append(Segment(p.start, p.end, p.speaker, text, p.words))
        else:
            out.append(p)
    ctx.write_json(PHRASES, [p.to_dict() for p in out])
    ctx.write_json(NAMES, names)
    return StageResult(
        [PHRASES, NAMES],
        {"voiceprint_matches": len(names), "embeddings": n_emb, "glossary_changed": changed},
    )
