"""Opciones comunes de las sesiones de onnxruntime de los motores.

**Sin espera activa** (``session.intra_op.allow_spinning = 0``). Por defecto los hilos de
onnxruntime giran en vacio entre operaciones esperando la siguiente; el worker y el proceso en
vivo corren con prioridad ``BELOW_NORMAL`` y, en cuanto el equipo esta ocupado, Windows
desaloja algunos de esos hilos y cada operacion paralela espera al mas lento. Medido con el
codificador de Parakeet (DECISIONS §23): con 16 procesos ocupando la CPU, 6 hilos tardaban
~100 s en un tramo que sin espera activa tarda ~10 s; con el equipo libre no empeora.
"""

from __future__ import annotations

from typing import Any


def session_options(threads: int) -> Any:
    """``onnxruntime.SessionOptions`` con ``threads`` hilos intra-op y sin espera activa."""
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.intra_op_num_threads = max(1, threads)
    so.inter_op_num_threads = 1
    so.log_severity_level = 3
    so.add_session_config_entry("session.intra_op.allow_spinning", "0")
    so.add_session_config_entry("session.inter_op.allow_spinning", "0")
    return so


__all__ = ["session_options"]
