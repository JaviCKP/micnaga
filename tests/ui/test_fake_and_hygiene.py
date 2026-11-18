"""Backend falso/demo y higiene de imports de la UI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from micnaga import paths
from micnaga.store.db import Database
from micnaga.types import Transcript
from micnaga.ui import fake
from micnaga.ui.contracts import ModelProgress


def test_demo_library_is_indexed_searchable_and_has_synthetic_flac(tmp_path: Path) -> None:
    root = tmp_path / "m"
    folders = fake.build_demo_library(root)
    assert len(folders) == 5
    with Database(root / "micnaga.db") as db:
        assert len(db.list_meetings()) == 5
        assert db.search("proveedor")
    first = root / folders[0]
    assert (first / paths.AUDIO_ME_FLAC).stat().st_size > 1000 and (
        first / paths.AUDIO_REMOTE_FLAC
    ).is_file()
    tr = Transcript.from_json((first / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8"))
    assert len(tr.markers) == 2 and tr.meeting.duration_s == 2820
    starts = [s.start for s in tr.segments]
    assert starts == sorted(starts) and tr.segments[-1].end <= tr.meeting.duration_s


def test_demo_audio_is_decodable_flac_with_the_right_duration(tmp_path: Path) -> None:
    import soundfile as sf

    root = tmp_path / "m"
    folders = fake.build_demo_library(root)
    info = sf.info(str(root / folders[0] / paths.AUDIO_REMOTE_FLAC))
    assert info.format == "FLAC" and abs(info.duration - 2820) < 1


def test_fake_levels_stay_in_range_and_move() -> None:
    t = [0.0]
    lv = fake.FakeLevels(clock=lambda: t[0])
    seen = set()
    for i in range(200):
        t[0] = i * 0.05
        m, s = lv()
        assert 0 <= m <= 1 and 0 <= s <= 1
        seen.add(round(m, 2))
    assert len(seen) > 20


def test_fake_download_reports_every_model_verified() -> None:
    events: list[ModelProgress] = []
    fake.fake_download_models(events.append, speed=200.0)
    names = {e.name for e in events}
    assert len(names) == 3
    assert {e.name for e in events if e.state == "verified"} == names
    assert all(e.state != "verified" or e.done == e.total for e in events)


def test_fake_ops_rename_merge_and_voiceprints(tmp_path: Path) -> None:
    root = tmp_path / "m"
    folders = fake.build_demo_library(root, audio=False)
    ops = fake.FakeLibraryOps(lambda: root)
    ops.rename_speaker(folders[0], "S2", "Marcos")
    ops.merge_speakers(folders[0], "S1", "S2")
    ops.save_voiceprint(folders[0], "S1")
    assert [v.name for v in ops.list_voiceprints()] == ["Laura"]
    ops.delete_voiceprint("Laura")
    assert ops.list_voiceprints() == []
    tr = Transcript.from_json(
        (root / folders[0] / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8")
    )
    assert [s.id for s in tr.speakers] == ["me", "S1"]


def test_importing_the_ui_package_does_not_load_gui_libraries() -> None:
    """webview / pystray / windows_toasts / PIL se importan al usarse, no al importar."""
    code = (
        "import sys\n"
        "import micnaga.ui.contracts, micnaga.ui.bridge, micnaga.ui.host, micnaga.ui.winapi, micnaga.ui.media\n"  # noqa: E501
        "import micnaga.ui.startup, micnaga.ui.i18n, micnaga.ui.settings_schema, micnaga.ui.windows\n"  # noqa: E501
        "import micnaga.ui.tray, micnaga.ui.toast, micnaga.ui.sound, micnaga.ui.icons, micnaga.ui.fake\n"  # noqa: E501
        "bad = [m for m in ('webview', 'pystray', 'windows_toasts', 'PIL', 'torch', 'diarize') if m in sys.modules]\n"  # noqa: E501
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


def test_demo_module_is_safe_by_construction() -> None:
    text = (Path(__file__).resolve().parents[2] / "micnaga" / "ui" / "demo.py").read_text(
        encoding="utf-8"
    )
    assert "set_default_backend(startup.MemoryRegistry())" in text  # jamas el Run real
    assert 'MICNAGA_UI_DEMO"] = "1"' in text and "MICNAGA_HOME" in text and "rmtree" in text
