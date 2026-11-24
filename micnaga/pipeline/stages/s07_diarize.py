"""Etapa 7: diarizacion (spec 8.7). Pista del sistema y, en reuniones presenciales, el micro.

Un unico diarizador cargado (``processing.diarization``; solo ``diarize``). Si es ademas un
``SpeakerEmbedder`` y las huellas estan activadas (``privacy.voiceprints_enabled``), se calculan
aqui los embeddings de cada hablante (el modelo solo esta cargado ahora) para la etapa 10.

* **Sistema** (remotos ``S1``, ``S2``...): como siempre.
* **Micro** (reunion presencial: varias personas hablando por el mismo micro): si el micro tiene
  al menos ``room.MIN_MIC_SPEECH_S`` s de voz se diariza tambien. El hablante con mas energia es
  «Yo» (``me``) y el resto, si de verdad son otra voz (ni eco de un remoto, ni la misma voz, ni
  casi sin voz; ``engines.diar.room``), pasan a ser ``P<n>``. Con una sola persona en el micro
  todo sigue siendo «Yo», como antes. Sale en ``work/07_turns_mic.json`` (vacio = todo «Yo»).
  Si la sesion es una reunion presencial declarada («Grabar reunión presencial»,
  ``capture.session.is_in_person``) el umbral baja a ``IN_PERSON_MIN_MIC_SPEECH_S`` (5 s).

Post-filtro de hablantes fantasma (``engines.diar.postfilter``), igual en las dos pistas: un
hablante con < 3 s de voz o un solo turno de < 4 s se absorbe en el mas parecido (embeddings, si
el motor los da; solo se calculan si hay alguno que absorber y no se guardan) o en el del turno
mas cercano. Va **antes** de calcular las huellas, asi nunca se guarda la de un fantasma.
``info.n_speakers_raw`` es lo que dio el motor y ``info.absorbed`` los hablantes absorbidos.
``info.embedder`` es el espacio de las huellas (``embedding_name`` del motor o su ``name``);
``info.mic`` resume la diarizacion del micro.

Los embeddings son datos biometricos: se escriben **cifrados con DPAPI** desde el primer byte
(``store.voiceprints.write_protected_json``; ni el temporal va en claro). Si DPAPI falla se avisa
y la etapa sigue sin embeddings; nunca se escriben en claro. Las versiones antiguas dejaban
``work/07_embeddings.json`` en claro: se borra al empezar esta etapa (y el runner lo borra al
arrancar, lo que fuerza a repetir esta etapa en un trabajo reanudado).

Degradacion: si no hay diarizador instalado (``EngineUnavailableError``) la etapa termina sin
turnos y la etapa 8 etiqueta toda la pista remota como ``S1`` y todo el micro como «Yo» («sin
diarizacion», F1); ``info.diarizer`` es ``None`` y ``info.skipped`` dice por que. Sin voz no se
carga ningun modelo.

Salidas: ``work/07_turns.json``, ``work/07_turns_mic.json`` y, si procede,
``work/07_embeddings.dpapi``.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from micnaga.capture.session import is_in_person
from micnaga.pipeline.engines.diar import postfilter, room
from micnaga.pipeline.engines.diar.base import Diarizer, SpeakerEmbedder
from micnaga.pipeline.stages.base import StageContext, StageResult, engine_scope
from micnaga.registry import EngineUnavailableError
from micnaga.types import SPEAKER_ME, AudioArray, Turn

log = logging.getLogger(__name__)

TURNS = "07_turns.json"
MIC_TURNS = "07_turns_mic.json"
WORDS_ME = "06_words_me.json"
EMBEDDINGS = "07_embeddings.dpapi"
LEGACY_EMBEDDINGS = "07_embeddings.json"
"""Embeddings en claro de versiones antiguas: nunca se leen, solo se borran."""
IN_PERSON_MIN_MIC_SPEECH_S = 5.0
"""Voz minima del micro para diarizarlo en una reunion presencial declarada (en el resto,
``room.MIN_MIC_SPEECH_S``): el usuario dijo que habia gente en la sala."""


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


def _turns_json(turns: list[Turn]) -> list[dict[str, Any]]:
    return [{"speaker": t.speaker, "start": t.start, "end": t.end} for t in turns]


def _clean_turns(
    diarizer: Diarizer, emb_engine: SpeakerEmbedder | None, audio: AudioArray, sr: int
) -> tuple[list[Turn], list[Turn], list[str]]:
    """Diariza ``audio`` y aplica el post-filtro: ``(crudos, limpios, absorbidos)``."""
    raw = list(diarizer.diarize(audio, sr, None))
    minor = postfilter.speakers_to_absorb(raw)
    similarity: dict[str, Any] = {}
    if minor and emb_engine is not None and len({t.speaker for t in raw}) > 1:
        similarity = dict(emb_engine.embed(audio, sr, raw))  # solo en memoria
    turns = postfilter.absorb_minor_speakers(raw, similarity)
    similarity.clear()
    if minor:
        log.info(
            "absorbed %d minor speaker(s) into others (%d -> %d speakers)",
            len(minor),
            len({t.speaker for t in raw}),
            len({t.speaker for t in turns}),
        )
    return raw, turns, sorted(minor)


def mic_speech_s(words: list[Any]) -> float:
    """Segundos de voz del micro (suma de la duracion de sus palabras)."""
    return float(sum(max(0.0, float(w["e"]) - float(w["s"])) for w in words))


def _diarize_room(
    ctx: StageContext,
    diarizer: Diarizer,
    emb_engine: SpeakerEmbedder | None,
    first_number: int,
    remote_emb: dict[str, NDArray[np.float32]],
) -> tuple[list[Turn], dict[str, NDArray[np.float32]], dict[str, Any]]:
    """Turnos del micro (``me`` / ``P<n>``), embeddings de las ``P<n>`` e ``info``."""
    audio = ctx.conditioned("mic")
    raw, turns, minor = _clean_turns(diarizer, emb_engine, audio, ctx.sr)
    info: dict[str, Any] = {
        "n_speakers_raw": len({t.speaker for t in raw}),
        "absorbed": minor,
        "room_speakers": 0,
    }
    if len({t.speaker for t in turns}) < 2:
        return [], {}, info
    mic_emb = dict(emb_engine.embed(audio, ctx.sr, turns)) if emb_engine is not None else {}
    labelled, reason = room.label_room_speakers(
        turns,
        room.speech_energy(audio, ctx.sr, turns),
        postfilter.speech_by_speaker(turns),
        mic_emb,
        remote_emb,
        first_number,
    )
    room_ids = sorted({t.speaker for t in labelled if t.speaker != SPEAKER_ME})
    info["room_speakers"] = len(room_ids)
    info["reasons"] = dict(sorted(reason.items()))
    if not room_ids:
        return [], {}, info
    log.info("in-person meeting: %d other speaker(s) on the mic", len(room_ids))
    # huellas de las personas de la sala, en el mismo espacio que las de los remotos
    room_emb: dict[str, NDArray[np.float32]] = {}
    if ctx.config.privacy.voiceprints_enabled and emb_engine is not None:
        room_turns = [t for t in labelled if t.speaker in room_ids]
        room_emb = dict(emb_engine.embed(audio, ctx.sr, room_turns))
    del audio
    return labelled, room_emb, info


def run(ctx: StageContext) -> StageResult:
    # nada de una ejecucion anterior: ni en claro (versiones antiguas) ni cifrado obsoleto
    remove_legacy_embeddings(ctx.work)
    with contextlib.suppress(OSError):
        ctx.path(EMBEDDINGS).unlink(missing_ok=True)
    words: list[Any] = ctx.read_json("06_words_remote.json")
    mic_words: list[Any] = ctx.read_json(WORDS_ME) if ctx.exists(WORDS_ME) else []
    in_person = is_in_person(ctx.session, ctx.meta.extra)
    min_mic_s = IN_PERSON_MIN_MIC_SPEECH_S if in_person else room.MIN_MIC_SPEECH_S
    want_room = mic_speech_s(mic_words) >= min_mic_s
    ctx.write_json(MIC_TURNS, [])
    outputs = [TURNS, MIC_TURNS]
    if not words and not want_room:
        ctx.write_json(TURNS, [])
        return StageResult(outputs, {"diarizer": None, "skipped": "no remote speech"})
    try:
        diarizer = ctx.deps.create_diarizer(ctx.config)
    except EngineUnavailableError as exc:
        log.warning("diarizer unavailable, labelling the remote track as one speaker: %s", exc)
        ctx.write_json(TURNS, [])
        return StageResult(
            outputs, {"diarizer": None, "skipped": "unavailable", "detail": str(exc)}
        )
    name = diarizer.name
    embedder = str(getattr(diarizer, "embedding_name", name))
    embeddings_out: dict[str, list[float]] = {}
    raw_turns: list[Turn] = []
    turns: list[Turn] = []
    minor: list[str] = []
    mic_turns: list[Turn] = []
    mic_info: dict[str, Any] = {"skipped": "little mic speech"}
    voiceprints = ctx.config.privacy.voiceprints_enabled
    with engine_scope(diarizer):
        emb_engine = diarizer if isinstance(diarizer, SpeakerEmbedder) else None
        remote_emb: dict[str, NDArray[np.float32]] = {}
        if words:
            audio = ctx.conditioned("system")
            ctx.progress(0.1)
            raw_turns, turns, minor = _clean_turns(diarizer, emb_engine, audio, ctx.sr)
            ctx.progress(0.5)
            if (voiceprints or want_room) and turns and emb_engine is not None:
                remote_emb = dict(emb_engine.embed(audio, ctx.sr, turns))
            del audio
        if want_room:
            ctx.progress(0.6)
            mic_turns, room_emb, mic_info = _diarize_room(
                ctx,
                diarizer,
                emb_engine,
                room.next_speaker_number(turns, has_remote_words=bool(words)),
                remote_emb,
            )
            remote_emb.update(room_emb)
            room_emb.clear()
        ctx.progress(0.9)
        if voiceprints:
            for sid, vec in remote_emb.items():
                embeddings_out[sid] = [float(x) for x in vec]
        remote_emb.clear()
        del emb_engine
    del diarizer
    ctx.write_json(TURNS, _turns_json(turns))
    ctx.write_json(MIC_TURNS, _turns_json(mic_turns))
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
            "absorbed": minor,
            "has_embeddings": has_embeddings,
            "mic": mic_info,
            "in_person": in_person,
        },
    )
