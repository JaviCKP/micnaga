"""«Grabar reunión presencial» en la maquina de estados y en la fachada ``Watcher``."""

from __future__ import annotations

from micnaga.config import DetectionConfig
from micnaga.watcher import Watcher
from micnaga.watcher.machine import (
    HidePill,
    Phase,
    PopupAnswer,
    PrivatePause,
    RecordInPerson,
    RecordNow,
    ShowPill,
    StartRecording,
    StopAndFinalize,
    SuspendWatching,
    UserStop,
    WatcherState,
)
from micnaga.watcher.model import MeetingInfo, MicApp, SignalSnapshot
from tests.watcher.helpers import CODE_A, CODE_B, OFF, ON, FakeController, Sim, snap, tab

IN_PERSON = MeetingInfo(manual=True, in_person=True)
TEAMS = SignalSnapshot(mic_apps=frozenset({MicApp(key="app:teams.exe", label="Teams")}))


def in_person(sim: Sim) -> None:
    acts = sim.event(RecordInPerson())
    assert acts == [StartRecording(provisional=False, meeting=IN_PERSON), ShowPill(False, False)]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED and sim.m.confirmed


def test_starts_confirmed_without_a_popup() -> None:
    in_person(Sim())


def test_does_not_adopt_a_visible_meet_call() -> None:
    sim = Sim()
    sim.tick(ON, 2)  # Meet en CANDIDATE (aun sin popup)
    in_person(sim)
    assert sim.m.session == IN_PERSON  # ni codigo ni titulo de la pestana


def test_no_call_app_does_not_end_it_and_a_meet_starting_does_not_prompt() -> None:
    sim = Sim()
    in_person(sim)
    assert sim.tick(OFF, 3600) == []  # una hora sin llamada: sigue grabando
    assert sim.tick(ON, 120) == []  # empieza un Meet: ni popup ni fin
    assert sim.tick(TEAMS, 120) == []  # ni una app generica
    assert sim.tick(snap(tab(CODE_B)), 30) == []
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED
    assert sim.m.needs_fast_ticks is False


def test_only_the_user_stops_it_and_it_is_finalized_untrimmed() -> None:
    sim = Sim()
    in_person(sim)
    sim.tick(ON, 10)
    acts = sim.event(UserStop())
    assert acts == [StopAndFinalize(True, IN_PERSON, trim=False), HidePill()]
    assert sim.m.phase is Phase.IDLE
    assert CODE_A in sim.m.ignored_keys  # el Meet que seguia vivo no pregunta al momento


def test_is_a_no_op_while_recording() -> None:
    sim = Sim()
    in_person(sim)
    assert sim.event(RecordInPerson()) == []
    sim2 = Sim()
    sim2.to_popup()
    assert sim2.event(RecordInPerson()) == []  # el popup sigue esperando respuesta
    sim2.event(PopupAnswer(True))
    assert sim2.event(RecordInPerson()) == []
    assert sim2.m.session is not None and not sim2.m.session.in_person


def test_record_now_during_in_person_changes_nothing() -> None:
    sim = Sim()
    in_person(sim)
    assert sim.event(RecordNow()) == []
    assert sim.m.session == IN_PERSON


def test_private_pause_works_as_in_any_recording() -> None:
    sim = Sim()
    in_person(sim)
    assert [type(a).__name__ for a in sim.event(PrivatePause(True))] == ["PauseRecording"]
    assert sim.m.state is WatcherState.PAUSED
    assert sim.tick(OFF, 600) == []


def test_while_suspended_it_records_and_returns_to_suspended() -> None:
    sim = Sim()
    sim.event(SuspendWatching())
    in_person(sim)
    sim.event(UserStop())
    assert sim.m.state is WatcherState.SUSPENDED


def test_watcher_facade_reports_whether_it_started() -> None:
    ctl = FakeController()
    w = Watcher(DetectionConfig(), ctl, sampler=None, init_com=False, clock=lambda: 1000.0)
    assert w.record_in_person() is True
    assert ctl.names() == ["start_recording", "show_pill"]
    assert ctl.calls[0][1] == {"provisional": False, "meeting": IN_PERSON}
    assert w.record_in_person() is False  # ya grabando: nada
    assert ctl.names() == ["start_recording", "show_pill"]
    assert w.diagnostics()["session"]["in_person"] is True
    w.user_stop()
    assert ctl.names()[-2:] == ["stop_and_finalize", "hide_pill"]
