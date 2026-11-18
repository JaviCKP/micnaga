"""Recuperacion tras cierre inesperado (MIC-09) y paso a FLAC (``finalize_session``).

Ciclo de vida de una sesion (``capture.json -> state``)::

    recording --stop()--> stopped --finalize_session()--> finalized
        |                                                      ^
        +--(cierre brusco)--> recover_sessions() --> recovered --+

``recover_sessions(root)`` debe llamarse **al arrancar la app** (y ``micnaga record --recover``
lo hace a mano): busca carpetas cuyo ``capture.json`` sigue en ``recording`` y cuyo proceso ya
no existe, repara los WAV (el numero de muestras sale del tamano del fichero; ``capture.json``
puede ir 10 s por detras), iguala las dos pistas con silencio y las marca ``recovered``. Devuelve
las carpetas recuperadas **en esa llamada** para que la app las encole; una segunda llamada no las
vuelve a devolver (``find_sessions`` permite listar por estado si hace falta reencolar).
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from micnaga import paths
from micnaga.capture import wavio
from micnaga.capture.session import (
    STATE_FINALIZED,
    STATE_RECORDING,
    STATE_RECOVERED,
    read_capture_json,
    read_state,
    write_capture_json,
)
from micnaga.types import CaptureMeta, SyncMark, TrackInfo

log = logging.getLogger(__name__)


def find_sessions(root: Path, states: tuple[str, ...]) -> list[Path]:
    """Carpetas hijas de ``root`` con ``capture.json`` cuyo ``state`` esta en ``states``."""
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and read_state(d) in states)


def _owner_alive(extra: dict[str, object]) -> bool:
    """``True`` si el proceso que grababa (pid + hora de creacion) sigue vivo."""
    pid = extra.get("pid")
    if not isinstance(pid, int):
        return False
    if pid == os.getpid():
        return True
    try:
        import psutil

        if not psutil.pid_exists(pid):
            return False
        created = extra.get("pid_create_time")
        if isinstance(created, int | float):
            return abs(psutil.Process(pid).create_time() - float(created)) < 2.0
        return True
    except Exception:
        return False


def recover_session(session_dir: Path) -> CaptureMeta | None:
    """Repara una sesion interrumpida. ``None`` si no hace falta (o si aun se esta grabando)."""
    try:
        meta = read_capture_json(session_dir)
    except (OSError, ValueError, KeyError) as exc:
        log.warning("unreadable capture.json in %s: %s", session_dir, exc)
        return None
    if meta.extra.get("state") != STATE_RECORDING or _owner_alive(meta.extra):
        return None
    rate = meta.mic.sample_rate or 16000
    mic_wav = session_dir / (meta.mic.file or paths.AUDIO_ME_WAV)
    sys_wav = session_dir / (meta.system.file or paths.AUDIO_REMOTE_WAV)
    n_mic = wavio.repair_wav(mic_wav, rate)
    n_sys = wavio.repair_wav(sys_wav, rate)
    target = max(n_mic, n_sys)
    padded = {"mic": target - n_mic, "system": target - n_sys}
    for wav, missing in ((mic_wav, padded["mic"]), (sys_wav, padded["system"])):
        if missing > 0:
            wavio.append_zeros(wav, missing)

    def trimmed(track: TrackInfo, n: int) -> TrackInfo:
        marks: list[SyncMark] = [m for m in track.sync if m.samples <= n]
        return replace(track, samples=n, sync=marks)

    extra = dict(meta.extra)
    extra["state"] = STATE_RECOVERED
    extra["duration_s"] = round(target / rate, 3)
    extra["recovered"] = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mic_samples": n_mic,
        "system_samples": n_sys,
        "padded_samples": padded,
    }
    new = replace(
        meta, mic=trimmed(meta.mic, target), system=trimmed(meta.system, target), extra=extra
    )
    write_capture_json(session_dir, new)
    log.info("recovered %s (%.1f s)", session_dir, target / rate)
    return new


def recover_sessions(root: Path | None = None) -> list[Path]:
    """Recupera todas las sesiones interrumpidas bajo ``root`` (por defecto la raiz de reuniones).

    Devuelve las carpetas recuperadas en esta llamada, listas para encolar.
    """
    base = root if root is not None else paths.meetings_root()
    recovered: list[Path] = []
    for d in find_sessions(base, (STATE_RECORDING,)):
        try:
            if recover_session(d) is not None:
                recovered.append(d)
        except Exception:
            log.exception("could not recover %s", d)
    return recovered


def finalize_session(session_dir: Path, *, flac_level: int = 5) -> CaptureMeta:
    """WAV -> FLAC (nivel ``flac_level``, 5 por defecto) y borra los WAV **tras verificarlos**.

    Idempotente: si ya esta en FLAC no hace nada; si el proceso se interrumpio entre la
    conversion y el borrado, se reverifica y se borra. Una sesion ``recording`` huerfana se
    recupera antes; si todavia la esta grabando otro proceso lanza ``RuntimeError``.
    Actualiza ``capture.json`` (``file`` apunta a los FLAC, ``state = "finalized"``).
    """
    meta = read_capture_json(session_dir)
    if meta.extra.get("state") == STATE_RECORDING:
        recovered = recover_session(session_dir)
        if recovered is None:
            raise RuntimeError(f"{session_dir} is still being recorded")
        meta = recovered
    rate = meta.mic.sample_rate or 16000
    tracks: dict[str, TrackInfo] = {}
    for key, info, flac_name in (
        ("mic", meta.mic, paths.AUDIO_ME_FLAC),
        ("system", meta.system, paths.AUDIO_REMOTE_FLAC),
    ):
        wav = session_dir / info.file
        flac = session_dir / flac_name
        if wav.suffix.lower() == ".wav" and wav.exists():
            wavio.repair_wav(wav, rate)
            n = wavio.wav_to_flac(wav, flac, level=flac_level, delete_wav=True)
            if n == 0:  # pista vacia: no hay FLAC; se conserva el WAV
                tracks[key] = replace(info, samples=0)
                continue
        elif flac.exists():
            n = wavio.wav_samples(flac)
            if n < 0:
                raise ValueError(f"{flac} is not a valid FLAC")
        else:
            raise FileNotFoundError(f"no audio for track {key!r} in {session_dir}")
        tracks[key] = replace(info, file=flac_name, samples=n)
    extra = dict(meta.extra)
    extra["state"] = STATE_FINALIZED
    extra["flac_level"] = flac_level
    new = replace(meta, mic=tracks["mic"], system=tracks["system"], extra=extra)
    write_capture_json(session_dir, new)
    return new
