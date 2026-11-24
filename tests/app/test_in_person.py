"""«Grabar reunión presencial» de punta a punta: bandeja/biblioteca -> vigilante -> captura."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from micnaga import gates, paths
from micnaga.capture import session as capture_session
from micnaga.config import Config
from micnaga.store.db import Database
from micnaga.types import CaptureMeta, TrackInfo
from micnaga.ui import tray
from micnaga.ui.bridge import LibraryApi
from micnaga.ui.contracts import UiCallbacks
from micnaga.watcher import MeetingInfo, WatcherState
from tests.app.fakes import Rig, app_snapshot, meet_snapshot

IN_PERSON = MeetingInfo(manual=True, in_person=True)


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


def test_in_person_flow_from_the_app_callback() -> None:
    rig = Rig()
    action = rig.app.callbacks.on_record_in_person
    assert action is not None  # lo pone la aplicacion
    assert action() == "started"
    assert rig.rec.calls[0][1] == (True, "Reunión presencial", None)
    assert rig.pill.visible and not rig.popup.visible  # sin «¿Estás en una reu?»
    rig.feed(None, 5)
    rig.feed(meet_snapshot(), 30)  # empieza un Meet: ni popup ni cambio de sesion
    rig.feed(app_snapshot(), 30)
    rig.feed(None, 60)  # nadie usa el micro: sigue grabando
    assert rig.ctl.recording and not rig.popup.visible
    assert rig.watcher is not None
    assert rig.watcher.state is WatcherState.RECORDING_CONFIRMED
    assert action() == "busy"  # ya se graba: no hace nada
    assert len(rig.recorders.made) == 1
    rig.app.callbacks.on_stop()  # la pastilla para
    rig.settle()
    assert len(rig.jobs()) == 1
    assert not rig.ctl.recording


def test_failed_capture_is_reported_as_failed_not_started(tmp_path: Path) -> None:
    rig = Rig()
    rig.recorders.fail_next = True
    assert rig.host is not None
    api = rig.host.library_api()
    assert api.record_in_person() == {"ok": False, "error": "toast.error.capture_failed.title"}
    assert not rig.ctl.recording


def test_quit_during_in_person_finalizes_and_queues() -> None:
    rig = Rig()
    action = rig.app.callbacks.on_record_in_person
    assert action is not None and action() == "started"
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
    assert sizes == {8}  # 8: «Actualizar a la versión X», siempre presente (oculta sin version)


def test_tray_dispatch_uses_the_callback() -> None:
    seen: list[str] = []
    t = tray.Tray(UiCallbacks())
    assert not t._in_person_on()
    t.dispatch("record_in_person")  # sin app detras: no hace nada (ni lanza)
    t = tray.Tray(UiCallbacks(on_record_in_person=lambda: seen.append("sala") or "started"))
    assert t._in_person_on()
    t.dispatch("record_in_person")
    assert seen == ["sala"]


def test_tray_entry_reaches_the_real_watcher() -> None:
    rig = Rig()
    assert rig.host is not None
    tray.Tray(rig.host._tray_callbacks()).dispatch("record_in_person")  # cableado de host.py
    assert rig.ctl.recording and rig.rec.calls[0][1][0] is True
    assert rig.rec.calls[0][1][1] == "Reunión presencial"


# ------------------------------------------------------------------ biblioteca


def library(tmp_path: Path, action: Callable[[], object] | None = None) -> LibraryApi:
    cfg = Config()
    return LibraryApi(
        db_factory=lambda: Database(tmp_path / "micnaga.db"),
        meetings_root=lambda: tmp_path,
        audio_url=lambda f, t: "",
        config=lambda: cfg,
        record_in_person=action,  # type: ignore[arg-type]
    )


def test_library_button_is_offered_only_with_an_app_behind(tmp_path: Path) -> None:
    api = library(tmp_path)
    assert api.bootstrap()["in_person"] is False
    assert api.record_in_person() == {"ok": False, "error": "library.err.not_found"}
    assert library(tmp_path, lambda: "started").bootstrap()["in_person"] is True


def test_library_button_reaches_the_app_through_host() -> None:
    rig = Rig()
    assert rig.host is not None
    api = rig.host.library_api()
    assert api.bootstrap()["in_person"] is True
    assert api.record_in_person() == {"ok": True, "started": True}
    assert rig.ctl.recording


@pytest.mark.parametrize(
    ("returned", "expected"),
    [
        ("started", {"ok": True, "started": True}),
        ("busy", {"ok": True, "started": False}),
        ("failed", {"ok": False, "error": "toast.error.capture_failed.title"}),
        (None, {"ok": False, "error": "toast.error.capture_failed.title"}),
    ],
)
def test_library_record_in_person_reports_the_real_result(
    tmp_path: Path, returned: object, expected: dict[str, object]
) -> None:
    assert library(tmp_path, lambda: returned).record_in_person() == expected


def test_library_record_in_person_survives_a_failing_action(tmp_path: Path) -> None:
    def boom() -> str:
        raise RuntimeError("roto")

    assert library(tmp_path, boom).record_in_person() == {
        "ok": False,
        "error": "library.err.op_failed",
    }


def test_library_list_flags_in_person_meetings_by_the_marker(tmp_path: Path) -> None:
    from micnaga.store.db import MeetingRecord

    with Database(tmp_path / "micnaga.db") as db:
        for name in ("2026-11-20_reunion-presencial", "2026-11-21_meet"):
            (tmp_path / name).mkdir()
            db.upsert_meeting(MeetingRecord(name, name, "2026-11-20T10:00:00", 60.0))
    capture_session.mark_in_person(tmp_path / "2026-11-20_reunion-presencial")
    rows = {r["folder"]: r["in_person"] for r in library(tmp_path).list_meetings()}
    assert rows == {"2026-11-20_reunion-presencial": True, "2026-11-21_meet": False}


def test_default_title_is_generic_so_the_secretaria_can_retitle() -> None:
    from micnaga.brain.secretary import is_generic_title
    from micnaga.integration.controller import IN_PERSON_TITLE

    assert is_generic_title(IN_PERSON_TITLE) and is_generic_title("reunion presencial")
    assert not is_generic_title("Reunión presencial con Acme")
