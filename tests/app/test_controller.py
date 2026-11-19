"""``AppController``: cada llamada del vigilante, con grabador y UI falsos."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from micnaga import holding, paths
from micnaga.watcher import MeetingInfo
from tests.app.fakes import Rig
from tests.watcher.helpers import CODE_A, MONITOR

MEETING = MeetingInfo(title="Revisión semanal", code=CODE_A, browser="chrome.exe", monitor=MONITOR)


@pytest.fixture
def rig() -> Rig:
    return Rig()


def test_start_recording_opens_session_and_recorder(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    rec = rig.rec
    assert rec.names == ["start"]
    assert rec.calls[0][1] == (False, "Revisión semanal", CODE_A)  # provisional: no confirmada
    assert rec.dir is not None and rec.dir.parent == paths.meetings_root()
    assert "revision-semanal" in rec.dir.name
    assert rig.ctl.recording and rig.ctl.session_dir == rec.dir


def test_start_recording_confirmed_when_not_provisional(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=False, meeting=MeetingInfo(manual=True))
    assert rig.rec.calls[0][1] == (True, None, None)
    assert paths.DEFAULT_SLUG in rig.rec.dir.name  # type: ignore[union-attr]


def test_second_start_is_ignored(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    assert len(rig.recorders.made) == 1


def test_failed_start_makes_everything_else_a_noop(rig: Rig) -> None:
    rig.recorders.fail_next = True
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    assert not rig.ctl.recording
    rig.ctl.confirm_recording()
    rig.ctl.pause_recording()
    rig.ctl.resume_recording()
    rig.ctl.show_pill(provisional=True, paused=False)
    assert not rig.pill.visible  # nunca una pastilla sin grabacion
    rig.ctl.stop_and_finalize(confirmed=True, meeting=MEETING)
    rig.ctl.begin_discard(undo_s=8)
    rig.ctl.hold_recording(reason="declined", trim=False)
    rig.ctl.show_popup(title="x", code=None, monitor_hint=None, timeout_s=15.0)
    assert not rig.popup.visible  # ni popup sin grabacion detras
    assert rig.ctl.add_marker() is False
    rig.settle()
    assert rig.jobs() == []


def test_failed_start_tells_the_user_and_the_watcher(rig: Rig) -> None:
    """Hallazgo 2: el fallo de apertura se ve (toast) y la maquina vuelve a vigilar."""
    rig.recorders.fail_next = True
    assert rig.watcher is not None
    rig.watcher.record_now()  # «Grabar ahora» -> StartRecording falla
    assert not rig.ctl.recording
    assert rig.toaster.toasts, "no error toast"
    assert rig.watcher.phase.value == "idle"
    assert not rig.pill.visible
    rig.watcher.record_now()  # reintento inmediato: ahora si abre
    assert rig.ctl.recording and rig.pill.visible
    assert rig.watcher.phase.value == "recording_confirmed"


def test_show_popup_uses_watcher_rect_long_timeout_and_real_total(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    rig.ctl.show_popup(title="Revisión semanal", code=CODE_A, monitor_hint=MONITOR, timeout_s=15.0)
    assert ("monitor_info", (MONITOR.rect,)) in rig.w32.calls
    shown = next(a for a in rig.js(rig.popup) if a.startswith("micnaga.popup.show("))
    payload = json.loads(shown[len("micnaga.popup.show(") : -1])
    assert payload["timeoutS"] >= 3600  # la cuenta atras del JS no manda
    assert rig.app.ui.popup_state()["total_s"] == 15.0  # type: ignore[attr-defined]


def test_show_popup_generic_uses_foreground_window(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    rig.ctl.show_popup(title="Teams", code=None, monitor_hint=None, timeout_s=15.0)
    assert ("monitor_info", (4321,)) in rig.w32.calls


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("confirmed", "micnaga.popup.accept()"),
        ("declined", "micnaga.popup.dismiss()"),
        ("timeout", "micnaga.popup.expire"),
        ("meeting_ended", "micnaga.popup.end()"),
        ("suspended", "micnaga.popup.end()"),
    ],
)
def test_close_popup_maps_every_reason(rig: Rig, reason: str, expected: str) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    rig.ctl.show_popup(title="x", code=None, monitor_hint=None, timeout_s=15.0)
    rig.ctl.close_popup(reason=reason)
    assert any(expected in js for js in rig.js(rig.popup)), rig.js(rig.popup)


def test_confirm_sets_the_recorder_flag(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    rig.ctl.confirm_recording()
    assert ("set_confirmed", True) in rig.rec.calls


def test_no_is_persisted_synchronously_and_pauses_capture(rig: Rig) -> None:
    """Hallazgo 1: la marca de descartada esta en disco al volver de ``begin_discard`` (antes
    de cerrar el popup); la captura se pausa y la UI ofrece «Deshacer»."""
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    folder = rig.rec.dir
    assert folder is not None
    rig.ctl.begin_discard(undo_s=8)
    assert holding.discard_reason(folder) == holding.REASON_DECLINED
    assert "pause" in rig.rec.names and "stop" not in rig.rec.names
    assert any(js.startswith("micnaga.pill.undo(") for js in rig.js(rig.pill))
    assert rig.pill.visible  # la capsula muestra «Descartada · Deshacer»


def test_undo_removes_the_mark_and_resumes(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    folder = rig.rec.dir
    assert folder is not None
    rig.ctl.begin_discard(undo_s=8)
    rig.ctl.cancel_discard()
    assert holding.discard_reason(folder) is None
    assert rig.rec.names[-1] == "resume"
    assert rig.ctl.recording


def test_hold_stops_and_moves_to_descartadas_and_queues_nothing(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    folder = rig.rec.dir
    assert folder is not None and folder.is_dir()
    rig.ctl.begin_discard(undo_s=8)
    rig.ctl.hold_recording(reason="declined", trim=False)
    rig.settle()
    assert "stop" in rig.rec.names
    assert not folder.exists()  # ya no esta entre las reuniones...
    held = holding.held_root(folder.parent) / folder.name
    assert held.is_dir() and (held / holding.HELD_FILE).is_file()  # ...sino apartada
    assert not (held / holding.DISCARD_MARKER).exists()
    assert rig.jobs() == []
    assert not rig.ctl.recording


def test_stop_sets_metadata_before_stopping_then_queues(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MeetingInfo(title="Teams"))
    final = MeetingInfo(title="Revisión final", code=CODE_A)
    rig.ctl.stop_and_finalize(confirmed=True, meeting=final)
    rig.settle()
    names = rig.rec.names
    assert names.index("set_meeting_info") < names.index("stop")  # el grabador solo persiste antes
    assert names.index("set_confirmed") < names.index("stop")
    assert ("set_meeting_info", ("Revisión final", CODE_A)) in rig.rec.calls
    jobs = rig.jobs()
    assert len(jobs) == 1 and Path(jobs[0].session_dir) == rig.rec.dir
    assert not rig.ctl.recording


def test_stop_provisional_does_not_confirm(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    rig.ctl.stop_and_finalize(confirmed=False, meeting=MEETING)
    rig.settle()
    assert "set_confirmed" not in rig.rec.names
    assert len(rig.jobs()) == 1


def test_stop_twice_queues_once(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    rig.ctl.stop_and_finalize(confirmed=True, meeting=MEETING)
    rig.ctl.stop_and_finalize(confirmed=True, meeting=MEETING)
    rig.settle()
    assert len(rig.jobs()) == 1


def test_show_pill_sets_paused_first_and_uses_recorder_elapsed(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    rig.rec.elapsed = 33.0
    rig.ctl.show_pill(provisional=True, paused=True)
    js = rig.js(rig.pill)
    paused_at = next(i for i, c in enumerate(js) if "setPaused(true)" in c)
    show_at = next(i for i, c in enumerate(js) if c.startswith("micnaga.pill.show("))
    assert paused_at < show_at
    payload = json.loads(js[show_at][len("micnaga.pill.show(") : -1])
    assert payload["elapsed"] == 33.0 and payload["provisional"] is True
    assert payload["paused"] is True


def test_hide_pill(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    rig.ctl.show_pill(provisional=True, paused=False)
    assert rig.pill.visible
    rig.ctl.hide_pill()
    assert not rig.pill.visible


def test_pause_and_resume_drive_recorder_and_pill(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    rig.ctl.show_pill(provisional=True, paused=False)
    rig.ctl.pause_recording()
    assert "pause" in rig.rec.names
    assert any("setPaused(true)" in js for js in rig.js(rig.pill))
    rig.rec.elapsed = 40.0
    rig.ctl.resume_recording()
    assert "resume" in rig.rec.names
    assert any("setPaused(false)" in js for js in rig.js(rig.pill))
    assert any("pill.sync(40.00)" in js for js in rig.js(rig.pill))


def test_add_marker_only_while_recording(rig: Rig) -> None:
    assert rig.ctl.add_marker() is False
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    assert rig.ctl.add_marker() is True
    assert rig.rec.markers == [""]


def test_levels_follow_the_recorder(rig: Rig) -> None:
    assert rig.ctl.levels() == (0.0, 0.0)
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    assert rig.app.ui.levels() == (0.25, 0.5)  # type: ignore[attr-defined]


def test_shutdown_with_a_confirmed_recording_queues_it(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=False, meeting=MEETING)
    rig.ctl.show_pill(provisional=False, paused=False)
    rig.ctl.shutdown(wait_s=10)
    assert "stop" in rig.rec.names
    assert rig.rec.dir is not None and rig.rec.dir.is_dir()  # no se borra
    assert len(rig.jobs()) == 1
    assert not rig.pill.visible


def test_shutdown_with_an_unconfirmed_recording_holds_it(rig: Rig) -> None:
    """Sin «Si» nunca se transcribe, ni siquiera al salir; tampoco se pierde."""
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    folder = rig.rec.dir
    assert folder is not None
    rig.ctl.shutdown(wait_s=10)
    assert "stop" in rig.rec.names
    assert rig.jobs() == []
    assert (holding.held_root(folder.parent) / folder.name).is_dir()


def test_ticker_syncs_pill_clock(rig: Rig) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    rig.rec.elapsed = 61.0
    rig.ctl.tick_sync()
    assert any("pill.sync(61.00)" in js for js in rig.js(rig.pill))
