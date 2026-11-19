"""El controlador lanza y para el procesado en vivo con la grabacion (``integration.live``)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from micnaga.config import Config
from micnaga.integration.live import LiveRunner
from micnaga.pipeline import live
from micnaga.watcher import MeetingInfo
from tests.app.fakes import Rig
from tests.watcher.helpers import CODE_A, MONITOR

MEETING = MeetingInfo(title="Revisión semanal", code=CODE_A, browser="chrome.exe", monitor=MONITOR)


class FakeLive:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def start(self, session: Path) -> None:
        self.calls.append(("start", session))

    def signal_stop(self, session: Path) -> None:
        self.calls.append(("signal_stop", session))

    def stop(self, session: Path, *, wait_s: float | None = None) -> None:
        self.calls.append(("stop", session))

    def stop_all(self, *, wait_s: float | None = None) -> None:
        self.calls.append(("stop_all", Path()))

    @property
    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def fake() -> FakeLive:
    return FakeLive()


@pytest.fixture
def rig(fake: FakeLive) -> Rig:
    return Rig(live=fake)


def test_recording_starts_live_processing_of_its_folder(rig: Rig, fake: FakeLive) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    assert fake.calls == [("start", rig.rec.dir)]


def test_no_stops_live_processing_at_once_and_undo_restarts_it(rig: Rig, fake: FakeLive) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    rig.ctl.begin_discard(undo_s=8)
    assert fake.names == ["start", "signal_stop"]
    rig.ctl.cancel_discard()
    assert fake.names == ["start", "signal_stop", "start"]


def test_stop_waits_for_live_processing_before_finalizing(rig: Rig, fake: FakeLive) -> None:
    rig.ctl.start_recording(provisional=False, meeting=MEETING)
    folder = rig.rec.dir
    rig.ctl.stop_and_finalize(confirmed=True, meeting=MEETING)
    rig.settle()
    assert fake.calls[-1] == ("stop", folder)
    assert "stop" in rig.rec.names and len(rig.jobs()) == 1


def test_hold_stops_live_processing_and_drops_its_results(rig: Rig, fake: FakeLive) -> None:
    rig.ctl.start_recording(provisional=True, meeting=MEETING)
    folder = rig.rec.dir
    assert folder is not None
    (live.live_dir(folder) / "asr").mkdir(parents=True)
    (live.live_dir(folder) / "asr" / "mic_0_1000.json").write_text("[]", encoding="utf-8")
    rig.ctl.begin_discard(undo_s=8)
    rig.ctl.hold_recording(reason="declined", trim=False)
    rig.settle()
    assert ("stop", folder) in fake.calls
    from micnaga import holding

    held = holding.held_root(folder.parent) / folder.name
    assert held.is_dir() and not (held / "work" / "live").exists()


# ----------------------------------------------------------------------------- LiveRunner


class FakeProc:
    def __init__(self, exits: bool = True) -> None:
        self.exits = exits
        self.killed = False
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code

    def wait(self, timeout: float | None = None) -> int:
        if not self.exits and not self.killed:
            raise subprocess.TimeoutExpired("live", timeout or 0)
        self.code = 0
        return 0

    def kill(self) -> None:
        self.killed = True


def test_runner_spawns_once_asks_to_stop_and_kills_a_stuck_process(tmp_path: Path) -> None:
    procs: list[FakeProc] = []

    def spawn(_session: Path) -> FakeProc:
        procs.append(FakeProc(exits=False))
        return procs[-1]

    cfg = Config()
    runner = LiveRunner(lambda: cfg, spawn=spawn, stop_wait_s=0.01)
    session = tmp_path / "s"
    session.mkdir()
    runner.start(session)
    runner.start(session)  # ya corre: no se lanza otro
    assert len(procs) == 1
    runner.stop(session)
    assert (live.live_dir(session) / live.STOP_FILE).is_file()
    assert procs[0].killed
    runner.start(session)  # p. ej. «Deshacer»: se quita la orden de parar
    assert not (live.live_dir(session) / live.STOP_FILE).exists()


def test_runner_does_nothing_when_disabled_or_without_models(tmp_path: Path) -> None:
    spawned: list[Path] = []
    cfg = Config()
    cfg.processing.live = False
    LiveRunner(lambda: cfg, spawn=lambda s: spawned.append(s)).start(tmp_path)
    cfg.processing.live = True
    LiveRunner(lambda: cfg, spawn=lambda s: spawned.append(s), models_ready=lambda: False).start(
        tmp_path
    )
    assert spawned == []
