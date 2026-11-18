"""App + cola + worker real + tuberia de eventos + toast, con los modelos reales.

La app (con UI falsa) encola una sesion TTS confirmada; su supervisor lanza ``micnaga worker
--once`` de verdad (proceso aparte, prioridad baja); el worker procesa, avisa por la tuberia y
sale al vaciar la cola. La app recibe ``JobDone`` y muestra el toast «Lista».
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from micnaga import paths
from micnaga.config import Config, save_config
from micnaga.pipeline.worker import is_worker_running
from micnaga.store.db import JobState, open_database
from tests.app.fakes import Rig
from tests.e2e.tts_session import build_session

pytestmark = [pytest.mark.models, pytest.mark.timeout(900)]


def test_queue_worker_events_and_toast_end_to_end(tmp_storage: Path) -> None:
    cfg = Config()
    cfg.gates.min_duration_s = 20.0  # sesion corta de prueba (confirmada: solo cuenta la 1)
    save_config(cfg)
    session = build_session(tmp_storage / "_rec_queue", seconds=70.0, confirmed=True)

    rig = Rig(cfg, real_worker=True)
    rig.app.start()
    try:
        db = open_database(cfg)
        try:
            db.enqueue_job(session.folder)
        finally:
            db.close()
        rig.app.supervisor.kick()
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and not rig.toaster.toasts:
            time.sleep(0.5)
        assert rig.toaster.toasts, "no JobDone toast arrived"
        text = " ".join(map(str, rig.toaster.toasts[0].text_fields))
        assert "Revisión semanal e2e" in text
        # el worker lanzado por la app sale solo al vaciar la cola (proceso limpio por tanda)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and is_worker_running():
            time.sleep(0.5)
        assert not is_worker_running()
        out = tmp_storage / "2026-11-18_1030_revision-semanal-e2e"
        assert (out / paths.TRANSCRIPT_JSON).is_file()
        db = open_database(cfg)
        try:
            assert db.count_jobs()[JobState.DONE] == 1
            job = db.list_jobs()[0]
            assert Path(job.session_dir) == out  # ``set_session_dir`` siguio al renombrado
        finally:
            db.close()
        assert rig.app.supervisor.spawn_count == 1
    finally:
        rig.app.shutdown(wait_s=10)
