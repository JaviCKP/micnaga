"""Maquina de estados (spec §5-§6) con reloj falso: una prueba por fila de la tabla de §6."""

from __future__ import annotations

from micnaga.watcher.machine import (
    ClosePopup,
    ConfirmRecording,
    DiscardRecording,
    HidePill,
    PauseRecording,
    Phase,
    PopupAnswer,
    PopupHover,
    PrivatePause,
    RecordNow,
    ResumeRecording,
    ResumeWatching,
    ShowPill,
    ShowPopup,
    StartRecording,
    StopAndFinalize,
    SuspendWatching,
    UserStop,
    WatcherState,
)
from micnaga.watcher.model import MeetingInfo
from tests.watcher.helpers import (
    CODE_A,
    CODE_B,
    EDGE,
    MEET_ONLY,
    MIC_ONLY,
    MONITOR,
    OFF,
    ON,
    Sim,
    names,
    snap,
    tab,
)


def popup_session(sim: Sim) -> None:
    sim.to_popup()
    assert sim.m.state is WatcherState.PROMPTING


# ----------------------------------------------------------------------------- deteccion


def test_meet_open_with_mic_unused_never_prompts() -> None:
    """MIC-01: cero avisos con Meet abierto y el micro sin usar (durante «1 h»)."""
    sim = Sim()
    assert sim.tick(MEET_ONLY, 3600) == []
    assert sim.m.state is WatcherState.IDLE


def test_mic_in_use_without_meet_tab_never_prompts() -> None:
    sim = Sim()
    assert sim.tick(MIC_ONLY, 600) == []
    assert sim.m.state is WatcherState.IDLE


def test_mic_used_by_a_different_browser_than_the_meet_tab_does_not_count() -> None:
    sim = Sim()
    assert sim.tick(snap(tab(browser=EDGE), mic=("chrome.exe",)), 60) == []


def test_popup_appears_after_confirm_after_s_with_recording_started_first() -> None:
    sim = Sim()
    for _ in range(5):  # t = 0..4 s
        assert sim.tick(ON) == []
    assert sim.m.state is WatcherState.CANDIDATE
    acts = sim.tick(ON)  # t = 5 s
    assert names(acts) == ["StartRecording", "ShowPopup"]
    start, popup = acts
    assert isinstance(start, StartRecording) and start.provisional is True
    assert start.meeting.code == CODE_A
    assert isinstance(popup, ShowPopup)
    assert popup.code == CODE_A and popup.monitor_hint == MONITOR and popup.timeout_s == 15.0
    assert sim.m.state is WatcherState.PROMPTING


def test_confirm_after_s_is_configurable() -> None:
    sim = Sim(confirm_after_s=2.0)
    assert sim.tick(ON, 2) == []
    assert "ShowPopup" in names(sim.tick(ON))


def test_flapping_signals_do_not_trigger() -> None:
    sim = Sim()
    for _ in range(50):
        assert sim.tick(ON, 4) == []  # 4 s estables < 5 s
        assert sim.tick(OFF, 1) == []
    assert sim.m.state is WatcherState.IDLE


def test_mic_blip_in_candidate_resets_the_stability_timer() -> None:
    sim = Sim()
    sim.tick(ON, 4)
    sim.tick(MEET_ONLY)  # el micro se suelta 1 s
    assert sim.tick(ON, 5) == []
    assert "ShowPopup" in names(sim.tick(ON))


# ----------------------------------------------------------------------------- popup


def test_grabar_confirms_and_shows_pill() -> None:
    sim = Sim()
    popup_session(sim)
    acts = sim.event(PopupAnswer(True))
    assert acts == [ConfirmRecording(), ClosePopup("confirmed"), ShowPill(False, False)]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED


def test_ahora_no_discards_and_ignores_that_code_across_tab_switch() -> None:
    sim = Sim()
    popup_session(sim)
    acts = sim.event(PopupAnswer(False))
    assert acts == [DiscardRecording(), ClosePopup("declined")]
    assert sim.m.state is WatcherState.IGNORED
    # El usuario cambia de pestana: Meet deja de ser la activa pero sigue abierta y el micro sigue.
    background = snap(tab(active=False))
    assert sim.tick(background, 120) == []
    assert sim.tick(ON, 120) == []
    assert sim.m.state is WatcherState.IGNORED


def test_ahora_no_reprompts_for_a_different_code() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(False))
    both = snap(tab(CODE_A, active=False), tab(CODE_B, active=True))
    acts = sim.tick(both, 6)
    assert "ShowPopup" in names(acts)
    popup = next(a for a in acts if isinstance(a, ShowPopup))
    assert popup.code == CODE_B


def test_ahora_no_is_forgotten_only_after_hanging_up_15s() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(False))
    sim.tick(OFF, 10)  # un parpadeo de 10 s NO es colgar
    assert sim.m.ignored_keys == {CODE_A, "app:chrome.exe"}
    assert sim.tick(ON, 30) == []
    sim.tick(OFF, 16)  # colgar de verdad
    assert sim.m.ignored_keys == frozenset()
    assert sim.m.state is WatcherState.IDLE
    assert "ShowPopup" in names(sim.tick(ON, 6))  # misma sala otra vez: reunion nueva


def test_ahora_no_without_code_falls_back_to_a_per_browser_key() -> None:
    sim = Sim()
    titled = snap(tab(code=None, title="Revisión semanal"))
    sim.to_popup(titled)
    acts = sim.event(PopupAnswer(False))
    assert names(acts) == ["DiscardRecording", "ClosePopup"]
    assert sim.m.ignored_keys == {"nocode:chrome.exe", "app:chrome.exe"}
    assert sim.tick(titled, 120) == []


def test_codeless_meeting_stays_ignored_when_its_title_changes_or_vanishes() -> None:
    """El nombre de pestana lleva decoraciones: la identidad no puede depender del titulo."""
    sim = Sim()
    sim.to_popup(snap(tab(code=None, title="Revisión semanal")))
    sim.event(PopupAnswer(False))
    assert sim.tick(snap(tab(code=None, title="Revisión semanal - Zorglub activo")), 60) == []
    assert sim.tick(snap(tab(code=None, title=None, active=False)), 60) == []  # visto por UIA
    assert sim.tick(snap(tab(code=None, title="Otro nombre")), 60) == []
    assert sim.m.state is WatcherState.IGNORED


def test_meeting_ends_before_answering_discards_everything() -> None:
    sim = Sim()
    popup_session(sim)
    assert sim.tick(OFF, 14) == []  # 14 s sin senal: aun no
    assert sim.m.state is WatcherState.ENDING
    acts = sim.tick(OFF, 2)
    assert acts == [ClosePopup("meeting_ended"), DiscardRecording()]
    assert sim.m.state is WatcherState.IDLE
    assert not any(isinstance(a, StopAndFinalize) for a in sim.log)  # nada llega a las compuertas


def test_10s_blip_does_not_end_the_meeting() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(True))
    assert sim.tick(MIC_ONLY, 10) == []  # la pestana "desaparece" 10 s
    assert sim.m.state is WatcherState.ENDING
    assert sim.tick(ON, 1) == []
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED
    assert sim.tick(MEET_ONLY, 14) == []  # y la cuenta se reinicia si vuelve a caer
    assert sim.m.state is WatcherState.ENDING


def test_timeout_keeps_recording_as_provisional() -> None:
    sim = Sim()
    popup_session(sim)
    acts = sim.tick(ON, 15)
    assert acts == [ClosePopup("timeout"), ShowPill(provisional=True, paused=False)]
    assert sim.m.state is WatcherState.RECORDING_PROVISIONAL


def test_timeout_does_not_fire_before_15s() -> None:
    sim = Sim()
    popup_session(sim)
    assert sim.tick(ON, 14) == []
    assert sim.m.popup_remaining_s == 1.0


def test_provisional_session_finalizes_unconfirmed_when_the_meeting_ends() -> None:
    sim = Sim()
    popup_session(sim)
    sim.tick(ON, 15)
    acts = sim.tick(OFF, 16)
    assert [type(a) for a in acts] == [StopAndFinalize, HidePill]
    final = acts[0]
    assert isinstance(final, StopAndFinalize)
    assert final.confirmed is False and final.meeting.code == CODE_A
    assert sim.m.state is WatcherState.IDLE


def test_confirmed_session_finalizes_confirmed() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(True))
    acts = sim.tick(OFF, 16)
    assert [type(a) for a in acts] == [StopAndFinalize, HidePill]
    assert acts[0] == StopAndFinalize(confirmed=True, meeting=MeetingInfo.from_tab(tab()))


def test_on_timeout_discard_drops_the_recording_and_does_not_reprompt() -> None:
    sim = Sim(on_timeout="discard")
    popup_session(sim)
    acts = sim.tick(ON, 15)
    assert acts == [DiscardRecording(), ClosePopup("timeout")]
    assert sim.tick(ON, 120) == []  # sin esto volveria a preguntar cada 5 s
    assert sim.m.state is WatcherState.IGNORED


def test_hover_pauses_the_countdown() -> None:
    sim = Sim()
    popup_session(sim)
    sim.tick(ON, 10)  # 10 s consumidos
    sim.event(PopupHover(True))
    assert sim.tick(ON, 60) == []  # con el raton encima nunca vence
    assert sim.m.state is WatcherState.PROMPTING
    assert sim.m.popup_remaining_s == 5.0
    sim.event(PopupHover(False))
    assert sim.tick(ON, 4) == []
    assert sim.tick(ON, 1) == [ClosePopup("timeout"), ShowPill(True, False)]


def test_timer_only_ticks_advance_the_countdown() -> None:
    sim = Sim()
    popup_session(sim)
    acts: list[object] = []
    for _ in range(16):
        acts.extend(sim.tick(None))
    assert ClosePopup("timeout") in acts


def test_late_popup_events_are_ignored() -> None:
    sim = Sim()
    assert sim.event(PopupAnswer(True)) == []
    assert sim.event(PopupAnswer(False)) == []
    assert sim.event(PopupHover(True)) == []
    popup_session(sim)
    sim.event(PopupAnswer(True))
    assert sim.event(PopupAnswer(False)) == []  # el popup ya no existe


# ----------------------------------------------------------------------------- fin y sesion


def test_session_ends_after_15s_of_signal_loss_not_before() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(True))
    # El primer tick sin senal arranca la cuenta; el fin llega 15 s despues de ese tick.
    assert sim.tick(MIC_ONLY, 15) == []
    assert sim.m.state is WatcherState.ENDING
    acts = sim.tick(MIC_ONLY, 1)
    assert [type(a) for a in acts] == [StopAndFinalize, HidePill]


def test_mic_drop_also_ends_the_session() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(True))
    acts = sim.tick(MEET_ONLY, 16)
    assert [type(a) for a in acts] == [StopAndFinalize, HidePill]


def test_end_after_s_is_configurable() -> None:
    sim = Sim(end_after_s=3.0)
    popup_session(sim)
    sim.event(PopupAnswer(True))
    assert sim.tick(OFF, 3) == []
    assert [type(a) for a in sim.tick(OFF, 1)] == [StopAndFinalize, HidePill]


def test_sticky_session_identity_with_two_meet_tabs() -> None:
    """La sesion es la de su codigo: otra pestana de Meet no la mantiene viva."""
    sim = Sim()
    sim.to_popup(snap(tab(CODE_A)))
    sim.event(PopupAnswer(True))
    other = snap(tab(CODE_B))  # A se cierra; B sigue abierta con el micro en uso
    acts = sim.tick(other, 16)
    assert [type(a) for a in acts] == [StopAndFinalize, HidePill]
    assert "ShowPopup" in names(sim.tick(other, 8))  # y B es una reunion nueva


def test_session_title_and_late_code_are_refreshed() -> None:
    sim = Sim()
    sim.to_popup(snap(tab(code=None, title="Meet")))
    sim.event(PopupAnswer(True))
    sim.tick(snap(tab(code=None, title="Revisión semanal")), 2)
    sim.tick(snap(tab(code=CODE_A, title="Revisión semanal")), 2)
    acts = sim.tick(OFF, 16)
    final = next(a for a in acts if isinstance(a, StopAndFinalize))
    assert final.meeting.title == "Revisión semanal" and final.meeting.code == CODE_A


def test_second_meeting_after_the_first_ends_prompts_again() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(True))
    sim.tick(OFF, 16)
    assert "ShowPopup" in names(sim.tick(snap(tab(CODE_B)), 6))


# ----------------------------------------------------------------------------- manual / tray


def test_record_now_without_a_meeting_is_a_confirmed_manual_session() -> None:
    sim = Sim()
    acts = sim.event(RecordNow())
    assert acts == [
        StartRecording(provisional=False, meeting=MeetingInfo(manual=True)),
        ShowPill(False, False),
    ]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED


def test_manual_session_is_not_ended_by_signals_nor_prompted_by_meet() -> None:
    sim = Sim()
    sim.event(RecordNow())
    assert sim.tick(OFF, 600) == []  # sin senales: sigue grabando
    assert sim.tick(ON, 600) == []  # Meet aparece: no hay popup
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED


def test_user_stop_ends_a_manual_session() -> None:
    sim = Sim()
    sim.event(RecordNow())
    acts = sim.event(UserStop())
    assert acts == [StopAndFinalize(True, MeetingInfo(manual=True)), HidePill()]
    assert sim.m.state is WatcherState.IDLE


def test_record_now_while_prompting_confirms() -> None:
    sim = Sim()
    popup_session(sim)
    acts = sim.event(RecordNow())
    assert acts == [ConfirmRecording(), ClosePopup("confirmed"), ShowPill(False, False)]


def test_record_now_on_provisional_confirms_it() -> None:
    sim = Sim()
    popup_session(sim)
    sim.tick(ON, 15)
    acts = sim.event(RecordNow())
    assert acts == [ConfirmRecording(), ShowPill(False, False)]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED
    assert sim.event(RecordNow()) == []


def test_record_now_in_candidate_adopts_the_meeting_and_ends_with_it() -> None:
    sim = Sim()
    sim.tick(ON, 2)
    acts = sim.event(RecordNow())
    start = acts[0]
    assert isinstance(start, StartRecording) and not start.meeting.manual
    assert start.meeting.code == CODE_A and start.provisional is False
    assert [type(a) for a in sim.tick(OFF, 16)] == [StopAndFinalize, HidePill]


def test_user_stop_on_provisional_is_unconfirmed_and_not_reprompted() -> None:
    sim = Sim()
    popup_session(sim)
    sim.tick(ON, 15)
    acts = sim.event(UserStop())
    assert acts[0] == StopAndFinalize(False, MeetingInfo.from_tab(tab()))
    assert (
        sim.tick(ON, 120) == []
    )  # el usuario paro: no vuelve a preguntar mientras siga la reunion


def test_user_stop_during_popup_is_a_decline() -> None:
    sim = Sim()
    popup_session(sim)
    assert sim.event(UserStop()) == [DiscardRecording(), ClosePopup("declined")]
    assert sim.tick(ON, 60) == []


def test_user_stop_when_not_recording_is_a_noop() -> None:
    sim = Sim()
    assert sim.event(UserStop()) == []


# ----------------------------------------------------------------------------- pausa privada


def test_private_pause_toggle_and_state() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(True))
    assert sim.event(PrivatePause()) == [PauseRecording()]
    assert sim.m.state is WatcherState.PAUSED
    assert sim.event(PrivatePause(True)) == []  # ya pausada
    assert sim.event(PrivatePause()) == [ResumeRecording()]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED


def test_private_pause_is_ignored_when_not_recording() -> None:
    sim = Sim()
    assert sim.event(PrivatePause()) == []
    assert sim.m.state is WatcherState.IDLE


def test_meeting_end_while_paused_still_finalizes() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(True))
    sim.event(PrivatePause())
    acts = sim.tick(OFF, 16)
    assert [type(a) for a in acts] == [StopAndFinalize, HidePill]
    assert sim.m.paused is False and sim.m.state is WatcherState.IDLE


def test_pause_during_popup_freezes_the_countdown() -> None:
    sim = Sim()
    popup_session(sim)
    sim.tick(ON, 5)
    sim.event(PrivatePause())
    assert sim.tick(ON, 60) == []
    sim.event(PrivatePause())
    assert sim.m.popup_remaining_s == 10.0


def test_accepting_the_popup_while_paused_keeps_the_pill_amber() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PrivatePause())
    acts = sim.event(PopupAnswer(True))
    assert acts[-1] == ShowPill(provisional=False, paused=True)


# ----------------------------------------------------------------------------- suspender


def test_suspend_one_hour_blocks_prompts_and_expiry_needs_fresh_5s() -> None:
    sim = Sim()
    sim.event(SuspendWatching())
    assert sim.m.state is WatcherState.SUSPENDED
    assert sim.tick(ON, 3599) == []
    assert sim.m.state is WatcherState.SUSPENDED
    assert sim.tick(ON, 1) == []  # t = 3600: expira, pasa a IDLE y empieza a contar
    assert sim.m.state is WatcherState.CANDIDATE
    assert sim.tick(ON, 4) == []
    assert "ShowPopup" in names(sim.tick(ON, 1))


def test_resume_watching_cancels_the_suspension() -> None:
    sim = Sim()
    sim.event(SuspendWatching())
    sim.event(ResumeWatching())
    assert sim.m.state is WatcherState.IDLE
    assert "ShowPopup" in names(sim.tick(ON, 6))


def test_suspend_while_recording_does_not_interrupt_and_applies_afterwards() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(True))
    assert sim.event(SuspendWatching()) == []
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED
    sim.tick(OFF, 16)
    assert sim.m.state is WatcherState.SUSPENDED
    assert sim.tick(snap(tab(CODE_B)), 60) == []


def test_suspend_during_popup_discards_it() -> None:
    sim = Sim()
    popup_session(sim)
    assert sim.event(SuspendWatching()) == [DiscardRecording(), ClosePopup("suspended")]
    assert sim.m.state is WatcherState.SUSPENDED


def test_manual_recording_while_suspended_returns_to_suspended_afterwards() -> None:
    sim = Sim()
    sim.event(SuspendWatching())
    sim.event(RecordNow())
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED
    sim.event(UserStop())
    assert sim.m.state is WatcherState.SUSPENDED


# ----------------------------------------------------------------------------- estados


def test_phase_and_state_exposure() -> None:
    sim = Sim()
    assert sim.m.phase is Phase.IDLE and sim.m.session is None
    popup_session(sim)
    assert sim.m.phase is Phase.PROMPTING and sim.m.session is not None
    assert sim.m.popup_remaining_s == 15.0
    sim.event(PopupAnswer(True))
    assert sim.m.popup_remaining_s is None
