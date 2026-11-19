"""Huellas de voz (MIC-11, spec secciones 3 y 13). Desactivadas por defecto (biometria, RGPD).

Cada persona se guarda en un fichero propio de ``paths.voiceprints_dir()`` cifrado con DPAPI de
Windows (``CryptProtectData``: ligado a la cuenta del usuario). El **nombre va dentro** de la carga
cifrada y el nombre del fichero es un hash, de modo que la carpeta no revela quienes estan
guardados. Cada carga guarda el motor que produjo los embeddings y su dimension; ``match`` ignora
los que no coinciden con el vector consultado.

* ``save(name, embedding)``: anade el embedding a la persona (maximo ``MAX_EMBEDDINGS``).
* ``match(embedding)``: ``(nombre, similitud coseno)`` de la mejor persona si supera el umbral
  (``privacy.voiceprint_threshold`` por defecto), o ``None``.
* ``delete(name)`` / ``list()`` / ``delete_all()``.

Ademas, ``save_meeting_embeddings`` / ``load_meeting_embeddings`` guardan (cifrados) los embeddings
de los hablantes de una reunion dentro de su carpeta, para poder aprender la huella cuando el
usuario renombre a un hablante (``rename_speaker(..., learn_voiceprint=True)``). Solo se escriben
si las huellas estan activadas.

``write_protected_json`` / ``read_protected_json`` sirven para cualquier otro fichero con vectores
de voz (p. ej. ``work/07_embeddings.dpapi`` del pipeline): nunca llegan al disco en claro.
"""

from __future__ import annotations

import builtins
import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from micnaga import paths

MAX_EMBEDDINGS = 10
SUFFIX = ".vp"
MEETING_EMBEDDINGS_FILE = "speaker_embeddings.dpapi"
_ENTROPY = b"micnaga-voiceprint-v1"
_DESCRIPTION = "micnaga voiceprint"
_FORMAT = 1


class VoiceprintError(RuntimeError):
    """No se pudo cifrar/descifrar (DPAPI) o el fichero esta corrupto."""


def _protect(data: bytes) -> bytes:
    import win32crypt

    try:
        blob = win32crypt.CryptProtectData(data, _DESCRIPTION, _ENTROPY, None, None, 0)
    except Exception as exc:  # pywintypes.error
        raise VoiceprintError(f"DPAPI encryption failed: {exc}") from exc
    return bytes(blob)


def _unprotect(blob: bytes) -> bytes:
    import win32crypt

    try:
        _desc, data = win32crypt.CryptUnprotectData(blob, _ENTROPY, None, None, 0)
    except Exception as exc:
        raise VoiceprintError(f"DPAPI decryption failed: {exc}") from exc
    return bytes(data)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".vp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_protected_json(path: Path, obj: Any) -> None:
    """Escribe ``obj`` como JSON cifrado con DPAPI, de forma atomica.

    El cifrado se hace en memoria antes de tocar el disco: ni el fichero final ni el temporal
    contienen nunca el JSON en claro. Lanza ``VoiceprintError`` si DPAPI falla (y entonces no
    escribe nada).
    """
    _atomic_write(path, _protect(json.dumps(obj).encode("utf-8")))


def read_protected_json(path: Path) -> Any:
    """Lee un fichero escrito con ``write_protected_json``.

    Lanza ``VoiceprintError`` si no se puede descifrar (otra cuenta, corrupto) o no es JSON y
    ``OSError`` si no se puede leer.
    """
    try:
        return json.loads(_unprotect(path.read_bytes()).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise VoiceprintError(f"corrupt protected file: {exc}") from exc


def _unit(vec: NDArray[np.float32] | Any) -> NDArray[np.float32]:
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(v))
    if norm == 0.0 or not np.isfinite(norm):
        raise ValueError("embedding must be a finite, non-zero vector")
    return np.asarray(v / norm, dtype=np.float32)


def cosine(a: NDArray[np.float32], b: NDArray[np.float32]) -> float:
    """Similitud coseno entre dos vectores (cualquier escala)."""
    return float(np.dot(_unit(a), _unit(b)))


def _key(name: str) -> str:
    return hashlib.sha256(name.strip().casefold().encode("utf-8")).hexdigest()[:32]


def _pack(name: str, engine: str, embeddings: builtins.list[NDArray[np.float32]]) -> bytes:
    payload: dict[str, Any] = {
        "format": _FORMAT,
        "name": name,
        "engine": engine,
        "dim": int(embeddings[0].shape[0]),
        "embeddings": [e.astype(float).tolist() for e in embeddings],
    }
    return _protect(json.dumps(payload).encode("utf-8"))


def _unpack(blob: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(_unprotect(blob).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise VoiceprintError(f"corrupt voiceprint: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("format") != _FORMAT:
        raise VoiceprintError("unsupported voiceprint format")
    return payload


class VoiceprintStore:
    def __init__(self, directory: Path | None = None, *, threshold: float | None = None) -> None:
        self._directory = directory
        self._threshold = threshold

    @property
    def directory(self) -> Path:
        return self._directory or paths.voiceprints_dir()

    def _threshold_value(self, override: float | None) -> float:
        if override is not None:
            return override
        if self._threshold is not None:
            return self._threshold
        from micnaga.config import load_config

        return load_config().privacy.voiceprint_threshold

    def _file(self, name: str) -> Path:
        return self.directory / f"{_key(name)}{SUFFIX}"

    def save(self, name: str, embedding: NDArray[np.float32], *, engine: str = "") -> None:
        """Anade ``embedding`` a la huella de ``name`` (conserva los ultimos ``MAX_EMBEDDINGS``)."""
        name = name.strip()
        if not name:
            raise ValueError("name must not be empty")
        vec = _unit(embedding)
        existing: builtins.list[NDArray[np.float32]] = []
        file = self._file(name)
        if file.is_file():
            payload = _unpack(file.read_bytes())
            if int(payload["dim"]) == vec.shape[0] and payload.get("engine", "") == engine:
                existing = [np.asarray(e, dtype=np.float32) for e in payload["embeddings"]]
        existing.append(vec)
        _atomic_write(file, _pack(name, engine, existing[-MAX_EMBEDDINGS:]))

    def _load_all(self) -> builtins.list[dict[str, Any]]:
        out: builtins.list[dict[str, Any]] = []
        if not self.directory.is_dir():
            return out
        for file in sorted(self.directory.glob(f"*{SUFFIX}")):
            try:
                out.append(_unpack(file.read_bytes()))
            except (VoiceprintError, OSError):
                continue  # corrupta o de otra cuenta: se ignora
        return out

    def match(
        self,
        embedding: NDArray[np.float32],
        *,
        threshold: float | None = None,
        engine: str | None = None,
    ) -> tuple[str, float] | None:
        """Mejor persona por similitud coseno (maximo sobre sus embeddings) si >= umbral."""
        vec = _unit(embedding)
        limit = self._threshold_value(threshold)
        best: tuple[str, float] | None = None
        for payload in self._load_all():
            if int(payload["dim"]) != vec.shape[0]:
                continue
            if engine is not None and payload.get("engine", "") != engine:
                continue
            score = max(float(np.dot(vec, _unit(np.asarray(e)))) for e in payload["embeddings"])
            if best is None or score > best[1]:
                best = (str(payload["name"]), score)
        if best is not None and best[1] >= limit:
            return best
        return None

    def delete(self, name: str) -> bool:
        file = self._file(name)
        if file.is_file():
            file.unlink()
            return True
        return False

    def delete_all(self) -> int:
        n = 0
        if self.directory.is_dir():
            for file in self.directory.glob(f"*{SUFFIX}"):
                file.unlink(missing_ok=True)
                n += 1
        return n

    def list(self) -> builtins.list[str]:
        """Nombres guardados (ordenados)."""
        return sorted(str(p["name"]) for p in self._load_all())


# ----------------------------------------------------------------------------- por reunion


def save_meeting_embeddings(
    folder: Path, embeddings: dict[str, NDArray[np.float32]], *, engine: str = ""
) -> Path:
    """Guarda (cifrados con DPAPI) los embeddings por hablante de una reunion."""
    payload: dict[str, Any] = {
        "format": _FORMAT,
        "engine": engine,
        "speakers": {
            k: np.asarray(v, dtype=np.float32).astype(float).tolist() for k, v in embeddings.items()
        },
    }
    target = folder / MEETING_EMBEDDINGS_FILE
    _atomic_write(target, _protect(json.dumps(payload).encode("utf-8")))
    return target


def load_meeting_embeddings(folder: Path) -> tuple[dict[str, NDArray[np.float32]], str]:
    """``({speaker_id: embedding}, motor)``; vacio si no hay fichero."""
    file = folder / MEETING_EMBEDDINGS_FILE
    if not file.is_file():
        return {}, ""
    try:
        payload = json.loads(_unprotect(file.read_bytes()).decode("utf-8"))
    except (ValueError, VoiceprintError):
        return {}, ""
    speakers = {
        str(k): np.asarray(v, dtype=np.float32)
        for k, v in dict(payload.get("speakers", {})).items()
    }
    return speakers, str(payload.get("engine", ""))


def delete_meeting_embeddings(folder: Path) -> None:
    with contextlib.suppress(OSError):
        (folder / MEETING_EMBEDDINGS_FILE).unlink(missing_ok=True)
