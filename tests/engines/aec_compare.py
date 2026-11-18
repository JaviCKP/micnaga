"""Comparativa WebRTC AEC3 vs SpeexDSP (spec F1, MIC-05). Se ejecuta a mano:

    $env:MICNAGA_MODELS = "<repo>\\.models"
    .venv\\Scripts\\python.exe -m tests.engines.aec_compare [--asr] [--json salida.json]

Imprime tablas en Markdown (las que se copian a ``docs/aec_comparison.md``). ``--asr`` carga
Parakeet y anade las metricas a nivel de ASR (palabras filtradas del lejano y WER relativo del
hablante cercano en doble conversacion). Todo es audio sintetico: voces SAPI (si estan) y voces
armonicas de ``tests/synth.py``, con respuestas al impulso de sala sinteticas.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from micnaga.pipeline.aec.speex import SpeexEchoCanceller
from micnaga.pipeline.aec.webrtc import WebRtcEchoCanceller
from micnaga.types import AudioArray
from tests.engines import aec_eval as ev

Process = Callable[[AudioArray, AudioArray, int], AudioArray]

CANDIDATES: dict[str, Callable[[], Any]] = {
    "webrtc": lambda: WebRtcEchoCanceller(),
    "webrtc+align": lambda: WebRtcEchoCanceller(auto_align=True),
    "webrtc+align+warm5s": lambda: WebRtcEchoCanceller(auto_align=True, warmup_s=5.0),
    "speex128": lambda: SpeexEchoCanceller(filter_ms=128),
    "speex128+align": lambda: SpeexEchoCanceller(filter_ms=128, auto_align=True),
    "speex256+align": lambda: SpeexEchoCanceller(filter_ms=256, auto_align=True),
    "speex128+align+pre": lambda: SpeexEchoCanceller(
        filter_ms=128, auto_align=True, enable_preprocess=True
    ),
    "speex256+align+pre": lambda: SpeexEchoCanceller(
        filter_ms=256, auto_align=True, enable_preprocess=True
    ),
}

GRID = [(30.0, 0.15), (90.0, 0.30), (150.0, 0.40)]


def scenarios(tmp: Path, echo_db: float = 0.0, tts_only: bool = False) -> list[ev.Scenario]:
    out: list[ev.Scenario] = []
    synth_far = ev.synthetic_voice(11, 110)
    synth_near = ev.synthetic_voice(22, 190)
    tts = ev.tts_sources(tmp)
    if tts is not None:
        far, near = tts
        for d, rt in GRID:
            out.append(
                ev.build_scenario(
                    far, near, delay_ms=d, rt60=rt, echo_db=echo_db, name=f"tts d{int(d)} rt{rt}"
                )
            )
        out.append(
            ev.build_scenario(
                far,
                near,
                delay_ms=90,
                rt60=0.3,
                nonlinear=True,
                echo_db=echo_db,
                name="tts d90 rt0.3 NL",
            )
        )
    for d, rt in [] if tts_only else GRID:
        out.append(
            ev.build_scenario(
                synth_far,
                synth_near,
                delay_ms=d,
                rt60=rt,
                echo_db=echo_db,
                name=f"synth d{int(d)} rt{rt}",
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asr", action="store_true", help="metricas a nivel de ASR (Parakeet)")
    ap.add_argument("--json", type=Path, help="guarda los resultados crudos")
    ap.add_argument("--echo-db", type=float, default=0.0, help="nivel del eco sobre la voz cercana")
    ap.add_argument("--only", help="candidatos separados por comas (ademas de 'none')")
    ap.add_argument("--tts-only", action="store_true", help="solo escenarios con voz SAPI")
    args = ap.parse_args(argv)

    transcribe: Callable[[AudioArray], str] | None = None
    if args.asr:
        from micnaga.config import Config
        from micnaga.pipeline.engines.asr import create_asr

        engine = create_asr(Config())

        def transcribe(a: AudioArray) -> str:
            if len(a) < 4800:
                return ""
            return " ".join(w.w for w in engine.transcribe(a, 16000))

    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="micnaga-aec-") as tmp_name:
        scs = scenarios(Path(tmp_name), args.echo_db, args.tts_only)
        names = args.only.split(",") if args.only else list(CANDIDATES)
        for sc in scs:
            for name, make in [("none", None), *((n, CANDIDATES[n]) for n in names)]:
                if make is None:
                    out, xrt = sc.mic, float("nan")
                else:
                    out, xrt = ev.run_canceller(make().process, sc)
                res: dict[str, Any] = {"scenario": sc.name, "candidate": name, "xrt": xrt}
                res.update(ev.evaluate(sc, out))
                if transcribe is not None and sc.name.startswith("tts"):
                    res.update(ev.asr_metrics(sc, out, transcribe))
                rows.append(res)
                print(
                    json.dumps(
                        {k: (round(v, 2) if isinstance(v, float) else v) for k, v in res.items()}
                    ),
                    file=sys.stderr,
                )
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(render(rows))
    return 0


def render(rows: list[dict[str, Any]]) -> str:
    cands = list(dict.fromkeys(r["candidate"] for r in rows))
    metrics = [
        ("erle_first5_db", "ERLE 0-5 s"),
        ("erle_conv_db", "ERLE 5-15 s"),
        ("erle_late_db", "ERLE 35-50 s"),
        ("dt_sdr_db", "Doble conv. SDR"),
        ("dt_gain_db", "Doble conv. ganancia"),
        ("near_only_sdr_db", "Solo cercano SDR"),
        ("xrt", "x tiempo real"),
        ("leak_words", "Palabras filtradas"),
        ("dt_wer_rel", "WER rel. doble conv."),
    ]
    lines = ["| Candidato | " + " | ".join(m[1] for m in metrics) + " |"]
    lines.append("|---|" + "---:|" * len(metrics))
    for c in cands:
        sel = [r for r in rows if r["candidate"] == c]
        cells = []
        for key, _ in metrics:
            vals = [r[key] for r in sel if key in r and not np.isnan(r[key])]
            cells.append(f"{np.mean(vals):.1f}" if vals else "-")
        lines.append(f"| {c} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
