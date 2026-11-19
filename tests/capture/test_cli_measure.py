"""``micnaga record`` (con audio sintetico), medidor de presupuesto e higiene de imports."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from micnaga import paths
from micnaga.capture import cli
from micnaga.capture.measure import measure
from micnaga.capture.recovery import recover_sessions
from micnaga.capture.session import read_capture_json, write_capture_json
from micnaga.capture.synthetic import demo_backend
from micnaga.cli import main as micnaga_main
from micnaga.config import CaptureConfig


def test_record_command_records_and_finalizes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "meetings"
    code = cli.run(["--synthetic", "--seconds", "2", "--out", str(out), "--title", "Prueba Uno"])
    assert code == 0
    (session,) = [d for d in out.iterdir() if d.is_dir()]
    assert session.name.endswith("_prueba-uno")
    assert (session / paths.AUDIO_ME_FLAC).exists() and (session / paths.AUDIO_REMOTE_FLAC).exists()
    assert not list(session.glob("*.wav"))
    meta = read_capture_json(session)
    assert meta.extra["state"] == "finalized" and meta.output_kind == "speakers"
    assert meta.meet_title == "Prueba Uno"  # el titulo llega a capture.json (y de ahi al procesado)
    assert meta.mic.samples > 1.5 * 16000
    text = capsys.readouterr().out
    assert "recording to" in text and "duration:" in text and "drift=" in text


def test_record_through_the_main_dispatcher(tmp_path: Path) -> None:
    out = tmp_path / "meetings"
    code = micnaga_main(
        ["record", "--synthetic", "--seconds", "1", "--out", str(out), "--no-finalize"]
    )
    assert code == 0
    (session,) = [d for d in out.iterdir() if d.is_dir()]
    assert (session / paths.AUDIO_ME_WAV).exists()  # --no-finalize deja los WAV


def test_record_without_title_leaves_meet_title_empty(tmp_path: Path) -> None:
    out = tmp_path / "meetings"
    assert cli.run(["--synthetic", "--seconds", "1", "--out", str(out), "--no-finalize"]) == 0
    (session,) = [d for d in out.iterdir() if d.is_dir()]
    assert session.name.endswith("_grabacion")
    assert read_capture_json(session).meet_title is None
    out2 = tmp_path / "blank"
    assert cli.run(["--synthetic", "--seconds", "1", "--out", str(out2), "--title", "  "]) == 0
    (blank,) = [d for d in out2.iterdir() if d.is_dir()]
    assert blank.name.endswith("_grabacion") and read_capture_json(blank).meet_title is None


def test_record_defaults_to_the_meetings_root(tmp_path: Path, tmp_storage: Path) -> None:
    assert cli.run(["--synthetic", "--seconds", "1", "--no-finalize"]) == 0
    assert len(list(tmp_storage.iterdir())) == 1  # MICNAGA_STORAGE (nunca Documentos reales)


def test_record_recover_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "meetings"
    assert cli.run(["--recover", "--out", str(out)]) == 0
    assert "nothing to recover" in capsys.readouterr().out
    cli.run(["--synthetic", "--seconds", "1", "--out", str(out), "--no-finalize"])
    (session,) = [d for d in out.iterdir() if d.is_dir()]
    meta = read_capture_json(session)
    write_capture_json(
        session,
        meta.__class__(
            **{**meta.__dict__, "extra": {**meta.extra, "state": "recording", "pid": 2**22 + 99}}
        ),
    )
    assert cli.run(["--recover", "--out", str(out)]) == 0
    assert f"recovered: {session}" in capsys.readouterr().out
    assert recover_sessions(out) == []


def test_level_bar() -> None:
    assert cli.level_bar(0.0) == ".........."
    assert cli.level_bar(1.0) == "##########"
    assert cli.level_bar(0.01).count("#") == 3  # -40 dB


def test_measure_reports_cpu_and_ram(tmp_path: Path) -> None:
    m = measure(3.0, demo_backend(), tmp_path / "s", CaptureConfig())
    print(m.report())
    assert m.cpu_pct_core >= 0 and m.ram_private_mb > 0
    assert m.cpu_pct_total < 3.0, m.report()  # presupuesto de CPU (spec seccion 2)


def test_importing_capture_does_not_load_windows_or_heavy_modules() -> None:
    code = (
        "import sys\n"
        "import micnaga.capture, micnaga.capture.cli, micnaga.capture.sync\n"
        "import micnaga.capture.recovery, micnaga.capture.recorder\n"
        "bad = [m for m in ('pyaudiowpatch', 'comtypes', 'pycaw', 'torch', 'faster_whisper')\n"
        "       if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""
