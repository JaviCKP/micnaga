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
  ``Pa_Initialize`` cuenta referencias en todo el proceso: si otro ``WasapiBackend`` (p. ej. el
  ``LevelMonitor`` del asistente) tiene PortAudio abierto a la vez, el reinicio no refresca la
  lista de dispositivos.
* **Un solo hilo para PortAudio.** Algunos controladores WASAPI fallan si los objetos de
  PortAudio se usan desde varios hilos (PyAudioWPatch, issue #23), y el grabador abre en el hilo
  de quien llama a ``start``, reengancha desde el hilo de cada pista y cierra desde otros. Por
  eso **todo** lo que toca PortAudio en un ``WasapiBackend`` (crear ``PyAudio()``, enumerar y
  resolver dispositivos, ``pa.open``, ``stop_stream``/``close``, ``terminate`` y el reinicio)
  se ejecuta en un hilo propio del backend (:class:`PortAudioThread`, con COM inicializado).
  Lo que se pide desde ese mismo hilo se ejecuta en linea (el reinicio llama a
  ``_suspend``/``_resume`` desde dentro de la apertura). El callback sigue en el hilo de
  PortAudio y ``read()`` sigue siendo una lectura de cola sin bloqueos en el hilo de la pista;
  ``probe()`` (solo COM) se queda en el hilo de quien sondea.
  Toda espera esta acotada (``OPEN_TIMEOUT_S`` 3 s, ``CLOSE_TIMEOUT_S`` 1,5 s: un reenganche,
  cerrar y abrir en el hilo de la pista, cabe en los 5 s con que ``Recorder.stop`` espera a sus
  hilos): un controlador colgado da ``PortAudioTimeout`` (un ``SourceError``), se registra y se
  sigue; mientras siga colgado, las llamadas siguientes fallan en el acto. Una apertura que
  termina tarde cierra su propio flujo (y si caduco en la cola ni se ejecuta); un cierre que
  caduco en la cola se ejecuta igualmente cuando el hilo se libera. ``close()`` pide al
  hilo que cierre todo, haga ``terminate`` y salga, y lo espera como mucho ``CLOSE_TIMEOUT_S``;
  es idempotente y, si el backend se vuelve a usar, el hilo arranca de nuevo (solo cuando el
  anterior ya ha salido, para no tener dos hilos con PortAudio). **No se puede confirmar sin
  conectar y desconectar dispositivos reales**: es un arreglo de diseno probado con fakes.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any, Generic, TypeVar, cast

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
OPEN_TIMEOUT_S = 3.0
"""Espera maxima de una apertura (incluido un reinicio de PortAudio)."""
CLOSE_TIMEOUT_S = 1.5
"""Espera maxima de un cierre y de la salida del hilo en ``WasapiBackend.close``."""
THREAD_NAME = "micnaga-portaudio"

T = TypeVar("T")

Resolver = Callable[[Any, bool], dict[str, Any]]
"""``(PyAudio, permitir_defecto) -> info dict`` del dispositivo de PortAudio de un punto final.

Con ``permitir_defecto=False`` solo vale una coincidencia por nombre (``DeviceNotFound`` si no
hay); con ``True`` se acepta el dispositivo por defecto que ve PortAudio (que puede estar
desfasado si PortAudio no se ha reiniciado)."""


class DeviceNotFound(SourceError):
    """PortAudio no ve (todavia) el dispositivo pedido: se reinicializa PortAudio y se reintenta."""


class PortAudioTimeout(SourceError):
    """El hilo de PortAudio no respondio a tiempo (controlador colgado)."""


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


# ------------------------------------------------------------------ hilo de PortAudio


class _Job(Generic[T]):
    """Una llamada encolada al hilo de PortAudio."""

    def __init__(
        self,
        fn: Callable[[], T],
        on_abandoned: Callable[[T], None] | None,
        *,
        run_late: bool,
    ) -> None:
        self.fn = fn
        self.on_abandoned = on_abandoned
        self.run_late = run_late
        """Ejecutar aunque caduque en la cola (cierres: el flujo tiene que pararse igual)."""
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.abandoned = False
        """Quien llamo dejo de esperar (tiempo agotado)."""
        self.result: T | None = None
        self.error: Exception | None = None

    def run(self) -> None:
        """Se ejecuta en el hilo de PortAudio."""
        with self.lock:
            if self.abandoned and not self.run_late:
                self.done.set()  # apertura caducada en la cola: ya nadie la espera
                return
        result: T | None = None
        error: Exception | None = None
        try:
            result = self.fn()
        except Exception as exc:
            error = exc
        with self.lock:
            self.result, self.error = result, error
            self.done.set()
            late = self.abandoned
        if not late:
            return
        if error is not None:
            log.warning("late PortAudio call failed: %s", error)
        elif self.on_abandoned is not None:
            log.warning("late PortAudio call finished; undoing it")
            try:
                self.on_abandoned(cast(T, result))
            except Exception:
                log.exception("undoing a late PortAudio call failed")

    def outcome(self) -> T:
        if self.error is not None:
            raise self.error
        return cast(T, self.result)


class PortAudioThread:
    """Hilo dedicado (daemon) que ejecuta en serie las llamadas a PortAudio.

    Arranca con el primer :meth:`call`; :meth:`stop` le pide salir (antes ejecuta ``on_exit`` en
    el propio hilo) y espera un tiempo acotado. Tras ``stop`` puede volver a arrancar.
    """

    def __init__(
        self, name: str = THREAD_NAME, *, on_exit: Callable[[], None] | None = None
    ) -> None:
        self._name = name
        self._on_exit = on_exit
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._queue: queue.SimpleQueue[_Job[Any] | None] | None = None
        self._stuck: _Job[Any] | None = None
        self._local = threading.local()

    @property
    def thread(self) -> threading.Thread | None:
        """El hilo actual o el ultimo (puede haber terminado)."""
        return self._thread

    @property
    def running(self) -> bool:
        """Arrancado y sin peticion de salida."""
        return self._queue is not None

    def on_thread(self) -> bool:
        return bool(getattr(self._local, "owner", False))

    def call(
        self,
        fn: Callable[[], T],
        *,
        timeout: float,
        on_abandoned: Callable[[T], None] | None = None,
        run_late: bool = False,
    ) -> T:
        """Ejecuta ``fn`` en el hilo (en linea si ya se esta en el) y devuelve su resultado.

        Lanza ``PortAudioTimeout`` si no termina en ``timeout`` s o si el hilo sigue colgado de
        una llamada anterior. Si ``fn`` termina despues de agotarse el tiempo, se llama a
        ``on_abandoned(resultado)`` en el hilo (p. ej. para cerrar un flujo que nadie recogera).
        Sin ``run_late`` una llamada que caduca antes de empezar ya no se ejecuta; con
        ``run_late`` (cierres) se ejecuta siempre: si el hilo esta colgado se encola igualmente
        y se lanza ``PortAudioTimeout`` en el acto.
        """
        if self.on_thread():
            return fn()
        with self._lock:
            stuck = self._stuck
            hung = stuck is not None and not stuck.done.is_set()
            if hung and not run_late:
                raise PortAudioTimeout("PortAudio thread is still stuck in a previous call")
            q = self._ensure_started()
            job: _Job[T] = _Job(fn, on_abandoned, run_late=run_late)
            job.abandoned = hung
            q.put(job)
        if hung:
            raise PortAudioTimeout("PortAudio thread is stuck; the call will run when it frees up")
        if not job.done.wait(timeout):
            with job.lock:
                timed_out = not job.done.is_set()
                if timed_out:
                    job.abandoned = True
            if timed_out:
                with self._lock:
                    self._stuck = job
                raise PortAudioTimeout(f"PortAudio call did not finish in {timeout:.1f} s")
        return job.outcome()

    def _ensure_started(self) -> queue.SimpleQueue[_Job[Any] | None]:
        """Con ``self._lock`` tomado."""
        old = self._thread
        if self._queue is not None and old is not None and old.is_alive():
            return self._queue
        if old is not None and old.is_alive():
            raise PortAudioTimeout("the previous PortAudio thread has not exited yet")
        q: queue.SimpleQueue[_Job[Any] | None] = queue.SimpleQueue()
        thread = threading.Thread(target=self._run, args=(q,), name=self._name, daemon=True)
        self._thread, self._queue, self._stuck = thread, q, None
        thread.start()
        return q

    def stop(self, timeout: float) -> bool:
        """Pide salir y espera como mucho ``timeout`` s (nada si se sabe colgado).

        Devuelve ``True`` si el hilo ya no existe. Idempotente.
        """
        with self._lock:
            thread, q, stuck = self._thread, self._queue, self._stuck
            self._queue = None
        if thread is None:
            return True
        if q is not None:
            q.put(None)
        if self.on_thread():  # no puede esperarse a si mismo: saldra al acabar la llamada
            return True
        hung = stuck is not None and not stuck.done.is_set()
        thread.join(0.0 if hung else timeout)
        if thread.is_alive():
            log.error("PortAudio thread did not exit in %.1f s (driver hung?)", timeout)
            return False
        return True

    def _run(self, q: queue.SimpleQueue[_Job[Any] | None]) -> None:
        self._local.owner = True
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(devices.com_initialized())
            except Exception:
                log.warning("could not initialise COM on the PortAudio thread", exc_info=True)
            try:
                while (job := q.get()) is not None:
                    job.run()
            finally:
                if self._on_exit is not None:
                    try:
                        self._on_exit()
                    except Exception:
                        log.exception("PortAudio cleanup failed")


# ------------------------------------------------------------------ fuentes y backend


class PortAudioSource:
    """Un flujo de PortAudio en modo callback con paquetes sellados con QPC.

    ``open``/``close`` se ejecutan en el hilo de PortAudio del backend; ``read`` no.
    """

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
        self._backend.call(
            lambda: self._open_stream(self._backend.pyaudio()),
            timeout=self._backend.open_timeout_s,
            on_abandoned=lambda _none: self._suspend(),
        )

    def _open_stream(self, pa: Any) -> None:
        """En el hilo de PortAudio."""
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
        """Para el flujo sin marcar la fuente como cerrada (reinicio de PortAudio). En el hilo
        de PortAudio."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                log.exception("closing stream for %s", self.info.name)

    def _resume(self, pa: Any) -> None:
        """Reabre tras un reinicio de PortAudio. En el hilo de PortAudio."""
        if self._closed:
            return
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
        if not self._backend.can_call():
            return  # el backend ya se cerro: su hilo cerro todos los flujos al salir
        try:  # run_late: aunque se agote la espera, el flujo se para cuando el hilo se libere
            self._backend.call(
                self._close_here, timeout=self._backend.close_timeout_s, run_late=True
            )
        except PortAudioTimeout as exc:
            log.error("closing %s: %s", self.info.name, exc)

    def _close_here(self) -> None:
        self._suspend()
        self._backend.forget(self)

    def _shutdown(self) -> None:
        """Cierre desde el apagado del backend (en el hilo de PortAudio, sin encolar nada)."""
        self._closed = True
        self._suspend()


class WasapiBackend:
    """Implementa ``CaptureBackend`` sobre PyAudioWPatch + pycaw.

    ``_pa`` y ``_sources`` solo se tocan desde el hilo de PortAudio (:class:`PortAudioThread`),
    asi que no necesitan cerrojo.
    """

    def __init__(
        self,
        *,
        open_timeout_s: float = OPEN_TIMEOUT_S,
        close_timeout_s: float = CLOSE_TIMEOUT_S,
    ) -> None:
        self.open_timeout_s = open_timeout_s
        self.close_timeout_s = close_timeout_s
        self._pa: Any = None
        self._sources: list[PortAudioSource] = []
        self._runner = PortAudioThread(on_exit=self._shutdown_here)

    # --- hilo de PortAudio

    @property
    def portaudio_thread(self) -> threading.Thread | None:
        """El hilo de PortAudio actual o el ultimo (diagnostico y tests)."""
        return self._runner.thread

    def can_call(self) -> bool:
        """Hay un hilo de PortAudio en marcha al que pedir algo (o ya se esta en el)."""
        return self._runner.running or self._runner.on_thread()

    def call(
        self,
        fn: Callable[[], T],
        *,
        timeout: float,
        on_abandoned: Callable[[T], None] | None = None,
        run_late: bool = False,
    ) -> T:
        """Ejecuta ``fn`` en el hilo de PortAudio (ver :meth:`PortAudioThread.call`)."""
        return self._runner.call(fn, timeout=timeout, on_abandoned=on_abandoned, run_late=run_late)

    def pyaudio(self) -> Any:
        """La instancia de ``PyAudio`` (creada en el hilo de PortAudio)."""
        return self.call(self._pyaudio_here, timeout=self.open_timeout_s)

    def _pyaudio_here(self) -> Any:
        if self._pa is None:
            import pyaudiowpatch as pyaudio

            self._pa = pyaudio.PyAudio()
        return self._pa

    def forget(self, src: PortAudioSource) -> None:
        """En el hilo de PortAudio."""
        if src in self._sources:
            self._sources.remove(src)

    def _reinit_portaudio(self) -> None:
        """Reinicia PortAudio para que vea los dispositivos nuevos (ver docstring del modulo)."""
        self.call(self._reinit_here, timeout=self.open_timeout_s)

    def _reinit_here(self) -> None:
        import pyaudiowpatch as pyaudio

        log.info("re-initialising PortAudio to refresh the device list")
        sources = list(self._sources)
        for s in sources:
            s._suspend()
        pa, self._pa = self._pa, None
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                log.exception("PortAudio terminate failed")
        self._pa = pyaudio.PyAudio()
        for s in sources:
            s._resume(self._pa)

    def _shutdown_here(self) -> None:
        """Cierra todos los flujos y hace ``terminate`` (al salir el hilo de PortAudio)."""
        for s in list(self._sources):  # tambien las que ya marcaron _closed sin llegar aqui
            try:
                s._shutdown()
            except Exception:
                log.exception("closing source %s failed", s.info.name)
        self._sources.clear()
        pa, self._pa = self._pa, None
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                log.exception("PortAudio terminate failed")

    # --- fabrica

    def open_system(self) -> AudioSource:
        return self._open_job(self._open_system_here)

    def open_mic(self) -> AudioSource:
        return self._open_job(self._open_mic_here)

    def _open_job(self, fn: Callable[[], PortAudioSource]) -> AudioSource:
        return self.call(fn, timeout=self.open_timeout_s, on_abandoned=_close_late_source)

    def _open_system_here(self) -> PortAudioSource:
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

        return self._open_here(info, resolve)

    def _open_mic_here(self) -> PortAudioSource:
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

        return self._open_here(info, resolve)

    def _open_here(self, info: EndpointInfo, resolve: Resolver) -> PortAudioSource:
        """En el hilo de PortAudio: abre y, si PortAudio no ve el dispositivo, reinicia y
        reintenta aceptando el dispositivo por defecto."""
        src = PortAudioSource(self, info, resolve)
        try:
            src.open()
        except DeviceNotFound:
            self._reinit_portaudio()
            src = PortAudioSource(self, info, resolve, allow_default=True)
            src.open()
        self._sources.append(src)
        return src

    # --- sondeo y cierre

    def probe(self) -> EndpointSnapshot | None:
        """Solo COM (sin PortAudio): se queda en el hilo de quien sondea."""
        try:
            return devices.poll_ids()
        except Exception:
            log.exception("device probe failed")
            return None

    def close(self) -> None:
        """Cierra los flujos, hace ``terminate`` y para el hilo (espera acotada). Idempotente;
        si el backend se vuelve a usar, el hilo arranca de nuevo."""
        if not self._runner.stop(self.close_timeout_s):
            log.error("PortAudio backend close did not complete; continuing")


def _close_late_source(src: PortAudioSource) -> None:
    """Una apertura que termino tras agotarse la espera: nadie usara la fuente."""
    src.close()
