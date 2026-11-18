"""Motores falsos para pruebas y para el modo ``--synthetic`` del banco de pruebas.

No hay modelos: reconocen las sesiones de ``micnaga.bench.synthetic`` por la frecuencia
fundamental de cada enunciado (cada uno tiene la suya) y devuelven el texto/hablante del guion
``synthetic_script.json``. Sirven para ejercitar el pipeline de punta a punta (reanudacion, eco,
asignacion, salidas), **no** para medir calidad: el banco de pruebas lo indica en el informe.

* ``energy_vad``: VAD por energia (tramas de 20 ms) con los mismos parametros que Silero;
* ``FakeAsr`` / ``FakeDiarizer`` (con ``embed``: huella de un solo valor por hablante) /
  ``PassthroughAec``;
* ``fake_deps(script)`` y ``fake_deps_for_session(session)`` (``MICNAGA_FAKE_ENGINES=1``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

from micnaga.pipeline.stages.base import Deps
from micnaga.types import AudioArray, CaptureMeta, SpeechRegion, Turn, Word

if TYPE_CHECKING:
    from micnaga.config import Config

FRAME_S = 0.02
ENERGY_THRESHOLD = 0.012
F0_TOLERANCE_HZ = 3.0
F0_BAND = (80.0, 760.0)
ASR_MIN_SILENCE_MS = 400


def energy_vad(
    audio: AudioArray, sr: int, min_speech_ms: int, min_silence_ms: int, pad_ms: int
) -> list[SpeechRegion]:
    frame = max(1, int(FRAME_S * sr))
    n = len(audio) // frame
    if n == 0:
        return []
    rms = np.sqrt(np.mean(audio[: n * frame].reshape(n, frame).astype(np.float64) ** 2, axis=1))
    active = rms > ENERGY_THRESHOLD
    max_gap = max(1, round(min_silence_ms / 1000 / FRAME_S))
    spans: list[list[int]] = []
    start: int | None = None
    last = -1
    for i, a in enumerate(active):
        if a:
            if start is None:
                start = i
            elif i - last > max_gap:
                spans.append([start, last + 1])
                start = i
            last = i
    if start is not None:
        spans.append([start, last + 1])
    duration = len(audio) / sr
    out: list[SpeechRegion] = []
    for a0, b0 in spans:
        s, e = a0 * FRAME_S, b0 * FRAME_S
        if e - s < min_speech_ms / 1000:
            continue
        s = max(0.0, s - pad_ms / 1000)
        e = min(duration, e + pad_ms / 1000)
        if out and s <= out[-1].end:
            out[-1] = SpeechRegion(out[-1].start, max(out[-1].end, e))
        else:
            out.append(SpeechRegion(s, e))
    return out


def estimate_f0(audio: AudioArray, sr: int) -> float:
    """Frecuencia fundamental dominante (pico del espectro en 80-760 Hz)."""
    x = audio[: 8 * sr].astype(np.float64)
    if len(x) < sr // 10:
        return 0.0
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x)), n=4 * len(x)))
    freqs = np.fft.rfftfreq(4 * len(x), 1.0 / sr)
    band = (freqs >= F0_BAND[0]) & (freqs <= F0_BAND[1])
    return float(freqs[band][int(np.argmax(spectrum[band]))])


class _Script:
    def __init__(self, utterances: Sequence[dict[str, Any]]) -> None:
        self.table = {float(u["f0"]): u for u in utterances}

    def lookup(self, f0: float) -> dict[str, Any] | None:
        if not self.table or f0 <= 0:
            return None
        nearest = min(self.table, key=lambda k: abs(k - f0))
        return self.table[nearest] if abs(nearest - f0) <= F0_TOLERANCE_HZ else None


class FakeAsr:
    name = "fake-asr"

    def __init__(self, script: Sequence[dict[str, Any]] = ()) -> None:
        self._script = _Script(script)
        self.calls = 0
        self.closed = False

    def transcribe(self, audio: AudioArray, sr: int) -> list[Word]:
        self.calls += 1
        words: list[Word] = []
        for region in energy_vad(audio, sr, 250, ASR_MIN_SILENCE_MS, 0):
            piece = audio[int(region.start * sr) : int(region.end * sr)]
            utt = self._script.lookup(estimate_f0(piece, sr))
            if utt is None:
                continue
            tokens = str(utt["text"]).split()
            step = region.duration / len(tokens)
            for i, tok in enumerate(tokens):
                words.append(
                    Word(tok, region.start + i * step, region.start + (i + 1) * step - 0.02, 0.9)
                )
        return words

    def close(self) -> None:
        self.closed = True


class FakeDiarizer:
    name = "fake-diarizer"

    def __init__(self, script: Sequence[dict[str, Any]] = ()) -> None:
        self._script = _Script(script)
        self.closed = False

    def diarize(self, audio: AudioArray, sr: int, n_speakers: int | None) -> list[Turn]:
        turns: list[Turn] = []
        for region in energy_vad(audio, sr, 250, ASR_MIN_SILENCE_MS, 0):
            piece = audio[int(region.start * sr) : int(region.end * sr)]
            utt = self._script.lookup(estimate_f0(piece, sr))
            if utt is not None and utt["speaker"] != "me":
                turns.append(Turn(str(utt["speaker"]), region.start, region.end))
        return turns

    def embed(
        self, audio: AudioArray, sr: int, turns: list[Turn]
    ) -> dict[str, NDArray[np.float32]]:
        out: dict[str, NDArray[np.float32]] = {}
        for t in turns:
            if t.speaker not in out:
                vec = np.full(8, 0.05, dtype=np.float32)
                vec[int("".join(c for c in t.speaker if c.isdigit()) or 0) % 8] = 1.0
                out[t.speaker] = vec
        return out

    def close(self) -> None:
        self.closed = True


class PassthroughAec:
    name = "fake-aec"

    def process(self, mic: AudioArray, ref: AudioArray, sr: int) -> AudioArray:
        return mic.copy()


def identity_align(
    mic: AudioArray, system: AudioArray, meta: CaptureMeta
) -> tuple[AudioArray, AudioArray]:
    return mic, system


def fake_deps(script: Sequence[dict[str, Any]] = ()) -> Deps:
    """``Deps`` con motores falsos (VAD de energia, ASR/diarizador por guion, AEC transparente)."""

    def detect(audio: AudioArray, sr: int, config: Config) -> list[SpeechRegion]:
        v = config.vad
        return energy_vad(audio, sr, v.min_speech_ms, v.min_silence_ms, v.pad_ms)

    def group(regions: Sequence[SpeechRegion], max_len_s: float) -> list[tuple[float, float]]:
        blocks: list[tuple[float, float]] = []
        for r in sorted(regions, key=lambda r: r.start):
            if blocks and r.end - blocks[-1][0] <= max_len_s:
                blocks[-1] = (blocks[-1][0], r.end)
            else:
                blocks.append((r.start, r.end))
        return blocks

    return Deps(
        create_asr=lambda _c: FakeAsr(script),
        create_diarizer=lambda _c: FakeDiarizer(script),
        create_aec=lambda _c: PassthroughAec(),
        detect_speech=detect,
        group_regions=group,
        align_tracks=identity_align,
    )


def load_script(session: Path) -> list[dict[str, Any]]:
    file = session / "synthetic_script.json"
    if not file.is_file():
        return []
    return list(json.loads(file.read_text(encoding="utf-8")).get("utterances", []))


def fake_deps_for_session(session: Path) -> Deps:
    return fake_deps(load_script(session))
