"""Evaluacion SINTETICA de diarizadores (voces SAPI/OneCore de Windows; nunca grabaciones reales).

    $env:MICNAGA_MODELS = "<repo>\\.models"
    python -m tests.engines.diar_eval --engine diarize --minutes 5 --speakers 3 [--n-known]

Cada ejecucion es un proceso nuevo (el pico de RAM no se contamina). Voces: Helena, Laura, Pablo
(es-ES; Laura y Pablo son OneCore, accesibles por SAPI con su token) y Zira (en-US).
Las voces TTS son irrealmente distintas y consistentes: los numeros NO valen para voces reales.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path
from typing import Any

import numpy as np

from micnaga.types import AudioArray, Turn
from tests.engines import tts

SR = 16000
HKLM_ONECORE = "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Speech_OneCore\\Voices\\Tokens\\"
HKLM_DESKTOP = "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Speech\\Voices\\Tokens\\"
VOICES: list[tuple[str, str, str]] = [  # (id, token, idioma)
    ("helena", HKLM_DESKTOP + "TTS_MS_ES-ES_HELENA_11.0", "es"),
    ("zira", HKLM_DESKTOP + "TTS_MS_EN-US_ZIRA_11.0", "en"),
    ("pablo", HKLM_ONECORE + "MSTTS_V110_esES_PabloM", "es"),
    ("laura", HKLM_ONECORE + "MSTTS_V110_esES_LauraM", "es"),
]
SENTENCES = {
    "es": [
        "Buenos días a todos. Empezamos la reunión de seguimiento del proyecto.",
        "Hoy vamos a revisar el estado del despliegue y los próximos pasos.",
        "El equipo de desarrollo ha terminado la migración de la base de datos.",
        "Falta probar la integración con el sistema de facturación.",
        "Después hablaremos del presupuesto del próximo trimestre, que preocupa bastante.",
        "Yo creo que deberíamos aplazar la entrega una semana para hacerlo bien.",
        "¿Alguien tiene alguna duda sobre el calendario que acabo de enseñar?",
        "Perfecto, entonces quedamos así y mandamos el resumen por correo esta tarde.",
        "He hablado con el cliente y está de acuerdo con el nuevo alcance.",
        "Necesitamos más pruebas antes de pasar a producción, sobre todo de rendimiento.",
    ],
    "en": [
        "Thanks everyone for joining. I would like to share a quick update about the new release.",
        "The testing phase finished yesterday and we found only two minor bugs.",
        "Both of them are already fixed and the build is ready for deployment.",
        "I think we should postpone the delivery by one week to get it right.",
        "Does anybody have questions about the schedule I just presented?",
        "Great, so we agree and I will send the summary by email this afternoon.",
        "I spoke with the customer and they accept the new scope of the project.",
        "We need more performance testing before going to production.",
    ],
}


def available_voices() -> list[tuple[str, str, str]]:
    import win32com.client as w

    ok: list[tuple[str, str, str]] = []
    for vid, token, lang in VOICES:
        try:
            tok = w.Dispatch("SAPI.SpObjectToken")
            tok.SetId(token)
            ok.append((vid, token, lang))
        except Exception:  # pragma: no cover
            continue
    return ok


def synth_token(text: str, path: Path, token: str, rate: int = 0) -> AudioArray | None:
    try:
        import win32com.client as w

        tok = w.Dispatch("SAPI.SpObjectToken")
        tok.SetId(token)
        sp = w.Dispatch("SAPI.SpVoice")
        sp.Voice = tok
        sp.Rate = rate
        st = w.Dispatch("SAPI.SpFileStream")
        fmt = w.Dispatch("SAPI.SpAudioFormat")
        fmt.Type = tts.SAFT_16KHZ_16BIT_MONO
        st.Format = fmt
        path.parent.mkdir(parents=True, exist_ok=True)
        st.Open(str(path), 3)
        sp.AudioOutputStream = st
        sp.Speak(text)
        st.Close()
    except Exception:  # pragma: no cover - SAPI no disponible
        return None
    if not path.exists() or path.stat().st_size < 1000:
        return None
    x = tts.load(path)
    nz = np.flatnonzero(np.abs(x) > 0.01)
    return x[nz[0] : nz[-1] + 1] if len(nz) else None


def build_dialogue(
    n_speakers: int, duration_s: float, work: Path, seed: int = 1
) -> tuple[AudioArray, list[Turn]] | None:
    """Dialogo sintetico de ``n_speakers`` voces (2..4), turnos de 1-3 frases, huecos 0,3-1,2 s.

    Se sintetiza cada (voz, frase, velocidad) una vez y se reusa (en 60 min se repiten, mezcladas
    de otra forma). Devuelve ``(audio, turnos reales con S1.. por orden de voz)``.
    """
    voices = available_voices()[:n_speakers]
    if len(voices) < n_speakers:
        return None
    rng = random.Random(seed)
    bank: dict[tuple[int, int, int], AudioArray] = {}
    for vi, (vid, token, lang) in enumerate(voices):
        for si, text in enumerate(SENTENCES[lang]):
            for rate in (0, 1):
                x = synth_token(text, work / f"{vid}_{si}_{rate}.wav", token, rate)
                if x is None:
                    return None
                bank[(vi, si, rate)] = x
    parts: list[AudioArray] = []
    truth: list[Turn] = []
    t = 0.0
    prev = -1
    while t < duration_s:
        vi = rng.choice([v for v in range(n_speakers) if v != prev])
        prev = vi
        lang = voices[vi][2]
        start = t
        for _ in range(rng.choice([1, 1, 2, 3])):
            x = bank[(vi, rng.randrange(len(SENTENCES[lang])), rng.randrange(2))]
            parts += [x, np.zeros(int(rng.uniform(0.15, 0.4) * SR), dtype=np.float32)]
            t += len(x) / SR + (len(parts[-1]) / SR)
        truth.append(Turn(f"S{vi + 1}", start, t - len(parts[-1]) / SR))
        gap = rng.uniform(0.3, 1.0)
        parts.append(np.zeros(int(gap * SR), dtype=np.float32))
        t += gap
    return np.concatenate(parts).astype(np.float32), truth


def der(hyp: list[Turn], truth: list[Turn], collar: float = 0.25) -> float:
    from pyannote.core import Annotation, Segment
    from pyannote.metrics.diarization import DiarizationErrorRate

    def ann(turns: list[Turn]) -> Annotation:
        a = Annotation()
        for tn in turns:
            a[Segment(tn.start, tn.end)] = tn.speaker
        return a

    return float(DiarizationErrorRate(collar=collar, skip_overlap=False)(ann(truth), ann(hyp)))


def run_one(engine: str, minutes: float, speakers: int, n_known: bool, seed: int) -> dict[str, Any]:
    import tempfile

    import psutil

    from micnaga.config import Config
    from micnaga.pipeline.engines.diar import create_diarizer

    with tempfile.TemporaryDirectory(prefix="micnaga-dereval-") as td:
        built = build_dialogue(speakers, minutes * 60, Path(td), seed)
        if built is None:
            return {"error": "voices unavailable"}
        audio, truth = built
        proc = psutil.Process()
        rss0 = proc.memory_info().rss / 1e6
        t0 = time.perf_counter()
        d = create_diarizer(Config(), engine)
        load_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        turns = d.diarize(audio, SR, speakers if n_known else None)
        run_s = time.perf_counter() - t0
        peak = proc.memory_info().peak_wset / 1e6
        res: dict[str, Any] = {
            "engine": engine,
            "minutes": minutes,
            "true_speakers": speakers,
            "n_known": n_known,
            "est_speakers": len({t.speaker for t in turns}),
            "der": round(der(turns, truth), 4),
            "load_s": round(load_s, 2),
            "run_s": round(run_s, 2),
            "xrt": round(len(audio) / SR / run_s, 1),
            "rss_before_mb": round(rss0),
            "peak_mb": round(peak),
            "audio_s": round(len(audio) / SR, 1),
        }
        d.close()
        return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="diarize")
    ap.add_argument("--minutes", type=float, default=1.0)
    ap.add_argument("--speakers", type=int, default=2)
    ap.add_argument("--n-known", action="store_true")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    print(json.dumps(run_one(a.engine, a.minutes, a.speakers, a.n_known, a.seed)))


if __name__ == "__main__":
    main()
