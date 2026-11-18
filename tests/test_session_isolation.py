"""La linea base de sesion protege tambien a los fixtures de modulo y al codigo de importacion."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from micnaga import paths

# Se evalua al importar el modulo (fase de recopilacion), antes de cualquier fixture por test.
IMPORT_TIME_HOME = paths.app_data_dir()
IMPORT_TIME_STORAGE = paths.meetings_root()


def _under_temp(p: Path) -> bool:
    return Path(tempfile.gettempdir()).resolve() in p.resolve().parents


@pytest.fixture(scope="module")
def module_scoped_paths() -> dict[str, Path]:
    return {
        "home": paths.app_data_dir(),
        "storage": paths.meetings_root(),
        "db": paths.db_path(),
        "logs": paths.logs_dir(),
    }


def test_import_time_paths_are_temporary() -> None:
    assert _under_temp(IMPORT_TIME_HOME)
    assert _under_temp(IMPORT_TIME_STORAGE)


def test_module_scoped_fixture_never_sees_real_folders(
    module_scoped_paths: dict[str, Path],
) -> None:
    for p in module_scoped_paths.values():
        assert _under_temp(p), p
        assert "Documents" not in p.parts
        assert "Micnaga" not in p.parts


def test_appdata_is_redirected_too() -> None:
    assert _under_temp(Path(os.environ["APPDATA"]))
