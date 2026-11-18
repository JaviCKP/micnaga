from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from micnaga import paths
from micnaga.config import Config, save_config


def _inside(path: Path, root: Path) -> bool:
    return root.resolve() in path.resolve().parents or path.resolve() == root.resolve()


def test_every_path_function_stays_in_tmp(
    tmp_home: Path, tmp_storage: Path, tmp_models: Path, tmp_path: Path
) -> None:
    results = [
        paths.app_data_dir(),
        paths.config_path(),
        paths.logs_dir(),
        paths.voiceprints_dir(),
        paths.models_dir(),
        paths.meetings_root(),
        paths.db_path(),
        paths.session_dir("2026-11-18_1030_x"),
        paths.work_dir(paths.session_dir("2026-11-18_1030_x")),
    ]
    for p in results:
        assert _inside(p, tmp_path), p
    assert paths.db_path() == tmp_storage / "micnaga.db"
    assert paths.models_dir() == tmp_models
    assert paths.config_path() == tmp_home / "config.toml"


def test_paths_do_not_create_directories(tmp_home: Path, tmp_storage: Path) -> None:
    paths.logs_dir()
    paths.db_path()
    paths.models_dir()
    assert not tmp_home.exists()
    assert not tmp_storage.exists()


def test_env_is_read_at_call_time(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "a"))
    assert paths.app_data_dir() == tmp_path / "a"
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "b"))
    assert paths.app_data_dir() == tmp_path / "b"


def test_defaults_without_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in (paths.ENV_HOME, paths.ENV_STORAGE, paths.ENV_MODELS):
        monkeypatch.delenv(var)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    cfg = Config()
    assert paths.app_data_dir() == tmp_path / "roaming" / "Micnaga"
    assert paths.models_dir(cfg) == tmp_path / "roaming" / "Micnaga" / "models"
    root = paths.meetings_root(cfg)
    assert root == Path("~/Documents/Micnaga").expanduser()
    assert "~" not in str(root)


def test_env_beats_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_storage: Path
) -> None:
    cfg = Config()
    cfg.storage.root = str(tmp_path / "from-config")
    cfg.storage.models_dir = str(tmp_path / "models-from-config")
    assert paths.meetings_root(cfg) == tmp_storage  # MICNAGA_STORAGE gana
    monkeypatch.delenv(paths.ENV_STORAGE)
    monkeypatch.delenv(paths.ENV_MODELS)
    assert paths.meetings_root(cfg) == tmp_path / "from-config"
    assert paths.models_dir(cfg) == tmp_path / "models-from-config"


def test_paths_read_config_file_when_no_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = Config()
    cfg.storage.root = str(tmp_path / "saved-root")
    save_config(cfg)
    monkeypatch.delenv(paths.ENV_STORAGE)
    assert paths.meetings_root() == tmp_path / "saved-root"


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Revisión semanal", "revision-semanal"),
        ("  Sprint #42 — Planning!!  ", "sprint-42-planning"),
        ("Ñandú & Compañía", "nandu-compania"),
        ("", "reunion"),
        ("***", "reunion"),
        ("Meet - abc-defg-hij", "meet-abc-defg-hij"),
    ],
)
def test_slugify(title: str, slug: str) -> None:
    assert paths.slugify(title) == slug


def test_slugify_truncates() -> None:
    slug = paths.slugify("a" * 200)
    assert len(slug) <= paths.SLUG_MAX_LEN
    assert not slug.endswith("-")


def test_meeting_folder_name() -> None:
    name = paths.meeting_folder_name(datetime(2026, 11, 18, 10, 30), "Revisión semanal")
    assert name == "2026-11-18_1030_revision-semanal"


def test_unique_folder(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    (tmp_path / "x-2").mkdir()
    assert paths.unique_folder(tmp_path, "x") == tmp_path / "x-3"
    assert paths.unique_folder(tmp_path, "y") == tmp_path / "y"
