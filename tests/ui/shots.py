"""Capturas de cada superficie (claro y oscuro) en ``docs/screenshots/``.

``python -m tests.ui.shots [directorio] [--only popup,pill,...]``. Usa Chromium headless por CDP
(``tests/ui/cdp.py``) contra el servidor local con ``dev_api`` (las paginas hablan con los
puentes Python reales: ``LibraryApi``, ``SettingsApi``, ``WizardApi`` sobre datos de demo en una
carpeta temporal).

Las ventanas superpuestas (popup, pastilla) se capturan con fondo transparente y se componen
sobre una pagina simulada (una reunion oscura llena de mosaicos y un documento blanco), para
juzgarlas donde viven: encima de contenido arbitrario.

Son capturas de Chromium, no de WebView2: la fidelidad del DOM/CSS es la misma, pero el
efecto Mica, la transparencia real de la ventana y las animaciones de Windows no se ven aqui.
"""

from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
POPUP = (372, 112)  # ventana del popup (px logicos)


class _FakeController:
    """Controlador minimo para PopupApi/PillApi: niveles que respiran (como una voz)."""

    def __init__(self, levels: tuple[float, float]) -> None:
        self._levels = levels
        self._t0 = time.monotonic()

    def levels(self) -> tuple[float, float]:
        t = time.monotonic() - self._t0
        a = abs(math.sin(t * 5.1) * math.sin(t * 1.7)) * 0.9 + 0.1
        b = abs(math.sin(t * 3.3 + 1.2) * math.sin(t * 0.8)) * 0.9 + 0.1
        return (self._levels[0] * a, self._levels[1] * b)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("handle_"):
            return lambda *a, **k: None
        raise AttributeError(name)


def _backdrop(w: int, h: int, dark: bool) -> Any:
    """Una pagina simulada: Meet con mosaicos (oscuro) o un documento (claro)."""
    from PIL import Image, ImageDraw, ImageFilter

    if dark:
        img = Image.new("RGBA", (w, h), (32, 33, 36, 255))
        d = ImageDraw.Draw(img)
        tiles = [(24, 90, w // 2 - 12, h - 90), (w // 2 + 12, 90, w - 24, h - 90)]
        colors = [((76, 92, 120), (40, 46, 60)), ((120, 84, 72), (52, 40, 38))]
        for (x0, y0, x1, y1), (c0, c1) in zip(tiles, colors, strict=True):
            for y in range(y0, y1):
                k = (y - y0) / max(1, y1 - y0)
                col = tuple(round(c0[i] * (1 - k) + c1[i] * k) for i in range(3))
                d.line((x0, y, x1, y), fill=(*col, 255))
            d.ellipse(
                (
                    (x0 + x1) // 2 - 60,
                    (y0 + y1) // 2 - 70,
                    (x0 + x1) // 2 + 60,
                    (y0 + y1) // 2 + 50,
                ),
                fill=(200, 200, 210, 70),
            )
            d.rounded_rectangle((x0 + 14, y1 - 38, x0 + 130, y1 - 14), 8, fill=(0, 0, 0, 120))
        d.rounded_rectangle(
            (w // 2 - 170, h - 64, w // 2 + 170, h - 16), 24, fill=(60, 64, 67, 255)
        )
        for i in range(5):
            cx = w // 2 - 120 + i * 60
            d.ellipse(
                (cx - 18, h - 58, cx + 18, h - 22),
                fill=(234, 67, 53, 255) if i == 4 else (95, 99, 104, 255),
            )
        return img.filter(ImageFilter.GaussianBlur(0.6))
    img = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w, 56), fill=(241, 243, 244, 255))
    for i, y in enumerate(range(96, h - 20, 26)):
        ln = w - 120 - (i * 53) % 260
        d.rounded_rectangle((60, y, ln, y + 9), 4, fill=(60, 64, 67, 60 if i % 5 else 140))
    d.rounded_rectangle((w - 260, 110, w - 60, 250), 12, fill=(26, 115, 232, 40))
    return img


def _composite(
    layers: list[tuple[Path, tuple[int, int], float]],
    size: tuple[int, int],
    dark: bool,
    out: Path,
    scale: int,
    acrylic: int = 0,
) -> Path:
    """Superpone capturas transparentes (ruta, posicion logica, opacidad) sobre el fondo.

    ``acrylic`` (radio de esquina en px logicos) simula el Acrylic de DWM bajo cada capa:
    desenfoque gaussiano del fondo recortado a la ventana, como hace Windows de verdad.
    """
    from PIL import Image, ImageDraw, ImageFilter

    bg = _backdrop(size[0] * scale, size[1] * scale, dark)
    for path, (x, y), alpha in layers:
        im = Image.open(path).convert("RGBA")
        if alpha < 1:
            a = im.getchannel("A").point(lambda v, k=alpha: round(v * k))
            im.putalpha(a)
        pos = (x * scale, y * scale)
        if acrylic:
            box = (pos[0], pos[1], pos[0] + im.width, pos[1] + im.height)
            blurred = bg.crop(box).filter(ImageFilter.GaussianBlur(30 * scale))
            mask = Image.new("L", im.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, im.width - 1, im.height - 1), radius=acrylic * scale, fill=255
            )
            bg.paste(blurred, pos, mask)
        bg.alpha_composite(im, pos)
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.convert("RGB").save(out)
    return out


DEMO_FULL_ACTA = """# Acta: Dental Triana · alcance ampliado

**Fecha:** jueves 19 de noviembre de 2026, 10:30 · **Duración:** 1 min · **Asistentes:** Yo,
Carlos (Dental Triana), Marta Gil (recepción)

## Orden del día
1. Alcance de la fase 1
2. Presupuesto
3. Integraciones y demo

## 1. Alcance de la fase 1
Carlos confirma que dirección aprueba incluir el **panel de ventas** en la fase 1 [00:20].
Marta se incorpora como responsable de recepción [00:05] y deja su correo para los envíos.

## 2. Presupuesto
El total sube a unos 6.900 € [00:23]. Yo me comprometo a enviar el presupuesto revisado
antes del viernes [00:26].

## 3. Integraciones y demo
Se pide integración con Odoo y con la sede de Triana [00:33]. La demo será el 3 de diciembre
con datos reales [00:36]. Queda abierta la gestión de respuestas fuera de horario [00:39].

## Decisiones
- Panel de ventas en la fase 1, por decisión de dirección [00:20].

## Acuerdos y tareas
- **Yo:** presupuesto revisado, antes del viernes 20/11 [00:26].
- **Marta:** lista de plantillas de WhatsApp, el martes 24/11 [00:29].

## Preguntas abiertas
- Respuestas a pacientes fuera de horario [00:39].

## Próximos pasos
- Demo con datos reales el 3 de diciembre [00:36].

## Fuentes consultadas
Ninguna, salvo la transcripción.
"""


def _dress_cleanup(folder: Path, root: Path) -> None:
    """La reunion de demo como queda tras la limpieza con IA (texto original guardado) y con su
    «Acta completa» ya escrita (un ``claude`` falso en proceso)."""
    import dataclasses

    from micnaga.brain import cleanup, full_acta
    from micnaga.brain.claude import ClaudeResult
    from micnaga.config import Config
    from micnaga.pipeline import output
    from micnaga.store.db import Database

    tr = output.read_transcript(folder)
    fixes = {
        1: "Buenos días, Javier. Te presento a Marta, que lleva la recepción de la clínica.",
        5: "El total sube a unos 6.900 euros.",
        8: "Queremos que esté integrado con Odoo y con la sede de Triana.",
        9: "Hacemos la demo el 3 de diciembre con datos reales.",
    }
    segs = list(tr.segments)
    for i, text in fixes.items():
        s = segs[i]
        segs[i] = dataclasses.replace(
            s, text=text, text_original=s.text, words=cleanup.remap_words(s.words, text)
        )
    output.write_transcript(folder, dataclasses.replace(tr, segments=segs))

    class Writer:
        def run_once(self, req: Any, on_event: Any = None) -> ClaudeResult:
            return ClaudeResult(None, DEMO_FULL_ACTA, 0.15, {}, 40.0, 1, "claude-demo")

    def with_db(fn: Any) -> Any:
        with Database(root / "micnaga.db") as db:
            return fn(db)

    full_acta.FullActa(Config(), Writer(), with_db=with_db).make(folder)


def run(out_dir: Path, only: set[str] | None = None) -> list[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="micnaga-shots-"))
    env_keys = (
        "MICNAGA_HOME",
        "MICNAGA_STORAGE",
        "MICNAGA_MODELS",
        "MICNAGA_UI_DEMO",
        "MICNAGA_CLAUDE_EXE",
    )
    saved = {k: os.environ.get(k) for k in env_keys}
    os.environ.update(
        MICNAGA_HOME=str(tmp / "home"),
        MICNAGA_STORAGE=str(tmp / "meetings"),
        MICNAGA_MODELS=str(tmp / "models"),
        MICNAGA_UI_DEMO="1",
    )
    shots: list[Path] = []

    def want(name: str) -> bool:
        return not only or any(name.startswith(o) for o in only)

    try:
        from micnaga import paths
        from micnaga.config import Config
        from micnaga.store.db import Database
        from micnaga.ui import fake, startup
        from micnaga.ui.bridge import LibraryApi, PillApi, PopupApi, SettingsApi, WizardApi
        from micnaga.ui.contracts import UiCallbacks, Voiceprint
        from micnaga.ui.host import FileConfigStore
        from micnaga.ui.media import LocalServer
        from tests.ui.cdp import Browser

        startup.set_default_backend(startup.MemoryRegistry())
        cfg = Config()
        cfg.privacy.voiceprints_enabled = True
        cfg.secretaria.enabled = True
        cfg.secretaria.odoo_url = "https://odoo.example.com"
        cfg.secretaria.odoo_db = "empresa"
        cfg.secretaria.odoo_login = "javier@example.com"
        root = paths.meetings_root(cfg)
        folders = fake.build_demo_library(root)
        # Secretaria: una reunion con acta y propuestas, un claude falso (sesion detectada)
        from micnaga.brain import secrets
        from micnaga.brain.service import BrainService
        from tests.brain.demo import write_demo_meeting

        acta_folder = write_demo_meeting(root)
        _dress_cleanup(root / acta_folder, root)
        # Odoo de negocio: un Odoo falso con permisos de empleado (sin CRM, contactos de solo
        # lectura) para el panel «Partes de horas» y una reunion con propuestas agrupadas.
        from datetime import datetime

        from micnaga.brain import capabilities
        from micnaga.brain.odoo import OdooClient, OdooConfig
        from tests.brain.demo import write_followup_meeting
        from tests.brain.fake_odoo import DB as FAKE_DB
        from tests.brain.fake_odoo import KEY as FAKE_KEY
        from tests.brain.fake_odoo import LOGIN as FAKE_LOGIN
        from tests.brain.fake_odoo import FakeOdoo

        odoo_srv = FakeOdoo().start()
        odoo_srv.access.update(
            {
                "crm.lead": set(),
                "res.partner": {"read"},
                "project.project": {"read"},
                "crm.stage": {"read"},
            }
        )

        def odoo_factory(_cfg: object) -> OdooClient:
            return OdooClient(OdooConfig(odoo_srv.url, FAKE_DB, FAKE_LOGIN, FAKE_KEY))

        oc = odoo_factory(cfg)
        oc.authenticate()
        profile = capabilities.discover(oc)
        s_cfg = cfg.secretaria
        profile["key"] = capabilities.connection_key(
            s_cfg.odoo_url, s_cfg.odoo_db, s_cfg.odoo_login
        )
        capabilities.save(profile)
        followup_folder = write_followup_meeting(root, oc, profile)
        fake_claude = tmp / "claude_fake.py"
        fake_claude.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(REPO / 'tests' / 'brain')!r})\n"
            "import fake_claude\n"
            "sys.exit(fake_claude.main(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        os.environ["MICNAGA_CLAUDE_EXE"] = str(fake_claude)
        secrets.put(secrets.ODOO_KEY, "demo")
        from micnaga.brain import learn

        learn.add_learnings(
            [
                {
                    "hecho": "Las oportunidades se titulan «Empresa · servicio».",
                    "fuente": "crm",
                    "evidencia": "crm.lead",
                    "confianza": 0.9,
                },
                {
                    "hecho": "En las reuniones con Dental Triana suele estar recepción.",
                    "fuente": "reunion",
                    "evidencia": "#1",
                    "confianza": 0.8,
                },
            ]
        )
        brain = BrainService(lambda: cfg, odoo_factory=odoo_factory)
        brain.now = lambda: datetime(2026, 11, 19, 12, 0)
        ops = fake.FakeLibraryOps(lambda: root)
        ops.voiceprints["Laura"] = Voiceprint("Laura", "2026-11-02", 3)
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
            brain=brain,
        )
        api: dict[str, object] = {
            "library": lib,
            "settings": SettingsApi(
                store=store,
                callbacks=UiCallbacks(),
                registry=reg,
                diagnostics=fake.fake_diagnostics,
                ops=ops,
                brain=brain,
            ),
            "wizard": WizardApi(
                download_models=fake.fake_download_models,
                levels=lambda: (0.5, 0.012),
                callbacks=UiCallbacks(),
            ),
            "popup": PopupApi(_FakeController((0.42, 0.16))),
            "pill": PillApi(_FakeController((0.5, 0.2))),
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
                transparent: bool = False,
                keep: bool = True,
                **q: Any,
            ) -> Path:
                p = b.new_page(w, h, dpr)
                p.emulate(dark=theme == "dark")
                if transparent:
                    p.transparent()
                p.goto(url(page, surface, theme, **q), wait=wait)
                if js:
                    p.eval(js)
                    time.sleep(0.6)
                path = p.screenshot(out_dir / f"{name}_{theme}.png")
                b.call("Target.closeTarget", {"targetId": p.target})
                if keep:
                    shots.append(path)
                return path

            for theme in ("light", "dark"):
                dark = theme == "dark"
                # ---- popup over a page (top-right corner of the work area, like the app)
                for name, q in (
                    ("popup", {}),
                    ("popup_acrylic", {"backdrop": "acrylic"}),
                    ("popup_app", {"generic": 1, "title": "Microsoft Teams"}),
                    ("popup_nonotice", {"notice": 0}),
                    ("popup_confirming", {"phase": "confirming"}),
                ):
                    if not want(name):
                        continue
                    card = shot(
                        name + "_card",
                        theme,
                        "popup.html",
                        "popup",
                        *POPUP,
                        2,
                        wait=1.8,
                        transparent=True,
                        keep=False,
                        progress="0.62",
                        **q,
                    )
                    acr = 8 if q.get("backdrop") == "acrylic" else 0
                    out = out_dir / f"{name}_{theme}.png"
                    shots.append(
                        _composite([(card, (564, 24), 1.0)], (960, 300), dark, out, 2, acr)
                    )
                    card.unlink()
                # ---- pill states, over the page at its real resting opacity
                for state, q in (
                    ("rec", {}),
                    ("hover", {"hover": 1}),
                    ("paused", {"paused": 1}),
                    ("provisional", {"provisional": 1}),
                    ("provisional_hover", {"provisional": 1, "hover": 1}),
                    ("acrylic", {"backdrop": "acrylic"}),
                    ("acrylic_hover", {"backdrop": "acrylic", "hover": 1}),
                ):
                    name = f"pill_{state}"
                    if not want(name):
                        continue
                    width = 296 if "hover" in state else 200
                    img = shot(
                        name + "_raw",
                        theme,
                        "pill.html",
                        "pill",
                        width,
                        40,
                        2,
                        wait=1.6,
                        transparent=True,
                        keep=False,
                        elapsed=754,
                        **q,
                    )
                    acr = 8 if "acrylic" in state else 0
                    out = out_dir / f"{name}_{theme}.png"
                    shots.append(
                        _composite(
                            [(img, (180 - width // 2, 12), 1.0)], (360, 64), dark, out, 2, acr
                        )
                    )
                    img.unlink()
                if want("secretaria"):
                    shot(
                        "secretaria_acta",
                        theme,
                        "library.html",
                        "library",
                        1120,
                        900,
                        1,
                        wait=2.0,
                        folder=acta_folder,
                    )
                    shot(
                        "secretaria_horas_panel",
                        theme,
                        "library.html",
                        "library",
                        1120,
                        900,
                        1,
                        wait=2.6,
                        view="horas",
                    )
                    shot(
                        "secretaria_horas_acta",
                        theme,
                        "library.html",
                        "library",
                        1120,
                        900,
                        1,
                        wait=2.2,
                        folder=followup_folder,
                        js="document.querySelector('.sec-props').scrollIntoView({block: 'start'})",
                    )
                    shot(
                        "secretaria_horas_agenda",
                        theme,
                        "library.html",
                        "library",
                        1120,
                        900,
                        1,
                        wait=2.2,
                        folder=followup_folder,
                        # el editor de items abierto en la tarea y en el evento
                        js="const open = (t) => [...document.querySelectorAll('.sec-p')]"
                        ".find((li) => li.textContent.includes(t)).querySelector('.sec-pedit')"
                        ".click();"
                        "open('Tarea:'); setTimeout(() => {"
                        "const i = document.querySelector('.sec-edit input');"
                        "i.value = 'Cuaderno de ejercicios (v2)';"
                        "i.dispatchEvent(new Event('change')); }, 60);"
                        "setTimeout(() => { open('Evento:');"
                        "[...document.querySelectorAll('.sec-gh')].pop()"
                        ".scrollIntoView({block: 'start'}); }, 450);",
                    )
                    shot(
                        "secretaria_settings",
                        theme,
                        "settings.html",
                        "settings",
                        800,
                        900,
                        1,
                        wait=2.4,
                        tab="secretaria",
                    )
                    img = shot(
                        "secretaria_pill_raw",
                        theme,
                        "pill.html",
                        "pill",
                        420,
                        40,
                        2,
                        wait=1.6,
                        transparent=True,
                        keep=False,
                        elapsed=134,
                        hover=1,
                        title="Dental Triana · presupuesto CRM",
                    )
                    out = out_dir / f"secretaria_pill_{theme}.png"
                    shots.append(_composite([(img, (30, 12), 1.0)], (480, 64), dark, out, 2))
                    img.unlink()
                if want("cleanup"):
                    to_row = (
                        "document.querySelector('.ph[data-i=\"5\"]')"
                        ".scrollIntoView({block: 'center'})"
                    )
                    shot(
                        "cleanup_transcript",
                        theme,
                        "library.html",
                        "library",
                        1120,
                        720,
                        1,
                        wait=2.0,
                        folder=acta_folder,
                        js=to_row,
                    )
                    shot(
                        "cleanup_original",
                        theme,
                        "library.html",
                        "library",
                        1120,
                        720,
                        1,
                        wait=2.0,
                        folder=acta_folder,
                        js="document.getElementById('orig-toggle').click();"
                        f"setTimeout(() => {to_row}, 80)",
                    )
                    shot(
                        "acta_completa",
                        theme,
                        "library.html",
                        "library",
                        1120,
                        900,
                        1,
                        wait=2.0,
                        folder=acta_folder,
                        js="document.querySelector('.sec-full').click()",
                    )
                if want("library"):
                    shot(
                        "library",
                        theme,
                        "library.html",
                        "library",
                        1120,
                        720,
                        1,
                        wait=1.6,
                        folder=folders[0],
                        t="610",
                    )
                    shot(
                        "library_search",
                        theme,
                        "library.html",
                        "library",
                        1120,
                        720,
                        1,
                        wait=1.6,
                        folder=folders[0],
                        q="proveedor",
                    )
                    shot(
                        "library_menu",
                        theme,
                        "library.html",
                        "library",
                        1120,
                        720,
                        1,
                        wait=1.6,
                        folder=folders[0],
                        t="610",
                        js="document.querySelectorAll('.spk .more')[1].click()",
                    )
                    shot("library_empty", theme, "library.html", "library", 820, 540, 1, wait=1.4)
                if want("settings"):
                    for tab in ("general", "detection", "privacy", "diagnostics", "about"):
                        suffix = "" if tab == "general" else "_" + tab
                        shot(
                            "settings" + suffix,
                            theme,
                            "settings.html",
                            "settings",
                            800,
                            760,
                            1,
                            wait=1.6,
                            tab=tab,
                        )
                    shot(
                        "settings_min",
                        theme,
                        "settings.html",
                        "settings",
                        640,
                        520,
                        1,
                        wait=1.4,
                        tab="detection",
                    )
                if want("secretaria") and theme == "dark":
                    # aplica solo las correcciones (son locales) y cambia la reunion de demo:
                    # por eso va al final y una sola vez
                    shot(
                        "secretaria_applied",
                        theme,
                        "library.html",
                        "library",
                        1120,
                        900,
                        1,
                        wait=2.0,
                        folder=acta_folder,
                        js="document.querySelectorAll('.sec-props input[type=checkbox]')"
                        ".forEach((c, i) => { if (i < 5) c.checked = false; });"
                        "document.querySelectorAll('.sec-props input')[0]"
                        ".dispatchEvent(new Event('change'));"
                        "document.querySelector('.sec-apply').click();"
                        "setTimeout(() => document.querySelectorAll('.sec-gh')[1]"
                        ".scrollIntoView({block: 'start'}), 300)",
                    )
                if want("wizard"):
                    shot(
                        "wizard",
                        theme,
                        "wizard.html",
                        "wizard",
                        600,
                        680,
                        1,
                        wait=2.4,
                        hold=1,
                        p="0.42",
                    )
                    shot(
                        "wizard_done",
                        theme,
                        "wizard.html",
                        "wizard",
                        600,
                        680,
                        1,
                        wait=2.4,
                        hold=1,
                        p="1",
                        js="micnaga.wizard && document.getElementById('wiz').setAttribute("
                        "'data-state', 'done')",
                    )
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
    args = sys.argv[1:]
    sel: set[str] | None = None
    if "--only" in args:
        i = args.index("--only")
        sel = set(args[i + 1].split(","))
        del args[i : i + 2]
    target = Path(args[0]) if args else REPO / "docs" / "screenshots"
    for s in run(target, sel):
        print(s)
