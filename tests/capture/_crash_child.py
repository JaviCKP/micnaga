"""Proceso hijo del test de cierre brusco: graba audio sintetico hasta que lo matan.

Uso: ``python _crash_child.py <carpeta_sesion> <fichero_progreso>``. Escribe en el fichero de
progreso (de forma atomica) los segundos grabados cada 20 ms; el padre lo lee justo antes de
``kill`` para saber hasta donde habia llegado la grabacion.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from micnaga.capture.recorder import Recorder
from micnaga.capture.synthetic import demo_backend
from micnaga.config import CaptureConfig


def main() -> None:
    session, progress = Path(sys.argv[1]), Path(sys.argv[2])
    rec = Recorder(CaptureConfig(), backend=demo_backend())  # chunk_flush_s = 2 (por defecto)
    rec.start(session)
    print("READY", flush=True)
    tmp = progress.with_name(progress.name + ".tmp")
    while True:
        try:
            tmp.write_text(repr(rec.elapsed_s()))
            os.replace(tmp, progress)
        except OSError:  # el padre lo esta leyendo en este instante (Windows): se reintenta
            pass
        time.sleep(0.02)


if __name__ == "__main__":
    main()
