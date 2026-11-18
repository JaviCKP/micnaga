"""WAV recuperable, reparacion de cabeceras y FLAC verificado."""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from micnaga.capture import wavio
from micnaga.capture.dsp import float_to_int16
from tests.synth import SR, noise, speech_like


def _pcm(sig: np.ndarray) -> np.ndarray:
    return float_to_int16(sig)


def test_header_is_valid_after_every_flush_without_closing(tmp_path: Path) -> None:
    path = tmp_path / "a.wav"
    w = wavio.ChunkedWavWriter(path, SR, block_samples=2 * SR, fsync=False)
    sig = _pcm(noise(6.0, seed=1))
    for i in range(0, len(sig), 1600):
        w.append(sig[i : i + 1600])
        # lectura del fichero tal como esta en disco (sin cerrar el escritor)
        snap = tmp_path / "snap.wav"
        shutil.copyfile(path, snap)
        with sf.SoundFile(str(snap)) as f:
            assert f.frames == w.flushed_samples
            assert f.samplerate == SR and f.channels == 1 and f.subtype == "PCM_16"
    w.flush()
    data, _ = sf.read(str(path), dtype="int16")
    np.testing.assert_array_equal(data, sig)
    w.close()


def test_blocks_are_two_seconds(tmp_path: Path) -> None:
    w = wavio.ChunkedWavWriter(tmp_path / "a.wav", SR, fsync=False)
    w.append(np.zeros(SR + SR // 2, dtype=np.int16))
    assert w.flushed_samples == 0 and w.pending_samples == SR + SR // 2
    w.append(np.zeros(SR, dtype=np.int16))  # 2.5 s: vacia un bloque
    assert w.flushed_samples == round(2.5 * SR)
    w.close()


def test_repair_after_abrupt_stop_with_partial_block(tmp_path: Path) -> None:
    path = tmp_path / "a.wav"
    w = wavio.ChunkedWavWriter(path, SR, fsync=False)
    sig = _pcm(noise(5.0, seed=2))
    w.append(sig[: 4 * SR + SR // 2])  # 4.5 s: un bloque de 4 s vaciado, 0.5 s pendiente
    w.append(sig[4 * SR + SR // 2 :])
    flushed = w.flushed_samples
    assert flushed >= 4 * SR
    crashed = tmp_path / "crashed.wav"
    shutil.copyfile(path, crashed)  # lo que habria en disco tras matar el proceso
    # peor caso: datos escritos pero cabecera desfasada y un byte suelto
    with open(crashed, "ab") as f:
        f.write(sig[: SR // 2].tobytes() + b"\x01")
    assert wavio.wav_samples(crashed) == flushed  # la cabecera sigue diciendo lo anterior
    n = wavio.repair_wav(crashed)
    assert n == flushed + SR // 2
    assert wavio.wav_samples(crashed) == n
    assert crashed.stat().st_size == 44 + n * 2
    data, rate = sf.read(str(crashed), dtype="int16")
    assert rate == SR
    np.testing.assert_array_equal(data[:flushed], sig[:flushed])
    w.close()


def test_repair_handles_garbage_and_missing(tmp_path: Path) -> None:
    assert wavio.repair_wav(tmp_path / "nope.wav") == 0
    assert not (tmp_path / "nope.wav").exists()
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"garbage" + b"\x00" * 100)
    n = wavio.repair_wav(bad, SR)
    assert wavio.wav_samples(bad) == n
    tiny = tmp_path / "tiny.wav"
    tiny.write_bytes(b"RIFF")
    assert wavio.repair_wav(tiny, SR) == 0
    assert wavio.wav_samples(tiny) == 0


def test_append_zeros(tmp_path: Path) -> None:
    path = tmp_path / "a.wav"
    w = wavio.ChunkedWavWriter(path, SR, fsync=False)
    w.append(_pcm(noise(1.0, seed=3)))
    w.close()
    wavio.append_zeros(path, 8000)
    data, rate = sf.read(str(path), dtype="int16")
    assert len(data) == SR + 8000 and rate == SR and not data[SR:].any()


def test_flac_roundtrip_is_bit_exact_and_deletes_wav(tmp_path: Path) -> None:
    wav, flac = tmp_path / "a.wav", tmp_path / "a.flac"
    sig = _pcm(speech_like(5.0, seed=4))
    w = wavio.ChunkedWavWriter(wav, SR, fsync=False)
    w.append(sig)
    w.close()
    n = wavio.wav_to_flac(wav, flac, level=5)
    assert n == len(sig)
    assert not wav.exists()
    assert not flac.with_name(flac.name + ".tmp").exists()
    data, rate = sf.read(str(flac), dtype="int16")
    assert rate == SR
    np.testing.assert_array_equal(data, sig)
    assert flac.stat().st_size < len(sig) * 2


def test_flac_compression_level_mapping() -> None:
    assert wavio.flac_compression(5) == 0.625
    assert wavio.flac_compression(0) == 0.0 and wavio.flac_compression(8) == 1.0
    assert wavio.flac_compression(99) == 1.0


def test_flac_level_changes_size(tmp_path: Path) -> None:
    rng = np.random.default_rng(0)
    sig = (np.cumsum(rng.standard_normal(SR * 20) * 800) % 20000).astype(np.int16)
    sizes = {}
    for lv in (0, 5, 8):
        wav = tmp_path / f"l{lv}.wav"
        w = wavio.ChunkedWavWriter(wav, SR, fsync=False)
        w.append(sig)
        w.close()
        wavio.wav_to_flac(wav, tmp_path / f"l{lv}.flac", level=lv)
        sizes[lv] = (tmp_path / f"l{lv}.flac").stat().st_size
    assert sizes[0] >= sizes[5] >= sizes[8]
    assert sizes[0] > sizes[8]


def test_failed_verification_keeps_the_wav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wav, flac = tmp_path / "a.wav", tmp_path / "a.flac"
    w = wavio.ChunkedWavWriter(wav, SR, fsync=False)
    w.append(_pcm(noise(1.0, seed=5)))
    w.close()

    def boom(*_a: object, **_k: object) -> None:
        raise ValueError("verification failed")

    monkeypatch.setattr(wavio, "_verify_flac", boom)
    with pytest.raises(ValueError):
        wavio.wav_to_flac(wav, flac)
    assert wav.exists() and not flac.exists()
    assert not flac.with_name(flac.name + ".tmp").exists()


def test_empty_wav_converts(tmp_path: Path) -> None:
    wav, flac = tmp_path / "a.wav", tmp_path / "a.flac"
    wavio.ChunkedWavWriter(wav, SR, fsync=False).close()
    assert wavio.wav_to_flac(wav, flac) == 0
    assert wav.exists() and not flac.exists()  # una pista vacia se queda como WAV


def test_header_layout() -> None:
    h = wavio.build_header(100, 16000)
    assert len(h) == 44 and h[:4] == b"RIFF" and h[8:12] == b"WAVE"
    assert struct.unpack("<I", h[4:8])[0] == 36 + 200
    assert struct.unpack("<I", h[40:44])[0] == 200
