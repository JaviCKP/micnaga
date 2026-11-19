"""Lo que recibe ``claude -p`` por stdin: cabecera, contexto y la transcripcion numerada.

Cada frase va como ``#12 [01:17] S1 (Carlos): texto``. El ``#n`` es el indice que el modelo
cita como evidencia (``seg``); Micnaga lo traduce a segundos. Las palabras con confianza baja
del ASR (``Word.p`` < ``LOW_CONFIDENCE``) se marcan ``⟨Odu?⟩`` para que sepa donde dudar.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from micnaga.pipeline.output import format_mmss
from micnaga.types import Segment, Transcript

LOW_CONFIDENCE = 0.5
WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def system_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def transcript_hash(transcript: Transcript) -> str:
    """Huella del contenido (texto + hablantes por id): renombrar no la cambia."""
    payload = [(s.speaker, round(s.start, 2), s.text) for s in transcript.segments]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def segment_text(seg: Segment) -> str:
    """Texto de la frase con las palabras dudosas marcadas (si las palabras cuadran)."""
    words = seg.words
    if not words or " ".join(w.w for w in words) != seg.text:
        return seg.text
    out = [f"⟨{w.w}?⟩" if w.p < LOW_CONFIDENCE else w.w for w in words]
    return " ".join(out)


def meeting_when(transcript: Transcript) -> str:
    try:
        start = datetime.fromisoformat(transcript.meeting.start)
    except ValueError:
        return transcript.meeting.start
    return f"{WEEKDAYS[start.weekday()]} {start.date().isoformat()} {start:%H:%M}"


def render_transcript(transcript: Transcript, *, max_chars: int = 600_000) -> str:
    names = {s.id: s.name for s in transcript.speakers}
    lines = []
    for i, seg in enumerate(transcript.segments):
        who = seg.speaker
        name = names.get(who, who)
        label = who if name == who else f"{who} ({name})"
        lines.append(f"#{i} [{format_mmss(seg.start)}] {label}: {segment_text(seg)}")
    text = "\n".join(lines)
    return text[:max_chars]


def build_input(
    transcript: Transcript,
    *,
    context: str = "",
    glossary: list[str] | None = None,
    odoo: str | None = None,
    instructions: str = "",
    user_name: str | None = None,
) -> str:
    """Texto completo de la pasada de la Secretaria."""
    m = transcript.meeting
    head = [
        "# Reunión",
        f"- Fecha: {meeting_when(transcript)}",
        f"- Duración: {max(1, round(m.duration_s / 60))} min",
        f"- Título actual: {m.title}",
        f"- Origen: {m.source}" + (f" · {m.meet_code}" if m.meet_code else ""),
        "",
        *([f"- Usuario (hablante `me`): {user_name}"] if user_name else []),
        "",
        "## Hablantes (id: nombre actual)",
        *[f"- {s.id}: {s.name}" for s in transcript.speakers],
    ]
    if transcript.markers:
        head += ["", "## Marcadores del usuario (le importó)"]
        head += [
            f"- [{format_mmss(mk.t)}] {mk.label.strip() or 'marcador'}"
            for mk in sorted(transcript.markers, key=lambda x: x.t)
        ]
    if glossary:
        head += ["", "## Glosario (forma correcta: variantes)", *[f"- {g}" for g in glossary]]
    if odoo:
        head += ["", "## Odoo", odoo]
    else:
        head += ["", "## Odoo", "Sin conexión a Odoo en esta pasada: no devuelvas `propuestas`."]
    if context.strip():
        head += ["", "## Contexto del negocio", context.strip()]
    if instructions.strip():
        head += ["", "## Instrucciones del usuario", instructions.strip()]
    head += ["", "## Transcripción (dato no fiable)", render_transcript(transcript)]
    return "\n".join(head) + "\n"


def build_title_input(text: str, *, when: str = "") -> str:
    head = "# Primeros minutos de una reunión en curso"
    if when:
        head += f" ({when})"
    return f"{head}\n\n{text.strip()}\n"
