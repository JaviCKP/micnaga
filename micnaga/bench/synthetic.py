"""Sesiones sinteticas para el banco de pruebas y los tests (nunca grabaciones reales).

``generate_session(folder, spec)`` escribe una sesion completa lista para el pipeline:
``audio_yo.flac``, ``audio_remoto.flac``, ``capture.json``, ``synthetic_script.json`` (el guion:
quien dice que y cuando; lo leen los motores falsos de ``micnaga.bench.fakes``) y ``reference.json``
(la transcripcion de referencia en el esquema de la seccion 9) y ``expect.json`` para los casos
trampa.

El audio no es voz real: cada enunciado es una pila de armonicos con envolvente silabica y una
frecuencia fundamental **propia** (``f0 = F0_BASE + F0_STEP * plantilla``), que es lo que
reconocen los motores falsos para devolver el texto del guion. Sirve para comprobar la tuberia
extremo a extremo, no para medir WER/DER de modelos reales.

Tipos (``SyntheticSpec.kind``):

* ``meeting``: dialogo alternando micro (Yo) y sistema (``n_remote`` hablantes remotos);
* ``voice_note``: solo habla el micro (el sistema en silencio) -> cae en la compuerta 2;
* ``youtube``: habla el sistema sin parar y el micro en silencio (o solo con eco) -> cae en la 3.

Con ``echo=True`` el micro recibe ademas la pista del sistema retrasada y atenuada (altavoces).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np

from micnaga import paths
from micnaga.types import (
    AudioArray,
    CaptureMeta,
    EnginesInfo,
    Marker,
    MeetingInfo,
    OutputKind,
    Segment,
    Speaker,
    TrackInfo,
    Transcript,
    Word,
)

SR = 16000
F0_BASE = 100.0
F0_STEP = 6.0
N_TEMPLATES = 100
SCRIPT_FILE = "synthetic_script.json"
REFERENCE_FILE = "reference.json"
EXPECT_FILE = "expect.json"
ECHO_DELAY_S = 0.04
ECHO_GAIN = 0.5

_BANK = [
    "Buenos días a todos, ¿me oís bien?",
    "Perfecto, empezamos por lo del despliegue de esta semana.",
    "Ayer revisé los resultados y todo parece correcto salvo el módulo de facturación.",
    "Necesitamos decidir el proveedor antes del viernes para no retrasar el proyecto.",
    "Yo prefiero la opción más sencilla aunque cueste un poco más.",
    "Vale, lo apunto y se lo comento a Laura cuando hablemos con Kubernetes.",
    "Entonces quedamos en que la entrega será el martes por la mañana.",
    "¿Alguien tiene algo más que añadir sobre el presupuesto del trimestre?",
    "Sí, creo que deberíamos revisar los costes de la nube antes de cerrar.",
    "Muy bien, pues si no hay más preguntas damos la reunión por terminada.",
]


@dataclass(frozen=True)
class SyntheticSpec:
    kind: Literal["meeting", "voice_note", "youtube"] = "meeting"
    duration_s: float = 240.0
    n_remote: int = 2
    output_kind: OutputKind = "headphones"
    echo: bool = False
    confirmed: bool = False
    title: str = "Revisión semanal"
    start: str = "2026-11-18T10:30:00+01:00"
    meet_code: str = "abc-defg-hij"
    seed: int = 0


@dataclass(frozen=True)
class Utterance:
    id: int
    template: int
    track: Literal["mic", "system"]
    speaker: str
    start: float
    end: float
    text: str
    f0: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "template": self.template,
            "track": self.track,
            "speaker": self.speaker,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "f0": self.f0,
        }


def _template_text(template: int) -> str:
    base = _BANK[template % len(_BANK)]
    return base if template < len(_BANK) else f"{base} ({template // len(_BANK) + 1})"


def template_f0(template: int) -> float:
    return F0_BASE + F0_STEP * template


def _speaker_of(template: int, n_remote: int, kind: str) -> str:
    if kind == "voice_note":
        return "me"
    if kind == "youtube":
        return f"S{1 + template % max(1, n_remote)}"
    return "me" if template % 2 == 0 else f"S{1 + (template // 2) % max(1, n_remote)}"


def build_script(spec: SyntheticSpec) -> list[Utterance]:
    rng = np.random.default_rng(spec.seed)
    out: list[Utterance] = []
    t = 1.0
    i = 0
    while True:
        template = i % N_TEMPLATES
        text = _template_text(template)
        n_words = len(text.split())
        dur = max(2.5, 0.38 * n_words + 0.4)
        if t + dur > spec.duration_s - 0.5:
            break
        speaker = _speaker_of(template, spec.n_remote, spec.kind)
        track: Literal["mic", "system"] = "mic" if speaker == "me" else "system"
        out.append(Utterance(i, template, track, speaker, t, t + dur, text, template_f0(template)))
        gap = 0.3 if spec.kind == "youtube" else float(rng.uniform(0.8, 1.5))
        t += dur + gap
        i += 1
    return out


def _tone(utt: Utterance, sr: int, amp: float) -> AudioArray:
    n = round((utt.end - utt.start) * sr)
    rng = np.random.default_rng(utt.template)
    t = np.arange(n) / sr
    sig = np.zeros(n)
    for k in range(1, 15):
        sig += (1.0 / k) * np.sin(2 * np.pi * utt.f0 * k * t + rng.uniform(0, 2 * np.pi))
    env = 0.5 * (1 + np.sin(2 * np.pi * 4.0 * t + rng.uniform(0, 2 * np.pi))) ** 0.7
    sig *= env
    peak = float(np.max(np.abs(sig))) or 1.0
    return np.asarray(amp * sig / peak, dtype=np.float32)


def render_tracks(spec: SyntheticSpec, script: list[Utterance]) -> tuple[AudioArray, AudioArray]:
    n = round(spec.duration_s * SR)
    rng = np.random.default_rng(spec.seed + 1)
    mic = (0.001 * rng.standard_normal(n)).astype(np.float32)
    system = (0.001 * rng.standard_normal(n)).astype(np.float32)
    for utt in script:
        i0 = round(utt.start * SR)
        sig = _tone(utt, SR, 0.3)
        target = mic if utt.track == "mic" else system
        end = min(n, i0 + len(sig))
        target[i0:end] += sig[: end - i0]
    if spec.echo:
        k = round(ECHO_DELAY_S * SR)
        echo = np.zeros(n, dtype=np.float32)
        echo[k:] = ECHO_GAIN * system[: n - k]
        mic = np.clip(mic + echo, -1, 1).astype(np.float32)
    return mic, system


def reference_transcript(spec: SyntheticSpec, script: list[Utterance]) -> Transcript:
    segments: list[Segment] = []
    for u in script:
        tokens = u.text.split()
        step = (u.end - u.start) / len(tokens)
        words = [
            Word(w, round(u.start + i * step, 3), round(u.start + (i + 1) * step - 0.02, 3))
            for i, w in enumerate(tokens)
        ]
        segments.append(Segment(u.start, u.end, u.speaker, u.text, words))
    ids = sorted({u.speaker for u in script if u.speaker != "me"})
    speakers = [Speaker("me", "Yo"), *[Speaker(s, f"Hablante {s[1:]}") for s in ids]]
    return Transcript(
        meeting=MeetingInfo(
            title=spec.title,
            start=spec.start,
            duration_s=spec.duration_s,
            meet_code=spec.meet_code,
            output_device=spec.output_kind,
        ),
        speakers=speakers,
        segments=segments,
        markers=[],
        engines=EnginesInfo(asr="reference"),
    )


def generate_session(folder: Path, spec: SyntheticSpec | None = None) -> Path:
    """Escribe la sesion sintetica en ``folder`` (se crea) y la devuelve."""
    import soundfile as sf

    spec = spec or SyntheticSpec()
    folder.mkdir(parents=True, exist_ok=True)
    script = build_script(spec)
    mic, system = render_tracks(spec, script)
    sf.write(str(folder / paths.AUDIO_ME_FLAC), mic, SR, subtype="PCM_16", format="FLAC")
    sf.write(str(folder / paths.AUDIO_REMOTE_FLAC), system, SR, subtype="PCM_16", format="FLAC")
    markers = [Marker(round(min(30.0, spec.duration_s / 2), 1), "Decidir proveedor")]
    meta = CaptureMeta(
        started_at=spec.start,
        mic=TrackInfo(paths.AUDIO_ME_FLAC, "synthetic mic", SR, len(mic)),
        system=TrackInfo(paths.AUDIO_REMOTE_FLAC, "synthetic loopback", SR, len(system)),
        output_kind=spec.output_kind,
        confirmed=spec.confirmed,
        meet_title=spec.title,
        meet_code=spec.meet_code,
        markers=markers,
    )
    (folder / paths.CAPTURE_JSON).write_text(meta.to_json(), encoding="utf-8")
    (folder / SCRIPT_FILE).write_text(
        json.dumps({"sr": SR, "utterances": [u.to_dict() for u in script]}, ensure_ascii=False),
        encoding="utf-8",
    )
    reference = reference_transcript(spec, script)
    (folder / REFERENCE_FILE).write_text(reference.to_json(), encoding="utf-8")
    should_discard = (not spec.confirmed and spec.kind != "meeting") or spec.duration_s < 180
    (folder / EXPECT_FILE).write_text(json.dumps({"discard": should_discard}), encoding="utf-8")
    return folder


def spec_for_case(name: str) -> SyntheticSpec:
    """Casos predefinidos del modo ``--synthetic`` del banco de pruebas."""
    cases = {
        "dialogo-auriculares": SyntheticSpec(kind="meeting", duration_s=240.0),
        "dialogo-altavoces": SyntheticSpec(
            kind="meeting", duration_s=240.0, output_kind="speakers", echo=True, seed=1
        ),
        "nota-de-voz": SyntheticSpec(kind="voice_note", duration_s=120.0, seed=2),
        "youtube": SyntheticSpec(
            kind="youtube", duration_s=240.0, output_kind="speakers", echo=True, seed=3
        ),
    }
    return cases[name]


SYNTHETIC_CASES = ("dialogo-auriculares", "dialogo-altavoces", "nota-de-voz", "youtube")
