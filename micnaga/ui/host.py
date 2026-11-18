"""``UiHost``: orquesta ventanas, bandeja y notificaciones. Es lo que usa el integrador.

Modelo de hilos
---------------
* ``UiHost.run()`` **ocupa el hilo principal** (pywebview/WinForms lo exigen) hasta ``quit()``.
* Justo despues de arrancar el bucle, pywebview lanza ``_bootstrap`` en un hilo auxiliar: ahi se
  retocan los estilos Win32, se arranca la bandeja (``pystray.Icon.run_detached``: hilo propio) y
  se llama a ``on_ready(host)`` -> el integrador arranca ahi vigilante, captura y cola.
* Dos ventanas ocultas (popup = ventana maestra y pastilla) se crean antes del bucle y se
  **reutilizan** (mostrar/ocultar): evita la latencia de crear un WebView2 (<3 s hasta el popup)
  y mantiene vivo el bucle (pywebview sale al cerrarse la ultima ventana).
* Biblioteca, Ajustes y asistente se crean bajo demanda y se destruyen al cerrarse.
* Toda la API publica es segura desde cualquier hilo. Los callbacks de ``UiCallbacks`` se
  invocan desde hilos de pywebview o de la bandeja, nunca desde el hilo del vigilante.

Popup y pastilla son ``WS_EX_NOACTIVATE``: nunca roban el foco a Meet. Consecuencia: Enter/Esc
solo llegan al popup si tiene el foco. Se resuelve asi: (1) clic en el cuerpo de la tarjeta lo
activa (``popup_activate``, accion deliberada); (2) ``accept_popup()`` / ``dismiss_popup()``
para que el integrador los ate a atajos globales si quiere teclado sin ratón.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from micnaga import paths
from micnaga.config import Config, load_config, save_config
from micnaga.store.db import Database, open_database
from micnaga.ui import i18n, sound, startup, winapi
from micnaga.ui.bridge import LibraryApi, PillApi, PopupApi, SettingsApi, WizardApi
from micnaga.ui.contracts import (
    ConfigStore,
    DiagnosticsProvider,
    DownloadModels,
    LevelsProvider,
    LibraryOps,
    MonitorHint,
    Rect,
    TrayState,
    UiCallbacks,
    normalize_levels,
)
from micnaga.ui.icons import ICO_PATH
from micnaga.ui.media import LocalServer
from micnaga.ui.toast import ToastNotifier
from micnaga.ui.tray import Tray
from micnaga.ui.windows import PywebviewFactory, UiWindow, WindowFactory, WindowSpec

log = logging.getLogger(__name__)

FLY_SECONDS = 0.4
THEME_POLL_S = 5.0
_BG = {"light": "#f4f3f1", "dark": "#1b1b1d"}


class FileConfigStore:
    """``config.toml`` real (``paths.config_path()``)."""

    def load(self) -> Config:
        return load_config()

    def save(self, config: Config) -> None:
        save_config(config)


@dataclass
class _Pill:
    visible: bool = False
    paused: bool = False
    provisional: bool = False
    base: Rect | None = None
    """Rectangulo compacto (200 x 40 fisicos) actual."""
    user_pos: tuple[int, int] | None = None
    expanded: bool = False
    ignore_moves_until: float = 0.0
    monitor: winapi.MonitorInfo | None = None


class UiHost:
    """Fachada de la UI. Ver el docstring del modulo para el modelo de hilos."""

    def __init__(
        self,
        *,
        config: Config | None = None,
        callbacks: UiCallbacks | None = None,
        levels: LevelsProvider | None = None,
        library_ops: LibraryOps | None = None,
        download_models: DownloadModels | None = None,
        diagnostics: DiagnosticsProvider | None = None,
        config_store: ConfigStore | None = None,
        db_factory: Callable[[], Database] | None = None,
        registry: startup.RegistryBackend | None = None,
        factory: WindowFactory | None = None,
        server: LocalServer | None = None,
        win32: winapi.Win32 | None = None,
        toaster: Any = None,
        backdrop: str = "solid",
        spawn: Callable[[Callable[[], None]], None] | None = None,
        play: Callable[[bytes], None] | None = None,
        tray: bool = True,
        folder_opener: Callable[[Path], None] | None = None,
    ) -> None:
        self._store: ConfigStore = config_store or FileConfigStore()
        self._config: Config = config if config is not None else self._store.load()
        self._cb = callbacks or UiCallbacks()
        self._levels = levels or (lambda: (0.0, 0.0))
        self._ops = library_ops
        self._download = download_models
        self._diagnostics = diagnostics
        self._registry = registry
        self._w32 = win32 or winapi.default_api()
        self._server = server or LocalServer(
            meetings_root=lambda: paths.meetings_root(self._config)
        )
        self._factory: WindowFactory = factory or PywebviewFactory(self._server, self._w32)
        self._db_factory = db_factory or (lambda: open_database(self._config))
        self._backdrop = backdrop
        self._spawn = spawn or _spawn_thread
        self._play = play
        self._want_tray = tray
        self._folder_opener = folder_opener
        self._toast = ToastNotifier(
            self._open_from_toast, language=self._config.ui.language, toaster=toaster
        )

        self._lock = threading.RLock()
        self._popup: UiWindow | None = None
        self._pillwin: UiWindow | None = None
        self._windows: dict[str, UiWindow] = {}
        self._tray: Tray | None = None
        self._stopping = threading.Event()
        self._theme_thread: threading.Thread | None = None
        self._pushed_theme: str = ""
        self._processing = False
        self._on_window_closed: Callable[[str], None] | None = None

        # popup
        self._popup_state = "idle"  # idle | showing | morphing
        self._popup_rect: Rect | None = None
        self._popup_mon: winapi.MonitorInfo | None = None
        self._popup_shown_at = 0.0
        self._popup_on_timeout = "provisional"
        # pastilla
        self._pill = _Pill()
        self._pill_after_morph: tuple[float, bool] | None = None

    # ================================================================= ciclo de vida

    @property
    def config(self) -> Config:
        return self._config

    def run(self, on_ready: Callable[[UiHost], None] | None = None) -> None:
        """Arranca la GUI en el hilo actual (debe ser el principal). Bloquea hasta ``quit()``."""
        winapi.ensure_dpi_aware()
        self._server.start()
        self._create_overlays()
        self._factory.start(lambda: self._bootstrap(on_ready), icon=ICO_PATH)
        self._shutdown_services()

    def quit(self) -> None:
        """Cierra todo: ventanas, bandeja, servidor. Hace que ``run()`` devuelva."""
        self._stopping.set()
        if self._tray is not None:
            self._tray.stop()
        with self._lock:
            wins = list(self._windows.values())
            overlays = [w for w in (self._popup, self._pillwin) if w is not None]
        for w in wins:
            w.destroy()
        for w in overlays:
            w.destroy()

    def _shutdown_services(self) -> None:
        self._stopping.set()
        if self._tray is not None:
            self._tray.stop()
        self._server.stop()

    def _bootstrap(self, on_ready: Callable[[UiHost], None] | None) -> None:
        for win in (self._popup, self._pillwin):
            if win is not None:
                win.wait_loaded(15.0)
                if win.hwnd:
                    winapi.apply_no_activate(win.hwnd, self._w32)
        if self._want_tray:
            self._tray = Tray(self._tray_callbacks(), language=self._config.ui.language)
            self._tray.start()
        self._start_theme_watch()
        if on_ready is not None:
            try:
                on_ready(self)
            except Exception:
                log.exception("on_ready failed")

    def _create_overlays(self) -> None:
        self._popup = self._factory.create(
            self._overlay_spec("popup", "popup.html", "Micnaga", 400, 232, PopupApi(self))
        )
        self._pillwin = self._factory.create(
            self._overlay_spec("pill", "pill.html", "Micnaga ", 200, 40, PillApi(self))
        )
        self._pillwin.on_moved(self._on_pill_moved)

    def _overlay_spec(
        self, key: str, page: str, title: str, w: int, h: int, api: object
    ) -> WindowSpec:
        return WindowSpec(
            key=key,
            page=page,
            title=title,
            width=w,
            height=h,
            params=self._page_params(),
            js_api=api,
            transparent=True,
            frameless=True,
            on_top=True,
            focus=False,
            hidden=True,
            resizable=False,
            min_size=(100, 30),
            overlay=True,
            background="#000000",
        )

    # ================================================================= tema / movimiento

    def _theme(self) -> str:
        return winapi.resolve_theme(self._config.ui.theme, self._w32)

    def _page_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"theme": self._theme(), "lang": self._config.ui.language}
        if not winapi.animations_enabled(self._w32):
            params["motion"] = "off"
        return params

    def apply_config(self, config: Config) -> None:
        """Aplica una configuracion nueva (tema, idioma, recordatorio) a las ventanas abiertas."""
        self._config = config
        self._push_theme(force=True)

    def _push_theme(self, *, force: bool = False) -> None:
        theme = self._theme()
        if theme == self._pushed_theme and not force:
            return
        self._pushed_theme = theme
        motion = "true" if winapi.animations_enabled(self._w32) else "false"
        script = f"window.mic && (mic.setTheme({json.dumps(theme)}), mic.setMotion({motion}))"
        with self._lock:
            wins = [
                *self._windows.values(),
                *[w for w in (self._popup, self._pillwin) if w is not None],
            ]
        for w in wins:
            w.run_js(script)

    def _start_theme_watch(self) -> None:
        self._pushed_theme = self._theme()

        def loop() -> None:
            while not self._stopping.wait(THEME_POLL_S):
                try:
                    self._push_theme()
                except Exception:
                    log.debug("theme poll failed", exc_info=True)

        self._theme_thread = threading.Thread(target=loop, name="micnaga-ui-theme", daemon=True)
        self._theme_thread.start()

    # ================================================================= niveles

    def levels(self) -> tuple[float, float]:
        try:
            return normalize_levels(self._levels())
        except Exception:
            log.debug("levels provider failed", exc_info=True)
            return (0.0, 0.0)

    def set_levels_provider(self, provider: LevelsProvider) -> None:
        self._levels = provider

    # ================================================================= popup

    def show_popup(
        self,
        title: str,
        code: str | None = None,
        monitor_hint: MonitorHint = None,
        *,
        timeout_s: float | None = None,
        on_timeout: str | None = None,
    ) -> None:
        """Muestra «¿Estas en una reu?» en el monitor del navegador (``monitor_hint``)."""
        win = self._popup
        if win is None:
            raise RuntimeError("UiHost.run() has not created the windows yet")
        cfg = self._config
        mon = self._w32.monitor_info(monitor_hint)
        rect = winapi.popup_rect(mon)
        payload = {
            "title": title,
            "code": code or "",
            "timeoutS": timeout_s if timeout_s is not None else cfg.detection.popup_timeout_s,
            "onTimeout": on_timeout or cfg.detection.on_timeout,
            "remindNotice": cfg.privacy.remind_notice,
        }
        with self._lock:
            self._popup_state = "showing"
            self._popup_rect, self._popup_mon = rect, mon
            self._popup_shown_at = time.monotonic()
            self._popup_on_timeout = str(payload["onTimeout"])
            self._pill_after_morph = None
        win.place(rect)
        win.run_js(f"mic.setMotion({'true' if winapi.animations_enabled(self._w32) else 'false'});")
        win.run_js(f"micnaga.popup.show({json.dumps(payload)})")
        win.show(activate=False)
        sound.play_chime(
            enabled=cfg.ui.sound, dnd=lambda: winapi.do_not_disturb(self._w32), player=self._play
        )

    def hide_popup(self, reason: str = "ended") -> None:
        """La reunion acabo antes de responder: la tarjeta se desvanece (180 ms)."""
        win = self._popup
        with self._lock:
            if win is None or self._popup_state != "showing":
                return
        win.run_js("micnaga.popup.end()")
        # red de seguridad: si el JS no contesta, se oculta igualmente
        threading.Timer(0.6, lambda: self.handle_popup_closed("fading")).start()

    def accept_popup(self) -> None:
        """Como pulsar «Grabar» (para atajos globales)."""
        if self._popup is not None and self._popup_state == "showing":
            self._popup.run_js("micnaga.popup.accept()")

    def dismiss_popup(self) -> None:
        """Como pulsar «Ahora no»."""
        if self._popup is not None and self._popup_state == "showing":
            self._popup.run_js("micnaga.popup.dismiss()")

    @property
    def popup_visible(self) -> bool:
        return self._popup_state != "idle"

    # -- eventos del JS (PopupApi)
    def handle_popup_decide(self, decision: str) -> None:
        if decision == "record":
            self._pill.provisional = False
        elif decision == "timeout" and self._popup_on_timeout == "provisional":
            self._pill.provisional = True
        self._cb.on_popup_decision(decision)  # type: ignore[arg-type]

    def handle_popup_hover(self, hovering: bool) -> None:
        self._cb.on_popup_hover(hovering)

    def handle_notice_dismissed(self) -> None:
        """Ocultar el recordatorio es permanente (``privacy.remind_notice = false``)."""
        try:
            cfg = self._store.load()
            cfg.privacy.remind_notice = False
            self._store.save(cfg)
            self._config.privacy.remind_notice = False
        except Exception:
            log.exception("cannot persist remind_notice")

    def handle_popup_activate(self) -> None:
        win = self._popup
        if win is not None and win.hwnd:
            winapi.clear_no_activate(win.hwnd, self._w32)
            self._w32.set_foreground(win.hwnd)

    def handle_popup_morph(self, provisional: bool) -> None:
        with self._lock:
            self._popup_state = "morphing"
            self._pill.provisional = provisional
        self._spawn(self._fly_to_pill)

    def handle_popup_closed(self, kind: str) -> None:
        win = self._popup
        with self._lock:
            if self._popup_state == "idle":
                return
            self._popup_state = "idle"
        if win is not None:
            win.hide()
            if win.hwnd:
                winapi.apply_no_activate(win.hwnd, self._w32)  # por si se activo con un clic
        self._cb.on_popup_closed(kind)

    def _fly_to_pill(self) -> None:
        """Vuelo del popup (ventana) hasta el hueco de la pastilla, 400 ms."""
        win = self._popup
        start = self._popup_rect
        mon = self._popup_mon
        if win is None or start is None or mon is None:
            return
        target = self._pill_target(mon)
        # la tarjeta se encoge hacia arriba-centro de la ventana (CSS): se alinea con la pastilla
        end = (target[0] - (start[2] - target[2]) // 2, target[1], start[2], start[3])
        duration = FLY_SECONDS if winapi.animations_enabled(self._w32) else 0.0
        if win.hwnd:
            winapi.animate_rect(win.hwnd, start, end, duration, self._w32)
        with self._lock:
            pending = self._pill_after_morph
            self._pill_after_morph = None
        elapsed = pending[0] if pending else max(0.0, time.monotonic() - self._popup_shown_at)
        provisional = pending[1] if pending else self._pill.provisional
        self._show_pill(elapsed, provisional, mon)

    # ================================================================= pastilla

    def show_pill(
        self,
        *,
        elapsed_s: float = 0.0,
        provisional: bool = False,
        monitor_hint: MonitorHint = None,
    ) -> None:
        """Muestra la pastilla «Grabando» (si el popup vuela hacia ella, espera al aterrizaje)."""
        with self._lock:
            if self._popup_state == "morphing":
                self._pill_after_morph = (elapsed_s, provisional)
                return
        mon = (
            self._w32.monitor_info(monitor_hint)
            if monitor_hint is not None or self._pill.monitor is None
            else self._pill.monitor
        )
        self._show_pill(elapsed_s, provisional, mon)

    def _pill_target(self, mon: winapi.MonitorInfo) -> Rect:
        pill = self._pill
        if pill.user_pos is not None:
            x, y = pill.user_pos
            w, h = (round(v * mon.scale) for v in winapi.PILL_SIZE)
            return winapi.clamp_to_work_area((x, y, w, h), mon.work)
        return winapi.pill_rect(mon)

    def _show_pill(self, elapsed: float, provisional: bool, mon: winapi.MonitorInfo) -> None:
        win = self._pillwin
        if win is None:
            return
        rect = self._pill_target(mon)
        payload = {"elapsed": elapsed, "provisional": provisional, "paused": self._pill.paused}
        with self._lock:
            pill = self._pill
            pill.visible, pill.provisional, pill.monitor = True, provisional, mon
            pill.base, pill.expanded = rect, False
            pill.ignore_moves_until = time.monotonic() + 0.4
        win.place(rect)
        win.run_js(f"micnaga.pill.show({json.dumps(payload)})")
        win.show(activate=False)
        self._update_tray()

    def hide_pill(self) -> None:
        win = self._pillwin
        with self._lock:
            self._pill.visible = False
            self._pill.paused = False
            self._pill_after_morph = None
        if win is not None:
            win.run_js("micnaga.pill.stop()")
            win.hide()
        self._update_tray()

    def set_pill_paused(self, paused: bool) -> None:
        with self._lock:
            self._pill.paused = paused
        if self._pillwin is not None:
            self._pillwin.run_js(f"micnaga.pill.setPaused({json.dumps(paused)})")
        self._update_tray()

    def set_pill_provisional(self, provisional: bool) -> None:
        with self._lock:
            self._pill.provisional = provisional
        if self._pillwin is not None:
            self._pillwin.run_js(f"micnaga.pill.setProvisional({json.dumps(provisional)})")

    def sync_pill(self, elapsed_s: float) -> None:
        """Corrige el cronometro (tiempo efectivo de grabacion, sin pausas) si deriva."""
        if self._pillwin is not None:
            self._pillwin.run_js(f"micnaga.pill.sync({elapsed_s:.2f})")

    @property
    def pill_visible(self) -> bool:
        return self._pill.visible

    # -- eventos del JS (PillApi)
    def handle_pill_hover(self, hovering: bool) -> None:
        win = self._pillwin
        with self._lock:
            pill = self._pill
            if win is None or pill.base is None or not pill.visible or pill.expanded == hovering:
                return
            x, y, w, h = pill.base
            scale = pill.monitor.scale if pill.monitor else 1.0
            if hovering:
                wide = round(winapi.PILL_HOVER_WIDTH * scale)
                rect: Rect = (x - (wide - w) // 2, y, wide, h)
            else:
                rect = pill.base
            pill.expanded = hovering
            pill.ignore_moves_until = time.monotonic() + 0.3
        win.place(rect)

    def handle_pill_marker(self) -> None:
        self._cb.on_marker()

    def handle_pill_toggle_pause(self) -> None:
        with self._lock:
            paused = not self._pill.paused
        try:
            self._cb.on_pause_toggle(paused)
        except Exception:
            log.exception("on_pause_toggle failed")
            return
        self.set_pill_paused(paused)

    def handle_pill_stop(self) -> None:
        self._cb.on_stop()

    def _on_pill_moved(self, x: int, y: int) -> None:
        with self._lock:
            pill = self._pill
            if not pill.visible or pill.base is None or time.monotonic() < pill.ignore_moves_until:
                return
            scale = pill.monitor.scale if pill.monitor else 1.0
            if pill.expanded:
                x += (round(winapi.PILL_HOVER_WIDTH * scale) - pill.base[2]) // 2
            pill.user_pos = (x, y)
            pill.base = (x, y, pill.base[2], pill.base[3])
        self._cb.on_pill_moved(x, y)

    # ================================================================= bandeja y avisos

    def _tray_callbacks(self) -> UiCallbacks:
        cb = self._cb
        return UiCallbacks(
            on_record_now=cb.on_record_now,
            on_pause_watching=cb.on_pause_watching,
            on_resume_watching=cb.on_resume_watching,
            on_open_library=self._tray_open_library,
            on_open_settings=self._tray_open_settings,
            on_quit=self._tray_quit,
        )

    def _tray_open_library(self) -> None:
        self.open_library()
        self._cb.on_open_library()

    def _tray_open_settings(self) -> None:
        self.open_settings()
        self._cb.on_open_settings()

    def _tray_quit(self) -> None:
        self._cb.on_quit()  # primero el integrador (parar captura/vigilante), luego la GUI
        self.quit()

    def set_processing(self, processing: bool) -> None:
        """Azul en la bandeja mientras el worker procesa."""
        self._processing = processing
        self._update_tray()

    def tray_state(self) -> TrayState:
        if self._pill.visible:
            return "paused" if self._pill.paused else "recording"
        return "processing" if self._processing else "watching"

    def _update_tray(self) -> None:
        if self._tray is not None:
            self._tray.set_state(self.tray_state())

    def set_watching_paused(self, paused: bool) -> None:
        if self._tray is not None:
            self._tray.set_watching_paused(paused)

    def notify_ready(self, folder: str, title: str, duration_s: float, speakers: int) -> None:
        """Toast «Lista»: «Revision semanal · 47 min · 3 hablantes» con boton «Abrir»."""
        self._toast.show_ready(folder, title, duration_s, speakers)

    def _open_from_toast(self, folder: str) -> None:
        self.open_library(folder=folder)
        self._cb.on_open_meeting(folder)

    # ================================================================= ventanas normales

    def _open_normal(self, spec: WindowSpec, *, js: str | None = None) -> UiWindow:
        """Crea o reutiliza una ventana normal. Si ya existe, la trae al frente."""
        with self._lock:
            existing = self._windows.get(spec.key)
        if existing is not None:
            existing.show(activate=True)
            if js:
                existing.run_js(js)
            return existing
        win = self._factory.create(spec)
        with self._lock:
            self._windows[spec.key] = win

        def closed() -> None:
            with self._lock:
                if self._windows.get(spec.key) is win:
                    del self._windows[spec.key]
            if self._on_window_closed is not None:
                self._on_window_closed(spec.key)

        win.on_closed(closed)
        if self._backdrop == "mica":
            self._spawn(lambda: self._apply_backdrop(win))
        return win

    def _apply_backdrop(self, win: UiWindow) -> None:
        win.wait_loaded(10.0)
        if win.hwnd:
            winapi.set_backdrop(win.hwnd, "mica", dark=self._theme() == "dark", api=self._w32)

    def _normal_spec(
        self,
        key: str,
        page: str,
        title: str,
        w: int,
        h: int,
        api: object,
        params: dict[str, Any] | None = None,
        **kw: Any,
    ) -> WindowSpec:
        merged = {**self._page_params(), **(params or {})}
        if self._backdrop == "mica":
            merged["backdrop"] = "mica"
        return WindowSpec(
            key=key,
            page=page,
            title=title,
            width=w,
            height=h,
            params=merged,
            js_api=api,
            background=_BG[self._theme()],
            **kw,
        )

    def library_api(self) -> LibraryApi:
        return LibraryApi(
            db_factory=self._db_factory,
            meetings_root=lambda: paths.meetings_root(self._config),
            audio_url=self._server.audio_url,
            config=lambda: self._config,
            ops=self._ops,
            opener=self._folder_opener,
        )

    def open_library(self, folder: str | None = None, seek_s: float | None = None) -> None:
        """Abre la biblioteca (opcionalmente directamente en una reunion y un instante)."""
        t = i18n.t("library.window_title", self._config.ui.language)
        params: dict[str, Any] = {}
        if folder:
            params["folder"] = folder
            if seek_s is not None:
                params["t"] = f"{seek_s:.2f}"
        spec = self._normal_spec(
            "library", "library.html", t, 1120, 720, self.library_api(), params, min_size=(820, 540)
        )
        js = (
            "window.micnaga && micnaga.library && "
            f"micnaga.library.open({json.dumps(folder)}, {json.dumps(seek_s)})"
            if folder
            else None
        )
        self._open_normal(spec, js=js)

    def settings_api(self) -> SettingsApi:
        return SettingsApi(
            store=self._store,
            callbacks=self._cb,
            registry=self._registry,
            diagnostics=self._diagnostics,
            ops=self._ops,
            on_applied=self.apply_config,
            folder_picker=self._pick_folder,
        )

    def open_settings(self) -> None:
        t = i18n.t("settings.window_title", self._config.ui.language)
        spec = self._normal_spec(
            "settings", "settings.html", t, 800, 760, self.settings_api(), min_size=(640, 520)
        )
        self._open_normal(spec)

    def open_wizard(self) -> None:
        """Asistente de primer arranque (3 pasos)."""
        t = i18n.t("wizard.window_title", self._config.ui.language)
        api = WizardApi(
            download_models=self._download,
            levels=self._levels,
            callbacks=UiCallbacks(
                on_levels_monitor=self._cb.on_levels_monitor,
                on_first_run_done=self._wizard_done,
            ),
        )
        spec = self._normal_spec(
            "wizard", "wizard.html", t, 600, 680, api, resizable=False, min_size=(520, 560)
        )
        self._open_normal(spec)

    def _wizard_done(self) -> None:
        self._cb.on_first_run_done()
        self.close_window("wizard")

    def close_window(self, key: str) -> None:
        with self._lock:
            win = self._windows.get(key)
        if win is not None:
            win.destroy()

    def _pick_folder(self, current: str) -> str | None:
        with self._lock:
            win = self._windows.get("settings")
        return win.pick_folder(current) if win is not None else None

    def set_on_window_closed(self, callback: Callable[[str], None] | None) -> None:
        """Aviso al cerrarse una ventana normal (``library`` | ``settings`` | ``wizard``)."""
        self._on_window_closed = callback

    def has_window(self, key: str) -> bool:
        return key in self._windows


def _spawn_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="micnaga-ui-task", daemon=True).start()
