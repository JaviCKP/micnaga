"""Fachada ``Watcher``: hilo real con senales y controlador falsos (tiempos cortos)."""

from __future__ import annotations

import dataclasses
import itertools
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import psutil
import pytest

from micnaga.config import DetectionConfig
from micnaga.watcher import Watcher, WatcherState
from micnaga.watcher.model import MeetTab, SignalReading, SignalSnapshot
from micnaga.watcher.sampler import SignalSampler
from micnaga.watcher.sources import MicReading, TabsReading
from tests.watcher.helpers import CODE_A, FakeController

FAST = DetectionConfig(
    poll_interval_s=0.03,
    confirm_after_s=0.15,
    end_after_s=0.3,
    end_confirm_s=0.1,
    end_popup_timeout_s=30.0,  # los popups esperan respuesta: el test contesta explicitamente
    undo_s=30.0,
    popup_timeout_s=30.0,
)
SHORT_TIMEOUT = dataclasses.replace(FAST, popup_timeout_s=0.5)
SHORT_END_TIMEOUT = dataclasses.replace(FAST, end_popup_timeout_s=0.3)


class Signals:
    """Senales mutables: el test las cambia mientras el hilo del vigilante las lee."""

    def __init__(self) -> None:
        self.on = False
        self.fail = False

    def sampler(self) -> SignalSampler:
        return SignalSampler(self._mic(), self._tabs(), None)

    def _mic(self) -> Any:
        sig = self

        class Mic:
            def read(self) -> MicReading:
                if sig.fail:
                    raise RuntimeError("sampler broke")
                return MicReading(
                    frozenset({"chrome.exe"}) if sig.on else frozenset(),
                    SignalReading("mic", sig.on),
                )

        return Mic()

    def _tabs(self) -> Any:
        sig = self

        class Tabs:
            def read(self, mic_browsers: frozenset[str]) -> TabsReading:
                tabs = (MeetTab("chrome.exe", None, CODE_A, active=True),) if sig.on else ()
                return TabsReading(tabs, SignalReading("meet_tab", bool(tabs)))

        return Tabs()


def until(pred: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _make_rig(
    cfg: DetectionConfig,
) -> Iterator[tuple[Watcher, FakeController, Signals]]:
    sig, ctl = Signals(), FakeController()
    w = Watcher(cfg, ctl, sampler=sig.sampler())
    try:
        yield w, ctl, sig
    finally:
        w.stop()
        assert not w.running  # nunca quedan hilos vivos


@pytest.fixture
def rig() -> Iterator[tuple[Watcher, FakeController, Signals]]:
    """Popup con 30 s de margen: los tests con 4 agentes cargando la maquina no flaquean."""
    yield from _make_rig(FAST)


@pytest.fixture
def short_rig() -> Iterator[tuple[Watcher, FakeController, Signals]]:
    yield from _make_rig(SHORT_TIMEOUT)


def test_start_stop_is_prompt_and_idempotent() -> None:
    w = Watcher(FAST, FakeController(), sampler=Signals().sampler())
    w.start()
    w.start()  # idempotente
    assert w.running
    t0 = time.monotonic()
    w.stop()
    w.stop()
    assert not w.running and time.monotonic() - t0 < 1.0
    assert not [t for t in threading.enumerate() if t.name == "micnaga-watcher"]


def test_end_to_end_prompt_confirm_and_hangup(
    rig: tuple[Watcher, FakeController, Signals],
) -> None:
    w, ctl, sig = rig
    w.start()
    time.sleep(0.3)
    assert ctl.calls == [] and w.state is WatcherState.IDLE  # nada sin senales
    sig.on = True
    assert until(lambda: "show_popup" in ctl.names())
    assert ctl.names()[:2] == ["start_recording", "show_popup"]  # grabar antes de mostrar
    assert ctl.calls[0][1]["provisional"] is True
    w.popup_answer(True)
    assert ctl.names()[2:5] == ["confirm_recording", "close_popup", "show_pill"]
    sig.on = False
    assert until(lambda: "show_end_popup" in ctl.names())
    assert ctl.names()[-2:] == ["mark_end", "show_end_popup"]
    assert ctl.calls[-1][1]["confirmed"] is True
    assert w.state is WatcherState.END_PROMPT
    w.end_answer("finish")
    assert ctl.names()[-3:] == ["close_end_popup", "stop_and_finalize", "hide_pill"]
    final = ctl.calls[-2][1]
    assert final == {"confirmed": True, "meeting": final["meeting"], "trim": True}
    assert final["meeting"].code == CODE_A
    assert "hold_recording" not in ctl.names()


def test_end_popup_timeout_finalizes_a_confirmed_session() -> None:
    sig, ctl = Signals(), FakeController()
    w = Watcher(SHORT_END_TIMEOUT, ctl, sampler=sig.sampler())
    sig.on = True
    w.start()
    try:
        assert until(lambda: "show_popup" in ctl.names())
        w.popup_answer(True)
        sig.on = False
        assert until(lambda: "hide_pill" in ctl.names()[-1:])
        assert ctl.names()[-3:] == ["close_end_popup", "stop_and_finalize", "hide_pill"]
        assert ctl.calls[-3] == ("close_end_popup", {"reason": "timeout"})
        assert ctl.calls[-2][1]["trim"] is True and ctl.calls[-2][1]["confirmed"] is True
    finally:
        w.stop()


def test_undo_window_is_timed_by_fast_ticks_not_by_polls() -> None:
    """Con el sondeo a 5 s, solo los ticks rapidos (0.25 s) pueden cerrar el «Deshacer»."""
    cfg = dataclasses.replace(FAST, poll_interval_s=5.0, confirm_after_s=0.0, undo_s=1.5)
    sig, ctl = Signals(), FakeController()
    w = Watcher(cfg, ctl, sampler=sig.sampler())
    sig.on = True
    w.start()
    try:
        assert until(lambda: "show_popup" in ctl.names())  # primer sondeo
        w.popup_answer(False)
        assert ctl.names()[-2:] == ["close_popup", "begin_discard"]
        assert w.state is WatcherState.DISCARD_PENDING and w.needs_fast_ticks
        assert until(lambda: "hold_recording" in ctl.names(), timeout=4.0)  # < sondeo de 5 s
        assert ctl.calls[-1] == ("hold_recording", {"reason": "declined", "trim": False})
        assert w.state is WatcherState.IGNORED
    finally:
        w.stop()


def test_recording_failed_from_inside_start_recording_returns_to_watching() -> None:
    sig = Signals()
    holder: list[Watcher] = []

    class FailsOnce(FakeController):
        failed = False

        def start_recording(self, **kw: Any) -> None:
            super().start_recording(**kw)
            if not self.failed:
                self.failed = True
                holder[0].recording_failed()  # reentrada: la captura no se pudo abrir

    ctl = FailsOnce()
    w = Watcher(FAST, ctl, sampler=sig.sampler())
    holder.append(w)
    sig.on = True
    w.start()
    try:
        assert until(lambda: "close_popup" in ctl.names())
        assert ctl.names()[:3] == ["start_recording", "show_popup", "close_popup"]
        assert ctl.calls[2][1] == {"reason": "meeting_ended"}
        assert w.state is WatcherState.IGNORED
        time.sleep(0.4)  # la reunion sigue: no se reintenta sola
        assert ctl.names().count("start_recording") == 1
        w.record_now()  # pero «Grabar ahora» si
        assert ctl.names().count("start_recording") == 2
        assert ctl.calls[-2][1]["provisional"] is False
        assert w.state is WatcherState.RECORDING_CONFIRMED
    finally:
        w.stop()


def test_popup_timeout_becomes_provisional(
    short_rig: tuple[Watcher, FakeController, Signals],
) -> None:
    w, ctl, sig = short_rig
    sig.on = True
    w.start()
    assert until(lambda: "show_pill" in ctl.names())
    assert ctl.calls[-1] == ("show_pill", {"provisional": True, "paused": False})
    assert w.state is WatcherState.RECORDING_PROVISIONAL


def test_hover_keeps_the_popup_open(short_rig: tuple[Watcher, FakeController, Signals]) -> None:
    w, ctl, sig = short_rig
    sig.on = True
    w.start()
    assert until(lambda: "show_popup" in ctl.names())
    w.popup_hover(True)
    time.sleep(1.0)  # el doble del timeout
    assert w.state is WatcherState.PROMPTING
    assert "close_popup" not in ctl.names()
    w.popup_hover(False)
    assert until(lambda: "close_popup" in ctl.names())


def test_controller_may_call_back_from_inside_an_action_without_deadlock() -> None:
    sig = Signals()
    holder: list[Watcher] = []

    class ReentrantController(FakeController):
        def show_popup(self, **kw: Any) -> None:
            super().show_popup(**kw)
            holder[0].popup_hover(True)  # reentrada en el mismo hilo
            holder[0].record_now()

    ctl = ReentrantController()
    w = Watcher(FAST, ctl, sampler=sig.sampler())
    holder.append(w)
    sig.on = True
    w.start()
    try:
        assert until(lambda: "show_pill" in ctl.names())
        # orden global preservado aunque haya reentrada
        assert ctl.names()[:2] == ["start_recording", "show_popup"]
        assert "confirm_recording" in ctl.names()
    finally:
        w.stop()


def test_controller_exceptions_do_not_kill_the_watcher() -> None:
    sig = Signals()

    class Exploding(FakeController):
        def show_popup(self, **kw: Any) -> None:
            super().show_popup(**kw)
            raise RuntimeError("UI exploded")

    ctl = Exploding()
    w = Watcher(SHORT_TIMEOUT, ctl, sampler=sig.sampler())
    sig.on = True
    w.start()
    try:
        assert until(lambda: "show_pill" in ctl.names())  # el timeout siguio funcionando
        assert w.running
    finally:
        w.stop()


def test_sampler_errors_are_counted_and_survived(
    rig: tuple[Watcher, FakeController, Signals],
) -> None:
    w, _ctl, sig = rig
    sig.fail = True
    w.start()
    assert until(lambda: w.diagnostics()["poll_errors"] >= 2)
    assert "sampler broke" in (w.diagnostics()["last_error"] or "")
    assert w.running


def test_state_listener_sees_transitions() -> None:
    sig, ctl = Signals(), FakeController()
    seen: list[WatcherState] = []
    w = Watcher(FAST, ctl, sampler=sig.sampler(), on_state_change=seen.append)
    sig.on = True
    w.start()
    try:
        assert until(lambda: WatcherState.PROMPTING in seen)
        w.popup_answer(True)
        assert until(lambda: seen[-1] is WatcherState.RECORDING_CONFIRMED)
        w.toggle_private_pause()
        assert seen[-1] is WatcherState.PAUSED
        w.toggle_private_pause()
        assert seen[-1] is WatcherState.RECORDING_CONFIRMED
    finally:
        w.stop()
    assert seen[0] is WatcherState.CANDIDATE


def test_events_from_other_threads_are_serialised(
    rig: tuple[Watcher, FakeController, Signals],
) -> None:
    w, ctl, _sig = rig
    w.record_now()  # sesion manual: no depende de senales
    threads = [threading.Thread(target=w.set_private_pause, args=(i % 2 == 0,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pauses = [n for n in ctl.names() if n in ("pause_recording", "resume_recording")]
    # Alternancia estricta: nunca dos pausas (o dos reanudaciones) seguidas
    assert all(a != b for a, b in itertools.pairwise(pauses))
    w.user_stop()
    assert ctl.names()[-2:] == ["stop_and_finalize", "hide_pill"]


def test_feed_and_suspend_via_facade() -> None:
    ctl = FakeController()
    w = Watcher(FAST, ctl, sampler=Signals().sampler())
    w.suspend()
    assert w.state is WatcherState.SUSPENDED
    w.resume_watching()
    assert w.state is WatcherState.IDLE
    w.feed(SignalSnapshot())
    assert ctl.calls == []


def test_diagnostics_shape(rig: tuple[Watcher, FakeController, Signals]) -> None:
    w, _ctl, sig = rig
    sig.on = True
    w.start()
    assert until(lambda: w.diagnostics()["state"] == "prompting")
    d = w.diagnostics()
    assert d["session"]["code"] == CODE_A and d["popup_remaining_s"] is not None
    assert [s["name"] for s in d["sampler"]["signals"]] == ["mic", "meet_tab"]


def test_idle_polling_does_not_spin(rig: tuple[Watcher, FakeController, Signals]) -> None:
    w, _ctl, _sig = rig
    proc = psutil.Process()
    w.start()
    before = sum(proc.cpu_times()[:2])
    time.sleep(1.5)
    used = sum(proc.cpu_times()[:2]) - before
    assert used < 0.4  # un bucle activo gastaria ~1.5 s de CPU


def test_extension_stub_import_is_available() -> None:
    from micnaga.watcher import ExtensionSignalSource, NullExtensionSource

    assert isinstance(NullExtensionSource(), ExtensionSignalSource)
