"""Etapa 6: transcripcion por bloque (spec 8.6).

Se crea **un** motor ASR (``processing.asr_engine``; los hilos los toma el motor de
``processing.threads``), se transcribe cada bloque de las dos pistas (los bloques de la etapa 5) y
los tiempos de cada palabra, relativos al bloque, se desplazan a la linea de tiempo global. Despues
se cierra el motor, ``del`` y ``gc.collect()``.

Micro y sistema van **en paralelo** (dos hilos sobre el mismo modelo cargado: la memoria no se
duplica) si el motor lo admite (``concurrent = True``, Parakeet/onnxruntime). Mientras un hilo
decodifica (bucle Python de TDT) el otro usa el codificador; ver DECISIONS §23.

Salidas: ``work/06_words_me.json`` y ``work/06_words_remote.json`` (listas de palabras
``{"w","s","e","p"}``); el nombre del motor queda en la marca (``info.engine``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import Any

from micnaga.pipeline.engines.asr.base import AsrEngine
from micnaga.pipeline.stages.base import StageContext, StageResult, engine_scope
from micnaga.types import AudioArray, Word

WORDS_ME = "06_words_me.json"
WORDS_REMOTE = "06_words_remote.json"
MIN_CHUNK_S = 0.1


def transcribe_piece(engine: AsrEngine, piece: AudioArray, a: float, sr: int) -> list[Word]:
    """Palabras del audio ``piece`` que empieza en ``a`` s de la pista, en tiempo de la pista."""
    words: list[Word] = []
    for w in engine.transcribe(piece, sr):
        if not w.w.strip():
            continue
        words.append(Word(w.w.strip(), w.s + a, max(w.e, w.s) + a, w.p))
    return words


def transcribe_block(
    engine: AsrEngine, audio: AudioArray, a: float, b: float, sr: int
) -> list[Word]:
    """Palabras de un bloque ``[a, b)`` (s) de ``audio`` (pista entera), en tiempo de la pista."""
    if b - a < MIN_CHUNK_S:
        return []
    i0, i1 = max(0, int(a * sr)), min(len(audio), int(b * sr))
    return transcribe_piece(engine, audio[i0:i1], a, sr)


def run_tracks(tasks: Sequence[Callable[[], Any]], *, parallel: bool) -> list[Any]:
    """Ejecuta las tareas (una por pista) en hilos o en serie; propaga el primer error."""
    if not parallel or len(tasks) < 2:
        return [t() for t in tasks]
    results: list[Any] = [None] * len(tasks)
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            results[i] = tasks[i]()
        except BaseException as exc:  # se relanza en el hilo principal
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(len(tasks))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    if errors:
        raise errors[0]
    return results


def run(ctx: StageContext) -> StageResult:
    vad: dict[str, Any] = ctx.read_json("05_vad.json")
    sr = ctx.sr
    total = sum(b - a for t in ("mic", "system") for a, b in vad[t]["chunks"]) or 1.0
    done = [0.0]
    lock = threading.Lock()
    outputs = {"mic": WORDS_ME, "system": WORDS_REMOTE}
    engine = ctx.deps.create_asr(ctx.config)
    name = engine.name
    parallel = bool(getattr(engine, "concurrent", False))

    def track_task(track: str) -> Callable[[], int]:
        def task() -> int:
            chunks: list[list[float]] = vad[track]["chunks"]
            words: list[Word] = []
            audio = ctx.conditioned(track) if chunks else None
            for a, b in chunks:
                if audio is not None:
                    words.extend(transcribe_block(engine, audio, a, b, sr))
                with lock:
                    done[0] += b - a
                    ctx.progress(done[0] / total)
            words.sort(key=lambda w: (w.s, w.e))
            ctx.write_json(outputs[track], [w.to_dict() for w in words])
            return len(words)

        return task

    with engine_scope(engine):
        counts = run_tracks([track_task("mic"), track_task("system")], parallel=parallel)
    return StageResult(
        [WORDS_ME, WORDS_REMOTE],
        {
            "engine": name,
            "words": {"mic": counts[0], "system": counts[1]},
            "parallel": parallel,
        },
    )
