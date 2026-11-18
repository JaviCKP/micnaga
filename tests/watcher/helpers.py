"""Utilidades de los tests del vigilante: controlador falso, instantaneas y simulador de reloj."""

from __future__ import annotations

from typing import Any

from micnaga.config import DetectionConfig
from micnaga.watcher.machine import Action, Event, Tick, WatcherStateMachine
from micnaga.watcher.model import MeetingInfo, MeetTab, MonitorHint, SignalSnapshot

CHROME = "chrome.exe"
EDGE = "msedge.exe"
MONITOR = MonitorHint(handle=7, rect=(1920, 0, 3840, 1080), work_area=(1920, 0, 3840, 1040))

CODE_A = "abc-defg-hij"
CODE_B = "xyz-wxyz-uvw"


def tab(
    code: str | None = CODE_A,
    title: str | None = None,
    browser: str = CHROME,
    active: bool = True,
) -> MeetTab:
    return MeetTab(
        browser=browser,
        title=title,
        code=code,
        raw_name=f"Meet - {code}",
        active=active,
        hwnd=1,
        monitor=MONITOR,
    )


def snap(*tabs: MeetTab, mic: tuple[str, ...] = (CHROME,)) -> SignalSnapshot:
    return SignalSnapshot(mic_browsers=frozenset(mic), meet_tabs=tuple(tabs))


ON = snap(tab())  # Meet A abierto y micro en uso
OFF = SignalSnapshot()  # nada
MIC_ONLY = snap(mic=(CHROME,))  # micro en uso sin pestana de Meet
MEET_ONLY = SignalSnapshot(meet_tabs=(tab(),))  # Meet abierto, micro sin usar


class Sim:
    """Reloj falso + maquina. ``tick`` avanza 1 s por llamada (como el sondeo real)."""

    def __init__(self, **overrides: Any) -> None:
        self.cfg = DetectionConfig(**overrides)
        self.m = WatcherStateMachine(self.cfg)
        self.now = 1000.0
        self.log: list[Action] = []

    def event(self, ev: Event) -> list[Action]:
        acts = self.m.step(ev, self.now)
        self.log.extend(acts)
        return acts

    def tick(self, snapshot: SignalSnapshot | None, n: int = 1) -> list[Action]:
        """``n`` ticks de 1 s con la misma instantanea; devuelve todas las acciones emitidas.

        Cada tick avanza el reloj ANTES de entregarse: tras ``tick`` el reloj marca el instante
        del ultimo tick y los eventos de usuario ocurren en ese mismo instante.
        """
        out: list[Action] = []
        for _ in range(n):
            self.now += 1.0
            out.extend(self.event(Tick(snapshot)))
        return out

    def to_popup(self, snapshot: SignalSnapshot = ON) -> list[Action]:
        """Avanza hasta que aparece el popup."""
        out: list[Action] = []
        for _ in range(60):
            out.extend(self.tick(snapshot))
            if any(type(a).__name__ == "ShowPopup" for a in out):
                return out
        raise AssertionError("popup never appeared")


def names(actions: list[Action]) -> list[str]:
    return [type(a).__name__ for a in actions]


class FakeController:
    """Implementa ``WatcherController`` registrando las llamadas."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _rec(self, name: str, **kw: Any) -> None:
        self.calls.append((name, kw))

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def start_recording(self, *, provisional: bool, meeting: MeetingInfo) -> None:
        self._rec("start_recording", provisional=provisional, meeting=meeting)

    def show_popup(self, **kw: Any) -> None:
        self._rec("show_popup", **kw)

    def close_popup(self, *, reason: str) -> None:
        self._rec("close_popup", reason=reason)

    def confirm_recording(self) -> None:
        self._rec("confirm_recording")

    def discard_recording(self) -> None:
        self._rec("discard_recording")

    def stop_and_finalize(self, *, confirmed: bool, meeting: MeetingInfo) -> None:
        self._rec("stop_and_finalize", confirmed=confirmed, meeting=meeting)

    def show_pill(self, *, provisional: bool, paused: bool) -> None:
        self._rec("show_pill", provisional=provisional, paused=paused)

    def hide_pill(self) -> None:
        self._rec("hide_pill")

    def pause_recording(self) -> None:
        self._rec("pause_recording")

    def resume_recording(self) -> None:
        self._rec("resume_recording")
