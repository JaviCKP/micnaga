"""Etapa 6: silencios largos acortados dentro de un bloque (``s06_asr.compact``) y tiempos
devueltos a la pista; glosario como prompt de Whisper (DECISIONS §32)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from micnaga.glossary import Glossary
from micnaga.pipeline.stages.s06_asr import (
    MAX_GAP_S,
    compact,
    to_track_time,
    track_regions,
    transcribe_block,
)
from micnaga.types import AudioArray, Word

SR = 100  # baja frecuencia: los tiempos son faciles de leer


def ramp(seconds: float) -> AudioArray:
    return np.arange(int(seconds * SR), dtype=np.float32)


def test_long_silences_are_shortened_and_short_ones_kept() -> None:
    piece = ramp(10.0)  # pista 50-60 s
    regions = [(50.5, 52.0), (52.6, 53.0), (56.0, 57.0), (57.3, 59.0)]
    out, pieces = compact(piece, 50.0, regions, SR)
    # solo el hueco 53-56 (3 s) pasa de MAX_GAP_S: queda en 1 s (0,5 s de cada lado)
    assert len(out) / SR == pytest.approx(10.0 - (3.0 - MAX_GAP_S))
    assert pieces == [(0.0, 50.0, 3.5), (3.5, 55.5, 4.5)]
    assert out[349] == piece[349] and out[350] == piece[550]  # audio real a cada lado del corte
    assert to_track_time(1.0, pieces) == pytest.approx(51.0)
    assert to_track_time(3.5, pieces) == pytest.approx(55.5)
    assert to_track_time(4.0, pieces) == pytest.approx(56.0)
    assert to_track_time(99.0, pieces) == pytest.approx(60.0)  # acotado al final


def test_nothing_to_cut_returns_the_same_audio() -> None:
    piece = ramp(4.0)
    out, pieces = compact(piece, 10.0, [(10.2, 11.0), (11.5, 13.9)], SR)
    assert out is piece and pieces == [(0.0, 10.0, 4.0)]
    out, pieces = compact(piece, 10.0, [], SR)  # sin tramos que lo toquen: tal cual
    assert out is piece


def test_leading_and_trailing_silence_are_capped_too() -> None:
    out, pieces = compact(ramp(10.0), 0.0, [(4.0, 5.0)], SR)
    assert len(out) / SR == pytest.approx(1.0 + 2 * MAX_GAP_S)  # 0,5 s + 0,5 s a cada lado
    assert pieces[0] == (0.0, 0.0, 0.5) and pieces[1][1] == pytest.approx(3.5)


class Recorder:
    """Motor falso: devuelve una palabra al principio y otra al final del audio recibido."""

    name = "fake"

    def __init__(self) -> None:
        self.lengths: list[float] = []

    def transcribe(self, audio: AudioArray, sr: int) -> list[Word]:
        dur = len(audio) / sr
        self.lengths.append(dur)
        return [Word("hola", 0.6, 1.0), Word("adiós", dur - 0.9, dur - 0.6)]

    def close(self) -> None:
        pass


def test_transcribe_block_maps_words_back_to_the_track() -> None:
    audio = ramp(100.0)
    eng: Any = Recorder()
    regions = [(20.0, 22.0), (40.0, 42.0)]
    words = transcribe_block(eng, audio, 19.5, 42.5, SR, regions)
    assert eng.lengths == [pytest.approx(2.5 + MAX_GAP_S + 2.5)]  # 23 s -> 6 s
    assert [(w.w, round(w.s, 2), round(w.e, 2)) for w in words] == [
        ("hola", 20.1, 20.5),
        ("adiós", 41.6, 41.9),
    ]
    # sin tramos (05_vad.json antiguo) se transcribe el bloque entero, como antes
    words = transcribe_block(eng, audio, 19.5, 42.5, SR)
    assert eng.lengths[-1] == pytest.approx(23.0)
    assert words[0].s == pytest.approx(20.1) and words[1].s == pytest.approx(41.6)


def test_track_regions_reads_vad_json() -> None:
    vad = {"mic": {"regions": [[1, 2.5]], "chunks": [[1, 2.5]]}, "system": {"chunks": []}}
    assert track_regions(vad, "mic") == [(1.0, 2.5)]
    assert track_regions(vad, "system") is None


def test_glossary_prompt_lists_canonical_forms_within_a_budget() -> None:
    g = Glossary(["Claude: claudio", "PQC", "Día Q: dia q", "PQC"])
    assert g.prompt() == "Glosario: Claude, PQC, Día Q."
    assert Glossary().prompt() == ""
    long = Glossary([f"Termino{i:03d}" for i in range(200)])
    text = long.prompt(max_chars=100)
    assert len(text) <= 100 and text.endswith(".") and "Termino000" in text


def test_blocks_grouped_by_stage_5_never_exceed_30_s_once_compacted() -> None:
    """``vad.group_regions`` y ``compact`` usan el mismo ``MAX_GAP_S``: el audio que llega al
    ASR cabe siempre en una ventana de Whisper."""
    from micnaga.pipeline.vad import group_regions
    from micnaga.types import SpeechRegion

    rng = np.random.default_rng(7)
    t, regions = 0.0, []
    for _ in range(300):
        t += float(rng.choice([0.1, 0.4, 0.8, 2.0, 6.0, 25.0]))
        d = float(rng.uniform(0.3, 12.0))
        regions.append(SpeechRegion(t, t + d))
        t += d
    audio = np.zeros(int((t + 1) * SR), dtype=np.float32)
    spans = [(r.start, r.end) for r in regions]
    blocks = group_regions(regions, 30.0)
    assert len(blocks) < len(group_regions(regions, 30.0, max_gap_s=None))  # menos ventanas
    for a, b in blocks:
        out, _ = compact(audio[int(a * SR) : int(b * SR)], a, spans, SR)
        assert len(out) / SR <= 30.0 + 2 / SR


def test_a_word_stretched_across_a_cut_does_not_swallow_the_silence() -> None:
    """Un fin de palabra estirado por el modelo que cruza un corte se queda al final de su trozo
    (si no, la palabra duraria todo el silencio quitado)."""
    from micnaga.pipeline.stages.s06_asr import to_track_span

    pieces = [(0.0, 50.0, 3.5), (3.5, 55.5, 4.5)]  # corte: 53,5-55,5 s de la pista
    assert to_track_span(3.0, 3.8, pieces) == pytest.approx((53.0, 53.5))
    assert to_track_span(3.6, 3.9, pieces) == pytest.approx((55.6, 55.9))  # todo en el segundo
    assert to_track_span(1.0, 0.5, pieces) == pytest.approx((51.0, 51.0))  # fin < inicio


def test_compact_seconds_counts_speech_plus_capped_gaps() -> None:
    from micnaga.pipeline.stages.s06_asr import compact_seconds

    regions = [(0.0, 10.0), (12.0, 20.0), (40.0, 45.0), (100.0, 101.0)]
    assert compact_seconds(0.0, 45.0, regions) == pytest.approx(10 + 1 + 8 + 1 + 5)
    assert compact_seconds(0.0, 45.0, None) == pytest.approx(45.0)  # 05_vad.json antiguo
    assert compact_seconds(50.0, 60.0, regions) == pytest.approx(10.0)  # sin tramos: el bloque
    # coincide con lo que de verdad llega al ASR
    out, _ = compact(np.zeros(45 * SR, dtype=np.float32), 0.0, regions, SR)
    assert len(out) / SR == pytest.approx(compact_seconds(0.0, 45.0, regions))
