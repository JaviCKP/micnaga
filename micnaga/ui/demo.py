"""Modo demo: ``python -m micnaga.ui.demo popup|pill|library|settings|wizard|toast|tray``.

Abre cada superficie con datos FALSOS para revisarla a ojo. Seguro por construccion:

* ``MICNAGA_HOME`` / ``MICNAGA_STORAGE`` apuntan a una carpeta temporal (jamas toca tu
  ``%APPDATA%\\Micnaga`` ni ``Documentos\\Micnaga``), que se borra al salir.
* El registro de «Arrancar con Windows» es uno falso en memoria; ademas ``MICNAGA_UI_DEMO=1``
  hace que el acceso real al registro se niegue a escribir.
* Niveles, reuniones, descargas y diagnosticos son sinteticos.

Opciones: ``--theme light|dark|system``, ``--auto-quit SEGUNDOS``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

SURFACES = ("popup", "pill", "library", "settings", "wizard", "toast", "tray")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m micnaga.ui.demo", description=__doc__.split("\n")[0]
    )
    ap.add_argument("surface", choices=SURFACES)
    ap.add_argument("--theme", choices=("system", "light", "dark"), default="system")
    ap.add_argument(
        "--auto-quit", type=float, default=0.0, metavar="S", help="cerrar tras S segundos"
    )
    args = ap.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="micnaga-ui-demo-"))
    os.environ["MICNAGA_HOME"] = str(tmp / "home")
    os.environ["MICNAGA_STORAGE"] = str(tmp / "meetings")
    os.environ["MICNAGA_MODELS"] = str(tmp / "models")
    os.environ["MICNAGA_UI_DEMO"] = "1"
    try:
        return _run(args, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(args: argparse.Namespace, tmp: Path) -> int:
    from micnaga import paths
    from micnaga.config import Config, save_config
    from micnaga.ui import fake, startup
    from micnaga.ui.contracts import UiCallbacks
    from micnaga.ui.host import FileConfigStore, UiHost

    startup.set_default_backend(startup.MemoryRegistry())  # JAMAS el Run real
    cfg = Config()
    cfg.ui.theme = args.theme
    cfg.privacy.voiceprints_enabled = args.surface in ("library", "settings")
    save_config(cfg)

    ops = fake.FakeLibraryOps(lambda: paths.meetings_root(cfg))
    folders: list[str] = []
    if args.surface in ("library", "toast", "tray"):
        folders = fake.build_demo_library(paths.meetings_root(cfg))

    log = fake.demo_event_logger()
    cb = UiCallbacks(**log)
    host_ref: list[UiHost] = []

    def quit_now(*_a: object) -> None:
        if host_ref:
            host_ref[0].quit()

    def stop_and_quit() -> None:
        log["on_stop"]()
        host_ref[0].hide_pill()
        quit_now()

    if args.surface in ("pill", "popup"):
        cb.on_stop = stop_and_quit

    host = UiHost(
        config=cfg,
        callbacks=cb,
        levels=fake.FakeLevels(),
        library_ops=ops,
        download_models=fake.fake_download_models,
        diagnostics=fake.fake_diagnostics,
        config_store=FileConfigStore(),
        registry=startup.default_backend(),
        tray=args.surface == "tray",
    )
    host_ref.append(host)

    def on_ready(h: UiHost) -> None:
        if args.auto_quit:
            threading.Timer(args.auto_quit, h.quit).start()
        s = args.surface
        if s == "popup":
            h.show_popup("Revisión semanal", "abc-defg-hij")
        elif s == "pill":
            h.show_pill(elapsed_s=754)
        elif s == "library":
            h.set_on_window_closed(lambda _k: h.quit())
            h.open_library(folder=folders[0] if folders else None)
        elif s == "settings":
            h.set_on_window_closed(lambda _k: h.quit())
            h.open_settings()
        elif s == "wizard":
            h.set_on_window_closed(lambda _k: h.quit())
            h.open_wizard()
        elif s == "toast":
            h.notify_ready(folders[0], "Revisión semanal", 2820, 3)
            threading.Timer(args.auto_quit or 8.0, h.quit).start()
        elif s == "tray":
            h.set_processing(False)
            print("Icono de bandeja activo; usa «Salir» del menu para cerrar.")

    host.run(on_ready)
    return 0


if __name__ == "__main__":
    sys.exit(main())
