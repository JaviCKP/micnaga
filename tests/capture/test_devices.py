"""Deteccion de auriculares/altavoces y el backend WASAPI con un ``PyAudio`` falso."""

from __future__ import annotations

import contextlib
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest

from micnaga.capture import devices, wasapi
from micnaga.capture.recorder import CaptureError, Recorder, RecorderEvent
from micnaga.capture.sources import EndpointInfo, SourceError
from tests.capture.fakes import cfg

# --------------------------------------------------------------- tipo de salida


@pytest.mark.parametrize(
    ("form_factor", "name", "expected"),
    [
        (1, "Speakers (Realtek)", "speakers"),
        (3, "Headphones (Realtek)", "headphones"),
        (5, "Headset Earphone (USB)", "headphones"),
        (1, "Altavoces (Realtek Audio)", "speakers"),
        (9, "LG HDR (HDMI)", "unknown"),  # DigitalAudioDisplayDevice
        (8, "SPDIF Out", "unknown"),
        (2, "Line Out", "unknown"),
        (10, "Algo", "unknown"),
        (None, "Algo", "unknown"),
        (None, "", "unknown"),
        # Bluetooth manos libres: cuenta como auriculares aunque el factor de forma no lo diga
        (1, "Auriculares (Galaxy Buds Hands-Free AG Audio)", "headphones"),
        (None, "Manos libres (Sony WH-1000XM4)", "headphones"),
        (10, "Headset (BT hands-free)", "headphones"),
        (3, "Altavoces", "headphones"),
    ],
)
def test_classify_output(form_factor: int | None, name: str, expected: str) -> None:
    assert devices.classify_output(form_factor, name) == expected


def test_find_form_factor_in_pycaw_properties() -> None:
    props = {
        "{A45C254E-DF1C-4EFD-8020-67D146A850E0} 14": "Altavoces",
        "{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E} 0": 3,
        "{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E} 2": 99,
    }
    assert devices.find_form_factor(props) == 3
    assert devices.find_form_factor({k.lower(): v for k, v in props.items()}) == 3
    assert devices.find_form_factor({"{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E} 0": "19:?"}) is None
    assert devices.find_form_factor({}) is None


def test_names_match_and_pick_device() -> None:
    assert wasapi.names_match(
        "Speakers (Realtek(R) Audio)", "Speakers (Realtek(R) Audio) [Loopback]"
    )
    assert wasapi.names_match("Microphone (Realtek High Definition Audio)", "Microphone (Realtek")
    assert not wasapi.names_match("Speakers (A)", "Speakers (B)")
    assert not wasapi.names_match("", "x")
    pool = [
        {"name": "Speakers (Realtek) 2 [Loopback]", "index": 1},
        {"name": "Speakers (Realtek) [Loopback]", "index": 2},
    ]
    picked = wasapi.pick_device("Speakers (Realtek)", pool)
    assert picked is not None and picked["index"] == 2  # exacto antes que prefijo
    assert wasapi.pick_device("Otro", pool) is None


# ----------------------------------------------------------- PyAudio falso

CALLS: list[tuple[str, str, int]] = []
"""``(operacion, nombre del hilo, ident del hilo)`` de cada llamada a PortAudio."""


def _record(op: str) -> None:
    th = threading.current_thread()
    CALLS.append((op, th.name, threading.get_ident()))


class FakeStream:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.stopped = False
        self.closed = False

    def stop_stream(self) -> None:
        _record("stop_stream")
        self.stopped = True

    def close(self) -> None:
        _record("close")
        self.closed = True


class FakePA:
    """PortAudio toma una instantanea de ``world`` al crearse (como Pa_Initialize)."""

    world: ClassVar[list[dict[str, Any]]] = []
    instances: ClassVar[list[FakePA]] = []

    def __init__(self) -> None:
        _record("PyAudio")
        self.snapshot = [dict(d) for d in FakePA.world]
        self.terminated = False
        self.streams: list[FakeStream] = []
        FakePA.instances.append(self)

    def get_loopback_device_info_generator(self) -> Any:
        _record("enumerate")
        yield from (d for d in self.snapshot if d.get("isLoopbackDevice"))

    def get_device_info_generator_by_host_api(
        self, *, host_api_index: Any = None, host_api_type: Any = None
    ) -> Any:
        _record("enumerate")
        yield from (d for d in self.snapshot if not d.get("isLoopbackDevice"))

    def get_default_wasapi_loopback(self) -> dict[str, Any]:
        loops = [d for d in self.snapshot if d.get("isLoopbackDevice")]
        if not loops:
            raise LookupError("no loopback")
        return loops[0]

    def get_default_wasapi_device(
        self, *, d_out: bool = False, d_in: bool = False
    ) -> dict[str, Any]:
        ins = [d for d in self.snapshot if not d.get("isLoopbackDevice") and d["maxInputChannels"]]
        if not ins:
            raise OSError("no input")
        return ins[0]

    def open(self, **kwargs: Any) -> FakeStream:
        _record("open")
        stream = FakeStream(kwargs)
        self.streams.append(stream)
        return stream

    def terminate(self) -> None:
        _record("terminate")
        self.terminated = True


def _dev(
    index: int, name: str, rate: int, channels: int, *, loopback: bool = False
) -> dict[str, Any]:
    return {
        "index": index,
        "name": name,
        "defaultSampleRate": float(rate),
        "maxInputChannels": channels,
        "isLoopbackDevice": loopback,
    }


@pytest.fixture
def fake_pa(monkeypatch: pytest.MonkeyPatch) -> type[FakePA]:
    import pyaudiowpatch

    FakePA.world = [
        _dev(1, "Microphone (Realtek High Definition Audio)", 44100, 1),
        _dev(2, "Speakers (Realtek High Definition Audio) [Loopback]", 48000, 2, loopback=True),
    ]
    FakePA.instances = []
    CALLS.clear()
    monkeypatch.setattr(pyaudiowpatch, "PyAudio", FakePA)
    monkeypatch.setattr(devices, "com_initialized", contextlib.nullcontext)
    endpoints = {
        (devices.E_RENDER, devices.E_CONSOLE): EndpointInfo(
            "id-spk", "Speakers (Realtek High Definition Audio)", "speakers"
        ),
        (devices.E_CAPTURE, devices.E_COMMUNICATIONS): EndpointInfo(
            "id-mic", "Microphone (Realtek High Definition Audio)"
        ),
    }

    def default_endpoint(flow: int, role: int) -> EndpointInfo | None:
        _record("endpoint")
        return endpoints.get((flow, role))

    monkeypatch.setattr(devices, "default_endpoint", default_endpoint)
    monkeypatch.setattr(
        devices,
        "poll_ids",
        lambda: devices.EndpointSnapshot(
            render=endpoints[(devices.E_RENDER, devices.E_CONSOLE)],
            capture=endpoints[(devices.E_CAPTURE, devices.E_COMMUNICATIONS)],
        ),
    )
    FakePA.endpoints = endpoints  # type: ignore[attr-defined]
    return FakePA


def _callback_of(src: Any) -> Any:
    return src._stream.kwargs["stream_callback"]


def test_wasapi_system_source_opens_loopback_and_stamps_packets(fake_pa: type[FakePA]) -> None:
    backend = wasapi.WasapiBackend()
    src = backend.open_system()
    assert src.info.id == "id-spk" and src.info.output_kind == "speakers"
    assert src.sample_rate == 48000 and src.channels == 2
    kwargs = src._stream.kwargs  # type: ignore[attr-defined]
    assert kwargs["input_device_index"] == 2 and kwargs["input"] is True
    assert kwargs["frames_per_buffer"] == 4800  # bloques de 100 ms
    assert src.read(0.01) is None  # silencio: no hay paquetes (el loopback no entrega nada)
    block = np.linspace(-0.5, 0.5, 4800 * 2, dtype=np.float32)
    cb = _callback_of(src)
    ret = cb(block.tobytes(), 4800, {}, 0)
    assert ret[1] == 0  # paContinue
    pkt = src.read(0.1)
    assert pkt is not None and pkt.frames.shape == (4800, 2) and pkt.frames.dtype == np.float32
    np.testing.assert_array_equal(pkt.frames.reshape(-1), block)
    import time

    assert abs(time.perf_counter_ns() - pkt.arrival_ns) < 5_000_000_000
    src.close()
    assert fake_pa.instances[0].streams[0].closed
    backend.close()
    assert fake_pa.instances[0].terminated


def test_wasapi_mic_is_the_communications_device_by_name(fake_pa: type[FakePA]) -> None:
    fake_pa.world.append(_dev(3, "Headset Microphone (USB)", 16000, 1))
    fake_pa.endpoints[(devices.E_CAPTURE, devices.E_COMMUNICATIONS)] = EndpointInfo(  # type: ignore[attr-defined]
        "id-usb", "Headset Microphone (USB)"
    )
    backend = wasapi.WasapiBackend()
    mic = backend.open_mic()
    assert mic.info.name == "Headset Microphone (USB)"
    assert mic._stream.kwargs["input_device_index"] == 3  # type: ignore[attr-defined]
    assert mic.sample_rate == 16000 and mic.channels == 1
    backend.close()


def test_hot_swap_reinitialises_portaudio_when_the_new_device_is_not_visible(
    fake_pa: type[FakePA],
) -> None:
    backend = wasapi.WasapiBackend()
    mic = backend.open_mic()
    system = backend.open_system()
    first = fake_pa.instances[0]
    # se conectan auriculares: Windows ya los tiene como salida por defecto, PortAudio aun no
    fake_pa.world.append(_dev(7, "Headphones (Bluetooth) [Loopback]", 44100, 2, loopback=True))
    fake_pa.endpoints[(devices.E_RENDER, devices.E_CONSOLE)] = EndpointInfo(  # type: ignore[attr-defined]
        "id-bt", "Headphones (Bluetooth)", "headphones"
    )
    system.close()
    new = backend.open_system()
    assert new.info.output_kind == "headphones" and new.sample_rate == 44100
    assert len(fake_pa.instances) == 2 and first.terminated
    assert new._stream.kwargs["input_device_index"] == 7  # type: ignore[attr-defined]
    # el micro se reengancho sobre la nueva instancia y sigue entregando paquetes
    assert mic._stream is not None and mic._stream in fake_pa.instances[1].streams  # type: ignore[attr-defined]
    _callback_of(mic)(struct.pack("<2f", 0.1, 0.2), 2, {}, 0)
    assert mic.read(0.1) is not None
    backend.close()


def test_missing_devices_raise_source_error(
    fake_pa: type[FakePA], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(devices, "default_endpoint", lambda flow, role: None)
    backend = wasapi.WasapiBackend()
    with pytest.raises(SourceError):
        backend.open_system()
    with pytest.raises(SourceError):
        backend.open_mic()
    backend.close()


def test_backend_probe_returns_ids(fake_pa: type[FakePA]) -> None:
    snap = wasapi.WasapiBackend().probe()
    assert snap is not None and snap.render is not None and snap.render.id == "id-spk"
    assert snap.capture is not None and snap.capture.id == "id-mic"


# ------------------------------------------------- un solo hilo para PortAudio

SPK = (devices.E_RENDER, devices.E_CONSOLE)
MIC = (devices.E_CAPTURE, devices.E_COMMUNICATIONS)


def _wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def _threads_of_calls() -> tuple[set[str], set[int], set[str]]:
    return {c[1] for c in CALLS}, {c[2] for c in CALLS}, {c[0] for c in CALLS}


def _in_thread(name: str, fn: Callable[[], None]) -> None:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            fn()
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=run, name=name)
    t.start()
    t.join(10)
    assert not t.is_alive()
    if errors:
        raise errors[0]


def test_all_portaudio_calls_run_on_one_backend_thread(fake_pa: type[FakePA]) -> None:
    backend = wasapi.WasapiBackend()
    # apertura inicial desde el hilo de quien llama (Recorder.start)
    mic = backend.open_mic()
    system = backend.open_system()
    pa_thread = backend.portaudio_thread
    assert pa_thread is not None and pa_thread.is_alive()
    assert pa_thread.name == wasapi.THREAD_NAME and pa_thread is not threading.current_thread()
    sources: dict[str, Any] = {"mic": mic, "system": system}

    # reenganche desde el hilo de la pista del sistema: auriculares nuevos que PortAudio aun no
    # ve -> DeviceNotFound -> reinicio de PortAudio
    fake_pa.world.append(_dev(7, "Headphones (Bluetooth) [Loopback]", 44100, 2, loopback=True))
    fake_pa.endpoints[SPK] = EndpointInfo("id-bt", "Headphones (Bluetooth)", "headphones")  # type: ignore[attr-defined]

    def reattach_system() -> None:
        sources["system"].close()
        sources["system"] = backend.open_system()

    _in_thread("micnaga-cap-system", reattach_system)
    # y desde el hilo de la pista del micro (otro reinicio)
    fake_pa.world.append(_dev(8, "Headset Microphone (USB)", 16000, 1))
    fake_pa.endpoints[MIC] = EndpointInfo("id-usb", "Headset Microphone (USB)")  # type: ignore[attr-defined]

    def reattach_mic() -> None:
        sources["mic"].close()
        sources["mic"] = backend.open_mic()

    _in_thread("micnaga-cap-mic", reattach_mic)
    assert len(fake_pa.instances) == 3  # dos reinicios
    assert sources["system"].info.id == "id-bt" and sources["mic"].info.id == "id-usb"
    assert sources["system"].sample_rate == 44100 and sources["mic"].sample_rate == 16000
    # cierre desde otro hilo (como _drain_and_finish) y del backend desde el principal
    _in_thread("micnaga-cap-mic-drain", sources["mic"].close)
    pa_ident = pa_thread.ident
    backend.close()
    assert not pa_thread.is_alive()
    assert all(pa.terminated for pa in fake_pa.instances)
    assert all(s.closed for pa in fake_pa.instances for s in pa.streams)

    names, idents, ops = _threads_of_calls()
    assert names == {wasapi.THREAD_NAME}
    assert idents == {pa_ident}
    assert {"PyAudio", "endpoint", "enumerate", "open", "stop_stream", "close", "terminate"} <= ops
    backend.close()  # idempotente


def test_recorder_device_changes_keep_portaudio_on_one_thread(
    fake_pa: type[FakePA], tmp_path: Path
) -> None:
    backend = wasapi.WasapiBackend()
    openers: list[tuple[str, str]] = []
    real_mic, real_system = backend.open_mic, backend.open_system

    def open_mic() -> Any:
        openers.append(("mic", threading.current_thread().name))
        return real_mic()

    def open_system() -> Any:
        openers.append(("system", threading.current_thread().name))
        return real_system()

    backend.open_mic = open_mic  # type: ignore[method-assign]
    backend.open_system = open_system  # type: ignore[method-assign]
    events: list[RecorderEvent] = []
    rec = Recorder(
        cfg(), backend=backend, on_event=events.append, poll_interval_s=0.05, fsync=False
    )
    rec.start(tmp_path / "s")
    pa_thread = backend.portaudio_thread
    assert pa_thread is not None
    pa_ident = pa_thread.ident

    def changed(track: str) -> bool:
        return any(e.kind == "device_change" and e.track == track for e in events)

    fake_pa.world.append(_dev(7, "Headphones (Bluetooth) [Loopback]", 44100, 2, loopback=True))
    fake_pa.endpoints[SPK] = EndpointInfo("id-bt", "Headphones (Bluetooth)", "headphones")  # type: ignore[attr-defined]
    _wait_for(lambda: changed("system"))
    fake_pa.world.append(_dev(8, "Headset Microphone (USB)", 16000, 1))
    fake_pa.endpoints[MIC] = EndpointInfo("id-usb", "Headset Microphone (USB)")  # type: ignore[attr-defined]
    _wait_for(lambda: changed("mic"))
    rec.stop()

    assert not pa_thread.is_alive()
    caller = threading.current_thread().name
    assert ("mic", caller) in openers and ("system", caller) in openers
    assert ("system", "micnaga-cap-system") in openers and ("mic", "micnaga-cap-mic") in openers
    assert len(fake_pa.instances) == 3 and all(pa.terminated for pa in fake_pa.instances)
    assert all(s.closed for pa in fake_pa.instances for s in pa.streams)
    names, idents, ops = _threads_of_calls()
    assert names == {wasapi.THREAD_NAME} and idents == {pa_ident}
    assert {"PyAudio", "open", "stop_stream", "close", "terminate"} <= ops


def test_a_hung_stream_close_cannot_block_the_backend(fake_pa: type[FakePA]) -> None:
    backend = wasapi.WasapiBackend(open_timeout_s=0.3, close_timeout_s=0.3)
    mic = backend.open_mic()
    backend.open_system()
    pa_thread = backend.portaudio_thread
    assert pa_thread is not None
    release = threading.Event()
    stream = fake_pa.instances[0].streams[0]
    stream.close = lambda: release.wait(10)  # type: ignore[assignment,method-assign,return-value]
    try:
        t0 = time.monotonic()
        mic.close()  # se cuelga en el controlador: se registra y se sigue
        assert time.monotonic() - t0 < 1.0
        t0 = time.monotonic()
        with pytest.raises(wasapi.PortAudioTimeout):  # sigue colgado: falla en el acto
            backend.open_system()
        backend.close()
        assert time.monotonic() - t0 < 0.3
        assert pa_thread.is_alive()
        with pytest.raises(wasapi.PortAudioTimeout):  # no arranca un segundo hilo
            backend.open_mic()
    finally:
        release.set()
        pa_thread.join(5)
    assert not pa_thread.is_alive()
    assert fake_pa.instances[0].terminated  # al soltarse, el hilo cerro todo antes de salir
    assert all(s.stopped for s in fake_pa.instances[0].streams)


def test_backend_close_is_bounded_when_the_driver_hangs(fake_pa: type[FakePA]) -> None:
    backend = wasapi.WasapiBackend(close_timeout_s=0.3)
    backend.open_system()
    pa_thread = backend.portaudio_thread
    assert pa_thread is not None
    release = threading.Event()
    stream = fake_pa.instances[0].streams[0]
    stream.close = lambda: release.wait(10)  # type: ignore[assignment,method-assign,return-value]
    try:
        t0 = time.monotonic()
        backend.close()
        assert time.monotonic() - t0 < 1.0
    finally:
        release.set()
        pa_thread.join(5)
    assert not pa_thread.is_alive() and fake_pa.instances[0].terminated


def test_a_late_open_closes_its_own_stream(
    fake_pa: type[FakePA], monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = wasapi.WasapiBackend(open_timeout_s=0.2)
    release = threading.Event()
    real_open = FakePA.open

    def slow_open(self: FakePA, **kwargs: Any) -> FakeStream:
        release.wait(10)
        return real_open(self, **kwargs)

    monkeypatch.setattr(FakePA, "open", slow_open)
    with pytest.raises(wasapi.PortAudioTimeout):
        backend.open_system()
    release.set()
    _wait_for(lambda: bool(fake_pa.instances and fake_pa.instances[0].streams))
    _wait_for(lambda: fake_pa.instances[0].streams[0].closed)
    monkeypatch.setattr(FakePA, "open", real_open)
    src = backend.open_system()  # el hilo ya no esta colgado
    assert src.sample_rate == 48000
    pa_thread = backend.portaudio_thread
    backend.close()
    assert pa_thread is not None and not pa_thread.is_alive()


def test_backend_restarts_its_thread_after_close(fake_pa: type[FakePA]) -> None:
    backend = wasapi.WasapiBackend()
    backend.open_mic()
    first = backend.portaudio_thread
    backend.close()
    backend.close()
    assert first is not None and not first.is_alive() and fake_pa.instances[0].terminated
    backend.open_system()
    second = backend.portaudio_thread
    assert second is not None and second is not first and second.is_alive()
    assert len(fake_pa.instances) == 2
    backend.close()
    assert not second.is_alive() and fake_pa.instances[1].terminated


def test_recorder_start_failure_closes_the_backend(
    fake_pa: type[FakePA], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def broken_open(self: FakePA, **kwargs: Any) -> FakeStream:
        _record("open")
        raise OSError("device in use")

    monkeypatch.setattr(FakePA, "open", broken_open)
    backend = wasapi.WasapiBackend()
    rec = Recorder(cfg(), backend=backend, fsync=False)
    with pytest.raises(CaptureError):
        rec.start(tmp_path / "s")
    pa_thread = backend.portaudio_thread
    assert pa_thread is not None and not pa_thread.is_alive()
    assert fake_pa.instances and all(pa.terminated for pa in fake_pa.instances)
    assert {c[1] for c in CALLS} == {wasapi.THREAD_NAME}
