"""Coherencia de los ficheros de empaquetado (sin ejecutar PyInstaller ni Inno Setup)."""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from micnaga.pipeline import worker as worker_module
from micnaga.ui import startup

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "packaging"


def _spec() -> str:
    return (PKG / "micnaga.spec").read_text(encoding="utf-8")


def _iss() -> str:
    return (PKG / "micnaga.iss").read_text(encoding="utf-8")


def test_spec_is_valid_python() -> None:
    ast.parse(_spec())


def test_spec_builds_three_executables_in_one_folder() -> None:
    spec = _spec()
    assert 'make_exe("micnaga", console=False)' in spec  # ventana, sin consola
    assert 'make_exe("micnaga-cli", console=True)' in spec
    assert 'make_exe("micnaga-host", console=True)' in spec
    assert "COLLECT(" in spec


def test_spec_matches_worker_and_startup_contract() -> None:
    """worker_command() y startup.default_command() esperan ``micnaga.exe <subcomando>``."""
    exe = r"C:\Micnaga\micnaga.exe"
    assert startup.default_command(frozen=True, executable=exe) == f'"{exe}" app'
    sys.frozen = True  # type: ignore[attr-defined]
    try:
        assert worker_module.worker_command() == [sys.executable, "worker"]
    finally:
        del sys.frozen  # type: ignore[attr-defined]
    from micnaga import cli

    assert {"app", "worker", "record", "process", "bench"} <= set(cli.COMMANDS)


def test_spec_collects_what_the_frozen_app_needs() -> None:
    spec = _spec()
    for needle in (
        'collect_submodules("micnaga")',
        "micnaga/ui/web",
        "micnaga.ico",
        "THIRD_PARTY_LICENSES.md",
        '"pystray": "py"',
        "onnxruntime",
        "ctranslate2",
        "aec_audio_processing",
        "pyaec",
        "soundfile",
        "pyaudiowpatch",
    ):
        assert needle in spec, needle


def test_spec_excludes_dev_tools_gpl_ffmpeg_and_torch_in_standard() -> None:
    spec = _spec()
    for needle in ('"pytest"', '"mypy"', '"ruff"', '"jiwer"', '"av"', "cudnn64_9.dll"):
        assert needle in spec, needle
    standard_block = spec[spec.index('if VARIANT == "standard":') : spec.index("a = Analysis(")]
    for needle in ('"torch"', '"torchaudio"', '"diarize"', '"sklearn"', '"pandas"'):
        assert needle in standard_block, needle


def test_stubs_exist_and_fail_loudly() -> None:
    for name in ("av", "kaldiio"):
        source = (PKG / "stubs" / name / "__init__.py").read_text(encoding="utf-8")
        assert "__getattr__" in source
        namespace: dict[str, object] = {}
        exec(compile(source, name, "exec"), namespace)
        getter = namespace["__getattr__"]
        assert callable(getter)
        with pytest.raises(AttributeError):
            getter("anything")
        with pytest.raises(AttributeError):
            getter("__path__")  # introspeccion normal no debe explotar con otro tipo de error


def test_frozen_entry_dispatch_and_stream_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    sys.path.insert(0, str(PKG))
    try:
        import frozen_entry
    finally:
        sys.path.remove(str(PKG))
    # En un exe «windowed» los flujos estandar son None
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    frozen_entry._fix_std_streams()
    assert sys.stdout is not None and sys.stderr is not None
    print("no debe fallar")
    # El host se reconoce por el nombre del ejecutable
    calls: list[str] = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\M\micnaga-host.exe")
    import micnaga.watcher.extension as ext

    monkeypatch.setattr(ext, "host_main", lambda: calls.append("host") or 0)
    assert frozen_entry.main(["chrome-extension://x/"]) == 0
    assert calls == ["host"]
    # Los assets web existen en el arbol de fuentes
    assert frozen_entry._check_assets() == []


def test_iss_is_per_user_with_aumid_and_clean_uninstall() -> None:
    iss = _iss()
    assert "PrivilegesRequired=lowest" in iss
    assert r"DefaultDirName={localappdata}\Programs\Micnaga" in iss
    assert "AppUserModelID:" in iss and "es.micnaga.Micnaga" in iss
    assert r"{userprograms}" in iss
    # clave Run: mismo valor y comando que ui/startup.py
    assert startup.RUN_KEY_PATH == r"Software\Microsoft\Windows\CurrentVersion\Run"
    assert 'ValueName: "{#AppName}"' in iss and startup.VALUE_NAME == "Micnaga"
    assert r'ValueData: """{app}\{#AppExe}"" app"' in iss
    assert "Check: ShouldWriteRunKey" in iss and "[Tasks]" not in iss  # siempre activo
    assert "RegDeleteValue(HKCU" in iss
    # pregunta antes de borrar datos de usuario y nunca en silencio
    assert "{userappdata}" in iss and "{userdocs}" in iss
    assert "MsgBox(" in iss and "UninstallSilent" in iss
    assert "Root: HKLM" not in iss
    # el AUMID del instalador es el que debe usar la app
    assert re.search(r'#define AppUserModelId "es\.micnaga\.Micnaga"', iss)


def test_iss_is_one_click_and_upgrades_in_place() -> None:
    iss = _iss()
    for directive in (
        "DisableWelcomePage=yes",
        "DisableDirPage=yes",
        "DisableProgramGroupPage=yes",
        "DisableReadyPage=yes",
        "DisableFinishedPage=yes",
    ):
        assert directive in iss, directive
    assert "LicenseFile" not in iss and "[Tasks]" not in iss
    # arranca la app al terminar (tambien en silencio): sin `postinstall`, que exige pagina final
    run = iss[iss.index("[Run]") : iss.index("[UninstallRun]")]
    assert (
        'Parameters: "app"' in run
        and "postinstall" not in run.replace("sin ", "").split("Flags:")[-1]
    )
    # actualizacion: cierra la app y compara con el mutex de micnaga/app.py
    assert "PrepareToInstall" in iss and "taskkill.exe" in iss and "CheckForMutexes" in iss
    prefix = "Local" + chr(92) + "micnaga-app-"
    assert prefix in iss
    from micnaga.app import SingleInstance

    assert SingleInstance().name.startswith(prefix)
    assert "UsePreviousAppDir=yes" in iss


def test_iss_version_matches_package() -> None:
    from micnaga import __version__

    assert f'#define AppVersion "{__version__}"' in _iss()


def test_build_script_parses_in_powershell() -> None:
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("powershell no disponible")
    for name in ("build.ps1", "register_native_host.ps1"):
        path = ROOT / "scripts" / name
        command = (
            "$e=$null;$t=$null;"
            f"[System.Management.Automation.Language.Parser]::ParseFile('{path}',[ref]$t,[ref]$e)|Out-Null;"
            "if($e.Count){$e|ForEach-Object{$_.Message};exit 1}"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_build_dirs_are_git_ignored() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "build/" in ignore and "dist/" in ignore


def test_measure_dist_estimates(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import measure_dist
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    (tmp_path / "_internal" / "pkg").mkdir(parents=True)
    (tmp_path / "_internal" / "pkg" / "a.bin").write_bytes(b"0" * 100_000)
    (tmp_path / "app.exe").write_bytes(b"1" * 10_000)
    total, top = measure_dist.dir_sizes(tmp_path)
    assert total == 110_000 and top["_internal/pkg"] == 100_000
    estimate = measure_dist.lzma_estimate(tmp_path)
    assert 0 < estimate < total
