"""Procesado en vivo con modelos reales: igual que por lotes (``MICNAGA_RUN_MODELS=1``).

Una reunion TTS de 215 s (voz sintetica de SAPI, no una grabacion real) se procesa dos veces:
por lotes y «en vivo» (trozos de 60 s calculados como si se estuviera grabando y la cola al
colgar). Las dos salidas deben coincidir: sin palabras perdidas ni repetidas en las fronteras de
los trozos y con una diferencia de WER dentro del ruido.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from micnaga import paths
from micnaga.capture.session import STATE_RECORDING, read_capture_json, write_capture_json
from micnaga.config import Config, save_config
from micnaga.pipeline import live
from micnaga.pipeline.runner import PipelineRunner
from micnaga.pipeline.stages.base import default_deps
from micnaga.types import SyncMark, Transcript
from tests.e2e.tts_session import build_session

pytestmark = [pytest.mark.models, pytest.mark.timeout(1200)]

SR = 16000


def _norm(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _set_state(session: Path, state: str, *, marks: bool = False) -> None:
    meta = read_capture_json(session)
    if marks:
        n = meta.mic.samples
        sync = [SyncMark(k, k / SR) for k in range(0, n + 1, 10 * SR)]
        meta = replace(
            meta, mic=replace(meta.mic, sync=sync), system=replace(meta.system, sync=sync)
        )
    write_capture_json(session, replace(meta, extra={**meta.extra, "state": state}))


def _run(session: Path, cfg: Config) -> Transcript:
    result = PipelineRunner(cfg, rename=False, keep_work=True).run(session)
    assert result.status == "done", result.reason
    assert result.transcript is not None
    return result.transcript


def test_live_and_batch_transcripts_match_on_a_tts_meeting(tmp_storage: Path) -> None:
    import jiwer

    cfg = Config()
    cfg.processing.threads = 6
    cfg.processing.diarization = "onnx"
    save_config(cfg)
    session = build_session(tmp_storage / "_rec_live", seconds=215.0, confirmed=True).folder
    batch = tmp_storage / "_rec_batch"
    shutil.copytree(session, batch)

    # mientras se graba: los trozos completos (0-60, 60-120, 120-180 s) y sus bloques estables
    _set_state(session, STATE_RECORDING, marks=True)
    engines = live.engines_from_deps(default_deps())
    assert engines is not None
    proc = live.LiveProcessor(session, cfg, engines)
    proc.ensure_manifest()
    assert proc.step()
    proc.close()
    assert len(proc.cached_slices()) == 3
    _set_state(session, "finalized")

    t_live = _run(session, cfg)
    t_batch = _run(batch, cfg)
    info = proc.stats
    assert info.blocks_done > 0

    for speaker_is_me in (True, False):

        def text(t: Transcript, me: bool = speaker_is_me) -> str:
            return " ".join(
                " ".join(_norm(s.text)) for s in t.segments if (s.speaker == "me") == me
            )

        a, b = text(t_batch), text(t_live)
        assert a and b
        assert jiwer.wer(a, b) <= 0.03, (speaker_is_me, jiwer.wer(a, b))
    # ninguna palabra repetida en una frontera (mismo texto y mismo instante)
    seen: set[tuple[str, int]] = set()
    for seg in t_live.segments:
        for w in seg.words:
            key = (w.w.lower(), round(w.s * 4))
            assert key not in seen, key
            seen.add(key)
    n_live = sum(len(s.words) for s in t_live.segments)
    n_batch = sum(len(s.words) for s in t_batch.segments)
    assert abs(n_live - n_batch) <= max(2, n_batch // 50)
    for name in (paths.TRANSCRIPT_JSON, paths.TRANSCRIPT_MD, paths.TRANSCRIPT_SRT):
        assert (session / name).is_file()
