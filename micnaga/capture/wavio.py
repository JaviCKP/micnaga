"""WAV PCM16 mono recuperable, reparacion de cabeceras y conversion a FLAC.

* :class:`ChunkedWavWriter` escribe a mano (``struct``), no con soundfile, para controlar el
  vaciado: tras cada bloque (2 s) escribe los datos, **reescribe la cabecera** con los tamanos
  validos, ``flush`` y ``os.fsync``. Un cierre brusco pierde como mucho el bloque en curso
  (MIC-09) y el fichero sigue siendo un WAV legible.
* :func:`repair_wav` arregla un WAV cuya cabecera quedo desfasada (cierre entre la escritura de
  datos y la de la cabecera): el numero de muestras sale del **tamano del fichero**, no de la
  cabecera, y se descarta un byte suelto final.
* :func:`wav_to_flac` convierte a FLAC (nivel 5 = ``compression_level`` 5/8 en soundfile),
  comprueba bit a bit el resultado y solo entonces sustituye/borra.
"""

from __future__ import annotations

import contextlib
import logging
import os
import struct
import time
from pathlib import Path
from typing import BinaryIO

import numpy as np
import soundfile as sf
from numpy.typing import NDArray

log = logging.getLogger(__name__)

HEADER_BYTES = 44
BYTES_PER_SAMPLE = 2
_BLOCK = 1 << 20

I16 = NDArray[np.int16]


def build_header(n_samples: int, sample_rate: int) -> bytes:
    """Cabecera RIFF/WAVE de 44 bytes (PCM, 1 canal, 16 bit) para ``n_samples`` muestras."""
    data = n_samples * BYTES_PER_SAMPLE
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data)
        + b"WAVEfmt "
        + struct.pack(
            "<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * BYTES_PER_SAMPLE, BYTES_PER_SAMPLE, 16
        )
        + b"data"
        + struct.pack("<I", data)
    )


class ChunkedWavWriter:
    """Escritor de WAV PCM16 mono con cabecera siempre valida y ``fsync`` por bloque."""

    def __init__(
        self,
        path: Path,
        sample_rate: int,
        *,
        block_samples: int | None = None,
        fsync: bool = True,
    ) -> None:
        self.path = path
        self.sample_rate = sample_rate
        self.block_samples = block_samples or 2 * sample_rate
        self._fsync = fsync
        self._pending: list[I16] = []
        self._pending_n = 0
        self._flushed = 0
        self._closed = False
        self._f: BinaryIO = open(path, "wb")  # noqa: SIM115 - se cierra en close()
        self._f.write(build_header(0, sample_rate))
        self._sync_file()

    @property
    def samples(self) -> int:
        """Muestras recibidas (en disco + pendientes de bloque)."""
        return self._flushed + self._pending_n

    @property
    def flushed_samples(self) -> int:
        """Muestras ya vaciadas a disco con cabecera actualizada."""
        return self._flushed

    @property
    def pending_samples(self) -> int:
        return self._pending_n

    def append(self, pcm: I16) -> None:
        """Anade muestras; vacia a disco en cuanto hay un bloque completo."""
        if self._closed:
            raise ValueError("writer is closed")
        if len(pcm) == 0:
            return
        self._pending.append(np.ascontiguousarray(pcm, dtype=np.int16))
        self._pending_n += len(pcm)
        if self._pending_n >= self.block_samples:
            self.flush()

    def flush(self) -> None:
        """Escribe lo pendiente, actualiza la cabecera y fuerza a disco."""
        if self._closed:
            return
        if self._pending_n:
            data = (
                self._pending[0] if len(self._pending) == 1 else np.concatenate(self._pending)
            ).tobytes()
            self._f.write(data)
            self._flushed += self._pending_n
            self._pending = []
            self._pending_n = 0
            self._write_sizes()
        self._sync_file()

    def _write_sizes(self) -> None:
        data = self._flushed * BYTES_PER_SAMPLE
        end = self._f.tell()
        self._f.seek(4)
        self._f.write(struct.pack("<I", 36 + data))
        self._f.seek(40)
        self._f.write(struct.pack("<I", data))
        self._f.seek(end)

    def _sync_file(self) -> None:
        self._f.flush()
        if self._fsync:
            with contextlib.suppress(OSError):
                os.fsync(self._f.fileno())

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._closed = True
        self._f.close()


def repair_wav(path: Path, sample_rate: int = 16000) -> int:
    """Deja ``path`` como un WAV PCM16 mono valido y devuelve su numero de muestras.

    Las muestras se cuentan por el tamano real del fichero (un byte suelto final se recorta).
    Si la cabecera no es utilizable se reescribe con ``sample_rate``. Si el fichero no existe
    devuelve 0 sin crear nada.
    """
    if not path.exists():
        return 0
    size = path.stat().st_size
    rate = sample_rate
    if size >= HEADER_BYTES:
        with open(path, "rb") as f:
            head = f.read(HEADER_BYTES)
        if head[:4] == b"RIFF" and head[8:16] == b"WAVEfmt " and head[36:40] == b"data":
            (parsed,) = struct.unpack("<I", head[24:28])
            if parsed > 0:
                rate = parsed
    n = max(size - HEADER_BYTES, 0) // BYTES_PER_SAMPLE
    with _open_retry(path, "r+b") as f:
        f.seek(0)
        f.write(build_header(n, rate))
        f.truncate(HEADER_BYTES + n * BYTES_PER_SAMPLE)
        f.flush()
        with contextlib.suppress(OSError):
            os.fsync(f.fileno())
    return n


def _open_retry(path: Path, mode: str, attempts: int = 20) -> BinaryIO:
    """``open`` con reintentos: Windows tarda unos ms en soltar los ficheros de un proceso
    recien muerto (``PermissionError`` por violacion de uso compartido)."""
    for attempt in range(attempts):
        try:
            return open(path, mode)  # type: ignore[return-value]
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)
    raise AssertionError("unreachable")  # pragma: no cover


def wav_samples(path: Path) -> int:
    """Muestras segun la cabecera (``-1`` si no existe o no es un WAV)."""
    try:
        with sf.SoundFile(str(path)) as f:
            return int(f.frames)
    except (OSError, RuntimeError, sf.LibsndfileError):
        return -1


def append_zeros(path: Path, n: int) -> None:
    """Anade ``n`` muestras de silencio a un WAV ya reparado y actualiza la cabecera."""
    if n <= 0:
        return
    current = repair_wav(path)
    with _open_retry(path, "r+b") as f:
        rate = struct.unpack("<I", (f.seek(24), f.read(4))[1])[0]
        f.seek(0, os.SEEK_END)
        remaining = n
        while remaining:
            step = min(remaining, _BLOCK)
            f.write(bytes(step * BYTES_PER_SAMPLE))
            remaining -= step
        f.seek(0)
        f.write(build_header(current + n, rate))
        f.flush()
        with contextlib.suppress(OSError):
            os.fsync(f.fileno())


def truncate_wav(path: Path, n: int) -> int:
    """Recorta un WAV PCM16 mono (ya reparado) a ``n`` muestras como maximo. Devuelve cuantas
    quedan. Idempotente: un fichero ya mas corto no cambia."""
    current = repair_wav(path)
    if n >= current:
        return current
    n = max(0, n)
    with _open_retry(path, "r+b") as f:
        rate = struct.unpack("<I", (f.seek(24), f.read(4))[1])[0]
        f.seek(0)
        f.write(build_header(n, rate))
        f.truncate(HEADER_BYTES + n * BYTES_PER_SAMPLE)
        f.flush()
        with contextlib.suppress(OSError):
            os.fsync(f.fileno())
    return n


def read_wav_float(path: Path) -> tuple[NDArray[np.float32], int]:
    """Lee un WAV/FLAC mono entero como ``float32`` (para pruebas y pipeline)."""
    data, rate = sf.read(str(path), dtype="float32", always_2d=False)
    return np.asarray(data, dtype=np.float32), int(rate)


def flac_compression(level: int) -> float:
    """Nivel FLAC 0-8 -> ``compression_level`` de soundfile (0-1; libsndfile usa ``round(8 x)``)."""
    return max(0, min(8, level)) / 8.0


def wav_to_flac(wav: Path, flac: Path, *, level: int = 5, delete_wav: bool = True) -> int:
    """Convierte ``wav`` en ``flac`` con verificacion; devuelve el numero de muestras.

    1. Escribe ``<flac>.tmp`` por bloques (memoria acotada).
    2. Relee el FLAC y lo compara bit a bit con el WAV.
    3. ``os.replace`` al nombre final y, solo entonces, borra el WAV.

    Lanza ``ValueError`` (y deja el WAV intacto) si la verificacion falla. Un WAV sin muestras
    no se convierte (libsndfile no escribe un FLAC vacio valido): se devuelve 0 y el WAV se
    queda donde esta.
    """
    if wav_samples(wav) == 0:
        return 0
    tmp = flac.with_name(flac.name + ".tmp")
    total = 0
    try:
        with (
            sf.SoundFile(str(wav), "r") as src,
            sf.SoundFile(
                str(tmp),
                "w",
                samplerate=src.samplerate,
                channels=1,
                subtype="PCM_16",
                format="FLAC",
                compression_level=flac_compression(level),
            ) as dst,
        ):
            for block in src.blocks(blocksize=_BLOCK, dtype="int16", always_2d=False):
                dst.write(block)
                total += len(block)
        _verify_flac(wav, tmp, total)
        os.replace(tmp, flac)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    if delete_wav:
        wav.unlink()
    return total


def _verify_flac(wav: Path, flac: Path, expected: int) -> None:
    with sf.SoundFile(str(wav), "r") as a, sf.SoundFile(str(flac), "r") as b:
        if b.frames != expected or a.frames != expected or b.samplerate != a.samplerate:
            raise ValueError(f"FLAC verification failed for {wav.name}: frame/rate mismatch")
        for blk_a, blk_b in zip(
            a.blocks(blocksize=_BLOCK, dtype="int16"),
            b.blocks(blocksize=_BLOCK, dtype="int16"),
            strict=True,
        ):
            if not np.array_equal(blk_a, blk_b):
                raise ValueError(f"FLAC verification failed for {wav.name}: samples differ")
