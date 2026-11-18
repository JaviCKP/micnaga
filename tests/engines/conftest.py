"""Fixtures de los tests de motores.

Con ``MICNAGA_RUN_MODELS=1`` el conftest raiz apunta ``MICNAGA_MODELS`` a ``<repo>/.models``
(modelos reales). Los tests unitarios que necesitan una carpeta de modelos VACIA piden
``tmp_models`` y aqui se redefine para que siempre sea una carpeta temporal vacia.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from micnaga import paths


@pytest.fixture
def tmp_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "empty-models"
    monkeypatch.setenv(paths.ENV_MODELS, str(folder))
    return folder
