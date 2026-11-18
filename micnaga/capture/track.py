"""Nucleo de una pista: coloca paquetes sellados con QPC en una linea de tiempo continua.

Es **sincrono y de un solo hilo** (sin reloj propio ni hilos): recibe ``feed(frames, arrival_ns)``
y ``idle(now_ns)`` y escribe en un ``ChunkedWavWriter``. Asi se prueba al detalle sin dormir.

Reglas (seccion 7 de la spec)
-----------------------------
* **Colocacion.** Un paquete de ``n`` muestras que llega en ``arrival`` ocupa
  ``[arrival - n / sr, arrival]``. La muestra 0 de la pista es ``rt = 0`` (inicio de sesion): el
  primer paquete se precede de ceros hasta su inicio.
* **Huecos (trampa 1).** Se mide el hueco *entre llegadas*: ``inicio_i - llegada_(i-1)``. Si
  supera ``gap_threshold_s`` se rellena con ceros de duracion exacta (``round(hueco * 16000)``).
  No se compara el total de muestras con el reloj: la deriva normal de la tarjeta de sonido no
  provoca falsos rellenos (la corrige ``sync.align_tracks`` con las marcas).
* **Silencio del loopback.** Si no llegan paquetes, ``idle`` rellena con ceros hasta
  ``ahora - idle_margin_s`` para que el WAV siga creciendo (y no se pierda nada si se cierra). Si
  luego llega un paquete que solapa con ese relleno, se recorta su cabeza.
* **Marcas de sincronia.** Cada ``sync_interval_s`` se guarda ``(muestras, reloj)`` del paquete
  con menor ``reloj - muestras / sr`` de la ventana (el jitter de llegada solo retrasa, asi que el
  minimo es la medida mas limpia). ``muestras`` incluye lo que soxr retiene (``pending``). Ademas
  hay marcas exactas al inicio, en cada corte (pausa) y al parar.
* **Pausas.** Los paquetes se recortan con ``Timeline.slices``: lo capturado durante una pausa
  nunca llega al disco. Al empezar la pausa la pista se rellena hasta el instante exacto de
  corte, de modo que las dos pistas se cortan en el mismo ``rt`` aunque sus paquetes no coincidan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from micnaga.capture.dsp import StreamResampler, float_to_int16, to_mono_float32
from micnaga.capture.timeline import NS, Timeline
from micnaga.capture.wavio import ChunkedWavWriter
from micnaga.types import SyncMark

log = logging.getLogger(__name__)

F32 = NDArray[np.float32]
_ZERO_BLOCK = 1 << 20
LEVEL_STALE_S = 0.4
"""Sin datos nuevos durante este tiempo, el medidor baja a 0 (loopback en silencio)."""


@dataclass(frozen=True)
class TrackStats:
    gap_fills: int
    gap_fill_s: float
    max_gap_fill_s: float
    idle_fill_s: float
    trimmed_s: float
    packets: int


class Track:
    """Una pista (micro o sistema) escrita a un WAV de 16 kHz."""

    def __init__(
        self,
        name: str,
        timeline: Timeline,
        writer: ChunkedWavWriter,
        *,
        gap_threshold_s: float = 0.1,
        sync_interval_s: float = 10.0,
        idle_margin_s: float = 0.5,
        idle_step_s: float = 0.25,
        flush_interval_s: float = 2.0,
    ) -> None:
        self.name = name
        self.timeline = timeline
        self.writer = writer
        self.out_rate = writer.sample_rate
        self.gap_threshold_s = gap_threshold_s
        self.sync_interval_s = sync_interval_s
        self.idle_margin_s = idle_margin_s
        self.idle_step_s = idle_step_s
        self.flush_interval_ns = int(flush_interval_s * NS)

        self._rs: StreamResampler | None = None
        self._in_rate = 0
        self._seg: int | None = None
        self._last_closed = -1
        self._prev_end_rt = 0.0
        self._has_data = False
        self._idle_filled = False
        self._marks: list[SyncMark] = [SyncMark(0, 0.0)]
        self.mark_seq = 0
        self._win_start_rt = 0.0
        self._win_best: tuple[float, int, float] | None = None
        self._last_flush_ns = timeline.now_ns()
        self._rms = 0.0
        self._peak = 0.0
        self._level_ns = 0
        # estadisticas
        self._gap_fills = 0
        self._gap_fill_s = 0.0
        self._max_gap_fill_s = 0.0
        self._idle_fill_s = 0.0
        self._trimmed_s = 0.0
        self._packets = 0

    # ------------------------------------------------------------- propiedades

    @property
    def samples(self) -> int:
        """Muestras de la pista (en disco + pendientes de bloque)."""
        return self.writer.samples

    @property
    def marks(self) -> list[SyncMark]:
        return list(self._marks)

    @property
    def stats(self) -> TrackStats:
        return TrackStats(
            self._gap_fills,
            self._gap_fill_s,
            self._max_gap_fill_s,
            self._idle_fill_s,
            self._trimmed_s,
            self._packets,
        )

    def levels(self, now_ns: int) -> tuple[float, float]:
        """``(rms, pico)`` del ultimo bloque en [0, 1]; ``(0, 0)`` si esta rancio o en pausa."""
        if now_ns - self._level_ns > int(LEVEL_STALE_S * NS) or self.timeline.paused:
            return 0.0, 0.0
        return self._rms, self._peak

    # ------------------------------------------------------------- formato

    def set_input_format(self, in_rate: int) -> None:
        """Cambia la frecuencia de entrada (reenganche a otro dispositivo)."""
        self._flush_resampler()
        if self._rs is None or in_rate != self._in_rate:
            self._in_rate = in_rate
            self._rs = StreamResampler(in_rate, self.out_rate)

    # ----------------------------------------------------------------- entrada

    def feed(self, frames: NDArray[np.generic], arrival_ns: int) -> None:
        """Procesa un paquete que llego en ``arrival_ns``."""
        if self._rs is None:
            raise RuntimeError("set_input_format() must be called before feed()")
        mono = to_mono_float32(frames)
        n_in = len(mono)
        if n_in == 0:
            return
        in_rate = self._in_rate
        start_ns = arrival_ns - n_in * NS // in_rate
        self._packets += 1
        for s_ns, e_ns, seg in self.timeline.slices(start_ns, arrival_ns):
            i0 = min(max(round((s_ns - start_ns) * in_rate / NS), 0), n_in)
            i1 = min(max(round((e_ns - start_ns) * in_rate / NS), 0), n_in)
            if i1 > i0:
                self._place(mono[i0:i1], s_ns, e_ns, seg)

    def _place(self, mono: F32, s_ns: int, e_ns: int, seg: int) -> None:
        assert self._rs is not None
        if seg <= self._last_closed:
            return  # llego tarde un trozo de un tramo ya cerrado (pausa): se descarta
        self._enter_segment(seg)
        start_rt = self.timeline.rt(s_ns)
        end_rt = self.timeline.rt(e_ns)
        gap = start_rt - self._prev_end_rt
        if (not self._has_data and gap > 0.5 / self.out_rate) or gap >= self.gap_threshold_s:
            self._fill(gap, kind="gap" if self._has_data else "lead")
        elif gap < -1.0 / self.out_rate and self._idle_filled:
            drop = min(len(mono), round(-gap * self._in_rate))
            self._trimmed_s += drop / self._in_rate
            mono = mono[drop:]
            if len(mono) == 0:
                self._prev_end_rt = max(self._prev_end_rt, end_rt)
                self._idle_filled = False
                return
        self._idle_filled = False
        self._has_data = True
        self._emit(self._rs.process(mono))
        self._prev_end_rt = end_rt
        self._note_packet(end_rt)

    def _enter_segment(self, seg: int) -> None:
        if self._seg is None:
            self._seg = seg
        elif seg != self._seg:
            self._end_segment()
            self._seg = seg

    # ------------------------------------------------------------------ idle

    def idle(self, now_ns: int) -> None:
        """Sin paquetes: avanza la linea de tiempo con silencio hasta ``ahora - margen``."""
        seg = self.timeline.segment_at(now_ns)
        if self._seg is not None and seg != self._seg:
            self._end_segment()
        if seg is None:
            return
        if self._seg is None:
            self._seg = seg
        target = self.timeline.rt(now_ns) - self.idle_margin_s
        gap = target - self._prev_end_rt
        if gap >= self.idle_step_s:
            self._fill(gap, kind="idle")
            self._prev_end_rt = target
            self._idle_filled = True
            self._set_level(0.0, 0.0, now_ns)

    def tick(self, now_ns: int) -> None:
        """Vaciado periodico a disco (cota de perdida ante cierre brusco)."""
        if self.writer.pending_samples and now_ns - self._last_flush_ns >= self.flush_interval_ns:
            self._flush_writer(now_ns)
        elif not self.writer.pending_samples:
            self._last_flush_ns = now_ns

    # ----------------------------------------------------------------- cierre

    def finish(self, end_ns: int) -> None:
        """Cierra la pista: cola de soxr, silencio hasta ``end_ns`` y marca final exacta."""
        if self._seg is not None:
            seg_now = self.timeline.segment_at(end_ns)
            if seg_now == self._seg:
                # Si ya se colocaron datos que llegaron justo despues de ``end_ns``, el cierre
                # exacto es donde terminan esos datos.
                end_rt = max(self.timeline.rt(end_ns), self._prev_end_rt)
                self._pad_to(end_rt)
                self._anchor(end_rt)
            else:
                self._end_segment()
        self._emit_window_best()
        self.writer.flush()

    # ------------------------------------------------------------- internos

    def _end_segment(self) -> None:
        """Termina el tramo activo (empieza una pausa): corte exacto en el mismo ``rt``."""
        assert self._seg is not None
        end_rt = self.timeline.segment_end_rt(self._seg)
        if end_rt is not None:
            self._pad_to(end_rt)
            self._anchor(end_rt)
        self._emit_window_best()
        self._flush_writer(self.timeline.now_ns())
        self._last_closed = self._seg
        self._seg = None
        self._set_level(0.0, 0.0, 0)

    def _pad_to(self, end_rt: float) -> None:
        self._flush_resampler()
        gap = end_rt - self._prev_end_rt
        if gap > 0.5 / self.out_rate:
            self._write_zeros(round(gap * self.out_rate))
        self._prev_end_rt = max(self._prev_end_rt, end_rt)

    def _anchor(self, clock_s: float) -> None:
        """Marca exacta: la muestra actual corresponde a ``clock_s`` (tras vaciar soxr)."""
        self._add_mark(self.writer.samples, clock_s)
        self._win_start_rt = clock_s
        self._win_best = None

    def _fill(self, gap_s: float, *, kind: str) -> None:
        n = round(gap_s * self.out_rate)
        self._flush_resampler()
        if n <= 0:
            return
        self._write_zeros(n)
        if kind == "gap":
            self._gap_fills += 1
            self._gap_fill_s += n / self.out_rate
            self._max_gap_fill_s = max(self._max_gap_fill_s, n / self.out_rate)
        elif kind == "idle":
            self._idle_fill_s += n / self.out_rate

    def _write_zeros(self, n: int) -> None:
        while n > 0:
            step = min(n, _ZERO_BLOCK)
            self.writer.append(np.zeros(step, dtype=np.int16))
            n -= step

    def _flush_resampler(self) -> None:
        if self._rs is not None:
            self._emit(self._rs.flush())

    def _emit(self, out: F32) -> None:
        if len(out) == 0:
            return
        self.writer.append(float_to_int16(out))
        rms = float(np.sqrt(np.mean(np.square(out, dtype=np.float32))))
        peak = float(np.max(np.abs(out)))
        self._set_level(min(rms, 1.0), min(peak, 1.0), self.timeline.now_ns())

    def _set_level(self, rms: float, peak: float, now_ns: int) -> None:
        self._rms, self._peak, self._level_ns = rms, peak, now_ns

    def _flush_writer(self, now_ns: int) -> None:
        self.writer.flush()
        self._last_flush_ns = now_ns

    def _note_packet(self, arrival_rt: float) -> None:
        """Candidato a marca: el paquete de la ventana con menor ``reloj - muestras / sr``."""
        assert self._rs is not None
        samples = self.writer.samples + self._rs.pending
        offset = arrival_rt - samples / self.out_rate
        if self._win_best is None or offset < self._win_best[0]:
            self._win_best = (offset, samples, arrival_rt)
        if arrival_rt - self._win_start_rt >= self.sync_interval_s:
            self._emit_window_best()
            self._win_start_rt = arrival_rt

    def _emit_window_best(self) -> None:
        if self._win_best is not None:
            _, samples, clock = self._win_best
            self._add_mark(samples, clock)
            self._win_best = None

    def _add_mark(self, samples: int, clock_s: float) -> None:
        last = self._marks[-1]
        if samples > last.samples and clock_s > last.clock_s:
            self._marks.append(SyncMark(samples, clock_s))
            self.mark_seq += 1
