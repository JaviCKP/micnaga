"""Salidas de una reunion (spec 9): ``transcripcion.json`` (fuente de verdad), ``.md`` y ``.srt``.

El JSON es la fuente de verdad: ``regenerate_from_json`` reconstruye Markdown y SRT a partir de el
(la biblioteca de F4 lo llama al renombrar un hablante o corregir un texto). ``rename_speaker`` y
``merge_speakers`` editan el JSON, regeneran los otros dos ficheros y reindexan la reunion en
SQLite (MIC-14).

Formato Markdown (decisiones donde la spec no lo fija)
------------------------------------------------------
* Las lineas de la transcripcion van seguidas, tal como en el ejemplo de la spec.
* Fecha y hora salen del ISO de ``meeting.start`` tal cual (su propia zona, no la del equipo);
  duracion = ``round(duration_s / 60)`` minutos, minimo 1.
* ``## Marcadores`` solo aparece si hay marcadores; ``[mm:ss]`` con los minutos totales (pasada la
  hora sigue contando: ``[72:05]``); una etiqueta vacia se muestra como ``Marcador``.
* ``Participantes`` lista solo los hablantes con algun segmento (``—`` si no hay ninguno).
* El pasaje marcado se resalta con la sintaxis ``==texto==``: es el segmento que contiene el
  instante del marcador o, si cae en un hueco, el mas cercano (en empate, el anterior).
* Texto con saltos ``\\n`` y sin BOM, UTF-8.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from micnaga import paths
from micnaga.pipeline.stages.phrases import default_speaker_name, merge_segments
from micnaga.text import format_mmss as format_mmss
from micnaga.types import SPEAKER_ME, Segment, Speaker, Transcript, speaker_track

if TYPE_CHECKING:
    from micnaga.store.db import Database

log = logging.getLogger(__name__)

SRT_MAX_CUE_S = 7.0
SRT_MAX_CUE_CHARS = 84
LOCK_TIMEOUT_S = 30.0


# ----------------------------------------------------------------------------- cerrojo

_thread_locks: dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()


def _lock_key(folder: Path) -> str:
    return os.path.normcase(str(folder.resolve()))


@contextlib.contextmanager
def transcript_lock(folder: Path, timeout_s: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Cerrojo de lectura-modificacion-escritura de ``transcripcion.json`` de una reunion.

    Un ``RLock`` por carpeta (hilos de este proceso, reentrante) y un mutex con nombre de
    Windows (otros procesos: la mejora HQ corre en ``micnaga hq``). Sin el, un renombrado que
    leyo antes de que la mejora publicase y escribia despues pisaba la transcripcion mejorada.
    Si el mutex no llega en ``timeout_s`` se sigue igualmente (mejor que bloquear la edicion).
    """
    key = _lock_key(folder)
    with _thread_locks_guard:
        lock = _thread_locks.setdefault(key, threading.RLock())
    with lock:
        handle = _acquire_mutex(key, timeout_s)
        try:
            yield
        finally:
            if handle is not None:
                with contextlib.suppress(Exception):
                    import win32event

                    win32event.ReleaseMutex(handle)
                with contextlib.suppress(Exception):
                    handle.Close()


def _acquire_mutex(key: str, timeout_s: float) -> Any:
    try:
        import win32event
    except ImportError:  # pragma: no cover - solo Windows tiene el mutex entre procesos
        return None
    import hashlib

    name = "Local\\micnaga-transcript-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    try:
        handle = win32event.CreateMutex(None, False, name)  # type: ignore[arg-type]
        res = win32event.WaitForSingleObject(handle, int(timeout_s * 1000))  # type: ignore[arg-type]
    except Exception:
        log.warning("transcript lock unavailable; continuing without it", exc_info=True)
        return None
    if res in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
        return handle
    log.warning("transcript lock timed out after %.0f s; continuing without it", timeout_s)
    handle.Close()
    return None


# ----------------------------------------------------------------------------- formato


def format_hms(seconds: float) -> str:
    total = int(max(0.0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_srt_time(seconds: float) -> str:
    ms_total = round(max(0.0, seconds) * 1000)
    h, rem = divmod(ms_total, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def speaker_names(transcript: Transcript) -> dict[str, str]:
    return {s.id: s.name for s in transcript.speakers}


def marker_segment_index(segments: list[Segment], t: float) -> int | None:
    """Indice del segmento que contiene ``t`` o, si no hay, el mas cercano (anterior en empate)."""
    if not segments:
        return None
    best: tuple[float, int] | None = None
    for i, seg in enumerate(segments):
        if seg.start <= t <= seg.end:
            return i
        distance = seg.start - t if t < seg.start else t - seg.end
        key = (distance, i)
        if best is None or key < best:
            best = key
    return None if best is None else best[1]


def _header(transcript: Transcript) -> str:
    m = transcript.meeting
    try:
        when = datetime.fromisoformat(m.start).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        when = m.start
    minutes = max(1, round(m.duration_s / 60))
    return f"# {m.title} — {when} ({minutes} min)"


def render_markdown(transcript: Transcript) -> str:
    names = speaker_names(transcript)
    lines = [_header(transcript), ""]
    speaking = {seg.speaker for seg in transcript.segments}
    participants = [s.name for s in transcript.speakers if s.id in speaking]
    lines.append("Participantes: " + (", ".join(participants) or "—"))
    lines.append("")
    highlighted: set[int] = set()
    if transcript.markers:
        lines.append("## Marcadores")
        for marker in sorted(transcript.markers, key=lambda x: x.t):
            label = marker.label.strip() or "Marcador"
            lines.append(f"- [{format_mmss(marker.t)}] {label}")
            idx = marker_segment_index(transcript.segments, marker.t)
            if idx is not None:
                highlighted.add(idx)
        lines.append("")
    lines.append("## Transcripción")
    for i, seg in enumerate(transcript.segments):
        name = names.get(seg.speaker) or default_speaker_name(seg.speaker)
        text = f"=={seg.text}==" if i in highlighted else seg.text
        lines.append(f"**[{format_hms(seg.start)}] {name}:** {text}")
    return "\n".join(lines) + "\n"


def _cues(seg: Segment) -> list[tuple[float, float, str]]:
    """Trocea un segmento largo en cues de <= 7 s / 84 caracteres usando sus palabras."""
    words = seg.words
    joined = " ".join(w.w for w in words)
    if (
        not words
        or joined != seg.text
        or (seg.end - seg.start <= SRT_MAX_CUE_S and len(seg.text) <= SRT_MAX_CUE_CHARS)
    ):
        return [(seg.start, seg.end, seg.text)]
    cues: list[tuple[float, float, str]] = []
    current: list[str] = []
    cue_start = words[0].s
    last_end = words[0].e
    for w in words:
        projected = len(" ".join([*current, w.w]))
        if current and (projected > SRT_MAX_CUE_CHARS or w.e - cue_start > SRT_MAX_CUE_S):
            cues.append((cue_start, last_end, " ".join(current)))
            current, cue_start = [], w.s
        current.append(w.w)
        last_end = w.e
    if current:
        cues.append((cue_start, last_end, " ".join(current)))
    return cues


def render_srt(transcript: Transcript) -> str:
    names = speaker_names(transcript)
    blocks: list[str] = []
    n = 0
    for seg in transcript.segments:
        name = names.get(seg.speaker) or default_speaker_name(seg.speaker)
        for k, (start, end, text) in enumerate(_cues(seg)):
            n += 1
            end = max(end, start + 0.001)
            body = f"{name}: {text}" if k == 0 else text
            blocks.append(f"{n}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{body}\n")
    return "\n".join(blocks)


# ----------------------------------------------------------------------------- E/S


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_transcript(folder: Path) -> Transcript:
    return Transcript.from_json((folder / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8"))


def write_derived(folder: Path, transcript: Transcript) -> None:
    """Escribe ``transcripcion.md`` y ``transcripcion.srt``."""
    _atomic_write_text(folder / paths.TRANSCRIPT_MD, render_markdown(transcript))
    _atomic_write_text(folder / paths.TRANSCRIPT_SRT, render_srt(transcript))


def write_transcript(folder: Path, transcript: Transcript) -> None:
    """JSON (fuente de verdad) primero y despues MD y SRT."""
    _atomic_write_text(
        folder / paths.TRANSCRIPT_JSON,
        json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2),
    )
    write_derived(folder, transcript)


def regenerate_from_json(folder: Path) -> Transcript:
    """Reconstruye ``transcripcion.md`` y ``.srt`` desde el JSON (F4: tras renombrar o editar)."""
    transcript = read_transcript(folder)
    write_derived(folder, transcript)
    return transcript


def index_meeting(
    db: Database, folder: Path, transcript: Transcript, job_id: int | None = None
) -> int:
    """Indexa la reunion (``meetings`` + ``segments`` + FTS5) con el nombre de carpeta."""
    return db.index_transcript(folder.name, transcript, job_id)


def _reindex(folder: Path, transcript: Transcript, db: Database | None) -> None:
    try:
        if db is not None:
            index_meeting(db, folder, transcript)
            return
        # el indice de la raiz de la propia reunion (puede no ser la carpeta actual)
        db_file = folder.parent / paths.db_path().name
        if not db_file.is_file():
            db_file = paths.db_path()
        if not db_file.is_file():
            return
        from micnaga.store.db import Database as Db

        with Db(db_file) as own:
            index_meeting(own, folder, transcript)
    except Exception as exc:  # el indice se puede reconstruir; no debe romper la edicion
        log.warning("could not reindex %s: %s", folder.name, exc)


def _save(folder: Path, transcript: Transcript, db: Database | None, reindex: bool) -> Transcript:
    write_transcript(folder, transcript)
    if reindex:
        _reindex(folder, transcript, db)
    return transcript


# ----------------------------------------------------------------------------- ediciones


def rename_speaker(
    folder: Path,
    speaker_id: str,
    new_name: str,
    *,
    db: Database | None = None,
    reindex: bool = True,
    learn_voiceprint: bool = False,
) -> Transcript:
    """Renombra un hablante, regenera MD/SRT y reindexa.

    Con ``learn_voiceprint=True`` y las huellas activadas (``privacy.voiceprints_enabled``) guarda
    la huella de ese hablante con el nuevo nombre (el llamador debe haber pedido consentimiento).
    """
    name = new_name.strip()
    if not name:
        raise ValueError("speaker name must not be empty")
    with transcript_lock(folder):
        transcript = read_transcript(folder)
        if all(s.id != speaker_id for s in transcript.speakers):
            raise KeyError(f"unknown speaker {speaker_id!r}")
        speakers = [
            Speaker(s.id, name, None if s.name != name else s.voiceprint_match)
            if s.id == speaker_id
            else s
            for s in transcript.speakers
        ]
        updated = dataclasses.replace(transcript, speakers=speakers)
        _save(folder, updated, db, reindex)
    if learn_voiceprint and speaker_id != SPEAKER_ME:
        _learn_voiceprint(folder, speaker_id, name)
    return updated


def _learn_voiceprint(folder: Path, speaker_id: str, name: str) -> bool:
    from micnaga.config import load_config
    from micnaga.store.voiceprints import VoiceprintStore, load_meeting_embeddings

    if not load_config().privacy.voiceprints_enabled:
        return False
    embeddings, engine = load_meeting_embeddings(folder)
    vec = embeddings.get(speaker_id)
    if vec is None:
        return False
    VoiceprintStore().save(name, vec, engine=engine)
    return True


def merge_speakers(
    folder: Path,
    keep_id: str,
    merge_id: str,
    *,
    db: Database | None = None,
    reindex: bool = True,
    merge_gap_s: float = 1.5,
) -> Transcript:
    """Fusiona ``merge_id`` en ``keep_id`` (diarizacion que partio a una persona en dos).

    Los segmentos consecutivos de la misma pista y hablante se vuelven a unir (hueco
    < ``merge_gap_s``). No se puede fusionar ``me`` en otro hablante.
    """
    if keep_id == merge_id:
        raise ValueError("cannot merge a speaker into itself")
    if merge_id == SPEAKER_ME:
        raise ValueError("the 'me' speaker cannot be merged into another one")
    with transcript_lock(folder):
        transcript = read_transcript(folder)
        known = {s.id for s in transcript.speakers}
        for sid in (keep_id, merge_id):
            if sid not in known:
                raise KeyError(f"unknown speaker {sid!r}")
        track = speaker_track(keep_id)
        relabelled = [
            dataclasses.replace(
                s, speaker=keep_id, track=s.audio_track if s.audio_track != track else None
            )
            if s.speaker == merge_id
            else s
            for s in transcript.segments
        ]
        segments = merge_segments(relabelled, merge_gap_s=merge_gap_s)
        speakers = [s for s in transcript.speakers if s.id != merge_id]
        updated = dataclasses.replace(transcript, speakers=speakers, segments=segments)
        return _save(folder, updated, db, reindex)
