"""Voz sintetica local (Windows SAPI) para los tests con modelos. NUNCA grabaciones de usuarios.

Genera WAV 16 kHz mono 16 bit en una carpeta temporal con ``win32com`` (``SAPI.SpVoice``). Si
no hay SAPI o no hay voz del idioma pedido, ``synth`` devuelve ``None`` y el test se salta.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from micnaga.types import AudioArray

SAFT_16KHZ_16BIT_MONO = 18

# Texto de referencia conocido (para WER). Frases cortas, vocabulario de oficina.
SPANISH_TEXT = (
    "Buenos días a todos. Empezamos la reunión de seguimiento del proyecto. "
    "Hoy vamos a revisar el estado del despliegue y los próximos pasos. "
    "El equipo de desarrollo ha terminado la migración de la base de datos. "
    "Falta probar la integración con el sistema de facturación. "
    "Después hablaremos del presupuesto del próximo trimestre. "
    "¿Me oís bien todos? ¡Perfecto, muchas gracias!"
)
ENGLISH_TEXT = (
    "Thanks everyone for joining. I would like to share a quick update about the new release. "
    "The testing phase finished yesterday and we found only two minor bugs. "
    "Both of them are already fixed and the build is ready for deployment."
)


def voices() -> list[tuple[int, str, str]]:
    """Voces SAPI instaladas: ``(indice, descripcion, codigo de idioma hex)``."""
    try:
        import win32com.client
    except ImportError:  # pragma: no cover
        return []
    try:
        sp = win32com.client.Dispatch("SAPI.SpVoice")
        vs = sp.GetVoices()
        return [
            (i, str(vs.Item(i).GetDescription()), str(vs.Item(i).GetAttribute("Language")))
            for i in range(vs.Count)
        ]
    except Exception:  # pragma: no cover - SAPI roto
        return []


def find_voice(language_hex: str) -> int | None:
    """Indice de la primera voz cuyo idioma empieza por ``language_hex`` (p. ej. ``"C0A"``)."""
    for i, _desc, lang in voices():
        if lang.upper().startswith(language_hex.upper()):
            return i
    return None


def synth(text: str, path: Path, voice: int, rate: int = 0) -> Path | None:
    """Escribe ``text`` dicho con la voz ``voice`` en ``path`` (WAV 16 kHz)."""
    try:
        import win32com.client

        sp = win32com.client.Dispatch("SAPI.SpVoice")
        sp.Voice = sp.GetVoices().Item(voice)
        sp.Rate = rate
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
        fmt.Type = SAFT_16KHZ_16BIT_MONO
        stream.Format = fmt
        path.parent.mkdir(parents=True, exist_ok=True)
        stream.Open(str(path), 3)
        sp.AudioOutputStream = stream
        sp.Speak(text)
        stream.Close()
    except Exception:  # pragma: no cover - SAPI no disponible
        return None
    return path if path.exists() and path.stat().st_size > 1000 else None


def load(path: Path) -> AudioArray:
    data, sr = sf.read(str(path), dtype="float32")
    assert sr == 16000
    return np.asarray(data, dtype=np.float32)
