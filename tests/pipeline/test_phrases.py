from __future__ import annotations

import pytest

from micnaga.pipeline.stages.phrases import (
    build_phrases,
    build_speakers,
    default_speaker_name,
    drop_echo,
    merge_segments,
    text_similarity,
)
from micnaga.pipeline.stages.s05_vad import as_spans
from micnaga.pipeline.stages.s08_assign import assign_speakers, snap_changes_to_sentence_ends
from micnaga.types import Segment, SpeechRegion, Turn, Word


def w(text: str, s: float, e: float) -> Word:
    return Word(text, s, e)


def seg(speaker: str, start: float, end: float, text: str) -> Segment:
    return Segment(start, end, speaker, text, [w(t, start, end) for t in text.split()])


# ------------------------------------------------------------------ asignacion (etapa 8)


def test_word_goes_to_the_turn_with_max_overlap() -> None:
    turns = [Turn("S1", 0, 10), Turn("S2", 9, 20)]
    words = [w("a", 1, 2), w("b", 8.5, 9.8), w("c", 9.4, 11.0), w("d", 15, 16)]
    # b: 1.3 s con S1 frente a 0.8 s con S2
    assert assign_speakers(words, turns) == ["S1", "S1", "S2", "S2"]


def test_word_between_overlapping_turns_picks_larger_overlap() -> None:
    turns = [Turn("S1", 0, 10), Turn("S2", 9, 20)]
    assert assign_speakers([w("x", 9.6, 10.6)], turns) == ["S2"]  # 1.0 s con S2 vs 0.4 con S1


def test_word_without_overlap_goes_to_nearest_turn() -> None:
    turns = [Turn("S1", 0, 5), Turn("S2", 20, 30)]
    words = [w("a", 6, 6.5), w("b", 18, 18.5), w("c", 12.0, 12.4), w("d", 40, 41), w("e", 13, 13.5)]
    # a: 1 s de S1; b: 1.5 s de S2; c: 7 s de S1 frente a 7.6 s de S2; d: tras S2; e: 8 vs 6.5
    assert assign_speakers(words, turns) == ["S1", "S2", "S1", "S2", "S2"]


def test_no_turns_means_single_remote_speaker() -> None:
    assert assign_speakers([w("a", 0, 1), w("b", 1, 2)], []) == ["S1", "S1"]


# ------------------------------------------------------------------ frases / montaje


def test_phrases_split_on_gap_speaker_and_sentence_end() -> None:
    pairs = [
        ("me", w("Hola,", 0.0, 0.3)),
        ("me", w("qué", 0.35, 0.5)),
        ("me", w("tal?", 0.55, 0.9)),  # fin de frase
        ("me", w("Bien", 1.0, 1.3)),
        ("me", w("gracias", 2.5, 2.9)),  # hueco >= 0.8
        ("S1", w("Vale", 0.2, 0.5)),  # otro hablante, solapado en el tiempo
    ]
    phrases = build_phrases(pairs)
    assert [(p.speaker, p.text) for p in phrases] == [
        ("me", "Hola, qué tal?"),
        ("S1", "Vale"),
        ("me", "Bien"),
        ("me", "gracias"),
    ]
    assert phrases[0].start == 0.0 and phrases[0].end == 0.9


def test_remote_phrase_is_cut_at_a_turn_boundary_without_pause() -> None:
    # podcast real (01:13-01:19): la respuesta de S2 llega sin pausa en medio de la frase de S1;
    # antes S1 «saltaba» por encima de S2 y las dos voces acababan en el mismo segmento
    turns = [Turn("S1", 0.0, 2.0), Turn("S2", 2.0, 2.6), Turn("S1", 2.6, 5.0)]
    words = [
        w("Uy,", 0.2, 0.5),
        w("qué", 0.6, 0.9),
        w("bien,", 1.0, 1.5),
        w("qué", 1.6, 1.9),
        w("hora", 2.05, 2.3),  # S2
        w("son", 2.32, 2.55),  # S2
        w("Faltan", 2.65, 3.0),  # S1 otra vez, a 0,75 s de su palabra anterior
        w("diez", 3.1, 3.4),
    ]
    pairs = list(zip(assign_speakers(words, turns), words, strict=True))
    phrases = build_phrases(pairs)
    assert [(p.speaker, p.text) for p in phrases] == [
        ("S1", "Uy, qué bien, qué"),
        ("S2", "hora son"),
        ("S1", "Faltan diez"),
    ]
    # y el montaje no vuelve a unir las dos frases de S1 por encima de S2
    merged = merge_segments(phrases)
    assert [s.speaker for s in merged] == ["S1", "S2", "S1"]


def _timed(texts: str, step: float = 0.3) -> list[Word]:
    return [w(t, i * step, i * step + step) for i, t in enumerate(texts.split())]


def test_speaker_change_inside_a_sentence_moves_to_the_sentence_end() -> None:
    # podcast real: la frontera de la diarizacion cae en «hemos | llegado a la vida.»
    times = [73.82, 74.38, 74.62, 74.86, 74.94, 74.94, 75.1, 75.34, 75.58, 75.82, 75.9, 76.3]
    texts = "Juan, hemos llegado a la vida. Porque debo ir a trabajar."
    words = [w(t, a, b) for t, a, b in zip(texts.split(), times, times[1:], strict=False)]
    spk = ["S2", "S2", "S1", "S1", "S1", "S1", "S1", "S1", "S1", "S1", "S1"]
    out = snap_changes_to_sentence_ends(words, spk)
    assert out == ["S2"] * 6 + ["S1"] * 5
    # retrasar: «Uy, qué bien. | ¿Qué» -> el «¿Qué» suelto vuelve con su oracion
    words = _timed("Vale, perfecto. Hasta luego entonces. ¿Qué hora es?")
    spk = ["S1", "S1", "S1", "S1", "S1", "S2", "S2", "S2"]
    assert snap_changes_to_sentence_ends(words, spk) == spk  # ya cae en fin de oracion
    spk = ["S1", "S1", "S1", "S1", "S2", "S2", "S2", "S2"]  # «Hasta luego | entonces.»
    assert snap_changes_to_sentence_ends(words, spk) == ["S1"] * 5 + ["S2"] * 3


def test_sentence_end_snap_keeps_real_short_replies_and_pauses() -> None:
    # «Vale.» de S2 sin pausa tras una oracion sin punto de S1: S2 solo tiene esa palabra
    words = _timed("entonces lo hacemos asi Vale. Bien")
    spk = ["S1", "S1", "S1", "S1", "S2", "S1"]
    assert snap_changes_to_sentence_ends(words, spk) == spk
    # con pausa en el cambio no se toca nada
    words = [w("vamos", 0.0, 0.3), w("a", 0.3, 0.5), w("ver", 1.0, 1.3), w("eso.", 1.3, 1.6)]
    spk = ["S1", "S1", "S2", "S2"]
    assert snap_changes_to_sentence_ends(words, spk) == spk
    # demasiado lejos (> 0,6 s) o demasiadas palabras: tampoco
    words = _timed("uno dos tres cuatro cinco seis siete ocho.")
    spk = ["S1", "S1", "S2", "S2", "S2", "S2", "S2", "S2"]
    assert snap_changes_to_sentence_ends(words, spk) == spk
    assert snap_changes_to_sentence_ends([], []) == []


def test_mic_phrase_is_not_cut_by_overlapping_remote_words() -> None:
    pairs = [
        ("me", w("Yo", 0.0, 0.3)),
        ("S1", w("sí", 0.35, 0.5)),  # pista remota, solapada en el tiempo con el micro
        ("me", w("creo", 0.4, 0.7)),
        ("me", w("que", 0.75, 0.9)),
    ]
    phrases = build_phrases(pairs)
    assert [(p.speaker, p.text) for p in phrases] == [("me", "Yo creo que"), ("S1", "sí")]


def test_merge_same_speaker_when_gap_below_threshold() -> None:
    phrases = [
        seg("me", 0, 2, "Primera frase."),
        seg("me", 3.4, 5, "Segunda frase."),  # hueco 1.4 < 1.5: se une
        seg("me", 6.6, 8, "Tercera."),  # hueco 1.6 >= 1.5: no
        seg("S1", 8.2, 9, "Respuesta."),
        seg("me", 9.3, 10, "Otra vez yo."),  # no consecutiva de la anterior del mismo hablante
    ]
    merged = merge_segments(phrases, merge_gap_s=1.5)
    assert [(m.speaker, m.text) for m in merged] == [
        ("me", "Primera frase. Segunda frase."),
        ("me", "Tercera."),
        ("S1", "Respuesta."),
        ("me", "Otra vez yo."),
    ]
    assert merged[0].start == 0 and merged[0].end == 5
    assert len(merged[0].words) == 4


def test_merge_exactly_at_threshold_is_not_merged() -> None:
    merged = merge_segments([seg("me", 0, 1, "a."), seg("me", 2.5, 3, "b.")], merge_gap_s=1.5)
    assert len(merged) == 2


def test_long_paragraphs_are_split_at_a_sentence_end() -> None:
    phrases = [seg("S1", i * 10.0, i * 10.0 + 9.5, f"Frase {i}.") for i in range(8)]
    merged = merge_segments(phrases, merge_gap_s=1.5, max_paragraph_s=45)
    assert len(merged) > 1
    assert all(m.end - m.start <= 45 + 10 for m in merged)


# ------------------------------------------------------------------ eco (etapa 9)


def test_echo_phrase_overlapping_in_time_and_similar_is_dropped() -> None:
    remote = seg("S1", 10, 14, "Necesitamos decidir el proveedor antes del viernes")
    echo = seg("me", 10.05, 14.1, "necesitamos decidir el proveedor antes del viernes")
    kept, dropped = drop_echo([remote, echo], similarity=0.8)
    assert kept == [remote] and dropped == [echo]


def test_similar_but_not_overlapping_phrase_is_kept() -> None:
    remote = seg("S1", 10, 14, "Necesitamos decidir el proveedor antes del viernes")
    later = seg("me", 60, 64, "Necesitamos decidir el proveedor antes del viernes")
    kept, dropped = drop_echo([remote, later], similarity=0.8)
    assert later in kept and not dropped


def test_overlapping_but_different_phrase_is_kept() -> None:
    remote = seg("S1", 10, 14, "Necesitamos decidir el proveedor antes del viernes")
    mine = seg("me", 11, 13, "Sí, estoy de acuerdo contigo")
    kept, dropped = drop_echo([remote, mine], similarity=0.8)
    assert mine in kept and not dropped


def test_echo_chunked_differently_is_caught_against_remote_words_in_window() -> None:
    remote = seg(
        "S1", 10, 20, "Primero revisamos los costes de la nube y despues hablamos de plazos"
    )
    echo = seg("me", 10.1, 14.6, "primero revisamos los costes de la nube")
    _kept, dropped = drop_echo([remote, echo], similarity=0.8)
    assert dropped == [echo]


def test_short_backchannel_is_not_treated_as_echo() -> None:
    remote = seg("S1", 10, 14, "Entonces sí, lo dejamos para el martes por la mañana")
    mine = seg("me", 11, 11.5, "sí")
    kept, _ = drop_echo([remote, mine], similarity=0.8)
    assert mine in kept


def test_threshold_is_configurable() -> None:
    remote = seg("S1", 10, 14, "Necesitamos decidir el proveedor antes del viernes")
    approx = seg("me", 10, 14, "Necesitamos decidir el proveedor antes del lunes quizas")
    assert drop_echo([remote, approx], similarity=0.99)[1] == []
    assert drop_echo([remote, approx], similarity=0.7)[1] == [approx]


def test_text_similarity_normalizes() -> None:
    assert text_similarity("¡Hola, qué tal!", "hola que tal") == pytest.approx(1.0)
    assert text_similarity("", "algo") == 0.0


# ------------------------------------------------------------------ hablantes / VAD


def test_build_speakers_orders_me_first_and_names() -> None:
    segs = [seg("S2", 0, 1, "a"), seg("S1", 1, 2, "b"), seg("me", 2, 3, "c")]
    speakers = build_speakers(segs, {"S1": ("Laura", 0.84)})
    assert [(s.id, s.name, s.voiceprint_match) for s in speakers] == [
        ("me", "Yo", None),
        ("S1", "Laura", 0.84),
        ("S2", "Hablante 2", None),
    ]


def test_build_speakers_lists_only_speakers_with_segments() -> None:
    # micro callado (video de YouTube): «Yo» no es participante; un nombre sin segmentos tampoco
    segs = [seg("S1", 0, 1, "a"), seg("S2", 1, 2, "b")]
    speakers = build_speakers(segs, {"S3": ("Marta", 0.9)})
    assert [s.id for s in speakers] == ["S1", "S2"]
    assert build_speakers([]) == []
    assert default_speaker_name("S10") == "Hablante 10"


def test_as_spans_normalizes_group_regions_outputs() -> None:
    r1, r2 = SpeechRegion(0, 5), SpeechRegion(6, 9)
    assert as_spans([(0.0, 9.0), (10, 12)]) == [(0.0, 9.0), (10.0, 12.0)]
    assert as_spans([r1, r2]) == [(0.0, 5.0), (6.0, 9.0)]
    assert as_spans([[r1, r2]]) == [(0.0, 9.0)]
    assert as_spans([]) == []
