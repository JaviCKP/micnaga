"""Dobles de prueba de la app: UI, grabador y rig completo (vigilante real + UiHost falso)."""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from micnaga.config import Config
from micnaga.integration.application import Application
from micnaga.ui.host import UiHost
from micnaga.ui.windows import FakeFactory, FakeWindow
from micnaga.watcher import Watcher
from micnaga.watcher.model import MeetTab, MicApp, SignalSnapshot
from tests.ui.fakes import FakeWin32
from tests.watcher.helpers import CODE_A, MONITOR


class CardView:
    """La tarjeta «¿Estás en una reu?» dentro de la ventana de la pastilla: delega en ella y
    esta «visible» mientras la ventana se ve CON la tarjeta abierta."""

    def __init__(self, win: FakeWindow, host: UiHost | None) -> None:
        self._win = win
        self._host = host

    def __getattr__(self, name: str) -> Any:
        return getattr(self._win, name)

    @property
    def visible(self) -> bool:
        return self._win.visible and self._host is not None and self._host.popup_visible


class SurfaceSim(FakeWindow):
    """La ventana de la pastilla con un JS minimo: cuando Python le pide cerrar la tarjeta
    (respondida, caducada, retirada) el JS real la encoge y ~0,3 s despues avisa con
    ``popup_closed``. Aqui ese aviso se encola y llega en el siguiente ``Rig.feed``/``settle``
    (como en la app: despues de las acciones que el vigilante emite en el mismo paso)."""

    host: UiHost | None = None

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.pending: list[Callable[[], None]] = []

    def run_js(self, script: str) -> None:
        super().run_js(script)
        h = self.host
        if h is None:
            return
        if script.startswith(("micnaga.popup.show(", "micnaga.popup.showEnd(")):
            # el JS funde la pastilla y pide la talla de tarjeta (sin efecto si ya la tiene)
            self.pending.append(lambda: h.handle_surface_resize("card"))
        elif script in ("micnaga.popup.end()", "micnaga.popup.dismiss()") or script.startswith(
            "micnaga.popup.endClose("
        ):
            kind = "fading" if "end" in script else "leaving"
            self.pending.append(lambda: h.handle_popup_closed(kind))
        elif script == "micnaga.popup.accept()":
            self.pending.append(
                lambda: (h.handle_popup_morph(False), h.handle_popup_closed("morphed"))
            )
        elif "micnaga.popup.expire()" in script:
            if h._popup_on_timeout == "discard":
                self.pending.append(lambda: h.handle_popup_closed("leaving"))
            else:
                self.pending.append(
                    lambda: (h.handle_popup_morph(True), h.handle_popup_closed("morphed"))
                )

    def flush(self) -> None:
        while self.pending:
            self.pending.pop(0)()


class SimFactory(FakeFactory):
    def create(self, spec: Any) -> Any:
        if spec.key != "pill":
            return super().create(spec)
        with self._lock:
            self._next += 1
            win = SurfaceSim(spec, self._next)
            self.windows[spec.key] = win
            self.created.append(spec)
        return win


class FakeRecorder:
    """Grabador falso: apunta las llamadas (con su orden) y crea la carpeta."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.dir: Path | None = None
        self.confirmed = False
        self.title: str | None = None
        self.code: str | None = None
        self.stopped = threading.Event()
        self.elapsed = 12.5
        self.fail_start = False
        self.markers: list[str] = []

    def start(
        self,
        session_dir: Path,
        *,
        confirmed: bool = False,
        meet_title: str | None = None,
        meet_code: str | None = None,
    ) -> Path:
        self.calls.append(("start", (confirmed, meet_title, meet_code)))
        if self.fail_start:
            raise RuntimeError("no audio source")
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "audio_yo.wav").write_bytes(b"x")
        self.dir, self.confirmed, self.title, self.code = (
            session_dir,
            confirmed,
            meet_title,
            meet_code,
        )
        return session_dir

    def pause(self) -> bool:
        self.calls.append(("pause", None))
        return True

    def resume(self) -> bool:
        self.calls.append(("resume", None))
        return True

    def add_marker(self, label: str = "") -> Any:
        self.calls.append(("marker", label))
        self.markers.append(label)
        return None

    def set_confirmed(self, confirmed: bool = True) -> None:
        self.calls.append(("set_confirmed", confirmed))
        self.confirmed = confirmed

    def set_meeting_info(self, title: str | None = None, code: str | None = None) -> None:
        self.calls.append(("set_meeting_info", (title, code)))
        self.title, self.code = title or self.title, code or self.code

    def stop(self) -> Path:
        self.calls.append(("stop", None))
        self.stopped.set()
        assert self.dir is not None
        return self.dir

    def levels(self) -> tuple[float, float]:
        return (0.25, 0.5)

    def elapsed_s(self) -> float:
        return self.elapsed

    @property
    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


class RecorderBox:
    """Fabrica de grabadores falsos: guarda el ultimo y todos."""

    def __init__(self) -> None:
        self.made: list[FakeRecorder] = []
        self.fail_next = False

    def __call__(self, _capture: Any) -> FakeRecorder:
        rec = FakeRecorder()
        rec.fail_start = self.fail_next
        self.fail_next = False
        self.made.append(rec)
        return rec

    @property
    def last(self) -> FakeRecorder:
        return self.made[-1]


class FakeToaster:
    def __init__(self) -> None:
        self.toasts: list[Any] = []

    def show_toast(self, toast: Any) -> None:
        self.toasts.append(toast)


class NullSampler:
    def sample(self) -> SignalSnapshot:
        return SignalSnapshot()

    def diagnostics(self) -> dict[str, Any]:
        return {"signals": []}

    def close(self) -> None:
        return None


class FakeHotkeys:
    def __init__(self, marker: Callable[[], None], private_pause: Callable[[], None]) -> None:
        self.marker = marker
        self.private_pause = private_pause
        self.started = False
        self.stopped = False

    def start(self, timeout_s: float = 5.0) -> dict[str, Any]:
        self.started = True
        return {}

    def stop(self, timeout_s: float = 5.0) -> None:
        self.stopped = True


class FakeProc:
    """``Popen`` falso para el supervisor del worker."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def meet_snapshot(code: str = CODE_A, title: str | None = "Revisión semanal") -> SignalSnapshot:
    tab = MeetTab("chrome.exe", title, code, active=True, hwnd=1, monitor=MONITOR)
    return SignalSnapshot(mic_browsers=frozenset({"chrome.exe"}), meet_tabs=(tab,))


def app_snapshot(label: str = "Teams", exe: str = "teams.exe") -> SignalSnapshot:
    app = MicApp(key=f"app:{exe}", label=label, exe=exe)
    return SignalSnapshot(mic_apps=frozenset({app}))


class Rig:
    """Aplicacion completa con vigilante real (reloj falso, ``feed``), ``UiHost`` falso y
    grabador falso. Nada toca dispositivos, registro ni procesos reales."""

    def __init__(
        self, cfg: Config | None = None, *, real_worker: bool = False, **app_kwargs: Any
    ) -> None:
        self.cfg = cfg or Config()
        self.clock = Clock()
        self.w32 = FakeWin32()
        self.w32.animation = False
        self.factory = SimFactory()
        self.toaster = FakeToaster()
        self.recorders = RecorderBox()
        self.spawned: list[FakeProc] = []
        self.hotkeys: FakeHotkeys | None = None
        self.host: UiHost | None = None
        self.watcher: Watcher | None = None
        self.worker_running = False
        self.models_ok = True

        def ui_factory(callbacks: Any, ops: Any, download: Any, diagnostics: Any) -> UiHost:
            host = UiHost(
                config=copy.deepcopy(self.cfg),
                callbacks=callbacks,
                library_ops=ops,
                download_models=download,
                diagnostics=diagnostics,
                factory=self.factory,
                win32=self.w32,
                toaster=self.toaster,
                spawn=lambda fn: fn(),
                tray=False,
                play=lambda _b: None,
            )
            host._create_overlays()
            SurfaceSim.host = None
            self.factory.windows["pill"].host = host  # type: ignore[attr-defined]
            self.host = host
            return host

        def watcher_factory(config: Config, controller: Any, on_state: Any) -> Watcher:
            w = Watcher(
                config,
                controller,
                sampler=NullSampler(),  # type: ignore[arg-type]
                clock=self.clock,
                on_state_change=on_state,
            )
            self.watcher = w
            return w

        def hotkeys_factory(config: Config, marker: Any, private_pause: Any) -> FakeHotkeys:
            self.hotkeys = FakeHotkeys(marker, private_pause)
            return self.hotkeys

        def spawn() -> FakeProc | None:
            if self.worker_running:
                return None
            proc = FakeProc()
            self.spawned.append(proc)
            return proc

        wiring: dict[str, Any] = {
            "spawn": spawn,
            "is_worker_running": lambda: self.worker_running,
            "models_ready": lambda: self.models_ok,
            "events_name": rf"\\.\pipe\micnaga-test-{id(self):x}",
            "start_events_server": False,
        }
        if real_worker:  # worker y tuberia de verdad (tests e2e con modelos)
            wiring = {"start_events_server": True, "models_ready": lambda: True}
        self.app = Application(
            self.cfg,
            ui_factory=ui_factory,
            watcher_factory=watcher_factory,  # type: ignore[arg-type]
            hotkeys_factory=hotkeys_factory,  # type: ignore[arg-type]
            recorder_factory=self.recorders,
            recover_at_boot=False,
            warmup=False,
            start_watcher=False,
            supervisor_interval_s=0.05 if not real_worker else 1.0,
            foreground=lambda: 4321,
            **wiring,
            **app_kwargs,
        )
        assert self.host is not None and self.watcher is not None

    # ------------------------------------------------------------------ atajos

    @property
    def popup(self) -> Any:
        """La tarjeta: vive en la ventana de la pastilla (superficie unica)."""
        return CardView(self.factory.windows["pill"], self.host)

    @property
    def pill(self) -> FakeWindow:
        return self.factory.windows["pill"]

    @property
    def ctl(self) -> Any:
        return self.app.controller

    @property
    def rec(self) -> FakeRecorder:
        return self.recorders.last

    def feed(self, snap: SignalSnapshot | None, seconds: int = 1) -> None:
        assert self.watcher is not None
        for _ in range(seconds):
            self.clock.t += 1.0
            self.watcher.feed(snap)
            self.ui_tick()

    def ui_tick(self) -> None:
        """El JS de la superficie termina lo que tenia en marcha (encoger la tarjeta)."""
        self.factory.windows["pill"].flush()  # type: ignore[attr-defined]

    def settle(self) -> None:
        self.ui_tick()
        assert self.ctl.wait_idle(30)
        self.ui_tick()

    def jobs(self) -> list[Any]:
        from micnaga.store.db import open_database

        db = open_database(self.cfg)
        try:
            return db.list_jobs()
        finally:
            db.close()

    def js(self, win: FakeWindow) -> list[str]:
        return [str(a) for k, a in win.log if k == "js"]
