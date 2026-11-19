"""Post-filtro de hablantes fantasma (``engines.diar.postfilter``): funciones puras."""

from __future__ import annotations

import numpy as np
import pytest

from micnaga.pipeline.engines.diar import postfilter as pf
from micnaga.types import Turn


def _ids(turns: list[Turn]) -> list[str]:
    return [t.speaker for t in turns]


def test_speech_by_speaker_counts_overlaps_once() -> None:
    turns = [Turn("S1", 0, 2), Turn("S1", 1, 3), Turn("S1", 5, 6), Turn("S2", 2, 2.5)]
    totals = pf.speech_by_speaker(turns)
    assert totals == {"S1": pytest.approx(4.0), "S2": pytest.approx(0.5)}
    assert pf.speech_by_speaker([]) == {}


def test_minor_speakers_by_total_and_by_single_short_turn() -> None:
    turns = [
        Turn("S1", 0, 30),
        Turn("S2", 31, 32),
        Turn("S2", 40, 41),  # 2 s en dos turnos: menor por total
        Turn("S3", 50, 53.5),  # 3,5 s en un solo turno: menor (turno unico corto)
        Turn("S4", 60, 62),
        Turn("S4", 70, 72),  # 4 s en dos turnos: se conserva
        Turn("S5", 80, 85),  # 5 s en un turno: se conserva
    ]
    assert pf.minor_speakers(turns) == {"S2", "S3"}


def test_podcast_ghost_at_the_end_is_absorbed_by_the_nearest_turn() -> None:
    # podcast real: «Hablante 3» con un «No» de 0,65 s a los 3:09, tras un silencio largo
    turns = [Turn("S1", 1.3, 4.5), Turn("S2", 6.1, 8.8), Turn("S1", 9.5, 30.0)]
    turns += [Turn("S2", 31.0, 50.0), Turn("S1", 52.0, 112.4), Turn("S3", 189.9, 190.6)]
    out = pf.absorb_minor_speakers(turns)
    assert set(_ids(out)) == {"S1", "S2"}
    assert out[-1] == Turn("S1", 189.9, 190.6)  # el turno mas cercano es de S1


def test_minor_speaker_goes_to_the_most_similar_by_embedding() -> None:
    turns = [Turn("S1", 0, 10), Turn("S3", 10.2, 11.0), Turn("S2", 11.5, 20), Turn("S1", 21, 30)]
    emb = {
        "S1": np.array([1.0, 0.0, 0.0], dtype=np.float32),
        "S2": np.array([0.0, 1.0, 0.0], dtype=np.float32),
        "S3": np.array([0.1, 0.9, 0.1], dtype=np.float32),  # se parece a S2, aunque S1 este al lado
    }
    out = pf.absorb_minor_speakers(turns, emb)
    assert out == [Turn("S1", 0, 10), Turn("S2", 10.2, 20), Turn("S1", 21, 30)]


def test_minor_without_embedding_falls_back_to_adjacency() -> None:
    turns = [Turn("S1", 0, 10), Turn("S2", 20, 30), Turn("S3", 18.0, 18.5)]
    emb = {"S1": np.ones(3, np.float32), "S2": -np.ones(3, np.float32)}  # S3 sin embedding
    out = pf.absorb_minor_speakers(turns, emb)
    assert out == [Turn("S1", 0, 10), Turn("S2", 18.0, 18.5), Turn("S2", 20, 30)]


def test_overlapping_minor_turn_goes_to_the_overlapped_speaker() -> None:
    turns = [Turn("S1", 0, 10), Turn("S2", 9.0, 9.6), Turn("S3", 12, 30)]
    out = pf.absorb_minor_speakers(turns)
    assert out == [Turn("S1", 0, 10), Turn("S2", 12, 30)]


def test_dominant_speaker_is_never_absorbed() -> None:
    turns = [Turn("S1", 0, 1.0), Turn("S2", 2, 4.0), Turn("S3", 6, 6.5)]  # todos menores
    out = pf.absorb_minor_speakers(turns)
    # se queda el que mas habla (el antiguo S2) y absorbe a los demas; renumerado a S1
    assert out == [Turn("S1", 0, 1.0), Turn("S1", 2, 4.0), Turn("S1", 6, 6.5)]
    assert pf.absorb_minor_speakers([Turn("S1", 0, 0.5)]) == [Turn("S1", 0, 0.5)]


def test_real_speakers_are_kept_and_renumbered_by_first_appearance() -> None:
    turns = [Turn("S2", 0, 5), Turn("S1", 5, 10), Turn("S2", 10, 15), Turn("S1", 15, 20)]
    out = pf.absorb_minor_speakers(turns)
    assert out == [Turn("S1", 0, 5), Turn("S2", 5, 10), Turn("S1", 10, 15), Turn("S2", 15, 20)]
    assert pf.absorb_minor_speakers([]) == []
    assert pf.absorb_minor_speakers([Turn("S1", 3, 3)]) == []


def test_merge_and_renumber_joins_contiguous_and_overlapping_turns() -> None:
    turns = [
        Turn("S5", 0, 2),
        Turn("S5", 2.3, 4),  # hueco 0,3 s: se funde
        Turn("S7", 4, 6),
        Turn("S5", 5.5, 8),  # solapa con S7: turno propio
        Turn("S7", 5.8, 6.5),  # solapa con su propio turno anterior: se funde
    ]
    out = pf.merge_and_renumber(turns)
    assert out == [Turn("S1", 0, 4), Turn("S2", 4, 6.5), Turn("S1", 5.5, 8)]
