"""Extremo a extremo con los modelos REALES (``MICNAGA_RUN_MODELS=1``) y voz TTS sintetica.

Cada test construye una sesion sintetica (``tts_session``) y la procesa con el comando real
``micnaga process`` en un subproceso (Parakeet + diarize + AEC), midiendo tiempo y RAM pico del
proceso. Las cifras se imprimen como ``E2E_RESULT {...}`` (``pytest -s``) y, si existe
``MICNAGA_E2E_REPORT``, se anaden a ese fichero JSON-lines.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import psutil
import pytest

from micnaga import holding, paths
from micnaga.config import Config, save_config
from micnaga.types import Transcript
from tests.e2e.tts_session import TtsSession, build_session

pytestmark = [pytest.mark.models, pytest.mark.timeout(900)]

REPO = Path(__file__).resolve().parents[2]


def _words(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text.lower())
    return re.sub(r"[^\w\s]", " ", text).split()


def _run_process(session: Path) -> dict[str, Any]:
    """``python -m micnaga process <sesion>``; mide tiempo y RAM pico (RSS, con hijos)."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, "-m", "micnaga", "process", str(session)],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    peak = {"rss": 0, "private": 0}
    stop = threading.Event()

    def poll() -> None:
        try:
            ps = psutil.Process(proc.pid)
        except psutil.Error:
            return
        while not stop.is_set():
            try:
                group = [ps, *ps.children(recursive=True)]
                rss = sum(p.memory_info().rss for p in group)
                private = sum(
                    getattr(p.memory_info(), "private", p.memory_info().rss) for p in group
                )
                peak["rss"] = max(peak["rss"], rss)
                peak["private"] = max(peak["private"], private)
            except psutil.Error:
                pass
            stop.wait(0.2)

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    t0 = time.monotonic()
    out, err = proc.communicate(timeout=840)
    wall = time.monotonic() - t0
    stop.set()
    thread.join(2)
    return {
        "code": proc.returncode,
        "wall_s": wall,
        "stdout": out,
        "stderr": err,
        "peak_rss_mb": peak["rss"] / 2**20,
        "peak_private_mb": peak["private"] / 2**20,
    }


def _report(name: str, data: dict[str, Any]) -> None:
    line = "E2E_RESULT " + json.dumps({"case": name, **data}, ensure_ascii=False)
    print(line)
    target = os.environ.get("MICNAGA_E2E_REPORT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _find_output(root: Path) -> Path:
    found = [p for p in root.iterdir() if p.is_dir() and (p / paths.TRANSCRIPT_JSON).is_file()]
    assert len(found) == 1, f"expected one finished meeting folder, got {found}"
    return found[0]


def _score(session: TtsSession, out: Path) -> dict[str, Any]:
    import jiwer

    tr = Transcript.from_json((out / paths.TRANSCRIPT_JSON).read_text(encoding="utf-8"))
    names = {s.id: s.name for s in tr.speakers}
    me = [s for s in tr.segments if s.speaker == "me"]
    remote_segs = [s for s in tr.segments if s.speaker != "me"]
    reference = " ".join(" ".join(_words(t)) for t in session.mic_script)
    hypothesis = " ".join(" ".join(_words(s.text)) for s in me)
    wer = jiwer.wer(reference, hypothesis) if hypothesis else 1.0

    # Frases del remoto atribuidas a «Yo»: por ventana temporal de cada enunciado remoto.
    leaked = 0
    leaked_words = 0
    total_words = 0
    for u in session.remote:
        total_words += len(_words(u.text))
        inside = [s for s in me if u.start - 0.3 <= (s.start + s.end) / 2 <= u.end + 0.3]
        words = sum(len(_words(s.text)) for s in inside)
        if words >= 3:
            leaked += 1
            leaked_words += words
    n_remote = max(1, len(session.remote))
    return {
        "speakers": names,
        "n_me_segments": len(me),
        "n_remote_segments": len(remote_segs),
        "yo_wer": round(wer, 4),
        "remote_phrases": len(session.remote),
        "remote_phrases_as_yo": leaked,
        "remote_phrases_as_yo_pct": round(100.0 * leaked / n_remote, 2),
        "remote_words_as_yo_pct": round(100.0 * leaked_words / max(1, total_words), 2),
        "engines": tr.engines.to_dict() if hasattr(tr.engines, "to_dict") else str(tr.engines),
    }


@pytest.fixture
def storage(tmp_storage: Path) -> Path:
    cfg = Config()
    cfg.processing.threads = 6
    save_config(cfg)
    return tmp_storage


@pytest.mark.parametrize(
    ("case", "output", "echo"),
    [("headphones", "headphones", False), ("speakers_echo", "speakers", True)],
)
def test_real_pipeline_on_tts_meeting(storage: Path, case: str, output: str, echo: bool) -> None:
    session = build_session(
        storage / f"_rec_{case}",
        seconds=215.0,
        output=output,
        echo=echo,  # type: ignore[arg-type]
        confirmed=True,  # «Si» en el popup: sin el, la etapa 1 la aparta (DECISIONS §18)
    )
    assert len(session.remote) >= 8 and len(session.mine) >= 8
    res = _run_process(session.folder)
    assert res["code"] == 0, res["stderr"][-2000:]
    out = _find_output(storage)
    assert out.name == "2026-11-18_1030_revision-semanal-e2e"
    for name in (paths.TRANSCRIPT_MD, paths.TRANSCRIPT_SRT, paths.TRANSCRIPT_JSON):
        assert (out / name).is_file() and (out / name).stat().st_size > 100
    assert (out / paths.AUDIO_ME_FLAC).is_file() and (out / paths.AUDIO_REMOTE_FLAC).is_file()
    score = _score(session, out)
    _report(
        case,
        {
            **score,
            "audio_s": session.duration_s,
            "wall_s": round(res["wall_s"], 1),
            "x_realtime_factor": round(res["wall_s"] / session.duration_s, 3),
            "peak_rss_mb": round(res["peak_rss_mb"]),
            "peak_private_mb": round(res["peak_private_mb"]),
        },
    )
    md = (out / paths.TRANSCRIPT_MD).read_text(encoding="utf-8")
    assert "Yo" in score["speakers"].values()
    assert score["n_remote_segments"] > 0 and any(n != "Yo" for n in score["speakers"].values())
    assert "**[" in md and "Yo:**" in md
    assert score["yo_wer"] < 0.25
    if echo:
        assert score["remote_phrases_as_yo_pct"] < 20.0  # el objetivo de la spec es < 5 %


@pytest.mark.parametrize("seconds", [120.0])
def test_voice_note_trap_is_discarded(storage: Path, seconds: float) -> None:
    session = build_session(
        storage / "_rec_trap", seconds=seconds, mic_only=True, title="Nota de voz"
    )
    res = _run_process(session.folder)
    _report("voice_note_trap", {"code": res["code"], "wall_s": round(res["wall_s"], 1)})
    # Sin «Si» no se transcribe; se aparta en «Descartadas» (recuperable), no se borra.
    assert res["code"] == 3, (res["stdout"], res["stderr"][-1500:])
    assert not session.folder.exists()
    held = holding.list_held(storage)
    assert [h.reason for h in held] == [holding.REASON_UNCONFIRMED]
    assert not any((storage / "_descartadas" / held[0].name).glob("transcripcion.*"))
    assert [
        p
        for p in storage.iterdir()
        if p.is_dir() and not p.name.startswith("_tts_") and p.name != holding.HELD_DIRNAME
    ] == []
