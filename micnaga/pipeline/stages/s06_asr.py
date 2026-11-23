"""Etapa 6: transcripcion por bloque (spec 8.6).

Se crea **un** motor ASR (``processing.asr_engine``; los hilos los toma el motor de
``processing.threads``), se transcribe cada bloque de las dos pistas (los bloques de la etapa 5) y
los tiempos de cada palabra, relativos al bloque, se desplazan a la linea de tiempo global. Despues
se cierra el motor, ``del`` y ``gc.collect()``.

Micro y sistema van **en serie** con un solo modelo: medido (DECISIONS §23), pasarlos en
paralelo (un modelo compartido o dos con la mitad de hilos cada uno) no gana tiempo y duplica la
memoria; el cuello es el codificador, no el bucle de decodificacion.

**Silencios dentro del bloque** (``compact``): un bloque va del primer tramo de voz al ultimo y
puede llevar silencios largos (el otro habla en la otra pista). Whisper inventa frases en esos
huecos («Gracias por ver el video») y despues sigue sin puntuacion; asi que, con los tramos de
la etapa 5, cada silencio de mas de ``MAX_GAP_S`` se acorta a ``MAX_GAP_S`` (se conserva audio
real a cada lado) antes de transcribir, y los tiempos de las palabras se devuelven a la linea de
tiempo de la pista. Lo usan la etapa 6, el vivo y «Mejorar transcripcion». La etapa 5 agrupa
los bloques contando cada silencio como mucho ``MAX_GAP_S`` (``vad.group_regions``): el audio
compacto llena la ventana de 30 s de Whisper y hacen falta menos ventanas.

Salidas: ``work/06_words_me.json`` y ``work/06_words_remote.json`` (listas de palabras
``{"w","s","e","p"}``); el nombre del motor queda en la marca (``info.engine``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from micnaga.pipeline import vad
from micnaga.pipeline.engines.asr.base import AsrEngine
from micnaga.pipeline.stages.base import StageContext, StageResult, engine_scope
from micnaga.types import AudioArray, Word

WORDS_ME = "06_words_me.json"
WORDS_REMOTE = "06_words_remote.json"
MIN_CHUNK_S = 0.1
MAX_GAP_S = vad.MAX_GAP_S
"""Silencio maximo que se deja dentro de un bloque entre dos tramos de voz (ver ``compact``); el
mismo con el que la etapa 5 agrupa los bloques, asi el audio compacto no pasa de 30 s."""

Span = tuple[float, float]
Piece = tuple[float, float, float]
"""``(inicio en el audio compacto, inicio en la pista, duracion)`` en segundos."""


def compact(
    piece: AudioArray, a: float, regions: Sequence[Span], sr: int, max_gap_s: float = MAX_GAP_S
) -> tuple[AudioArray, list[Piece]]:
    """Acorta a ``max_gap_s`` los silencios de ``piece`` (que empieza en ``a`` s de la pista).

    ``regions`` son los tramos de voz de la pista (segundos de la pista). De cada silencio largo
    se conserva la mitad de ``max_gap_s`` a cada lado (tambien al principio y al final del
    trozo). Devuelve el audio y el mapa de trozos
    para ``to_track_time``; sin silencios largos devuelve ``piece`` tal cual."""
    dur = len(piece) / sr
    spans = sorted((max(0.0, s - a), min(dur, e - a)) for s, e in regions if e > a and s < a + dur)
    spans = [(s, e) for s, e in spans if e > s]
    half = max_gap_s / 2
    cuts: list[Span] = []  # tramos (relativos a ``piece``) que se quitan
    prev_end = 0.0
    for s, e in [*spans, (dur, dur)]:
        lo, hi = prev_end + half, s - half
        if spans and hi - lo > 1.0 / sr:
            cuts.append((lo, hi))
        prev_end = max(prev_end, e)
    if not cuts:
        return piece, [(0.0, a, dur)]
    keep: list[AudioArray] = []
    pieces: list[Piece] = []
    out_t, src_t = 0.0, 0.0
    for lo, hi in [*cuts, (dur, dur)]:
        i0, i1 = round(src_t * sr), round(lo * sr)
        if i1 > i0:
            keep.append(piece[i0:i1])
            pieces.append((out_t, a + src_t, (i1 - i0) / sr))
            out_t += (i1 - i0) / sr
        src_t = hi
    return np.concatenate(keep).astype(np.float32, copy=False), pieces


def to_track_time(t: float, pieces: Sequence[Piece]) -> float:
    """Tiempo del audio compacto -> tiempo de la pista (en un corte, el borde del trozo)."""
    best = pieces[0][1]
    for start, track, length in pieces:
        if t < start:
            break
        best = track + min(t - start, length)
    return best


def to_track_span(s: float, e: float, pieces: Sequence[Piece]) -> tuple[float, float]:
    """``(inicio, fin)`` de una palabra en la pista. El fin no pasa del trozo donde empieza: una
    palabra cuyo final (estirado por el modelo) cruza un corte no se alarga con todo el silencio
    quitado (sus tiempos van a la asignacion de hablante, a los parrafos y al SRT)."""
    start = to_track_time(s, pieces)
    home = pieces[0]
    for piece in pieces:  # el trozo donde empieza: el ultimo cuyo inicio no pasa de ``s``
        if s < piece[0]:
            break
        home = piece
    end = min(to_track_time(max(e, s), pieces), home[1] + home[2])
    return start, max(end, start)


def transcribe_piece(
    engine: AsrEngine,
    piece: AudioArray,
    a: float,
    sr: int,
    regions: Sequence[Span] | None = None,
) -> list[Word]:
    """Palabras del audio ``piece`` que empieza en ``a`` s de la pista, en tiempo de la pista.

    Con ``regions`` (tramos de voz de la pista) se acortan antes los silencios largos
    (``compact``)."""
    pieces: list[Piece] = [(0.0, a, len(piece) / sr)]
    if regions is not None:
        piece, pieces = compact(piece, a, regions, sr)
    words: list[Word] = []
    for w in engine.transcribe(piece, sr):
        if not w.w.strip():
            continue
        s, e = to_track_span(w.s, w.e, pieces)
        words.append(Word(w.w.strip(), s, e, w.p))
    return words


def transcribe_block(
    engine: AsrEngine,
    audio: AudioArray,
    a: float,
    b: float,
    sr: int,
    regions: Sequence[Span] | None = None,
) -> list[Word]:
    """Palabras de un bloque ``[a, b)`` (s) de ``audio`` (pista entera), en tiempo de la pista."""
    if b - a < MIN_CHUNK_S:
        return []
    i0, i1 = max(0, int(a * sr)), min(len(audio), int(b * sr))
    return transcribe_piece(engine, audio[i0:i1], a, sr, regions)


def compact_seconds(a: float, b: float, regions: Sequence[Span] | None) -> float:
    """Duracion del bloque ``[a, b)`` una vez acortados sus silencios (lo que paga el ASR)."""
    if regions is None:
        return b - a
    spans = sorted((max(a, s), min(b, e)) for s, e in regions if e > a and s < b)
    total, prev = 0.0, None
    for s, e in spans:
        if prev is not None and s > prev:
            total += min(s - prev, MAX_GAP_S)
        total += max(0.0, e - max(s, prev if prev is not None else s))
        prev = e if prev is None else max(prev, e)
    return total if spans else b - a


def track_regions(vad: dict[str, Any], track: str) -> list[Span] | None:
    """Tramos de voz de ``05_vad.json`` (``None`` si el fichero es de una version sin ellos)."""
    raw = vad.get(track, {}).get("regions")
    if raw is None:
        return None
    return [(float(s), float(e)) for s, e in raw]


def run(ctx: StageContext) -> StageResult:
    vad: dict[str, Any] = ctx.read_json("05_vad.json")
    sr = ctx.sr
    total = sum(b - a for t in ("mic", "system") for a, b in vad[t]["chunks"]) or 1.0
    done = 0.0
    counts: dict[str, int] = {}
    outputs = {"mic": WORDS_ME, "system": WORDS_REMOTE}
    engine = ctx.deps.create_asr(ctx.config)
    name = engine.name
    with engine_scope(engine):
        for track in ("mic", "system"):
            chunks: list[list[float]] = vad[track]["chunks"]
            regions = track_regions(vad, track)
            words: list[Word] = []
            audio = ctx.conditioned(track) if chunks else None
            for a, b in chunks:
                if audio is not None:
                    words.extend(transcribe_block(engine, audio, a, b, sr, regions))
                done += b - a
                ctx.progress(done / total)
            words.sort(key=lambda w: (w.s, w.e))
            ctx.write_json(outputs[track], [w.to_dict() for w in words])
            counts[track] = len(words)
            del audio
    return StageResult([WORDS_ME, WORDS_REMOTE], {"engine": name, "words": counts})
