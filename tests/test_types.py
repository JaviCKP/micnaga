from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from micnaga.types import (
    CaptureMeta,
    DeviceChange,
    EnginesInfo,
    Marker,
    MeetingInfo,
    PrivatePause,
    Segment,
    Speaker,
    SyncMark,
    TrackInfo,
    Transcript,
    Turn,
    Word,
)

SPEC_JSON = """
{
  "version": 1,
  "meeting": {"title": "Revisión semanal", "start": "2026-11-18T10:30:00+01:00", "duration_s": 2820,
              "source": "meet", "meet_code": "abc-defg-hij", "output_device": "headphones"},
  "speakers": [{"id": "me", "name": "Yo"}, {"id": "S1", "name": "Laura", "voiceprint_match": 0.84}],
  "segments": [{"start": 12.3, "end": 14.1, "speaker": "me", "text": "Buenos días, ¿me oís bien?",
                "words": [{"w": "Buenos", "s": 12.3, "e": 12.6, "p": 0.97}]}],
  "markers": [{"t": 724.0, "label": "Decidir proveedor antes del viernes"}],
  "engines": {"asr": "parakeet-tdt-0.6b-v3-int8", "diarization": "diarize-x.y"}
}
"""


def test_transcript_parses_spec_example_and_roundtrips() -> None:
    t = Transcript.from_json(SPEC_JSON)
    assert t.meeting.meet_code == "abc-defg-hij"
    assert t.speakers[1] == Speaker("S1", "Laura", 0.84)
    assert t.speakers[0].voiceprint_match is None
    assert t.segments[0].words == [Word("Buenos", 12.3, 12.6, 0.97)]
    assert t.markers == [Marker(724.0, "Decidir proveedor antes del viernes")]
    again = Transcript.from_json(t.to_json())
    assert again == t
    # exactamente el esquema de la spec (sin claves extra salvo aec opcional)
    assert json.loads(t.to_json()) == json.loads(SPEC_JSON)


def test_transcript_rejects_other_version() -> None:
    data: dict[str, Any] = json.loads(SPEC_JSON)
    data["version"] = 2
    with pytest.raises(ValueError):
        Transcript.from_dict(data)
    with pytest.raises(ValueError):
        Transcript.from_json("[]")


def test_value_types_are_frozen() -> None:
    w = Word("a", 0.0, 1.0, 0.5)
    with pytest.raises(AttributeError):
        w.w = "b"  # type: ignore[misc]
    assert Turn("S1", 0.0, 1.0).speaker == "S1"


def test_minimal_transcript() -> None:
    t = Transcript(
        meeting=MeetingInfo("T", "2026-01-01T00:00:00+00:00", 1.0),
        speakers=[],
        segments=[Segment(0.0, 1.0, "me", "hola")],
        markers=[],
        engines=EnginesInfo(asr="x", aec="webrtc"),
    )
    assert Transcript.from_json(t.to_json()) == t


def _capture() -> CaptureMeta:
    return CaptureMeta(
        started_at="2026-11-18T10:30:00+01:00",
        mic=TrackInfo(
            "mic.wav",
            "Micro (Realtek)",
            16000,
            160000,
            [SyncMark(0, 0.0), SyncMark(160000, 10.002)],
        ),
        system=TrackInfo("system.wav", "Altavoces", 16000, 160000),
        output_kind="speakers",
        confirmed=True,
        meet_title="Revisión",
        meet_code="abc-defg-hij",
        pauses=[PrivatePause(at_s=30.0, wall_s=12.5)],
        markers=[Marker(5.0, "ojo")],
        device_changes=[DeviceChange(8.0, "system", "Auriculares", "headphones")],
        extra={"capture_app_version": "0.1.0"},
    )


def test_capture_meta_roundtrip_and_extra_preserved() -> None:
    c = _capture()
    d = c.to_dict()
    assert d["capture_app_version"] == "0.1.0"
    assert CaptureMeta.from_json(c.to_json()) == c


def test_capture_meta_rejects_other_version() -> None:
    d = _capture().to_dict()
    d["version"] = 9
    with pytest.raises(ValueError):
        CaptureMeta.from_dict(d)


def test_audio_convention_dtype() -> None:
    from tests.synth import sine

    a = sine(440, 0.1)
    assert a.dtype == np.float32
    assert a.ndim == 1
