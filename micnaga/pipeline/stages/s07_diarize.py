"""Etapa 7: diarizacion de la pista remota (spec 8.7). Solo la pista del sistema.

Un unico diarizador cargado (``processing.diarization``). Si es ademas un ``SpeakerEmbedder`` y
las huellas estan activadas (``privacy.voiceprints_enabled``), se calculan aqui los embeddings de
cada hablante (el modelo solo esta cargado ahora) para la etapa 10.

Post-filtro de hablantes fantasma (``engines.diar.postfilter``), igual para todos los motores: un
hablante con < 3 s de voz o un solo turno de < 4 s se absorbe en el mas parecido (embeddings, si
el motor los da; solo se calculan si hay alguno que absorber y no se guardan) o en el del turno
mas cercano. Va **antes** de calcular las huellas, asi nunca se guarda la de un fantasma.
``info.n_speakers_raw`` es lo que dio el motor y ``info.absorbed`` los hablantes absorbidos.
``info.embedder`` es el espacio de las huellas (``embedding_name`` del motor o su ``name``).

Los embeddings son datos biometricos: se escriben **cifrados con DPAPI** desde el primer byte
(``store.voiceprints.write_protected_json``; ni el temporal va en claro). Si DPAPI falla se avisa
y la etapa sigue sin embeddings; nunca se escriben en claro. Las versiones antiguas dejaban
``work/07_embeddings.json`` en claro: se borra al empezar esta etapa (y el runner lo borra al
arrancar, lo que fuerza a repetir esta etapa en un trabajo reanudado).

Degradacion: si no hay diarizador instalado (``EngineUnavailableError``) la etapa termina sin
turnos y la etapa 8 etiqueta toda la pista remota como ``S1`` («sin diarizacion», F1);
``info.diarizer`` es ``None`` y ``info.skipped`` dice por que. Sin palabras remotas no se carga
ningun modelo.

Salidas: ``work/07_turns.json`` y, si procede, ``work/07_embeddings.dpapi``.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

from micnaga.pipeline.engines.diar import postfilter
from micnaga.pipeline.engines.diar.base import SpeakerEmbedder
from micnaga.pipeline.stages.base import StageContext, StageResult, engine_scope
from micnaga.registry import EngineUnavailableError

log = logging.getLogger(__name__)

TURNS = "07_turns.json"
EMBEDDINGS = "07_embeddings.dpapi"
LEGACY_EMBEDDINGS = "07_embeddings.json"
"""Embeddings en claro de versiones antiguas: nunca se leen, solo se borran."""


def remove_legacy_embeddings(work: Path) -> bool:
    """Borra ``work/07_embeddings.json`` (en claro, versiones antiguas). ``True`` si existia."""
    legacy = work / LEGACY_EMBEDDINGS
    if not legacy.is_file():
        return False
    try:
        legacy.unlink()
    except OSError as exc:
        log.warning("could not delete legacy plaintext speaker embeddings: %s", exc)
        return False
    log.info("deleted legacy plaintext speaker embeddings")
    return True


def _write_embeddings(ctx: StageContext, embeddings: dict[str, list[float]]) -> bool:
    """Escribe los embeddings cifrados; ``False`` (y aviso) si no se pudo cifrar o escribir."""
    from micnaga.store.voiceprints import VoiceprintError, write_protected_json

    try:
        write_protected_json(ctx.path(EMBEDDINGS), embeddings)
    except (VoiceprintError, ImportError, OSError) as exc:  # ImportError: sin win32crypt
        log.warning("speaker embeddings skipped, could not store them encrypted: %s", exc)
        with contextlib.suppress(OSError):
            ctx.path(EMBEDDINGS).unlink(missing_ok=True)
        return False
    return True


def run(ctx: StageContext) -> StageResult:
    # nada de una ejecucion anterior: ni en claro (versiones antiguas) ni cifrado obsoleto
    remove_legacy_embeddings(ctx.work)
    with contextlib.suppress(OSError):
        ctx.path(EMBEDDINGS).unlink(missing_ok=True)
    words: list[Any] = ctx.read_json("06_words_remote.json")
    if not words:
        ctx.write_json(TURNS, [])
        return StageResult([TURNS], {"diarizer": None, "skipped": "no remote speech"})
    try:
        diarizer = ctx.deps.create_diarizer(ctx.config)
    except EngineUnavailableError as exc:
        log.warning("diarizer unavailable, labelling the remote track as one speaker: %s", exc)
        ctx.write_json(TURNS, [])
        return StageResult(
            [TURNS], {"diarizer": None, "skipped": "unavailable", "detail": str(exc)}
        )
    name = diarizer.name
    embedder = str(getattr(diarizer, "embedding_name", name))
    outputs = [TURNS]
    embeddings_out: dict[str, list[float]] = {}
    with engine_scope(diarizer):
        audio = ctx.conditioned("system")
        ctx.progress(0.1)
        raw_turns = diarizer.diarize(audio, ctx.sr, None)
        ctx.progress(0.7)
        emb_engine = diarizer if isinstance(diarizer, SpeakerEmbedder) else None
        minor = postfilter.minor_speakers(raw_turns)
        similarity: dict[str, Any] = {}
        if minor and emb_engine is not None and len({t.speaker for t in raw_turns}) > 1:
            similarity = dict(emb_engine.embed(audio, ctx.sr, raw_turns))  # solo en memoria
        turns = postfilter.absorb_minor_speakers(raw_turns, similarity)
        similarity.clear()
        if minor:
            log.info(
                "absorbed %d minor speaker(s) into others (%d -> %d speakers)",
                len(minor),
                len({t.speaker for t in raw_turns}),
                len({t.speaker for t in turns}),
            )
        ctx.progress(0.8)
        if ctx.config.privacy.voiceprints_enabled and turns and emb_engine is not None:
            for sid, vec in emb_engine.embed(audio, ctx.sr, turns).items():
                embeddings_out[sid] = [float(x) for x in vec]
        del audio, emb_engine
    del diarizer
    ctx.write_json(TURNS, [{"speaker": t.speaker, "start": t.start, "end": t.end} for t in turns])
    has_embeddings = bool(embeddings_out) and _write_embeddings(ctx, embeddings_out)
    embeddings_out.clear()
    if has_embeddings:
        outputs.append(EMBEDDINGS)
    return StageResult(
        outputs,
        {
            "diarizer": name,
            "embedder": embedder,
            "n_speakers": len({t.speaker for t in turns}),
            "n_speakers_raw": len({t.speaker for t in raw_turns}),
            "absorbed": sorted(minor),
            "has_embeddings": has_embeddings,
        },
    )
