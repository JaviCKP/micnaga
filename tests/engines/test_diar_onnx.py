"""Diarizador ``onnx`` (sin torch): fbank, clustering, turnos, cableado y extremo a extremo.

La parte por defecto no necesita modelos: un ONNX/VAD falsos alimentan el motor real.
"""

from __future__ import annotations

import ast
import itertools
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from micnaga import models
from micnaga.config import Config
from micnaga.pipeline.engines.diar import DIARIZERS, SpeakerEmbedder, create_diarizer
from micnaga.pipeline.engines.diar import onnx as onx
from micnaga.registry import EngineUnavailableError
from micnaga.types import AudioArray, Turn

SR = 16000
FORBIDDEN = ("torch", "torchaudio", "sklearn", "diarize", "wespeakerruntime", "kaldiio")


# ------------------------------------------------------------------------- fbank


def _ref_signal() -> AudioArray:
    rng = np.random.default_rng(0)
    t = np.arange(8000) / SR
    return (0.3 * np.sin(2 * np.pi * 440 * t) + 0.05 * rng.standard_normal(8000)).astype(np.float32)


def test_fbank_matches_torchaudio_reference_values() -> None:
    """Valores calculados una vez con ``torchaudio.compliance.kaldi.fbank`` (misma entrada)."""
    f = onx.fbank(_ref_signal())
    assert f.shape == (48, 80) and f.dtype == np.float32
    np.testing.assert_allclose(f[0, :4], [12.4912577, 8.8032107, 12.3436489, 12.8759680], atol=2e-3)
    np.testing.assert_allclose(
        f[-1, -4:], [23.1393776, 22.5460911, 23.2575684, 23.1753063], atol=2e-3
    )
    np.testing.assert_allclose(
        f[3, :4], [13.3416786, 14.4641914, 12.0769205, 14.0239273], atol=2e-3
    )
    assert float(f.mean()) == pytest.approx(19.65975, abs=1e-3)


def test_fbank_frame_count_and_short_input() -> None:
    assert onx.fbank(np.zeros(399, dtype=np.float32)).shape == (0, 80)
    assert onx.fbank(np.zeros(400, dtype=np.float32)).shape == (1, 80)
    assert onx.fbank(np.zeros(24000, dtype=np.float32)).shape == (148, 80)  # 1,5 s


def test_fbank_silence_is_the_epsilon_floor_and_tone_peaks_in_the_right_bin() -> None:
    silent = onx.fbank(np.zeros(4000, dtype=np.float32))
    np.testing.assert_allclose(silent, np.log(np.finfo(np.float32).eps), rtol=1e-6)
    t = np.arange(16000) / SR
    for hz in (300.0, 1000.0, 3000.0):
        f = onx.fbank((0.5 * np.sin(2 * np.pi * hz * t)).astype(np.float32)).mean(axis=0)
        mel = lambda v: 1127.0 * np.log(1.0 + v / 700.0)  # noqa: E731
        lo, hi = mel(20.0), mel(8000.0)
        expected = (mel(hz) - lo) / ((hi - lo) / 81) - 1  # indice del filtro centrado en hz
        assert abs(int(np.argmax(f)) - expected) <= 1.0


def test_fbank_window_slice_equals_fbank_of_the_window() -> None:
    """Base del truco de eficiencia: fbank del tramo cortado == fbank del audio de la ventana."""
    rng = np.random.default_rng(1)
    x = rng.standard_normal(SR * 4).astype(np.float32) * 0.1
    full = onx.fbank(x)
    start = 37
    win = onx.fbank(x[start * 160 : start * 160 + 24000])
    np.testing.assert_allclose(full[start : start + 148], win, atol=1e-4)


def test_mel_bank_properties() -> None:
    b = onx.mel_banks()
    assert b.shape == (80, 257)
    assert (b >= 0).all() and (b[:, -1] == 0).all()
    peaks = b.argmax(axis=1)
    assert (np.diff(peaks) >= 0).all() and peaks[-1] > 200  # centros no decrecientes
    assert (b.sum(axis=1) > 0).all()


# --------------------------------------------------------------------- clustering


def blobs(sizes: list[int], noise: float = 0.15, dim: int = 32, seed: int = 0) -> tuple[Any, Any]:
    rng = np.random.default_rng(seed)
    centers = np.linalg.qr(rng.standard_normal((dim, len(sizes))))[0].T  # ortogonales
    xs, ys = [], []
    for i, n in enumerate(sizes):
        v = centers[i] + noise * rng.standard_normal((n, dim)) / np.sqrt(dim) * 3
        xs.append(v / np.linalg.norm(v, axis=1, keepdims=True))
        ys += [i] * n
    order = rng.permutation(len(ys))
    return np.concatenate(xs).astype(np.float32)[order], np.array(ys)[order]


def same_partition(a: NDArray[np.int64], b: NDArray[np.int64]) -> bool:
    pairs = {(int(x), int(y)) for x, y in zip(a, b, strict=True)}
    return len(pairs) == len(set(a.tolist())) == len(set(b.tolist()))


@pytest.mark.parametrize("k", [2, 3, 4])
def test_cluster_recovers_blobs_with_and_without_n_speakers(k: int) -> None:
    x, y = blobs([60] * k)
    auto = onx.cluster_embeddings(x)
    assert same_partition(auto, y)
    forced = onx.cluster_embeddings(x, k)
    assert same_partition(forced, y)


def test_cluster_n_speakers_overrides_the_threshold_both_ways() -> None:
    x, _ = blobs([50, 50, 50])
    assert len(set(onx.cluster_embeddings(x, 2).tolist())) == 2
    assert len(set(onx.cluster_embeddings(x, 1).tolist())) == 1
    assert len(set(onx.cluster_embeddings(x, 3).tolist())) == 3
    assert len(set(onx.cluster_embeddings(x[:2], 5).tolist())) == 2  # nunca mas que ventanas


def test_cluster_edge_cases() -> None:
    assert onx.cluster_embeddings(np.zeros((0, 8), dtype=np.float32)).shape == (0,)
    assert onx.cluster_embeddings(np.ones((1, 8), dtype=np.float32)).tolist() == [0]


def test_cluster_labels_follow_first_appearance() -> None:
    x, _ = blobs([40, 40])
    lab = onx.cluster_embeddings(x)
    assert lab[0] == 0


def test_cluster_caps_speakers_and_subsamples_large_inputs() -> None:
    x, _ = blobs([20] * 6, noise=0.05)
    assert len(set(onx.cluster_embeddings(x, max_speakers=3).tolist())) <= 3
    big, yb = blobs([700, 700, 700], noise=0.15)
    lab = onx.cluster_embeddings(big, max_points=300)  # submuestra + asignacion al centroide
    assert same_partition(lab, yb)


def test_small_similar_cluster_is_merged_but_distinct_small_one_is_kept() -> None:
    x, y = blobs([80, 80], noise=0.1)
    rng = np.random.default_rng(3)
    # 6 ventanas (4,5 s) del hablante 0 desplazadas: un cluster pequeno, parecido
    near = x[y == 0][:6] * 0.7 + 0.3 * rng.standard_normal((6, x.shape[1])).astype(np.float32) * 0.2
    near /= np.linalg.norm(near, axis=1, keepdims=True)
    lab = onx.cluster_embeddings(np.concatenate([x, near]), threshold=0.5)
    assert len(set(lab.tolist())) == 2
    # un hablante pequeno pero distinto (ortogonal) se mantiene
    x3, _ = blobs([80, 80, 6], noise=0.1, seed=5)
    assert len(set(onx.cluster_embeddings(x3).tolist())) == 3


def test_centroid_merge_joins_variants_of_one_voice() -> None:
    x, y = blobs([100, 100], noise=0.1)
    labels = (np.arange(len(x)) % 3 == 0).astype(np.int64) + 2 * y  # voz 0 partida en dos
    merged = onx._merge_close(x, labels.astype(np.int64), 0.55)
    assert same_partition(merged, y)


# ------------------------------------------------------------- ventanas y turnos


def test_window_starts() -> None:
    assert onx.window_starts(0.8) == [0.0]
    assert onx.window_starts(1.5) == [0.0]
    assert onx.window_starts(2.25) == [0.0, 0.75]
    s = onx.window_starts(3.0)
    assert s == [0.0, 0.75, 1.5]
    s = onx.window_starts(3.5)  # resto 0,5 s sin cubrir -> ventana final alineada al borde
    assert s[-1] == pytest.approx(2.0) and s[:-1] == [0.0, 0.75, 1.5]
    assert all(round(v * 100) == v * 100 or abs(v * 100 - round(v * 100)) < 1e-6 for v in s)


def test_turns_from_windows_midpoint_boundaries_and_merging() -> None:
    spans = [(0.0, 1.5), (0.75, 2.25), (1.5, 3.0), (2.25, 3.75)]
    segs = onx.turns_from_windows(spans, [0, 0, 1, 1], [(0.0, 3.75)], [0, 0, 0, 0])
    # centros 0.75, 1.5, 2.25, 3.0 -> frontera entre ventanas 1 y 2 en 1,875
    assert segs == [(0.0, 1.875, 0), (1.875, 3.75, 1)]
    # mismo hablante en dos tramos separados por <= merge_gap: se funde
    segs = onx.turns_from_windows(
        [(0.0, 1.5), (1.8, 3.3)], [0, 0], [(0.0, 1.5), (1.8, 3.3)], [0, 1], merge_gap_s=0.5
    )
    assert segs == [(0.0, 3.3, 0)]
    segs = onx.turns_from_windows(
        [(0.0, 1.5), (3.0, 4.5)], [0, 0], [(0.0, 1.5), (3.0, 4.5)], [0, 1], merge_gap_s=0.5
    )
    assert len(segs) == 2


def test_speaker_change_boundary_moves_to_the_pause_between_window_centres() -> None:
    # voz continua con una pausa breve en 1,60-1,70 s (cambio de turno sin hueco de VAD)
    audio = (0.2 * np.sin(2 * np.pi * 180 * np.arange(int(3.75 * SR)) / SR)).astype(np.float32)
    audio[int(1.60 * SR) : int(1.70 * SR)] = 0.0
    energy = onx.frame_energy(audio)
    assert energy.shape == (375,)
    spans = [(0.0, 1.5), (0.75, 2.25), (1.5, 3.0), (2.25, 3.75)]
    segs = onx.turns_from_windows(spans, [0, 0, 1, 1], [(0.0, 3.75)], [0, 0, 0, 0], energy=energy)
    assert segs[0][2] == 0 and segs[1][2] == 1
    assert 1.60 <= segs[0][1] <= 1.70 and segs[0][1] == segs[1][0]  # en la pausa, no en 1,875
    # entre ventanas del mismo hablante no se mueve nada; sin tramas, punto medio
    same = onx.turns_from_windows(spans, [0, 0, 0, 0], [(0.0, 3.75)], [0] * 4, energy=energy)
    assert same == [(0.0, 3.75, 0)]
    assert onx.quietest_point(np.zeros(0, np.float32), 1.0, 2.0) == pytest.approx(1.5)
    assert onx.frame_energy(np.zeros(10, np.float32)).shape == (0,)


def test_turns_never_overlap() -> None:
    rng = np.random.default_rng(7)
    spans = [(0.75 * i, 0.75 * i + 1.5) for i in range(40)]
    labels = rng.integers(0, 3, 40).tolist()
    segs = onx.turns_from_windows(spans, labels, [(0.0, 31.5)], [0] * 40)
    for (_, b, _), (a, _, _) in itertools.pairwise(segs):
        assert b <= a + 1e-9
    assert segs[0][0] == 0.0 and segs[-1][1] == 31.5


def test_smooth_isolated() -> None:
    lab = np.array([0, 0, 1, 0, 0, 1, 1, 2, 1], dtype=np.int64)
    out = onx.smooth_isolated(lab, [0, 0, 0, 0, 0, 0, 0, 0, 0])
    assert out.tolist() == [0, 0, 0, 0, 0, 1, 1, 1, 1]
    cross = onx.smooth_isolated(np.array([0, 1, 0], dtype=np.int64), [0, 1, 1])
    assert cross.tolist() == [0, 1, 0]  # no suaviza a traves de tramos


def test_to_turns_label_mapping_by_first_appearance() -> None:
    turns = onx.to_turns([(5.0, 6.0, 9), (0.0, 1.0, 4), (2.0, 3.0, 9), (3.0, 4.0, 2)])
    assert [(t.speaker, t.start) for t in turns] == [
        ("S1", 0.0),
        ("S2", 2.0),
        ("S3", 3.0),
        ("S2", 5.0),
    ]
    assert all(isinstance(t, Turn) for t in turns)


# ---------------------------------------------- motor con ONNX y VAD falsos


class FakeVad:
    def probs(self, audio_16k: AudioArray) -> NDArray[np.float32]:
        n = -(-len(audio_16k) // 512)
        pad = np.zeros(n * 512, dtype=np.float32)
        pad[: len(audio_16k)] = audio_16k
        return (np.abs(pad.reshape(n, 512)).mean(axis=1) > 0.02).astype(np.float32)


class FakeSession:
    """Embedding = f(frecuencia dominante). Recibe el fbank falso de ``fake_fbank``."""

    def __init__(self) -> None:
        self.batches: list[int] = []

    def run(self, names: list[str], feed: dict[str, NDArray[np.float32]]) -> list[Any]:
        f = feed["feats"]
        self.batches.append(len(f))
        v = np.abs(f[:, :, 0]).mean(axis=1)  # 1 = tono grave, 3 = tono agudo
        e = np.stack([(3 - v) / 2 + 0.05, (v - 1) / 2 + 0.05], axis=1)
        return [e.astype(np.float32)]


def fake_fbank(audio: AudioArray) -> NDArray[np.float32]:
    n = 1 + (len(audio) - onx.FRAME_LEN) // onx.FRAME_SHIFT if len(audio) >= onx.FRAME_LEN else 0
    out = np.zeros((n, 80), dtype=np.float32)
    for i in range(n):
        seg = audio[i * 160 : i * 160 + 400]
        zc = int(np.sum(np.signbit(seg[1:]) != np.signbit(seg[:-1])))
        out[i, 0] = (1.0 if zc < 40 else 3.0) * (-1.0) ** i  # alterna: el CMN no lo borra
    return out


def tone(hz: float, seconds: float, amp: float = 0.3) -> AudioArray:
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def silence(seconds: float) -> AudioArray:
    return np.zeros(int(seconds * SR), dtype=np.float32)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> onx.OnnxDiarizer:
    monkeypatch.setattr(onx, "fbank", fake_fbank)
    d = object.__new__(onx.OnnxDiarizer)
    d._sess = FakeSession()
    d._config = None
    d._threshold = onx.DEFAULT_THRESHOLD
    d._max_speakers = onx.DEFAULT_MAX_SPEAKERS
    d._vad_model = FakeVad()
    d._closed = False
    return d


def dialogue_audio() -> tuple[AudioArray, list[tuple[str, float, float]]]:
    parts = [tone(300, 6.0), silence(1.0), tone(1500, 6.0), silence(1.0), tone(300, 5.0)]
    truth = [("S1", 0.0, 6.0), ("S2", 7.0, 13.0), ("S1", 14.0, 19.0)]
    return np.concatenate(parts), truth


def test_engine_separates_two_speakers_and_labels_by_first_appearance(
    engine: onx.OnnxDiarizer,
) -> None:
    audio, truth = dialogue_audio()
    for n in (None, 2):
        turns = engine.diarize(audio, SR, n)
        assert [t.speaker for t in turns] == ["S1", "S2", "S1"]
        for t, (_, a, b) in zip(turns, truth, strict=True):
            assert t.start == pytest.approx(a, abs=0.4) and t.end == pytest.approx(b, abs=0.4)
        assert all(x.end <= y.start for x, y in itertools.pairwise(turns))
    assert len({t.speaker for t in engine.diarize(audio, SR, 1)}) == 1
    assert max(engine._sess.batches) <= onx.BATCH


def test_engine_edge_cases(engine: onx.OnnxDiarizer) -> None:
    assert engine.diarize(np.zeros(100, dtype=np.float32), SR, None) == []
    assert engine.diarize(silence(5.0), SR, None) == []
    with pytest.raises(ValueError):
        engine.diarize(tone(300, 3.0), SR, 0)
    engine.close()
    with pytest.raises(RuntimeError, match="closed"):
        engine.diarize(tone(300, 3.0), SR, None)


def test_engine_resamples_other_rates(engine: onx.OnnxDiarizer) -> None:
    import soxr

    audio, _ = dialogue_audio()
    x = np.asarray(soxr.resample(audio, SR, 48000), dtype=np.float32)
    assert [t.speaker for t in engine.diarize(x, 48000, None)] == ["S1", "S2", "S1"]


def test_engine_embed_returns_unit_vector_per_speaker(engine: onx.OnnxDiarizer) -> None:
    audio, _ = dialogue_audio()
    turns = engine.diarize(audio, SR, None)
    assert isinstance(engine, SpeakerEmbedder)
    emb = engine.embed(audio, SR, turns)
    assert set(emb) == {"S1", "S2"}
    for v in emb.values():
        assert v.dtype == np.float32 and float(np.linalg.norm(v)) == pytest.approx(1.0, abs=1e-4)
    assert float(emb["S1"] @ emb["S2"]) < 0.5
    assert engine.embed(audio, SR, [Turn("A", 0.0, 0.3)]) == {}  # < 0,8 s de voz


# -------------------------------------------------------- cableado y errores


def test_registry_wiring_and_default() -> None:
    assert DIARIZERS.target("onnx") == "micnaga.pipeline.engines.diar.onnx:create"
    assert callable(DIARIZERS.resolve("onnx"))
    cfg = Config()
    assert cfg.processing.diarization == "onnx"
    cfg.validate()
    assert models.WESPEAKER_GROUP in models.required_groups(cfg)


def test_missing_wespeaker_model_gives_helpful_error(tmp_models: Path) -> None:
    with pytest.raises(EngineUnavailableError, match="download_models"):
        create_diarizer(Config(), "onnx")


def test_missing_onnx_file_raises_engine_unavailable(tmp_path: Path) -> None:
    with pytest.raises(EngineUnavailableError, match="WeSpeaker model not found"):
        onx.OnnxDiarizer(tmp_path / "nope.onnx")


def test_corrupt_onnx_file_raises_engine_unavailable(tmp_path: Path) -> None:
    bad = tmp_path / "bad.onnx"
    bad.write_bytes(b"not an onnx model")
    with pytest.raises(EngineUnavailableError, match="cannot load WeSpeaker"):
        onx.OnnxDiarizer(bad)


# ----------------------------------------------------------- higiene de imports


def test_module_source_has_no_forbidden_imports() -> None:
    tree = ast.parse(Path(onx.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    assert not names & set(FORBIDDEN), names & set(FORBIDDEN)


def test_running_onnx_diarizer_code_does_not_load_torch_or_friends() -> None:
    code = (
        "import sys\n"
        "import numpy as np\n"
        "from micnaga.pipeline.engines.diar import onnx\n"
        "onnx.fbank(np.zeros(16000, dtype=np.float32))\n"
        "x = np.random.default_rng(0).standard_normal((50, 8)).astype(np.float32)\n"
        "onnx.cluster_embeddings(x)\n"
        "onnx.cluster_embeddings(x, 2)\n"
        f"bad = [m for m in {FORBIDDEN!r} if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


# ------------------------------------------------- con modelos reales (TTS)

DER_BOUND = 0.08  # medido ~0,02 en TTS limpio; margen para otra maquina/voces SAPI


@pytest.mark.models
@pytest.mark.timeout(900)
@pytest.mark.parametrize("n_voices", [2, 3])
def test_real_models_on_tts_dialogue_speaker_count_and_der(n_voices: int, tmp_path: Path) -> None:
    from tests.engines import diar_eval

    built = diar_eval.build_dialogue(n_voices, 75.0, tmp_path)
    if built is None:
        pytest.skip("not enough SAPI voices (needs Helena, Zira, Pablo)")
    audio, truth = built
    d = create_diarizer(Config(), "onnx")
    try:
        turns = d.diarize(audio, SR, None)
        assert len({t.speaker for t in turns}) == n_voices
        der = diar_eval.der(turns, truth)
        print(f"\n[onnx] {n_voices} voices auto: DER={der:.3f}")
        assert der < DER_BOUND
        known = d.diarize(audio, SR, n_voices)
        assert len({t.speaker for t in known}) == n_voices
        assert diar_eval.der(known, truth) < DER_BOUND
        emb = d.embed(audio, SR, turns)  # type: ignore[attr-defined]
        assert set(emb) == {f"S{i + 1}" for i in range(n_voices)}
        sims = [
            float(a @ b) for i, a in enumerate(emb.values()) for b in list(emb.values())[i + 1 :]
        ]
        assert max(sims) < 0.6 and all(v.shape == (256,) for v in emb.values())
    finally:
        d.close()


@pytest.mark.models
@pytest.mark.timeout(900)
def test_real_embedding_matches_wespeakerruntime_on_same_audio(tmp_path: Path) -> None:
    pytest.importorskip("wespeakerruntime")
    torch = pytest.importorskip("torch")
    kaldi = pytest.importorskip("torchaudio.compliance.kaldi")
    import wespeakerruntime

    from tests.engines import diar_eval

    built = diar_eval.build_dialogue(2, 20.0, tmp_path)
    if built is None:
        pytest.skip("SAPI voices unavailable")
    audio, _ = built
    d = create_diarizer(Config(), "onnx")
    onnx_file = models.group_dir(models.WESPEAKER_GROUP, Config()) / onx.WESPEAKER_FILE
    ref_model = wespeakerruntime.Speaker(onnx_path=str(onnx_file))
    try:
        for start in (0.0, 4.2, 9.7):
            w = audio[int(start * SR) : int(start * SR) + 24000]
            ref_f = kaldi.fbank(
                torch.from_numpy(w.copy())[None] * 32768,
                num_mel_bins=80,
                dither=0.0,
                sample_frequency=SR,
                window_type="hamming",
                use_energy=False,
            ).numpy()
            ref = ref_model.extract_embedding_feat((ref_f - ref_f.mean(0))[None], cmn=False)[0]
            mine_f = onx.fbank(w)
            mine = d._run((mine_f - mine_f.mean(0))[None])[0]  # type: ignore[attr-defined]
            cos = float(ref @ mine / np.linalg.norm(ref) / np.linalg.norm(mine))
            assert cos >= 0.99
    finally:
        d.close()


@pytest.mark.models
@pytest.mark.timeout(900)
def test_real_models_long_silence_and_noise_burst_make_no_ghost(tmp_path: Path) -> None:
    """Dialogo de 2 voces + 70 s de silencio + rafaga de ruido: 2 hablantes tras la etapa 7."""
    from micnaga.pipeline.engines.diar.postfilter import absorb_minor_speakers
    from tests.engines import diar_compare, diar_eval

    built = diar_compare.mixed_case(tmp_path)
    if built is None:
        pytest.skip("SAPI voices unavailable")
    audio, truth = built
    d = create_diarizer(Config(), "onnx")
    try:
        raw = d.diarize(audio, SR, None)
        turns = absorb_minor_speakers(raw, d.embed(audio, SR, raw))  # type: ignore[attr-defined]
    finally:
        d.close()
    assert len({t.speaker for t in turns}) == 2
    assert all(t.start < 65.0 for t in turns)  # nada en el silencio ni en la rafaga
    assert diar_eval.der(turns, truth) < DER_BOUND
