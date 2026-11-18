"""Toast «Lista» y sonido opcional del popup."""

from __future__ import annotations

import io
import wave
from typing import Any

from micnaga.ui import sound, toast


def test_ready_text_matches_the_spec_example() -> None:
    assert (
        toast.format_ready_text("Revisión semanal", 47 * 60, 3)
        == "Revisión semanal · 47 min · 3 hablantes"
    )
    assert toast.format_ready_text("Sync", 20, 1) == "Sync · 1 min · 1 hablante"


def test_parse_arguments() -> None:
    assert (
        toast.parse_arguments("open:2026-11-18_1030_revision-semanal")
        == "2026-11-18_1030_revision-semanal"
    )
    assert toast.parse_arguments("open:") is None
    assert toast.parse_arguments("other:thing") is None


class _Toaster:
    def __init__(self) -> None:
        self.shown: list[Any] = []

    def show_toast(self, t: Any) -> None:
        self.shown.append(t)


def test_toast_has_open_button_and_calls_back_with_the_folder() -> None:
    opened: list[str] = []
    fake = _Toaster()
    n = toast.ToastNotifier(opened.append, toaster=fake)
    n.show_ready("2026-11-18_1030_revision-semanal", "Revisión semanal", 2820, 3)
    t = fake.shown[0]
    assert t.text_fields == ["Lista", "Revisión semanal · 47 min · 3 hablantes"]
    assert [a.content for a in t.actions] == ["Abrir"]
    assert t.actions[0].arguments == "open:2026-11-18_1030_revision-semanal"

    class Args:
        arguments = "open:2026-11-18_1030_revision-semanal"

    t.on_activated(Args())
    assert opened == ["2026-11-18_1030_revision-semanal"]


def test_toast_activation_ignores_foreign_arguments_and_callback_errors() -> None:
    def boom(_f: str) -> None:
        raise RuntimeError("x")

    n = toast.ToastNotifier(boom, toaster=_Toaster())

    class A:
        arguments = "garbage"

    n._activated(A())  # no lanza

    class B:
        arguments = "open:abc"

    n._activated(B())  # el callback roto no se propaga


def test_chime_is_a_short_quiet_valid_wav() -> None:
    data = sound.chime_wav()
    with wave.open(io.BytesIO(data)) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        dur = w.getnframes() / w.getframerate()
        frames = w.readframes(w.getnframes())
    assert 0.1 < dur < 0.4
    peak = max(
        abs(int.from_bytes(frames[i : i + 2], "little", signed=True))
        for i in range(0, len(frames), 2)
    )
    assert peak < 0.2 * 32767  # «muy suave»


def test_chime_is_silent_when_disabled_or_dnd() -> None:
    played: list[bytes] = []
    assert not sound.play_chime(enabled=False, dnd=lambda: False, player=played.append)
    assert not sound.play_chime(enabled=True, dnd=lambda: True, player=played.append)
    assert played == []
    assert sound.play_chime(enabled=True, dnd=lambda: False, player=played.append)
    assert len(played) == 1
