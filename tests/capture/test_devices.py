"""Deteccion de auriculares/altavoces y el backend WASAPI con un ``PyAudio`` falso."""

from __future__ import annotations

import contextlib
import struct
from typing import Any, ClassVar

import numpy as np
import pytest

from micnaga.capture import devices, wasapi
from micnaga.capture.sources import EndpointInfo, SourceError

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


class FakeStream:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.stopped = False
        self.closed = False

    def stop_stream(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class FakePA:
    """PortAudio toma una instantanea de ``world`` al crearse (como Pa_Initialize)."""

    world: ClassVar[list[dict[str, Any]]] = []
    instances: ClassVar[list[FakePA]] = []

    def __init__(self) -> None:
        self.snapshot = [dict(d) for d in FakePA.world]
        self.terminated = False
        self.streams: list[FakeStream] = []
        FakePA.instances.append(self)

    def get_loopback_device_info_generator(self) -> Any:
        yield from (d for d in self.snapshot if d.get("isLoopbackDevice"))

    def get_device_info_generator_by_host_api(
        self, *, host_api_index: Any = None, host_api_type: Any = None
    ) -> Any:
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
        stream = FakeStream(kwargs)
        self.streams.append(stream)
        return stream

    def terminate(self) -> None:
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
    monkeypatch.setattr(devices, "default_endpoint", lambda flow, role: endpoints.get((flow, role)))
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
