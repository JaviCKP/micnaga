"""«Grabar reunión presencial» de punta a punta: bandeja/biblioteca -> vigilante -> captura."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from micnaga import gates, paths
from micnaga.capture import session as capture_session
from micnaga.config import Config
from micnaga.store.db import Database
from micnaga.types import CaptureMeta, TrackInfo
from micnaga.ui import tray
from micnaga.ui.bridge import LibraryApi
from micnaga.ui.contracts import ACTION_RECORD_IN_PERSON, UiCallbacks, app_action, set_app_action
from micnaga.watcher import MeetingInfo, WatcherState
from tests.app.fakes import Rig, app_snapshot, meet_snapshot

IN_PERSON = MeetingInfo(manual=True, in_person=True)


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """El registro de acciones es global: cada test empieza y acaba sin nada registrado."""
    set_app_action(ACTION_RECORD_IN_PERSON, None)
    yield
    set_app_action(ACTION_RECORD_IN_PERSON, None)


def write_capture_json(folder: Path) -> None:
    """``capture.json`` como lo deja el grabador real al parar (el falso no lo escribe)."""
    meta = CaptureMeta(
        started_at="2026-11-23T10:00:00+01:00",
        mic=TrackInfo(paths.AUDIO_ME_WAV, "mic"),
        system=TrackInfo(paths.AUDIO_REMOTE_WAV, "sys"),
        confirmed=True,
        extra={"state": capture_session.STATE_STOPPED},
    )
    assert capture_session.write_capture_json(folder, meta)


# ------------------------------------------------------------------ controlador


def test_start_uses_the_default_title_and_marks_the_folder() -> None:
    rig = Rig()
    rig.ctl.start_recording(provisional=False, meeting=IN_PERSON)
    assert rig.rec.calls[0][1] == (True, "Reunión presencial", None)  # confirmada desde ya
    folder = rig.rec.dir
    assert folder is not None and "reunion-presencial" in folder.name
    assert (folder / capture_session.IN_PERSON_MARKER).is_file()  # antes de parar


def test_a_normal_manual_recording_is_not_in_person() -> None:
    rig = Rig()
    rig.ctl.start_recording(provisional=False, meeting=MeetingInfo(manual=True))
    folder = rig.rec.dir
    assert folder is not None and not (folder / capture_session.IN_PERSON_MARKER).exists()


def test_stop_writes_in_person_to_capture_json_and_queues() -> None:
    rig = Rig()
    rig.ctl.start_recording(provisional=False, meeting=IN_PERSON)
    folder = rig.rec.dir
    assert folder is not None
    write_capture_json(folder)
    rig.ctl.stop_and_finalize(confirmed=True, meeting=IN_PERSON, trim=False)
    rig.settle()
    meta = capture_session.read_capture_json(folder)
    assert meta.extra[gates.IN_PERSON_KEY] is True
    assert len(rig.jobs()) == 1


# ------------------------------------------------------------------ flujo con el vigilante real


def test_in_person_flow_from_the_registered_action() -> None:
    rig = Rig()
    action = app_action(ACTION_RECORD_IN_PERSON)
    assert action is not None  # lo registra la aplicacion
    assert action() is True
    assert rig.rec.calls[0][1] == (True, "Reunión presencial", None)
    assert rig.pill.visible and not rig.popup.visible  # sin «¿Estás en una reu?»
    rig.feed(None, 5)
    rig.feed(meet_snapshot(), 30)  # empieza un Meet: ni popup ni cambio de sesion
    rig.feed(app_snapshot(), 30)
    rig.feed(None, 60)  # nadie usa el micro: sigue grabando
    assert rig.ctl.recording and not rig.popup.visible
    assert rig.watcher is not None
    assert rig.watcher.state is WatcherState.RECORDING_CONFIRMED
    assert action() is False  # ya se graba: no hace nada
    assert len(rig.recorders.made) == 1
    rig.app.callbacks.on_stop()  # la pastilla para
    rig.settle()
    assert len(rig.jobs()) == 1
    assert not rig.ctl.recording


def test_shutdown_unregisters_the_action() -> None:
    rig = Rig()
    assert app_action(ACTION_RECORD_IN_PERSON) is not None
    rig.app.shutdown(reason="test")
    assert app_action(ACTION_RECORD_IN_PERSON) is None


def test_quit_during_in_person_finalizes_and_queues() -> None:
    rig = Rig()
    action = app_action(ACTION_RECORD_IN_PERSON)
    assert action is not None and action() is True
    folder = rig.rec.dir
    assert folder is not None
    write_capture_json(folder)
    rig.app.callbacks.on_quit()
    assert rig.app.stopped
    assert len(rig.jobs()) == 1
    assert capture_session.read_capture_json(folder).extra[gates.IN_PERSON_KEY] is True


# ------------------------------------------------------------------ bandeja


def test_tray_entry_sits_after_record_now_and_follows_the_state() -> None:
    spec = tray.menu_spec("watching", watching_paused=False)
    assert [e.key for e in spec[:2]] == ["record_now", "record_in_person"]
    assert spec[1].label == "Grabar reunión presencial" and spec[1].enabled
    assert not tray.menu_spec("recording", watching_paused=False)[1].enabled
    assert not tray.menu_spec("paused", watching_paused=False)[1].enabled
    assert not tray.menu_spec("watching", watching_paused=False, in_person=False)[1].enabled
    # el menu se arma por posicion: mismo numero de entradas en todos los estados
    sizes = {
        len(tray.menu_spec(s, watching_paused=p, in_person=i))  # type: ignore[arg-type]
        for s in ("watching", "recording", "paused", "processing")
        for p in (False, True)
        for i in (False, True)
    }
    assert sizes == {7}


def test_tray_dispatch_uses_the_registered_action() -> None:
    seen: list[str] = []
    t = tray.Tray(UiCallbacks())
    assert not t._in_person_on()
    t.dispatch("record_in_person")  # sin app detras: no hace nada (ni lanza)
    set_app_action(ACTION_RECORD_IN_PERSON, lambda: seen.append("sala") or True)
    assert t._in_person_on()
    t.dispatch("record_in_person")
    assert seen == ["sala"]


def test_tray_entry_reaches_the_real_watcher() -> None:
    rig = Rig()
    tray.Tray(UiCallbacks()).dispatch("record_in_person")
    assert rig.ctl.recording and rig.rec.calls[0][1][0] is True
    assert rig.rec.calls[0][1][1] == "Reunión presencial"


# ------------------------------------------------------------------ biblioteca


def library(tmp_path: Path) -> LibraryApi:
    cfg = Config()
    return LibraryApi(
        db_factory=lambda: Database(tmp_path / "micnaga.db"),
        meetings_root=lambda: tmp_path,
        audio_url=lambda f, t: "",
        config=lambda: cfg,
    )


def test_library_button_is_offered_only_with_an_app_behind(tmp_path: Path) -> None:
    api = library(tmp_path)
    assert api.bootstrap()["in_person"] is False
    assert api.record_in_person() == {"ok": False, "error": "library.err.not_found"}
    set_app_action(ACTION_RECORD_IN_PERSON, lambda: True)
    assert api.bootstrap()["in_person"] is True


@pytest.mark.parametrize(("returned", "started"), [(True, True), (False, False), (None, True)])
def test_library_record_in_person_reports_whether_it_started(
    tmp_path: Path, returned: object, started: bool
) -> None:
    set_app_action(ACTION_RECORD_IN_PERSON, lambda: returned)
    assert library(tmp_path).record_in_person() == {"ok": True, "started": started}


def test_library_record_in_person_survives_a_failing_action(tmp_path: Path) -> None:
    def boom() -> bool:
        raise RuntimeError("roto")

    set_app_action(ACTION_RECORD_IN_PERSON, boom)
    assert library(tmp_path).record_in_person() == {"ok": False, "error": "library.err.op_failed"}


def test_default_title_is_generic_so_the_secretaria_can_retitle() -> None:
    from micnaga.brain.secretary import is_generic_title
    from micnaga.integration.controller import IN_PERSON_TITLE

    assert is_generic_title(IN_PERSON_TITLE) and is_generic_title("reunion presencial")
    assert not is_generic_title("Reunión presencial con Acme")
