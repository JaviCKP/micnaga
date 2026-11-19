"""Maquina de estados (spec §5-§6, DECISIONS §18) con reloj falso: una prueba por fila de §6."""

from __future__ import annotations

from collections.abc import Callable
from typing import get_args

import pytest

from micnaga.watcher import machine
from micnaga.watcher.machine import (
    Action,
    BeginDiscard,
    CancelDiscard,
    ClearEnd,
    CloseEndPopup,
    ClosePopup,
    ConfirmRecording,
    EndAnswer,
    HidePill,
    HoldRecording,
    MarkEnd,
    PauseRecording,
    Phase,
    PopupAnswer,
    PopupHover,
    PrivatePause,
    RecordingFailed,
    RecordNow,
    ResumeRecording,
    ResumeWatching,
    ShowEndPopup,
    ShowPill,
    ShowPopup,
    StartRecording,
    StopAndFinalize,
    SuspendWatching,
    Undo,
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
    NON_DESTRUCTIVE_ACTIONS,
    OFF,
    ON,
    Sim,
    names,
    snap,
    tab,
)

MEETING_A = MeetingInfo.from_tab(tab())
END_ASK_A = ShowEndPopup(False, None, MONITOR, 10.0)  # «¿La guardo?»
END_DONE_A = ShowEndPopup(True, None, MONITOR, 10.0)  # «¿Ha terminado?»


def popup_session(sim: Sim) -> None:
    sim.to_popup()
    assert sim.m.state is WatcherState.PROMPTING


def confirmed_session(sim: Sim) -> None:
    popup_session(sim)
    sim.event(PopupAnswer(True))
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED


def provisional_session(sim: Sim) -> None:
    popup_session(sim)
    sim.tick(ON, 15)
    assert sim.m.state is WatcherState.RECORDING_PROVISIONAL


def end_popup(sim: Sim) -> list[Action]:
    """La senal cae ``end_confirm_s`` (2 s = tercer tick sin senal) y sale el popup de fin."""
    assert sim.tick(OFF, 2) == []
    assert sim.m.state is WatcherState.ENDING
    acts = sim.tick(OFF)
    assert sim.m.state is WatcherState.END_PROMPT
    return acts


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


def test_ahora_no_offers_undo_then_holds_and_ignores_that_code_across_tab_switch() -> None:
    sim = Sim()
    popup_session(sim)
    acts = sim.event(PopupAnswer(False))
    assert acts == [ClosePopup("declined"), BeginDiscard(8.0)]
    assert sim.m.state is WatcherState.DISCARD_PENDING
    assert sim.m.ignored_keys == {CODE_A, "app:chrome.exe"}  # ignorada ya, no al expirar
    # El usuario cambia de pestana: Meet deja de ser la activa pero sigue abierta y el micro sigue.
    background = snap(tab(active=False))
    assert sim.tick(background, 7) == []
    assert sim.tick(background) == [HoldRecording("declined", trim=False)]
    assert sim.m.state is WatcherState.IGNORED
    assert sim.tick(background, 120) == []
    assert sim.tick(ON, 120) == []
    assert sim.m.state is WatcherState.IGNORED


def test_ahora_no_reprompts_for_a_different_code() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(False))
    assert sim.tick(ON, 8) == [HoldRecording("declined")]  # termina el «Deshacer»
    both = snap(tab(CODE_A, active=False), tab(CODE_B, active=True))
    acts = sim.tick(both, 6)
    assert "ShowPopup" in names(acts)
    popup = next(a for a in acts if isinstance(a, ShowPopup))
    assert popup.code == CODE_B


def test_ahora_no_is_forgotten_only_after_hanging_up_15s() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(False))
    assert sim.tick(OFF, 8) == [HoldRecording("declined")]
    sim.tick(OFF, 10)  # un parpadeo de 10 s NO es colgar
    assert sim.m.ignored_keys == {CODE_A, "app:chrome.exe"}
    assert sim.tick(ON, 30) == []
    sim.tick(OFF, 16)  # colgar de verdad
    assert sim.m.ignored_keys == frozenset()
    assert sim.m.state is WatcherState.IDLE
    assert "ShowPopup" in names(sim.tick(ON, 6))  # misma sala otra vez: reunion nueva


def test_end_after_s_only_ages_ignored_meetings() -> None:
    """``end_after_s`` ya no termina sesiones: solo es cuanto tarda en olvidarse una ignorada."""
    sim = Sim(end_after_s=3.0)
    popup_session(sim)
    sim.event(PopupAnswer(False))
    sim.tick(ON, 8)
    assert sim.tick(OFF, 3) == []
    assert sim.m.ignored_keys == {CODE_A, "app:chrome.exe"}
    sim.tick(OFF)
    assert sim.m.ignored_keys == frozenset()


def test_ahora_no_without_code_falls_back_to_a_per_browser_key() -> None:
    sim = Sim()
    titled = snap(tab(code=None, title="Revisión semanal"))
    sim.to_popup(titled)
    acts = sim.event(PopupAnswer(False))
    assert names(acts) == ["ClosePopup", "BeginDiscard"]
    assert sim.m.ignored_keys == {"nocode:chrome.exe", "app:chrome.exe"}
    assert sim.tick(titled, 120) == [HoldRecording("declined")]


def test_codeless_meeting_stays_ignored_when_its_title_changes_or_vanishes() -> None:
    """El nombre de pestana lleva decoraciones: la identidad no puede depender del titulo."""
    sim = Sim()
    sim.to_popup(snap(tab(code=None, title="Revisión semanal")))
    sim.event(PopupAnswer(False))
    assert sim.tick(snap(tab(code=None, title="Revisión semanal")), 8) == [
        HoldRecording("declined")
    ]
    assert sim.tick(snap(tab(code=None, title="Revisión semanal - Zorglub activo")), 60) == []
    assert sim.tick(snap(tab(code=None, title=None, active=False)), 60) == []  # visto por UIA
    assert sim.tick(snap(tab(code=None, title="Otro nombre")), 60) == []
    assert sim.m.state is WatcherState.IGNORED


def test_meeting_ends_before_answering_asks_with_the_end_popup() -> None:
    sim = Sim()
    popup_session(sim)
    assert sim.tick(OFF, 2) == []  # 2 ticks sin senal (1 s perdida): aun no
    assert sim.m.state is WatcherState.ENDING
    assert sim.m.popup_frozen  # la cuenta atras del popup se congela con la senal caida
    acts = sim.tick(OFF)
    assert acts == [ClosePopup("meeting_ended"), MarkEnd(), END_ASK_A]
    assert sim.m.state is WatcherState.END_PROMPT
    assert not any(isinstance(a, StopAndFinalize) for a in sim.log)  # sin «Si» no se transcribe


def test_short_blip_does_not_end_the_meeting() -> None:
    sim = Sim()
    confirmed_session(sim)
    assert sim.tick(MIC_ONLY, 2) == []  # la pestana "desaparece" 2 ticks
    assert sim.m.state is WatcherState.ENDING
    assert sim.tick(ON, 1) == []
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED
    assert sim.tick(MEET_ONLY, 2) == []  # y la cuenta se reinicia si vuelve a caer
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


def test_on_timeout_discard_holds_the_recording_and_does_not_reprompt() -> None:
    sim = Sim(on_timeout="discard")
    popup_session(sim)
    acts = sim.tick(ON, 15)
    assert acts == [ClosePopup("timeout"), HoldRecording("unanswered")]
    assert sim.m.phase is Phase.IDLE
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
    assert sim.event(EndAnswer("finish")) == []
    assert sim.event(Undo()) == []
    popup_session(sim)
    sim.event(PopupAnswer(True))
    assert sim.event(PopupAnswer(False)) == []  # el popup ya no existe
    assert sim.event(EndAnswer("discard")) == []  # ni hay popup de fin


# ----------------------------------------------------------------------------- «Deshacer»


def test_undo_window_expiry_holds_without_trim_and_returns_to_idle() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(False))
    assert sim.tick(ON, 7) == []  # sin deteccion durante el «Deshacer»
    assert sim.m.state is WatcherState.DISCARD_PENDING
    assert sim.tick(ON) == [HoldRecording("declined", trim=False)]
    assert sim.m.phase is Phase.IDLE and sim.m.session is None
    assert sim.event(Undo()) == []  # tarde: ya no hay nada que deshacer


def test_undo_window_expires_with_timer_only_ticks() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(False))
    assert sim.tick(None, 8) == [HoldRecording("declined")]


def test_undo_confirms_and_keeps_recording() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(False))
    sim.tick(ON, 3)
    acts = sim.event(Undo())
    assert acts == [CancelDiscard(), ConfirmRecording(), ShowPill(False, False)]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED and sim.m.confirmed
    assert sim.m.ignored_keys == frozenset()  # deja de ignorarse
    assert sim.tick(ON, 60) == []  # sigue grabando, no hay expiracion pendiente
    assert end_popup(sim) == [MarkEnd(), END_DONE_A]


def test_record_now_during_undo_is_an_undo() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(False))
    sim.tick(ON, 7)
    acts = sim.event(RecordNow())
    assert acts == [CancelDiscard(), ConfirmRecording(), ShowPill(False, False)]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED
    assert sim.tick(ON, 30) == []
    assert not any(isinstance(a, StartRecording) for a in acts)


def test_user_stop_and_pause_during_undo_are_ignored() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(False))
    assert sim.event(UserStop()) == []
    assert sim.event(PrivatePause()) == []
    assert sim.m.state is WatcherState.DISCARD_PENDING
    assert sim.tick(ON, 8) == [HoldRecording("declined")]


# ----------------------------------------------------------------------------- popup de fin


def test_end_popup_appears_after_end_confirm_s_of_signal_loss_not_before() -> None:
    sim = Sim()
    confirmed_session(sim)
    # El primer tick sin senal arranca la cuenta; el fin llega 2 s despues de ese tick.
    assert sim.tick(MIC_ONLY, 2) == []
    assert sim.m.state is WatcherState.ENDING
    assert sim.tick(MIC_ONLY, 1) == [MarkEnd(), END_DONE_A]
    assert sim.m.state is WatcherState.END_PROMPT
    assert sim.m.popup_remaining_s == 10.0 and sim.m.popup_total_s == 10.0


def test_mic_drop_also_ends_the_session() -> None:
    sim = Sim()
    confirmed_session(sim)
    assert names(sim.tick(MEET_ONLY, 3)) == ["MarkEnd", "ShowEndPopup"]


def test_end_confirm_s_is_configurable() -> None:
    sim = Sim(end_confirm_s=5.0)
    confirmed_session(sim)
    assert sim.tick(OFF, 5) == []
    assert names(sim.tick(OFF, 1)) == ["MarkEnd", "ShowEndPopup"]


def test_confirmed_end_popup_finish_finalizes_trimmed() -> None:
    sim = Sim()
    confirmed_session(sim)
    end_popup(sim)
    acts = sim.event(EndAnswer("finish"))
    assert acts == [
        CloseEndPopup("answered"),
        StopAndFinalize(confirmed=True, meeting=MEETING_A, trim=True),
        HidePill(),
    ]
    assert sim.m.phase is Phase.IDLE and sim.m.session is None


def test_confirmed_end_popup_keep_clears_the_end_and_only_the_user_stops_it() -> None:
    sim = Sim()
    confirmed_session(sim)
    end_popup(sim)
    acts = sim.event(EndAnswer("keep"))
    assert acts == [CloseEndPopup("answered"), ClearEnd(), ShowPill(False, False)]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED
    assert sim.tick(OFF, 600) == []  # la senal ya no la termina
    assert sim.tick(ON, 60) == []
    assert sim.event(UserStop()) == [StopAndFinalize(True, MEETING_A, trim=False), HidePill()]


def test_confirmed_end_popup_timeout_finalizes_trimmed() -> None:
    sim = Sim()
    confirmed_session(sim)
    end_popup(sim)
    assert sim.tick(OFF, 9) == []
    acts = sim.tick(OFF)
    assert acts == [
        CloseEndPopup("timeout"),
        StopAndFinalize(confirmed=True, meeting=MEETING_A, trim=True),
        HidePill(),
    ]
    assert sim.m.phase is Phase.IDLE


def test_unconfirmed_end_popup_keep_confirms_and_keeps_recording() -> None:
    sim = Sim()
    provisional_session(sim)
    end_popup(sim)
    acts = sim.event(EndAnswer("keep"))
    assert acts == [
        CloseEndPopup("answered"),
        ClearEnd(),
        ConfirmRecording(),
        ShowPill(False, False),
    ]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED and sim.m.confirmed
    assert sim.tick(OFF, 120) == []


def test_end_popup_countdown_freezes_on_hover() -> None:
    sim = Sim()
    confirmed_session(sim)
    end_popup(sim)
    sim.tick(OFF, 4)
    sim.event(PopupHover(True))
    assert sim.m.popup_frozen
    assert sim.tick(OFF, 60) == []
    assert sim.m.popup_remaining_s == 6.0
    sim.event(PopupHover(False))
    assert sim.tick(OFF, 5) == []
    assert names(sim.tick(OFF)) == ["CloseEndPopup", "StopAndFinalize", "HidePill"]


def test_provisional_session_asks_to_save_when_the_meeting_ends() -> None:
    sim = Sim()
    provisional_session(sim)
    assert end_popup(sim) == [MarkEnd(), END_ASK_A]
    acts = sim.event(EndAnswer("save"))
    assert acts == [
        CloseEndPopup("answered"),
        ConfirmRecording(),
        StopAndFinalize(confirmed=True, meeting=MEETING_A, trim=True),
        HidePill(),
    ]
    assert sim.m.phase is Phase.IDLE


def test_unconfirmed_end_popup_discard_offers_undo_then_holds_trimmed() -> None:
    sim = Sim()
    provisional_session(sim)
    end_popup(sim)
    acts = sim.event(EndAnswer("discard"))
    assert acts == [CloseEndPopup("answered"), HidePill(), BeginDiscard(8.0)]
    assert sim.m.state is WatcherState.DISCARD_PENDING
    assert sim.tick(OFF, 7) == []
    assert sim.tick(OFF) == [HoldRecording("declined", trim=True)]
    assert sim.m.phase is Phase.IDLE


def test_undo_of_an_end_discard_saves_it() -> None:
    sim = Sim()
    provisional_session(sim)
    end_popup(sim)
    sim.event(EndAnswer("discard"))
    sim.tick(OFF, 2)
    acts = sim.event(Undo())
    assert acts == [
        CancelDiscard(),
        ConfirmRecording(),
        StopAndFinalize(confirmed=True, meeting=MEETING_A, trim=True),
    ]
    assert sim.m.phase is Phase.IDLE
    assert sim.tick(OFF, 20) == []  # no queda ningun «Deshacer» pendiente


def test_unconfirmed_end_popup_timeout_holds_trimmed() -> None:
    sim = Sim()
    provisional_session(sim)
    end_popup(sim)
    assert sim.tick(OFF, 9) == []
    acts = sim.tick(OFF)
    assert acts == [CloseEndPopup("timeout"), HoldRecording("unanswered", trim=True), HidePill()]
    assert sim.m.phase is Phase.IDLE


def test_meeting_ended_during_popup_then_end_popup_timeout_is_held() -> None:
    sim = Sim()
    popup_session(sim)
    sim.tick(OFF, 3)  # popup retirado, «¿La guardo?»
    acts = sim.tick(OFF, 10)
    assert acts == [CloseEndPopup("timeout"), HoldRecording("unanswered", trim=True)]


def test_signal_back_retracts_the_end_popup_and_keeps_confirmed_recording() -> None:
    sim = Sim()
    confirmed_session(sim)
    end_popup(sim)
    sim.tick(OFF, 4)
    assert sim.tick(ON) == [CloseEndPopup("resumed"), ClearEnd()]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED
    assert sim.tick(ON, 60) == []
    assert end_popup(sim) == [MarkEnd(), END_DONE_A]  # y puede volver a terminar


def test_signal_back_retracts_the_end_popup_and_keeps_provisional_recording() -> None:
    sim = Sim()
    provisional_session(sim)
    end_popup(sim)
    assert sim.tick(ON) == [CloseEndPopup("resumed"), ClearEnd()]
    assert sim.m.state is WatcherState.RECORDING_PROVISIONAL
    assert sim.tick(ON, 60) == []


def test_signal_back_while_the_ask_popup_was_retracted_asks_again() -> None:
    sim = Sim()
    popup_session(sim)
    sim.tick(ON, 6)  # el popup ya llevaba 6 s
    sim.tick(OFF, 3)  # la reunion "acaba": se retira el popup, sale el de fin
    acts = sim.tick(ON)
    assert acts == [
        CloseEndPopup("resumed"),
        ClearEnd(),
        ShowPopup(None, CODE_A, MONITOR, 15.0),
    ]
    assert sim.m.state is WatcherState.PROMPTING
    assert sim.m.popup_remaining_s == 15.0  # cuenta atras completa otra vez
    assert sim.event(PopupAnswer(True))[0] == ConfirmRecording()


def test_record_now_in_the_end_popup_keeps_or_saves() -> None:
    confirmed = Sim()
    confirmed_session(confirmed)
    end_popup(confirmed)
    assert names(confirmed.event(RecordNow())) == ["CloseEndPopup", "ClearEnd", "ShowPill"]
    unconfirmed = Sim()
    provisional_session(unconfirmed)
    end_popup(unconfirmed)
    assert names(unconfirmed.event(RecordNow())) == [
        "CloseEndPopup",
        "ConfirmRecording",
        "StopAndFinalize",
        "HidePill",
    ]


# ----------------------------------------------------------------------------- fin y sesion


def test_sticky_session_identity_with_two_meet_tabs() -> None:
    """La sesion es la de su codigo: otra pestana de Meet no la mantiene viva."""
    sim = Sim()
    sim.to_popup(snap(tab(CODE_A)))
    sim.event(PopupAnswer(True))
    other = snap(tab(CODE_B))  # A se cierra; B sigue abierta con el micro en uso
    assert sim.tick(other, 3) == [MarkEnd(), END_DONE_A]
    assert sim.event(EndAnswer("finish"))[1] == StopAndFinalize(True, MEETING_A, trim=True)


def test_session_title_and_late_code_are_refreshed() -> None:
    sim = Sim()
    sim.to_popup(snap(tab(code=None, title="Meet")))
    sim.event(PopupAnswer(True))
    sim.tick(snap(tab(code=None, title="Revisión semanal")), 2)
    sim.tick(snap(tab(code=CODE_A, title="Revisión semanal")), 2)
    shown = end_popup(sim)[-1]
    assert isinstance(shown, ShowEndPopup) and shown.title == "Revisión semanal"
    acts = sim.event(EndAnswer("finish"))
    final = next(a for a in acts if isinstance(a, StopAndFinalize))
    assert final.meeting.title == "Revisión semanal" and final.meeting.code == CODE_A


def test_second_meeting_after_the_first_ends_prompts_again() -> None:
    sim = Sim()
    confirmed_session(sim)
    sim.tick(OFF, 16)  # popup de fin + 10 s sin respuesta: termina
    assert "StopAndFinalize" in names(sim.log)
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


def test_user_stop_ends_a_manual_session_untrimmed() -> None:
    sim = Sim()
    sim.event(RecordNow())
    acts = sim.event(UserStop())
    assert acts == [StopAndFinalize(True, MeetingInfo(manual=True), trim=False), HidePill()]
    assert sim.m.state is WatcherState.IDLE


def test_user_stop_on_a_confirmed_meeting_finalizes_and_ignores_live_signals() -> None:
    sim = Sim()
    both = snap(tab(CODE_A), tab(CODE_B, active=False))
    sim.to_popup(both)
    sim.event(PopupAnswer(True))
    acts = sim.event(UserStop())
    assert acts == [StopAndFinalize(True, MEETING_A, trim=False), HidePill()]
    assert {CODE_A, CODE_B} <= sim.m.ignored_keys  # el usuario paro: no se vuelve a preguntar
    assert sim.tick(both, 120) == []


def test_record_now_while_prompting_confirms() -> None:
    sim = Sim()
    popup_session(sim)
    acts = sim.event(RecordNow())
    assert acts == [ConfirmRecording(), ClosePopup("confirmed"), ShowPill(False, False)]


def test_record_now_on_provisional_confirms_it() -> None:
    sim = Sim()
    provisional_session(sim)
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
    assert end_popup(sim) == [MarkEnd(), END_DONE_A]


def test_user_stop_on_provisional_asks_to_save_and_is_not_reprompted() -> None:
    sim = Sim()
    provisional_session(sim)
    acts = sim.event(UserStop())
    assert acts == [MarkEnd(), END_ASK_A]
    assert sim.m.state is WatcherState.END_PROMPT
    # Lo abrio el usuario: que la senal siga viva no lo retira.
    assert sim.tick(ON, 9) == []
    assert sim.tick(ON) == [
        CloseEndPopup("timeout"),
        HoldRecording("unanswered", trim=True),
        HidePill(),
    ]
    assert sim.tick(ON, 120) == []  # el usuario paro: no vuelve a preguntar en esta reunion


def test_user_stop_on_provisional_then_save() -> None:
    sim = Sim()
    provisional_session(sim)
    sim.event(UserStop())
    acts = sim.event(EndAnswer("save"))
    assert acts == [
        CloseEndPopup("answered"),
        ConfirmRecording(),
        StopAndFinalize(True, MEETING_A, trim=True),
        HidePill(),
    ]


def test_user_stop_in_the_end_popup_finishes_or_saves() -> None:
    sim = Sim()
    provisional_session(sim)
    end_popup(sim)
    assert names(sim.event(UserStop())) == [
        "CloseEndPopup",
        "ConfirmRecording",
        "StopAndFinalize",
        "HidePill",
    ]


def test_user_stop_during_popup_is_a_decline() -> None:
    sim = Sim()
    popup_session(sim)
    assert sim.event(UserStop()) == [ClosePopup("declined"), BeginDiscard(8.0)]
    assert sim.tick(ON, 8) == [HoldRecording("declined")]
    assert sim.tick(ON, 60) == []


def test_user_stop_when_not_recording_is_a_noop() -> None:
    sim = Sim()
    assert sim.event(UserStop()) == []


# ----------------------------------------------------------------------------- captura fallida


def test_recording_failed_on_a_manual_session_returns_to_idle_and_can_retry() -> None:
    sim = Sim()
    sim.event(RecordNow())
    assert sim.event(RecordingFailed()) == [HidePill()]
    assert sim.m.state is WatcherState.IDLE and sim.m.session is None
    assert sim.m.ignored_keys == frozenset()
    assert names(sim.event(RecordNow())) == ["StartRecording", "ShowPill"]


def test_recording_failed_while_prompting_closes_the_popup_and_ignores_the_meeting() -> None:
    sim = Sim()
    popup_session(sim)
    assert sim.event(RecordingFailed()) == [ClosePopup("meeting_ended")]
    assert sim.m.phase is Phase.IDLE and sim.m.session is None
    assert sim.m.state is WatcherState.IGNORED
    assert sim.tick(ON, 60) == []  # un dispositivo roto no da un aviso cada 5 s
    acts = sim.event(RecordNow())  # pero «Grabar ahora» reintenta al momento
    assert acts == [StartRecording(provisional=False, meeting=MEETING_A), ShowPill(False, False)]
    assert CODE_A not in sim.m.ignored_keys


def test_recording_failed_when_idle_is_a_noop() -> None:
    sim = Sim()
    assert sim.event(RecordingFailed()) == []
    sim.tick(ON, 2)
    assert sim.event(RecordingFailed()) == []
    assert sim.m.state is WatcherState.CANDIDATE


# ----------------------------------------------------------------------------- pausa privada


def test_private_pause_toggle_and_state() -> None:
    sim = Sim()
    confirmed_session(sim)
    assert sim.event(PrivatePause()) == [PauseRecording()]
    assert sim.m.state is WatcherState.PAUSED
    assert sim.event(PrivatePause(True)) == []  # ya pausada
    assert sim.event(PrivatePause()) == [ResumeRecording()]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED


def test_private_pause_is_ignored_when_not_recording() -> None:
    sim = Sim()
    assert sim.event(PrivatePause()) == []
    assert sim.m.state is WatcherState.IDLE


def test_meeting_end_while_paused_still_asks_and_finalizes() -> None:
    sim = Sim()
    confirmed_session(sim)
    sim.event(PrivatePause())
    assert sim.tick(OFF, 2) == []
    assert sim.m.state is WatcherState.PAUSED  # la pausa se superpone a ENDING
    assert sim.tick(OFF) == [MarkEnd(), END_DONE_A]
    acts = sim.event(EndAnswer("finish"))
    assert names(acts) == ["CloseEndPopup", "StopAndFinalize", "HidePill"]
    assert sim.m.paused is False and sim.m.phase is Phase.IDLE


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
    confirmed_session(sim)
    assert sim.event(SuspendWatching()) == []
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED
    end_popup(sim)
    sim.event(EndAnswer("finish"))
    assert sim.m.state is WatcherState.SUSPENDED
    assert sim.tick(snap(tab(CODE_B)), 60) == []


def test_suspend_during_popup_holds_it() -> None:
    sim = Sim()
    popup_session(sim)
    assert sim.event(SuspendWatching()) == [ClosePopup("suspended"), HoldRecording("suspended")]
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
    assert sim.m.popup_remaining_s == 15.0 and sim.m.popup_total_s == 15.0
    sim.event(PopupAnswer(True))
    assert sim.m.popup_remaining_s is None and sim.m.popup_total_s is None
    end_popup(sim)
    assert sim.m.phase is Phase.END_PROMPT and sim.m.state is WatcherState.END_PROMPT


def test_needs_fast_ticks_only_while_a_short_timer_runs() -> None:
    sim = Sim()
    assert not sim.m.needs_fast_ticks  # IDLE
    sim.tick(ON, 2)
    assert not sim.m.needs_fast_ticks  # CANDIDATE
    sim.tick(ON, 4)
    assert sim.m.phase is Phase.PROMPTING and sim.m.needs_fast_ticks
    sim.event(PopupAnswer(True))
    assert not sim.m.needs_fast_ticks  # grabando con la senal viva
    sim.tick(ON, 30)
    assert not sim.m.needs_fast_ticks
    sim.tick(OFF)
    assert sim.m.state is WatcherState.ENDING and sim.m.needs_fast_ticks  # senal perdida
    sim.tick(ON)
    assert not sim.m.needs_fast_ticks
    end_popup(sim)
    assert sim.m.needs_fast_ticks  # END_PROMPT
    sim.event(EndAnswer("keep"))
    assert not sim.m.needs_fast_ticks  # «Seguir grabando»: sin temporizador
    sim.tick(OFF, 30)
    assert not sim.m.needs_fast_ticks  # la senal ya no cuenta


def test_needs_fast_ticks_during_the_undo_window() -> None:
    sim = Sim()
    popup_session(sim)
    sim.event(PopupAnswer(False))
    assert sim.m.phase is Phase.DISCARD_PENDING and sim.m.needs_fast_ticks
    sim.tick(ON, 8)
    assert not sim.m.needs_fast_ticks


# ----------------------------------------------------------------------------- nada se borra


def test_no_action_deletes_anything() -> None:
    """DECISIONS §18: ya no existe una accion que borre; solo se aparta o se finaliza."""
    assert not hasattr(machine, "DiscardRecording")
    assert {t.__name__ for t in get_args(Action)} == NON_DESTRUCTIVE_ACTIONS


def _decline(sim: Sim) -> None:
    popup_session(sim)
    sim.event(PopupAnswer(False))
    sim.tick(ON, 30)


def _undo_then_end(sim: Sim) -> None:
    popup_session(sim)
    sim.event(PopupAnswer(False))
    sim.event(Undo())
    sim.tick(OFF, 20)


def _popup_timeout_discard(sim: Sim) -> None:
    popup_session(sim)  # con on_timeout="discard" (ver el parametro)
    sim.tick(ON, 30)


def _ended_while_prompting(sim: Sim) -> None:
    popup_session(sim)
    sim.tick(OFF, 30)


def _provisional_end_discard(sim: Sim) -> None:
    provisional_session(sim)
    end_popup(sim)
    sim.event(EndAnswer("discard"))
    sim.tick(OFF, 30)


def _provisional_end_discard_undone(sim: Sim) -> None:
    provisional_session(sim)
    end_popup(sim)
    sim.event(EndAnswer("discard"))
    sim.event(Undo())


def _confirmed_end_timeout(sim: Sim) -> None:
    confirmed_session(sim)
    sim.tick(OFF, 30)


def _user_stop_provisional_unanswered(sim: Sim) -> None:
    provisional_session(sim)
    sim.event(UserStop())
    sim.tick(ON, 30)


def _suspend_prompting(sim: Sim) -> None:
    popup_session(sim)
    sim.event(SuspendWatching())


def _manual_stop(sim: Sim) -> None:
    sim.event(RecordNow())
    sim.event(UserStop())


@pytest.mark.parametrize(
    ("flow", "on_timeout"),
    [
        (_decline, "provisional"),
        (_undo_then_end, "provisional"),
        (_popup_timeout_discard, "discard"),
        (_ended_while_prompting, "provisional"),
        (_provisional_end_discard, "provisional"),
        (_provisional_end_discard_undone, "provisional"),
        (_confirmed_end_timeout, "provisional"),
        (_user_stop_provisional_unanswered, "provisional"),
        (_suspend_prompting, "provisional"),
        (_manual_stop, "provisional"),
    ],
)
def test_every_session_ends_in_exactly_one_hold_or_finalize(
    flow: Callable[[Sim], None], on_timeout: str
) -> None:
    sim = Sim(on_timeout=on_timeout)
    flow(sim)
    starts = [a for a in sim.log if isinstance(a, StartRecording)]
    ends = [a for a in sim.log if isinstance(a, (HoldRecording, StopAndFinalize))]
    assert len(starts) == 1 and len(ends) == 1, names(sim.log)
    assert sim.m.session is None


# ------------------------------------------------------------- regresiones de la revision


def test_back_to_back_meeting_is_not_ignored_after_the_first_one_ends() -> None:
    """Al acabar una reunion por perdida de senal solo se ignora ESA reunion: otra que siga
    viva (reuniones seguidas) se pregunta como nueva."""
    sim = Sim()
    sim.to_popup(snap(tab(CODE_A)))
    sim.event(PopupAnswer(True))
    b_only = snap(tab(CODE_B))
    sim.tick(b_only, 3)
    assert sim.m.phase is Phase.END_PROMPT
    sim.event(EndAnswer("finish"))
    acts = sim.tick(b_only, 7)
    assert any(isinstance(a, ShowPopup) and a.code == CODE_B for a in acts), acts
    assert CODE_B not in " ".join(sim.m.ignored_keys)


def test_other_app_during_the_undo_window_gets_its_own_session() -> None:
    sim = Sim()
    sim.to_popup(snap(tab(CODE_A)))
    sim.event(PopupAnswer(False))
    both = snap(tab(CODE_A), tab(CODE_B))
    acts = sim.tick(both, 20)
    assert any(isinstance(a, ShowPopup) and a.code == CODE_B for a in acts), acts


def test_undo_resets_a_stale_signal_loss_timer() -> None:
    sim = Sim()
    sim.to_popup()
    sim.tick(OFF)  # la senal cae un instante con el popup abierto
    sim.event(PopupAnswer(False))
    sim.tick(ON, 3)  # vuelve durante «Deshacer»
    sim.event(Undo())
    acts = sim.event(machine.Tick(None))
    assert acts == [] and sim.m.phase is Phase.RECORDING_CONFIRMED
