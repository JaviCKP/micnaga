r"""Fixtures y marcadores comunes.

* **Linea base de sesion** (``pytest_configure``): antes de recopilar o ejecutar nada,
  ``MICNAGA_HOME``, ``MICNAGA_STORAGE``, ``MICNAGA_MODELS`` y ``APPDATA`` apuntan a una carpeta
  temporal de la sesion. Asi tampoco los fixtures de modulo/sesion ni el codigo de
  recopilacion pueden tocar ``%APPDATA%\Micnaga`` ni ``Documentos\Micnaga``.
* ``isolated_env`` (autouse, por test): carpetas nuevas y vacias para cada test.
* Modelos: con ``MICNAGA_RUN_MODELS=1`` la carpeta de modelos es la de ``MICNAGA_MODELS`` si esta
  definida y, si no, ``<repo>/.models`` (compartida por toda la sesion: no se redescarga ~1 GB
  por test). Sin ``MICNAGA_RUN_MODELS`` los modelos van a una carpeta temporal.
* Marcadores: ``slow``/``models`` se saltan salvo ``MICNAGA_RUN_MODELS=1`` (``slow`` tambien con
  ``MICNAGA_RUN_SLOW=1``); ``realdevice`` salvo ``MICNAGA_RUN_REALDEVICE=1``. Los tests con
  modelos deben subir el limite: ``@pytest.mark.timeout(600)``.
* Audio sintetico: ``tests/synth.py``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from micnaga import paths

REPO_ROOT = Path(__file__).resolve().parent.parent
_SESSION: dict[str, Path] = {}
_ORIGINAL_ENV: dict[str, str | None] = {}
_MANAGED_VARS = (paths.ENV_HOME, paths.ENV_STORAGE, paths.ENV_MODELS, "APPDATA")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip() not in ("", "0", "false", "False")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    run_models = _flag("MICNAGA_RUN_MODELS")
    run_slow = run_models or _flag("MICNAGA_RUN_SLOW")
    run_real = _flag("MICNAGA_RUN_REALDEVICE")
    for item in items:
        if "models" in item.keywords and not run_models:
            item.add_marker(pytest.mark.skip(reason="set MICNAGA_RUN_MODELS=1 to run"))
        elif "slow" in item.keywords and not run_slow:
            item.add_marker(pytest.mark.skip(reason="set MICNAGA_RUN_SLOW=1 to run"))
        if "realdevice" in item.keywords and not run_real:
            item.add_marker(pytest.mark.skip(reason="set MICNAGA_RUN_REALDEVICE=1 to run"))


def pytest_configure(config: pytest.Config) -> None:
    for var in _MANAGED_VARS:
        _ORIGINAL_ENV[var] = os.environ.get(var)
    root = Path(tempfile.mkdtemp(prefix="micnaga-pytest-"))
    models = root / "models"
    if _flag("MICNAGA_RUN_MODELS"):
        models = Path(_ORIGINAL_ENV[paths.ENV_MODELS] or REPO_ROOT / ".models")
    _SESSION.update(home=root / "home", storage=root / "storage", models=models, root=root)
    os.environ[paths.ENV_HOME] = str(root / "home")
    os.environ[paths.ENV_STORAGE] = str(root / "storage")
    os.environ[paths.ENV_MODELS] = str(models)
    os.environ["APPDATA"] = str(root / "appdata")


def pytest_unconfigure(config: pytest.Config) -> None:
    for var, value in _ORIGINAL_ENV.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value
    root = _SESSION.get("root")
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    home = tmp_path / "home"
    storage = tmp_path / "storage"
    models = _SESSION["models"] if _flag("MICNAGA_RUN_MODELS") else tmp_path / "models"
    monkeypatch.setenv(paths.ENV_HOME, str(home))
    monkeypatch.setenv(paths.ENV_STORAGE, str(storage))
    monkeypatch.setenv(paths.ENV_MODELS, str(models))
    # Que ninguna ruta salga de un APPDATA/Documentos reales por descuido.
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    yield {"home": home, "storage": storage, "models": models}


@pytest.fixture
def tmp_home(isolated_env: dict[str, Path]) -> Path:
    return isolated_env["home"]


@pytest.fixture
def tmp_storage(isolated_env: dict[str, Path]) -> Path:
    return isolated_env["storage"]


@pytest.fixture
def tmp_models(isolated_env: dict[str, Path]) -> Path:
    return isolated_env["models"]
