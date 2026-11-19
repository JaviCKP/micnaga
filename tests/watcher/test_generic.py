"""Ruta generica (cualquier app con el micro) y su union con la ruta rapida de Meet."""

from __future__ import annotations

from micnaga.watcher.machine import (
    BeginDiscard,
    CancelDiscard,
    CloseEndPopup,
    ClosePopup,
    ConfirmRecording,
    EndAnswer,
    HidePill,
    HoldRecording,
    MarkEnd,
    Phase,
    PopupAnswer,
    PrivatePause,
    RecordingFailed,
    RecordNow,
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
from micnaga.watcher.model import MeetingInfo, MicApp, SignalSnapshot
from tests.watcher.helpers import CODE_A, MONITOR, OFF, Sim, names, tab

TEAMS = MicApp("app:msteams_8wekyb3d8bbwe", "Teams", package="MSTeams_8wekyb3d8bbwe")
ZOOM = MicApp("app:zoom.exe", "Zoom", exe="zoom.exe")
CHROME_APP = MicApp("app:chrome.exe", "navegador", exe="chrome.exe", is_browser=True)


def asnap(*apps: MicApp, tabs: tuple[object, ...] = ()) -> SignalSnapshot:
    browsers = frozenset(a.exe for a in apps if a.is_browser and a.exe)
    return SignalSnapshot(
        mic_browsers=browsers,
        meet_tabs=tabs,  # type: ignore[arg-type]
        mic_apps=frozenset(apps),
    )


IN_TEAMS = asnap(TEAMS)
IN_CHROME = asnap(CHROME_APP)
TEAMS_MEETING = MeetingInfo(title="Teams", generic_app=TEAMS.key)


def to_recording(sim: Sim, snap: SignalSnapshot = IN_TEAMS) -> None:
    for _ in range(60):
        acts = sim.tick(snap)
        if any(isinstance(a, StartRecording) for a in acts):
            return
    raise AssertionError("generic recording never started")


def to_popup(sim: Sim, snap: SignalSnapshot = IN_TEAMS) -> None:
    to_recording(sim, snap)
    for _ in range(60):
        if any(isinstance(a, ShowPopup) for a in sim.tick(snap)):
            return
    raise AssertionError("popup never appeared")


# ----------------------------------------------------------------------------- tiempos


def test_short_mic_use_does_nothing() -> None:
    sim = Sim()
    assert sim.tick(IN_TEAMS, 10) == []
    assert sim.m.state is WatcherState.CANDIDATE
    assert sim.tick(OFF, 120) == []
    assert sim.m.state is WatcherState.IDLE


def test_mic_interruption_resets_the_continuous_use_counter() -> None:
    sim = Sim()
    for _ in range(5):
        assert sim.tick(IN_TEAMS, 15) == []  # 15 s < mic_grace_s
        assert sim.tick(OFF, 1) == []
    assert sim.m.state is WatcherState.IDLE


def test_provisional_recording_starts_at_20s_without_popup() -> None:
    sim = Sim()
    assert sim.tick(IN_TEAMS, 20) == []  # t = 1..20 s: aun no (el primero cuenta como 0)
    acts = sim.tick(IN_TEAMS)  # t = 21 s = 20 s despues del primer tick
    assert acts == [StartRecording(provisional=True, meeting=TEAMS_MEETING), ShowPill(True, False)]
    assert sim.m.state is WatcherState.RECORDING_PRE_POPUP
    assert sim.tick(IN_TEAMS, 19) == []  # sin popup todavia


def test_popup_appears_20s_after_the_recording_started() -> None:
    sim = Sim()
    to_recording(sim)
    assert sim.tick(IN_TEAMS, 19) == []
    acts = sim.tick(IN_TEAMS)
    assert acts == [ShowPopup("Teams", None, None, 15.0)]
    assert sim.m.state is WatcherState.PROMPTING
    assert sim.m.popup_remaining_s == 15.0


def test_timings_are_configurable() -> None:
    sim = Sim(mic_grace_s=3.0, popup_after_recording_s=2.0)
    assert sim.tick(IN_TEAMS, 3) == []
    assert names(sim.tick(IN_TEAMS)) == ["StartRecording", "ShowPill"]
    assert sim.tick(IN_TEAMS, 1) == []
    assert names(sim.tick(IN_TEAMS)) == ["ShowPopup"]


# ----------------------------------------------------------------------------- respuestas


def test_grabar_confirms() -> None:
    sim = Sim()
    to_popup(sim)
    acts = sim.event(PopupAnswer(True))
    assert acts == [ConfirmRecording(), ClosePopup("confirmed"), ShowPill(False, False)]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED


def test_ahora_no_offers_undo_hides_the_pill_and_ignores_the_app_session() -> None:
    sim = Sim()
    to_popup(sim)
    acts = sim.event(PopupAnswer(False))
    assert acts == [ClosePopup("declined"), HidePill(), BeginDiscard(8.0)]
    assert sim.m.state is WatcherState.DISCARD_PENDING
    assert sim.m.ignored_keys == {TEAMS.key}
    assert sim.tick(IN_TEAMS, 8) == [HoldRecording("declined", trim=False)]
    assert sim.m.state is WatcherState.IGNORED
    assert sim.tick(IN_TEAMS, 300) == []  # el mismo uso del micro no vuelve a disparar
    assert sim.tick(OFF, 10) == []  # un corte de 10 s no es soltar el micro
    assert sim.tick(IN_TEAMS, 300) == []


def test_undo_of_ahora_no_confirms_and_shows_the_pill_again() -> None:
    sim = Sim()
    to_popup(sim)
    sim.event(PopupAnswer(False))
    sim.tick(IN_TEAMS, 5)
    acts = sim.event(Undo())
    assert acts == [CancelDiscard(), ConfirmRecording(), ShowPill(False, False)]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED
    assert sim.m.ignored_keys == frozenset()
    assert sim.tick(IN_TEAMS, 60) == []


def test_new_mic_session_of_the_same_app_after_release_can_trigger_again() -> None:
    sim = Sim()
    to_popup(sim)
    sim.event(PopupAnswer(False))
    sim.tick(IN_TEAMS, 30)
    sim.tick(OFF, 16)  # suelta el micro 15 s: la sesion ignorada se olvida
    assert sim.m.ignored_keys == frozenset()
    acts = sim.tick(IN_TEAMS, 21)
    assert "StartRecording" in names(acts)


def test_other_apps_are_not_ignored_by_ahora_no() -> None:
    sim = Sim()
    to_popup(sim)
    sim.event(PopupAnswer(False))
    assert sim.tick(IN_TEAMS, 8) == [HoldRecording("declined")]
    both = asnap(TEAMS, ZOOM)
    assert "StartRecording" in names(sim.tick(both, 21))


def test_popup_timeout_keeps_the_recording_provisional_without_a_second_pill() -> None:
    sim = Sim()
    to_popup(sim)
    acts = sim.tick(IN_TEAMS, 15)
    assert acts == [ClosePopup("timeout")]  # la pastilla provisional ya estaba
    assert sim.m.state is WatcherState.RECORDING_PROVISIONAL


def test_on_timeout_discard_holds_hides_pill_and_ignores() -> None:
    sim = Sim(on_timeout="discard")
    to_popup(sim)
    acts = sim.tick(IN_TEAMS, 15)
    assert acts == [ClosePopup("timeout"), HoldRecording("unanswered"), HidePill()]
    assert sim.tick(IN_TEAMS, 120) == []


# ----------------------------------------------------------------------------- fin


def test_release_during_the_prepopup_phase_is_held_silently() -> None:
    """Nunca se pregunto: sin popup de fin, se aparta recortada (recuperable)."""
    sim = Sim()
    to_recording(sim)
    sim.tick(IN_TEAMS, 5)
    assert sim.tick(OFF, 2) == []
    assert sim.m.state is WatcherState.ENDING
    acts = sim.tick(OFF, 1)
    assert acts == [MarkEnd(), HoldRecording("unanswered", trim=True), HidePill()]
    assert sim.m.phase is Phase.IDLE and sim.m.session is None
    assert not any(isinstance(a, (ShowEndPopup, ShowPopup, StopAndFinalize)) for a in sim.log)


def test_prepopup_blip_shorter_than_end_confirm_s_keeps_recording() -> None:
    sim = Sim()
    to_recording(sim)
    assert sim.tick(OFF, 2) == []
    assert sim.tick(IN_TEAMS) == []
    assert sim.m.state is WatcherState.RECORDING_PRE_POPUP


def test_generic_session_ending_while_the_popup_is_up_asks_with_the_end_popup() -> None:
    sim = Sim()
    to_popup(sim)
    sim.tick(IN_TEAMS, 3)
    assert sim.tick(OFF, 2) == []  # la cuenta atras del popup se congela con la senal caida
    assert sim.m.popup_remaining_s == 11.0  # 3 s + el tick que detecto la caida; luego quieta
    acts = sim.tick(OFF, 1)
    assert acts == [
        ClosePopup("meeting_ended"),
        MarkEnd(),
        ShowEndPopup(False, "Teams", None, 10.0),
    ]
    acts = sim.tick(OFF, 10)  # sin respuesta: se aparta (nunca se dijo «Si»)
    assert acts == [CloseEndPopup("timeout"), HoldRecording("unanswered", trim=True), HidePill()]
    assert not any(isinstance(a, StopAndFinalize) for a in sim.log)


def test_unconfirmed_generic_end_popup_save_finalizes_confirmed() -> None:
    sim = Sim()
    to_popup(sim)
    sim.tick(OFF, 3)
    acts = sim.event(EndAnswer("save"))
    assert acts == [
        CloseEndPopup("answered"),
        ConfirmRecording(),
        StopAndFinalize(True, TEAMS_MEETING, trim=True),
        HidePill(),
    ]


def test_confirmed_generic_session_asks_and_finishes_confirmed() -> None:
    sim = Sim()
    to_popup(sim)
    sim.event(PopupAnswer(True))
    assert sim.tick(OFF, 3) == [MarkEnd(), ShowEndPopup(True, "Teams", None, 10.0)]
    acts = sim.event(EndAnswer("finish"))
    assert acts == [
        CloseEndPopup("answered"),
        StopAndFinalize(True, TEAMS_MEETING, trim=True),
        HidePill(),
    ]


def test_generic_mic_back_retracts_the_end_popup() -> None:
    sim = Sim()
    to_popup(sim)
    sim.event(PopupAnswer(True))
    sim.tick(OFF, 3)
    assert names(sim.tick(IN_TEAMS)) == ["CloseEndPopup", "ClearEnd"]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED


def test_other_app_does_not_keep_the_session_alive() -> None:
    sim = Sim()
    to_recording(sim)
    acts = sim.tick(asnap(ZOOM), 3)
    assert "HoldRecording" in names(acts)


# ----------------------------------------------------------------------------- tray / usuario


def test_record_now_during_prepopup_confirms() -> None:
    sim = Sim()
    to_recording(sim)
    assert sim.event(RecordNow()) == [ConfirmRecording(), ShowPill(False, False)]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED
    assert sim.tick(IN_TEAMS, 60) == []  # ya no habra popup


def test_user_stop_during_prepopup_asks_to_save_and_is_not_retriggered() -> None:
    sim = Sim()
    to_recording(sim)
    acts = sim.event(UserStop())
    assert acts == [MarkEnd(), ShowEndPopup(False, "Teams", None, 10.0)]
    assert sim.m.state is WatcherState.END_PROMPT
    acts = sim.event(EndAnswer("discard"))
    assert acts == [CloseEndPopup("answered"), HidePill(), BeginDiscard(8.0)]
    assert sim.tick(IN_TEAMS, 8) == [HoldRecording("declined", trim=True)]
    assert sim.tick(IN_TEAMS, 120) == []


def test_recording_failed_in_prepopup_hides_the_pill_and_ignores_the_app() -> None:
    sim = Sim()
    to_recording(sim)
    assert sim.event(RecordingFailed()) == [HidePill()]
    assert sim.m.phase is Phase.IDLE and sim.m.session is None
    assert sim.m.state is WatcherState.IGNORED
    assert sim.tick(IN_TEAMS, 120) == []  # no vuelve a intentarlo solo
    acts = sim.event(RecordNow())  # pero «Grabar ahora» si, al momento
    assert acts == [
        StartRecording(provisional=False, meeting=MeetingInfo(manual=True)),
        ShowPill(False, False),
    ]


def test_recording_failed_while_the_generic_popup_is_up() -> None:
    sim = Sim()
    to_popup(sim)
    assert sim.event(RecordingFailed()) == [ClosePopup("meeting_ended"), HidePill()]
    assert sim.m.phase is Phase.IDLE
    assert sim.tick(IN_TEAMS, 120) == []


def test_suspend_during_the_generic_popup_holds_and_hides_the_pill() -> None:
    sim = Sim()
    to_popup(sim)
    acts = sim.event(SuspendWatching())
    assert acts == [ClosePopup("suspended"), HoldRecording("suspended"), HidePill()]
    assert sim.m.state is WatcherState.SUSPENDED


def test_needs_fast_ticks_when_the_prepopup_signal_is_lost() -> None:
    sim = Sim()
    to_recording(sim)
    assert not sim.m.needs_fast_ticks
    sim.tick(OFF)
    assert sim.m.needs_fast_ticks


def test_private_pause_freezes_the_prepopup_timer() -> None:
    sim = Sim()
    to_recording(sim)
    sim.tick(IN_TEAMS, 10)
    sim.event(PrivatePause())
    assert sim.m.state is WatcherState.PAUSED
    assert sim.tick(IN_TEAMS, 60) == []
    sim.event(PrivatePause())
    assert sim.tick(IN_TEAMS, 9) == []
    assert names(sim.tick(IN_TEAMS)) == ["ShowPopup"]


def test_earliest_app_wins_and_a_second_app_does_not_start_another_session() -> None:
    sim = Sim()
    sim.tick(IN_TEAMS, 10)
    both = asnap(TEAMS, ZOOM)
    acts = sim.tick(both, 11)
    starts = [a for a in acts if isinstance(a, StartRecording)]
    assert len(starts) == 1 and starts[0].meeting.title == "Teams"
    assert sim.tick(both, 15) == []  # Zoom sigue sonando: no hay segunda sesion


# ----------------------------------------------------------------------------- Meet


def test_meet_fast_path_is_unchanged_even_with_generic_apps_enabled() -> None:
    sim = Sim()
    snap = asnap(CHROME_APP, tabs=(tab(),))
    assert sim.tick(snap, 5) == []
    acts = sim.tick(snap)
    assert names(acts) == ["StartRecording", "ShowPopup"]  # sin pastilla antes del popup
    start, popup = acts
    assert isinstance(start, StartRecording) and start.meeting.code == CODE_A
    assert start.meeting.generic_app is None
    assert isinstance(popup, ShowPopup) and popup.monitor_hint == MONITOR
    # y la ruta generica no abre una segunda sesion 20 s despues
    assert sim.tick(snap, 40) == [ClosePopup("timeout"), ShowPill(True, False)]


def test_generic_session_is_upgraded_when_the_meet_tab_appears_before_the_popup() -> None:
    sim = Sim()
    to_recording(sim, IN_CHROME)  # chrome con el micro 20 s, sin pestana de Meet
    sim.tick(IN_CHROME, 9)
    with_tab = asnap(CHROME_APP, tabs=(tab(),))
    acts = sim.tick(with_tab)
    assert [type(a) for a in acts] == [ShowPopup]  # popup AHORA, sin reiniciar la grabacion
    popup = acts[0]
    assert isinstance(popup, ShowPopup) and popup.code == CODE_A and popup.monitor_hint == MONITOR
    assert sim.m.session is not None and sim.m.session.generic_app is None
    assert sim.m.session.code == CODE_A
    assert sum(isinstance(a, StartRecording) for a in sim.log) == 1
    # la sesion ahora vive de la pestana de Meet: sin ella (aunque chrome siga con el micro) acaba
    assert names(sim.tick(IN_CHROME, 3)) == ["ClosePopup", "MarkEnd", "ShowEndPopup"]
    final = sim.event(EndAnswer("save"))[2]
    assert isinstance(final, StopAndFinalize) and final.meeting.code == CODE_A


def test_upgrade_after_the_popup_was_shown_adopts_the_code_silently() -> None:
    sim = Sim()
    to_popup(sim, IN_CHROME)
    acts = sim.tick(asnap(CHROME_APP, tabs=(tab(),)))
    assert acts == []  # no se vuelve a mostrar
    sim.event(PopupAnswer(True))
    sim.tick(OFF, 3)
    final = sim.event(EndAnswer("finish"))[1]
    assert isinstance(final, StopAndFinalize) and final.meeting.code == CODE_A


def test_meet_tab_during_the_grace_period_uses_the_fast_path_once() -> None:
    sim = Sim()
    sim.tick(IN_CHROME, 10)
    snap = asnap(CHROME_APP, tabs=(tab(),))
    acts = sim.tick(snap, 6)
    assert names(acts) == ["StartRecording", "ShowPopup"]
    assert sim.tick(snap, 30) == [ClosePopup("timeout"), ShowPill(True, False)]


def test_ahora_no_on_an_upgraded_session_ignores_both_paths() -> None:
    sim = Sim()
    to_recording(sim, IN_CHROME)
    snap = asnap(CHROME_APP, tabs=(tab(),))
    sim.tick(snap)  # mejora a Meet y abre el popup
    acts = sim.event(PopupAnswer(False))
    assert acts == [ClosePopup("declined"), HidePill(), BeginDiscard(8.0)]
    assert sim.m.ignored_keys == {CODE_A, "app:chrome.exe"}
    assert sim.tick(snap, 120) == [HoldRecording("declined")]


def test_meet_path_ending_before_answering_asks_with_the_end_popup() -> None:
    sim = Sim()
    sim.to_popup(asnap(CHROME_APP, tabs=(tab(),)))
    acts = sim.tick(OFF, 3)
    assert acts == [
        ClosePopup("meeting_ended"),
        MarkEnd(),
        ShowEndPopup(False, None, MONITOR, 10.0),
    ]


# ----------------------------------------------------------------------------- config


def test_generic_apps_false_only_meet_path_is_active() -> None:
    sim = Sim(generic_apps=False)
    assert sim.tick(IN_TEAMS, 600) == []
    assert sim.tick(IN_CHROME, 600) == []
    assert sim.m.state is WatcherState.IDLE
    snap = asnap(CHROME_APP, tabs=(tab(),))
    assert names(sim.tick(snap, 6)) == ["StartRecording", "ShowPopup"]


def test_suspend_clears_the_generic_counters() -> None:
    sim = Sim()
    sim.tick(IN_TEAMS, 15)
    sim.event(SuspendWatching(100))
    assert sim.m.state is WatcherState.SUSPENDED
    assert sim.tick(IN_TEAMS, 100) == []
    assert sim.m.phase in (Phase.IDLE, Phase.CANDIDATE)
    assert sim.tick(IN_TEAMS, 19) == []  # la cuenta de 20 s empezo de cero al reanudar
