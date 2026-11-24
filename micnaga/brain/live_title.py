"""Titulo provisional en directo (design §4.4): hacia el minuto 2 de una grabacion CONFIRMADA.

Se aparta de SPEC §1.2 («nada de IA en directo»), con estas salvaguardas:

* una sola vez por grabacion, solo si esta confirmada (``capture.json -> confirmed``) y
  ``secretaria.enabled`` y ``secretaria.live_title``;
* el texto de los primeros ``SECONDS`` s sale de la **transcripcion en vivo** que ya esta
  en disco (``work/live/asr/``): sin cargar un segundo modelo (Whisper turbo pesa ~2 GB). Con
  el vivo activo se espera a que haya texto (hasta ``GIVE_UP_S``); solo sin vivo se transcribe
  en **otro proceso** (``micnaga brain live-asr``) con prioridad BELOW_NORMAL y ``THREADS``
  hilos; la captura (otro hilo del proceso de la app) no comparte CPU con modelos;
* se salta con el ahorro de bateria activo o la CPU ocupada (> ``CPU_BUSY`` %), reintentando
  hasta ``GIVE_UP_S``;
* si algo falla no pasa nada: queda el titulo de hoy.

El titulo se muestra en la pastilla (al pasar el raton) y se guarda en
``<sesion>/titulo_provisional.json``; la Secretaria lo usa como titulo si el definitivo falla y
el actual es generico. El hijo informa de su coste (CPU-segundos, pico de memoria).
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from micnaga import paths
from micnaga.brain import acta as acta_io

log = logging.getLogger(__name__)

START_AFTER_S = 120.0
SECONDS = 90.0
THREADS = 2
CPU_BUSY = 70.0
GIVE_UP_S = 420.0
POLL_S = 5.0
CHILD_TIMEOUT_S = 180.0
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_NO_WINDOW = 0x08000000
BYTES_PER_S = 16000 * 2


def live_text(session: Path, seconds: float = SECONDS) -> str:
    """Texto de los bloques de la transcripcion en vivo que empiezan antes de ``seconds``.

    Los bloques son ``work/live/asr/<pista>_<ini_ms>_<fin_ms>.json`` (listas de palabras). Vacio
    si no hay vivo o aun no hay bloques estables."""
    folder = paths.work_dir(session) / "live" / "asr"
    pieces: list[tuple[float, str, str]] = []
    try:
        files = sorted(folder.glob("*.json"))
    except OSError:
        return ""
    for file in files:
        track, _, rest = file.stem.partition("_")
        start_ms = rest.split("_", 1)[0]
        if track not in ("mic", "system") or not start_ms.isdigit():
            continue
        start = int(start_ms) / 1000.0
        if start >= seconds:
            continue
        try:
            words = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        text = " ".join(str(w.get("w", "")).strip() for w in words if isinstance(w, dict))
        text = " ".join(text.split())
        if text:
            pieces.append((start, "Yo" if track == "mic" else "Otros", text))
    pieces.sort()
    lines = [f"[{int(a) // 60:02d}:{int(a) % 60:02d}] {who}: {t}" for a, who, t in pieces]
    return "\n".join(lines)


def recorded_seconds(session: Path) -> float:
    """Segundos ya escritos en disco (por tamano del WAV, no por su cabecera)."""
    best = 0.0
    for name in (paths.AUDIO_REMOTE_WAV, paths.AUDIO_ME_WAV):
        with contextlib.suppress(OSError):
            best = max(best, max(0, (session / name).stat().st_size - 44) / BYTES_PER_S)
    return best


def is_confirmed(session: Path) -> bool:
    try:
        data = json.loads((session / paths.CAPTURE_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(data.get("confirmed")) if isinstance(data, dict) else False


class _PowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def battery_saver_on() -> bool:
    try:
        st = _PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return False
        return bool(st.SystemStatusFlag == 1)
    except Exception:
        return False


def cpu_busy(threshold: float = CPU_BUSY) -> bool:
    try:
        import psutil

        return float(psutil.cpu_percent(interval=1.0)) > threshold
    except Exception:
        return False


def child_command(session: Path, seconds: float, threads: int) -> list[str]:
    args = ["brain", "live-asr", str(session), "--seconds", str(seconds), "--threads", str(threads)]
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        cli = exe.with_name("micnaga-cli.exe")
        return [str(cli if cli.is_file() else exe), *args]
    return [sys.executable, "-m", "micnaga", *args]


def child_env(threads: int) -> dict[str, str]:
    env = dict(os.environ)
    value = str(max(1, threads))
    for var in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        env[var] = value
    env["OMP_WAIT_POLICY"] = "PASSIVE"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["PYTHONUTF8"] = "1"
    return env


def run_asr_child(
    session: Path,
    *,
    seconds: float = SECONDS,
    threads: int = THREADS,
    timeout_s: float = CHILD_TIMEOUT_S,
) -> dict[str, Any]:
    """Lanza el hijo de ASR y devuelve su JSON (texto + coste medido)."""
    started = time.monotonic()
    proc = subprocess.run(
        child_command(session, seconds, threads),
        capture_output=True,
        env=child_env(threads),
        timeout=timeout_s,
        creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    out = proc.stdout.decode("utf-8", "replace").strip().splitlines()
    if proc.returncode != 0 or not out:
        raise RuntimeError(f"live asr failed (exit {proc.returncode})")
    data: dict[str, Any] = json.loads(out[-1])
    data["parent_wall_s"] = round(time.monotonic() - started, 2)
    return data


def _calendar_title(session: Path) -> bool:
    try:
        data = json.loads((session / "contexto_google.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("titulo"))


# ============================================================================== hijo


def transcribe_head(session: Path, seconds: float, threads: int) -> dict[str, Any]:
    """Proceso hijo: VAD + ASR de los primeros ``seconds`` de las dos pistas."""
    import numpy as np

    from micnaga.config import load_config
    from micnaga.pipeline import vad
    from micnaga.pipeline.engines.asr import create_asr

    t0 = time.monotonic()
    cfg = load_config()
    cfg.processing.threads = threads
    sr = cfg.capture.sample_rate
    pieces: list[tuple[float, str, str]] = []
    engine = create_asr(cfg)
    try:
        for name, who in ((paths.AUDIO_ME_WAV, "Yo"), (paths.AUDIO_REMOTE_WAV, "Otros")):
            file = session / name
            if not file.is_file():
                continue
            with file.open("rb") as fh:
                fh.seek(44)
                raw = fh.read(int(seconds * sr) * 2)
            raw = raw[: len(raw) // 2 * 2]
            audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            if len(audio) < sr:
                continue
            regions = vad.detect_speech(audio, sr, config=cfg)
            for a, b in vad.group_regions(regions, cfg.vad.max_block_s):
                chunk = audio[int(a * sr) : int(b * sr)]
                if len(chunk) < sr // 10:
                    continue
                text = " ".join(w.w.strip() for w in engine.transcribe(chunk, sr) if w.w.strip())
                if text:
                    pieces.append((a, who, text))
    finally:
        with contextlib.suppress(Exception):
            engine.close()
        vad.release_model()
    pieces.sort()
    lines = [f"[{int(a) // 60:02d}:{int(a) % 60:02d}] {who}: {text}" for a, who, text in pieces]
    stats: dict[str, Any] = {"wall_s": round(time.monotonic() - t0, 2)}
    try:
        import psutil

        p = psutil.Process()
        ct = p.cpu_times()
        mem = p.memory_info()
        stats["cpu_s"] = round(ct.user + ct.system, 2)
        stats["peak_rss_mb"] = round(getattr(mem, "peak_wset", mem.rss) / 2**20, 1)
    except Exception:
        pass
    return {"text": "\n".join(lines), **stats}


# ============================================================================== padre


class LiveTitler:
    """Hilo que vigila la grabacion en curso (sin tocar el controlador: solo su carpeta)."""

    def __init__(
        self,
        config: Callable[[], Any],
        session_dir: Callable[[], Path | None],
        on_title: Callable[[str], None],
        *,
        asr: Callable[[Path], dict[str, Any]] | None = None,
        ask_title: Callable[[str], str | None] | None = None,
        battery_saver: Callable[[], bool] = battery_saver_on,
        busy: Callable[[], bool] = cpu_busy,
        poll_s: float = POLL_S,
        start_after_s: float = START_AFTER_S,
    ) -> None:
        self._config = config
        self._session_dir = session_dir
        self._on_title = on_title
        self._asr = asr or run_asr_child
        self._ask_title = ask_title or self._default_ask
        self._battery_saver = battery_saver
        self._busy = busy
        self.poll_s = poll_s
        self.start_after_s = start_after_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._done: set[str] = set()
        self.last_stats: dict[str, Any] | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, name="micnaga-live-title", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)

    def _enabled(self) -> bool:
        s = self._config().secretaria
        return bool(s.enabled and s.live_title)

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.check()
            except Exception:
                log.exception("live title check failed")

    def check(self) -> str | None:
        """Una comprobacion (los tests la llaman directamente)."""
        session = self._session_dir()
        if session is None or not self._enabled() or session.name in self._done:
            return None
        if _calendar_title(session):  # el titulo ya vino de Google Calendar
            self._done.add(session.name)
            return None
        recorded = recorded_seconds(session)
        if recorded < self.start_after_s or not is_confirmed(session):
            return None
        if recorded > GIVE_UP_S:
            self._done.add(session.name)
            return None
        if self._battery_saver() or self._busy():
            log.info("live title postponed (battery saver or busy CPU)")
            return None
        live = live_text(session) if self._config().processing.live else ""
        if self._config().processing.live and len(live) < 40:
            return None  # el vivo aun no tiene texto: se espera (sin cargar otro modelo)
        self._done.add(session.name)
        stats = {"text": live, "source": "live"} if live else self._asr(session)
        self.last_stats = {k: v for k, v in stats.items() if k != "text"}
        log.info("live title ASR: %s", self.last_stats)
        text = str(stats.get("text") or "").strip()
        if len(text) < 40:
            return None
        title = self._ask_title(text)
        if not title:
            return None
        if self._session_dir() == session:  # sigue siendo la misma grabacion
            self._on_title(title)
        with contextlib.suppress(OSError):
            acta_io.write_live_title(session, title, asr=self.last_stats)
        return title

    def _default_ask(self, text: str) -> str | None:
        from micnaga.brain.claude import ClaudeRequest, ClaudeRunner
        from micnaga.brain.prompt import build_title_input, system_prompt
        from micnaga.brain.schema import TITLE_SCHEMA, clean_title, confidence
        from micnaga.glossary import Glossary

        cfg = self._config()
        runner = ClaudeRunner(auth=cfg.secretaria.auth)
        res = runner.run(
            ClaudeRequest(
                prompt=build_title_input(text, glossary=Glossary.load(config=cfg).prompt()),
                system_prompt=system_prompt("titulo"),
                model=cfg.secretaria.model_title,
                schema=TITLE_SCHEMA,
                max_turns=3,
                timeout_s=60,
            )
        )
        data = res.structured or {}
        if confidence(data.get("confianza")) < 0.4:
            return None
        return clean_title(data.get("titulo")) or None
