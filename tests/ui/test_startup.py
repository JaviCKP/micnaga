"""«Arrancar con Windows»: siempre contra un registro falso (nunca el Run real)."""

from __future__ import annotations

import contextlib
import uuid
import winreg
from collections.abc import Iterator

import pytest

from micnaga.ui import startup


def test_enable_then_disable_with_fake_backend() -> None:
    reg = startup.MemoryRegistry()
    assert not startup.is_run_at_login(backend=reg)
    startup.set_run_at_login(True, backend=reg, command='"C:\\x\\Micnaga.exe" app')
    assert startup.is_run_at_login(backend=reg)
    assert startup.get_run_command(backend=reg) == '"C:\\x\\Micnaga.exe" app'
    startup.set_run_at_login(False, backend=reg)
    assert not startup.is_run_at_login(backend=reg)
    startup.set_run_at_login(False, backend=reg)  # idempotente


def test_default_command_for_installed_and_dev() -> None:
    assert (
        startup.default_command(frozen=True, executable="C:\\Micnaga\\Micnaga.exe")
        == '"C:\\Micnaga\\Micnaga.exe" app'
    )
    dev = startup.default_command(frozen=False, executable="C:\\py\\python.exe")
    assert dev.endswith("-m micnaga app") and dev.startswith('"')


def test_default_backend_can_be_replaced_and_restored() -> None:
    reg = startup.MemoryRegistry()
    startup.set_default_backend(reg)
    try:
        startup.set_run_at_login(True, command="x")
        assert reg.values == {startup.VALUE_NAME: "x"}
    finally:
        startup.set_default_backend(None)
    assert isinstance(startup.default_backend(), startup.WinRegistry)


def test_real_run_key_refuses_to_write_under_pytest() -> None:
    real = startup.WinRegistry()  # clave Run real
    with pytest.raises(startup.RegistryWriteBlockedError):
        real.set("MicnagaTest", "x")
    with pytest.raises(startup.RegistryWriteBlockedError):
        real.delete("MicnagaTest")
    assert real.get("MicnagaTest-does-not-exist") is None  # leer si se puede


@pytest.fixture
def scratch_key() -> Iterator[str]:
    """Clave HKCU de usar y tirar (NO la Run) para probar WinRegistry de verdad."""
    path = rf"Software\MicnagaUiTest\{uuid.uuid4().hex}"
    yield path
    for sub in (path, r"Software\MicnagaUiTest"):
        with contextlib.suppress(OSError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)


def test_winregistry_roundtrip_on_a_scratch_key(scratch_key: str) -> None:
    reg = startup.WinRegistry(scratch_key)
    startup.set_run_at_login(True, backend=reg, command="cmd /c echo")
    assert reg.get(startup.VALUE_NAME) == "cmd /c echo"
    assert startup.is_run_at_login(backend=reg)
    startup.set_run_at_login(False, backend=reg)
    assert reg.get(startup.VALUE_NAME) is None
    reg.delete(startup.VALUE_NAME)  # ausente: sin error


def test_ensure_run_at_login_writes_only_when_missing_or_different() -> None:
    reg = startup.MemoryRegistry()
    cmd = "C:/x/micnaga.exe app"
    assert startup.ensure_run_at_login(backend=reg, command=cmd) is True
    assert reg.get("Micnaga") == cmd
    assert startup.ensure_run_at_login(backend=reg, command=cmd) is False
    reg.set("Micnaga", "stale")
    assert startup.ensure_run_at_login(backend=reg, command=cmd) is True
