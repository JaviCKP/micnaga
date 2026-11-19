from __future__ import annotations

import importlib
import subprocess
import sys
import types

import pytest

from micnaga import __version__, cli


@pytest.mark.parametrize("command", sorted(cli.COMMANDS))
def test_missing_module_prints_not_implemented(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Se fuerza un modulo inexistente: asi el test no depende de lo que implementen otros.
    monkeypatch.setitem(cli.COMMANDS, command, ("micnaga.not_there_yet.cli", "run"))
    assert cli.main([command, "--whatever"]) == cli.EXIT_NOT_IMPLEMENTED
    assert "not implemented" in capsys.readouterr().err


def test_not_implemented_error_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = types.ModuleType("stub_cli")

    def run(argv: list[str]) -> int:
        raise NotImplementedError

    mod.run = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "stub_cli", mod)
    monkeypatch.setitem(cli.COMMANDS, "app", ("stub_cli", "run"))
    assert cli.main(["app"]) == cli.EXIT_NOT_IMPLEMENTED
    assert "not implemented" in capsys.readouterr().err


def test_command_table_follows_convention() -> None:
    assert cli.COMMANDS == {
        "record": ("micnaga.capture.cli", "run"),
        "process": ("micnaga.pipeline.cli", "run_process"),
        "worker": ("micnaga.pipeline.cli", "run_worker"),
        "bench": ("micnaga.bench.cli", "run"),
        "app": ("micnaga.app", "run"),
        "tools": ("micnaga.brain.mcp", "run"),
        "brain": ("micnaga.brain.cli", "run"),
        "hook-guard": ("micnaga.brain.google.guard", "main"),
    }


def test_dispatch_passes_remaining_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    mod = types.ModuleType("fake_capture_cli")

    def run(argv: list[str]) -> int:
        seen.append(argv)
        return 7

    mod.run = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_capture_cli", mod)
    monkeypatch.setitem(cli.COMMANDS, "record", ("fake_capture_cli", "run"))
    assert cli.main(["record", "--seconds", "5", "--help"]) == 7
    assert seen == [["--seconds", "5", "--help"]]


def test_real_import_errors_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.import_module

    def fake_import(name: str, package: str | None = None) -> types.ModuleType:
        if name == "wants_third_party":
            raise ModuleNotFoundError("No module named 'some_third_party'", name="some_third_party")
        return real(name, package)

    monkeypatch.setattr(cli.importlib, "import_module", fake_import)
    monkeypatch.setitem(cli.COMMANDS, "record", ("wants_third_party", "run"))
    with pytest.raises(ModuleNotFoundError):
        cli.main(["record"])


def test_no_args_and_unknown_command() -> None:
    with pytest.raises(SystemExit) as e1:
        cli.main([])
    assert e1.value.code == 0
    with pytest.raises(SystemExit) as e2:
        cli.main(["bogus"])
    assert e2.value.code == 2


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert __version__ in capsys.readouterr().out


def test_python_dash_m() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "micnaga", "--version"], capture_output=True, text=True, check=True
    )
    assert __version__ in out.stdout
