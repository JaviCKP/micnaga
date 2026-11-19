# ruff: noqa: E501  (guion de capturas: cadenas de JS largas)
"""Capturas de la Secretaria en ventanas REALES (pywebview + WebView2) con ``PrintWindow``.

``python -m tests.ui.real_shots [carpeta] [--only acta,review,refine,plan,applied,live,horas,settings]``

Una instancia aislada y sintetica, nunca la app del usuario:

* ``MICNAGA_HOME`` / ``MICNAGA_STORAGE`` / ``MICNAGA_MODELS`` / ``APPDATA`` en una carpeta temporal,
  ``MICNAGA_SECRETS_FILE`` (jamas el Administrador de credenciales) y el registro «Arrancar con
  Windows» en memoria. Sin bandeja, sin atajos, sin vigilante: solo las ventanas de la biblioteca y
  de Ajustes, con los puentes Python reales (``LibraryApi``, ``SettingsApi``, ``BrainService``).
* Odoo FALSO (``tests/brain/fake_odoo.py``) y un ``claude`` falso en proceso: responde despues de una
  pausa corta y avisa de las consultas como lo haria el real (la latencia real se mide en
  ``tests/brain/test_real_refine.py``). Ninguna escritura sale del equipo.
* Se captura solo el HWND de cada ventana de Micnaga (``PrintWindow`` con
  ``PW_RENDERFULLCONTENT``), recortado al area cliente; nunca el escritorio.

Salida: ``docs/screenshots/secretaria_ux_<vista>_<tema>.png``.
"""

from __future__ import annotations

import contextlib
import copy
import ctypes
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "docs" / "screenshots"
LIB_SIZE = (1180, 900)
SET_SIZE = (820, 960)
PROJECT_REGULAI = 12


# ============================================================================== claude falso


class ScriptedRunner:
    """Hace de ``claude -p`` para las capturas: pausa, avisa de las consultas, responde."""

    def __init__(self, today: date) -> None:
        self.today = today

    def _thursday(self) -> str:
        d = self.today + timedelta(days=1)
        while d.weekday() != 3:
            d += timedelta(days=1)
        return d.isoformat()

    def run_once(self, req: Any, on_event: Callable[[Any], None] | None = None) -> Any:
        from micnaga.brain.claude import ClaudeResult, ToolCall, ToolResult

        def tool(name: str, arg: dict[str, Any], pause: float) -> None:
            time.sleep(pause)
            if on_event is not None:
                on_event(ToolCall(name, arg))

        prompt = req.prompt
        if prompt.startswith("# Corregir una propuesta"):
            item = json.loads(prompt.split("## La propuesta\n\n", 1)[1].split("\n", 1)[0])
            tool("mcp__micnaga__proyectos", {"texto": "RegulAI"}, 0.7)
            time.sleep(2.0)
            if item["tipo"] == "crear_tarea":
                out = {"fecha": self._thursday(), "proyecto_id": PROJECT_REGULAI}
            elif item["tipo"] == "crear_evento":
                out = {"hora_inicio": "11:30"}
            else:
                out = {}
            structured: dict[str, Any] = {"cambiado": bool(out), "nota": "", "datos": out}
        else:
            tool("mcp__micnaga__tareas", {"texto": "plantillas"}, 0.6)
            tool("mcp__micnaga__proyectos", {"texto": "Formación"}, 0.9)
            time.sleep(1.4)
            structured = {
                "nota": "",
                "cambiar": [],
                "quitar": [],
                "anadir": [
                    {
                        "tipo": "crear_tarea",
                        "titulo": "Tarea",
                        "motivo": "Lo pidió el usuario",
                        "seg": 1,
                        "datos": {
                            "proyecto_id": 5,
                            "nombre": "Revisar plantillas de ejercicios",
                            "fecha": (self.today + timedelta(days=4)).isoformat(),
                        },
                    }
                ],
            }
        return ClaudeResult(
            structured=structured,
            text="",
            cost_usd=0.0,
            usage={},
            duration_s=2.7,
            num_turns=3,
            model="claude-sonnet",
            tool_results=[ToolResult("t", "mcp__micnaga__proyectos", False, "")],
        )

    def run(self, req: Any, on_event: Callable[[Any], None] | None = None) -> Any:
        return self.run_once(req, on_event)

    def auth_status(self, timeout_s: float = 20.0) -> dict[str, Any]:
        return {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}


# ============================================================================== datos


def seed(tmp: Path) -> dict[str, Any]:
    from micnaga import paths
    from micnaga.brain import acta as acta_io
    from micnaga.brain import capabilities, learn, plan, secrets
    from micnaga.brain import store as bstore
    from micnaga.brain.google import detect
    from micnaga.brain.google.analysis import refresh
    from micnaga.brain.odoo import OdooClient, OdooConfig
    from micnaga.config import Config
    from micnaga.store.db import Database
    from micnaga.ui import fake
    from tests.brain.demo import write_demo_meeting, write_followup_meeting
    from tests.brain.fake_odoo import DB, KEY, LOGIN, FakeOdoo

    cfg = Config()
    cfg.privacy.voiceprints_enabled = True
    s = cfg.secretaria
    s.enabled = True
    srv = FakeOdoo().start()
    srv.access.update(
        {
            "crm.lead": set(),
            "res.partner": {"read"},
            "project.project": {"read"},
            "crm.stage": {"read"},
        }
    )
    srv.data["project.project"].append(
        {
            "id": PROJECT_REGULAI,
            "name": "RegulAI",
            "active": True,
            "allow_timesheets": True,
            "partner_id": False,
        }
    )
    s.odoo_url, s.odoo_db, s.odoo_login = srv.url, DB, LOGIN
    s.google_gmail = s.google_calendar = s.google_drive = True
    secrets.put(secrets.ODOO_KEY, KEY)
    root = paths.meetings_root(cfg)
    folders = fake.build_demo_library(root)
    write_demo_meeting(root)

    def odoo_factory(_cfg: object) -> OdooClient:
        return OdooClient(OdooConfig(srv.url, DB, LOGIN, KEY))

    oc = odoo_factory(cfg)
    oc.authenticate()
    profile = capabilities.discover(oc)
    profile["key"] = capabilities.connection_key(s.odoo_url, s.odoo_db, s.odoo_login)
    capabilities.save(profile)
    detect.write_cache(
        {
            "services": {"gmail": "connected", "calendar": "connected", "drive": "connected"},
            "checked": time.time() - 3600,
            "foreign": [],
        }
    )
    learn.add_learnings(
        [
            {
                "hecho": "Las tareas de formación van en «Formación Ayuntamiento».",
                "fuente": "crm",
                "evidencia": "project.project",
                "confianza": 0.9,
            },
            {
                "hecho": "Lucía Pérez coordina a los técnicos del ayuntamiento.",
                "fuente": "reunion",
                "evidencia": "#1",
                "confianza": 0.8,
            },
        ]
    )

    # la reunion de seguimiento: propuestas de Odoo + Google + investigacion
    name = write_followup_meeting(root, oc, profile)
    folder = root / name
    acta = acta_io.read_acta(folder) or {}
    props = acta["propuestas"]
    prev = sorted(f for f in folders if f != name)[0]
    acta.update(
        contexto="3.ª reunión con el Ayuntamiento; la última, el 12 de noviembre",
        desde_la_ultima=[
            {
                "texto": "Enviar el temario revisado",
                "estado": "hecho",
                "t": 3.0,
                "evidencia": {
                    "tipo": "gmail",
                    "titulo": "Re: temario revisado",
                    "url": "https://mail.google.com/mail/u/0/#all/abc",
                },
            },
            {
                "texto": "Plantillas de ejercicios para técnicos",
                "estado": "abierto",
                "t": None,
                "evidencia": {
                    "tipo": "odoo",
                    "titulo": "Tarea abierta en Odoo",
                    "url": f"{srv.url}/web#id=300&model=project.task&view_type=form",
                },
            },
            {
                "texto": "Confirmar aula para diciembre",
                "estado": "desconocido",
                "t": None,
                "evidencia": None,
            },
        ],
        contrastes=[
            {
                "dicho": "La revisión es el martes a las diez",
                "escrito": "El correo de Lucía dice el martes a las 9:30",
                "fuente": "Gmail",
                "url": "https://mail.google.com/mail/u/0/#all/def",
                "t": 69.0,
            },
        ],
        fuentes=[
            {"tipo": "acta", "titulo": "Acta anterior", "ref": prev},
            {
                "tipo": "gmail",
                "titulo": "Re: temario revisado",
                "url": "https://mail.google.com/mail/u/0/#all/abc",
            },
            {
                "tipo": "odoo",
                "titulo": "Migrar informes de Atlas",
                "url": f"{srv.url}/web#id=300&model=project.task&view_type=form",
            },
            {
                "tipo": "drive",
                "titulo": "Temario 2026",
                "url": "https://docs.google.com/document/d/xyz",
            },
        ],
        decisiones=[{"texto": "Más ejercicios prácticos en las sesiones", "seg": 1, "t": 25.0}],
    )
    ev = {
        "grupo": "google",
        "tipo": "evento_google",
        "motivo": "Se acordó en la reunión",
        "confianza": 0.9,
        "seg": 3,
        "t": 69.0,
        "checked": True,
        "aviso": None,
        "estado": "pendiente",
        "datos": {
            "titulo": "Revisión de materiales",
            "inicio": "2026-11-24T10:00",
            "duracion_min": 45,
            "huecos": [],
            "asistentes": [{"nombre": "Lucía Pérez", "email": "lucia@ayto.example", "on": False}],
            "descripcion": "Revisar el cuaderno de ejercicios.",
            "calendario": "primary",
        },
    }
    mail = {
        "grupo": "google",
        "tipo": "correo_google",
        "motivo": "",
        "confianza": 0.8,
        "seg": None,
        "t": None,
        "checked": True,
        "aviso": None,
        "estado": "pendiente",
        "datos": {
            "modo": "draft",
            "para": ["lucia@ayto.example"],
            "asunto": "Resumen y próximos pasos",
            "cuerpo": "Hola Lucía, …",
        },
    }
    drive = {
        "grupo": "google",
        "tipo": "acta_drive",
        "motivo": "",
        "confianza": 0.5,
        "seg": None,
        "t": None,
        "checked": False,
        "aviso": None,
        "estado": "pendiente",
        "datos": {"titulo": "Acta · Seguimiento de la formación", "carpeta": None},
    }
    for p in (ev, mail, drive):
        refresh(p)
    # Google en vez de crear_evento de Odoo (lo que hace route_odoo_events con Calendar activo)
    props = [p for p in props if p["tipo"] != "crear_evento"]
    acta["propuestas"] = [ev, mail, drive, *props]
    plan.number(acta["propuestas"])
    acta["opciones"] = plan.options(oc, profile, acta["propuestas"])
    acta_io.write_acta(folder, acta)

    # una reunion que se esta analizando (la linea viva con su rastro de fuentes)
    live = next(f for f in folders if "revision-semanal" in f)
    with Database(root / "micnaga.db") as db:
        bstore.ensure_schema(db)
        bstore.enqueue(db, live)
        bstore.claim(db, live)
    return {
        "cfg": cfg,
        "srv": srv,
        "root": root,
        "followup": name,
        "live": live,
        "prev": prev,
        "odoo_factory": odoo_factory,
        "pristine": copy.deepcopy(acta),
    }


# ============================================================================== captura


def _dpi_aware() -> None:
    with contextlib.suppress(Exception):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)


def capture(hwnd: int, path: Path) -> Path:
    """``PrintWindow`` (PW_RENDERFULLCONTENT) de esa ventana, recortada a su area cliente."""
    import win32gui
    import win32ui
    from PIL import Image

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    wdc = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(wdc)
    mem = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    mem.SelectObject(bmp)
    try:
        ok = ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), 2)
        if not ok:
            raise RuntimeError("PrintWindow failed")
        info = bmp.GetInfo()
        im = Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1
        )
        _, _, cw, ch = win32gui.GetClientRect(hwnd)
        sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
        im = im.crop((sx - left, sy - top, sx - left + cw, sy - top + ch))
        path.parent.mkdir(parents=True, exist_ok=True)
        im.save(path)
        return path
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        mfc.DeleteDC()
        win32gui.ReleaseDC(hwnd, wdc)


# ============================================================================== guion


class Driver:
    def __init__(self, win: Any, title: str) -> None:
        self.win = win
        self.title = title

    @property
    def hwnd(self) -> int:
        import win32gui

        return int(win32gui.FindWindow(None, self.title))

    def js(self, code: str) -> Any:
        return self.win.evaluate_js(code)

    def until(self, cond: str, timeout: float = 15.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with contextlib.suppress(Exception):
                if self.js(f"!!({cond})"):
                    return
            time.sleep(0.15)
        raise TimeoutError(cond)

    def shot(self, name: str, theme: str, out: Path, settle: float = 0.5) -> Path:
        time.sleep(settle)
        return capture(self.hwnd, out / f"secretaria_ux_{name}_{theme}.png")


def run(out: Path, only: set[str] | None = None) -> list[Path]:
    _dpi_aware()
    tmp = Path(tempfile.mkdtemp(prefix="micnaga-ux-shots-"))
    os.environ.update(
        MICNAGA_HOME=str(tmp / "home"),
        MICNAGA_STORAGE=str(tmp / "meetings"),
        MICNAGA_MODELS=str(tmp / "models"),
        MICNAGA_SECRETS_FILE=str(tmp / "secrets.json"),
        MICNAGA_UI_DEMO="1",
        APPDATA=str(tmp / "appdata"),
    )
    import webview

    from micnaga.brain import acta as acta_io
    from micnaga.brain.service import BrainService
    from micnaga.store.db import Database
    from micnaga.ui import fake, startup
    from micnaga.ui.bridge import LibraryApi, SettingsApi
    from micnaga.ui.contracts import UiCallbacks
    from micnaga.ui.host import FileConfigStore
    from micnaga.ui.media import LocalServer

    startup.set_default_backend(startup.MemoryRegistry())
    data = seed(tmp)
    cfg, root = data["cfg"], data["root"]
    now = datetime.now()
    runner = ScriptedRunner(now.date())
    brain = BrainService(
        lambda: cfg, odoo_factory=data["odoo_factory"], runner_factory=lambda _c: runner
    )
    brain.now = lambda: now
    ops = fake.FakeLibraryOps(lambda: root)
    store = FileConfigStore()
    store.save(cfg)
    reg = startup.MemoryRegistry()
    srv_ref: list[LocalServer] = []
    lib = LibraryApi(
        db_factory=lambda: Database(root / "micnaga.db"),
        meetings_root=lambda: root,
        audio_url=lambda f, t: srv_ref[0].audio_url(f, t),
        config=lambda: cfg,
        ops=ops,
        opener=lambda p: None,
        brain=brain,
        settings=lambda: settings,
    )
    settings = SettingsApi(
        store=store,
        callbacks=UiCallbacks(),
        registry=reg,
        diagnostics=fake.fake_diagnostics,
        ops=ops,
        brain=brain,
    )
    shots: list[Path] = []
    errors: list[BaseException] = []

    def want(name: str) -> bool:
        return not only or name in only

    server = LocalServer(meetings_root=lambda: root)
    server.__enter__()
    srv_ref.append(server)
    followup, live = data["followup"], data["live"]
    lib_title, set_title = "Micnaga · capturas biblioteca", "Micnaga · capturas ajustes"
    lw = webview.create_window(
        lib_title,
        url=server.page_url("library.html", theme="light"),
        js_api=lib,
        width=LIB_SIZE[0],
        height=LIB_SIZE[1],
        background_color="#f3f3f0",
        text_select=False,
        zoomable=False,
    )
    sw = webview.create_window(
        set_title,
        url=server.page_url("settings.html", theme="light"),
        js_api=settings,
        width=SET_SIZE[0],
        height=SET_SIZE[1],
        background_color="#f3f3f0",
        hidden=not want("settings"),
        text_select=False,
        zoomable=False,
    )

    def reset_acta() -> None:
        acta_io.write_acta(root / followup, copy.deepcopy(data["pristine"]))

    def live_status(stop: threading.Event) -> None:
        steps = [
            {
                "fase": "herramienta",
                "servidor": "micnaga",
                "tool": "reuniones_con",
                "arg": "Lucía Pérez",
            },
            {
                "fase": "herramienta",
                "servidor": "claude_ai_Gmail",
                "tool": "search_threads",
                "arg": None,
            },
            {"fase": "herramienta", "servidor": "micnaga", "tool": "tareas", "arg": None},
        ]
        i = 0
        while not stop.is_set():
            with brain._lock:
                brain._status[live] = steps[min(i, len(steps) - 1)]
            i += 1
            stop.wait(1.3)

    def script() -> None:
        try:
            d = Driver(lw, lib_title)
            d.until(
                "window.micnaga && micnaga.library && document.querySelector('.row[data-folder]')",
                30,
            )
            for theme in ("light", "dark"):
                reset_acta()
                d.js(f"mic.setTheme('{theme}')")
                d.js(f"micnaga.library.open({json.dumps(followup)})")
                d.until("document.querySelector('.sec-props')")
                d.js("document.querySelector('#tr').scrollTop = 0")
                if want("acta"):
                    shots.append(d.shot("acta", theme, out, 0.9))  # vista «Resumen»
                    d.js("micnaga.views && micnaga.views.set('acta')")
                    shots.append(d.shot("actaview", theme, out, 0.6))
                d.js("micnaga.views && micnaga.views.set('propuestas')")
                d.js("document.querySelector('.sec-props').scrollIntoView({block: 'start'})")
                if want("review"):
                    shots.append(d.shot("review", theme, out, 0.6))
                task = d.js(
                    "(micnaga.secretaria.state().acta.propuestas.find(p => p.tipo === 'crear_tarea') || {}).id"
                )
                if want("refine") and task:
                    d.js(
                        f"document.querySelector('.sec-p[data-id=\"{task}\"]').scrollIntoView({{block: 'center'}})"
                    )
                    d.js(
                        f"micnaga.secretaria.ask('{task}', 'mejor para el jueves y en el proyecto RegulAI')"
                    )
                    shots.append(d.shot("refine_before", theme, out, 0.5))
                    d.js(f"micnaga.secretaria.send('{task}')")
                    time.sleep(1.4)
                    shots.append(d.shot("refine_thinking", theme, out, 0.0))
                    d.until(f"document.querySelector('.sec-p[data-id=\"{task}\"] .lit')", 15)
                    shots.append(d.shot("refine_after", theme, out, 0.35))
                if want("plan"):
                    d.js(
                        "micnaga.secretaria.plan('añade una tarea para revisar las plantillas el lunes')"
                    )
                    d.js("document.querySelector('.sec-bar').scrollIntoView({block: 'end'})")
                    d.js("micnaga.secretaria.sendPlan()")
                    time.sleep(2.3)
                    shots.append(d.shot("plan_thinking", theme, out, 0.0))
                    d.until("document.querySelector('.sec-p.is-new')", 15)
                    d.js(
                        "document.querySelector('.sec-p.is-new').scrollIntoView({block: 'center'})"
                    )
                    shots.append(d.shot("plan", theme, out, 0.5))
                if want("applied"):
                    acta = acta_io.read_acta(root / followup) or {}
                    for p in acta["propuestas"]:  # una que falla: Odoo no respondia
                        if p["tipo"] == "acta_drive":
                            p.update(estado="fallida", error="odoo_offline")
                    acta_io.write_acta(root / followup, acta)
                    d.js(f"micnaga.library.open({json.dumps(data['prev'])})")
                    time.sleep(0.6)
                    d.js(f"micnaga.library.open({json.dumps(followup)})")
                    d.until("document.querySelector('.sec-props')")
                    ids = [
                        p["id"]
                        for p in acta["propuestas"]
                        if p["grupo"] in ("agenda",) and p["estado"] == "pendiente"
                    ]
                    d.js(
                        f"document.querySelectorAll('.sec-cb').forEach(c => {{ c.checked = {json.dumps(ids)}.includes(c.dataset.id); }})"
                    )
                    d.js("document.querySelector('.sec-apply').click()")
                    d.until("document.querySelector('.sec-p[data-state=\"aplicada\"]')", 15)
                    d.js(
                        "document.querySelector('.sec-p[data-state=\"aplicada\"]').scrollIntoView({block: 'center'})"
                    )
                    shots.append(d.shot("applied", theme, out, 0.8))
                if want("live"):
                    stop = threading.Event()
                    th = threading.Thread(target=live_status, args=(stop,), daemon=True)
                    th.start()
                    d.js(f"micnaga.library.open({json.dumps(live)})")
                    d.until("document.querySelector('.sec-live')")
                    time.sleep(3.2)
                    shots.append(d.shot("live", theme, out, 0.0))
                    stop.set()
                if want("horas"):
                    d.js("micnaga.horas.open()")
                    d.until("document.querySelector('.hx-day')", 20)
                    shots.append(d.shot("horas", theme, out, 0.7))
                    d.js("micnaga.horas.close()")
                if want("embedded"):
                    d.js("micnaga.library.showSettings()")
                    d.until(
                        "(() => { const f = document.querySelector('#settings-view iframe');"
                        " return f && f.contentDocument && f.contentDocument.querySelector('.tab'); })()",
                        30,
                    )
                    d.js(
                        "document.querySelector('#settings-view iframe').contentWindow.mic.setTheme("
                        f"'{theme}')"
                    )
                    shots.append(d.shot("settings_embedded", theme, out, 1.0))
                    d.js(
                        "document.querySelector('#settings-view iframe').contentDocument"
                        ".getElementById('nav-back').click()"
                    )
                    d.until("document.getElementById('settings-view').hidden", 5)
                    shots.append(d.shot("settings_back", theme, out, 0.4))
            if want("settings"):
                ds = Driver(sw, set_title)
                ds.until(
                    "document.querySelector('#tab-secretaria, [data-id=secretaria], .tab')", 30
                )
                for theme in ("light", "dark"):
                    ds.js(f"mic.setTheme('{theme}')")
                    ds.js(
                        "(document.querySelector('#tab-secretaria') || [...document.querySelectorAll('.tab')].find(t => /Secretaria/.test(t.textContent))).click()"
                    )
                    ds.until("document.querySelector('.sec-status .sig[data-s=\"ok\"]')", 15)
                    shots.append(ds.shot("settings", theme, out, 1.2))
                    ds.js(
                        "[...document.querySelectorAll('#pane-secretaria .group > h3')]"
                        ".find(h => /Google/i.test(h.textContent))?.scrollIntoView({block: 'start'})"
                    )
                    shots.append(ds.shot("settings_google", theme, out, 0.6))
                    ds.js(
                        "document.querySelector('#pane-secretaria h2').scrollIntoView({block: 'start'})"
                    )
        except BaseException as exc:
            errors.append(exc)
        finally:
            for w in (sw, lw):
                with contextlib.suppress(Exception):
                    w.destroy()

    try:
        webview.start(script, gui="edgechromium", private_mode=True)
    finally:
        server.__exit__(None, None, None)
        data["srv"].stop()
        shutil.rmtree(tmp, ignore_errors=True)
    if errors:
        raise errors[0]
    return shots


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    only = None
    if "--only" in args:
        i = args.index("--only")
        only = set(args[i + 1].split(","))
        del args[i : i + 2]
    out = Path(args[0]) if args else OUT
    for p in run(out, only):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
