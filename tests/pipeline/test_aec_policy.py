"""Regla de cancelacion de eco (``aec.policy``): modos, auriculares, medida por tramos y la
misma decision en el lote (etapa 3) y en el vivo (DECISIONS §35)."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from micnaga import paths
from micnaga.bench.fakes import fake_live_deps, load_script
from micnaga.bench.synthetic import SyntheticSpec
from micnaga.config import Config, ConfigError, config_from_dict, load_config
from micnaga.pipeline.aec import create_echo_canceller, policy
from micnaga.types import CaptureMeta, DeviceChange, TrackInfo
from tests.pipeline.helpers import make_session, runner_for
from tests.pipeline.test_echo_check import _room, _talk

SR = 16000
pytestmark = pytest.mark.timeout(300)  # varias medidas de eco por test (segundos con CPU ocupada)


def _meta(
    kind: str = "unknown", changes: list[DeviceChange] | None = None, **extra: Any
) -> CaptureMeta:
    track = TrackInfo("a.wav", "dev")
    return CaptureMeta(
        "2026-11-23T10:00:00+01:00",
        track,
        track,
        output_kind=kind,  # type: ignore[arg-type]
        device_changes=changes or [],
        extra=dict(extra),
    )


# ----------------------------------------------------------------------------- modos y config


@pytest.mark.parametrize(
    ("value", "mode"),
    [
        ("auto", "auto"),
        ("always", "always"),
        ("off", "off"),
        ("webrtc", "always"),
        ("speex", "always"),
    ],
)
def test_mode_of_every_config_value(value: str, mode: str) -> None:
    assert policy.aec_mode(value) == mode


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("webrtc", "always"),
        ("speex", "always"),
        ("auto", "auto"),
        ("off", "off"),
        ("always", "always"),
    ],
)
def test_old_config_values_load_as_the_new_modes(tmp_path: Path, old: str, new: str) -> None:
    file = tmp_path / "config.toml"
    file.write_text(f'[processing]\naec = "{old}"\n', encoding="utf-8")
    assert load_config(file).processing.aec == new


def test_unknown_aec_value_is_rejected() -> None:
    with pytest.raises(ConfigError):
        config_from_dict({"processing": {"aec": "maybe"}})


def test_settings_offer_the_three_modes() -> None:
    from micnaga.ui.settings_schema import FIELDS

    field = FIELDS["processing.aec"]
    assert tuple(field.choices) == ("auto", "always", "off")
    assert not field.advanced


def test_always_uses_the_first_available_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    class Webrtcish:
        name = "webrtc"

        def process(self, mic: Any, ref: Any, sr: int) -> Any:
            return mic

    mod = types.ModuleType("fake_webrtc2")
    mod.create = lambda config: Webrtcish()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_webrtc2", mod)
    from micnaga.pipeline.aec import AEC_ENGINES

    monkeypatch.setattr(
        AEC_ENGINES, "_entries", {"webrtc": "fake_webrtc2:create", "speex": "x.y:z"}
    )
    cfg = Config()
    cfg.processing.aec = "always"
    engine = create_echo_canceller(cfg)
    assert engine is not None and engine.name == "webrtc"


# ----------------------------------------------------------------------------- auriculares


def test_output_kinds_follow_device_changes() -> None:
    meta = _meta(
        "speakers",
        [DeviceChange(300.0, "system", "Altavoces", "speakers")],
        initial_output_kind="headphones",
    )
    assert policy.output_kinds_during(meta, 0, 60) == {"headphones"}
    assert policy.output_kinds_during(meta, 240, 300) == {"headphones"}
    assert policy.output_kinds_during(meta, 280, 340) == {"headphones", "speakers"}
    assert policy.output_kinds_during(meta, 360, 420) == {"speakers"}
    # el microfono que cambia no cuenta
    mic_change = _meta("headphones", [DeviceChange(10.0, "mic", "Otro", "unknown")])
    assert policy.output_kinds_during(mic_change, 0, 60) == {"headphones"}


def test_precheck_by_mode_and_output() -> None:
    assert policy.precheck("off", _meta("speakers")) == "off"
    assert policy.precheck("auto", _meta("headphones")) == "headphones"
    assert policy.precheck("auto", _meta("unknown")) is None  # desconocido: decide la medida
    assert policy.precheck("auto", _meta("speakers")) is None
    assert policy.precheck("always", _meta("headphones")) is None  # «Siempre» es siempre


# ----------------------------------------------------------------------------- decision por trozo


def _tracks(
    seconds: float, leak_from: float | None, gain: float = 0.15
) -> tuple[np.ndarray, np.ndarray]:
    """Conversacion por turnos de ``seconds`` s; con ``leak_from`` el altavoz se oye en el micro
    desde ese segundo (cambio de auriculares a altavoces a mitad)."""
    n = int(seconds * SR)
    system = np.zeros(n, dtype=np.float32)
    mic = np.zeros(n, dtype=np.float32)
    for i, t0 in enumerate(range(0, int(seconds), 60)):
        sysp = _talk([(1, 9), (13, 21), (25, 33), (40, 51)], seed=3 + i)
        micp = _talk([(9, 13), (21, 25), (33, 40), (51, 59)], seed=50 + i)
        a = t0 * SR
        m = min(len(sysp), n - a)
        system[a : a + m] = sysp[:m]
        mic[a : a + m] = micp[:m]
    mic += (0.002 * np.random.default_rng(9).standard_normal(n)).astype(np.float32)
    if leak_from is not None:
        leak = _room(system, 0.12, gain)
        k = int(leak_from * SR)
        mic[k:] += leak[k:]
    return mic, system


class _Recorder:
    name = "rec-aec"

    def __init__(self) -> None:
        self.calls: list[int] = []

    def process(self, mic: np.ndarray, ref: np.ndarray, sr: int) -> np.ndarray:
        self.calls.append(len(mic))
        return np.zeros_like(mic)  # «cancela» todo: se ve que trozos se tocaron


def test_auto_cancels_only_from_the_switch_to_speakers() -> None:
    mic, system = _tracks(240.0, leak_from=120.0)
    rec = _Recorder()
    out, summary = policy.cancel_track("auto", _meta("unknown"), mic, system, SR, rec)
    assert summary.applied == [2, 3], summary.slices
    assert out is not None
    assert np.array_equal(out[: 120 * SR], mic[: 120 * SR])  # antes del cambio, intacto
    assert not np.any(out[120 * SR :])
    # cada trozo cancelado lleva PREROLL_S de audio previo
    assert rec.calls == [int((60 + policy.PREROLL_S) * SR)] * 2
    info = summary.info()
    assert info["aec"] == "rec-aec" and info["applied_slices"] == [2, 3]
    assert info["reasons"] == {"no echo": 2, "echo": 2}


def test_auto_never_touches_the_mic_without_echo_or_with_headphones() -> None:
    mic, system = _tracks(180.0, leak_from=None)
    out, summary = policy.cancel_track("auto", _meta("unknown"), mic, system, SR, _Recorder())
    assert out is None and summary.applied == []
    assert summary.info()["skipped"] == "no echo"
    # con auriculares ni se mide (aunque hubiera fuga: no puede haberla)
    leaky, system = _tracks(120.0, leak_from=0.0)
    out, summary = policy.cancel_track("auto", _meta("headphones"), leaky, system, SR, _Recorder())
    assert out is None and summary.info()["reasons"] == {"headphones": 2}


def test_always_cancels_whenever_the_system_sounds_but_not_in_silence() -> None:
    mic, system = _tracks(120.0, leak_from=None)
    system[60 * SR :] = 0.0  # el segundo minuto sin nadie remoto... y sin nada en los 5 min?
    out, summary = policy.cancel_track("always", _meta("headphones"), mic, system, SR, _Recorder())
    # la ventana del trozo 1 (ultimos 5 min) aun tiene el sistema del minuto 0: se cancela
    assert summary.applied == [0, 1]
    silent = np.zeros_like(system)
    out, summary = policy.cancel_track("always", _meta("speakers"), mic, silent, SR, _Recorder())
    assert out is None and summary.info()["skipped"] == "no system audio"


def test_off_never_cancels() -> None:
    mic, system = _tracks(60.0, leak_from=0.0)
    out, summary = policy.cancel_track("off", _meta("speakers"), mic, system, SR, _Recorder())
    assert out is None and summary.info()["reasons"] == {"off": 1}


# ----------------------------------------------------------------------------- lote = vivo


def _stage3_info(session: Path) -> dict[str, Any]:
    from micnaga.pipeline.stages import get_stages

    stage = next(s for s in get_stages() if s.name == "aec")
    data = json.loads((paths.work_dir(session) / stage.marker_name).read_text(encoding="utf-8"))
    return dict(data["info"])


def _as_recording(session: Path) -> None:
    from micnaga.capture.session import STATE_RECORDING
    from micnaga.types import SyncMark

    meta = CaptureMeta.from_json((session / paths.CAPTURE_JSON).read_text(encoding="utf-8"))
    marks = [SyncMark(k, k / SR) for k in range(0, meta.mic.samples + 1, 10 * SR)]
    meta = replace(
        meta,
        mic=replace(meta.mic, sync=marks),
        system=replace(meta.system, sync=marks),
        extra={**meta.extra, "state": STATE_RECORDING},
    )
    (session / paths.CAPTURE_JSON).write_text(meta.to_json(), encoding="utf-8")


def _finalized(session: Path) -> None:
    meta = CaptureMeta.from_json((session / paths.CAPTURE_JSON).read_text(encoding="utf-8"))
    meta = replace(meta, extra={**meta.extra, "state": "finalized"})
    (session / paths.CAPTURE_JSON).write_text(meta.to_json(), encoding="utf-8")


@pytest.mark.parametrize(
    ("kind", "echo"), [("unknown", True), ("unknown", False), ("headphones", False)]
)
def test_live_and_batch_take_the_same_echo_decisions(kind: str, echo: bool) -> None:
    from micnaga.pipeline.live import LiveProcessor, engines_from_deps

    cfg = Config()
    cfg.gates.min_duration_s = 20.0
    a = make_session(
        f"aec-{kind}-{echo}",
        SyntheticSpec(duration_s=150.0, confirmed=True, seed=4, output_kind=kind, echo=echo),  # type: ignore[arg-type]
    )
    b = a.parent / (a.name + "-batch")
    shutil.copytree(a, b)
    _as_recording(a)
    deps = fake_live_deps(load_script(a))
    engines = engines_from_deps(deps)
    assert engines is not None
    proc = LiveProcessor(a, cfg, engines)
    proc.ensure_manifest()
    assert proc.step()
    proc.close()
    _finalized(a)
    for session in (a, b):
        runner = runner_for(config=cfg, deps=fake_live_deps(load_script(session)), rename=False)
        runner.keep_work = True
        assert runner.run(session).status == "done"
    live_info, batch_info = _stage3_info(a), _stage3_info(b)
    assert live_info.get("live") is True
    if kind == "headphones":
        assert batch_info == {"skipped": "headphones"}
        assert live_info["reasons"] == {"headphones": 3}
        return
    assert live_info["applied_slices"] == batch_info["applied_slices"]
    assert live_info["reasons"] == batch_info["reasons"]
    assert bool(batch_info["applied_slices"]) is echo
