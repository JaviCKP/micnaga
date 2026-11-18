"""Flujos completos: vigilante real (reloj falso) + controlador + ``UiHost`` falso + cola SQLite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from micnaga.config import Config
from micnaga.watcher import WatcherState
from tests.app.fakes import Rig, app_snapshot, meet_snapshot
from tests.watcher.helpers import OFF


def _host(rig: Rig) -> Any:
    return rig.app.ui


def _to_popup(rig: Rig, snap: Any = None) -> None:
    rig.feed(snap or meet_snapshot(), 7)
    assert rig.watcher is not None and rig.watcher.state is WatcherState.PROMPTING


def _answer(rig: Rig, decision: str) -> None:
    """El usuario pulsa en el popup: llega por el callback real del host."""
    _host(rig).handle_popup_decide(decision)


# ------------------------------------------------------------------------------ ruta Meet


def test_meet_path_popup_record_stop_enqueues_confirmed_job() -> None:
    rig = Rig()
    _to_popup(rig)
    assert rig.rec.calls[0][0] == "start" and rig.rec.calls[0][1][0] is False  # provisional
    assert rig.popup.visible
    assert not rig.pill.visible

    _answer(rig, "record")  # «Grabar»
    assert ("set_confirmed", True) in rig.rec.calls
    # la pastilla espera al vuelo del popup (no aparece debajo de la tarjeta)
    assert not rig.pill.visible
    _host(rig).handle_popup_morph(False)  # el JS termina el check de 300 ms y encoge la tarjeta
    assert rig.pill.visible
    assert sum(1 for js in rig.js(rig.pill) if js.startswith("micnaga.pill.show(")) == 1
    payload = next(js for js in rig.js(rig.pill) if js.startswith("micnaga.pill.show("))
    assert json.loads(payload[len("micnaga.pill.show(") : -1])["elapsed"] == rig.rec.elapsed
    assert rig.watcher.state is WatcherState.RECORDING_CONFIRMED  # type: ignore[union-attr]

    rig.feed(OFF, 16)  # cuelga la reunion (15 s sin senal)
    rig.settle()
    assert not rig.pill.visible
    assert "stop" in rig.rec.names
    jobs = rig.jobs()
    assert len(jobs) == 1
    assert rig.recorders.last.confirmed is True


def test_user_stop_from_pill_button() -> None:
    rig = Rig()
    _to_popup(rig)
    _answer(rig, "record")
    _host(rig).handle_popup_morph(False)
    rig.app.callbacks.on_stop()  # boton parar de la pastilla -> watcher.user_stop
    rig.settle()
    assert "stop" in rig.rec.names and len(rig.jobs()) == 1
    assert not rig.pill.visible


def test_decline_discards_and_ignores_the_meeting() -> None:
    rig = Rig()
    _to_popup(rig)
    folder = rig.rec.dir
    _answer(rig, "dismiss")  # «Ahora no»
    rig.settle()
    assert folder is not None and not folder.exists()
    assert rig.jobs() == []
    rig.feed(meet_snapshot(), 20)  # sigue en la misma reunion: no vuelve a preguntar
    assert len(rig.recorders.made) == 1


def test_end_before_answer_discards_everything_on_meet_path() -> None:
    rig = Rig()
    _to_popup(rig)
    folder = rig.rec.dir
    rig.feed(OFF, 16)
    rig.settle()
    assert any("micnaga.popup.end()" in js for js in rig.js(rig.popup))
    assert folder is not None and not folder.exists()
    assert rig.jobs() == []
    assert not rig.pill.visible


def test_timeout_keeps_recording_provisional_then_enqueues_unconfirmed() -> None:
    rig = Rig()
    _to_popup(rig)
    rig.feed(meet_snapshot(), 15)  # el vigilante agota su cuenta atras
    assert rig.watcher.state is WatcherState.RECORDING_PROVISIONAL  # type: ignore[union-attr]
    assert any("micnaga.popup.expire" in js for js in rig.js(rig.popup))
    _host(rig).handle_popup_morph(True)
    assert rig.pill.visible
    assert any('"provisional": true' in js for js in rig.js(rig.pill))
    rig.feed(OFF, 16)
    rig.settle()
    jobs = rig.jobs()
    assert len(jobs) == 1
    assert "set_confirmed" not in rig.rec.names  # las compuertas deciden


def test_ui_timeout_decision_is_ignored_the_watcher_owns_the_countdown() -> None:
    rig = Rig()
    _to_popup(rig)
    _answer(rig, "timeout")
    assert rig.watcher.state is WatcherState.PROMPTING  # type: ignore[union-attr]
    assert rig.rec.dir is not None and rig.rec.dir.exists()


def test_popup_state_follows_the_watcher_countdown_and_freezes() -> None:
    rig = Rig()
    _to_popup(rig)
    host = _host(rig)
    first = host.popup_state()
    assert first["total_s"] == rig.cfg.detection.popup_timeout_s
    assert not first["paused"]
    rig.feed(meet_snapshot(), 3)
    later = host.popup_state()
    assert later["remaining_s"] == pytest.approx(first["remaining_s"] - 3, abs=0.01)
    host.handle_popup_hover(True)  # raton encima -> vigilante congela
    assert host.popup_state()["paused"] is True
    rig.feed(meet_snapshot(), 3)
    assert host.popup_state()["remaining_s"] == pytest.approx(later["remaining_s"], abs=0.01)
    host.handle_popup_hover(False)
    assert host.popup_state()["paused"] is False
    rig.feed(OFF, 2)  # senal caida: tambien congela
    frozen = host.popup_state()
    assert frozen["paused"] is True


def test_popup_state_is_idle_without_a_popup() -> None:
    rig = Rig()
    state = _host(rig).popup_state()
    assert state["paused"] is False and state["remaining_s"] == state["total_s"]


def test_hover_reaches_the_watcher() -> None:
    rig = Rig()
    _to_popup(rig)
    before = rig.watcher.popup_remaining_s  # type: ignore[union-attr]
    _host(rig).handle_popup_hover(True)
    rig.feed(meet_snapshot(), 5)
    assert rig.watcher.popup_remaining_s == before  # type: ignore[union-attr]


def test_confirm_from_tray_while_prompting() -> None:
    rig = Rig()
    _to_popup(rig)
    rig.app.callbacks.on_record_now()
    assert ("set_confirmed", True) in rig.rec.calls
    assert any("micnaga.popup.accept()" in js for js in rig.js(rig.popup))
    _host(rig).handle_popup_morph(False)
    assert rig.pill.visible


def test_pill_is_flushed_even_if_the_popup_never_morphs() -> None:
    """Red de seguridad: si el JS no contesta, la pastilla sale igualmente (no hay grabacion
    silenciosa)."""
    import micnaga.ui.host as host_mod

    rig = Rig()
    _to_popup(rig)
    _answer(rig, "record")
    assert not rig.pill.visible
    _host(rig)._flush_pending_pill()
    assert rig.pill.visible
    assert host_mod.PILL_FLUSH_S > 0


# ------------------------------------------------------------------------------ ruta generica


def test_generic_path_pill_first_then_popup_then_decline_discards() -> None:
    rig = Rig()
    rig.feed(app_snapshot(), 21)  # 20 s de uso continuo del micro
    assert rig.watcher.state is WatcherState.RECORDING_PRE_POPUP  # type: ignore[union-attr]
    assert rig.pill.visible and not rig.popup.visible
    assert rig.rec.calls[0][1][0] is False and rig.rec.calls[0][1][1] == "Teams"
    folder = rig.rec.dir
    rig.feed(app_snapshot(), 21)  # popup 20 s despues, con la pastilla ya visible
    assert rig.popup.visible and rig.pill.visible
    assert (
        "monitor_info",
        (4321,),
    ) in rig.w32.calls  # sin hint de monitor: ventana en primer plano
    _answer(rig, "dismiss")
    rig.settle()
    assert folder is not None and not folder.exists()
    assert rig.jobs() == []
    assert not rig.pill.visible


def test_generic_accept_does_not_double_the_pill() -> None:
    rig = Rig()
    rig.feed(app_snapshot(), 21)
    rig.feed(app_snapshot(), 21)
    shows_before = sum(1 for js in rig.js(rig.pill) if js.startswith("micnaga.pill.show("))
    assert shows_before == 1
    _answer(rig, "record")
    host = _host(rig)
    host.handle_popup_morph(
        False
    )  # el JS encoge la tarjeta: no hay vuelo a una pastilla que ya esta
    assert sum(1 for js in rig.js(rig.pill) if js.startswith("micnaga.pill.show(")) == 1
    assert any("setProvisional(false)" in js for js in rig.js(rig.pill))
    assert rig.pill.visible
    assert not any(k == "place" and a != rig.pill.current_rect for k, a in rig.pill.log[-1:])


def test_generic_timeout_leaves_the_existing_pill() -> None:
    rig = Rig()
    rig.feed(app_snapshot(), 21)
    rig.feed(app_snapshot(), 21)
    rig.feed(app_snapshot(), 15)  # el popup caduca
    assert rig.watcher.state is WatcherState.RECORDING_PROVISIONAL  # type: ignore[union-attr]
    assert rig.pill.visible
    rig.feed(OFF, 16)
    rig.settle()
    assert len(rig.jobs()) == 1  # provisional: las compuertas deciden


# ------------------------------------------------------------------ pausa privada y marcadores


def test_private_pause_hotkey_pauses_recorder_and_pill() -> None:
    rig = Rig()
    rig.app.start()
    try:
        _to_popup(rig)
        _answer(rig, "record")
        _host(rig).handle_popup_morph(False)
        assert rig.hotkeys is not None and rig.hotkeys.started
        rig.hotkeys.private_pause()  # Ctrl+Alt+P
        assert "pause" in rig.rec.names
        assert any("setPaused(true)" in js for js in rig.js(rig.pill))
        assert rig.watcher.state is WatcherState.PAUSED  # type: ignore[union-attr]
        rig.hotkeys.private_pause()
        assert "resume" in rig.rec.names
        assert rig.watcher.state is WatcherState.RECORDING_CONFIRMED  # type: ignore[union-attr]
    finally:
        rig.app.shutdown(wait_s=10)


def test_pill_pause_button_goes_through_the_watcher() -> None:
    rig = Rig()
    _to_popup(rig)
    _answer(rig, "record")
    _host(rig).handle_popup_morph(False)
    _host(rig).handle_pill_toggle_pause()
    assert "pause" in rig.rec.names
    assert rig.watcher.state is WatcherState.PAUSED  # type: ignore[union-attr]


def test_marker_hotkey_and_pill_button_add_markers() -> None:
    rig = Rig()
    rig.app.start()
    try:
        _to_popup(rig)
        assert rig.hotkeys is not None
        rig.hotkeys.marker()  # Ctrl+Alt+M
        _host(rig).handle_pill_marker()  # boton de la pastilla
        assert rig.rec.markers == ["", ""]
    finally:
        rig.app.shutdown(wait_s=10)


# ------------------------------------------------------------------------------ bandeja


def test_tray_actions_reach_the_watcher() -> None:
    rig = Rig()
    cb = rig.app.callbacks
    cb.on_pause_watching(60)
    assert rig.watcher.state is WatcherState.SUSPENDED  # type: ignore[union-attr]
    assert rig.watcher.diagnostics()["phase"] == "suspended"  # type: ignore[union-attr]
    cb.on_resume_watching()
    assert rig.watcher.state is WatcherState.IDLE  # type: ignore[union-attr]
    cb.on_record_now()  # «Grabar ahora»: sesion manual confirmada
    assert rig.rec.calls[0][1][0] is True
    assert rig.pill.visible
    cb.on_stop()
    rig.settle()
    assert len(rig.jobs()) == 1


def test_quit_while_recording_finalizes_and_does_not_discard() -> None:
    rig = Rig()
    _to_popup(rig)  # grabando provisional con popup abierto
    rig.app.callbacks.on_quit()
    assert rig.app.stopped
    assert rig.rec.dir is not None and rig.rec.dir.exists()
    assert len(rig.jobs()) == 1


def test_config_discard_on_timeout_flows() -> None:
    cfg = Config()
    cfg.detection.on_timeout = "discard"
    rig = Rig(cfg)
    _to_popup(rig)
    folder = rig.rec.dir
    rig.feed(meet_snapshot(), 15)
    rig.settle()
    assert folder is not None and not folder.exists()
    assert rig.jobs() == []


def test_session_folder_lives_under_the_meetings_root(tmp_storage: Path) -> None:
    rig = Rig()
    _to_popup(rig)
    assert rig.rec.dir is not None and rig.rec.dir.parent == tmp_storage
