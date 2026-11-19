from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from micnaga import paths
from micnaga.store import voiceprints as vp
from micnaga.store.voiceprints import VoiceprintStore


def vec(seed: int, dim: int = 16) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(dim).astype(np.float32)


def test_dpapi_round_trip(tmp_home: Path) -> None:
    blob = vp._protect(b"secreto")
    assert b"secreto" not in blob
    assert vp._unprotect(blob) == b"secreto"
    with pytest.raises(vp.VoiceprintError):
        vp._unprotect(b"no es dpapi")


def test_protected_json_round_trip_never_writes_plaintext(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "emb.dpapi"
    vp.write_protected_json(target, {"S1": [0.0371337, 0.25]})
    raw = target.read_bytes()
    assert b'"S1": [' not in raw and b"0.0371337" not in raw
    assert vp.read_protected_json(target) == {"S1": [0.0371337, 0.25]}
    assert [p.name for p in target.parent.iterdir()] == ["emb.dpapi"]  # sin temporales
    target.write_bytes(b"basura")
    with pytest.raises(vp.VoiceprintError):
        vp.read_protected_json(target)


def test_protected_json_writes_nothing_if_dpapi_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_data: bytes) -> bytes:
        raise vp.VoiceprintError("DPAPI encryption failed: test")

    monkeypatch.setattr(vp, "_protect", broken)
    with pytest.raises(vp.VoiceprintError):
        vp.write_protected_json(tmp_path / "emb.dpapi", {"S1": [0.5]})
    assert list(tmp_path.iterdir()) == []


def test_save_match_delete_list(tmp_home: Path) -> None:
    store = VoiceprintStore(threshold=0.75)
    store.save("Laura", vec(1))
    store.save("Pedro", vec(2))
    assert store.list() == ["Laura", "Pedro"]
    noisy = vec(1) + 0.05 * vec(99)
    found = store.match(noisy)
    assert found is not None and found[0] == "Laura" and found[1] > 0.95
    assert store.match(vec(3)) is None  # otra persona: por debajo del umbral
    assert store.delete("laura")  # el nombre no distingue mayusculas
    assert store.list() == ["Pedro"]
    assert not store.delete("Laura")
    assert store.match(noisy) is None


def test_files_do_not_reveal_names_or_vectors(tmp_home: Path) -> None:
    VoiceprintStore().save("Laura Gómez", vec(1))
    files = list(paths.voiceprints_dir().glob("*"))
    assert len(files) == 1
    assert "laura" not in files[0].name.lower()
    raw = files[0].read_bytes()
    assert b"Laura" not in raw and b"embeddings" not in raw


def test_explicit_threshold_overrides_default(tmp_home: Path) -> None:
    store = VoiceprintStore()
    store.save("Ana", vec(1))
    mixed = vec(1) + 1.0 * vec(7)
    sim = float(np.dot(mixed, vec(1)) / (np.linalg.norm(mixed) * np.linalg.norm(vec(1))))
    assert store.match(mixed, threshold=sim - 0.01) is not None
    assert store.match(mixed, threshold=sim + 0.01) is None


def test_default_threshold_comes_from_config(tmp_home: Path) -> None:
    store = VoiceprintStore()
    store.save("Ana", vec(1))
    assert store.match(vec(1)) is not None  # coseno 1.0 >= 0.75
    assert store.match(vec(2)) is None


def test_multiple_embeddings_use_best_and_cap(tmp_home: Path) -> None:
    store = VoiceprintStore(threshold=0.9)
    for seed in range(15):
        store.save("Ana", vec(seed))
    payload = vp._unpack(next(paths.voiceprints_dir().glob("*.vp")).read_bytes())
    assert len(payload["embeddings"]) == vp.MAX_EMBEDDINGS
    assert store.match(vec(14)) is not None  # el mas reciente se conserva
    assert store.match(vec(0)) is None  # el mas antiguo salio


def test_dimension_and_engine_mismatch_are_skipped(tmp_home: Path) -> None:
    store = VoiceprintStore(threshold=0.5)
    store.save("Ana", vec(1, 16), engine="wespeaker")
    assert store.match(vec(1, 32)) is None
    assert store.match(vec(1, 16), engine="otro") is None
    assert store.match(vec(1, 16), engine="wespeaker") is not None


def test_corrupt_file_is_ignored(tmp_home: Path) -> None:
    store = VoiceprintStore(threshold=0.5)
    store.save("Ana", vec(1))
    (paths.voiceprints_dir() / "basura.vp").write_bytes(b"xxxx")
    assert store.list() == ["Ana"]
    assert store.match(vec(1)) is not None


def test_invalid_input(tmp_home: Path) -> None:
    store = VoiceprintStore()
    with pytest.raises(ValueError):
        store.save("  ", vec(1))
    with pytest.raises(ValueError):
        store.save("Ana", np.zeros(8, dtype=np.float32))


def test_delete_all(tmp_home: Path) -> None:
    store = VoiceprintStore()
    store.save("A", vec(1))
    store.save("B", vec(2))
    assert store.delete_all() == 2 and store.list() == []


def test_meeting_embeddings_round_trip(tmp_path: Path) -> None:
    data = {"S1": vec(1), "S2": vec(2)}
    file = vp.save_meeting_embeddings(tmp_path, data, engine="x")
    # patron largo: 2 bytes sueltos pueden aparecer por azar en el cifrado (test inestable)
    assert b'"S1": [' not in file.read_bytes()
    loaded, engine = vp.load_meeting_embeddings(tmp_path)
    assert engine == "x" and set(loaded) == {"S1", "S2"}
    assert np.allclose(loaded["S1"], data["S1"], atol=1e-6)
    vp.delete_meeting_embeddings(tmp_path)
    assert vp.load_meeting_embeddings(tmp_path) == ({}, "")
