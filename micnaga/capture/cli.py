"""``micnaga record``: graba micro + sistema en una carpeta de sesion (F0).

::

    micnaga record [--out DIR] [--seconds N] [--confirm] [--title T] [--no-finalize]
    micnaga record --recover [--out DIR]

* ``--out DIR``: **raiz** bajo la que se crea la carpeta de la sesion
  (``AAAA-MM-DD_HHMM_titulo``); por defecto ``paths.meetings_root()``.
* ``--title T``: titulo de la reunion. Va a ``capture.json`` (``meet_title``), de donde lo toman
  el titulo de la transcripcion y el nombre final de la carpeta; sin el, la carpeta se llama
  ``..._grabacion`` y el procesado usa el titulo por defecto.
* Graba hasta Ctrl+C (o Ctrl+Pausa) o hasta ``--seconds``; imprime niveles cada 5 s.
* Al terminar convierte a FLAC (``finalize_session``) salvo ``--no-finalize``.
* ``--recover``: ejecuta la recuperacion de sesiones interrumpidas y sale.
* ``--synthetic``: audio generado en vez de dispositivos reales (demo y pruebas).
"""

from __future__ import annotations

import argparse
import logging
import math
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from types import FrameType

from micnaga import paths
from micnaga.capture.recorder import CaptureError, Recorder, RecorderEvent
from micnaga.capture.recovery import finalize_session, recover_sessions
from micnaga.capture.session import read_capture_json
from micnaga.capture.sync import estimate_drift
from micnaga.config import load_config
from micnaga.logging_setup import setup_logging

PRINT_EVERY_S = 5.0
DEFAULT_FOLDER = "grabacion"
POLL_S = 0.25


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="micnaga record",
        description="Graba el micro y el audio del sistema en dos pistas sincronizadas.",
    )
    p.add_argument("--out", type=Path, help="carpeta raiz de las sesiones (por defecto la config)")
    p.add_argument("--seconds", type=float, help="parar tras N segundos (por defecto: Ctrl+C)")
    p.add_argument("--confirm", action="store_true", help="marcar la sesion como confirmada")
    p.add_argument("--title", help="titulo de la reunion (transcripcion y nombre de la carpeta)")
    p.add_argument("--no-finalize", action="store_true", help="dejar los WAV (sin pasar a FLAC)")
    p.add_argument(
        "--recover", action="store_true", help="recuperar sesiones interrumpidas y salir"
    )
    p.add_argument("--synthetic", action="store_true", help="audio sintetico (sin dispositivos)")
    return p


def level_bar(level: float, width: int = 10) -> str:
    """Barra ASCII en escala dBFS (-60 dB .. 0 dB)."""
    if level <= 1e-6:
        return "." * width
    db = max(20 * math.log10(level), -60.0)
    filled = round((db + 60.0) / 60.0 * width)
    return "#" * filled + "." * (width - filled)


def _print_event(ev: RecorderEvent) -> None:
    if ev.kind != "state":
        print(f"[{ev.t:7.1f}s] {ev.kind}: {ev.message}", file=sys.stderr, flush=True)


class _Interrupted(Exception):
    pass


def _raise_interrupt(signum: int, frame: FrameType | None) -> None:
    raise _Interrupted


def run(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    setup_logging("record", level=logging.INFO)
    root = args.out if args.out is not None else paths.meetings_root()

    if args.recover:
        recovered = recover_sessions(root)
        if not recovered:
            print("nothing to recover")
        for d in recovered:
            print(f"recovered: {d}")
        return 0

    config = load_config()
    backend = None
    if args.synthetic:
        from micnaga.capture.synthetic import demo_backend

        backend = demo_backend()
    recorder = Recorder(config.capture, backend=backend, on_event=_print_event)
    title = (args.title or "").strip() or None
    session = paths.unique_folder(
        paths.ensure_dir(root), paths.meeting_folder_name(datetime.now(), title or DEFAULT_FOLDER)
    )
    previous = signal.signal(signal.SIGINT, _raise_interrupt)
    sigbreak = getattr(signal, "SIGBREAK", None)
    previous_break = signal.signal(sigbreak, _raise_interrupt) if sigbreak is not None else None
    try:
        try:
            recorder.start(session, confirmed=args.confirm, meet_title=title)
        except CaptureError as exc:
            print(f"cannot record: {exc}", file=sys.stderr)
            return 1
        print(f"recording to {session}  (Ctrl+C to stop)", flush=True)
        next_print = PRINT_EVERY_S
        try:
            while args.seconds is None or recorder.elapsed_s() < args.seconds:
                time.sleep(POLL_S)
                if recorder.elapsed_s() >= next_print:
                    next_print += PRINT_EVERY_S
                    mic, sysl = recorder.levels()
                    secs = int(recorder.elapsed_s())
                    print(
                        f"[{secs // 60:02d}:{secs % 60:02d}]"
                        f" mic {level_bar(mic)} | sys {level_bar(sysl)}",
                        flush=True,
                    )
        except (_Interrupted, KeyboardInterrupt):
            print("stopping...", flush=True)
        finally:
            session = recorder.stop()
    finally:
        signal.signal(signal.SIGINT, previous)
        if sigbreak is not None and previous_break is not None:
            signal.signal(sigbreak, previous_break)

    if not args.no_finalize:
        finalize_session(session, flac_level=config.capture.flac_level)
    _print_summary(session)
    return 0


def _print_summary(session: Path) -> None:
    meta = read_capture_json(session)
    rate = meta.mic.sample_rate
    print(f"session:  {session}")
    print(f"duration: {max(meta.mic.samples, meta.system.samples) / rate:.1f} s")
    print(f"output:   {meta.output_kind}")
    for name, track in (("mic", meta.mic), ("system", meta.system)):
        d = estimate_drift(track.sync, rate)
        print(
            f"{name:6s}:  {track.file}  device={track.device or '-'}  "
            f"drift={d.ppm:+.0f} ppm over {d.drift_s * 1000:+.0f} ms  marks={d.n_marks}"
        )
    if meta.pauses:
        print(f"pauses:   {len(meta.pauses)}")
    if meta.markers:
        print(f"markers:  {len(meta.markers)}")
    if meta.device_changes:
        print(f"device changes: {len(meta.device_changes)}")
