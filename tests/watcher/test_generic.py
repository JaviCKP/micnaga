"""Ruta generica (cualquier app con el micro) y su union con la ruta rapida de Meet."""

from __future__ import annotations

from micnaga.watcher.machine import (
    ClosePopup,
    ConfirmRecording,
    DiscardRecording,
    HidePill,
    Phase,
    PopupAnswer,
    PrivatePause,
    RecordNow,
    ShowPill,
    ShowPopup,
    StartRecording,
    StopAndFinalize,
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
    meeting = MeetingInfo(title="Teams", generic_app=TEAMS.key)
    assert acts == [StartRecording(provisional=True, meeting=meeting), ShowPill(True, False)]
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


def test_ahora_no_discards_everything_hides_the_pill_and_ignores_the_app_session() -> None:
    sim = Sim()
    to_popup(sim)
    acts = sim.event(PopupAnswer(False))
    assert acts == [DiscardRecording(), ClosePopup("declined"), HidePill()]
    assert sim.m.state is WatcherState.IGNORED
    assert sim.tick(IN_TEAMS, 300) == []  # el mismo uso del micro no vuelve a disparar
    assert sim.tick(OFF, 10) == []  # un corte de 10 s no es soltar el micro
    assert sim.tick(IN_TEAMS, 300) == []


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
    both = asnap(TEAMS, ZOOM)
    assert "StartRecording" in names(sim.tick(both, 21))


def test_popup_timeout_keeps_the_recording_provisional_without_a_second_pill() -> None:
    sim = Sim()
    to_popup(sim)
    acts = sim.tick(IN_TEAMS, 15)
    assert acts == [ClosePopup("timeout")]  # la pastilla provisional ya estaba
    assert sim.m.state is WatcherState.RECORDING_PROVISIONAL


def test_on_timeout_discard_drops_hides_pill_and_ignores() -> None:
    sim = Sim(on_timeout="discard")
    to_popup(sim)
    acts = sim.tick(IN_TEAMS, 15)
    assert acts == [DiscardRecording(), ClosePopup("timeout"), HidePill()]
    assert sim.tick(IN_TEAMS, 120) == []


# ----------------------------------------------------------------------------- fin


def test_release_during_the_prepopup_phase_finalizes_provisional() -> None:
    sim = Sim()
    to_recording(sim)
    sim.tick(IN_TEAMS, 5)
    assert sim.tick(OFF, 15) == []
    assert sim.m.state is WatcherState.ENDING
    acts = sim.tick(OFF, 1)
    assert [type(a) for a in acts] == [StopAndFinalize, HidePill]
    final = acts[0]
    assert isinstance(final, StopAndFinalize) and final.confirmed is False
    assert final.meeting.title == "Teams" and final.meeting.code is None


def test_generic_session_ending_while_the_popup_is_up_is_finalized_not_discarded() -> None:
    sim = Sim()
    to_popup(sim)
    sim.tick(IN_TEAMS, 3)
    assert sim.tick(OFF, 15) == []  # la cuenta atras del popup se congela con la senal caida
    acts = sim.tick(OFF, 1)
    assert [type(a) for a in acts] == [ClosePopup, StopAndFinalize, HidePill]
    assert acts[0] == ClosePopup("meeting_ended")
    assert isinstance(acts[1], StopAndFinalize) and acts[1].confirmed is False
    assert not any(isinstance(a, DiscardRecording) for a in sim.log)


def test_confirmed_generic_session_finalizes_confirmed() -> None:
    sim = Sim()
    to_popup(sim)
    sim.event(PopupAnswer(True))
    acts = sim.tick(OFF, 16)
    assert acts[0] == StopAndFinalize(True, MeetingInfo(title="Teams", generic_app=TEAMS.key))


def test_other_app_does_not_keep_the_session_alive() -> None:
    sim = Sim()
    to_recording(sim)
    acts = sim.tick(asnap(ZOOM), 16)
    assert "StopAndFinalize" in names(acts)


# ----------------------------------------------------------------------------- tray / usuario


def test_record_now_during_prepopup_confirms() -> None:
    sim = Sim()
    to_recording(sim)
    assert sim.event(RecordNow()) == [ConfirmRecording(), ShowPill(False, False)]
    assert sim.m.state is WatcherState.RECORDING_CONFIRMED
    assert sim.tick(IN_TEAMS, 60) == []  # ya no habra popup


def test_user_stop_during_prepopup_is_unconfirmed_and_not_retriggered() -> None:
    sim = Sim()
    to_recording(sim)
    acts = sim.event(UserStop())
    assert [type(a) for a in acts] == [StopAndFinalize, HidePill]
    assert sim.tick(IN_TEAMS, 120) == []


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
    # la sesion ahora vive de la pestana de Meet
    final = sim.tick(IN_CHROME, 16)
    assert isinstance(final[-2], StopAndFinalize) and final[-2].meeting.code == CODE_A


def test_upgrade_after_the_popup_was_shown_adopts_the_code_silently() -> None:
    sim = Sim()
    to_popup(sim, IN_CHROME)
    acts = sim.tick(asnap(CHROME_APP, tabs=(tab(),)))
    assert acts == []  # no se vuelve a mostrar
    sim.event(PopupAnswer(True))
    final = sim.tick(OFF, 16)[0]
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
    assert acts == [DiscardRecording(), ClosePopup("declined"), HidePill()]
    assert sim.m.ignored_keys == {CODE_A, "app:chrome.exe"}
    assert sim.tick(snap, 120) == []


def test_meet_path_ending_before_answering_still_discards() -> None:
    sim = Sim()
    sim.to_popup(asnap(CHROME_APP, tabs=(tab(),)))
    acts = sim.tick(OFF, 16)
    assert acts == [ClosePopup("meeting_ended"), DiscardRecording()]


# ----------------------------------------------------------------------------- config


def test_generic_apps_false_only_meet_path_is_active() -> None:
    sim = Sim(generic_apps=False)
    assert sim.tick(IN_TEAMS, 600) == []
    assert sim.tick(IN_CHROME, 600) == []
    assert sim.m.state is WatcherState.IDLE
    snap = asnap(CHROME_APP, tabs=(tab(),))
    assert names(sim.tick(snap, 6)) == ["StartRecording", "ShowPopup"]


def test_suspend_clears_the_generic_counters() -> None:
    from micnaga.watcher.machine import SuspendWatching

    sim = Sim()
    sim.tick(IN_TEAMS, 15)
    sim.event(SuspendWatching(100))
    assert sim.m.state is WatcherState.SUSPENDED
    assert sim.tick(IN_TEAMS, 100) == []
    assert sim.m.phase in (Phase.IDLE, Phase.CANDIDATE)
    assert sim.tick(IN_TEAMS, 19) == []  # la cuenta de 20 s empezo de cero al reanudar
