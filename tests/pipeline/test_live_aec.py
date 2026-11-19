"""Cancelacion de eco por trozos (procesado en vivo) frente a la pista entera.

El estado de Speex no se puede guardar entre trozos: cada trozo de 60 s se cancela con un Speex
nuevo y ``AEC_PREROLL_S`` de audio previo para que el filtro converja. Con fuga de altavoz, el
eco que queda debe ser equivalente al de cancelar la pista de una vez.
"""

from __future__ import annotations

import numpy as np
import pytest

from micnaga.pipeline.live import AEC_PREROLL_S, SLICE_S
from tests.pipeline.test_echo_check import _room, _talk

SR = 16000


def _speex() -> object:
    try:
        from micnaga.pipeline.aec.speex import SpeexEchoCanceller

        return SpeexEchoCanceller()
    except Exception as exc:  # pyaec sin instalar
        pytest.skip(f"speex not available: {exc}")


def _echo_only_residual_db(out: np.ndarray, echo: np.ndarray, near_gate: np.ndarray) -> float:
    """ERLE en las zonas donde solo habla el sistema: eco que entra / eco que queda (dB)."""
    mask = (near_gate == 0) & (np.abs(echo) > 0)
    return 10 * float(np.log10(np.sum(echo[mask] ** 2) / max(np.sum(out[mask] ** 2), 1e-12)))


def test_chunked_aec_with_preroll_matches_whole_track_aec() -> None:
    speex = _speex()
    total = 130.0
    remote_spans = [(a, a + 6) for a in np.arange(1, total - 7, 13.0)]
    me_spans = [(a + 7, a + 11) for a in np.arange(1, total - 12, 13.0)]
    n = int(total * SR)
    system = np.zeros(n, dtype=np.float32)
    for i, (a, b) in enumerate(remote_spans):
        piece = _talk([(0, b - a)], seed=20 + i)[: int((b - a) * SR)]
        system[int(a * SR) : int(a * SR) + len(piece)] = piece
    near = np.zeros(n, dtype=np.float32)
    for i, (a, b) in enumerate(me_spans):
        piece = _talk([(0, b - a)], seed=60 + i)[: int((b - a) * SR)]
        near[int(a * SR) : int(a * SR) + len(piece)] = piece
    echo = _room(system, 0.12, 0.3)
    mic = (near + echo).astype(np.float32)

    whole = np.asarray(speex.process(mic, system, SR), dtype=np.float32)  # type: ignore[attr-defined]
    slice_len, pre = int(SLICE_S * SR), int(AEC_PREROLL_S * SR)
    chunked = np.zeros(n, dtype=np.float32)
    for start in range(0, n, slice_len):
        end = min(n, start + slice_len)
        lo = max(0, start - pre)
        out = speex.process(mic[lo:end], system[lo:end], SR)  # type: ignore[attr-defined]
        chunked[start:end] = np.asarray(out, dtype=np.float32)[start - lo :]

    gate = (np.abs(near) > 0).astype(np.int8)
    erle_whole = _echo_only_residual_db(whole, echo, gate)
    erle_chunked = _echo_only_residual_db(chunked, echo, gate)
    assert erle_whole > 6.0  # el cancelador hace algo
    assert abs(erle_chunked - erle_whole) <= 3.0, (erle_whole, erle_chunked)
    # y la voz propia sigue igual de presente en las dos
    me = gate == 1
    ratio = np.sum(chunked[me] ** 2) / np.sum(whole[me] ** 2)
    assert 0.7 <= ratio <= 1.4
