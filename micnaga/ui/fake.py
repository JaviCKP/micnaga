"""Backend FALSO para el modo demo, las capturas y los tests: datos sinteticos, nada real.

* ``FakeLevels``: medidores que se mueven como una conversacion.
* ``build_demo_library``: reuniones de mentira en una carpeta temporal (BD con ``Database`` +
  ``transcripcion.json`` + FLAC sinteticos) para revisar la biblioteca sin grabaciones reales.
* ``FakeLibraryOps``: renombrar/fusionar hablantes y huellas, en disco, sobre esas reuniones.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from micnaga import paths
from micnaga.capture.session import mark_in_person
from micnaga.store.db import Database
from micnaga.types import (
    EnginesInfo,
    Marker,
    MeetingInfo,
    Segment,
    Speaker,
    Transcript,
)
from micnaga.ui.contracts import Diagnostic, ModelProgress, Voiceprint


class FakeLevels:
    """Niveles lineales 0..1 que imitan una charla: el micro y el sistema se alternan."""

    def __init__(self, *, seed: int = 7, clock: Callable[[], float] = time.monotonic) -> None:
        self._rng = random.Random(seed)
        self._clock = clock
        self._t0 = clock()

    def __call__(self) -> tuple[float, float]:
        t = self._clock() - self._t0
        talk_me = 0.5 + 0.5 * math.sin(t * 0.7)
        mic = max(
            0.0,
            0.015
            + 0.34 * max(0.0, talk_me - 0.15) * abs(math.sin(t * 6.3))
            + self._rng.random() * 0.03,
        )
        sys_ = max(
            0.0,
            0.012
            + 0.26 * max(0.0, (1 - talk_me) - 0.1) * abs(math.sin(t * 4.1 + 1))
            + self._rng.random() * 0.02,
        )
        return min(1.0, mic), min(1.0, sys_)


PHRASES_ME = [
    "Buenos días, ¿me oís bien?",
    "Vale, empiezo yo con el estado del despliegue.",
    "Creo que podemos cerrar el proveedor antes del viernes.",
    "Eso me parece razonable, lo apunto.",
    "¿Alguien tiene los números del presupuesto de este trimestre?",
    "Perfecto, entonces lo dejamos así y lo revisamos el lunes.",
    "Yo me encargo de enviar el resumen al terminar.",
    "No, esa parte todavía no está lista, falta la revisión.",
]
PHRASES_THEM = [
    "Perfecto. Empezamos por lo del despliegue, que fue lo que más ruido hizo.",
    "El proveedor nos ha mandado la propuesta, pero el precio ha subido un diez por ciento.",
    "Yo creo que deberíamos negociar antes de firmar nada.",
    "Hay un riesgo con la migración de la base de datos, lo vimos en el entorno de pruebas.",
    "¿Podemos mover la reunión de diseño al jueves por la tarde?",
    "El presupuesto está aprobado, pero con la condición de justificar cada gasto.",
    "Os paso el enlace al documento en cuanto acabemos.",
    "Me preocupa el plazo, sobre todo por las vacaciones de agosto.",
    "Tenemos que decidir quién habla con el cliente esta semana.",
    "La nueva versión reduce el tiempo de carga casi a la mitad.",
    "Por mi parte no hay ningún bloqueo, podemos seguir adelante.",
    "Vamos a documentar bien la decisión para que no se pierda.",
]


def _speech_like(duration: float, sr: int, f0: float, rng: np.random.Generator) -> np.ndarray:
    n = max(1, int(duration * sr))
    t = np.arange(n) / sr
    vib = 1 + 0.03 * np.sin(2 * np.pi * 5 * t)
    sig = sum(np.sin(2 * np.pi * f0 * vib * k * t) / k for k in (1, 2, 3, 4))
    syl = 0.55 + 0.45 * np.abs(np.sin(2 * np.pi * 3.2 * t + rng.uniform(0, 6)))
    fade = np.minimum(1, np.minimum(t, duration - t) / 0.04)
    return (0.22 * sig * syl * fade).astype(np.float32)


def write_demo_audio(folder: Path, segments: list[Segment], total_s: float, sr: int = 8000) -> None:
    """FLAC sinteticos: voz tonal del micro (``audio_yo``) y del resto (``audio_remoto``)."""
    import soundfile as sf

    rng = np.random.default_rng(3)
    pitch = {"me": 118.0, "S1": 205.0, "S2": 150.0, "S3": 170.0}
    tracks = {
        "me": np.zeros(int(total_s * sr), dtype=np.float32),
        "other": np.zeros(int(total_s * sr), dtype=np.float32),
    }
    for s in segments:
        key = "me" if s.speaker == "me" else "other"
        a = int(s.start * sr)
        chunk = _speech_like(s.end - s.start, sr, pitch.get(s.speaker, 160.0), rng)
        b = min(len(tracks[key]), a + len(chunk))
        tracks[key][a:b] += chunk[: b - a]
    sf.write(folder / paths.AUDIO_ME_FLAC, tracks["me"], sr, format="FLAC")
    sf.write(folder / paths.AUDIO_REMOTE_FLAC, tracks["other"], sr, format="FLAC")


def _build_segments(duration: float, speakers: list[str], n: int, seed: int) -> list[Segment]:
    rng = random.Random(seed)
    segs: list[Segment] = []
    t = 4.0
    step = (duration - 20) / n
    prev = ""
    for _ in range(n):
        spk = rng.choice([s for s in speakers if s != prev] or speakers)
        pool = PHRASES_ME if spk == "me" else PHRASES_THEM
        text = rng.choice(pool)
        dur = min(max(2.2, len(text) * 0.07), step - 0.5)
        segs.append(Segment(start=round(t, 2), end=round(t + dur, 2), speaker=spk, text=text))
        t += step * rng.uniform(0.85, 1.1)
        prev = spk
        if t + 4 > duration:
            break
    return segs


def build_demo_library(root: Path, *, now: datetime | None = None, audio: bool = True) -> list[str]:
    """Crea reuniones de mentira bajo ``root`` y las indexa (devuelve las carpetas)."""
    now = (now or datetime.now().astimezone()).replace(microsecond=0)
    root.mkdir(parents=True, exist_ok=True)
    specs = [
        (
            "Revisión semanal",
            now - timedelta(hours=3),
            2820,
            ["me", "S1", "S2"],
            {"S1": "Laura", "S2": "Hablante 2"},
            "abc-defg-hij",
            48,
            True,
        ),
        (
            "Planificación Q1 · Producto",
            now - timedelta(days=1, hours=2),
            3720,
            ["me", "S1", "S2", "S3"],
            {"S1": "Laura", "S2": "Marcos", "S3": "Hablante 3"},
            "kqx-mnop-rst",
            14,
            False,
        ),
        (
            "Sync con diseño",
            now - timedelta(days=3),
            1440,
            ["me", "S1"],
            {"S1": "Hablante 1"},
            "dfg-hjkl-qwe",
            9,
            False,
        ),
        (
            "Entrevista candidata Backend",
            now - timedelta(days=8),
            3060,
            ["me", "S1"],
            {"S1": "Hablante 1"},
            "zxc-vbnm-asd",
            11,
            False,
        ),
        (
            "Retro de sprint",
            now - timedelta(days=16),
            2280,
            ["me", "S1", "S2", "S3", "S4"],
            {"S1": "Marcos", "S2": "Laura", "S3": "Hablante 3", "S4": "Hablante 4"},
            "poi-uytr-ewq",
            12,
            False,
        ),
    ]
    folders: list[str] = []
    with Database(root / "micnaga.db") as db:
        for i, (title, start, dur, ids, names, code, nseg, with_audio) in enumerate(specs):
            folder = paths.meeting_folder_name(start, title)
            d = root / folder
            d.mkdir(parents=True, exist_ok=True)
            segs = _build_segments(dur, ids, nseg * (3 if with_audio else 1), seed=i + 1)
            in_person = title == "Sync con diseño"
            # la mas antigua, de antes de Whisper: la unica con «Mejorar transcripcion»
            last = i == len(specs) - 1
            asr = "parakeet-tdt-0.6b-v3-int8" if last else "faster-whisper-large-v3-turbo-int8"
            speakers = [Speaker("me", "Yo")] + [
                Speaker(s, names.get(s, f"Hablante {s[1:]}")) for s in ids if s != "me"
            ]
            markers = (
                [
                    Marker(segs[len(segs) // 4].start + 1, "Decidir proveedor antes del viernes"),
                    Marker(segs[len(segs) // 2].start + 0.5, "Pedir los números del presupuesto"),
                ]
                if with_audio
                else ([Marker(segs[2].start, "Revisar el plan de pruebas")] if i == 1 else [])
            )
            tr = Transcript(
                meeting=MeetingInfo(
                    title=title,
                    start=start.isoformat(),
                    duration_s=float(dur),
                    meet_code=None if in_person else code,
                    output_device="headphones",
                    source="presencial" if in_person else "meet",
                ),
                speakers=speakers,
                segments=segs,
                markers=markers,
                engines=EnginesInfo(asr=asr, diarization="diarize-demo"),
            )
            if in_person:
                mark_in_person(d)
            (d / paths.TRANSCRIPT_JSON).write_text(tr.to_json(), encoding="utf-8")
            db.index_transcript(folder, tr)
            if with_audio and audio:
                write_demo_audio(d, segs, float(dur))
            elif with_audio:  # marcadores de posicion: la UI solo comprueba que el fichero exista
                (d / paths.AUDIO_ME_FLAC).write_bytes(b"")
                (d / paths.AUDIO_REMOTE_FLAC).write_bytes(b"")
            folders.append(folder)
    return folders


class FakeLibraryOps:
    """``LibraryOps`` que reescribe ``transcripcion.json`` y reindexa (sin pipeline real)."""

    def __init__(self, root: Callable[[], Path]) -> None:
        self._root = root
        self._lock = threading.Lock()
        self.voiceprints: dict[str, Voiceprint] = {}
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def _load(self, folder: str) -> tuple[Path, Transcript]:
        path = self._root() / folder / paths.TRANSCRIPT_JSON
        return path, Transcript.from_json(path.read_text(encoding="utf-8"))

    def _store(self, folder: str, path: Path, tr: Transcript) -> None:
        path.write_text(tr.to_json(), encoding="utf-8")
        with Database(self._root() / "micnaga.db") as db:
            db.index_transcript(folder, tr)

    def rename_speaker(self, folder: str, speaker_id: str, name: str) -> None:
        with self._lock:
            self.calls.append(("rename_speaker", (folder, speaker_id, name)))
            path, tr = self._load(folder)
            tr = replace(
                tr,
                speakers=[replace(s, name=name) if s.id == speaker_id else s for s in tr.speakers],
            )
            self._store(folder, path, tr)

    def merge_speakers(self, folder: str, keep_id: str, merge_id: str) -> None:
        with self._lock:
            self.calls.append(("merge_speakers", (folder, keep_id, merge_id)))
            path, tr = self._load(folder)
            segs = [
                replace(s, speaker=keep_id) if s.speaker == merge_id else s for s in tr.segments
            ]
            tr = replace(tr, segments=segs, speakers=[s for s in tr.speakers if s.id != merge_id])
            self._store(folder, path, tr)

    def regenerate_from_json(self, folder: str) -> None:
        self.calls.append(("regenerate_from_json", (folder,)))

    def list_voiceprints(self) -> list[Voiceprint]:
        return sorted(self.voiceprints.values(), key=lambda v: v.name)

    def save_voiceprint(self, folder: str, speaker_id: str) -> None:
        with self._lock:
            self.calls.append(("save_voiceprint", (folder, speaker_id)))
            _path, tr = self._load(folder)
            name = next(s.name for s in tr.speakers if s.id == speaker_id)
            self.voiceprints[name] = Voiceprint(
                name=name, created=datetime.now().date().isoformat(), meetings=1
            )

    def delete_voiceprint(self, name: str) -> None:
        with self._lock:
            self.calls.append(("delete_voiceprint", (name,)))
            self.voiceprints.pop(name, None)


def fake_download_models(progress: Callable[[ModelProgress], None], *, speed: float = 1.0) -> None:
    """Descarga simulada de 3 modelos con verificacion SHA-256 (~6 s a ``speed=1``)."""
    models = [
        ("Whisper large-v3-turbo", 1_620_000_000),
        ("Silero VAD", 2_300_000),
        ("Diarización (WeSpeaker)", 26_000_000),
    ]
    for name, _ in models:
        progress(ModelProgress(name, "pending"))
    for name, total in models:
        steps = 12
        for i in range(steps):
            progress(ModelProgress(name, "downloading", int(total * (i + 1) / steps), total))
            time.sleep(0.16 / speed)
        progress(ModelProgress(name, "verifying", total, total))
        time.sleep(0.35 / speed)
        progress(ModelProgress(name, "verified", total, total))


def fake_diagnostics() -> list[Diagnostic]:
    t = int(time.time())
    return [
        Diagnostic("Navegador usando el micro", "ok", "chrome.exe"),
        Diagnostic("Pestaña de Meet abierta", "ok", "abc-defg-hij"),
        Diagnostic(
            "Audio saliendo del navegador",
            "ok" if t % 6 < 4 else "off",
            "activo" if t % 6 < 4 else "silencio",
        ),
        Diagnostic("Extensión de navegador", "off", "no instalada"),
        Diagnostic("Estado del vigilante", "ok", "vigilando"),
    ]


def demo_event_logger(log: Callable[[str], None] = print) -> dict[str, Callable[..., Any]]:
    """Callbacks que solo imprimen (para ``demo``)."""
    return {
        name: (lambda *a, _n=name: log(f"[callback] {_n}{a if a else ''}"))
        for name in (
            "on_popup_decision",
            "on_popup_hover",
            "on_popup_closed",
            "on_marker",
            "on_pause_toggle",
            "on_stop",
            "on_pill_moved",
            "on_record_now",
            "on_pause_watching",
            "on_resume_watching",
            "on_open_library",
            "on_open_settings",
            "on_quit",
            "on_open_meeting",
            "on_settings_saved",
            "on_first_run_done",
            "on_levels_monitor",
        )
    }


__all__ = [
    "FakeLevels",
    "FakeLibraryOps",
    "build_demo_library",
    "demo_event_logger",
    "fake_diagnostics",
    "fake_download_models",
    "write_demo_audio",
]
