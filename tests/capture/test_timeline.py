"""``Timeline``: tiempo de grabacion sin pausas, margen de guarda y recorte de paquetes."""

from __future__ import annotations

import pytest

from micnaga.capture.timeline import Timeline
from tests.capture.helpers import ManualClock, s_to_ns


def _tl(guard: float = 0.3) -> tuple[Timeline, ManualClock]:
    clock = ManualClock()
    return Timeline(clock, t0_ns=0, pause_guard_s=guard), clock


def test_rt_excludes_pauses_and_freezes_inside_them() -> None:
    tl, clock = _tl()
    assert tl.pause(s_to_ns(10))
    clock.set_s(15)
    assert tl.rt(s_to_ns(12)) == pytest.approx(10.0)  # congelado dentro de la pausa
    at_s, wall_s = tl.resume(s_to_ns(20)) or (0, 0)
    assert at_s == pytest.approx(10.0) and wall_s == pytest.approx(10.3)  # 10 s + 0.3 de guarda
    assert tl.rt(s_to_ns(20.3)) == pytest.approx(10.0)
    assert tl.rt(s_to_ns(25)) == pytest.approx(14.7)  # 25 - 10.3
    clock.set_s(25)
    assert tl.elapsed_s() == pytest.approx(14.7)


def test_markers_time_excludes_pauses() -> None:
    tl, clock = _tl(guard=0.0)
    tl.pause(s_to_ns(5))
    tl.resume(s_to_ns(8))
    tl.pause(s_to_ns(12))
    tl.resume(s_to_ns(13))
    clock.set_s(20)
    assert tl.elapsed_s() == pytest.approx(20 - 3 - 1)


def test_pause_resume_are_idempotent_and_state_flags() -> None:
    tl, _ = _tl()
    assert not tl.paused
    assert tl.resume(1) is None
    assert tl.pause(s_to_ns(1))
    assert tl.paused and not tl.pause(s_to_ns(2))
    assert tl.resume(s_to_ns(3)) is not None
    assert not tl.paused and tl.resume(s_to_ns(4)) is None


def test_repause_inside_guard_merges() -> None:
    tl, _ = _tl(guard=0.3)
    tl.pause(s_to_ns(1))
    tl.resume(s_to_ns(2))  # efectiva hasta 2.3
    assert tl.pause(s_to_ns(2.1))  # se fusiona con la anterior
    assert len(tl.pause_records()) == 1
    assert tl.paused


def test_slices_drop_paused_audio() -> None:
    tl, _ = _tl(guard=0.0)
    tl.pause(s_to_ns(10))
    tl.resume(s_to_ns(20))
    tl.pause(s_to_ns(30))
    tl.resume(s_to_ns(40))
    s = s_to_ns
    assert tl.slices(s(5), s(35)) == [(s(5), s(10), 0), (s(20), s(30), 1)]
    assert tl.slices(s(12), s(15)) == []
    assert tl.slices(s(45), s(50)) == [(s(45), s(50), 2)]
    assert tl.slices(s(8), s(22)) == [(s(8), s(10), 0), (s(20), s(22), 1)]


def test_open_pause_swallows_everything_after_it() -> None:
    tl, _ = _tl()
    tl.pause(s_to_ns(10))
    assert tl.slices(s_to_ns(5), s_to_ns(15)) == [(s_to_ns(5), s_to_ns(10), 0)]
    assert tl.slices(s_to_ns(12), s_to_ns(15)) == []
    assert tl.segment_at(s_to_ns(12)) is None and tl.segment_at(s_to_ns(5)) == 0


def test_close_ends_an_open_pause_without_guard() -> None:
    tl, _ = _tl()
    tl.pause(s_to_ns(10))
    rec = tl.close(s_to_ns(14))
    assert rec == (pytest.approx(10.0), pytest.approx(4.0))
    assert tl.pause_records() == [(pytest.approx(10.0), pytest.approx(4.0))]
