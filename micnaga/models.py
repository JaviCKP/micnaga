"""Manifiesto de modelos y descarga del primer arranque (spec seccion 11, principio 1).

Es **la unica** parte de Micnaga que toca la red. Reglas:

* Cada fichero tiene una URL fija (con el *commit* de Hugging Face fijado, nunca ``main``), un
  tamano y un SHA-256 **calculados sobre lo realmente descargado** (los ``lfs.oid`` publicados por
  Hugging Face coinciden con los hashes de abajo para los ficheros grandes).
* Solo se permiten URL ``https`` hacia ``huggingface.co`` y sus CDN (``*.hf.co``); una redireccion
  a cualquier otro host aborta la descarga.
* La descarga va a ``<fichero>.part`` (se reanuda con ``Range``), se verifica el SHA-256 y solo
  entonces se hace ``os.replace`` atomico al nombre final. Un fichero final corrupto nunca existe.
* Los modelos viven en ``paths.models_dir()`` (``MICNAGA_MODELS`` > ``[storage] models_dir`` >
  ``<datos de app>/models``), un subdirectorio por grupo (``ModelGroup.id``).

Grupos:

``faster-whisper-large-v3-turbo``  ASR por defecto (faster-whisper, DECISIONS §32) y opcion de
                               «Mejorar transcripcion»; MIT.
``parakeet-tdt-0.6b-v3-int8``  ASR alternativo, mas rapido y peor en espanol (onnx-asr), CC-BY-4.0.
``silero-vad``                 VAD (ONNX directo con onnxruntime), MIT.
``wespeaker``                  Embeddings de hablante que usa la libreria ``diarize`` (y las huellas
                               de voz). ``diarize`` los descarga solo de un servidor chino
                               (``wespeaker-...cos.ap-shanghai.myqcloud.com``) a ``~/.wespeaker``;
                               el adaptador ``diar/diarize.py`` lo parchea para leer de aqui
                               (fichero verificado byte a byte identico), asi no hay red oculta.
``faster-whisper-small``       ASR alternativo (opcional; no se descarga salvo que se pida).
``faster-whisper-large-v3``    «Mejorar transcripcion» (solo se descarga al primer uso).

``CANDIDATE_MANIFEST`` (aparte, nunca lo descarga la app): segmentacion pyannote 3.0 y cuatro
modelos de embeddings de sherpa-onnx evaluados para la diarizacion y no adoptados.

``faster-whisper-medium`` NO esta en el manifiesto: pesa ~1,5 GB y no estaba autorizado
descargarlo, y esta prohibido inventar hashes. Anadirlo = descargar, calcular SHA-256 y una entrada.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import logging
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from micnaga import paths
from micnaga.config import Config

log = logging.getLogger(__name__)

CHUNK = 1 << 20
USER_AGENT = "micnaga-model-downloader"
ALLOWED_HOSTS: tuple[str, ...] = ("huggingface.co", "hf.co")
"""Hosts permitidos y sus subdominios (incluida cualquier redireccion). Nada mas."""

PARAKEET_GROUP = "parakeet-tdt-0.6b-v3-int8"
SILERO_GROUP = "silero-vad"
WESPEAKER_GROUP = "wespeaker"
WHISPER_SMALL_GROUP = "faster-whisper-small"
WHISPER_LARGE_V3_GROUP = "faster-whisper-large-v3"
WHISPER_TURBO_GROUP = "faster-whisper-large-v3-turbo"
PYANNOTE_SEG_GROUP = "pyannote-segmentation-3.0"
SHERPA_WESPEAKER_GROUP = "sherpa-wespeaker-resnet34-lm"
SHERPA_CAMPPLUS_GROUP = "sherpa-3dspeaker-campplus-en"
SHERPA_ERES2NET_GROUP = "sherpa-3dspeaker-eres2net-en"
SHERPA_TITANET_GROUP = "sherpa-nemo-titanet-small"

_HF = "https://huggingface.co"
_PARAKEET_REV = "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce"
_SILERO_REV = "b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6"
_WESPEAKER_REV = "f0c48c298fd835726c27956a5d617bad7115627e"
_WHISPER_SMALL_REV = "536b0662742c02347bc0e980a01041f333bce120"
_WHISPER_LARGE_V3_REV = "edaa852ec7e145841d8ffdb056a99866b5f0a478"
_WHISPER_TURBO_REV = "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
_PYANNOTE_SEG_REV = "9403a6902bb58e3d5ae8c7e77c3422de279db2e0"
_SHERPA_EMB_REV = "3b5eb92b942ace929e479b6987108d291438547f"
_SHERPA_EMB_BASE = f"{_HF}/csukuangfj/speaker-embedding-models/resolve/{_SHERPA_EMB_REV}"


class ModelError(RuntimeError):
    """Error de descarga o verificacion de modelos.

    El texto (ingles) va al log; ``key`` es la clave i18n (``models.err.*``) que la UI ensena en
    castellano y ``info`` sus variables (p. ej. ``need``/``free`` en bytes para el disco)."""

    def __init__(self, message: str, key: str = "models.err.generic", **info: Any) -> None:
        super().__init__(message)
        self.key = key
        self.info = info


class DownloadCancelled(ModelError):
    """El usuario cancelo la descarga (los ``.part`` se conservan para reanudar)."""

    def __init__(self, message: str = "download cancelled") -> None:
        super().__init__(message, "models.err.cancelled")


@dataclass(frozen=True)
class ModelFile:
    """Un fichero del manifiesto."""

    group: str
    name: str
    url: str
    size: int
    sha256: str

    @property
    def rel_path(self) -> str:
        return f"{self.group}/{self.name}"

    def path(self, config: Config | None = None) -> Path:
        return paths.models_dir(config) / self.group / self.name


@dataclass(frozen=True)
class ModelGroup:
    """Conjunto de ficheros que forman un modelo."""

    id: str
    title: str
    license: str
    source: str
    files: tuple[ModelFile, ...]

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    def dir(self, config: Config | None = None) -> Path:
        return paths.models_dir(config) / self.id


def _files(group: str, base: str, entries: Sequence[tuple[str, int, str]]) -> tuple[ModelFile, ...]:
    return tuple(
        ModelFile(group=group, name=n, url=f"{base}/{n}", size=s, sha256=h) for n, s, h in entries
    )


MANIFEST: dict[str, ModelGroup] = {
    PARAKEET_GROUP: ModelGroup(
        id=PARAKEET_GROUP,
        title="Parakeet TDT 0.6B v3 (int8, ONNX)",
        license="CC-BY-4.0 (NVIDIA; ONNX de istupakov)",
        source=f"{_HF}/istupakov/parakeet-tdt-0.6b-v3-onnx",
        files=_files(
            PARAKEET_GROUP,
            f"{_HF}/istupakov/parakeet-tdt-0.6b-v3-onnx/resolve/{_PARAKEET_REV}",
            [
                (
                    "config.json",
                    97,
                    "666903c76b9798caf2c210afd4f6cd60b08a8dbf9800ec8d7a3bc0d2148ac466",
                ),
                (
                    "vocab.txt",
                    93939,
                    "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d",
                ),
                (
                    "decoder_joint-model.int8.onnx",
                    18202004,
                    "eea7483ee3d1a30375daedc8ed83e3960c91b098812127a0d99d1c8977667a70",
                ),
                (
                    "encoder-model.int8.onnx",
                    652183999,
                    "6139d2fa7e1b086097b277c7149725edbab89cc7c7ae64b23c741be4055aff09",
                ),
            ],
        ),
    ),
    SILERO_GROUP: ModelGroup(
        id=SILERO_GROUP,
        title="Silero VAD (ONNX)",
        license="MIT (snakers4/silero-vad; ONNX de istupakov)",
        source=f"{_HF}/istupakov/silero-vad-onnx",
        files=_files(
            SILERO_GROUP,
            f"{_HF}/istupakov/silero-vad-onnx/resolve/{_SILERO_REV}",
            [
                (
                    "silero_vad.onnx",
                    2327524,
                    "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
                ),
            ],
        ),
    ),
    WESPEAKER_GROUP: ModelGroup(
        id=WESPEAKER_GROUP,
        title="WeSpeaker ResNet34-LM (VoxCeleb, ONNX): embeddings de hablante",
        license="CC-BY-4.0 segun la ficha de Hugging Face (codigo WeSpeaker: Apache-2.0)",
        source=f"{_HF}/Wespeaker/wespeaker-voxceleb-resnet34-LM",
        files=_files(
            WESPEAKER_GROUP,
            f"{_HF}/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/{_WESPEAKER_REV}",
            [
                (
                    "voxceleb_resnet34_LM.onnx",
                    26530309,
                    "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068",
                ),
            ],
        ),
    ),
    WHISPER_SMALL_GROUP: ModelGroup(
        id=WHISPER_SMALL_GROUP,
        title="faster-whisper small (CTranslate2, fp16 -> int8 al cargar)",
        license="MIT (OpenAI Whisper; conversion de Systran)",
        source=f"{_HF}/Systran/faster-whisper-small",
        files=_files(
            WHISPER_SMALL_GROUP,
            f"{_HF}/Systran/faster-whisper-small/resolve/{_WHISPER_SMALL_REV}",
            [
                (
                    "config.json",
                    2370,
                    "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828",
                ),
                (
                    "tokenizer.json",
                    2203239,
                    "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
                ),
                (
                    "vocabulary.txt",
                    459861,
                    "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913",
                ),
                (
                    "model.bin",
                    483546902,
                    "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
                ),
            ],
        ),
    ),
    WHISPER_LARGE_V3_GROUP: ModelGroup(
        id=WHISPER_LARGE_V3_GROUP,
        title="faster-whisper large-v3 (CTranslate2, fp16 -> int8 al cargar)",
        license="MIT (OpenAI Whisper; conversion de Systran)",
        source=f"{_HF}/Systran/faster-whisper-large-v3",
        files=_files(
            WHISPER_LARGE_V3_GROUP,
            f"{_HF}/Systran/faster-whisper-large-v3/resolve/{_WHISPER_LARGE_V3_REV}",
            [
                (
                    "config.json",
                    2394,
                    "a9306624f5ec14270a014b647e5c316b6e03a662c369758d1b90697a7b0655b9",
                ),
                (
                    "preprocessor_config.json",
                    340,
                    "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711",
                ),
                (
                    "tokenizer.json",
                    2480617,
                    "6d8cbd7cd0d8d5815e478dac67b85a26bbe77c1f5e0c6d76d1ce2abc0e5f21ca",
                ),
                (
                    "vocabulary.json",
                    1068114,
                    "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1",
                ),
                (
                    "model.bin",
                    3087284237,
                    "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1",
                ),
            ],
        ),
    ),
    WHISPER_TURBO_GROUP: ModelGroup(
        id=WHISPER_TURBO_GROUP,
        title="faster-whisper large-v3-turbo (CTranslate2, fp16 -> int8 al cargar)",
        license="MIT (OpenAI Whisper; conversion de mobiuslabsgmbh)",
        source=f"{_HF}/mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        files=_files(
            WHISPER_TURBO_GROUP,
            f"{_HF}/mobiuslabsgmbh/faster-whisper-large-v3-turbo/resolve/{_WHISPER_TURBO_REV}",
            [
                (
                    "config.json",
                    2263,
                    "b0253ea6c0d3bea6b1e19e91a02acfd3b53f4467362efcb5a3e6b16c9b3a9b7e",
                ),
                (
                    "preprocessor_config.json",
                    340,
                    "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711",
                ),
                (
                    "tokenizer.json",
                    2710337,
                    "297b13372ac43916285644fb9687add3cc62ee2a1adb60da3dc25cc94c1871fd",
                ),
                (
                    "vocabulary.json",
                    1068114,
                    "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1",
                ),
                (
                    "model.bin",
                    1617884929,
                    "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da",
                ),
            ],
        ),
    ),
}


def _sherpa_embedding(
    gid: str, title: str, license_: str, entry: tuple[str, int, str]
) -> ModelGroup:
    return ModelGroup(
        id=gid,
        title=title,
        license=license_,
        source=f"{_HF}/csukuangfj/speaker-embedding-models",
        files=_files(gid, _SHERPA_EMB_BASE, [entry]),
    )


CANDIDATE_MANIFEST: dict[str, ModelGroup] = {
    PYANNOTE_SEG_GROUP: ModelGroup(
        id=PYANNOTE_SEG_GROUP,
        title="pyannote segmentation-3.0 (ONNX de sherpa-onnx)",
        license="MIT (pyannote, CNRS; exportacion ONNX de csukuangfj)",
        source=f"{_HF}/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0",
        files=_files(
            PYANNOTE_SEG_GROUP,
            f"{_HF}/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0/resolve/{_PYANNOTE_SEG_REV}",
            [
                (
                    "model.onnx",
                    5992913,
                    "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079",
                ),
            ],
        ),
    ),
    SHERPA_WESPEAKER_GROUP: _sherpa_embedding(
        SHERPA_WESPEAKER_GROUP,
        "WeSpeaker ResNet34-LM (VoxCeleb, ONNX con metadatos de sherpa-onnx)",
        "CC-BY-4.0 (WeSpeaker)",
        (
            "wespeaker_en_voxceleb_resnet34_LM.onnx",
            26530550,
            "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012",
        ),
    ),
    SHERPA_CAMPPLUS_GROUP: _sherpa_embedding(
        SHERPA_CAMPPLUS_GROUP,
        "3D-Speaker CAM++ (VoxCeleb, ONNX de sherpa-onnx)",
        "Apache-2.0 (3D-Speaker, Alibaba DAMO)",
        (
            "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx",
            29596979,
            "98daf9bcf1c8abf3db374fec4c27a5eda7c58705bb3f180d057b085a72a88bb5",
        ),
    ),
    SHERPA_ERES2NET_GROUP: _sherpa_embedding(
        SHERPA_ERES2NET_GROUP,
        "3D-Speaker ERes2Net (VoxCeleb, ONNX de sherpa-onnx)",
        "Apache-2.0 (3D-Speaker, Alibaba DAMO)",
        (
            "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx",
            26485264,
            "c4947caf150b8502fdd75bbe4089b971ab1ec42e90ee3ba547faca2cde44d9ee",
        ),
    ),
    SHERPA_TITANET_GROUP: _sherpa_embedding(
        SHERPA_TITANET_GROUP,
        "NVIDIA NeMo TitaNet small (ONNX de sherpa-onnx)",
        "CC-BY-4.0 (NVIDIA)",
        (
            "nemo_en_titanet_small.onnx",
            40257283,
            "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e",
        ),
    ),
}
"""Modelos evaluados para la diarizacion y NO adoptados (``tests/engines/diar_compare.py``,
``docs/diarization_onnx.md``): URL fijada, tamano y SHA-256 igual que en ``MANIFEST``, pero la
app nunca los descarga. Solo para repetir la comparativa (``download_models(manifest=...)``)."""

OPTIONAL_GROUPS: tuple[str, ...] = (WHISPER_SMALL_GROUP,)
"""Grupos que no se descargan en el asistente salvo que la configuracion los pida."""
HQ_GROUPS: tuple[str, ...] = (WHISPER_LARGE_V3_GROUP, WHISPER_TURBO_GROUP)
"""Modelos de «Mejorar transcripcion»: se descargan al primer uso. ``large-v3-turbo`` es ademas
el ASR por defecto, asi que ``required_groups`` lo pide y el asistente lo baja."""


def whisper_group(model: str) -> str:
    """Id de grupo para ``processing.whisper_model`` (small, large-v3-turbo o large-v3)."""
    group = f"faster-whisper-{model}"
    if group not in MANIFEST:
        raise ModelError(
            f"whisper_model {model!r} is not in the models manifest "
            f"(available: {[g for g in MANIFEST if g.startswith('faster-whisper-')]})"
        )
    return group


def group_dir(group: str, config: Config | None = None) -> Path:
    """Carpeta de un grupo dentro de ``paths.models_dir``."""
    return paths.models_dir(config) / group


def required_groups(config: Config, *, include_optional: bool = False) -> list[str]:
    """Grupos que necesita la configuracion actual (VAD siempre)."""
    groups = [SILERO_GROUP]
    if config.processing.asr_engine == "parakeet":
        groups.append(PARAKEET_GROUP)
    elif config.processing.asr_engine == "whisper":
        groups.append(f"faster-whisper-{config.processing.whisper_model}")
    if config.processing.diarization == "diarize":
        groups.append(WESPEAKER_GROUP)
    if include_optional:
        groups.extend(g for g in OPTIONAL_GROUPS if g not in groups)
    return groups


def _resolve_files(
    config: Config,
    groups: Iterable[str] | None,
    include_optional: bool,
    manifest: dict[str, ModelGroup],
) -> list[ModelFile]:
    wanted = list(groups) if groups is not None else required_groups(config)
    if groups is None and include_optional:
        wanted += [g for g in OPTIONAL_GROUPS if g not in wanted]
    out: list[ModelFile] = []
    for gid in wanted:
        if gid not in manifest:
            raise ModelError(f"unknown model group {gid!r}; known: {sorted(manifest)}")
        out.extend(manifest[gid].files)
    return out


def missing_models(
    config: Config,
    *,
    groups: Iterable[str] | None = None,
    include_optional: bool = False,
    manifest: dict[str, ModelGroup] | None = None,
) -> list[ModelFile]:
    """Ficheros que faltan o tienen un tamano distinto del esperado (comprobacion rapida)."""
    base = paths.models_dir(config)
    out: list[ModelFile] = []
    for f in _resolve_files(config, groups, include_optional, manifest or MANIFEST):
        p = base / f.group / f.name
        try:
            ok = p.is_file() and p.stat().st_size == f.size
        except OSError:
            ok = False
        if not ok:
            out.append(f)
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def verify_models(
    config: Config,
    *,
    groups: Iterable[str] | None = None,
    include_optional: bool = False,
    manifest: dict[str, ModelGroup] | None = None,
) -> list[ModelFile]:
    """Comprobacion completa (tamano + SHA-256). Devuelve los ficheros que NO pasan."""
    base = paths.models_dir(config)
    bad: list[ModelFile] = []
    for f in _resolve_files(config, groups, include_optional, manifest or MANIFEST):
        p = base / f.group / f.name
        try:
            ok = p.is_file() and p.stat().st_size == f.size and sha256_file(p) == f.sha256
        except OSError:
            ok = False
        if not ok:
            bad.append(f)
    return bad


# ------------------------------------------------------------------------------ descarga


@dataclass(frozen=True)
class DownloadProgress:
    """Estado para la barra de progreso del asistente."""

    file: str
    """``grupo/nombre`` del fichero en curso."""
    file_done: int
    file_total: int
    overall_done: int
    overall_total: int
    phase: str = "download"
    """``download`` | ``verify`` | ``done`` | ``skip``."""


ProgressCallback = Callable[[DownloadProgress], None]


def _host_allowed(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return False
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


class _StrictRedirects(urllib.request.HTTPRedirectHandler):
    """Sigue redirecciones (CDN de Hugging Face) solo hacia hosts permitidos."""

    def __init__(self, check: Callable[[str], bool]) -> None:
        self._check = check

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> urllib.request.Request | None:
        if not self._check(newurl):
            raise ModelError(
                f"redirect to a disallowed host/scheme blocked: {newurl[:80]}",
                "models.err.not_allowed",
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener(check: Callable[[str], bool]) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_StrictRedirects(check))


RETRY_BACKOFF_S: tuple[float, ...] = (2.0, 5.0, 15.0, 30.0)
"""Esperas entre reintentos de red de un mismo fichero (el ``.part`` se conserva: reanuda)."""
DISK_MARGIN = 200 << 20
"""Espacio libre que se deja ademas de lo que falta por bajar."""
_RETRY_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})


class _NetworkCut(Exception):
    """Fallo de red reintentable (interno de ``_download_one``)."""


def _announced_size(resp: Any, status: int) -> int | None:
    """Tamano total del fichero segun las cabeceras (``Content-Range`` en un 206,
    ``Content-Length`` en un 200), o ``None`` si no lo dicen."""
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        if status == 206:
            rng = headers.get("Content-Range") or ""
            total = rng.rsplit("/", 1)[1] if "/" in rng else "*"
            return None if total.strip() == "*" else int(total)
        length = headers.get("Content-Length")
        return None if length is None else int(length)
    except (TypeError, ValueError):
        return None


def _stream_to_part(
    resp: Any,
    part: Path,
    have: int,
    total: int,
    on_bytes: Callable[[int, int], None],
    cancel: threading.Event | None = None,
) -> None:
    status = getattr(resp, "status", 200)
    if have and status != 206:  # el servidor ignoro Range: empezar de cero
        have = 0
    announced = _announced_size(resp, status)
    if announced is not None and announced != total:  # otro fichero: reintentar no arregla nada
        raise ModelError(
            f"size mismatch: server announces {announced} bytes, expected {total}",
            "models.err.checksum",
        )
    got = have
    on_bytes(got, total)
    with part.open("ab" if have else "wb") as fh:
        while True:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled()
            try:
                block = resp.read(CHUNK)
            except (OSError, http.client.HTTPException) as exc:  # corte: reintentable
                raise _NetworkCut(str(exc) or type(exc).__name__) from exc
            if not block:
                break
            if got + len(block) > total:
                raise ModelError(f"server sent more than {total} bytes", "models.err.checksum")
            try:
                fh.write(block)
            except OSError as exc:  # disco lleno, permisos: no se arregla reintentando
                raise ModelError(f"cannot write {part}: {exc}", "models.err.write") from exc
            got += len(block)
            on_bytes(got, total)
        fh.flush()
        os.fsync(fh.fileno())
    if got < total:
        raise _NetworkCut(f"connection closed at {got}/{total} bytes")


def _download_one(
    f: ModelFile,
    dest: Path,
    opener: urllib.request.OpenerDirector,
    check_url: Callable[[str], bool],
    on_bytes: Callable[[int, int], None],
    *,
    on_verify: Callable[[], None] | None = None,
    cancel: threading.Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Descarga ``f`` a ``dest`` (via ``.part``, reanudable), verifica SHA-256 y renombra.

    Cortes de red, *timeouts* y HTTP 5xx/429 se reintentan con espera creciente
    (``RETRY_BACKOFF_S``), reanudando con ``Range`` desde lo ya escrito. Un ``.part`` que no
    verifica se borra y se baja una vez mas desde cero."""
    if not check_url(f.url):
        raise ModelError(f"URL not allowed for {f.rel_path}: {f.url}", "models.err.not_allowed")
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    corrupt = 0
    cuts = 0
    while True:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        have = part.stat().st_size if part.exists() else 0
        if have > f.size:
            part.unlink()
            have = 0
        if have < f.size:
            try:
                _fetch_into_part(f, part, have, opener, on_bytes, cancel)
            except _NetworkCut as exc:
                if cuts >= len(RETRY_BACKOFF_S):
                    raise ModelError(
                        f"network error downloading {f.rel_path}: {exc}", "models.err.network"
                    ) from exc
                wait = RETRY_BACKOFF_S[cuts]
                cuts += 1
                log.info("model %s: %s; retry %d in %.0f s", f.rel_path, exc, cuts, wait)
                sleep(wait)
                continue
        if on_verify is not None:
            on_verify()
        if part.stat().st_size == f.size and sha256_file(part) == f.sha256:
            os.replace(part, dest)
            return
        part.unlink(missing_ok=True)  # corrupto: reintentar una vez desde cero
        corrupt += 1
        log.warning("model %s failed verification (attempt %d)", f.rel_path, corrupt)
        if corrupt >= 2:
            raise ModelError(
                f"SHA-256/size mismatch downloading {f.rel_path}; file discarded",
                "models.err.checksum",
            )


def _fetch_into_part(
    f: ModelFile,
    part: Path,
    have: int,
    opener: urllib.request.OpenerDirector,
    on_bytes: Callable[[int, int], None],
    cancel: threading.Event | None,
) -> None:
    headers = {"User-Agent": USER_AGENT}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(f.url, headers=headers)
    try:
        resp = opener.open(req, timeout=60)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:  # el .part ya estaba completo o es invalido
            part.unlink(missing_ok=True)
            raise _NetworkCut("HTTP 416; restarting from zero") from exc
        if exc.code in _RETRY_HTTP:
            raise _NetworkCut(f"HTTP {exc.code}") from exc
        raise ModelError(f"HTTP {exc.code} downloading {f.rel_path}", "models.err.http") from exc
    except urllib.error.URLError as exc:
        raise _NetworkCut(f"{exc.reason}") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise _NetworkCut(str(exc) or type(exc).__name__) from exc
    with resp:
        _stream_to_part(resp, part, have, f.size, on_bytes, cancel)


def _byte_cb(
    emit: Callable[[ModelFile, int, str], None], f: ModelFile
) -> Callable[[int, int], None]:
    def cb(got: int, _total: int) -> None:
        emit(f, got, "download")

    return cb


def _part_size(dest: Path) -> int:
    try:
        return dest.with_name(dest.name + ".part").stat().st_size
    except OSError:
        return 0


def check_disk_space(folder: Path, need: int, *, margin: int = DISK_MARGIN) -> None:
    """``ModelError`` (``models.err.disk``, con ``need``/``free``) si no caben ``need`` bytes."""
    probe = folder
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return  # no se sabe: que lo diga la escritura
    if free < need + margin:
        raise ModelError(
            f"not enough disk space in {folder}: need {need + margin} bytes, free {free}",
            "models.err.disk",
            need=need + margin,
            free=free,
        )


_DOWNLOAD_LOCK = threading.Lock()
"""Una descarga de modelos a la vez por proceso (asistente y Ajustes escribirian el mismo
``.part``): la segunda espera y luego solo verifica lo que ya bajo la primera."""


def download_in_progress() -> bool:
    return _DOWNLOAD_LOCK.locked()


def download_models(
    config: Config,
    progress_cb: ProgressCallback | None = None,
    *,
    groups: Iterable[str] | None = None,
    include_optional: bool = False,
    manifest: dict[str, ModelGroup] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
    allow_url: Callable[[str], bool] | None = None,
    cancel: threading.Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Path]:
    """Descarga los modelos que faltan (ver ``_download_models``), de uno en uno por proceso."""
    with _DOWNLOAD_LOCK:
        return _download_models(
            config,
            progress_cb,
            groups=groups,
            include_optional=include_optional,
            manifest=manifest,
            opener=opener,
            allow_url=allow_url,
            cancel=cancel,
            sleep=sleep,
        )


def _download_models(
    config: Config,
    progress_cb: ProgressCallback | None = None,
    *,
    groups: Iterable[str] | None = None,
    include_optional: bool = False,
    manifest: dict[str, ModelGroup] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
    allow_url: Callable[[str], bool] | None = None,
    cancel: threading.Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Path]:
    """Descarga los modelos que faltan o estan corruptos. Devuelve las rutas descargadas.

    ``manifest``, ``opener``, ``allow_url`` y ``sleep`` existen para los tests (servidor local);
    en produccion se usan los valores por defecto (solo ``https`` hacia Hugging Face).
    Los ficheros ya presentes se verifican con SHA-256 antes de saltarlos (fase ``verify``).
    Antes de bajar nada se comprueba que cabe en el disco. ``cancel`` (o una excepcion lanzada
    desde ``progress_cb``) para la descarga y deja los ``.part`` para reanudar luego, tambien
    tras reiniciar la app.
    """
    check = allow_url or _host_allowed
    op = opener or _default_opener(check)
    base = paths.models_dir(config)
    files = _resolve_files(config, groups, include_optional, manifest or MANIFEST)
    overall_total = sum(f.size for f in files)
    done_before = 0
    downloaded: list[Path] = []

    def emit(f: ModelFile, d: int, phase: str) -> None:
        if progress_cb is not None:
            progress_cb(
                DownloadProgress(f.rel_path, d, f.size, done_before + d, overall_total, phase)
            )

    def verifying(f: ModelFile) -> Callable[[], None]:
        return lambda: emit(f, f.size, "verify")

    need = 0
    for f in files:
        dest = base / f.group / f.name
        with contextlib.suppress(OSError):
            if dest.is_file() and dest.stat().st_size == f.size:
                continue
        need += max(0, f.size - _part_size(dest))
    if need:
        check_disk_space(base, need)

    for f in files:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        dest = base / f.group / f.name
        with contextlib.suppress(OSError):
            if dest.is_file() and dest.stat().st_size == f.size:
                emit(f, 0, "verify")
                if sha256_file(dest) == f.sha256:
                    emit(f, f.size, "skip")
                    done_before += f.size
                    continue
        if dest.exists():  # presente pero corrupto
            log.warning("model %s is corrupt; downloading again", f.rel_path)
            dest.unlink()
        _download_one(
            f,
            dest,
            op,
            check,
            _byte_cb(emit, f),
            on_verify=verifying(f),
            cancel=cancel,
            sleep=sleep,
        )
        emit(f, f.size, "done")
        downloaded.append(dest)
        done_before += f.size
    return downloaded


def total_size(
    config: Config, *, groups: Iterable[str] | None = None, include_optional: bool = False
) -> int:
    """Bytes totales de los grupos pedidos (para el texto «~1 GB» del asistente)."""
    return sum(f.size for f in _resolve_files(config, groups, include_optional, MANIFEST))


def require_models(config: Config, group: str) -> Path:
    """Carpeta de ``group`` si esta completa; si no, ``EngineUnavailableError`` con la solucion."""
    from micnaga.registry import EngineUnavailableError

    absent = missing_models(config, groups=[group])
    if absent:
        names = ", ".join(f.name for f in absent)
        raise EngineUnavailableError(
            f"model files missing or incomplete in {group_dir(group, config)}: {names}. "
            "Run the first-start model download (micnaga.models.download_models)."
        )
    return group_dir(group, config)
