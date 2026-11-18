"""El nucleo no debe arrastrar torch ni librerias opcionales al importarse."""

from __future__ import annotations

import subprocess
import sys

FORBIDDEN = (
    "torch",
    "torchaudio",
    "silero_vad",
    "diarize",
    "pyannote",
    "faster_whisper",
    "webview",
)


def test_core_modules_do_not_import_heavy_deps() -> None:
    code = (
        "import sys\n"
        "import micnaga.cli, micnaga.config, micnaga.paths, micnaga.events, micnaga.types\n"
        "import micnaga.registry, micnaga.logging_setup, micnaga.store.db\n"
        "import micnaga.pipeline.engines.asr, micnaga.pipeline.engines.diar, micnaga.pipeline.aec\n"
        f"bad = [m for m in {FORBIDDEN!r} if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""
