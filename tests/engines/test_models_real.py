"""Pruebas con modelos reales (``MICNAGA_RUN_MODELS=1``). Audio: voces SAPI de Windows.

OJO: el WER de aqui es sobre voz sintetica limpia, NO el set de prueba real de la spec §12
(grabaciones de reuniones). Solo demuestra que el motor funciona y que los tiempos son sanos.
"""

from __future__ import annotations

import itertools
import json
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from micnaga import models, paths
from micnaga.config import Config
from micnaga.pipeline import vad
from micnaga.pipeline.aec.speex import SpeexEchoCanceller
from micnaga.pipeline.aec.webrtc import WebRtcEchoCanceller
from micnaga.pipeline.engines.asr import create_asr
from micnaga.pipeline.engines.diar import create_diarizer
from micnaga.types import AudioArray, Turn, Word
from tests.engines import tts
from tests.synth import SR, silence

pytestmark = [pytest.mark.models, pytest.mark.timeout(600)]


def wer(reference: str, hypothesis: str) -> float:
    import re

    import jiwer

    def norm(s: str) -> str:
        return " ".join(re.sub(r"[^\w\s]", " ", s.lower()).split())

    return float(jiwer.wer(norm(reference), norm(hypothesis)))


@pytest.fixture(scope="module")
def spanish_audio(tmp_path_factory: pytest.TempPathFactory) -> AudioArray:
    voice = tts.find_voice("C0A")
    if voice is None:
        pytest.skip("no Spanish SAPI voice installed")
    p = tts.synth(tts.SPANISH_TEXT, tmp_path_factory.mktemp("tts") / "es.wav", voice)
    if p is None:
        pytest.skip("SAPI synthesis failed")
    return tts.load(p)


@pytest.fixture(scope="module")
def dialogue(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[AudioArray, list[tuple[str, float, float]]]:
    """Dialogo de dos voces SAPI distintas (Helena es-ES / Zira en-US) con turnos conocidos."""
    es, en = tts.find_voice("C0A"), tts.find_voice("409")
    if es is None or en is None:
        pytest.skip("need two different SAPI voices (Spanish + English)")
    d = tmp_path_factory.mktemp("dlg")
    lines = [
        ("S1", es, "Buenos días a todos. Empezamos la reunión de seguimiento del proyecto."),
        ("S2", en, "Thanks everyone for joining. I would like to share a quick update."),
        ("S1", es, "Hoy vamos a revisar el estado del despliegue y los próximos pasos."),
        ("S2", en, "The testing phase finished yesterday and we found only two minor bugs."),
        ("S1", es, "Falta probar la integración con el sistema de facturación."),
        ("S2", en, "Both of them are already fixed and the build is ready for deployment."),
        ("S1", es, "Después hablaremos del presupuesto del próximo trimestre."),
        ("S2", en, "Please let me know if you have any questions about the schedule."),
    ]
    parts: list[AudioArray] = []
    truth: list[tuple[str, float, float]] = []
    t = 0.0
    gap = silence(0.6)
    for i, (who, voice, text) in enumerate(lines):
        p = tts.synth(text, d / f"{i}.wav", voice)
        if p is None:
            pytest.skip("SAPI synthesis failed")
        x = tts.load(p)
        nz = np.flatnonzero(np.abs(x) > 0.01)
        x = x[nz[0] : nz[-1] + 1]
        truth.append((who, t, t + len(x) / SR))
        parts += [x, gap]
        t += len(x) / SR + 0.6
    return np.concatenate(parts), truth


def check_words(words: list[Word], duration: float) -> None:
    assert len(words) > 10
    for w in words:
        assert 0.0 <= w.s <= w.e <= duration + 0.5, w
        assert 0.0 <= w.p <= 1.0
        assert w.w.strip() == w.w and w.w
    assert all(a.s <= b.s for a, b in itertools.pairwise(words))


@pytest.mark.parametrize("engine_name", ["parakeet", "whisper"])
def test_asr_word_timestamps_and_wer_on_spanish_tts(
    engine_name: str, spanish_audio: AudioArray
) -> None:
    eng = create_asr(Config(), engine_name)
    try:
        t0 = time.perf_counter()
        words = eng.transcribe(spanish_audio, SR)
        elapsed = time.perf_counter() - t0
    finally:
        eng.close()
    check_words(words, len(spanish_audio) / SR)
    text = " ".join(w.w for w in words)
    error = wer(tts.SPANISH_TEXT, text)
    print(
        f"\n[{engine_name}] WER={error:.3f} xRT={len(spanish_audio) / SR / elapsed:.1f} :: {text}"
    )
    assert error <= 0.12
    # el texto trae puntuacion y mayusculas (la ASR las emite)
    assert any(w.w.endswith(".") for w in words)
    assert words[0].w[0].isupper()


def test_parakeet_handles_input_longer_than_30s(spanish_audio: AudioArray) -> None:
    long_audio = np.concatenate(
        [spanish_audio, silence(1.0), spanish_audio, silence(1.0), spanish_audio]
    )
    assert len(long_audio) / SR > 60
    eng = create_asr(Config(), "parakeet")
    try:
        words = eng.transcribe(long_audio, SR)
    finally:
        eng.close()
    check_words(words, len(long_audio) / SR)
    assert wer(" ".join([tts.SPANISH_TEXT] * 3), " ".join(w.w for w in words)) <= 0.12


def test_vad_real_model_on_speech_with_gaps(spanish_audio: AudioArray) -> None:
    audio = np.concatenate([silence(3.0), spanish_audio, silence(4.0)])
    regions = vad.detect_speech(audio, SR, config=Config())
    vad.release_model()
    assert regions
    assert regions[0].start == pytest.approx(3.0, abs=0.6)
    assert regions[-1].end == pytest.approx(3.0 + len(spanish_audio) / SR, abs=1.2)
    covered = vad.speech_seconds(regions)
    assert 0.6 * len(spanish_audio) / SR < covered < len(spanish_audio) / SR + 2
    # nada de voz en silencio puro
    assert vad.detect_speech(silence(10.0), SR) == []
    vad.release_model()
    blocks = vad.group_regions(regions, 30.0)
    assert all(b - a <= 30.0 + 1e-6 for a, b in blocks)


def frame_accuracy(turns: list[Turn], truth: list[tuple[str, float, float]], dur: float) -> float:
    step = 0.05
    n = int(dur / step)
    ref = np.full(n, -1)
    hyp = np.full(n, -1)
    for who, a, b in truth:
        ref[int(a / step) : int(b / step)] = int(who[1:])
    labels = sorted({t.speaker for t in turns})
    for t in turns:
        hyp[int(t.start / step) : int(t.end / step)] = labels.index(t.speaker)
    m = ref >= 0
    correct = 0
    for h in set(hyp[m].tolist()):
        if h < 0:
            continue
        correct += Counter(ref[m & (hyp == h)].tolist()).most_common(1)[0][1]
    return correct / float(m.sum())


def test_diarize_two_voice_tts_dialogue(
    dialogue: tuple[AudioArray, list[tuple[str, float, float]]],
) -> None:
    audio, truth = dialogue
    dur = len(audio) / SR
    d = create_diarizer(Config(), "diarize")
    try:
        turns = d.diarize(audio, SR, 2)
        assert {t.speaker for t in turns} == {"S1", "S2"}
        assert frame_accuracy(turns, truth, dur) > 0.9
        # estimacion automatica (la ruta real del pipeline): n_speakers=None
        auto = d.diarize(audio, SR, None)
        n_auto = len({t.speaker for t in auto})
        print(f"\n[diarize] auto-estimated speakers on 2-voice TTS: {n_auto}")
        assert n_auto == 2  # fusion de centroides sobre la estimacion automatica de diarize
        # huellas: embeddings coherentes (misma voz parecida, voces distintas no)
        emb = d.embed(audio, SR, turns)  # type: ignore[attr-defined]
        assert set(emb) == {"S1", "S2"}
        for v in emb.values():
            assert v.shape == (256,) and v.dtype == np.float32
            assert float(np.linalg.norm(v)) == pytest.approx(1.0, abs=1e-3)
        cross = float(emb["S1"] @ emb["S2"])
        s1 = [t for t in turns if t.speaker == "S1"]
        half = len(s1) // 2
        a = d.embed(audio, SR, [Turn("A", t.start, t.end) for t in s1[:half]])["A"]  # type: ignore[attr-defined]
        b = d.embed(audio, SR, [Turn("A", t.start, t.end) for t in s1[half:]])["A"]  # type: ignore[attr-defined]
        same = float(a @ b)
        print(f"[diarize] embedding cosine same-speaker={same:.2f} cross-speaker={cross:.2f}")
        assert same > cross + 0.3
    finally:
        d.close()


def test_pyannote_adapter_skips_when_not_installed() -> None:
    pytest.importorskip("pyannote.audio")
    folder = paths.models_dir() / "pyannote-community-1"
    if not (folder / "config.yaml").is_file():
        pytest.skip(f"community-1 files not found in {folder}")
    d = create_diarizer(Config(), "pyannote")
    ref = np.zeros(SR, dtype=np.float32)
    assert d.diarize(ref, SR, 2) == []
    d.close()


def test_real_aec_on_tts_echo(tmp_path: Path) -> None:
    """Eco sintetico con voz SAPI: ambos motores reducen el eco de forma clara."""
    from tests.engines import aec_eval as ev

    src = ev.tts_sources(tmp_path)
    if src is None:
        pytest.skip("need two SAPI voices")
    far, near = src
    sc = ev.build_scenario(far, near, delay_ms=90, rt60=0.3)
    for make in (
        lambda: WebRtcEchoCanceller(auto_align=True),
        lambda: SpeexEchoCanceller(filter_ms=256, auto_align=True, enable_preprocess=True),
    ):
        ec = make()
        out = ec.process(sc.mic, sc.far, SR)
        res = ev.evaluate(sc, out)
        print(f"\n[{ec.name}] ERLE late={res['erle_late_db']:.1f} dB")
        assert res["erle_late_db"] > 20.0


def test_download_real_silero_from_huggingface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Descarga real (red) del fichero mas pequeno del manifiesto: valida URL fijada y SHA-256."""
    target = tmp_path / "models"
    monkeypatch.setenv(paths.ENV_MODELS, str(target))
    cfg = Config()
    done = models.download_models(cfg, groups=[models.SILERO_GROUP])
    assert [p.name for p in done] == ["silero_vad.onnx"]
    assert models.verify_models(cfg, groups=[models.SILERO_GROUP]) == []
    assert json.dumps(models.MANIFEST[models.SILERO_GROUP].files[0].size)


def test_download_real_small_non_lfs_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ficheros pequenos que no son LFS (redireccion relativa 307 de Hugging Face)."""
    import dataclasses

    monkeypatch.setenv(paths.ENV_MODELS, str(tmp_path / "models"))
    small = {
        models.PARAKEET_GROUP: ("config.json", "vocab.txt"),
        models.WHISPER_SMALL_GROUP: ("config.json", "tokenizer.json", "vocabulary.txt"),
    }
    manifest = {
        gid: dataclasses.replace(
            models.MANIFEST[gid],
            files=tuple(f for f in models.MANIFEST[gid].files if f.name in names),
        )
        for gid, names in small.items()
    }
    cfg = Config()
    done = models.download_models(cfg, groups=list(manifest), manifest=manifest)
    assert len(done) == 5
    assert models.verify_models(cfg, groups=list(manifest), manifest=manifest) == []
