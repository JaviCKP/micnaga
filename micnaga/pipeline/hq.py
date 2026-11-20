"""«Mejorar transcripcion»: re-transcribe una reunion entera con un Whisper grande (CPU).

Bajo demanda, desde la cabecera de la reunion. Corre en un **proceso aparte** (``micnaga hq``,
prioridad BELOW_NORMAL, como el worker) y deja su estado en ``hq_estado.json`` dentro de la
carpeta; la biblioteca lo lee para la linea de progreso. Cancelar = el fichero ``hq.cancelar``
(se mira entre bloques y durante la descarga). Nada se escribe hasta el final, asi que un cierre
brusco del proceso deja la transcripcion intacta.

* **Modelo**: ``processing.hq_model`` (``large-v3`` por defecto, ``large-v3-turbo`` como opcion;
  DECISIONS §HQ). Se descarga al primer uso con el descargador de ``micnaga.models`` (URL con
  commit fijado, tamano y SHA-256).
* **Que se decodifica**: solo los bloques con voz de la etapa 5 (``work/05_vad.json``). Si
  ``work/`` ya no existe (el worker lo borra al terminar) se rehacen las etapas 1-5 con el codigo
  de siempre y se borra al final.
* **Guardas contra alucinaciones**: umbrales de faster-whisper (no_speech / logprob /
  compression ratio), segmentos descartados por esos mismos criterios y ``drop_loops`` (bucles de
  n-gramas repetidos).
* **Hablantes**: se conservan la diarizacion y los nombres. Las palabras del micro son «Yo»; las
  del sistema van al hablante remoto cuyo tramo actual solapa (o queda mas cerca) en el tiempo.
  Despues, las etapas 9 y 11 de siempre (eco y parrafos), marcadores intactos, MD/SRT y FTS.
* **Previa**: antes de escribir, ``transcripcion.previa.json`` (y ``limpieza.previa.json`` si la
  limpieza con IA ya habia pasado). «Volver a la anterior» (``restore``) los devuelve.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from micnaga import paths
from micnaga.config import Config
from micnaga.pipeline.stages.phrases import (
    build_phrases,
    default_speaker_name,
    drop_echo,
    merge_segments,
)
from micnaga.types import SPEAKER_ME, Segment, Speaker, Transcript, Word

log = logging.getLogger(__name__)

PREVIA = "transcripcion.previa.json"
CLEAN_MARKER = "limpieza.json"
CLEAN_PREVIA = "limpieza.previa.json"
STATUS = "hq_estado.json"
CANCEL = "hq.cancelar"

MODELS: dict[str, str] = {
    "large-v3-turbo": "faster-whisper-large-v3-turbo",
    "large-v3": "faster-whisper-large-v3",
}
DEFAULT_MODEL = "large-v3"

# Tiempo real medido en este equipo (20 hilos logicos, 6 hilos de proceso), segundos de calculo
# por segundo de voz; DECISIONS §HQ. Solo para el «~N min» del aviso.
RTF: dict[str, float] = {"large-v3-turbo": 0.35, "large-v3": 0.6}

NO_SPEECH_THRESHOLD = 0.6
LOG_PROB_THRESHOLD = -1.0
COMPRESSION_RATIO_THRESHOLD = 2.4
MAX_GAP_TO_TURN_S = 3.0


MIN_KEPT_RATIO = 0.3
"""Una mejora que deja menos de este tanto de las palabras de antes se rechaza (revision
externa: una mejora paso de seis palabras a cero y se publico)."""


class Cancelled(Exception):
    """Se pidio cancelar (``hq.cancelar``)."""


class HqRejected(Exception):
    """La transcripcion mejorada salio vacia o mucho mas corta: no se publica."""


class AsrLike(Protocol):
    name: str

    def transcribe(self, audio: Any, sr: int) -> list[Word]: ...

    def close(self) -> None: ...


Progress = Callable[[str, float], None]


# ----------------------------------------------------------------------------- guardas


def drop_loops(words: Sequence[Word], *, max_n: int = 4, min_repeats: int = 3) -> list[Word]:
    """Quita bucles de alucinacion: un n-grama (n <= ``max_n``) repetido ``min_repeats`` veces o
    mas seguidas se deja en una sola aparicion. Compara texto en minusculas sin puntuacion."""

    def key(w: Word) -> str:
        return "".join(c for c in w.w.lower() if c.isalnum())

    out = list(words)
    changed = True
    while changed:
        changed = False
        keys = [key(w) for w in out]
        for n in range(1, max_n + 1):
            i = 0
            while i + n * min_repeats <= len(out):
                gram = keys[i : i + n]
                reps = 1
                while keys[i + reps * n : i + (reps + 1) * n] == gram:
                    reps += 1
                if reps >= min_repeats and any(gram):
                    del out[i + n : i + reps * n]
                    del keys[i + n : i + reps * n]
                    changed = True
                i += 1
    return out


# ----------------------------------------------------------------------------- hablantes


def _remote_turns(transcript: Transcript) -> list[tuple[float, float, str]]:
    """Tramos ``(inicio, fin, hablante)`` de los hablantes remotos (por palabra si las hay)."""
    turns: list[tuple[float, float, str]] = []
    for seg in transcript.segments:
        if seg.speaker == SPEAKER_ME:
            continue
        if seg.words:
            turns.extend((w.s, max(w.e, w.s), seg.speaker) for w in seg.words)
        else:
            turns.append((seg.start, seg.end, seg.speaker))
    turns.sort()
    return turns


def remote_speaker_for(word: Word, turns: Sequence[tuple[float, float, str]]) -> str | None:
    """Hablante del tramo que mas solapa con ``word``; si ninguno, el mas cercano (<= 3 s)."""
    best: tuple[float, str] | None = None
    near: tuple[float, str] | None = None
    for a, b, sp in turns:
        ov = min(b, word.e) - max(a, word.s)
        if ov > 0 and (best is None or ov > best[0]):
            best = (ov, sp)
        gap = max(a - word.e, word.s - b, 0.0)
        if near is None or gap < near[0]:
            near = (gap, sp)
    if best is not None:
        return best[1]
    if near is not None and near[0] <= MAX_GAP_TO_TURN_S:
        return near[1]
    return None


def assign_words(
    transcript: Transcript, mic: Sequence[Word], system: Sequence[Word]
) -> list[tuple[str, Word]]:
    """Palabras nuevas con hablante: micro = «Yo»; sistema = tramo remoto actual por tiempo.

    Una palabra del sistema lejos de cualquier tramo toma el hablante de la anterior (o el remoto
    mas frecuente si es la primera)."""
    out: list[tuple[str, Word]] = [(SPEAKER_ME, w) for w in mic]
    turns = _remote_turns(transcript)
    counts: dict[str, float] = {}
    for a, b, sp in turns:
        counts[sp] = counts.get(sp, 0.0) + (b - a)
    fallback = max(counts, key=lambda k: counts[k]) if counts else "S1"
    prev: str | None = None
    for w in sorted(system, key=lambda w: (w.s, w.e)):
        sp = remote_speaker_for(w, turns) or prev or fallback
        out.append((sp, w))
        prev = sp
    return out


def _round(s: Segment) -> Segment:
    words = [Word(w.w, round(w.s, 3), round(w.e, 3), round(w.p, 3)) for w in s.words]
    return Segment(round(s.start, 3), round(s.end, 3), s.speaker, s.text, words)


def rebuild(
    transcript: Transcript,
    mic: Sequence[Word],
    system: Sequence[Word],
    config: Config,
    engine_name: str,
) -> Transcript:
    """Transcripcion nueva con los hablantes, nombres y marcadores de ``transcript``."""
    phrases = build_phrases(assign_words(transcript, mic, system))
    kept, _dropped = drop_echo(phrases, similarity=config.processing.echo_similarity)
    segments = [_round(s) for s in merge_segments(kept, merge_gap_s=config.processing.merge_gap_s)]
    speakers = list(transcript.speakers)
    known = {s.id for s in speakers}
    for seg in segments:
        if seg.speaker not in known:
            speakers.append(Speaker(seg.speaker, default_speaker_name(seg.speaker)))
            known.add(seg.speaker)
    return replace(
        transcript,
        segments=segments,
        speakers=speakers,
        engines=replace(transcript.engines, asr=engine_name),
    )


# ----------------------------------------------------------------------------- estado


def write_status(folder: Path, **fields: Any) -> None:
    from micnaga.pipeline.stages.base import atomic_write_json

    with contextlib.suppress(OSError):
        atomic_write_json(folder / STATUS, {**fields, "t": time.time()})


def read_status(folder: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((folder / STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def request_cancel(folder: Path) -> None:
    with contextlib.suppress(OSError):
        (folder / CANCEL).write_text("1", encoding="utf-8")


def has_previa(folder: Path) -> bool:
    return (folder / PREVIA).is_file()


def speech_seconds(folder: Path) -> float | None:
    """Segundos de voz segun la etapa 5 (o, sin ``work/``, la duracion de las frases)."""
    vad = paths.work_dir(folder) / "05_vad.json"
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
        data = json.loads(vad.read_text(encoding="utf-8"))
        return float(sum(b - a for t in ("mic", "system") for a, b in data[t]["chunks"]))
    tr = folder / paths.TRANSCRIPT_JSON
    with contextlib.suppress(OSError, ValueError, KeyError):
        t = Transcript.from_json(tr.read_text(encoding="utf-8"))
        return float(sum(s.end - s.start for s in t.segments)) * 1.15
    return None


def estimate_minutes(folder: Path, model: str) -> int:
    secs = speech_seconds(folder) or 0.0
    return max(1, round(secs * RTF.get(model, 1.0) / 60.0 + 0.5))


# ----------------------------------------------------------------------------- previa


def _reindex(folder: Path, transcript: Transcript) -> None:
    from micnaga.pipeline import output

    output._reindex(folder, transcript, None)


def _has_words(file: Path) -> bool:
    try:
        return _count_words(Transcript.from_json(file.read_text(encoding="utf-8"))) > 0
    except (OSError, ValueError, KeyError, TypeError):
        return False


def save_previa(folder: Path) -> None:
    """Copia la transcripcion actual (y la marca de limpieza) a ``*.previa.json``.

    Una transcripcion actual sin palabras nunca pisa una previa que si las tiene: se conserva
    la copia buena para que «Volver a la anterior» no devuelva una reunion vacia."""
    current, previa = folder / paths.TRANSCRIPT_JSON, folder / PREVIA
    if previa.is_file() and not _has_words(current) and _has_words(previa):
        log.info("current transcript is empty; the previous backup is kept")
        (folder / CLEAN_MARKER).unlink(missing_ok=True)  # la limpieza debe volver a pasar
        return
    shutil.copy2(current, previa)
    marker = folder / CLEAN_MARKER
    if marker.is_file():
        os.replace(marker, folder / CLEAN_PREVIA)
    else:
        (folder / CLEAN_PREVIA).unlink(missing_ok=True)


def restore(folder: Path) -> bool:
    """«Volver a la anterior»: devuelve la previa (y su limpieza), MD/SRT y FTS."""
    from micnaga.pipeline import output

    previa = folder / PREVIA
    if not previa.is_file():
        return False
    transcript = Transcript.from_json(previa.read_text(encoding="utf-8"))
    output.write_transcript(folder, transcript)
    clean = folder / CLEAN_PREVIA
    if clean.is_file():
        os.replace(clean, folder / CLEAN_MARKER)
    else:
        (folder / CLEAN_MARKER).unlink(missing_ok=True)
    previa.unlink()
    _reindex(folder, transcript)
    (folder / STATUS).unlink(missing_ok=True)
    return True


# ----------------------------------------------------------------------------- trabajo


@dataclass
class HqResult:
    words_before: int
    words_after: int
    seconds: float
    engine: str


def _count_words(t: Transcript) -> int:
    return sum(len(s.text.split()) for s in t.segments)


def _validate(words_before: int, words_after: int, segments_after: int) -> None:
    """Lanza ``HqRejected`` si la mejora salio vacia o con mucho menos texto que antes."""
    if segments_after == 0 or words_after == 0:
        raise HqRejected(f"improved transcript is empty ({words_before} words before)")
    if words_before > 0 and words_after < MIN_KEPT_RATIO * words_before:
        raise HqRejected(f"improved transcript too short: {words_before} -> {words_after} words")


def _ensure_vad(folder: Path, config: Config, progress: Progress) -> bool:
    """Asegura ``work/04_*.wav`` y ``work/05_vad.json``. ``True`` si se creo ``work/`` aqui."""
    from micnaga.pipeline.runner import PipelineRunner, deps_for_session
    from micnaga.pipeline.stages import get_stages
    from micnaga.pipeline.stages.base import StageContext, is_stage_done, write_marker

    work = paths.work_dir(folder)
    created = not work.is_dir()
    stages = get_stages()[:5]
    if all(is_stage_done(folder, st) for st in stages):
        return created
    work.mkdir(parents=True, exist_ok=True)
    meta = PipelineRunner._load_meta(folder)
    deps = deps_for_session(folder)
    for i, st in enumerate(stages):
        if is_stage_done(folder, st):
            continue

        def on_progress(f: float, i: int = i) -> None:
            progress("prepara", (i + f) / 5)

        ctx = StageContext(folder, config, deps, meta, on_progress)
        t0 = time.monotonic()
        result = st.run(ctx)
        write_marker(folder, st, result, time.monotonic() - t0)
    return created


def improve(
    folder: Path,
    config: Config,
    create_engine: Callable[[], AsrLike],
    *,
    progress: Progress | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> HqResult:
    """Re-transcribe ``folder`` con el motor de ``create_engine`` (ver el docstring del modulo).

    Lanza ``Cancelled`` sin tocar la transcripcion si se cancela antes de escribir."""
    from micnaga.pipeline.stages.base import read_audio_file
    from micnaga.pipeline.stages.s06_asr import transcribe_block

    report = progress or (lambda _f, _p: None)
    is_cancelled = cancelled or (lambda: (folder / CANCEL).is_file())
    t0 = time.monotonic()
    # falla pronto (antes de gastar CPU) si la transcripcion no se puede leer; al publicar se
    # vuelve a leer la del disco
    Transcript.from_json((folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8"))
    work = paths.work_dir(folder)
    created_work = not work.is_dir()  # si se crea aqui, se borra al final (tambien si falla)
    try:
        _ensure_vad(folder, config, report)
        vad: dict[str, Any] = json.loads((work / "05_vad.json").read_text(encoding="utf-8"))
        sr = config.capture.sample_rate
        total = sum(b - a for t in ("mic", "system") for a, b in vad[t]["chunks"]) or 1.0
        done = 0.0
        words: dict[str, list[Word]] = {"mic": [], "system": []}
        report("transcribe", 0.0)
        engine = create_engine()
        try:
            for track in ("mic", "system"):
                chunks: list[list[float]] = vad[track]["chunks"]
                if not chunks:
                    continue
                audio = read_audio_file(work / f"04_{track}.wav", sr)
                for a, b in chunks:
                    if is_cancelled():
                        raise Cancelled()
                    words[track].extend(drop_loops(transcribe_block(engine, audio, a, b, sr)))
                    done += b - a
                    report("transcribe", done / total)
                del audio
            name = engine.name
        finally:
            engine.close()
        if is_cancelled():
            raise Cancelled()
        report("ensambla", 1.0)
        for track in words:
            words[track].sort(key=lambda w: (w.s, w.e))
        # Se relee del disco al publicar: un renombrado (o fusion de hablantes, o marcador)
        # hecho mientras corria la mejora se conserva en vez de pisarse con la copia del inicio.
        latest = Transcript.from_json((folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8"))
        new = rebuild(latest, words["mic"], words["system"], config, name)
        before, after = _count_words(latest), _count_words(new)
        _validate(before, after, len(new.segments))
        from micnaga.pipeline import output

        save_previa(folder)
        output.write_transcript(folder, new)
        _reindex(folder, new)
        return HqResult(before, after, time.monotonic() - t0, name)
    finally:
        (folder / CANCEL).unlink(missing_ok=True)
        if created_work:
            shutil.rmtree(work, ignore_errors=True)


# ----------------------------------------------------------------------------- modelo


def model_group(config: Config) -> str:
    return MODELS.get(config.processing.hq_model, MODELS[DEFAULT_MODEL])


def model_name(config: Config) -> str:
    m = config.processing.hq_model
    return m if m in MODELS else DEFAULT_MODEL


def download_bytes(config: Config) -> int:
    """Bytes que faltan por descargar del modelo HQ (0 = ya esta)."""
    from micnaga import models

    return sum(f.size for f in models.missing_models(config, groups=[model_group(config)]))


def create_engine(config: Config) -> AsrLike:
    from micnaga import models
    from micnaga.pipeline.engines.asr.whisper import WhisperEngine

    model_dir = models.require_models(config, model_group(config))
    return WhisperEngine(
        str(model_dir),
        model_name(config),
        config.processing.threads,
        guards=True,
    )


# ----------------------------------------------------------------------------- proceso


def mutex_name(folder: Path) -> str:
    import hashlib

    digest = hashlib.sha1(str(folder.resolve()).lower().encode("utf-8")).hexdigest()[:10]
    return f"Local\\micnaga-hq-{digest}"


def spawn(folder: Path) -> Any:
    """Lanza ``micnaga hq <carpeta>`` (sin ventana, prioridad BELOW_NORMAL, grupo propio)."""
    import subprocess

    from micnaga.pipeline.worker import (
        BELOW_NORMAL_PRIORITY_CLASS,
        CREATE_NEW_PROCESS_GROUP,
        CREATE_NO_WINDOW,
        worker_command,
    )

    cmd = worker_command(())
    cmd[-1] = "hq"
    (folder / CANCEL).unlink(missing_ok=True)
    write_status(folder, state="running", fase="cola", pct=0.0)
    return subprocess.Popen(
        [*cmd, str(folder)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | BELOW_NORMAL_PRIORITY_CLASS,
        close_fds=True,
    )


def run_cli(argv: list[str]) -> int:
    """``micnaga hq <carpeta>``: descarga el modelo si falta y mejora la transcripcion."""
    import argparse

    parser = argparse.ArgumentParser(prog="micnaga hq")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--restore", action="store_true", help="volver a la anterior")
    args = parser.parse_args(argv)
    folder: Path = args.folder.resolve()
    from micnaga.config import load_config

    config = load_config()
    if args.restore:
        return 0 if restore(folder) else 1
    from micnaga.pipeline.worker import NamedMutex, apply_thread_limits, set_low_priority

    apply_thread_limits(config.processing.threads)
    set_low_priority()
    from micnaga.logging_setup import setup_logging

    setup_logging("hq")
    with NamedMutex(mutex_name(folder)) as lock:
        if not lock.acquire():
            return 0  # ya hay una mejora en marcha para esta reunion
        return _run_locked(folder, config)


def _run_locked(folder: Path, config: Config) -> int:
    from micnaga import models

    def report(fase: str, pct: float) -> None:
        write_status(folder, state="running", fase=fase, pct=round(pct, 3), pid=os.getpid())

    try:
        if download_bytes(config):

            def on_dl(p: models.DownloadProgress) -> None:
                report("descarga", p.overall_done / max(1, p.overall_total))
                if (folder / CANCEL).is_file():
                    raise Cancelled()

            models.download_models(config, on_dl, groups=[model_group(config)])
        result = improve(folder, config, lambda: create_engine(config), progress=report)
    except Cancelled:
        (folder / CANCEL).unlink(missing_ok=True)
        write_status(folder, state="cancelled")
        return 0
    except HqRejected as exc:
        log.warning("hq transcription rejected, the current one is kept: %s", exc)
        write_status(folder, state="failed", error="HqRejected")
        return 1
    except Exception as exc:
        log.exception("hq transcription failed")
        write_status(folder, state="failed", error=type(exc).__name__)
        return 1
    log.info(
        "hq transcription done: %s, %.0f s, words %d -> %d",
        result.engine,
        result.seconds,
        result.words_before,
        result.words_after,
    )
    write_status(
        folder,
        state="done",
        engine=result.engine,
        seconds=round(result.seconds, 1),
        words_before=result.words_before,
        words_after=result.words_after,
    )
    return 0
