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
        "soundfile",
        "pyaudiowpatch",
        # diarizacion (unico motor): diarize + torch CPU
        '"diarize"',
        '"silero_vad"',
        "data/silero_vad.jit",
        '"wespeakerruntime"',
        '"sklearn"',
        "torchaudio.compliance.kaldi",
        "packaging" + chr(34) + ' / "stubs" / "kaldiio"',
    ):
        assert needle in spec, needle
    assert "diar.onnx" not in spec  # el diarizador ONNX se retiro


def _excludes(spec: str) -> list[str]:
    """Valor de ``excludes`` tras ejecutar solo las asignaciones de listas del .spec."""
    tree = ast.parse(spec)
    names: dict[str, list[str]] = {}
    for node in tree.body:
        targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
        value = getattr(node, "value", None)
        target = targets[0]
        if isinstance(target, ast.Name) and isinstance(value, (ast.List, ast.BinOp)):
            try:
                result = eval(compile(ast.Expression(value), "spec", "eval"), {}, dict(names))
            except Exception:
                continue
            if isinstance(result, list) and all(isinstance(x, str) for x in result):
                if isinstance(node, ast.AugAssign):
                    names[target.id] = names.get(target.id, []) + result
                else:
                    names[target.id] = result
    return names["excludes"]


def test_spec_excludes_dev_tools_gpl_ffmpeg_and_unsigned_aec() -> None:
    spec = _spec()
    excludes = _excludes(spec)
    for needle in ("pytest", "mypy", "ruff", "jiwer", "av", "kaldiio", "pyaec", "PIL._avif"):
        assert needle in excludes, needle
    for needle in ("cudnn64_9.dll", '"aec.dll"', '"torch/bin/"'):
        assert needle in spec, needle
    # torch va dentro (unico diarizador); solo se recortan subpaquetes que la diarizacion no usa
    for kept in ("torch", "torchaudio", "diarize", "sklearn", "silero_vad", "scipy", "PIL"):
        assert kept not in excludes, kept
    for trimmed in ("torch._inductor", "torch._dynamo", "torch.onnx", "pandas"):
        assert trimmed in excludes, trimmed
    assert '"pyaec"' not in spec.split("excludes = [")[0]  # ni hiddenimport ni DLL recogida


def test_spec_shares_python_modules_between_the_three_exes() -> None:
    """Con PYZ cada .exe llevaba su copia de todo el codigo Python (3 x 51 MiB con torch)."""
    assert "noarchive=True" in _spec()


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
    # pyaec (aec.dll sin firmar) no viaja; la diarizacion con torch si
    assert "pyaec" not in frozen_entry.THIRD_PARTY_IMPORTS
    assert "pyaec" in frozen_entry.BANNED_IMPORTS
    assert {"torch", "diarize", "silero_vad", "torchaudio.compliance.kaldi"} <= set(
        frozen_entry.DIAR_IMPORTS
    )
    assert not any("diar.onnx" in m for m in frozen_entry.THIRD_PARTY_IMPORTS)


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
    run = iss.split("\n[Run]\n", 1)[1].split("\n[UninstallRun]\n", 1)[0]
    assert (
        'Parameters: "app"' in run
        and "postinstall" not in run.replace("sin ", "").split("Flags:")[-1]
    )
    assert "PrepareToInstall" in iss and "MicnagaInUse()" in iss and "taskkill.exe" not in iss
    uninstall = iss.split("\n[UninstallRun]\n", 1)[1].split("\n[Code]\n", 1)[0]
    for action in ("StopMicnaga", "StopMicnagaHost"):
        command = f'Parameters: "/D /C exit /B 0"; Flags: runhidden; RunOnceId: "{action}"'
        assert command in uninstall
    assert "UsePreviousAppDir=yes" in iss
    # lo que la version nueva ya no trae (p. ej. aec.dll) no sobrevive a la actualizacion
    install_delete = iss[iss.index("[InstallDelete]") : iss.index("[Files]")]
    assert r'Type: files; Name: "{app}\_internal\pyaec\aec.dll"' in install_delete
    assert "filesandordirs" not in install_delete


def test_iss_single_product_webview2_check_and_optional_signing() -> None:
    iss = _iss()
    assert "Variant" not in iss and "micnaga-torch" not in iss
    assert r'#define SourceDir "..\dist\micnaga"' in iss
    # WebView2: aviso (nunca en silencio), no bloquea
    assert "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" in iss and "'pv'" in iss
    webview = iss[iss.index("procedure CurStepChanged") :]
    assert "WizardSilent" in webview and "ssPostInstall" in webview
    # firma solo si build.ps1 pasa /DSign=1 (un SignTool sin definir romperia la compilacion)
    sign = iss[iss.index("#ifdef Sign") : iss.index("#endif", iss.index("#ifdef Sign"))]
    assert "SignTool=micnaga" in sign and "SignedUninstaller=yes" in sign
    assert iss.count("SignTool=") == 1


def test_iss_registers_native_host_per_user_and_relaunches_after_silent_update() -> None:
    import json

    from micnaga.watcher.extension import HOST_NAME, extension_id_from_key

    iss = _iss()
    manifest = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
    assert f'#define ExtensionId "{extension_id_from_key(manifest["key"])}"' in iss
    assert f'#define HostName "{HOST_NAME}"' in iss
    registry = iss[iss.index("[Registry]") : iss.index("[Run]")]
    for browser in (r"Google\Chrome", r"Microsoft\Edge", r"BraveSoftware\Brave-Browser"):
        line = next(x for x in registry.splitlines() if browser + r"\NativeMessagingHosts" in x)
        assert line.startswith("Root: HKCU;") and "uninsdeletekey" in line
    assert "WriteHostManifest" in iss and r'Name: "{app}\{#HostName}.json"' in iss
    assert '"path": "micnaga-host.exe"' in iss
    # la actualizacion automatica (updates.py: /VERYSILENT ...) necesita que [Run] relance la app
    run = iss.split("\n[Run]\n", 1)[1].split("\n[UninstallRun]\n", 1)[0]
    entry = next(x for x in run.splitlines() if x.startswith("Filename:"))
    assert "{#AppExe}" in entry and "skipifsilent" not in entry and "postinstall" not in entry


def test_build_script_writes_sha256sums() -> None:
    script = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")
    assert "SHA256SUMS.txt" in script and "Get-FileHash" in script and "ToLowerInvariant" in script
    assert '"{0}  {1}"' in script  # formato de sha256sum: dos espacios


def test_build_script_signs_optionally() -> None:
    script = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")
    for needle in (
        "MICNAGA_SIGN_THUMBPRINT",
        "MICNAGA_SIGN_PFX",
        "Get-AuthenticodeSignature",
        "/DSign=1",
        "Write-Warning",
        "--check --dist",
    ):
        assert needle in script, needle
    assert "-Variant" not in script and "micnaga-torch" not in script


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
