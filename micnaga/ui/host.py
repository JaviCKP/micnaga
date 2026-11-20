"""``UiHost``: orquesta ventanas, bandeja y notificaciones. Es lo que usa el integrador.

Modelo de hilos
---------------
* ``UiHost.run()`` **ocupa el hilo principal** (pywebview/WinForms lo exigen) hasta ``quit()``.
* Justo despues de arrancar el bucle, pywebview lanza ``_bootstrap`` en un hilo auxiliar: ahi se
  retocan los estilos Win32, se arranca la bandeja (``pystray.Icon.run_detached``: hilo propio) y
  se llama a ``on_ready(host)`` -> el integrador arranca ahi vigilante, captura y cola.
* Una ventana oculta, la **superficie** (pastilla <-> tarjeta), se crea antes del bucle y se
  **reutiliza** (mostrar/ocultar): evita la latencia de crear un WebView2 (<3 s hasta el popup)
  y mantiene vivo el bucle (pywebview sale al cerrarse la ultima ventana).

Superficie unica
----------------
La tarjeta «¿Estás en una reu?» (y la de «Reunión terminada») nace de la pastilla: es la MISMA
forma, que crece en CSS (ancho, alto, posicion) dentro de una ventana transparente de tamano
fijo (``winapi.STAGE_SIZE``) y vuelve a encogerse. La ventana nunca se redimensiona durante la
animacion: solo cambia su region (``SetWindowRgn``), que recorta lo que se ve y se pulsa, de modo
que el hueco transparente deja pasar los clics. Fases (``surface_phase``):
``hidden -> pill -> expanding -> card -> collapsing -> pill`` (o ``undo`` / ``hidden``).
* Biblioteca, Ajustes y asistente se crean bajo demanda y se destruyen al cerrarse.
* Toda la API publica es segura desde cualquier hilo. Los callbacks de ``UiCallbacks`` se
  invocan desde hilos de pywebview o de la bandeja, nunca desde el hilo del vigilante.

Popup y pastilla son ``WS_EX_NOACTIVATE``: nunca roban el foco a Meet. Consecuencia: Enter/Esc
solo llegan al popup si tiene el foco. Se resuelve asi: (1) clic en el cuerpo de la tarjeta lo
activa (``popup_activate``, accion deliberada); (2) ``accept_popup()`` / ``dismiss_popup()``
para que el integrador los ate a atajos globales si quiere teclado sin ratón.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import sys
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
from micnaga.ui.bridge import LibraryApi, SettingsApi, SurfaceApi, WizardApi
from micnaga.ui.contracts import (
    BrainPort,
    ConfigStore,
    DiagnosticsProvider,
    DownloadModels,
    HeldOps,
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
from micnaga.ui.toast import ToastNotifier, default_aumid
from micnaga.ui.tray import Tray
from micnaga.ui.windows import PywebviewFactory, UiWindow, WindowFactory, WindowSpec

log = logging.getLogger(__name__)

POPUP_VERIFY_S = 1.2
"""Tras mostrar un popup se comprueba que la ventana sigue visible; si no, se reintenta."""
COLLAPSE_GUARD_S = 1.4
"""Red de seguridad: si el JS no avisa del fin del encogido, el host lo da por terminado."""
VANISH_S = 0.2
"""La pastilla se desvanece (160 ms) antes de ocultar la ventana: al volver a mostrarla nunca
asoma el ultimo fotograma viejo."""
UNDO_SIZE = (winapi.UNDO_WIDTH, 40)
MAX_GUI_RESTARTS = 5
"""Si el bucle de la GUI termina sin «Salir» se recrean las ventanas (como mucho tantas veces
seguidas en ``GUI_RESTART_WINDOW_S``; despues se sale, registrado)."""
GUI_RESTART_WINDOW_S = 600.0
SM_SHUTTINGDOWN = 0x2000
THEME_POLL_S = 5.0
_BG = {"light": "#f3f3f0", "dark": "#050507"}
ENV_ACRYLIC = "MICNAGA_UI_ACRYLIC"


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
    title: str | None = None
    """Titulo provisional de la Secretaria (la pastilla se ensancha mas al pasar el raton)."""
    stage: Rect | None = None
    """Rectangulo (fisico) de la ventana-lienzo que contiene la pastilla y la tarjeta."""
    layout: dict[str, float] | None = None
    """Posiciones CSS dentro del lienzo (``winapi.stage_layout``)."""
    shown: bool = False
    """La superficie esta en pantalla (pastilla, tarjeta o aviso «Deshacer»)."""


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
        acrylic: bool | None = None,
        held_ops: HeldOps | None = None,
        brain: BrainPort | None = None,
    ) -> None:
        self._brain = brain
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
        self._acrylic_pref = acrylic
        self._acrylic = False
        """Acrilico real de DWM activo en popup y pastilla (decidido en ``_bootstrap``)."""
        self._spawn = spawn or _spawn_thread
        self._play = play
        self._want_tray = tray
        self._folder_opener = folder_opener
        self._held_ops = held_ops
        self._toast = ToastNotifier(
            self._open_from_toast,
            language=self._config.ui.language,
            aumid=default_aumid(),
            toaster=toaster,
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
        self._popup_leaving = False  # respondido / retirado: la comprobacion no lo re-presenta
        self._popup_on_timeout = "provisional"
        self._popup_total_s = 15.0
        self._popup_state_provider: Callable[[], tuple[float, bool] | None] | None = None
        self._popup_mode = "ask"  # ask | end
        self._popup_script = ""
        self._surface_phase = "hidden"  # hidden | pill | expanding | card | collapsing | undo
        self._undo_visible = False
        self._undo_seconds = 0.0
        self._vanish_gen = 0
        # salida
        self._quit_reason: str | None = None
        self._loop_end_notes: list[str] = []
        self._restarts: list[float] = []
        self._pill_started = 0.0  # monotonic - segundos grabados (para recrear la pastilla)
        self.exit_reason: str = ""
        # pastilla
        self._pill = _Pill()

    # ================================================================= ciclo de vida

    @property
    def config(self) -> Config:
        return self._config

    def run(self, on_ready: Callable[[UiHost], None] | None = None) -> None:
        """Arranca la GUI en el hilo actual (debe ser el principal). Bloquea hasta ``quit()``.

        La app solo debe salir por «Salir» (``quit``), el fin de sesion de Windows o el
        instalador. Si el bucle de pywebview termina por otra cosa (se cerraron las ventanas
        ocultas, Ctrl+C en la consola, el proceso de WebView2 cayo...), se registra el motivo y
        se recrean las ventanas sin tocar la grabacion en curso (ver ``_loop_ended``).
        """
        winapi.ensure_dpi_aware()
        self._server.start()
        first = True
        while True:
            self._create_overlays()
            try:
                self._factory.start(
                    functools.partial(self._bootstrap, on_ready if first else None, first=first),
                    icon=ICO_PATH,
                )
            except Exception as exc:
                if first:
                    raise
                # recrear la GUI fallo: se sale, con el motivo en el log (no hay ventanas)
                log.exception("could not restart the GUI loop")
                self.exit_reason = f"gui_restart_failed ({exc.__class__.__name__})"
                break
            if not self._loop_ended():
                break
            first = False
        self._shutdown_services()

    def _loop_ended(self) -> bool:
        """El bucle de la GUI termino. ``True`` = recrear las ventanas y seguir."""
        if self._stopping.is_set():
            self.exit_reason = self._quit_reason or "quit"
            log.info("GUI loop ended: %s", self.exit_reason)
            return False
        notes = self._loop_end_notes or ["all windows closed or GUI thread exited"]
        if _sigint_pending():
            notes.append("console Ctrl+C (SIGINT) reached pywebview")
        reason = "; ".join(notes)
        self._loop_end_notes = []
        if self._session_ending():
            self.exit_reason = f"session_end ({reason})"
            log.info("GUI loop ended by the end of the Windows session: %s", reason)
            return False
        now = time.monotonic()
        self._restarts = [t for t in self._restarts if now - t < GUI_RESTART_WINDOW_S]
        if len(self._restarts) >= MAX_GUI_RESTARTS:
            self.exit_reason = f"gui_loop_failed ({reason})"
            log.error("GUI loop keeps ending (%s); giving up and exiting", reason)
            return False
        self._restarts.append(now)
        log.error("GUI loop ended without «Salir» (%s); recreating the windows", reason)
        _reset_pywebview_after_loop()
        with self._lock:
            # el estado (popup abierto, pastilla visible) se conserva: ``_restore_surfaces``
            # lo vuelve a pintar en las ventanas nuevas
            self._popup = self._pillwin = None
            self._windows.clear()
            if self._popup_state == "morphing":
                self._popup_state = "idle"
            self._pill.shown = False
            self._pill.stage = None
            self._surface_phase = "hidden"
        return True

    def _session_ending(self) -> bool:
        try:
            import ctypes

            return bool(ctypes.windll.user32.GetSystemMetrics(SM_SHUTTINGDOWN))
        except Exception:
            return False

    def quit(self, reason: str = "quit") -> None:
        """Cierra todo: ventanas, bandeja, servidor. Hace que ``run()`` devuelva."""
        if self._quit_reason is None:
            self._quit_reason = reason
        self._stopping.set()
        if self._tray is not None:
            self._tray.stop()
        with self._lock:
            wins = list(self._windows.values())
        overlays = self._overlays()
        for w in wins:
            w.destroy()
        for w in overlays:
            w.destroy()

    def _shutdown_services(self) -> None:
        self._stopping.set()
        if self._tray is not None:
            self._tray.stop()
        self._server.stop()

    def _overlays(self) -> list[UiWindow]:
        """Las ventanas superpuestas, sin repetir (popup y pastilla son la misma superficie)."""
        out: list[UiWindow] = []
        for w in (self._popup, self._pillwin):
            if w is not None and all(w is not o for o in out):
                out.append(w)
        return out

    def _bootstrap(self, on_ready: Callable[[UiHost], None] | None, *, first: bool = True) -> None:
        for win in self._overlays():
            if win is not None:
                win.wait_loaded(15.0)
                if win.hwnd:
                    winapi.apply_no_activate(win.hwnd, self._w32)
                    win.make_transparent()
                # pywebview (edgechromium) hace ``form.Show()`` en cada navegacion de una ventana
                # transparente aunque se creara oculta: sin esto, al arrancar quedaban una tarjeta
                # vacia y una pastilla «00:00» visibles en la esquina superior izquierda.
                if not self._overlay_in_use(win):
                    win.hide()
        self._setup_acrylic()
        if not first:
            self._restore_surfaces()
            return
        if self._want_tray:
            self._tray = Tray(
                self._tray_callbacks(),
                language=self._config.ui.language,
                timesheets=self._timesheets_available,
            )
            self._tray.start()
        self._start_theme_watch()
        if on_ready is not None:
            try:
                on_ready(self)
            except Exception:
                log.exception("on_ready failed")

    def _restore_surfaces(self) -> None:
        """Tras recrear las ventanas: la pastilla de una grabacion en curso vuelve a su sitio
        (el cronometro lo corrige el controlador en <= 30 s); un popup abierto se repinta."""
        with self._lock:
            showing = self._popup_state == "showing"
        if self._pill.visible and self._pill.monitor is not None and not showing:
            elapsed = max(0.0, time.monotonic() - self._pill_started)
            self._show_pill(elapsed, self._pill.provisional, self._pill.monitor)
        win, mon = self._popup, self._popup_mon
        if win is not None and mon is not None and self._popup_script and showing:
            self._present_popup(win, mon)

    def _note_overlay_closed(self, key: str) -> None:
        if not self._stopping.is_set():
            self._loop_end_notes.append(f"overlay window {key!r} was closed")
            log.warning("overlay window %s closed while running", key)

    def _overlay_in_use(self, win: UiWindow) -> bool:
        with self._lock:
            used = False
            if win is self._popup:
                used = used or self._popup_state != "idle"
            if win is self._pillwin:
                used = used or self._pill.visible or self._undo_visible
        return used

    def _want_acrylic(self) -> bool:
        if self._acrylic_pref is not None:
            return self._acrylic_pref
        env = os.environ.get(ENV_ACRYLIC, "").strip().lower()
        if env in ("0", "false", "no", "off"):
            return False
        return winapi.acrylic_supported(self._w32)

    def _setup_acrylic(self) -> None:
        """Sin acrilico DWM en la superficie: la ventana es un lienzo mayor que la forma visible
        (pastilla o tarjeta, pintadas opacas por la pagina, §25) y el acrilico pintaria el
        lienzo entero. La forma la dibuja siempre el CSS y la recorta la region de la ventana."""
        self._acrylic = False

    @property
    def acrylic(self) -> bool:
        return self._acrylic

    def _create_overlays(self) -> None:
        """Una sola ventana: la pastilla y la tarjeta que nace de ella (``pill.html``)."""
        stage_w, stage_h = winapi.STAGE_SIZE
        surface = self._factory.create(
            self._overlay_spec("pill", "pill.html", "Micnaga ", stage_w, stage_h, SurfaceApi(self))
        )
        self._popup = self._pillwin = surface
        surface.on_moved(self._on_pill_moved)
        surface.on_closed(lambda: self._note_overlay_closed("pill"))

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
        if self._acrylic and self._popup is not None and self._popup.hwnd:
            winapi.set_backdrop(self._popup.hwnd, "acrylic", dark=theme == "dark", api=self._w32)
        with self._lock:
            wins = [*self._windows.values(), *self._overlays()]
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

    def set_popup_state_provider(
        self, provider: Callable[[], tuple[float, bool] | None] | None
    ) -> None:
        """``provider() -> (segundos restantes, congelada)`` o ``None`` sin popup.

        La cuenta atras del popup es UNA, la del vigilante: la UI solo la pinta (``popup_state``).
        """
        self._popup_state_provider = provider

    def popup_state(self) -> dict[str, Any]:
        """Lo que el JS del popup consulta para dibujar la barra (``PopupApi.popup_state``)."""
        total = self._popup_total_s
        provider = self._popup_state_provider
        value = provider() if provider is not None else None
        authoritative = provider is not None
        if value is None:
            return {
                "remaining_s": total,
                "total_s": total,
                "paused": False,
                "authoritative": authoritative,
            }
        remaining, frozen = value
        return {
            "remaining_s": max(0.0, float(remaining)),
            "total_s": total,
            "paused": bool(frozen),
            "authoritative": authoritative,
        }

    # ================================================================= superficie (geometria)

    @property
    def surface_phase(self) -> str:
        """``hidden`` | ``pill`` | ``expanding`` | ``card`` | ``collapsing`` | ``undo``."""
        return self._surface_phase

    def _card_busy(self) -> bool:
        """La tarjeta esta abierta o encogiendose: la pastilla vive dentro de ella."""
        return self._popup_state != "idle"

    def _layout_stage(self, mon: winapi.MonitorInfo, pill_rect: Rect) -> dict[str, float]:
        """Coloca el lienzo alrededor de la pastilla y le dice al JS donde va cada forma."""
        win = self._pillwin
        stage = winapi.stage_rect(pill_rect, mon)
        layout = winapi.stage_layout(pill_rect, stage, mon.scale)
        with self._lock:
            pill = self._pill
            moved = stage != pill.stage
            pill.base, pill.stage, pill.layout, pill.monitor = pill_rect, stage, layout, mon
            if moved:
                pill.ignore_moves_until = time.monotonic() + 0.4
        if win is not None:
            win.run_js(f"micnaga.pill.layout({json.dumps(layout)})")
            if moved or not pill.shown:
                win.place(stage)
        return layout

    def _set_region(self, kind: str) -> None:
        """Zona visible y clicable del lienzo: ``pill`` | ``hover`` | ``undo`` | ``card``."""
        win = self._pillwin
        pill = self._pill
        lay, mon = pill.layout, pill.monitor
        if win is None or lay is None:
            return
        scale = mon.scale if mon else 1.0
        if kind == "card":
            box = (lay["cardL"], 0.0, *winapi.CARD_SIZE)
        else:
            if kind == "hover":
                width = float(_hover_width(pill))
            elif kind == "undo":
                width = float(UNDO_SIZE[0])
            else:
                width = float(winapi.PILL_SIZE[0])
            box = (lay["px"] - width / 2, lay["py"], width, float(winapi.PILL_SIZE[1]))
        win.set_region(winapi.css_region(box, scale))

    def _vanish(self) -> None:
        """La pastilla se desvanece y despues se oculta la ventana (si nadie la reclama)."""
        win = self._pillwin
        if win is None:
            return
        with self._lock:
            self._vanish_gen += 1
            gen = self._vanish_gen
            self._surface_phase = "hidden"
        win.run_js("micnaga.pill.vanish()")
        delay = VANISH_S if winapi.animations_enabled(self._w32) else 0.0

        def done() -> None:
            with self._lock:
                if gen != self._vanish_gen or self._surface_phase != "hidden":
                    return
                self._pill.shown = False
            win.hide()

        if delay <= 0:
            done()
        else:
            timer = threading.Timer(delay, done)
            timer.daemon = True
            timer.start()

    def _cancel_vanish(self) -> None:
        with self._lock:
            self._vanish_gen += 1

    # ================================================================= popup

    def show_popup(
        self,
        title: str,
        code: str | None = None,
        monitor_hint: MonitorHint = None,
        *,
        timeout_s: float | None = None,
        on_timeout: str | None = None,
        countdown_total_s: float | None = None,
    ) -> None:
        """«¿Estas en una reu?»: la pastilla crece hasta la tarjeta (nace de ella).

        Si aun no hay pastilla (ruta rapida de Meet) aparece primero en su sitio, en el monitor
        del navegador (``monitor_hint``), y crece. ``timeout_s`` es la cuenta atras *propia* del
        JS; con vigilante se pasa un valor enorme y la barra se dibuja con ``popup_state()``
        (``countdown_total_s`` = total real del vigilante).
        """
        win = self._popup
        if win is None:
            raise RuntimeError("UiHost.run() has not created the windows yet")
        cfg = self._config
        mon = self._surface_monitor(monitor_hint)
        timeout_value = timeout_s if timeout_s is not None else cfg.detection.popup_timeout_s
        payload = {
            "title": title,
            "code": code or "",
            "timeoutS": timeout_value,
            "onTimeout": on_timeout or cfg.detection.on_timeout,
            "remindNotice": cfg.privacy.remind_notice,
        }
        with self._lock:
            self._popup_state = "showing"
            self._popup_mon = mon
            self._popup_shown_at = time.monotonic()
            self._popup_leaving = False
            self._popup_on_timeout = str(payload["onTimeout"])
            self._popup_total_s = float(
                countdown_total_s if countdown_total_s is not None else timeout_value
            )
            self._popup_mode = "ask"
            self._popup_script = f"micnaga.popup.show({json.dumps(payload)})"
        self._present_popup(win, mon)
        log.info("popup shown from the pill at %s", self._popup_rect)
        sound.play_chime(
            enabled=cfg.ui.sound, dnd=lambda: winapi.do_not_disturb(self._w32), player=self._play
        )

    def _surface_monitor(self, monitor_hint: MonitorHint) -> winapi.MonitorInfo:
        """La tarjeta nace donde esta la pastilla; sin pastilla, en el monitor del navegador."""
        with self._lock:
            mon = self._pill.monitor if self._pill.shown else None
        return mon if mon is not None else self._w32.monitor_info(monitor_hint)

    def _present_popup(self, win: UiWindow, mon: winapi.MonitorInfo) -> None:
        """Pastilla (si aun no estaba) -> region de la tarjeta -> el JS la hace crecer."""
        self._cancel_vanish()
        with self._lock:
            born = not self._pill.shown or self._surface_phase == "hidden"
            paused = self._pill.paused
            elapsed = (
                max(0.0, time.monotonic() - self._pill_started)
                if self._pill.visible
                else max(0.0, time.monotonic() - self._popup_shown_at)
            )
            self._pill.expanded = False
            self._surface_phase = "expanding"
        layout = self._layout_stage(mon, self._pill_target(mon))
        self._popup_rect = self._pill.stage
        motion = "true" if winapi.animations_enabled(self._w32) else "false"
        win.run_js(f"mic.setMotion({motion});")
        if born:
            pill_payload = {
                "elapsed": elapsed,
                "provisional": True,
                "paused": paused,
                "layout": layout,
                "enter": True,
            }
            win.run_js(f"micnaga.pill.show({json.dumps(pill_payload)})")
        self._set_region("card")
        # la pastilla recien nacida se deja ver un instante antes de crecer (lo decide el JS)
        win.run_js(self._popup_script)
        win.show(activate=False)
        with self._lock:
            self._pill.shown = True
        self._arm_popup_check(self._popup_shown_at)

    def _arm_popup_check(self, shown_at: float) -> None:
        timer = threading.Timer(POPUP_VERIFY_S, lambda: self._verify_popup(shown_at))
        timer.daemon = True
        timer.start()

    def _verify_popup(self, shown_at: float, *, retry: bool = True) -> bool:
        """El popup debe estar en pantalla: si la ventana no es visible (o no esta donde se
        puso) se vuelve a colocar, a pintar y a mostrar, y se deja constancia en el log."""
        win = self._popup
        with self._lock:
            if (
                win is None
                or self._popup_state != "showing"
                or self._popup_shown_at != shown_at
                or self._popup_leaving
                or self._stopping.is_set()
            ):
                return True
            rect = self._popup_rect
        visible = win.is_visible()
        if visible:
            return True
        log.warning("popup not visible %.1f s after show; re-presenting it", POPUP_VERIFY_S)
        if retry and rect is not None:
            win.place(rect)
            self._set_region("card")
            win.run_js(self._popup_script)
            win.show(activate=False)
            self._arm_popup_check_once(shown_at)
        return False

    def _arm_popup_check_once(self, shown_at: float) -> None:
        timer = threading.Timer(POPUP_VERIFY_S, lambda: self._verify_popup(shown_at, retry=False))
        timer.daemon = True
        timer.start()

    def _arm_collapse_guard(self, kind: str) -> None:
        """Si el JS no avisa del final del encogido, el host lo cierra igualmente."""
        with self._lock:
            shown_at = self._popup_shown_at

        def guard() -> None:
            with self._lock:
                stale = self._popup_state == "idle" or self._popup_shown_at != shown_at
            if not stale:
                self.handle_popup_closed(kind)

        timer = threading.Timer(COLLAPSE_GUARD_S, guard)
        timer.daemon = True
        timer.start()

    # ================================================================= popup de fin

    def show_end_popup(
        self,
        title: str,
        *,
        duration_s: float,
        confirmed: bool,
        monitor_hint: MonitorHint = None,
        countdown_total_s: float = 10.0,
    ) -> None:
        """«Reunión terminada» (Terminar / Seguir grabando) o «¿La guardo?» (Guardar / Descartar).

        Misma superficie que el popup: la pastilla crece hasta la tarjeta; la cuenta atras la
        lleva el vigilante."""
        win = self._popup
        if win is None:
            raise RuntimeError("UiHost.run() has not created the windows yet")
        mon = self._surface_monitor(monitor_hint)
        payload = {
            "title": title,
            "durationS": round(max(0.0, duration_s), 1),
            "confirmed": bool(confirmed),
            "timeoutS": countdown_total_s,
        }
        with self._lock:
            self._popup_state = "showing"
            self._popup_mode = "end"
            self._popup_mon = mon
            self._popup_shown_at = time.monotonic()
            self._popup_leaving = False
            self._popup_total_s = float(countdown_total_s)
            self._popup_script = f"micnaga.popup.showEnd({json.dumps(payload)})"
        self._present_popup(win, mon)
        log.info("end popup shown from the pill at %s", self._popup_rect)

    def hide_end_popup(self, reason: str = "answered") -> None:
        win = self._popup
        with self._lock:
            if win is None or self._popup_state != "showing" or self._popup_mode != "end":
                return
        self._popup_leaving = True
        win.run_js(f"micnaga.popup.endClose({json.dumps(reason)})")
        self._arm_collapse_guard("fading")

    def handle_end_decide(self, choice: str) -> None:
        self._popup_leaving = True
        self._cb.on_end_decision(choice)  # type: ignore[arg-type]

    # ================================================================= «Descartada · Deshacer»

    def show_undo(self, seconds: float) -> None:
        """Aviso compacto en el sitio de la pastilla: «Descartada · Deshacer» ``seconds``.

        Si la tarjeta se esta encogiendo, el aviso sale cuando termina (la pastilla se
        ensancha hasta el aviso sin saltos)."""
        with self._lock:
            self._undo_visible = True
            self._undo_seconds = seconds
            busy = self._card_busy()
        if not busy:
            self._apply_undo()

    def _apply_undo(self) -> None:
        win = self._pillwin
        if win is None:
            return
        self._cancel_vanish()
        mon = self._pill.monitor or self._popup_mon or self._w32.monitor_info(None)
        with self._lock:
            shown = self._pill.shown
            seconds = self._undo_seconds
            self._surface_phase = "undo"
        if not shown:
            self._layout_stage(mon, self._pill_target(mon))
        self._set_region("undo")
        win.run_js(f"micnaga.pill.undo({json.dumps({'seconds': seconds})})")
        win.show(activate=False)
        with self._lock:
            self._pill.shown = True

    def hide_undo(self) -> None:
        win = self._pillwin
        with self._lock:
            was = self._undo_visible
            self._undo_visible = False
            pill_up = self._pill.visible
            busy = self._card_busy()
        if win is None or not was:
            return
        win.run_js("micnaga.pill.undoEnd()")
        if busy:
            return
        if pill_up:
            with self._lock:
                self._surface_phase = "pill"
            self._set_region("pill")
        else:
            self._vanish()

    @property
    def undo_visible(self) -> bool:
        return self._undo_visible

    def handle_pill_undo(self) -> None:
        self._cb.on_undo()

    def handle_pill_save(self) -> None:
        """«¿Guardar?» de la pastilla provisional = confirmar (como «Grabar ahora»)."""
        self._cb.on_record_now()

    def notify_error(self, key: str) -> None:
        """Aviso de error visible (toast) p. ej. ``capture_failed``."""
        try:
            self._toast.show_error(key)
        except Exception:
            log.exception("could not show the error toast")

    def hide_popup(self, reason: str = "ended") -> None:
        """La reunion acabo antes de responder: la tarjeta se encoge de vuelta a la pastilla."""
        win = self._popup
        with self._lock:
            if win is None or self._popup_state != "showing":
                return
        self._popup_leaving = True
        win.run_js("micnaga.popup.end()")
        # red de seguridad: si el JS no contesta, se cierra igualmente
        self._arm_collapse_guard("fading")

    def expire_popup(self) -> None:
        """El vigilante dio por agotado el tiempo: la tarjeta vuelve a ser pastilla (o se va).

        Pide al JS ``micnaga.popup.expire()`` (si existe); si en 0,8 s el popup sigue como
        estaba, lo resuelve aqui (la pastilla sin animacion, o desvanecida).
        """
        win = self._popup
        with self._lock:
            if win is None or self._popup_state != "showing":
                return
        provisional = self._popup_on_timeout != "discard"
        if self._popup_mode == "end":
            return
        if provisional:
            self._pill.provisional = True
        win.run_js(
            "window.micnaga && micnaga.popup && micnaga.popup.expire && micnaga.popup.expire()"
        )
        threading.Timer(0.8, lambda: self._force_expire(provisional)).start()

    def _force_expire(self, provisional: bool) -> None:
        with self._lock:
            if self._popup_state != "showing":
                return
        if provisional:
            self.handle_popup_morph(True)
            self.handle_popup_closed("morphed")
        else:
            self.handle_popup_closed("leaving")

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

    # -- eventos del JS (SurfaceApi)
    def handle_popup_decide(self, decision: str) -> None:
        self._popup_leaving = True
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

    def handle_surface_phase(self, phase: str) -> None:
        """El JS termino de abrir la tarjeta (``card``) o empezo a encogerla (``collapsing``)."""
        with self._lock:
            if self._popup_state == "idle":
                return
            if phase == "card" and self._surface_phase == "expanding":
                self._surface_phase = "card"
            elif phase == "collapsing":
                self._surface_phase = "collapsing"
                self._popup_state = "morphing"

    def handle_popup_morph(self, provisional: bool) -> None:
        """La tarjeta vuelve a ser la pastilla (tras «Sí» o sin respuesta)."""
        with self._lock:
            if self._popup_state == "idle":
                return
            self._popup_state = "morphing"
            self._surface_phase = "collapsing"
            self._pill.provisional = provisional
        if self._pillwin is not None:
            self._pillwin.run_js(f"micnaga.pill.setProvisional({json.dumps(provisional)})")

    def handle_popup_closed(self, kind: str) -> None:
        """Fin del encogido: la superficie queda en pastilla, en «Deshacer» o desaparece."""
        win = self._popup
        with self._lock:
            if self._popup_state == "idle":
                return
            self._popup_state = "idle"
            self._popup_leaving = True
        if win is not None:
            win.run_js("micnaga.pill.settle()")  # idempotente: si el JS no acabo, encaja ya
            if win.hwnd:
                winapi.apply_no_activate(win.hwnd, self._w32)  # por si se activo con un clic
        self._resolve_surface()
        self._cb.on_popup_closed(kind)

    def _resolve_surface(self) -> None:
        with self._lock:
            undo = self._undo_visible
            pill_up = self._pill.visible
        if undo:
            self._apply_undo()
        elif pill_up:
            with self._lock:
                self._surface_phase = "pill"
            self._set_region("pill")
            self._update_tray()
        else:
            self._vanish()

    # ================================================================= pastilla

    def show_pill(
        self,
        *,
        elapsed_s: float = 0.0,
        provisional: bool = False,
        monitor_hint: MonitorHint = None,
    ) -> None:
        """Muestra la pastilla «Grabando». Con la tarjeta abierta la pastilla ya esta dentro
        de ella: solo se anota (aparece al encogerse)."""
        with self._lock:
            if self._pill.visible or self._card_busy():
                update = True
                if not self._pill.visible:
                    self._pill.visible = True
                    self._pill_started = time.monotonic() - elapsed_s
            else:
                update = False
        if update:
            self.set_pill_provisional(provisional)
            if self._card_busy():
                self.sync_pill(elapsed_s)
            self._update_tray()
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
        self._cancel_vanish()
        with self._lock:
            pill = self._pill
            pill.visible, pill.provisional = True, provisional
            self._pill_started = time.monotonic() - elapsed
            pill.expanded = False
            self._undo_visible = False
            self._surface_phase = "pill"
        layout = self._layout_stage(mon, self._pill_target(mon))
        payload = {
            "elapsed": elapsed,
            "provisional": provisional,
            "paused": self._pill.paused,
            "layout": layout,
        }
        self._set_region("pill")
        win.run_js(f"micnaga.pill.show({json.dumps(payload)})")
        win.show(activate=False)
        with self._lock:
            self._pill.shown = True
        self._update_tray()

    def hide_pill(self) -> None:
        """La grabacion acabo: la pastilla se desvanece. Con la tarjeta abierta o encogiendose
        solo se anota; se resuelve al terminar el encogido (``handle_popup_closed``)."""
        win = self._pillwin
        with self._lock:
            self._pill.visible = False
            self._pill.paused = False
            self._pill.title = None
            busy = self._card_busy()
            undo = self._undo_visible
        if win is not None:
            win.run_js("micnaga.pill.stop()")
            if not busy and not undo:
                self._vanish()
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
        self._pill_started = time.monotonic() - elapsed_s
        if self._pillwin is not None:
            self._pillwin.run_js(f"micnaga.pill.sync({elapsed_s:.2f})")

    @property
    def pill_visible(self) -> bool:
        return self._pill.visible

    # -- eventos del JS (SurfaceApi)
    def handle_pill_hover(self, hovering: bool) -> None:
        """Al pasar el raton la pastilla se ensancha en CSS; aqui solo crece la region (antes de
        ensancharse) o se recoge (cuando ya se estrecho)."""
        with self._lock:
            pill = self._pill
            if (
                self._pillwin is None
                or pill.layout is None
                or not pill.visible
                or self._card_busy()
                or self._undo_visible
                or pill.expanded == hovering
            ):
                return
            pill.expanded = hovering
        self._set_region("hover" if hovering else "pill")

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
        """El usuario arrastro la superficie (``x, y`` = esquina del lienzo): se recuerda la
        posicion de la pastilla, que esta desplazada dentro del lienzo."""
        with self._lock:
            pill = self._pill
            if (
                not pill.visible
                or pill.base is None
                or pill.stage is None
                or self._card_busy()
                or time.monotonic() < pill.ignore_moves_until
            ):
                return
            dx = pill.base[0] - pill.stage[0]
            dy = pill.base[1] - pill.stage[1]
            pill.stage = (x, y, pill.stage[2], pill.stage[3])
            px, py = x + dx, y + dy
            pill.user_pos = (px, py)
            pill.base = (px, py, pill.base[2], pill.base[3])
        self._cb.on_pill_moved(px, py)

    # ================================================================= bandeja y avisos

    def _tray_callbacks(self) -> UiCallbacks:
        cb = self._cb
        return UiCallbacks(
            on_record_now=cb.on_record_now,
            on_pause_watching=cb.on_pause_watching,
            on_resume_watching=cb.on_resume_watching,
            on_open_library=self._tray_open_library,
            on_open_settings=self._tray_open_settings,
            on_open_timesheets=self._tray_open_timesheets,
            on_quit=self._tray_quit,
        )

    def _tray_open_library(self) -> None:
        self.open_library()
        self._cb.on_open_library()

    def _tray_open_timesheets(self) -> None:
        self.open_timesheets()
        self._cb.on_open_timesheets()

    def _timesheets_available(self) -> bool:
        """«Rellenar partes de horas» activo: Secretaria con Odoo que permite registrar horas
        (segun el ultimo perfil de capacidades guardado; sin red)."""
        sec = self._config.secretaria
        if self._brain is None or not (sec.enabled and sec.odoo_url):
            return False
        try:
            return bool(self._brain.api("caps", {}).get("horas"))
        except Exception:
            return False

    def open_timesheets(self) -> None:
        """La biblioteca con el panel «Partes de horas» abierto."""
        self.open_library(view="horas")

    def notify_hours(self, days: int) -> None:
        """Aviso del viernes: faltan partes de horas esta semana."""
        self._toast.show_hours(days, self.open_timesheets)

    def _tray_open_settings(self) -> None:
        self.open_settings()
        self._cb.on_open_settings()

    def _tray_quit(self) -> None:
        self._quit_reason = "tray_quit"
        self._cb.on_quit()  # primero el integrador (parar captura/vigilante), luego la GUI
        self.quit("tray_quit")

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

    def notify_ready(
        self, folder: str, title: str, duration_s: float, speakers: int, proposals: int = 0
    ) -> None:
        """Toast «Lista»: «Revision semanal · 47 min · 3 hablantes» (``· N propuestas``)."""
        self._toast.show_ready(folder, title, duration_s, speakers, proposals)

    def set_brain(self, brain: BrainPort | None) -> None:
        """La Secretaria (la crea el integrador); las ventanas abiertas despues la usan."""
        self._brain = brain
        setter = getattr(brain, "set_nudge", None)
        if setter is not None:
            setter(self.notify_hours)

    def set_pill_title(self, title: str | None) -> None:
        """Titulo provisional en directo: se ve en la pastilla al pasar el raton."""
        with self._lock:
            self._pill.title = title or None
        if self._pillwin is not None:
            self._pillwin.run_js(f"micnaga.pill.setTitle({json.dumps(title or '')})")

    def _open_from_toast(self, folder: str) -> None:
        resolve = getattr(self._brain, "resolve_folder", None)
        if resolve is not None:  # la Secretaria pudo retitular la reunion despues del aviso
            folder = str(resolve(folder))
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
            held=self._held_ops,
            brain=self._brain,
            open_settings=self.open_settings,
            settings=self.settings_api,
        )

    def open_library(
        self, folder: str | None = None, seek_s: float | None = None, view: str | None = None
    ) -> None:
        """Abre la biblioteca (opcionalmente directamente en una reunion y un instante, o en el
        panel ``view="horas"``)."""
        t = i18n.t("library.window_title", self._config.ui.language)
        params: dict[str, Any] = {}
        if folder:
            params["folder"] = folder
            if seek_s is not None:
                params["t"] = f"{seek_s:.2f}"
        if view in ("horas", "settings"):
            params["view"] = view
        spec = self._normal_spec(
            "library", "library.html", t, 1120, 720, self.library_api(), params, min_size=(820, 540)
        )
        js = (
            "window.micnaga && micnaga.library && "
            f"micnaga.library.open({json.dumps(folder)}, {json.dumps(seek_s)})"
            if folder
            else None
        )
        if view == "horas":
            js = "window.micnaga && micnaga.horas && micnaga.horas.open()"
        elif view == "settings":
            js = "window.micnaga && micnaga.library && micnaga.library.showSettings()"
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
            brain=self._brain,
        )

    def open_settings(self) -> None:
        """Ajustes vive dentro de la ventana de la biblioteca (misma ventana, vista «Ajustes»)."""
        self.open_library(view="settings")

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
            auto_finish=True,
        )
        spec = self._normal_spec(
            "wizard", "wizard.html", t, 600, 680, api, resizable=False, min_size=(520, 560)
        )
        self._open_normal(spec)
        # Primer arranque sin decisiones: la descarga empieza sola (el asistente solo muestra el
        # progreso) y, al terminar, el propio WizardApi cierra el asistente (auto_finish).
        api.start_download()

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
            win = self._windows.get("settings") or self._windows.get("library")
        return win.pick_folder(current) if win is not None else None

    def set_held_ops(self, ops: HeldOps | None) -> None:
        """«Descartadas» en la biblioteca (lo pone la integracion)."""
        self._held_ops = ops

    def set_on_window_closed(self, callback: Callable[[str], None] | None) -> None:
        """Aviso al cerrarse una ventana normal (``library`` | ``settings`` | ``wizard``)."""
        self._on_window_closed = callback

    def has_window(self, key: str) -> bool:
        return key in self._windows


def _sigint_pending() -> bool:
    mod = sys.modules.get("webview.platforms.winforms")
    return bool(getattr(mod, "_sigint_received", False))


def _reset_pywebview_after_loop() -> None:
    """pywebview deja estado de modulo tras ``start``: la bandera de SIGINT (si no, el bucle
    nuevo saldria al instante) y la lista de ventanas."""
    mod = sys.modules.get("webview.platforms.winforms")
    if mod is not None and getattr(mod, "_sigint_received", False):
        mod._sigint_received = False  # type: ignore[attr-defined]
    webview = sys.modules.get("webview")
    windows = getattr(webview, "windows", None)
    if isinstance(windows, list):
        windows.clear()


PILL_TITLE_HOVER_WIDTH = 420


def _hover_width(pill: _Pill) -> int:
    return PILL_TITLE_HOVER_WIDTH if pill.title else winapi.PILL_HOVER_WIDTH


def _spawn_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="micnaga-ui-task", daemon=True).start()
