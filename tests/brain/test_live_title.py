"""Titulo provisional en directo: cuando se lanza, salvaguardas y donde queda."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from micnaga import paths
from micnaga.brain import acta as acta_io
from micnaga.brain import live_title as lt
from micnaga.config import Config


def session(tmp_path: Path, seconds: float, confirmed: bool) -> Path:
    s = tmp_path / "2026-11-19_1200_reunion"
    s.mkdir(exist_ok=True)
    for name in (paths.AUDIO_ME_WAV, paths.AUDIO_REMOTE_WAV):
        (s / name).write_bytes(b"\0" * (44 + int(seconds * lt.BYTES_PER_S)))
    (s / paths.CAPTURE_JSON).write_text(json.dumps({"confirmed": confirmed}), encoding="utf-8")
    return s


class Rig:
    def __init__(self, path: Path | None, *, enabled: bool = True) -> None:
        self.cfg = Config()
        self.cfg.secretaria.enabled = enabled
        self.cfg.processing.live = False  # sin vivo: transcribe el hijo (``asr``)
        self.path = path
        self.titles: list[str] = []
        self.asr_calls = 0
        self.saver = False
        self.busy = False

        def asr(_s: Path) -> dict[str, Any]:
            self.asr_calls += 1
            return {
                "text": "Yo: hola Carlos, revisamos el presupuesto del CRM de la clínica " * 2,
                "cpu_s": 12.0,
                "peak_rss_mb": 950.0,
            }

        self.titler = lt.LiveTitler(
            lambda: self.cfg,
            lambda: self.path,
            self.titles.append,
            asr=asr,
            ask_title=lambda text: "Dental Triana · presupuesto CRM",
            battery_saver=lambda: self.saver,
            busy=lambda: self.busy,
        )


def test_only_once_after_two_minutes_of_a_confirmed_recording(tmp_path: Path) -> None:
    early = session(tmp_path, 60, True)
    rig = Rig(early)
    assert rig.titler.check() is None and rig.asr_calls == 0  # aun no
    s = session(tmp_path, 125, False)
    assert rig.titler.check() is None and rig.asr_calls == 0  # provisional: no
    s = session(tmp_path, 125, True)
    rig.saver = True
    assert rig.titler.check() is None and rig.asr_calls == 0  # ahorro de bateria
    rig.saver, rig.busy = False, True
    assert rig.titler.check() is None and rig.asr_calls == 0  # CPU ocupada
    rig.busy = False
    assert rig.titler.check() == "Dental Triana · presupuesto CRM"
    assert rig.titles == ["Dental Triana · presupuesto CRM"]
    assert acta_io.read_live_title(s) == "Dental Triana · presupuesto CRM"
    assert rig.titler.last_stats == {"cpu_s": 12.0, "peak_rss_mb": 950.0}
    assert rig.titler.check() is None and rig.asr_calls == 1  # una sola vez


def test_disabled_or_too_late_does_nothing(tmp_path: Path) -> None:
    s = session(tmp_path, 125, True)
    off = Rig(s, enabled=False)
    assert off.titler.check() is None and off.asr_calls == 0
    off.cfg.secretaria.enabled, off.cfg.secretaria.live_title = True, False
    assert off.titler.check() is None
    late = Rig(session(tmp_path, lt.GIVE_UP_S + 10, True))
    assert late.titler.check() is None and late.asr_calls == 0
    none = Rig(None)
    assert none.titler.check() is None


def test_recorded_seconds_come_from_the_file_size(tmp_path: Path) -> None:
    s = session(tmp_path, 90.5, True)
    assert lt.recorded_seconds(s) == 90.5
    assert lt.recorded_seconds(tmp_path / "nope") == 0.0


def test_child_command_runs_this_package(tmp_path: Path) -> None:
    cmd = lt.child_command(tmp_path, 90, 2)
    assert cmd[1:4] == ["-m", "micnaga", "brain"] and cmd[-4:] == [
        "--seconds",
        "90",
        "--threads",
        "2",
    ]
    env = lt.child_env(2)
    assert env["OMP_NUM_THREADS"] == "2" and env["PYTHONPATH"]


def live_block(s: Path, track: str, a_ms: int, b_ms: int, text: str) -> None:
    folder = paths.work_dir(s) / "live" / "asr"
    folder.mkdir(parents=True, exist_ok=True)
    data = [{"w": w, "s": 0.0, "e": 0.1, "p": 1.0} for w in text.split(" ")]
    (folder / f"{track}_{a_ms}_{b_ms}.json").write_text(json.dumps(data), encoding="utf-8")


def test_with_live_processing_reuses_live_text_and_never_loads_a_second_model(
    tmp_path: Path,
) -> None:
    s = session(tmp_path, 130.0, confirmed=True)
    rig = Rig(s)
    rig.cfg.processing.live = True
    asked: list[str] = []
    rig.titler._ask_title = lambda text: asked.append(text) or "Presupuesto CRM"  # type: ignore[method-assign]
    assert rig.titler.check() is None  # el vivo aun no tiene texto: espera, sin ASR propio
    assert rig.asr_calls == 0
    live_block(s, "mic", 1000, 9000, "hola Carlos revisamos el presupuesto del CRM")
    live_block(s, "system", 9500, 20000, "si la clinica quiere tres sedes conectadas")
    live_block(s, "mic", 95000, 99000, "fuera")  # empieza despues de SECONDS: no entra
    assert rig.titler.check() == "Presupuesto CRM"
    assert rig.asr_calls == 0
    assert asked == [
        "[00:01] Yo: hola Carlos revisamos el presupuesto del CRM\n"
        "[00:09] Otros: si la clinica quiere tres sedes conectadas"
    ]
