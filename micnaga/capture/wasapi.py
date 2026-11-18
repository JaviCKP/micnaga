"""Backend real: WASAPI loopback (sistema) y micro de comunicaciones con PyAudioWPatch.

**Solo se puede verificar en un PC con dispositivos de audio** (desde el shell del agente
PortAudio enumera 0 dispositivos); los tests de ``tests/capture`` lo ejercitan con un
``PyAudio`` falso y los ``realdevice`` en una sesion interactiva.

Decisiones
----------
* **Modo callback.** El callback de PortAudio sella ``time.perf_counter_ns()`` (QPC) al llegar
  el bloque y lo encola; el hilo de captura lee con ``timeout``. Un ``read()`` bloqueante en el
  loopback se queda colgado durante el silencio y no se podria parar ni reenganchar.
* **Bloques de 100 ms** (``frames_per_buffer``): pocas llamadas por segundo (CPU baja).
* **Formato nativo.** Se abre a la frecuencia y canales del dispositivo (WASAPI compartido no
  remuestrea) en ``float32``; la mezcla a mono y el remuestreo a 16 kHz los hace ``Track``.
* **Micro = ``eCommunications``.** Se resuelve el id con COM (``devices.default_endpoint``) y
  se busca el dispositivo de PortAudio por nombre (PortAudio usa el nombre amistoso del punto
  final WASAPI); si no aparece, micro por defecto de WASAPI con un aviso en el log.
* **Loopback.** El de la salida por defecto (``eConsole``): se busca ``"<nombre> [Loopback]"``;
  si no, ``get_default_wasapi_loopback()``.
* **Reenganche.** PortAudio toma una *instantanea* de los dispositivos en ``Pa_Initialize``; un
  dispositivo conectado despues puede no aparecer. Si no se encuentra el dispositivo nuevo, se
  reinicializa PortAudio (parar los dos flujos, ``terminate``, nuevo ``PyAudio()``, reabrir los
  dos); el micro pierde unos cientos de ms que quedan como ceros. (Sin verificar en hardware.)
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

import numpy as np

from micnaga.capture import devices
from micnaga.capture.sources import (
    AudioSource,
    EndpointInfo,
    EndpointSnapshot,
    Packet,
    SourceError,
)

log = logging.getLogger(__name__)

BUFFER_S = 0.1

Resolver = Callable[[Any, bool], dict[str, Any]]
"""``(PyAudio, permitir_defecto) -> info dict`` del dispositivo de PortAudio de un punto final.

Con ``permitir_defecto=False`` solo vale una coincidencia por nombre (``DeviceNotFound`` si no
hay); con ``True`` se acepta el dispositivo por defecto que ve PortAudio (que puede estar
desfasado si PortAudio no se ha reiniciado)."""


class DeviceNotFound(SourceError):
    """PortAudio no ve (todavia) el dispositivo pedido: se reinicializa PortAudio y se reintenta."""


def _norm(name: str) -> str:
    """Minusculas, espacios colapsados y sin el sufijo ``[Loopback]``."""
    text = " ".join(name.lower().split())
    return text.removesuffix("[loopback]").strip()


def names_match(endpoint_name: str, portaudio_name: str) -> bool:
    """Compara el nombre amistoso de WASAPI con el de PortAudio (que puede venir recortado)."""
    a, b = _norm(endpoint_name), _norm(portaudio_name)
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def pick_device(endpoint_name: str, candidates: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Mejor candidato por nombre: primero igualdad exacta, luego prefijo."""
    pool = list(candidates)
    key = _norm(endpoint_name)
    for d in pool:
        if _norm(str(d["name"])) == key:
            return dict(d)
    for d in pool:
        if names_match(endpoint_name, str(d["name"])):
            return dict(d)
    return None


class PortAudioSource:
    """Un flujo de PortAudio en modo callback con paquetes sellados con QPC."""

    def __init__(
        self,
        backend: WasapiBackend,
        info: EndpointInfo,
        resolver: Resolver,
        *,
        allow_default: bool = False,
    ) -> None:
        self._backend = backend
        self._allow_default = allow_default
        self.info = info
        self._resolver = resolver
        self.sample_rate = 0
        self.channels = 0
        self._q: queue.SimpleQueue[tuple[bytes, int]] = queue.SimpleQueue()
        self._stream: Any = None
        self._error: str | None = None
        self._closed = False

    # --- ciclo de vida

    def open(self) -> None:
        self._open_stream(self._backend.pyaudio())

    def _open_stream(self, pa: Any) -> None:
        import pyaudiowpatch as pyaudio

        dev = self._resolver(pa, self._allow_default)
        self.sample_rate = int(dev["defaultSampleRate"])
        self.channels = max(1, int(dev["maxInputChannels"]))
        frames = max(1, int(self.sample_rate * BUFFER_S))

        def callback(
            in_data: bytes | None, frame_count: int, time_info: Any, status: int
        ) -> tuple[None, int]:
            if in_data:
                self._q.put((in_data, time.perf_counter_ns()))
            return None, pyaudio.paContinue

        try:
            self._stream = pa.open(
                format=pyaudio.paFloat32,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=int(dev["index"]),
                frames_per_buffer=frames,
                stream_callback=callback,
            )
        except Exception as exc:
            raise SourceError(f"cannot open {self.info.name!r}: {exc}") from exc

    def _suspend(self) -> None:
        """Para el flujo sin marcar la fuente como cerrada (reinicio de PortAudio)."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                log.exception("closing stream for %s", self.info.name)

    def _resume(self, pa: Any) -> None:
        self._allow_default = True
        try:
            self._open_stream(pa)
        except Exception as exc:
            self._error = f"reopen after PortAudio reset failed: {exc}"

    def read(self, timeout: float) -> Packet | None:
        if self._error is not None:
            raise SourceError(self._error)
        try:
            data, arrival = self._q.get(timeout=timeout) if timeout > 0 else self._q.get_nowait()
        except queue.Empty:
            return None
        frames = np.frombuffer(data, dtype=np.float32).reshape(-1, self.channels)
        return Packet(frames=frames, arrival_ns=arrival)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._suspend()
        self._backend.forget(self)


class WasapiBackend:
    """Implementa ``CaptureBackend`` sobre PyAudioWPatch + pycaw."""

    def __init__(self) -> None:
        self._pa: Any = None
        self._lock = threading.RLock()
        self._sources: list[PortAudioSource] = []

    # --- PortAudio

    def pyaudio(self) -> Any:
        with self._lock:
            if self._pa is None:
                import pyaudiowpatch as pyaudio

                self._pa = pyaudio.PyAudio()
            return self._pa

    def forget(self, src: PortAudioSource) -> None:
        with self._lock:
            if src in self._sources:
                self._sources.remove(src)

    def _reinit_portaudio(self) -> None:
        """Reinicia PortAudio para que vea los dispositivos nuevos (ver docstring del modulo)."""
        import pyaudiowpatch as pyaudio

        with self._lock:
            log.info("re-initialising PortAudio to refresh the device list")
            sources = list(self._sources)
            for s in sources:
                s._suspend()
            if self._pa is not None:
                self._pa.terminate()
            self._pa = pyaudio.PyAudio()
            for s in sources:
                s._resume(self._pa)

    # --- fabrica

    def open_system(self) -> AudioSource:
        with devices.com_initialized():
            info = devices.default_endpoint(devices.E_RENDER, devices.E_CONSOLE)
        if info is None:
            raise SourceError("no default output device")

        def resolve(pa: Any, allow_default: bool) -> dict[str, Any]:
            found = pick_device(info.name, pa.get_loopback_device_info_generator())
            if found is not None:
                return found
            if not allow_default:
                raise DeviceNotFound(f"no loopback named {info.name!r} in PortAudio")
            log.warning("loopback for %r not found; using PortAudio's default", info.name)
            try:
                return dict(pa.get_default_wasapi_loopback())
            except (OSError, LookupError) as exc:
                raise SourceError(f"no loopback for {info.name!r}: {exc}") from exc

        return self._open(info, resolve, retry_after_reinit=True)

    def open_mic(self) -> AudioSource:
        with devices.com_initialized():
            info = devices.default_endpoint(devices.E_CAPTURE, devices.E_COMMUNICATIONS)
        if info is None:
            raise SourceError("no default communications microphone")

        def resolve(pa: Any, allow_default: bool) -> dict[str, Any]:
            import pyaudiowpatch as pyaudio

            try:
                inputs = (
                    d
                    for d in pa.get_device_info_generator_by_host_api(
                        host_api_type=pyaudio.paWASAPI
                    )
                    if int(d["maxInputChannels"]) > 0 and not d.get("isLoopbackDevice")
                )
                found = pick_device(info.name, inputs)
                if found is not None:
                    return found
                if not allow_default:
                    raise DeviceNotFound(f"no capture device named {info.name!r} in PortAudio")
                log.warning(
                    "communications mic %r not found in PortAudio; using the WASAPI default",
                    info.name,
                )
                return dict(pa.get_default_wasapi_device(d_in=True))
            except (OSError, LookupError) as exc:
                raise SourceError(f"no capture device: {exc}") from exc

        return self._open(info, resolve, retry_after_reinit=True)

    def _open(
        self, info: EndpointInfo, resolve: Resolver, *, retry_after_reinit: bool
    ) -> AudioSource:
        src = PortAudioSource(self, info, resolve)
        try:
            src.open()
        except DeviceNotFound:
            if not retry_after_reinit:
                raise
            self._reinit_portaudio()
            src = PortAudioSource(self, info, resolve, allow_default=True)
            src.open()
        with self._lock:
            self._sources.append(src)
        return src

    # --- sondeo y cierre

    def probe(self) -> EndpointSnapshot | None:
        try:
            return devices.poll_ids()
        except Exception:
            log.exception("device probe failed")
            return None

    def close(self) -> None:
        with self._lock:
            for s in list(self._sources):
                s.close()
            if self._pa is not None:
                self._pa.terminate()
                self._pa = None
