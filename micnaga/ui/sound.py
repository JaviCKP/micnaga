"""Sonido opcional del popup: dos notas muy suaves generadas en memoria (sin ficheros).

Desactivado por defecto (``ui.sound``) y en silencio si Windows pide no molestar
(``winapi.do_not_disturb``). ``winsound`` solo existe en Windows: se importa al reproducir.
"""

from __future__ import annotations

import io
import math
import struct
import wave
from collections.abc import Callable

SAMPLE_RATE = 22050


def chime_wav(*, volume: float = 0.12) -> bytes:
    """Dos sinusoides cortas (E5 -> A5, ~190 ms) con fundido, amplitud baja. WAV PCM 16 bits."""
    notes = ((659.25, 0.085), (880.0, 0.105))
    frames = bytearray()
    for freq, dur in notes:
        n = int(SAMPLE_RATE * dur)
        for i in range(n):
            t = i / SAMPLE_RATE
            env = math.sin(math.pi * i / n) ** 2  # ataque y cola suaves
            v = volume * env * math.sin(2 * math.pi * freq * t)
            frames += struct.pack("<h", int(v * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def play_chime(
    *,
    enabled: bool,
    dnd: Callable[[], bool],
    player: Callable[[bytes], None] | None = None,
) -> bool:
    """Suena si ``enabled`` y no hay DND. Devuelve si sono (el popup aparece igual si no)."""
    if not enabled or dnd():
        return False
    data = chime_wav()
    if player is not None:
        player(data)
        return True
    try:
        import winsound

        winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except (ImportError, RuntimeError):  # pragma: no cover - sin altavoces
        return False
    return True
