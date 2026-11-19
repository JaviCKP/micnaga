"""Procesado en vivo: transcribir mientras se graba para tenerlo casi listo al colgar.

Mientras una sesion graba (provisional o confirmada), un proceso aparte de prioridad baja
(``micnaga live <carpeta>``, ver ``run_live``) procesa el audio ya escrito en **trozos** de
``SLICE_S`` (60 s) del tiempo alineado y guarda los resultados en ``work/live/``. Al colgar, el
trabajo normal del worker (``PipelineRunner``) llama a ``finish``: procesa solo la cola que falte,
une los trozos y escribe las mismas salidas y marcas de las etapas 2-6 que el pipeline por lotes,
asi que las etapas 7-11 y el formato de salida no cambian.

Por trozo ``k`` (``[S, E)`` en muestras alineadas; ``trozo_k/`` con ``slice.json`` escrito el
ultimo):

1. **Alineacion** (``capture.sync.resample_range_to_clock``): la misma correspondencia
   muestra -> reloj que la etapa 2, leyendo solo el tramo necesario del WAV que se esta grabando.
   Un trozo solo se procesa cuando las marcas de sincronia (cada 10 s) ya lo cubren.
2. **Eco** (``aec.echo_check``): se mide en el trozo; solo si el sistema se oye en el micro se
   cancela con un Speex nuevo y ``AEC_PREROLL_S`` de audio previo para que converja (el estado
   de Speex no se puede guardar; asi un trozo no depende del proceso que hizo el anterior).
3. **Acondicionado**: el paso alto con 2 s de margen a cada lado (identico a la etapa 4, que
   filtra por bloques de 60 s); la ganancia sale de la energia activa **acumulada** hasta este
   trozo (la etapa 4 usa la de toda la pista: es la unica diferencia con el lote).
4. **VAD**: probabilidades de Silero continuando su estado recurrente desde el trozo anterior
   (identicas a pasarlo de una vez).

Despues, por pista, los tramos de voz se calculan sobre las probabilidades **acumuladas**
(mismo algoritmo que la etapa 5) y solo se transcriben los bloques que ya no pueden cambiar
(acaban antes del final del audio menos una guarda; el ultimo bloque abierto espera). Cada bloque
se guarda en ``asr/<pista>_<ini_ms>_<fin_ms>.json``: al final se reutiliza si el bloque final
coincide y si no se transcribe. Asi ninguna palabra se corta en una frontera de trozo (los
bloques salen de la voz, no de los trozos) y ninguna se duplica (``stitch_words``). Con el
diarizador ``onnx`` tambien se calculan en vivo los embeddings de las ventanas de cada tramo de
voz remota (cifrados con DPAPI, como todo embedding) y al final la etapa 7 solo agrupa.

Recorte, descarte y caidas: un trozo que pasa del final real (``trim_end_samples``) o de la
longitud final se rehace; un fichero solo cuenta cuando su escritura atomica termino; una huella
de configuracion (``manifest.json``) distinta invalida todo ``live/``. Si algo falla al final, se
borra ``live/`` y el pipeline sigue por lotes: nunca se pierde una reunion por esto.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
from numpy.typing import NDArray

from micnaga import paths
from micnaga.pipeline.stages.base import (
    StageContext,
    StageResult,
    atomic_write_bytes,
    atomic_write_json,
    read_audio_file,
    write_audio_file,
    write_marker,
)
from micnaga.types import AudioArray, CaptureMeta, Word

if TYPE_CHECKING:
    from micnaga.config import Config
    from micnaga.pipeline.aec.base import EchoCanceller
    from micnaga.pipeline.engines.asr.base import AsrEngine
    from micnaga.pipeline.stages.base import Deps, Stage
    from micnaga.pipeline.vad import VadCarry

log = logging.getLogger(__name__)

LIVE_DIRNAME = "live"
FORMAT = 1
SLICE_S = 60.0
"""Multiplo de los bloques de 60 s del paso alto de la etapa 4 (mismo filtrado)."""
HP_MARGIN_S = 2.0
AEC_PREROLL_S = 8.0
ECHO_LOOKBACK_S = 300.0
"""La decision de eco de un trozo mira los ultimos 5 min (como la etapa 3 mira toda la pista)."""
MANIFEST = "manifest.json"
SLICE_META = "slice.json"
STOP_FILE = "stop"
DIAR_INDEX = "diar_index.json"
TRACKS = ("mic", "system")
WORDS_FILES = {"mic": "06_words_me.json", "system": "06_words_remote.json"}
DEDUPE_TOLERANCE_S = 0.25


class StatefulProbModel(Protocol):
    """VAD con estado recurrente (``vad.SileroVad``)."""

    def probs_from(
        self, audio_16k: AudioArray, carry: VadCarry | None
    ) -> tuple[NDArray[np.float32], VadCarry]: ...


class WindowEmbedder(Protocol):
    """Lo que el procesado en vivo usa del diarizador ONNX."""

    name: str

    def speech_regions(
        self, probs: NDArray[np.float32], duration_s: float, *, final: bool = True
    ) -> list[tuple[float, float]]: ...

    def embed_windows(
        self, audio: AudioArray, regions: Sequence[tuple[float, float]], offset: int = 0
    ) -> tuple[NDArray[np.float32], list[tuple[float, float]], list[int]]: ...

    def close(self) -> None: ...


@dataclass
class LiveEngines:
    """Fabricas de motores (inyectables en los tests). ``None`` en ``vad`` = sin vivo."""

    create_asr: Callable[[Config], AsrEngine]
    create_aec: Callable[[Config], EchoCanceller | None]
    vad: Callable[[Config], StatefulProbModel] | None
    create_embedder: Callable[[Config], WindowEmbedder | None] = lambda _c: None


def engines_from_deps(deps: Deps) -> LiveEngines | None:
    """Motores del vivo a partir de las del runner (``None``: no hay VAD con estado)."""
    if deps.live_vad is None:
        return None
    return LiveEngines(
        create_asr=deps.create_asr,
        create_aec=deps.create_aec,
        vad=deps.live_vad,
        create_embedder=deps.create_live_embedder,
    )


# ----------------------------------------------------------------------------- utilidades


def live_dir(session: Path) -> Path:
    return paths.work_dir(session) / LIVE_DIRNAME


def has_cache(session: Path) -> bool:
    return (live_dir(session) / MANIFEST).is_file()


def discard_cache(session: Path) -> None:
    """Borra lo calculado en vivo (descarte, cambio de configuracion o fallo)."""
    folder = live_dir(session)
    for _ in range(5):
        try:
            shutil.rmtree(folder)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.2)
    shutil.rmtree(folder, ignore_errors=True)


def request_stop(session: Path) -> None:
    """Pide al proceso en vivo de la sesion que pare (lo mira entre bloques)."""
    folder = live_dir(session)
    with contextlib.suppress(OSError):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / STOP_FILE).write_bytes(b"")


def clear_stop(session: Path) -> None:
    with contextlib.suppress(OSError):
        (live_dir(session) / STOP_FILE).unlink(missing_ok=True)


def fingerprint(config: Config) -> dict[str, Any]:
    """Lo que, si cambia, invalida lo calculado en vivo."""
    p, v = config.processing, config.vad
    return {
        "format": FORMAT,
        "sample_rate": config.capture.sample_rate,
        "slice_s": SLICE_S,
        "asr_engine": p.asr_engine,
        "whisper_model": p.whisper_model if p.asr_engine == "whisper" else "",
        "aec": p.aec,
        "diarization": p.diarization,
        "vad": [v.min_speech_ms, v.min_silence_ms, v.pad_ms, v.max_block_s],
    }


def _ms(t: float) -> int:
    return round(t * 1000)


def stitch_words(blocks: Sequence[tuple[float, float, Sequence[Word]]]) -> list[Word]:
    """Une las palabras de bloques consecutivos sin perder ni duplicar ninguna.

    Normalmente los bloques no se solapan (salen de los tramos de voz) y esto solo ordena. Si
    dos bloques se solapan (p. ej. uno calculado en vivo y otro rehecho al final con otra
    frontera), el tramo comun se reparte por su punto medio: cada palabra se queda en el bloque
    que contiene su centro. Despues, **solo junto a ese tramo comun**, se quitan las repeticiones
    exactas (mismo texto normalizado y comienzo a menos de ``DEDUPE_TOLERANCE_S``) que pudieran
    quedar en la frontera; una repeticion real dentro de un bloque («no, no») se conserva.
    """
    ordered = sorted(blocks, key=lambda b: (b[0], b[1]))
    kept: list[Word] = []
    overlaps: list[tuple[float, float]] = []
    for i, (a, b, words) in enumerate(ordered):
        lo, hi = -np.inf, np.inf
        if i > 0 and ordered[i - 1][1] > a:  # solapa con el anterior
            lo = (a + ordered[i - 1][1]) / 2
            overlaps.append((a, ordered[i - 1][1]))
        if i + 1 < len(ordered) and ordered[i + 1][0] < b:  # solapa con el siguiente
            hi = (ordered[i + 1][0] + b) / 2
        for w in words:
            mid = (w.s + w.e) / 2
            if lo <= mid < hi:
                kept.append(w)
    kept.sort(key=lambda w: (w.s, w.e))
    if not overlaps:
        return kept

    def near_overlap(w: Word) -> bool:
        tol = DEDUPE_TOLERANCE_S
        return any(a - tol <= w.s <= b + tol for a, b in overlaps)

    out: list[Word] = []
    for w in kept:
        dup = False
        if near_overlap(w):  # solo en el tramo comun: «sí, sí» dentro de un bloque se respeta
            for prev in reversed(out):
                if w.s - prev.s > DEDUPE_TOLERANCE_S:
                    break
                if _norm(prev.w) == _norm(w.w):
                    dup = True
                    break
        if not dup:
            out.append(w)
    return out


def _norm(text: str) -> str:
    return "".join(c for c in text.casefold() if c.isalnum())


# ----------------------------------------------------------------------------- fuentes


class _Source:
    """Una pista de la sesion (WAV que crece o FLAC final) leida por tramos."""

    def __init__(self, session: Path, meta: CaptureMeta, track: str) -> None:
        info = meta.mic if track == "mic" else meta.system
        flac, wav = (
            (paths.AUDIO_ME_FLAC, paths.AUDIO_ME_WAV)
            if track == "mic"
            else (paths.AUDIO_REMOTE_FLAC, paths.AUDIO_REMOTE_WAV)
        )
        self.path: Path | None = None
        for name in (info.file, flac, wav):
            if name and (session / name).is_file():
                self.path = session / name
                break
        self.marks = list(info.sync)
        self.n = 0
        self.sr = info.sample_rate or 16000
        if self.path is not None:
            import soundfile as sf

            try:
                self.n = int(sf.info(str(self.path)).frames)
            except RuntimeError:  # cabecera a medio escribir: se reintenta en el siguiente paso
                self.n = 0

    def read(self, a: int, b: int) -> NDArray[np.float32]:
        import soundfile as sf

        assert self.path is not None
        data, _sr = sf.read(str(self.path), start=a, stop=b, dtype="float32", always_2d=True)
        mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
        out = np.zeros(b - a, dtype=np.float32)
        out[: len(mono)] = mono[: b - a]
        return out


@dataclass
class _Sources:
    mic: _Source
    system: _Source
    final: bool
    length: int
    """Muestras alineadas utilizables (todas al final; hasta la ultima marca mientras graba)."""

    def track(self, name: str) -> _Source:
        return self.mic if name == "mic" else self.system


def _open_sources(session: Path, meta: CaptureMeta, sr: int, final: bool) -> _Sources:
    from micnaga.capture.sync import clock_length

    mic, system = _Source(session, meta, "mic"), _Source(session, meta, "system")
    lengths = [clock_length(s.marks, s.n, sr) for s in (mic, system)]
    if final:
        length = max(lengths)
    else:
        # solo lo que las marcas de sincronia ya cubren en las dos pistas
        covered = [
            min(n, round(max((m.clock_s for m in s.marks), default=0.0) * sr))
            for s, n in zip((mic, system), lengths, strict=True)
        ]
        length = min(covered)
    return _Sources(mic, system, final, length)


# ----------------------------------------------------------------------------- trozos


@dataclass
class SliceResult:
    index: int
    start: int
    end: int
    gain_db: dict[str, float]
    active: dict[str, list[float]]
    """Energia activa acumulada hasta este trozo incluido: ``[energia, tramas]`` por pista."""
    echo: dict[str, Any] | None = None
    aec: str | None = None
    aec_skipped: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "gain_db": self.gain_db,
            "active": self.active,
            "echo": self.echo,
            "aec": self.aec,
            "aec_skipped": self.aec_skipped,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SliceResult:
        return cls(
            int(d["index"]),
            int(d["start"]),
            int(d["end"]),
            {k: float(v) for k, v in d["gain_db"].items()},
            {k: [float(x) for x in v] for k, v in d["active"].items()},
            d.get("echo"),
            d.get("aec"),
            d.get("aec_skipped"),
        )


@dataclass
class LiveStats:
    slices_cached: int = 0
    slices_done: int = 0
    blocks_cached: int = 0
    blocks_done: int = 0
    regions_cached: int = 0
    regions_done: int = 0
    times: dict[str, float] = field(default_factory=dict)

    def add_time(self, key: str, dt: float) -> None:
        self.times[key] = self.times.get(key, 0.0) + dt


class LiveProcessor:
    """Estado del procesado en vivo de una sesion (lo usan ``run_live`` y ``finish``)."""

    def __init__(
        self,
        session: Path,
        config: Config,
        engines: LiveEngines,
        *,
        stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self.session = session
        self.config = config
        self.engines = engines
        self.sr = config.capture.sample_rate
        self.slice_len = round(SLICE_S * self.sr)
        self.dir = live_dir(session)
        self._stop = stop
        self._asr: AsrEngine | None = None
        self._vad: StatefulProbModel | None = None
        self._embedder: WindowEmbedder | None = None
        self._embedder_tried = False
        self._asr_lock = threading.Lock()
        self.stats = LiveStats()
        self.meta: CaptureMeta | None = None

    # -------------------------------------------------------------- vida

    def close(self) -> None:
        for eng in (self._asr, self._embedder):
            if eng is not None:
                with contextlib.suppress(Exception):
                    eng.close()
        self._asr = None
        self._embedder = None
        self._vad = None
        import gc

        gc.collect()

    def ensure_manifest(self) -> bool:
        """Crea ``manifest.json`` o comprueba que casa; si no casa borra ``live/``."""
        want = fingerprint(self.config)
        file = self.dir / MANIFEST
        if file.is_file():
            try:
                have = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                have = None
            if have == want:
                return True
            log.info("live cache does not match the current configuration; discarded")
            stop_pending = (self.dir / STOP_FILE).is_file()
            discard_cache(self.session)
            if stop_pending:
                request_stop(self.session)
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(file, want)
        return False

    def stopped(self) -> bool:
        return self._stop()

    # -------------------------------------------------------------- trozos

    def _slice_dir(self, k: int) -> Path:
        return self.dir / f"slice_{k:05d}"

    def load_slice(self, k: int) -> SliceResult | None:
        file = self._slice_dir(k) / SLICE_META
        if not file.is_file():
            return None
        try:
            return SliceResult.from_dict(json.loads(file.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def cached_slices(self, length: int | None = None) -> list[SliceResult]:
        """Trozos validos consecutivos desde el 0 (con ``length``: solo los que caben enteros)."""
        out: list[SliceResult] = []
        k = 0
        while True:
            res = self.load_slice(k)
            if res is None or res.start != k * self.slice_len:
                break
            if length is not None:
                expected_end = min((k + 1) * self.slice_len, length)
                margin = round(HP_MARGIN_S * self.sr)
                if res.end != expected_end or (res.end < length and res.end + margin > length):
                    break
            out.append(res)
            k += 1
        return out

    def drop_slices_from(self, k: int) -> None:
        while True:
            d = self._slice_dir(k)
            if not d.exists():
                return
            shutil.rmtree(d, ignore_errors=True)
            k += 1

    def process_slice(self, k: int, src: _Sources, prev: SliceResult | None) -> SliceResult:
        """Alinea, (cancela eco), acondiciona y pasa el VAD por el trozo ``k``."""
        from micnaga.capture.sync import resample_range_to_clock
        from micnaga.pipeline.stages.s03_aec import needs_aec
        from micnaga.pipeline.stages.s04_condition import (
            ACTIVE_THRESHOLD_DBFS,
            FRAME_S,
            MAX_GAIN_DB,
            PEAK_LIMIT,
            TARGET_RMS_DBFS,
            highpass,
        )

        assert self.meta is not None
        sr = self.sr
        start = k * self.slice_len
        end = min(start + self.slice_len, src.length)
        margin = round(HP_MARGIN_S * sr)
        pre = round(AEC_PREROLL_S * sr)
        lo_echo = max(0, end - round(ECHO_LOOKBACK_S * sr))
        lo_aec = max(0, start - pre)
        lo, hi = min(lo_aec, lo_echo), min(src.length, end + margin)
        t0 = time.perf_counter()
        wide = {
            t: resample_range_to_clock(
                src.track(t).read, src.track(t).n, src.track(t).marks, lo, hi, sr
            )
            for t in TRACKS
        }
        self.stats.add_time("align", time.perf_counter() - t0)
        res = SliceResult(k, start, end, {}, {})
        # 2) eco: misma medida que la etapa 3, sobre los ultimos ECHO_LOOKBACK_S (no un minuto
        # suelto: con poco audio la decision oscila)
        if not needs_aec(self.meta):
            res.aec_skipped = "headphones"
        elif self.config.processing.aec == "off":
            res.aec_skipped = "off"
        else:
            t0 = time.perf_counter()
            from micnaga.pipeline.aec.echo_check import measure_echo

            a, b = lo_echo - lo, end - lo
            check = measure_echo(wide["mic"][a:b], wide["system"][a:b], sr)
            res.echo = check.to_dict()
            if not check.echo:
                res.aec_skipped = "no echo"
            else:
                head = lo_aec - lo
                cleaned, res.aec, res.aec_skipped = self._cancel(
                    wide["mic"][head:], wide["system"][head:]
                )
                wide["mic"] = np.concatenate([wide["mic"][:head], cleaned])
            self.stats.add_time("aec", time.perf_counter() - t0)
        aligned = wide
        mic = wide["mic"]
        # 3) acondicionado (paso alto con margen, ganancia por energia activa acumulada)
        t0 = time.perf_counter()
        frame = max(1, int(FRAME_S * sr))
        threshold = 10 ** (ACTIVE_THRESHOLD_DBFS / 20)
        folder = self._slice_dir(k)
        folder.mkdir(parents=True, exist_ok=True)
        conditioned: dict[str, NDArray[np.float32]] = {}
        for t in TRACKS:
            x = mic if t == "mic" else aligned["system"]
            h0 = max(0, start - margin) - lo
            filtered = highpass(np.ascontiguousarray(x[h0:]), sr)
            body = filtered[start - lo - h0 : end - lo - h0]
            energy, count = (prev.active.get(t, [0.0, 0.0]) if prev else [0.0, 0.0])[:2]
            n_frames = len(body) // frame
            if n_frames:
                rms = np.sqrt(
                    np.mean(
                        np.square(
                            body[: n_frames * frame].reshape(n_frames, frame), dtype=np.float64
                        ),
                        axis=1,
                    )
                )
                act = rms > threshold
                energy += float(np.sum(rms[act] ** 2))
                count += float(np.count_nonzero(act))
            res.active[t] = [energy, count]
            gain_db = 0.0
            if count > 0:
                level = float(np.sqrt(energy / count))
                gain_db = min(TARGET_RMS_DBFS - 20 * float(np.log10(level)), MAX_GAIN_DB)
                peak = float(np.max(np.abs(body))) if len(body) else 0.0
                gain = 10 ** (gain_db / 20)
                if peak * gain > PEAK_LIMIT:
                    gain = PEAK_LIMIT / peak
                body = body * np.float32(gain)
                gain_db = float(20 * np.log10(gain))
            res.gain_db[t] = round(gain_db, 2)
            write_audio_file(folder / f"{t}.wav", body, sr)
            conditioned[t] = read_audio_file(folder / f"{t}.wav", sr)  # cuantizado como en disco
        self.stats.add_time("condition", time.perf_counter() - t0)
        # 4) VAD con el estado del trozo anterior
        t0 = time.perf_counter()
        vad = self._vad_model()
        carries = self._load_carry(k - 1) if k > 0 else {}
        probs: dict[str, NDArray[np.float32]] = {}
        state: dict[str, NDArray[np.float32]] = {}
        for t in TRACKS:
            p, carry = vad.probs_from(conditioned[t], carries.get(t))
            probs[t] = p
            state[f"{t}_state"] = carry.state
            state[f"{t}_context"] = carry.context
        _save_npz(folder / "probs.npz", {**probs, **state})
        self.stats.add_time("vad", time.perf_counter() - t0)
        atomic_write_json(folder / SLICE_META, res.to_dict())  # el ultimo: el trozo ya cuenta
        self.stats.slices_done += 1
        return res

    def _cancel(
        self, mic: NDArray[np.float32], ref: NDArray[np.float32]
    ) -> tuple[NDArray[np.float32], str | None, str | None]:
        from micnaga.registry import EngineUnavailableError

        try:
            canceller = self.engines.create_aec(self.config)
        except EngineUnavailableError as exc:
            log.warning("no echo canceller available in live processing: %s", exc)
            return mic, None, "unavailable"
        if canceller is None:
            return mic, None, "off"
        try:
            out = np.asarray(canceller.process(mic, ref, self.sr), dtype=np.float32)
            if len(out) != len(mic):
                raise ValueError("echo canceller changed the length")
            return out, canceller.name, None
        finally:
            close = getattr(canceller, "close", None)
            if callable(close):
                close()

    def _vad_model(self) -> StatefulProbModel:
        if self._vad is None:
            assert self.engines.vad is not None
            self._vad = self.engines.vad(self.config)
        return self._vad

    def _load_carry(self, k: int) -> dict[str, VadCarry]:
        from micnaga.pipeline.vad import VadCarry

        data = _load_npz(self._slice_dir(k) / "probs.npz")
        if data is None:
            raise RuntimeError(f"live slice {k} has no VAD state")
        return {t: VadCarry(data[f"{t}_state"], data[f"{t}_context"]) for t in TRACKS}

    def slice_probs(self, slices: Sequence[SliceResult]) -> dict[str, NDArray[np.float32]]:
        parts: dict[str, list[NDArray[np.float32]]] = {t: [] for t in TRACKS}
        for s in slices:
            data = _load_npz(self._slice_dir(s.index) / "probs.npz")
            if data is None:
                raise RuntimeError(f"live slice {s.index} has no VAD probabilities")
            for t in TRACKS:
                parts[t].append(data[t])
        return {
            t: np.concatenate(v) if v else np.zeros(0, dtype=np.float32) for t, v in parts.items()
        }

    def read_conditioned(self, track: str, a: int, b: int) -> NDArray[np.float32]:
        """Muestras ``[a, b)`` del audio acondicionado de ``track`` (de los trozos)."""
        import soundfile as sf

        out = np.zeros(max(0, b - a), dtype=np.float32)
        k = a // self.slice_len
        while True:
            s0 = k * self.slice_len
            if s0 >= b:
                break
            path = self._slice_dir(k) / f"{track}.wav"
            if not path.is_file():
                break
            x0, x1 = max(a, s0), min(b, s0 + self.slice_len)
            data, _ = sf.read(str(path), start=x0 - s0, stop=x1 - s0, dtype="float32")
            out[x0 - a : x0 - a + len(data)] = data
            k += 1
        return out

    # -------------------------------------------------------------- bloques de ASR

    def asr_blocks(
        self, probs: NDArray[np.float32], length: int, final: bool
    ) -> list[tuple[float, float]]:
        """Bloques de la etapa 5 que ya no pueden cambiar (todos si ``final``)."""
        from micnaga.pipeline.vad import group_regions, region_params, regions_with

        params = region_params(self.config)
        duration = length / self.sr
        regions = regions_with(probs, duration, params)
        if not final:
            limit = duration - params.stable_guard_s
            regions = [r for r in regions if r.end <= limit]
        blocks = group_regions(regions, self.config.vad.max_block_s)
        if not final and blocks:
            blocks = blocks[:-1]  # el ultimo aun puede crecer con la voz que venga
        return blocks

    def _block_file(self, track: str, a: float, b: float) -> Path:
        return self.dir / "asr" / f"{track}_{_ms(a)}_{_ms(b)}.json"

    def _asr_engine(self) -> AsrEngine:
        with self._asr_lock:
            if self._asr is None:
                self._asr = self.engines.create_asr(self.config)
                atomic_write_json(self.dir / "asr_engine.json", {"name": self._asr.name})
            return self._asr

    def transcribe_blocks(self, track: str, blocks: Sequence[tuple[float, float]]) -> list[Word]:
        """Transcribe (o reutiliza) cada bloque; devuelve las palabras unidas de la pista."""
        from micnaga.pipeline.stages.s06_asr import MIN_CHUNK_S, transcribe_piece

        pieces: list[tuple[float, float, Sequence[Word]]] = []
        for a, b in blocks:
            file = self._block_file(track, a, b)
            words: list[Word] | None = None
            if file.is_file():
                with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
                    words = [Word.from_dict(d) for d in json.loads(file.read_text("utf-8"))]
            if words is not None:
                self.stats.blocks_cached += 1
            else:
                if self.stopped():
                    raise _Stopped
                words = []
                if b - a >= MIN_CHUNK_S:
                    i0, i1 = max(0, int(a * self.sr)), int(b * self.sr)
                    piece = self.read_conditioned(track, i0, i1)
                    t0 = time.perf_counter()
                    words = transcribe_piece(self._asr_engine(), piece, a, self.sr)
                    self.stats.add_time("asr", time.perf_counter() - t0)
                atomic_write_json(file, [w.to_dict() for w in words])
                self.stats.blocks_done += 1
            pieces.append((a, b, words))
        return stitch_words(pieces)

    def commit_asr(
        self, probs: dict[str, NDArray[np.float32]], length: int, final: bool
    ) -> dict[str, tuple[list[tuple[float, float]], list[Word]]]:
        """Transcribe (o reutiliza) los bloques estables de las dos pistas."""
        plan = {t: self.asr_blocks(probs[t], length, final) for t in TRACKS}
        (self.dir / "asr").mkdir(parents=True, exist_ok=True)
        return {t: (plan[t], self.transcribe_blocks(t, plan[t])) for t in TRACKS}

    # -------------------------------------------------------------- ventanas de diarizacion

    def embedder(self) -> WindowEmbedder | None:
        if not self._embedder_tried:
            self._embedder_tried = True
            if self.config.processing.diarization == "onnx":
                try:
                    self._embedder = self.engines.create_embedder(self.config)
                except Exception as exc:
                    log.warning("live speaker windows disabled: %s", exc)
                    self._embedder = None
        return self._embedder

    def _region_file(self, r0: float, r1: float) -> Path:
        return self.dir / "diar" / f"{_ms(r0)}_{_ms(r1)}.dpapi"

    def commit_diar(
        self, probs: NDArray[np.float32], length: int, final: bool
    ) -> list[tuple[float, float]] | None:
        """Embeddings de las ventanas de los tramos remotos estables (cifrados). ``None`` si no
        hay embedder o DPAPI falla (la etapa 7 diariza entonces de la forma normal)."""
        emb = self.embedder()
        if emb is None:
            return None
        from micnaga.store.voiceprints import VoiceprintError, write_protected_json

        regions = emb.speech_regions(probs, length / self.sr, final=final)
        (self.dir / "diar").mkdir(parents=True, exist_ok=True)
        for r0, r1 in regions:
            file = self._region_file(r0, r1)
            if file.is_file():
                self.stats.regions_cached += 1
                continue
            if self.stopped():
                raise _Stopped
            t0 = time.perf_counter()
            a0 = round(r0 * 100) * 160  # como embed_windows (16 kHz, tramas de 10 ms)
            piece = self.read_conditioned("system", a0, int(r1 * self.sr))
            vecs, spans, _ = emb.embed_windows(piece, [(r0, r1)], offset=a0)
            try:
                write_protected_json(
                    file,
                    {"emb": vecs.astype(np.float32).tolist(), "spans": [list(s) for s in spans]},
                )
            except (VoiceprintError, ImportError, OSError) as exc:
                log.warning("live speaker windows skipped, cannot store them encrypted: %s", exc)
                return None
            self.stats.add_time("diar", time.perf_counter() - t0)
            self.stats.regions_done += 1
        return regions

    # -------------------------------------------------------------- un paso en vivo

    def step(self) -> bool:
        """Procesa lo que haya disponible mientras se graba. ``True`` si hizo algo."""
        self.meta = read_meta_shared(self.session)
        src = _open_sources(self.session, self.meta, self.sr, final=False)
        margin = round(HP_MARGIN_S * self.sr)
        slices = self.cached_slices()
        did = False
        k = len(slices)
        while (k + 1) * self.slice_len + margin <= src.length:
            if self.stopped():
                raise _Stopped
            slices.append(self.process_slice(k, src, slices[-1] if slices else None))
            k += 1
            did = True
        if did and slices:
            length = slices[-1].end
            probs = self.slice_probs(slices)
            self.commit_asr(probs, length, final=False)
            self.commit_diar(probs["system"], length, final=False)
        return did


class _Stopped(Exception):
    """Se pidio parar (en vivo): se sale entre bloques sin dejar nada a medias."""


def _save_npz(path: Path, arrays: dict[str, NDArray[np.float32]]) -> None:
    buf = io.BytesIO()
    kwargs: dict[str, Any] = dict(arrays)
    np.savez(buf, **kwargs)
    atomic_write_bytes(path, buf.getvalue())


def _load_npz(path: Path) -> dict[str, NDArray[np.float32]] | None:
    if not path.is_file():
        return None
    try:
        with np.load(path) as data:
            return {k: np.asarray(data[k], dtype=np.float32) for k in data.files}
    except (OSError, ValueError):
        return None


# ----------------------------------------------------------------------------- al colgar


def finish(
    ctx: StageContext,
    stages: Sequence[Stage],
    engines: LiveEngines,
    *,
    on_progress: Callable[[float], None] | None = None,
) -> bool:
    """Completa lo calculado en vivo y deja hechas las etapas 2-6. ``False`` si no aplica.

    Procesa los trozos que falten (la cola), transcribe los bloques pendientes de las dos
    pistas, calcula las ventanas de diarizacion que falten, escribe ``04_*.wav``,
    ``05_vad.json``, ``05_probs.npz`` y ``06_words_*.json`` y las marcas 2-6 (las ultimas).
    """
    progress = on_progress or (lambda _f: None)
    proc = LiveProcessor(ctx.session, ctx.config, engines)
    if not proc.ensure_manifest():
        discard_cache(ctx.session)
        return False
    t_start = time.perf_counter()
    try:
        proc.meta = ctx.meta
        src = _open_sources(ctx.session, ctx.meta, proc.sr, final=True)
        length = src.length
        if length <= 0:
            return False
        slices = proc.cached_slices(length)
        proc.stats.slices_cached = len(slices)
        proc.drop_slices_from(len(slices))
        n_slices = -(-length // proc.slice_len)
        for k in range(len(slices), n_slices):
            slices.append(proc.process_slice(k, src, slices[-1] if slices else None))
            progress(0.3 * (k + 1) / n_slices)
        probs = proc.slice_probs(slices)
        _release_vad(proc)
        t0 = time.perf_counter()
        words = proc.commit_asr(probs, length, final=True)
        proc.stats.add_time("asr_wall", time.perf_counter() - t0)
        progress(0.8)
        regions = proc.commit_diar(probs["system"], length, final=True)
        progress(0.9)
        _write_outputs(proc, ctx, stages, slices, probs, words, regions, length, t_start)
        progress(1.0)
        return True
    finally:
        proc.close()


def _release_vad(proc: LiveProcessor) -> None:
    proc._vad = None
    with contextlib.suppress(Exception):
        from micnaga.pipeline.vad import release_model

        release_model()


def _write_outputs(
    proc: LiveProcessor,
    ctx: StageContext,
    stages: Sequence[Stage],
    slices: Sequence[SliceResult],
    probs: dict[str, NDArray[np.float32]],
    words: dict[str, tuple[list[tuple[float, float]], list[Word]]],
    regions: list[tuple[float, float]] | None,
    length: int,
    t_start: float,
) -> None:
    import soundfile as sf

    from micnaga.pipeline.stages.s05_vad import PROBS, save_probs
    from micnaga.pipeline.vad import region_params, regions_with

    sr = proc.sr
    # 04: audio acondicionado entero (PCM 16 bits, igual que la etapa 4)
    for t in TRACKS:
        parts = [
            sf.read(str(proc._slice_dir(s.index) / f"{t}.wav"), dtype="int16")[0] for s in slices
        ]
        data = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
        out = ctx.path(f"04_{t}.wav")
        tmp = out.with_name(f".tmp-04_{t}.wav")
        sf.write(str(tmp), data, sr, subtype="PCM_16", format="WAV")
        tmp.replace(out)
    # 05: tramos y bloques (mismo algoritmo y parametros que la etapa 5)
    params = region_params(ctx.config)
    vad_out: dict[str, dict[str, list[list[float]]]] = {}
    n_regions: dict[str, int] = {}
    for t in TRACKS:
        regs = regions_with(probs[t], length / sr, params)
        vad_out[t] = {
            "regions": [[r.start, r.end] for r in regs],
            "chunks": [[a, b] for a, b in words[t][0]],
        }
        n_regions[t] = len(regs)
    save_probs(ctx.path(PROBS), probs)
    ctx.write_json("05_vad.json", vad_out)
    # 06: palabras
    counts: dict[str, int] = {}
    for t in TRACKS:
        ws = words[t][1]
        ctx.write_json(WORDS_FILES[t], [w.to_dict() for w in ws])
        counts[t] = len(ws)
    # ventanas de diarizacion (indice de los tramos, en orden)
    if regions is not None:
        atomic_write_json(
            proc.dir / DIAR_INDEX,
            {"embedder": getattr(proc.embedder(), "name", ""), "regions": regions},
        )
    # marcas 2-6 (lo ultimo)
    used_aec = sorted({s.aec for s in slices if s.aec})
    stats = proc.stats
    live_info = {
        "slices": len(slices),
        "slices_cached": stats.slices_cached,
        "blocks_cached": stats.blocks_cached,
        "blocks_done": stats.blocks_done,
        "regions_cached": stats.regions_cached,
        "regions_done": stats.regions_done,
        "times": {k: round(v, 2) for k, v in stats.times.items()},
    }
    by_name = {s.name: s for s in stages}
    elapsed = time.perf_counter() - t_start
    echo_slices = sum(1 for s in slices if s.echo is not None and s.echo.get("echo"))
    results = {
        "align": StageResult(
            [],
            {
                "duration_s": length / sr,
                "length_mismatch_s": 0.0,
                "within_tolerance": True,
                "live": live_info,
            },
        ),
        "aec": StageResult(
            [],
            {"aec": used_aec[0], "echo_slices": echo_slices, "live": True}
            if used_aec
            else {
                "skipped": slices[0].aec_skipped if slices else "headphones",
                "echo_slices": echo_slices,
                "live": True,
            },
        ),
        "condition": StageResult(
            ["04_mic.wav", "04_system.wav"],
            {"gain_db": slices[-1].gain_db if slices else {}, "live": True},
        ),
        "vad": StageResult(["05_vad.json", PROBS], {"regions": n_regions, "live": True}),
        "asr": StageResult(
            list(WORDS_FILES.values()),
            {
                "engine": _engine_name(proc),
                "words": counts,
                "live": True,
            },
        ),
    }
    for name, result in results.items():
        write_marker(ctx.session, by_name[name], result, elapsed if name == "asr" else 0.0)
    log.info(
        "live processing finished: %d slices (%d cached), %d ASR blocks (%d cached), "
        "%d speaker regions (%d cached) in %.1f s",
        len(slices),
        stats.slices_cached,
        stats.blocks_cached + stats.blocks_done,
        stats.blocks_cached,
        stats.regions_cached + stats.regions_done,
        stats.regions_cached,
        elapsed,
    )


def _engine_name(proc: LiveProcessor) -> str:
    if proc._asr is not None:
        return str(proc._asr.name)
    try:
        return str(json.loads((proc.dir / "asr_engine.json").read_text("utf-8"))["name"])
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


def load_diar_windows(
    ctx: StageContext, diarizer: object
) -> (
    tuple[list[tuple[float, float]], NDArray[np.float32], list[tuple[float, float]], list[int]]
    | None
):
    """Ventanas de diarizacion calculadas en vivo (para la etapa 7), o ``None``."""
    folder = live_dir(ctx.session)
    index_file = folder / DIAR_INDEX
    if not index_file.is_file() or not callable(getattr(diarizer, "turns_from_embeddings", None)):
        return None
    try:
        index = json.loads(index_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if index.get("embedder") != getattr(diarizer, "name", None):
        return None
    from micnaga.store.voiceprints import VoiceprintError, read_protected_json

    regions: list[tuple[float, float]] = []
    vecs: list[NDArray[np.float32]] = []
    spans: list[tuple[float, float]] = []
    region_of: list[int] = []
    for r0, r1 in index.get("regions", []):
        file = folder / "diar" / f"{_ms(r0)}_{_ms(r1)}.dpapi"
        try:
            data = read_protected_json(file)
        except (VoiceprintError, ImportError, OSError):
            return None
        ri = len(regions)
        regions.append((float(r0), float(r1)))
        e = np.asarray(data["emb"], dtype=np.float32).reshape(-1, 256)
        if len(e):
            vecs.append(e)
            for s in data["spans"]:
                spans.append((float(s[0]), float(s[1])))
                region_of.append(ri)
    emb = np.concatenate(vecs) if vecs else np.zeros((0, 256), dtype=np.float32)
    if len(emb) != len(spans):
        return None
    return regions, emb, spans, region_of


# ----------------------------------------------------------------------------- proceso en vivo


BUSY_CPU_PERCENT = 85.0
POLL_S = 3.0
BUSY_WAIT_S = 5.0


def _session_hash(session: Path) -> str:
    return hashlib.sha1(str(session.resolve()).lower().encode("utf-8")).hexdigest()[:12]


def mutex_name(session: Path) -> str:
    return f"Local\\micnaga-live-{_session_hash(session)}"


def read_meta_shared(session: Path) -> CaptureMeta:
    """Lee ``capture.json`` de una vez y lo suelta enseguida.

    El grabador lo reescribe con ``os.replace`` cada 10 s y en Windows ese reemplazo falla
    mientras otro proceso lo tenga abierto (ni compartiendo el borrado); ``write_capture_json``
    reintenta durante 200 ms, asi que basta con no tenerlo abierto mas que una lectura (< 1 ms)
    y leerlo solo una vez por vuelta (cada pocos segundos), nunca entre bloques.
    """
    from micnaga.capture.session import capture_json_path

    return CaptureMeta.from_json(capture_json_path(session).read_text(encoding="utf-8"))


def quick_stop_reason(session: Path) -> str | None:
    """Comprobacion barata (solo existencia de ficheros) que se hace entre bloques."""
    from micnaga.holding import DISCARD_MARKER

    if not session.is_dir():
        return "session folder gone"
    if (live_dir(session) / STOP_FILE).is_file():
        return "stop requested"
    if (session / DISCARD_MARKER).exists():
        return "discarded"
    return None


def should_keep_running(session: Path) -> str | None:
    """Motivo para parar el proceso en vivo, o ``None`` si debe seguir."""
    from micnaga.capture.recovery import _owner_alive
    from micnaga.capture.session import STATE_RECORDING

    quick = quick_stop_reason(session)
    if quick is not None:
        return quick
    try:
        meta = read_meta_shared(session)
    except (OSError, ValueError, KeyError):
        return None  # capture.json a medio reescribir: se mira en el siguiente paso
    if meta.extra.get("state") != STATE_RECORDING:
        return "recording stopped"
    if not _owner_alive(meta.extra):
        return "recorder is gone"
    return None


MAX_STEP_ERRORS = 5


def run_live(
    session: Path,
    config: Config,
    engines: LiveEngines,
    *,
    poll_s: float = POLL_S,
    busy_percent: float = BUSY_CPU_PERCENT,
    cpu_percent: Callable[[], float] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Bucle del proceso en vivo; devuelve el motivo de salida.

    Entre bloques solo se mira si hay orden de parar (ficheros); ``capture.json`` se lee una
    vez por vuelta. Un fallo pasajero (lectura a medias) se reintenta en la vuelta siguiente;
    tras ``MAX_STEP_ERRORS`` seguidos se deja (el worker lo hara todo al colgar).
    """
    if cpu_percent is None:
        import psutil

        psutil.cpu_percent(None)

        def cpu_percent() -> float:
            return float(psutil.cpu_percent(None))

    reason: str | None = None

    def quick_stop() -> bool:
        nonlocal reason
        reason = quick_stop_reason(session)
        return reason is not None

    def full_stop() -> bool:
        nonlocal reason
        reason = should_keep_running(session)
        return reason is not None

    proc = LiveProcessor(session, config, engines, stop=quick_stop)
    errors = 0
    try:
        if full_stop():
            return reason or "stopped"
        proc.ensure_manifest()
        while not full_stop():
            if cpu_percent() >= busy_percent:
                sleep(BUSY_WAIT_S)  # el equipo esta ocupado: se cede (la cola espera al colgar)
                continue
            try:
                did = proc.step()
                errors = 0
            except _Stopped:
                break
            except Exception:
                errors += 1
                log.warning("live step failed (%d in a row)", errors, exc_info=True)
                if errors >= MAX_STEP_ERRORS:
                    return "too many errors"
                did = False
            if not did:
                sleep(poll_s)
        return reason or "stopped"
    finally:
        proc.close()


__all__ = [
    "LIVE_DIRNAME",
    "SLICE_S",
    "LiveEngines",
    "LiveProcessor",
    "clear_stop",
    "discard_cache",
    "engines_from_deps",
    "finish",
    "has_cache",
    "load_diar_windows",
    "request_stop",
    "run_live",
    "stitch_words",
]
