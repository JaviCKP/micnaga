"""``scripts/release_checksums.py``: lo que genera lo entiende el actualizador."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from micnaga.updates import parse_checksums

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_checksums.py"
spec = importlib.util.spec_from_file_location("release_checksums", SCRIPT)
assert spec is not None and spec.loader is not None
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)


def test_generated_file_round_trips_through_the_updater(tmp_path: Path) -> None:
    exe = tmp_path / "Micnaga-Setup-9.9.9.exe"
    exe.write_bytes(b"installer")
    assert rc.main([str(exe)]) == 0
    text = (tmp_path / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert parse_checksums(text, exe.name) == hashlib.sha256(b"installer").hexdigest()
    assert rc.main(["--check", str(tmp_path / "SHA256SUMS.txt")]) == 0
    exe.write_bytes(b"tampered")
    assert rc.main(["--check", str(tmp_path / "SHA256SUMS.txt")]) == 1
