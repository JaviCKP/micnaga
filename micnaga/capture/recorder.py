"""``Recorder``: dos hilos de captura (micro y sistema) + un vigilante de dispositivos.

Uso desde el CLI o desde la app (bandeja/pastilla)::

    rec = Recorder(load_config().capture, on_event=print)
    rec.start(session_dir, confirmed=False)     # empieza a grabar en este instante
    rec.levels()                                # (micro, sistema) 0..1, barato: 20 fps desde la UI
    rec.add_marker("esto me lo apunto")         # MIC-12
    rec.pause(); rec.resume()                   # MIC-13: nada de la pausa llega a disco
    rec.set_confirmed(True)                     # el usuario pulso «Grabar»
    session = rec.stop()                        # cierra WAV y capture.json (barato)
    finalize_session(session)                   # luego, en segundo plano: WAV -> FLAC

Hilos: ``start``/``stop``/``pause``/``resume``/``add_marker``/``levels``/``status`` son seguros
desde cualquier hilo. ``on_event`` se llama **desde hilos del grabador** (hay que reenviar al hilo
de la UI si hace falta) y nunca debe bloquear.

Cambio de dispositivo: un hilo sondea cada 250 ms (``poll_interval_s``) la salida por defecto y el
micro de comunicaciones; si cambian, el hilo de esa pista cierra la fuente, abre la nueva
(``backend.open_*``) y anota un ``DeviceChange``. Mientras tanto la pista se rellena con ceros
(``Track.idle`` / hueco entre llegadas), asi que la linea de tiempo no se rompe.
"""

from __future__ import annotations

import contextlib
import enum
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from micnaga import paths
from micnaga.capture.session import (
    STATE_RECORDING,
    STATE_STOPPED,
    capture_json_path,
    write_capture_json,
)
from micnaga.capture.sources import AudioSource, CaptureBackend, EndpointInfo, SourceError
from micnaga.capture.timeline import Timeline
from micnaga.capture.track import Track
from micnaga.capture.wavio import ChunkedWavWriter
from micnaga.config import CaptureConfig
from micnaga.types import (
    CaptureMeta,
    DeviceChange,
    Marker,
    OutputKind,
    PrivatePause,
    TrackInfo,
)

log = logging.getLogger(__name__)

MIC = "mic"
SYSTEM = "system"
READ_TIMEOUT_S = 0.1
REOPEN_RETRY_S = 1.0
JOIN_TIMEOUT_S = 5.0
RECORDER_FORMAT = 1
FLUSH_SLACK_S = 0.25
"""El vaciado por tiempo se adelanta este margen: asi, con la latencia del bucle (100 ms) y
el paquete en curso, un cierre brusco pierde menos de ``chunk_flush_s`` (MIC-09)."""


class CaptureError(Exception):
    """No se pudo empezar a grabar (ninguna fuente disponible, sesion ya iniciada...)."""


class RecorderState(enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass(frozen=True)
class RecorderEvent:
    """Aviso del grabador. ``kind``: ``device_change``, ``source_error``, ``source_recovered``,
    ``state`` (``message`` = nuevo estado) o ``warning``."""

    kind: str
    message: str
    track: str | None = None
    t: float = 0.0
    """Tiempo de grabacion (s) en que ocurrio."""


@dataclass(frozen=True)
class RecorderStatus:
    state: RecorderState
    elapsed_s: float
    session_dir: Path | None
    output_kind: OutputKind
    mic_device: str
    system_device: str
    mic_samples: int
    system_samples: int
    markers: int
    confirmed: bool


@dataclass
class _Worker:
    name: str
    track: Track
    source: AudioSource | None = None
    thread: threading.Thread | None = None
    reattach: threading.Event = field(default_factory=threading.Event)
    retry_at_ns: int = 0
    device: str = ""
    device_id: str = ""
    source_rate: int = 0
    source_channels: int = 0
    seen_mark_seq: int = 0
    fail_reported: bool = False


class Recorder:
    """Grabador de dos pistas sincronizadas (micro + audio del sistema)."""

    def __init__(
        self,
        config: CaptureConfig | None = None,
        *,
        backend: CaptureBackend | None = None,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        on_event: Callable[[RecorderEvent], None] | None = None,
        mic_gap_threshold_s: float = 0.25,
        system_gap_threshold_s: float = 0.1,
        poll_interval_s: float = 0.25,
        fsync: bool = True,
    ) -> None:
        self.config = config or CaptureConfig()
        self._backend = backend
        self._clock = clock_ns
        self._on_event = on_event
        self._mic_gap = mic_gap_threshold_s
        self._sys_gap = system_gap_threshold_s
        self._poll_s = poll_interval_s
        self._fsync = fsync

        self._lock = threading.RLock()
        self._meta_lock = threading.Lock()
        self._state = RecorderState.IDLE
        self._session_dir: Path | None = None
        self._timeline: Timeline | None = None
        self._workers: dict[str, _Worker] = {}
        self._monitor: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._stop_ns: int | None = None
        self._started_at = ""
        self._confirmed = False
        self._meet_title: str | None = None
        self._meet_code: str | None = None
        self._markers: list[Marker] = []
        self._device_changes: list[DeviceChange] = []
        self._kinds_seen: list[OutputKind] = []
        self._initial_kind: OutputKind = "unknown"
        self._pid_create_time: float | None = None

    # ------------------------------------------------------------- consulta

    @property
    def state(self) -> RecorderState:
        with self._lock:
            if self._state is RecorderState.RECORDING and self._timeline is not None:
                return RecorderState.PAUSED if self._timeline.paused else RecorderState.RECORDING
            return self._state

    @property
    def session_dir(self) -> Path | None:
        return self._session_dir

    @property
    def status(self) -> RecorderStatus:
        mic = self._workers.get(MIC)
        sysw = self._workers.get(SYSTEM)
        return RecorderStatus(
            state=self.state,
            elapsed_s=self.elapsed_s(),
            session_dir=self._session_dir,
            output_kind=self._overall_kind(),
            mic_device=mic.device if mic else "",
            system_device=sysw.device if sysw else "",
            mic_samples=mic.track.samples if mic else 0,
            system_samples=sysw.track.samples if sysw else 0,
            markers=len(self._markers),
            confirmed=self._confirmed,
        )

    def elapsed_s(self) -> float:
        """Segundos grabados (sin pausas privadas); se congela en pausa y tras ``stop``."""
        tl = self._timeline
        if tl is None:
            return 0.0
        if self._stop_ns is not None:
            return tl.rt(self._stop_ns)
        return tl.elapsed_s()

    def levels(self) -> tuple[float, float]:
        """``(micro, sistema)``: RMS del ultimo bloque en [0, 1]. Sin bloqueos (20 fps)."""
        if self._state is not RecorderState.RECORDING:
            return 0.0, 0.0
        now = self._clock()
        mic = self._workers.get(MIC)
        sysw = self._workers.get(SYSTEM)
        return (
            mic.track.levels(now)[0] if mic else 0.0,
            sysw.track.levels(now)[0] if sysw else 0.0,
        )

    def peaks(self) -> tuple[float, float]:
        """Como :meth:`levels` pero con el pico en vez del RMS."""
        if self._state is not RecorderState.RECORDING:
            return 0.0, 0.0
        now = self._clock()
        mic = self._workers.get(MIC)
        sysw = self._workers.get(SYSTEM)
        return (
            mic.track.levels(now)[1] if mic else 0.0,
            sysw.track.levels(now)[1] if sysw else 0.0,
        )

    # --------------------------------------------------------------- control

    def start(
        self,
        session_dir: Path,
        *,
        confirmed: bool = False,
        meet_title: str | None = None,
        meet_code: str | None = None,
    ) -> Path:
        """Empieza a grabar en ``session_dir`` (se crea). Devuelve la carpeta."""
        with self._lock:
            if self._state is not RecorderState.IDLE:
                raise CaptureError(f"recorder already used (state={self._state.value})")
            backend = self._backend
            if backend is None:
                from micnaga.capture.wasapi import WasapiBackend  # import perezoso

                backend = self._backend = WasapiBackend()
            session_dir = paths.ensure_dir(Path(session_dir))
            self._session_dir = session_dir
            self._confirmed = confirmed
            self._meet_title, self._meet_code = meet_title, meet_code
            self._started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            try:
                import psutil

                self._pid_create_time = psutil.Process().create_time()
            except Exception:  # pragma: no cover - psutil siempre esta instalado
                self._pid_create_time = None

            # t0 antes de abrir: el tiempo de apertura queda como ceros, no se pierde audio.
            self._timeline = tl = Timeline(self._clock)
            cfg = self.config
            for name, wav, gap in (
                (MIC, paths.AUDIO_ME_WAV, self._mic_gap),
                (SYSTEM, paths.AUDIO_REMOTE_WAV, self._sys_gap),
            ):
                writer = ChunkedWavWriter(
                    session_dir / wav,
                    cfg.sample_rate,
                    block_samples=max(1, round(cfg.chunk_flush_s * cfg.sample_rate)),
                    fsync=self._fsync,
                )
                track = Track(
                    name,
                    tl,
                    writer,
                    gap_threshold_s=gap,
                    sync_interval_s=cfg.sync_log_interval_s,
                    flush_interval_s=max(0.25, cfg.chunk_flush_s - FLUSH_SLACK_S),
                )
                self._workers[name] = _Worker(name=name, track=track)

            # capture.json ya existe antes de abrir nada: un cierre en este punto es recuperable.
            self._write_meta(STATE_RECORDING)
            opened = 0
            for w in self._workers.values():
                if self._attach(w, initial=True):
                    opened += 1
            if opened == 0:
                for w in self._workers.values():
                    w.track.writer.close()
                    with contextlib.suppress(OSError):
                        w.track.writer.path.unlink()
                for leftover in (
                    capture_json_path(session_dir),
                    capture_json_path(session_dir).with_suffix(".json.tmp"),
                ):
                    with contextlib.suppress(OSError):
                        leftover.unlink()
                with contextlib.suppress(OSError):  # solo si esta vacia (no se pisa nada)
                    session_dir.rmdir()
                self._state = RecorderState.IDLE
                self._workers.clear()
                self._timeline = None
                raise CaptureError("no audio source could be opened (mic and system failed)")
            self._state = RecorderState.RECORDING
            self._write_meta(STATE_RECORDING)
            for w in self._workers.values():
                w.thread = threading.Thread(
                    target=self._worker_loop, args=(w,), name=f"micnaga-cap-{w.name}", daemon=True
                )
                w.thread.start()
            self._monitor = threading.Thread(
                target=self._monitor_loop, name="micnaga-cap-monitor", daemon=True
            )
            self._monitor.start()
            self._fire("state", RecorderState.RECORDING.value)
            return session_dir

    def pause(self) -> bool:
        """Pausa privada (MIC-13). ``False`` si no estaba grabando."""
        with self._lock:
            tl = self._timeline
            if self._state is not RecorderState.RECORDING or tl is None or not tl.pause():
                return False
        self._write_meta(STATE_RECORDING)
        self._fire("state", RecorderState.PAUSED.value)
        return True

    def resume(self) -> bool:
        """Reanuda tras una pausa privada. ``False`` si no estaba en pausa."""
        with self._lock:
            tl = self._timeline
            if self._state is not RecorderState.RECORDING or tl is None or tl.resume() is None:
                return False
        self._write_meta(STATE_RECORDING)
        self._fire("state", RecorderState.RECORDING.value)
        return True

    def add_marker(self, label: str = "") -> Marker:
        """Marcador (MIC-12) en el tiempo de grabacion actual (sin pausas)."""
        with self._lock:
            if self._state is not RecorderState.RECORDING or self._timeline is None:
                raise CaptureError("not recording")
            marker = Marker(t=round(self._timeline.elapsed_s(), 3), label=label)
            self._markers.append(marker)
        self._write_meta(STATE_RECORDING)
        return marker

    def set_confirmed(self, confirmed: bool = True) -> None:
        """El usuario confirmo (o no) que es una reunion; solo cambia ``capture.json``."""
        with self._lock:
            self._confirmed = confirmed
            active = self._state is RecorderState.RECORDING
        if active:
            self._write_meta(STATE_RECORDING)

    def set_meeting_info(self, title: str | None = None, code: str | None = None) -> None:
        with self._lock:
            if title is not None:
                self._meet_title = title
            if code is not None:
                self._meet_code = code
            active = self._state is RecorderState.RECORDING
        if active:
            self._write_meta(STATE_RECORDING)

    def stop(self) -> Path:
        """Para, cierra los WAV y deja ``capture.json`` en estado ``stopped``. Barato:
        la conversion a FLAC es ``finalize_session``. Idempotente."""
        with self._lock:
            if self._state is RecorderState.STOPPED and self._session_dir is not None:
                return self._session_dir
            if self._state is not RecorderState.RECORDING or self._timeline is None:
                raise CaptureError("not recording")
            tl = self._timeline
            end_ns = self._clock()
            tl.close(end_ns)
            self._stop_ns = end_ns
            self._stop_evt.set()
            workers = list(self._workers.values())
        for w in workers:
            if w.thread is not None:
                w.thread.join(JOIN_TIMEOUT_S)
                if w.thread.is_alive():
                    log.error("capture thread %s did not stop in %.0f s", w.name, JOIN_TIMEOUT_S)
        if self._monitor is not None:
            self._monitor.join(JOIN_TIMEOUT_S)
        for w in workers:
            w.track.writer.close()
        with self._lock:
            self._state = RecorderState.STOPPED
        self._write_meta(STATE_STOPPED)
        if self._backend is not None:
            try:
                self._backend.close()
            except Exception:
                log.exception("backend close failed")
        self._fire("state", RecorderState.STOPPED.value)
        assert self._session_dir is not None
        return self._session_dir

    # ---------------------------------------------------------------- fuentes

    def _open_source(self, name: str) -> AudioSource:
        assert self._backend is not None
        return self._backend.open_mic() if name == MIC else self._backend.open_system()

    def _attach(self, w: _Worker, *, initial: bool = False) -> bool:
        """Cierra la fuente actual (si hay) y abre la del dispositivo por defecto de ahora."""
        old, w.source = w.source, None
        if old is not None:
            try:
                old.close()
            except Exception:
                log.exception("closing %s source failed", w.name)
        try:
            src = self._open_source(w.name)
        except Exception as exc:
            w.retry_at_ns = self._clock() + int(REOPEN_RETRY_S * 1e9)
            if not w.fail_reported:
                w.fail_reported = True
                self._fire("source_error", f"{w.name}: {exc}", w.name)
            log.warning("could not open %s source: %s", w.name, exc)
            return False
        w.track.set_input_format(src.sample_rate)
        w.source = src
        changed = (not initial) and bool(w.device_id) and src.info.id != w.device_id
        recovered = w.fail_reported
        w.fail_reported = False
        w.device, w.device_id = src.info.name, src.info.id
        w.source_rate, w.source_channels = src.sample_rate, src.channels
        if w.name == SYSTEM:
            self._note_kind(src.info, initial)
        if changed:
            self._record_device_change(w.name, src.info)
        elif recovered and not initial:
            self._fire("source_recovered", f"{w.name}: {src.info.name}", w.name)
        return True

    def _note_kind(self, info: EndpointInfo, initial: bool) -> None:
        with self._lock:
            if initial:
                self._initial_kind = info.output_kind
            self._kinds_seen.append(info.output_kind)

    def _overall_kind(self) -> OutputKind:
        """Politica: ``speakers`` si en algun momento sono por altavoces (o salida desconocida
        mezclada con auriculares); ``headphones`` solo si todo fue por auriculares."""
        kinds = set(self._kinds_seen)
        if not kinds:
            return "unknown"
        if kinds == {"headphones"}:
            return "headphones"
        if "speakers" in kinds:
            return "speakers"
        return "unknown"

    def _record_device_change(self, track: str, info: EndpointInfo) -> None:
        tl = self._timeline
        assert tl is not None
        t = round(tl.elapsed_s(), 3)
        change = DeviceChange(
            t=t,
            track=track,
            device=info.name,
            output_kind=info.output_kind if track == SYSTEM else "unknown",
        )
        with self._lock:
            self._device_changes.append(change)
        self._write_meta(STATE_RECORDING)
        self._fire("device_change", f"{track}: {info.name}", track, t)

    # ------------------------------------------------------------------ hilos

    def _worker_loop(self, w: _Worker) -> None:
        tl = self._timeline
        assert tl is not None
        track = w.track
        while not self._stop_evt.is_set():
            try:
                if w.reattach.is_set():
                    w.reattach.clear()
                    self._attach(w)
                elif w.source is None and self._clock() >= w.retry_at_ns:
                    self._attach(w)
                pkt = None
                src = w.source
                if src is not None:
                    try:
                        pkt = src.read(READ_TIMEOUT_S)
                    except SourceError as exc:
                        self._fail_source(w, exc)
                else:
                    self._stop_evt.wait(READ_TIMEOUT_S)
                stop_ns = self._stop_ns
                if pkt is not None and stop_ns is not None and pkt.arrival_ns > stop_ns:
                    pkt = None  # llego despues de pedir la parada: no pertenece a la sesion
                if pkt is not None:
                    track.feed(pkt.frames, pkt.arrival_ns)
                else:
                    track.idle(self._clock())
                track.tick(self._clock())
                if track.mark_seq != w.seen_mark_seq:
                    w.seen_mark_seq = track.mark_seq
                    self._write_meta(STATE_RECORDING)
            except Exception:
                log.exception("capture loop error on %s", w.name)
                self._stop_evt.wait(0.05)
        self._drain_and_finish(w)

    def _fail_source(self, w: _Worker, exc: Exception) -> None:
        log.warning("%s source failed: %s", w.name, exc)
        src, w.source = w.source, None
        if src is not None:
            try:
                src.close()
            except Exception:
                log.exception("closing failed %s source", w.name)
        w.retry_at_ns = self._clock()  # reintento inmediato (puede haber cambiado el defecto)
        if not w.fail_reported:
            w.fail_reported = True
            self._fire("source_error", f"{w.name}: {exc}", w.name)

    def _drain_and_finish(self, w: _Worker) -> None:
        end_ns = self._stop_ns
        assert end_ns is not None
        src = w.source
        try:
            while src is not None:
                try:
                    pkt = src.read(0.0)
                except SourceError:
                    break
                if pkt is None:
                    break
                if pkt.arrival_ns <= end_ns:
                    w.track.feed(pkt.frames, pkt.arrival_ns)
        except Exception:
            log.exception("draining %s failed", w.name)
        finally:
            if src is not None:
                try:
                    src.close()
                except Exception:
                    log.exception("closing %s source failed", w.name)
                w.source = None
        try:
            w.track.finish(end_ns)
        except Exception:
            log.exception("finishing %s failed", w.name)

    def _monitor_loop(self) -> None:
        backend = self._backend
        assert backend is not None
        # Linea base: los dispositivos con los que se abrieron las fuentes (ids de endpoint).
        last_render = self._workers[SYSTEM].device_id or None
        last_capture = self._workers[MIC].device_id or None
        while not self._stop_evt.wait(self._poll_s):
            try:
                snap = backend.probe()
            except Exception:
                log.exception("device probe failed")
                continue
            if snap is None:
                continue
            render = snap.render.id if snap.render else None
            capture = snap.capture.id if snap.capture else None
            if render != last_render:
                last_render = render
                self._workers[SYSTEM].reattach.set()
            if capture != last_capture:
                last_capture = capture
                self._workers[MIC].reattach.set()

    # ------------------------------------------------------------- metadatos

    def _fire(
        self, kind: str, message: str, track: str | None = None, t: float | None = None
    ) -> None:
        if self._on_event is None:
            return
        if t is None:
            t = self.elapsed_s()
        try:
            self._on_event(RecorderEvent(kind, message, track, t))
        except Exception:
            log.exception("on_event callback failed")

    def _track_info(self, w: _Worker, file: str) -> TrackInfo:
        return TrackInfo(
            file=file,
            device=w.device,
            sample_rate=self.config.sample_rate,
            samples=w.track.samples,
            sync=w.track.marks,
        )

    def build_meta(self, state: str) -> CaptureMeta:
        tl = self._timeline
        assert tl is not None
        mic, sysw = self._workers[MIC], self._workers[SYSTEM]
        with self._lock:
            extra: dict[str, Any] = {
                "state": state,
                "pid": os.getpid(),
                "pid_create_time": self._pid_create_time,
                "clock": "qpc",
                "timeline": "recording time (private pauses excluded)",
                "recorder_format": RECORDER_FORMAT,
                "initial_output_kind": self._initial_kind,
                "output_kinds_seen": list(dict.fromkeys(self._kinds_seen)),
                "sources": {
                    MIC: _source_format(mic),
                    SYSTEM: _source_format(sysw),
                },
                "stats": {
                    MIC: _stats_dict(mic.track),
                    SYSTEM: _stats_dict(sysw.track),
                },
                "duration_s": round(self.elapsed_s(), 3),
            }
            return CaptureMeta(
                started_at=self._started_at,
                mic=self._track_info(mic, paths.AUDIO_ME_WAV),
                system=self._track_info(sysw, paths.AUDIO_REMOTE_WAV),
                output_kind=self._overall_kind(),
                confirmed=self._confirmed,
                meet_title=self._meet_title,
                meet_code=self._meet_code,
                pauses=[PrivatePause(round(a, 3), round(b, 3)) for a, b in tl.pause_records()],
                markers=list(self._markers),
                device_changes=list(self._device_changes),
                extra=extra,
            )

    def _write_meta(self, state: str) -> None:
        if self._session_dir is None or self._timeline is None:
            return
        with self._meta_lock:
            write_capture_json(self._session_dir, self.build_meta(state))


def _source_format(w: _Worker) -> dict[str, Any]:
    return {"device": w.device, "sample_rate": w.source_rate, "channels": w.source_channels}


def _stats_dict(track: Track) -> dict[str, Any]:
    s = track.stats
    return {
        "gap_fills": s.gap_fills,
        "gap_fill_s": round(s.gap_fill_s, 3),
        "max_gap_fill_s": round(s.max_gap_fill_s, 3),
        "idle_fill_s": round(s.idle_fill_s, 3),
        "trimmed_s": round(s.trimmed_s, 3),
        "packets": s.packets,
    }
