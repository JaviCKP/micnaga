from __future__ import annotations

from pathlib import Path

import pytest

from micnaga import paths
from micnaga.config import (
    Config,
    ConfigError,
    config_from_dict,
    ensure_config,
    load_config,
    save_config,
)


def test_defaults_match_spec_section_10() -> None:
    c = Config()
    assert c.detection.browsers == ["chrome.exe", "msedge.exe", "brave.exe"]
    assert c.detection.confirm_after_s == 5
    assert c.detection.popup_timeout_s == 15
    assert c.detection.on_timeout == "provisional"
    assert (c.gates.min_duration_s, c.gates.min_remote_speech_s, c.gates.min_turns) == (180, 30, 6)
    assert c.capture.sample_rate == 16000
    assert c.capture.chunk_flush_s == 2
    assert c.processing.asr_engine == "parakeet"
    assert c.processing.whisper_model == "small"
    assert c.processing.diarization == "diarize"
    assert c.processing.threads == 6
    assert c.storage.root == "~/Documents/Micnaga"
    assert c.hotkeys.marker == "ctrl+alt+m"
    assert c.hotkeys.private_pause == "ctrl+alt+p"


def test_extra_defaults() -> None:
    c = Config()
    assert c.processing.aec == "auto"
    assert c.ui.theme == "system"
    assert c.privacy.voiceprints_enabled is False
    assert c.privacy.remind_notice is True
    assert c.startup.run_at_login is True
    assert c.storage.models_dir == ""
    c.validate()


def test_load_missing_file_gives_defaults(tmp_home: Path) -> None:
    assert load_config() == Config()
    assert not tmp_home.exists()  # leer no escribe


def test_roundtrip(tmp_home: Path) -> None:
    c = Config()
    c.processing.asr_engine = "whisper"
    c.processing.threads = 4
    c.processing.glossary = ["Micnaga", "RGPD"]
    c.ui.theme = "dark"
    c.detection.browsers = ["chrome.exe"]
    c.privacy.voiceprint_threshold = 0.8
    path = save_config(c)
    assert path == paths.config_path()
    assert load_config() == c


def test_partial_file_fills_defaults(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('[gates]\nmin_turns = 10\n[ui]\ntheme = "light"\n', encoding="utf-8")
    c = load_config(f)
    assert c.gates.min_turns == 10
    assert c.gates.min_duration_s == 180
    assert c.ui.theme == "light"


def test_unknown_keys_ignored(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text("[future]\nx = 1\n[gates]\nnew_gate = 3\nmin_turns = 7\n", encoding="utf-8")
    assert load_config(f).gates.min_turns == 7


def test_int_accepted_for_float(tmp_path: Path) -> None:
    f = tmp_path / "c.toml"
    f.write_text("[detection]\npopup_timeout_s = 20\n", encoding="utf-8")
    assert load_config(f).detection.popup_timeout_s == 20.0


@pytest.mark.parametrize(
    "toml",
    [
        "[gates]\nmin_turns = 'six'\n",
        "[gates]\nmin_turns = true\n",
        "[ui]\ntheme = 3\n",
        "[detection]\nbrowsers = 'chrome.exe'\n",
        "[privacy]\nvoiceprints_enabled = 1\n",
        "gates = 3\n",
        '[processing]\nasr_engine = "gpt"\n',
        "[processing]\nthreads = 0\n",
        "this is = not toml [",
    ],
)
def test_invalid_config_raises(tmp_path: Path, toml: str) -> None:
    f = tmp_path / "c.toml"
    f.write_text(toml, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(f)


def test_save_validates(tmp_path: Path) -> None:
    c = Config()
    c.ui.theme = "neon"
    with pytest.raises(ConfigError):
        save_config(c, tmp_path / "c.toml")
    assert not (tmp_path / "c.toml").exists()


def test_save_is_atomic_no_tmp_left(tmp_home: Path) -> None:
    save_config(Config())
    assert [p.name for p in tmp_home.iterdir()] == ["config.toml"]


def test_ensure_config_creates_defaults(tmp_home: Path) -> None:
    assert not (tmp_home / "config.toml").exists()
    c = ensure_config()
    assert c == Config()
    assert (tmp_home / "config.toml").exists()


def test_config_from_dict_and_helpers(tmp_storage: Path, tmp_models: Path) -> None:
    c = config_from_dict({"storage": {"root": "~/x"}})
    assert c.meetings_root() == tmp_storage  # el entorno gana
    assert c.models_dir() == tmp_models
    assert c.db_path() == tmp_storage / "micnaga.db"


def test_float_fields_accept_fractions(tmp_path: Path) -> None:
    f = tmp_path / "c.toml"
    f.write_text(
        "[detection]\npopup_timeout_s = 2.5\nconfirm_after_s = 0.5\n"
        "[gates]\nmin_duration_s = 90.5\n[capture]\nchunk_flush_s = 1.5\n"
        "[vad]\nmax_block_s = 12.5\n",
        encoding="utf-8",
    )
    c = load_config(f)
    assert c.detection.popup_timeout_s == 2.5
    assert c.detection.confirm_after_s == 0.5
    assert c.gates.min_duration_s == 90.5
    assert c.capture.chunk_flush_s == 1.5
    assert c.vad.max_block_s == 12.5


def test_every_float_field_has_a_float_default() -> None:
    import dataclasses

    c = Config()
    for section in dataclasses.fields(c):
        obj = getattr(c, section.name)
        for f in dataclasses.fields(obj):
            if f.type in ("float", float):
                assert isinstance(getattr(obj, f.name), float), f"{section.name}.{f.name}"


def test_detection_generic_path_defaults_and_roundtrip(tmp_path: Path) -> None:
    c = Config()
    d = c.detection
    assert d.generic_apps is True
    assert (d.mic_grace_s, d.popup_after_recording_s) == (20.0, 20.0)
    assert d.exclude_apps == []
    f = tmp_path / "c.toml"
    f.write_text(
        "[detection]\ngeneric_apps = false\nmic_grace_s = 12.5\n"
        "popup_after_recording_s = 8\nexclude_apps = ['handy.exe', 'Foo_abc123']\n",
        encoding="utf-8",
    )
    loaded = load_config(f).detection
    assert loaded.generic_apps is False
    assert loaded.mic_grace_s == 12.5 and loaded.popup_after_recording_s == 8.0
    assert loaded.exclude_apps == ["handy.exe", "Foo_abc123"]


@pytest.mark.parametrize(
    "toml",
    [
        "[detection]\nmic_grace_s = -1\n",
        "[detection]\npopup_after_recording_s = -0.5\n",
        "[detection]\nconfirm_after_s = -2\n",
        "[detection]\npopup_timeout_s = -1\n",
        "[detection]\nend_after_s = 0\n",
        "[detection]\npoll_interval_s = 0\n",
        "[detection]\nexclude_apps = 'handy.exe'\n",
        "[detection]\ngeneric_apps = 'yes'\n",
    ],
)
def test_detection_invalid_values_are_rejected(tmp_path: Path, toml: str) -> None:
    f = tmp_path / "c.toml"
    f.write_text(toml, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(f)
