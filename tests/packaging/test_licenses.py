"""``scripts/gen_licenses.py`` en modo comprobacion + pruebas de su politica."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gen_licenses.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_licenses", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_licenses"] = module
    spec.loader.exec_module(module)
    return module


gl = _load()


def test_check_mode_passes_on_this_environment() -> None:
    """Ninguna dependencia de ejecucion es GPL/AGPL/desconocida ni LGPL sin revisar."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_check_mode_does_not_write(tmp_path: Path) -> None:
    out = tmp_path / "x.md"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check", "--output", str(out)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0
    assert not out.exists()


def test_closure_excludes_dev_tools_and_includes_runtime_libs() -> None:
    infos, _missing = gl.runtime_closure()
    names = {i.name.lower() for i in infos.values()}
    for wanted in ("numpy", "soxr", "pystray", "onnxruntime", "aec-audio-processing", "pyaec"):
        assert wanted in names
    for dev in ("pytest", "ruff", "mypy", "pyinstaller", "jiwer", "pyannote-metrics"):
        assert dev not in names


def test_committed_licence_file_exists_and_has_required_sections() -> None:
    text = (ROOT / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8")
    assert text.index("Resumen de obligaciones LGPL") < text.index("## Dependencias de Python")
    for needle in (
        "Parakeet TDT 0.6B v3",
        "CC-BY-4.0",
        "WeSpeaker",
        "Silero VAD",
        "faster-whisper small",
        "community-1",
        "SpeexDSP",
        "WebRTC",
        "soxr",
        "pystray",
        "libsndfile",
    ):
        assert needle in text, needle


@pytest.mark.parametrize(
    ("expression", "category"),
    [
        ("MIT", "permissive"),
        ("BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0", "permissive"),
        ("Apache-2.0 OR BSD-2-Clause", "permissive"),
        ("Apache-2.0 OR GPL-3.0-only", "permissive"),  # se puede elegir la permisiva
        ("MIT AND GPL-3.0-only", "blocked"),
        ("MPL-2.0 AND MIT", "weak"),
        ("LGPL-2.1-or-later", "lgpl"),
        ("GPL-3.0-or-later WITH GCC-exception-3.1", "blocked"),
        ("AGPL-3.0-only", "blocked"),
        ("Some-Proprietary-1.0", "unknown"),
        ("(MIT OR Apache-2.0) AND BSD-3-Clause", "permissive"),
    ],
)
def test_expression_category(expression: str, category: str) -> None:
    assert gl.expression_category(expression) == category


@pytest.mark.parametrize(
    ("text", "spdx"),
    [
        ("MIT License", "MIT"),
        ("BSD 3-Clause License  Copyright (c) 2014", "BSD-3-Clause"),
        ("Apache licensed, as found in the LICENSE file", "Apache-2.0"),
        ("Apache 2.0", "Apache-2.0"),
        ("LGPLv3", "LGPL-3.0-or-later"),
        ("PSF", "PSF-2.0"),
        ("GNU General Public License v3", "GPL-3.0-or-later"),
    ],
)
def test_normalize_freeform(text: str, spdx: str) -> None:
    assert gl.normalize(text) == spdx


def test_sniff_license_texts() -> None:
    assert gl.sniff_text("GNU LESSER GENERAL PUBLIC LICENSE\n Version 3, 29 June 2007") == (
        "LGPL-3.0-or-later"
    )
    assert (
        gl.sniff_text(
            "Permission is hereby granted, free of charge, to any person obtaining a copy"
        )
        == "MIT"
    )
    assert gl.sniff_text("kaldiio SOFTWARE LICENSE AGREEMENT FOR EVALUATION") is None


def test_unreviewed_lgpl_and_unknown_block() -> None:
    infos = {
        "newlib": gl.DistInfo("newlib", "1.0", "LGPL-2.1-or-later", "x", "lgpl-unreviewed"),
        "mystery": gl.DistInfo("mystery", "1.0", "UNKNOWN", "x", "unknown"),
        "fine": gl.DistInfo("fine", "1.0", "MIT", "x", "permissive"),
        "gpl": gl.DistInfo("gpl", "1.0", "GPL-3.0-only", "x", "blocked"),
        "stub": gl.DistInfo("stub", "1.0", "x", "x", "not-bundled"),
    }
    problems = gl.blockers(infos)
    assert len(problems) == 3
    assert not any("fine" in p or "stub" in p for p in problems)


def test_reviewed_lgpl_entries_cover_the_closure() -> None:
    infos, _ = gl.runtime_closure()
    index = gl._installed_index()
    gl.classify_all(infos, index)
    lgpl_names = {k for k, i in infos.items() if i.category == "lgpl"}
    assert lgpl_names <= set(gl.LGPL_REVIEWED)
    for key in lgpl_names:
        review = gl.LGPL_REVIEWED[key]
        assert {"licence", "linking", "why", "source"} <= set(review)


def test_known_findings_in_this_environment() -> None:
    """Hallazgos verificados al escribir el script (si el entorno cambia, revisar)."""
    infos, _ = gl.runtime_closure()
    gl.classify_all(infos, gl._installed_index())
    assert infos["soxr"].category == "lgpl"
    assert infos["pystray"].category == "lgpl"
    assert infos["kaldiio"].category == "not-bundled"  # licencia de solo evaluacion (NTT)
    assert infos["av"].category == "not-bundled"  # FFmpeg con x264/x265 (GPL)
    assert infos["pyaec"].expression == "MIT" and "MANUAL" in infos["pyaec"].source
    assert infos["torch"].category == "permissive"


def test_scan_dist_flags_forbidden_binaries(tmp_path: Path) -> None:
    (tmp_path / "ok.dll").write_bytes(b"")
    (tmp_path / "libx264-165-abc.dll").write_bytes(b"")
    (tmp_path / "cudnn64_9.dll").write_bytes(b"")
    problems = gl.scan_dist(tmp_path)
    assert len(problems) == 2
    assert not any("ok.dll" in p for p in problems)
