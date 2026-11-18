"""Sesiones SINTETICAS con voz TTS (Windows SAPI) en el formato exacto que escribe el ``Recorder``.

NO es una grabacion real: «Yo» es la voz Helena (es-ES) en la pista del micro y el remoto es la voz
Zira (en-US) en la pista del sistema (la unica segunda voz instalada), con turnos alternos. Se
escriben ``audio_yo.wav`` + ``audio_remoto.wav`` (PCM 16 bit mono 16 kHz) y ``capture.json``
(estado ``stopped``), igual que ``Recorder.stop()``; despues ``finalize_session`` las pasa a FLAC.

Con ``echo=True`` se anade a la pista del micro la pista del sistema convolucionada con una
respuesta de sala sintetica (altavoces): el caso que el AEC y la segunda capa de eco deben limpiar.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from micnaga import paths
from micnaga.capture.recovery import finalize_session
from micnaga.capture.session import STATE_STOPPED, write_capture_json
from micnaga.types import AudioArray, CaptureMeta, OutputKind, TrackInfo
from tests.engines import tts

SR = 16000

SPANISH = [
    "Buenos días a todos, empezamos la reunión de seguimiento del proyecto.",
    "Yo he terminado la migración de la base de datos esta semana.",
    "Falta probar la integración con el sistema de facturación.",
    "Creo que podemos cerrar el proveedor antes del viernes.",
    "Me preocupa el plazo de entrega por las vacaciones de agosto.",
    "Voy a preparar un resumen con los próximos pasos y los responsables.",
    "Necesitamos revisar el presupuesto del próximo trimestre con más detalle.",
    "Estoy de acuerdo, lo dejamos así y lo revisamos el lunes por la mañana.",
    "La nueva versión reduce el tiempo de carga casi a la mitad.",
    "Puedo encargarme de la documentación si alguien revisa mis cambios.",
    "No hay ningún bloqueo por mi parte, podemos seguir adelante con el plan.",
    "Mañana enviaré el informe completo a todo el equipo de desarrollo.",
]
ENGLISH = [
    "Thanks everyone for joining the call this morning.",
    "The testing phase finished yesterday and we found only two minor bugs.",
    "Both of them are already fixed and the build is ready for deployment.",
    "We should schedule a review with the customer before the end of the week.",
    "The vendor sent the proposal but the price went up by ten percent.",
    "I think we need to negotiate before signing anything with them.",
    "There is a risk with the database migration that we saw in staging.",
    "Could we move the design meeting to Thursday afternoon, please?",
    "The budget is approved, but we must justify every single expense.",
    "I will send you the link to the document as soon as we finish.",
    "Let us document this decision so that it does not get lost later.",
    "Overall the project is on track and the team is doing great work.",
]


@dataclass(frozen=True)
class Utt:
    track: str  # "mic" | "system"
    start: float
    end: float
    text: str


@dataclass
class TtsSession:
    folder: Path
    utterances: list[Utt]
    duration_s: float
    mic_script: list[str] = field(default_factory=list)

    @property
    def remote(self) -> list[Utt]:
        return [u for u in self.utterances if u.track == "system"]

    @property
    def mine(self) -> list[Utt]:
        return [u for u in self.utterances if u.track == "mic"]


def _speak(text: str, voice: int, path: Path) -> AudioArray:
    if tts.synth(text, path, voice) is None:
        raise RuntimeError("SAPI synthesis failed")
    x = tts.load(path)
    peak = float(np.max(np.abs(x))) or 1.0
    return np.asarray(x * (0.5 / peak), dtype=np.float32)


def build_session(
    folder: Path,
    *,
    seconds: float,
    output: OutputKind = "headphones",
    echo: bool = False,
    mic_only: bool = False,
    confirmed: bool = False,
    title: str = "Revisión semanal e2e",
    seed: int = 0,
    echo_db: float = -6.0,
) -> TtsSession:
    es, en = tts.find_voice("C0A"), tts.find_voice("409")
    if es is None or en is None:
        raise RuntimeError("SAPI voices (es-ES + en-US) not installed")
    rng = np.random.default_rng(seed)
    cache = folder.parent / f"_tts_{folder.name}"
    cache.mkdir(parents=True, exist_ok=True)
    es_clips = [_speak(t, es, cache / f"es{i}.wav") for i, t in enumerate(SPANISH)]
    en_clips = [_speak(t, en, cache / f"en{i}.wav") for i, t in enumerate(ENGLISH)]

    n = round(seconds * SR)
    mic = np.zeros(n, dtype=np.float32)
    system = np.zeros(n, dtype=np.float32)
    utts: list[Utt] = []
    t = 1.0
    i = 0
    while True:
        mine = mic_only or i % 2 == 0
        k = (i // (1 if mic_only else 2)) % len(SPANISH)
        clip = es_clips[k] if mine else en_clips[k]
        text = SPANISH[k] if mine else ENGLISH[k]
        dur = len(clip) / SR
        if t + dur > seconds - 1.0:
            break
        a = round(t * SR)
        (mic if mine else system)[a : a + len(clip)] += clip
        utts.append(Utt("mic" if mine else "system", t, t + dur, text))
        t += dur + float(rng.uniform(0.6, 1.2))
        i += 1

    noise = lambda: (0.0008 * rng.standard_normal(n)).astype(np.float32)  # noqa: E731
    system = system + noise()
    if echo and not mic_only:
        from scipy.signal import fftconvolve

        from tests.engines.aec_eval import synthetic_rir

        h = synthetic_rir(60.0, 0.3, seed=seed + 3)
        echo_sig = fftconvolve(system, h)[:n].astype(np.float32)
        voice = mic[np.abs(mic) > 1e-3]
        ech = echo_sig[np.abs(echo_sig) > 1e-3]
        v_rms = float(np.sqrt(np.mean(voice**2))) if len(voice) else 0.1
        e_rms = float(np.sqrt(np.mean(ech**2))) if len(ech) else 0.1
        mic = mic + echo_sig * (v_rms / e_rms * 10 ** (echo_db / 20))
    mic = np.clip(mic + noise(), -1.0, 1.0).astype(np.float32)

    folder.mkdir(parents=True, exist_ok=True)
    sf.write(str(folder / paths.AUDIO_ME_WAV), mic, SR, subtype="PCM_16", format="WAV")
    sf.write(str(folder / paths.AUDIO_REMOTE_WAV), system, SR, subtype="PCM_16", format="WAV")
    meta = CaptureMeta(
        started_at="2026-11-18T10:30:00+01:00",
        mic=TrackInfo(paths.AUDIO_ME_WAV, "Synthetic TTS mic (Helena)", SR, n),
        system=TrackInfo(paths.AUDIO_REMOTE_WAV, "Synthetic TTS loopback (Zira)", SR, n),
        output_kind=output,
        confirmed=confirmed,
        meet_title=title,
        meet_code="abc-defg-hij",
        extra={"state": STATE_STOPPED, "duration_s": seconds, "synthetic": True},
    )
    write_capture_json(folder, meta)
    finalize_session(folder)  # WAV -> FLAC, como hace la app al parar
    (folder / "tts_script.json").write_text(
        json.dumps([u.__dict__ for u in utts], ensure_ascii=False), encoding="utf-8"
    )
    return TtsSession(folder, utts, seconds, [u.text for u in utts if u.track == "mic"])
