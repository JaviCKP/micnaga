"""Reuniones presenciales: varias personas por el mismo micro (``diar.room`` y etapas 7-9)."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from micnaga.bench.fakes import FakeDiarizer, fake_deps_for_session, load_script
from micnaga.bench.synthetic import SyntheticSpec
from micnaga.config import config_from_dict
from micnaga.pipeline import hq
from micnaga.pipeline.engines.diar import room
from micnaga.pipeline.stages.base import Deps
from micnaga.pipeline.stages.phrases import build_phrases, drop_echo
from micnaga.store.db import Database
from micnaga.types import Segment, Speaker, Transcript, Turn, Word, is_mic_speaker
from tests.pipeline.helpers import make_session, open_db, runner_for


def vec(i: int) -> np.ndarray:  # type: ignore[type-arg]
    v = np.full(8, 0.05, dtype=np.float32)
    v[i] = 1.0
    return v


# ------------------------------------------------------------------ etiquetas (puro)


def test_loudest_mic_speaker_is_me_and_others_continue_the_numbering() -> None:
    turns = [Turn("A", 0, 10), Turn("B", 10, 25), Turn("C", 25, 40)]
    out, reason = room.label_room_speakers(
        turns,
        energy={"A": 1.0, "B": 9.0, "C": 2.0},
        speech_s={"A": 10.0, "B": 15.0, "C": 15.0},
        mic_embeddings={"A": vec(0), "B": vec(1), "C": vec(2)},
        remote_embeddings={"S1": vec(5), "S2": vec(6)},
        first_number=3,
    )
    assert [t.speaker for t in out] == ["P3", "me", "P4"]
    assert reason == {"B": "me", "A": "room", "C": "room"}


def test_echo_same_voice_and_tiny_speakers_stay_me() -> None:
    turns = [Turn("A", 0, 30), Turn("E", 30, 45), Turn("V", 45, 60), Turn("T", 60, 63)]
    out, reason = room.label_room_speakers(
        turns,
        energy={"A": 10.0, "E": 1.0, "V": 1.0, "T": 1.0},
        speech_s={"A": 30.0, "E": 15.0, "V": 15.0, "T": 3.0},
        mic_embeddings={"A": vec(0), "E": vec(5), "V": vec(0) * 0.9 + vec(1) * 0.1, "T": vec(3)},
        remote_embeddings={"S1": vec(5)},
        first_number=2,
    )
    assert {t.speaker for t in out} == {"me"}
    assert reason == {"A": "me", "E": "echo", "V": "same_voice", "T": "small"}


def test_fragments_go_to_the_neighbouring_turn_not_to_a_new_person() -> None:
    """Voz real: un «tercer hablante» de 18 s hecho solo de trocitos de < 1,5 s."""
    frags = [Turn("F", 10.0 + 6 * i, 11.0 + 6 * i) for i in range(20)]  # 20 s, todo < 2 s
    turns = [Turn("A", 0, 10), Turn("B", 60, 80), *frags]
    out, reason = room.label_room_speakers(
        turns,
        energy={"A": 9.0, "B": 2.0, "F": 1.0},
        speech_s={"A": 10.0, "B": 20.0, "F": 20.0},
        mic_embeddings={"A": vec(0), "B": vec(1), "F": vec(2)},
        remote_embeddings={},
        first_number=1,
    )
    assert reason["F"] == "small" and reason["B"] == "room"
    assert {t.speaker for t in out} == {"me", "P1"}
    by_start = {t.start: t.speaker for t in out}
    assert by_start[10.0] == "me"  # pegado al turno de A («Yo»)
    assert by_start[64.0] == "P1"  # dentro del turno de B


def test_next_speaker_number_and_mic_speaker_ids() -> None:
    assert room.next_speaker_number([]) == 1
    assert room.next_speaker_number([Turn("S1", 0, 1), Turn("S4", 1, 2)]) == 5
    assert is_mic_speaker("me") and is_mic_speaker("P3")
    assert not is_mic_speaker("S1") and not is_mic_speaker("Pepe")


def test_speech_energy_sums_each_speaker() -> None:
    audio = np.zeros(16000 * 4, dtype=np.float32)
    audio[: 16000 * 2] = 0.5
    e = room.speech_energy(audio, 16000, [Turn("A", 0, 2), Turn("B", 2, 4), Turn("A", 3, 4)])
    assert e["A"] == pytest.approx(0.25 * 32000) and e["B"] == 0.0


# ------------------------------------------------------------------ frases y eco


def w(text: str, s: float) -> Word:
    return Word(text, s, s + 0.4, 0.9)


def test_room_speakers_share_the_mic_track_and_their_echo_is_dropped() -> None:
    pairs = [
        ("me", w("hola.", 0.0)),
        ("P3", w("que", 0.5)),
        ("P3", w("tal", 0.9)),
        ("S1", w("buenos", 0.6)),
        ("S1", w("dias", 1.0)),
        ("P3", w("buenos dias", 5.0)),
        ("S1", w("buenos dias", 5.1)),
    ]
    phrases = build_phrases(pairs)
    # el remoto S1 solapa en el tiempo con P3 sin cortarle la frase: pistas separadas
    assert [(p.speaker, p.text) for p in phrases if p.start < 2] == [
        ("me", "hola."),
        ("P3", "que tal"),
        ("S1", "buenos dias"),
    ]
    kept, dropped = drop_echo(phrases)
    assert [(p.speaker, p.text) for p in dropped] == [("P3", "buenos dias")]
    assert len(kept) == len(phrases) - 1


def test_hq_keeps_room_speakers() -> None:
    t = Transcript(
        meeting=None,  # type: ignore[arg-type]
        speakers=[Speaker("me", "Yo"), Speaker("P2", "Hablante 2"), Speaker("S1", "Ana")],
        segments=[
            Segment(0, 2, "me", "a", [w("a", 0.0)]),
            Segment(3, 5, "P2", "b", [w("b", 3.0)]),
            Segment(0, 5, "S1", "c", [w("c", 1.0)]),
        ],
        markers=[],
        engines=None,  # type: ignore[arg-type]
    )
    out = hq.assign_words(t, [w("x", 0.2), w("y", 3.2)], [w("z", 1.1)])
    assert sorted((sp, x.w) for sp, x in out) == [("P2", "y"), ("S1", "z"), ("me", "x")]


# ------------------------------------------------------------------ config


@pytest.mark.parametrize("old", ["onnx", "pyannote"])
def test_retired_diarizers_migrate_to_diarize(old: str) -> None:
    cfg = config_from_dict({"processing": {"diarization": old}})
    assert cfg.processing.diarization == "diarize"


# ------------------------------------------------------------------ pipeline entero


class RoomDiarizer(FakeDiarizer):
    """Remotos como ``FakeDiarizer``; en el micro (segunda llamada) parte la voz en dos personas."""

    def __init__(self, script: Any) -> None:
        super().__init__(script)
        self.calls = 0

    def diarize(self, audio, sr, n_speakers):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            return super().diarize(audio, sr, n_speakers)
        from micnaga.bench.fakes import ASR_MIN_SILENCE_MS, energy_vad

        regions = list(energy_vad(audio, sr, 250, ASR_MIN_SILENCE_MS, 0))
        return [Turn("S1" if i % 2 == 0 else "S2", r.start, r.end) for i, r in enumerate(regions)]

    def embed(self, audio, sr, turns):  # type: ignore[no-untyped-def]
        out = super().embed(audio, sr, turns)
        if self.calls < 2:
            return out
        # voces del micro: distintas de las remotas (el fake solo mira la etiqueta)
        return {sid: np.roll(v, 4) for sid, v in out.items()}


@pytest.fixture
def db() -> Database:
    database = open_db()
    yield database  # type: ignore[misc]
    database.close()


def test_in_person_meeting_gets_room_speakers(db: Database) -> None:
    session = make_session("room", SyntheticSpec(duration_s=150.0))
    base = fake_deps_for_session(session)
    diar = RoomDiarizer(load_script(session))
    deps = Deps(**{**base.__dict__, "create_diarizer": lambda _c: diar})
    result = runner_for(deps=deps, db=db, keep_work=True, rename=False).run(session)
    t = result.transcript
    assert result.status == "done" and t is not None, result.reason
    assert diar.calls == 2
    ids = {s.id for s in t.speakers}
    room_ids = sorted(i for i in ids if i.startswith("P"))
    assert room_ids, ids
    remote_max = max(int(i[1:]) for i in ids if i.startswith("S"))
    assert int(room_ids[0][1:]) == remote_max + 1  # «Hablante N» nunca se repite
    assert "me" in ids
    work = result.session_dir / "work"
    mic_turns = json.loads((work / "07_turns_mic.json").read_text("utf-8"))
    assert {x["speaker"] for x in mic_turns} == {"me", *room_ids}
    marker = json.loads((work / "07_diarize.done.json").read_text("utf-8"))
    assert marker["info"]["mic"]["room_speakers"] == len(room_ids)


def test_single_voice_on_the_mic_stays_me(db: Database) -> None:
    session = make_session("solo", SyntheticSpec(duration_s=150.0))
    result = runner_for(db=db, keep_work=True, rename=False).run(session)
    t = result.transcript
    assert result.status == "done" and t is not None, result.reason
    assert not any(s.id.startswith("P") for s in t.speakers)
