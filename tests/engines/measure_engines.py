"""Mide x-tiempo-real y RAM maxima de cada motor en ESTE PC (no el equipo de referencia).

    $env:MICNAGA_MODELS = "<repo>\\.models"
    python -m tests.engines.measure_engines --all --markdown docs/engine_measurements.md

Cada motor se mide en un **proceso nuevo** (el pico de RSS no se contamina con los demas). El
audio es voz SAPI sintetica (no una grabacion de usuario) repetida hasta ~5 minutos, y el ASR se
alimenta como lo hara el pipeline: VAD -> bloques <= 30 s -> ``transcribe``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from micnaga.types import AudioArray
from tests.engines import tts
from tests.synth import SR, silence

ENGINES = ["vad", "parakeet", "whisper", "diarize", "diarize-60min", "aec-webrtc", "aec-speex"]
TARGET_S = 300.0


def build_audio(tmp: Path, kind: str, target_s: float = TARGET_S) -> AudioArray:
    es, en = tts.find_voice("C0A"), tts.find_voice("409")
    if es is None:
        raise SystemExit("no Spanish SAPI voice")
    if kind == "dialogue":
        if en is None:
            raise SystemExit("no English SAPI voice")
        parts: list[AudioArray] = []
        for i, (a, b) in enumerate(
            [(tts.SPANISH_TEXT, tts.ENGLISH_TEXT)] * 4
        ):  # 4 repeticiones del par
            for j, (txt, v) in enumerate([(a, es), (b, en)]):
                p = tts.synth(txt, tmp / f"d{i}{j}.wav", v)
                assert p is not None
                parts += [tts.load(p), silence(0.8)]
        base = np.concatenate(parts)
    else:
        p = tts.synth(tts.SPANISH_TEXT, tmp / "es.wav", es)
        assert p is not None
        base = np.concatenate([tts.load(p), silence(1.2)])
    reps = int(np.ceil(target_s / (len(base) / SR)))
    return np.tile(base, reps)[: int(target_s * SR)].astype(np.float32)


def run_one(engine: str) -> dict[str, Any]:
    import psutil

    from micnaga.config import Config

    proc = psutil.Process()
    cfg = Config()
    out: dict[str, Any] = {"engine": engine}
    with tempfile.TemporaryDirectory(prefix="micnaga-measure-") as t:
        tmp = Path(t)
        long = engine == "diarize-60min"
        audio = build_audio(
            tmp,
            "dialogue" if engine.startswith("diarize") else "speech",
            3600.0 if long else TARGET_S,
        )
        out["audio_s"] = len(audio) / SR
        out["rss_before_mb"] = proc.memory_info().rss / 1e6
        if engine == "vad":
            from micnaga.pipeline import vad

            t0 = time.perf_counter()
            vad.load_model(cfg)
            out["load_s"] = time.perf_counter() - t0
            t0 = time.perf_counter()
            regions = vad.detect_speech(audio, SR, config=cfg)
            out["run_s"] = time.perf_counter() - t0
            out["regions"] = len(regions)
        elif engine in ("parakeet", "whisper"):
            from micnaga.pipeline import vad
            from micnaga.pipeline.engines.asr import create_asr

            regions = vad.detect_speech(audio, SR, config=cfg)
            blocks = vad.group_regions(regions, cfg.vad.max_block_s)
            vad.release_model()
            out["blocks"] = len(blocks)
            t0 = time.perf_counter()
            eng = create_asr(cfg, engine)
            out["load_s"] = time.perf_counter() - t0
            out["rss_loaded_mb"] = proc.memory_info().rss / 1e6
            t0 = time.perf_counter()
            n_words = 0
            for a, b in blocks:
                n_words += len(eng.transcribe(audio[int(a * SR) : int(b * SR)], SR))
            out["run_s"] = time.perf_counter() - t0
            out["words"] = n_words
            eng.close()
        elif engine.startswith("diarize"):
            from micnaga.pipeline.engines.diar import create_diarizer

            t0 = time.perf_counter()
            d = create_diarizer(cfg, "diarize")
            out["load_s"] = time.perf_counter() - t0
            t0 = time.perf_counter()
            turns = d.diarize(audio, SR, None if long else 2)  # 60 min: ruta automatica
            out["run_s"] = time.perf_counter() - t0
            out["speakers"] = len({x.speaker for x in turns})
            if not long:
                t0 = time.perf_counter()
                d.embed(audio, SR, turns)  # type: ignore[attr-defined]
                out["embed_s"] = time.perf_counter() - t0
            d.close()
        else:
            from tests.engines import aec_eval as ev

            src = ev.tts_sources(tmp)
            assert src is not None
            sc = ev.build_scenario(src[0], src[1], delay_ms=90, rt60=0.3)
            if engine == "aec-webrtc":
                from micnaga.pipeline.aec.webrtc import WebRtcEchoCanceller

                ec: Any = WebRtcEchoCanceller(auto_align=True)
            else:
                from micnaga.pipeline.aec.speex import SpeexEchoCanceller

                ec = SpeexEchoCanceller(filter_ms=256, auto_align=True, enable_preprocess=True)
            out["audio_s"] = len(sc.mic) / SR
            t0 = time.perf_counter()
            ec.process(sc.mic, sc.far, SR)
            out["run_s"] = time.perf_counter() - t0
    out["xrt"] = out["audio_s"] / out["run_s"]
    mi = proc.memory_info()
    out["rss_end_mb"] = mi.rss / 1e6
    out["peak_rss_mb"] = getattr(mi, "peak_wset", mi.rss) / 1e6
    out["threads_cfg"] = cfg.processing.threads
    return out


def markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| Motor | Audio (s) | Carga (s) | Proceso (s) | x tiempo real | Factor (proceso/duracion) "
        "| RSS pico (MB) | Notas |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        notes = ", ".join(
            f"{k}={r[k]}" for k in ("blocks", "words", "regions", "speakers") if k in r
        )
        if "embed_s" in r:
            notes += f", embed={r['embed_s']:.1f}s"
        lines.append(
            f"| {r['engine']} | {r['audio_s']:.0f} | {r.get('load_s', float('nan')):.2f} | "
            f"{r['run_s']:.1f} | {r['xrt']:.1f} | {1 / r['xrt']:.3f} | "
            f"{r['peak_rss_mb']:.0f} | {notes} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("engine", nargs="?", choices=ENGINES)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--markdown", type=Path)
    args = ap.parse_args(argv)
    if args.all:
        rows: list[dict[str, Any]] = []
        for e in ENGINES:
            res = subprocess.run(
                [sys.executable, "-m", "tests.engines.measure_engines", e],
                capture_output=True,
                text=True,
                check=False,
            )
            line = [ln for ln in res.stdout.splitlines() if ln.startswith("{")]
            if not line:
                print(f"{e} failed:\n{res.stderr[-2000:]}", file=sys.stderr)
                return 1
            rows.append(json.loads(line[-1]))
            print(line[-1], file=sys.stderr)
        table = markdown(rows)
        print(table)
        if args.markdown:
            args.markdown.write_text(table + "\n", encoding="utf-8")
        return 0
    if not args.engine:
        ap.error("engine or --all required")
    print(json.dumps(run_one(args.engine)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
