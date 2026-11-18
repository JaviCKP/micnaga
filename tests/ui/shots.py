"""Capturas de cada superficie (claro y oscuro) en ``docs/screenshots/``.

``python -m tests.ui.shots [directorio]``. Usa Chromium headless por CDP (``tests/ui/cdp.py``)
contra el servidor local con ``dev_api`` (las paginas hablan con los puentes Python reales:
``LibraryApi``, ``SettingsApi``, ``WizardApi`` sobre datos de demo en una carpeta temporal).

Son capturas de Chromium, no de WebView2: la fidelidad del DOM/CSS es la misma, pero el
efecto Mica, la transparencia real de la ventana y las animaciones de Windows no se ven aqui.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]


class _FakeController:
    """Controlador minimo para PopupApi/PillApi en las capturas."""

    def __init__(self, levels: tuple[float, float]) -> None:
        self._levels = levels

    def levels(self) -> tuple[float, float]:
        return self._levels

    def __getattr__(self, name: str) -> Any:
        if name.startswith("handle_"):
            return lambda *a, **k: None
        raise AttributeError(name)


def _composite_popup(card: Path, dark: bool, out: Path) -> None:
    """El popup sobre un fondo de pagina (simula Meet) con su sombra, para el README/revision."""
    from PIL import Image, ImageDraw, ImageFilter

    img = Image.open(card).convert("RGBA")
    w, h = img.size
    pad = 56
    bg = Image.new(
        "RGBA", (w + pad * 2, h + pad * 2), (32, 33, 36, 255) if dark else (233, 232, 235, 255)
    )
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=24, fill=255)
    shadow = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (pad, pad + 14, pad + w, pad + h + 14), radius=24, fill=(0, 0, 0, 120 if dark else 70)
    )
    bg = Image.alpha_composite(bg, shadow.filter(ImageFilter.GaussianBlur(26)))
    bg.paste(img, (pad, pad), mask)
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.convert("RGB").save(out)


def run(out_dir: Path) -> list[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="micnaga-shots-"))
    env_keys = ("MICNAGA_HOME", "MICNAGA_STORAGE", "MICNAGA_MODELS", "MICNAGA_UI_DEMO")
    saved = {k: os.environ.get(k) for k in env_keys}
    os.environ.update(
        MICNAGA_HOME=str(tmp / "home"),
        MICNAGA_STORAGE=str(tmp / "meetings"),
        MICNAGA_MODELS=str(tmp / "models"),
        MICNAGA_UI_DEMO="1",
    )
    shots: list[Path] = []
    try:
        from micnaga import paths
        from micnaga.config import Config
        from micnaga.store.db import Database
        from micnaga.ui import fake, startup
        from micnaga.ui.bridge import LibraryApi, PillApi, PopupApi, SettingsApi, WizardApi
        from micnaga.ui.contracts import UiCallbacks
        from micnaga.ui.host import FileConfigStore
        from micnaga.ui.media import LocalServer
        from tests.ui.cdp import Browser

        startup.set_default_backend(startup.MemoryRegistry())
        cfg = Config()
        cfg.privacy.voiceprints_enabled = True
        root = paths.meetings_root(cfg)
        folders = fake.build_demo_library(root)
        ops = fake.FakeLibraryOps(lambda: root)
        ops.voiceprints["Laura"] = fake.Voiceprint("Laura", "2026-11-02", 3)
        store = FileConfigStore()
        store.save(cfg)
        reg = startup.MemoryRegistry()
        reg.set(startup.VALUE_NAME, "demo")
        srv_ref: list[LocalServer] = []
        lib = LibraryApi(
            db_factory=lambda: Database(root / "micnaga.db"),
            meetings_root=lambda: root,
            audio_url=lambda f, t: srv_ref[0].audio_url(f, t),
            config=lambda: cfg,
            ops=ops,
            opener=lambda p: None,
        )
        api: dict[str, object] = {
            "library": lib,
            "settings": SettingsApi(
                store=store,
                callbacks=UiCallbacks(),
                registry=reg,
                diagnostics=fake.fake_diagnostics,
                ops=ops,
            ),
            "wizard": WizardApi(
                download_models=fake.fake_download_models,
                levels=lambda: (0.5, 0.012),
                callbacks=UiCallbacks(),
            ),
            "popup": PopupApi(_FakeController((0.3, 0.08))),
            "pill": PillApi(_FakeController((0.4, 0.1))),
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        with LocalServer(meetings_root=lambda: root, dev_api=api) as srv, Browser() as b:
            srv_ref.append(srv)

            def url(page: str, surface: str, theme: str, **q: Any) -> str:
                return srv.page_url(page, theme=theme, bridge="http", surface=surface, **q)

            def shot(
                name: str,
                theme: str,
                page: str,
                surface: str,
                w: int,
                h: int,
                dpr: float = 1.0,
                wait: float = 1.0,
                js: str | None = None,
                **q: Any,
            ) -> Path:
                p = b.new_page(w, h, dpr)
                p.emulate(dark=theme == "dark")
                p.goto(url(page, surface, theme, **q), wait=wait)
                if js:
                    p.eval(js)
                    import time

                    time.sleep(0.5)
                path = p.screenshot(out_dir / f"{name}_{theme}.png")
                b.call("Target.closeTarget", {"targetId": p.target})
                shots.append(path)
                return path

            for theme in ("light", "dark"):
                card = shot(
                    "popup_card",
                    theme,
                    "popup.html",
                    "popup",
                    400,
                    232,
                    2,
                    levels="0.30,0.08",
                    progress="0.62",
                    wait=1.2,
                )
                _composite_popup(card, theme == "dark", out_dir / f"popup_{theme}.png")
                card.unlink()
                shots.remove(card)
                shots.append(out_dir / f"popup_{theme}.png")
                for state, q in (
                    ("rec", {}),
                    ("hover", {"hover": 1}),
                    ("paused", {"paused": 1}),
                    ("provisional", {"provisional": 1}),
                    ("provisional_hover", {"provisional": 1, "hover": 1}),
                ):
                    width = 296 if "hover" in state else 200
                    shot(
                        f"pill_{state}",
                        theme,
                        "pill.html",
                        "pill",
                        width + 8,
                        48,
                        2,
                        levels="0.45,0.12",
                        elapsed=754,
                        wait=0.9,
                        **q,
                    )
                shot(
                    "library",
                    theme,
                    "library.html",
                    "library",
                    1180,
                    760,
                    1,
                    wait=1.4,
                    folder=folders[0],
                    t="610",
                )
                shot(
                    "library_search",
                    theme,
                    "library.html",
                    "library",
                    1180,
                    760,
                    1,
                    wait=1.4,
                    folder=folders[0],
                    q="proveedor",
                )
                shot(
                    "library_menu",
                    theme,
                    "library.html",
                    "library",
                    1180,
                    760,
                    1,
                    wait=1.4,
                    folder=folders[0],
                    t="610",
                    js="document.querySelectorAll('.spk .more')[0].click()",
                )
                shot(
                    "settings",
                    theme,
                    "settings.html",
                    "settings",
                    880,
                    760,
                    1,
                    wait=1.2,
                    tab="general",
                )
                shot(
                    "settings_detection",
                    theme,
                    "settings.html",
                    "settings",
                    880,
                    760,
                    1,
                    wait=1.2,
                    tab="detection",
                )
                shot(
                    "settings_privacy",
                    theme,
                    "settings.html",
                    "settings",
                    880,
                    760,
                    1,
                    wait=1.4,
                    tab="privacy",
                )
                shot(
                    "settings_diagnostics",
                    theme,
                    "settings.html",
                    "settings",
                    880,
                    760,
                    1,
                    wait=1.6,
                    tab="diagnostics",
                )
                shot("wizard_1", theme, "wizard.html", "wizard", 600, 680, 1, step=1)
                shot(
                    "wizard_2",
                    theme,
                    "wizard.html",
                    "wizard",
                    600,
                    680,
                    1,
                    wait=1.2,
                    step=2,
                    js="document.getElementById('next').click()",
                )
                shot("wizard_3", theme, "wizard.html", "wizard", 600, 680, 1, wait=1.6, step=3)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    return shots


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "docs" / "screenshots"
    for s in run(target):
        print(s)
