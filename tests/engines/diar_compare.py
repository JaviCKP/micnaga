"""Comparativa de diarizadores sobre los mismos casos (voz SAPI sintetica + audio opcional).

    $env:MICNAGA_MODELS = "<repo>\\.models"
    python -m tests.engines.diar_compare --engine onnx --engine sherpa:<grupo> \\
        [--podcast <wav 16 kHz>] [--cache <carpeta>] [--threshold 0.5] [--minutes 5]

Casos:

* ``tts2`` / ``tts3`` / ``tts4``: dialogos de ``diar_eval.build_dialogue`` (2, 3 y 4 voces, turnos
  de 1-3 frases, sin solapes ni ruido). DER con collar 0,25 s.
* ``mixed``: dialogo de 2 voces (60 s) + 70 s de silencio + rafaga de ruido de 0,5 s + 10 s de
  silencio. La referencia es solo el dialogo: un 3.er hablante es un fantasma.
* ``podcast`` (opcional): un fichero real sin referencia (solo numero de hablantes y turnos).

Motores: los del registro (``onnx``, ``diarize``, ``pyannote``) y ``sherpa:<grupo>``
(``tests/engines/sherpa_diar.py``, solo evaluacion) con un modelo de embeddings de
``models.CANDIDATE_MANIFEST`` o ``MANIFEST``. Para cada caso se da el resultado crudo del motor
y el del post-filtro de la etapa 7 (``postfilter.absorb_minor_speakers``).
Imprime una linea JSON por (motor, caso). Los dialogos se cachean en ``--cache``; con
``--dump-turns DIR`` se guardan ademas los turnos filtrados de cada (motor, umbral, caso).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from micnaga.types import AudioArray, Turn
from tests.engines.diar_eval import SR, build_dialogue, der

TTS_SEED = 1


def _cached_dialogue(
    cache: Path, speakers: int, seconds: float, seed: int
) -> tuple[AudioArray, list[Turn]] | None:
    key = cache / f"dialogue_{speakers}v_{int(seconds)}s_{seed}"
    wav, ref = key.with_suffix(".wav"), key.with_suffix(".json")
    if wav.is_file() and ref.is_file():
        x, _sr = sf.read(str(wav), dtype="float32")
        truth = [Turn(t["speaker"], t["start"], t["end"]) for t in json.loads(ref.read_text())]
        return np.asarray(x, dtype=np.float32), truth
    work = cache / f"tts_{speakers}v"
    built = build_dialogue(speakers, seconds, work, seed)
    if built is None:
        return None
    audio, truth = built
    cache.mkdir(parents=True, exist_ok=True)
    sf.write(str(wav), audio, SR, subtype="PCM_16")
    ref.write_text(json.dumps([t.__dict__ for t in truth]))
    return audio, truth


def mixed_case(cache: Path) -> tuple[AudioArray, list[Turn]] | None:
    """Dialogo de 2 voces + silencio largo + rafaga corta de ruido (no es un hablante)."""
    built = _cached_dialogue(cache, 2, 60.0, TTS_SEED + 7)
    if built is None:
        return None
    audio, truth = built
    rng = np.random.default_rng(3)
    burst = rng.normal(0.0, 0.08, int(0.5 * SR)).astype(np.float32)
    burst *= np.hanning(len(burst)).astype(np.float32)
    tail = [np.zeros(int(70 * SR), np.float32), burst, np.zeros(int(10 * SR), np.float32)]
    return np.concatenate([audio, *tail]).astype(np.float32), truth


def make_engine(name: str, threshold: float | None) -> Any:
    from micnaga import models
    from micnaga.config import Config

    cfg = Config()
    if name.startswith("sherpa:"):
        from micnaga.pipeline.engines.diar.onnx import WESPEAKER_FILE
        from tests.engines import sherpa_diar as sherpa

        group_id = name.split(":", 1)[1]
        group = {**models.CANDIDATE_MANIFEST, **models.MANIFEST}[group_id]
        seg = models.group_dir(sherpa.SEGMENTATION_GROUP, cfg) / sherpa.SEGMENTATION_FILE
        emb = models.group_dir(group_id, cfg) / group.files[0].name
        wes = models.group_dir(models.WESPEAKER_GROUP, cfg) / WESPEAKER_FILE
        return sherpa.SherpaDiarizer(
            seg,
            emb,
            threads=cfg.processing.threads,
            threshold=sherpa.DEFAULT_THRESHOLD if threshold is None else threshold,
            wespeaker=wes,
            config=cfg,
            name=name,
        )
    from micnaga.pipeline.engines.diar import create_diarizer

    return create_diarizer(cfg, name)


def evaluate(
    engine: Any, audio: AudioArray, truth: list[Turn] | None, dump: Path | None = None
) -> dict[str, Any]:
    from micnaga.pipeline.engines.diar.base import SpeakerEmbedder
    from micnaga.pipeline.engines.diar.postfilter import absorb_minor_speakers, speech_by_speaker

    t0 = time.perf_counter()
    raw = engine.diarize(audio, SR, None)
    run_s = time.perf_counter() - t0
    emb = engine.embed(audio, SR, raw) if isinstance(engine, SpeakerEmbedder) and raw else {}
    filtered = absorb_minor_speakers(raw, emb)
    if dump is not None:
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(json.dumps([t.__dict__ for t in filtered]), encoding="utf-8")
    res: dict[str, Any] = {
        "audio_s": round(len(audio) / SR, 1),
        "run_s": round(run_s, 2),
        "xrt": round(len(audio) / SR / max(run_s, 1e-6), 1),
        "raw_speakers": len({t.speaker for t in raw}),
        "raw_speech_s": {k: round(v, 1) for k, v in sorted(speech_by_speaker(raw).items())},
        "speakers": len({t.speaker for t in filtered}),
        "turns": len(filtered),
    }
    if truth is not None:
        res["true_speakers"] = len({t.speaker for t in truth})
        res["raw_der"] = round(der(raw, truth), 4)
        res["der"] = round(der(filtered, truth), 4)
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", action="append", required=True)
    ap.add_argument("--podcast", type=Path)
    ap.add_argument("--cache", type=Path, default=Path("diar_cache"))
    ap.add_argument("--threshold", type=float)
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--cases", default="tts2,tts3,tts4,mixed,podcast")
    ap.add_argument("--dump-turns", type=Path)
    a = ap.parse_args()
    wanted = set(a.cases.split(","))
    cases: dict[str, tuple[AudioArray, list[Turn] | None]] = {}
    for n in (2, 3, 4):
        if f"tts{n}" in wanted:
            built = _cached_dialogue(a.cache, n, a.minutes * 60, TTS_SEED)
            if built is not None:
                cases[f"tts{n}"] = built
    if "mixed" in wanted:
        mixed = mixed_case(a.cache)
        if mixed is not None:
            cases["mixed"] = mixed
    if "podcast" in wanted and a.podcast is not None:
        x, sr = sf.read(str(a.podcast), dtype="float32")
        assert sr == SR
        cases["podcast"] = (np.asarray(x, dtype=np.float32), None)
    for name in a.engine:
        try:
            engine = make_engine(name, a.threshold)
        except Exception as exc:
            print(json.dumps({"engine": name, "error": f"{type(exc).__name__}: {exc}"}))
            continue
        for case, (audio, truth) in cases.items():
            dump = None
            if a.dump_turns is not None:
                tag = name.replace(":", "_") + (f"_{a.threshold}" if a.threshold else "")
                dump = a.dump_turns / f"{tag}_{case}.json"
            res = evaluate(engine, audio, truth, dump)
            print(json.dumps({"engine": name, "threshold": a.threshold, "case": case, **res}))
        engine.close()


if __name__ == "__main__":
    main()
