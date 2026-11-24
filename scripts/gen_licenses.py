"""Genera ``THIRD_PARTY_LICENSES.md`` y comprueba que ninguna licencia impide distribuir en MIT.

Spec §11: «El agente genera THIRD_PARTY_LICENSES.md con la licencia de cada dependencia y modelo, y
bloquea cualquier dependencia con licencia incompatible con MIT.»

Que hace
--------
1. Calcula la **clausura de dependencias de ejecucion** de ``micnaga`` (requisitos del paquete mas los
   extras ``asr``, ``diar``, ``aec`` y ``ui``; sin ``dev``, ``bench`` ni ``pyannote``) sobre las
   distribuciones instaladas, evaluando los marcadores de entorno con ``packaging``.
2. Para cada distribucion lee los metadatos (``License-Expression``, ``License``, clasificadores) y,
   si no bastan, **el propio fichero de licencia** de la distribucion (huella del texto). Solo cuando
   la distribucion no trae nada utilizable se usa la tabla ``MANUAL_LICENSES`` (cada entrada indica
   su fuente). No se adivina: lo que no se puede determinar queda ``UNKNOWN`` y bloquea.
3. Clasifica y **bloquea** (codigo de salida 1): GPL, AGPL, UNKNOWN y cualquier LGPL que no figure en
   ``LGPL_REVIEWED`` con el modo de enlazado y las obligaciones revisados. Asi una dependencia LGPL
   nueva falla hasta que alguien la revise.
4. Escribe el Markdown (``--write``), con el resumen de obligaciones LGPL arriba, la tabla de
   dependencias, los componentes nativos, los MODELOS (texto fijo, mantenido a mano) y los textos de
   licencia completos.

Uso::

    python scripts/gen_licenses.py --check            # solo comprueba (CI / test); no escribe
    python scripts/gen_licenses.py --write            # regenera THIRD_PARTY_LICENSES.md
    python scripts/gen_licenses.py --check --dist dist/micnaga   # ademas escanea el instalador

``--dist`` busca en la carpeta congelada binarios que no se pueden distribuir (x264/x265 de FFmpeg,
cuDNN...). Salida 0 = todo en orden; 1 = hay bloqueos; 2 = error de uso.
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata as md
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "THIRD_PARTY_LICENSES.md"
ROOT_DIST = "micnaga"
RUNTIME_EXTRAS = ("asr", "diar", "aec", "ui")

# --------------------------------------------------------------------------- politica

PERMISSIVE = {
    "mit",
    "mit-0",
    "mit-cmu",
    "bsd-2-clause",
    "bsd-3-clause",
    "bsd",  # variante sin especificar: se verifica con el texto cuando existe
    "0bsd",
    "apache-2.0",
    "isc",
    "psf-2.0",
    "python-2.0",
    "zlib",
    "cc0-1.0",
    "unlicense",
    "hpnd",
    "bsl-1.0",
    "public-domain",
}
WEAK_COPYLEFT = {"mpl-2.0"}  # copyleft por fichero: se puede distribuir junto a codigo MIT
LGPL = {"lgpl-2.1-only", "lgpl-2.1-or-later", "lgpl-3.0-only", "lgpl-3.0-or-later", "lgpl"}
BLOCKED_PREFIXES = ("gpl", "agpl", "sspl", "cc-by-nc", "busl")

# Licencias LGPL revisadas a mano: (modo de enlazado, por que cumple, obligaciones).
LGPL_REVIEWED: dict[str, dict[str, str]] = {
    "soxr": {
        "licence": "LGPL-2.1-or-later (python-soxr + libsoxr)",
        "linking": "libsoxr va compilada DENTRO de `soxr/soxr_ext.cp311-win_amd64.pyd` (un unico "
        "binario de extension; no hay DLL aparte).",
        "why": "El `.pyd` se distribuye SIN modificar y como fichero independiente en la carpeta de "
        "instalacion (PyInstaller onedir): se puede sustituir por otro build de python-soxr; el "
        "codigo de Micnaga solo lo usa por `import soxr` (no se enlaza estaticamente con el). "
        "Alternativa sin LGPL si se prefiere: `scipy.signal.resample_poly` (BSD-3).",
        "source": "https://github.com/dofuuz/python-soxr (libsoxr: https://sourceforge.net/p/soxr/code)",
    },
    "pystray": {
        "licence": "LGPL-3.0",
        "linking": "Python puro. El instalador lo incluye como ficheros `.py` sueltos en "
        "`_internal/pystray/` (no dentro del archivo PYZ) mediante "
        "`module_collection_mode={'pystray': 'py'}`.",
        "why": "Cualquiera puede editar o reemplazar esa carpeta y Micnaga usara su version "
        "modificada: se cumple el derecho a sustituir la biblioteca.",
        "source": "https://github.com/moses-palmer/pystray",
    },
}

# Solo para distribuciones sin metadatos ni fichero de licencia utilizables. Cada entrada DEBE
# llevar la fuente donde se comprobo (no se inventan licencias).
MANUAL_LICENSES: dict[str, tuple[str, str]] = {
    "pyaec": (
        "MIT",
        "https://github.com/thewh1teagle/aec/blob/main/LICENSE (el wheel de PyPI no declara "
        "licencia ni trae fichero; contiene SpeexDSP, BSD-3-Clause)",
    ),
}

# Dependencias que estan en la clausura pero NO viajan en el instalador (se sustituyen por un stub
# en ``packaging/stubs``). Su licencia se muestra pero no bloquea; ``--dist`` verifica que el stub
# esta en su sitio y que los binarios reales no se han colado.
NOT_BUNDLED: dict[str, str] = {
    "av": "PyAV: sus ruedas traen FFmpeg con libx264/libx265 (GPL-2.0+). Sustituido por "
    "packaging/stubs/av; faster-whisper solo recibe arrays.",
    "pyaec": "pyaec (SpeexDSP, `aec.dll`): su DLL no esta firmada ni declara editor y Windows/antivirus "
    "bloquearon la instalacion en un PC. Solo para desarrollo (extra `aec-speex`); el instalador "
    "usa webrtc (aec-audio-processing). Mientras la instalacion de desarrollo lo declare en el "
    "extra `aec` aparece en la clausura, pero no viaja.",
    "kaldiio": "kaldiio (nttcslab-sp): su LICENSE es un «Software License Agreement for Evaluation» "
    "(uso interno de evaluacion, prohibe redistribuir) verificado en el paquete y en el repo "
    "upstream. Solo la importa wespeakerruntime en `extract_embedding_kaldiio`, que Micnaga no "
    "usa. Sustituido por packaging/stubs/kaldiio.",
}

# Binarios que no pueden viajar en el instalador (``--dist``): (patron, motivo).
FORBIDDEN_BINARIES: tuple[tuple[str, str], ...] = (
    (r"libx26[45]", "FFmpeg con x264/x265: GPL-2.0+"),
    (r"libpostproc|libavfilter.*gpl", "FFmpeg GPL"),
    (r"cudnn", "NVIDIA cuDNN: licencia propietaria; solo sirve con CUDA"),
    (r"^(dbghelp|dbgcore)\.dll$", "DLL de depuracion de Windows"),
    (r"libfdk", "FDK-AAC: licencia no libre"),
    (r"^aec\.dll$", "pyaec/SpeexDSP: DLL sin firmar (bloqueada por antivirus); solo desarrollo"),
)

# --------------------------------------------------------------------------- SPDX

_FREEFORM: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), v)
    for p, v in (
        (r"^mit[- ]cmu\b", "MIT-CMU"),
        (r"^mit[- ]0$", "MIT-0"),
        (r"^mit( license)?( \(mit\))?$|^the mit license", "MIT"),
        (r"^bsd[- ]?3|3-clause bsd|bsd 3|new bsd|modified bsd|bsd-3", "BSD-3-Clause"),
        (r"^bsd[- ]?2|2-clause bsd|simplified bsd|bsd-2", "BSD-2-Clause"),
        (r"^bsd( license)?$", "BSD"),
        (r"^0bsd$", "0BSD"),
        (
            r"apache[- ]?(software )?(license)?[ ,-]*(v|version )?2(\.0)?|^apache licensed",
            "Apache-2.0",
        ),
        (r"^psf|python software foundation", "PSF-2.0"),
        (r"^mpl[- ]?2|mozilla public license.*2", "MPL-2.0"),
        (r"^lgplv?3|lesser general public license.*(v3|version 3)", "LGPL-3.0-or-later"),
        (r"^lgplv?2\.1|lesser general public license.*2\.1", "LGPL-2.1-or-later"),
        (r"^lgpl|lesser general public|library or lesser", "LGPL"),
        (r"^agpl|affero", "AGPL-3.0-or-later"),
        (r"^gplv?[23]|^gnu general public|^gpl", "GPL-3.0-or-later"),
        (r"^isc( license)?", "ISC"),
        (r"^zlib", "Zlib"),
        (r"^cc0", "CC0-1.0"),
        (r"^unlicense|^the unlicense", "Unlicense"),
        (r"^public domain", "Public-Domain"),
    )
)

_CLASSIFIERS: dict[str, str] = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: MIT No Attribution License (MIT-0)": "MIT-0",
    "License :: OSI Approved :: BSD License": "BSD",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: Zope Public License": "ZPL-2.1",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
    "License :: OSI Approved :: GNU Lesser General Public License v3 or later (LGPLv3+)": "LGPL-3.0-or-later",
    "License :: OSI Approved :: GNU Library or Lesser General Public License (LGPL)": "LGPL",
    "License :: OSI Approved :: GNU General Public License v3 (GPLv3)": "GPL-3.0-only",
    "License :: OSI Approved :: GNU General Public License v2 (GPLv2)": "GPL-2.0-only",
    "License :: OSI Approved :: GNU General Public License (GPL)": "GPL",
    "License :: OSI Approved :: GNU Affero General Public License v3": "AGPL-3.0-only",
    "License :: Public Domain": "Public-Domain",
    "License :: CC0 1.0 Universal (CC0 1.0) Public Domain Dedication": "CC0-1.0",
}

# Huellas de texto de licencia (para ficheros LICENSE/COPYING cuando los metadatos no bastan).
_SNIFF: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE | re.DOTALL), v)
    for p, v in (
        (r"GNU AFFERO GENERAL PUBLIC LICENSE", "AGPL-3.0-or-later"),
        (r"GNU LESSER GENERAL PUBLIC LICENSE.{0,80}Version 3", "LGPL-3.0-or-later"),
        (r"GNU (?:LESSER|LIBRARY) GENERAL PUBLIC LICENSE.{0,80}Version 2\.1", "LGPL-2.1-or-later"),
        (r"GNU (?:LESSER|LIBRARY) GENERAL PUBLIC LICENSE", "LGPL"),
        (r"GNU GENERAL PUBLIC LICENSE", "GPL-3.0-or-later"),
        (r"Mozilla Public License.{0,40}(?:Version )?2\.0", "MPL-2.0"),
        (r"Apache License.{0,40}Version 2\.0", "Apache-2.0"),
        (r"Redistribution and use in source and binary forms.*?Neither the name", "BSD-3-Clause"),
        (r"Redistribution and use in source and binary forms", "BSD-2-Clause"),
        (r"Permission is hereby granted, free of charge, to any person obtaining a copy", "MIT"),
        (
            r"Permission to use, copy, modify, and/or distribute this software for any purpose",
            "ISC",
        ),
        (r"Python Software Foundation License|PYTHON SOFTWARE FOUNDATION", "PSF-2.0"),
    )
)

_LICENSE_FILE_RE = re.compile(r"(licen[sc]e|copying|notice|copyright|authors)", re.IGNORECASE)


def normalize(text: str) -> str | None:
    """Texto libre -> id SPDX aproximado (o ``None``)."""
    value = " ".join(text.split())
    if not value:
        return None
    for pattern, spdx in _FREEFORM:
        if pattern.search(value):
            return spdx
    return None


def sniff_text(text: str) -> str | None:
    for pattern, spdx in _SNIFF:
        if pattern.search(text):
            return spdx
    return None


# ---------------------------------------------------------------- expresiones SPDX (AND / OR)

_TOKEN_RE = re.compile(r"\(|\)|\bAND\b|\bOR\b|\bWITH\b|[^\s()]+", re.IGNORECASE)


def _category_of_id(spdx: str) -> str:
    low = spdx.lower().removesuffix("+")
    if low in PERMISSIVE:
        return "permissive"
    if low in WEAK_COPYLEFT:
        return "weak"
    if low in LGPL:
        return "lgpl"
    if low.startswith(BLOCKED_PREFIXES):
        return "blocked"
    return "unknown"


_RANK = {"permissive": 0, "weak": 1, "lgpl": 2, "unknown": 3, "blocked": 4}


def expression_category(expression: str) -> str:
    """Peor categoria de ``A AND B``; mejor categoria de ``A OR B`` (se puede elegir)."""
    tokens = _TOKEN_RE.findall(expression)
    pos = 0

    def parse_or() -> str:
        nonlocal pos
        parts = [parse_and()]
        while pos < len(tokens) and tokens[pos].upper() == "OR":
            pos += 1
            parts.append(parse_and())
        return min(parts, key=_RANK.__getitem__)

    def parse_and() -> str:
        nonlocal pos
        parts = [parse_atom()]
        while pos < len(tokens) and tokens[pos].upper() == "AND":
            pos += 1
            parts.append(parse_atom())
        return max(parts, key=_RANK.__getitem__)

    def parse_atom() -> str:
        nonlocal pos
        if pos >= len(tokens):
            return "unknown"
        token = tokens[pos]
        if token == "(":
            pos += 1
            inner = parse_or()
            if pos < len(tokens) and tokens[pos] == ")":
                pos += 1
            return inner
        pos += 1
        category = _category_of_id(token)
        if pos < len(tokens) and tokens[pos].upper() == "WITH":  # excepcion: se acepta la base
            pos += 2
        return category

    return parse_or()


# --------------------------------------------------------------------------- distribuciones


@dataclass
class LicenseFile:
    name: str
    text: str


@dataclass
class DistInfo:
    name: str
    version: str
    expression: str = ""
    source: str = ""  # de donde sale la licencia
    category: str = "unknown"
    homepage: str = ""
    files: list[LicenseFile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    required_by: set[str] = field(default_factory=set)


def _installed_index() -> dict[str, md.Distribution]:
    index: dict[str, md.Distribution] = {}
    for dist in md.distributions():
        name = dist.metadata["Name"]
        if name:
            index.setdefault(canonicalize_name(name), dist)
    return index


def _requirements(dist: md.Distribution, extras: Iterable[str]) -> Iterable[Requirement]:
    wanted = set(extras) or {""}
    for raw in dist.requires or []:
        req = Requirement(raw)
        if req.marker is None or any(req.marker.evaluate({"extra": e}) for e in wanted):
            yield req


def runtime_closure(
    root: str = ROOT_DIST, extras: Iterable[str] = RUNTIME_EXTRAS
) -> tuple[dict[str, DistInfo], list[str]]:
    """Distribuciones instaladas alcanzables desde ``root``. Devuelve (info, requisitos sin instalar)."""
    index = _installed_index()
    result: dict[str, DistInfo] = {}
    missing: list[str] = []
    stack: list[tuple[str, frozenset[str], str]] = [
        (canonicalize_name(root), frozenset(extras), "")
    ]
    seen: set[tuple[str, frozenset[str]]] = set()
    while stack:
        name, ex, parent = stack.pop()
        if (name, ex) in seen:
            continue
        seen.add((name, ex))
        dist = index.get(name)
        if dist is None:
            missing.append(f"{name} (requerido por {parent})")
            continue
        if name != canonicalize_name(root):
            info = result.get(name)
            if info is None:
                info = result[name] = DistInfo(dist.metadata["Name"], dist.version)
            info.required_by.add(parent)
        for req in _requirements(dist, ex):
            stack.append(
                (canonicalize_name(req.name), frozenset(req.extras), dist.metadata["Name"])
            )
    return result, sorted(set(missing))


def _license_files(dist: md.Distribution) -> list[LicenseFile]:
    found: list[LicenseFile] = []
    for rel in dist.files or []:
        parts = rel.parts
        in_dist_info = len(parts) > 1 and parts[0].endswith((".dist-info", ".egg-info"))
        if not (in_dist_info and _LICENSE_FILE_RE.search(rel.name)):
            continue
        if rel.suffix.lower() in {".py", ".pyc", ".json", ".png"}:
            continue
        try:
            text = Path(str(dist.locate_file(rel))).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.strip():
            found.append(LicenseFile("/".join(parts[1:]), text))
    # tambien ficheros LICENSE/COPYING junto al paquete (p. ej. `_soundfile_data/COPYING`)
    top = (dist.read_text("top_level.txt") or "").split()
    for pkg in top:
        pkg_dir = Path(str(dist.locate_file(pkg)))
        if not pkg_dir.is_dir():
            continue
        for path in sorted(pkg_dir.glob("*")):
            if path.is_file() and re.fullmatch(r"(?i)(licen[sc]e|copying)[^/]*", path.name):
                with contextlib.suppress(OSError):
                    found.append(
                        LicenseFile(f"{pkg}/{path.name}", path.read_text("utf-8", "replace"))
                    )
    return found


def _field(meta: md.PackageMetadata, name: str) -> str:
    values = meta.get_all(name)
    return str(values[0]) if values else ""


def resolve_license(info: DistInfo, dist: md.Distribution) -> None:
    meta = dist.metadata
    info.homepage = _field(meta, "Home-page")
    if not info.homepage:
        for item in meta.get_all("Project-URL") or []:
            label, _, url = item.partition(",")
            if label.strip().lower() in {"homepage", "home", "source", "repository"}:
                info.homepage = url.strip()
                break
    info.files = _license_files(dist)
    candidates: list[tuple[str, str]] = []  # (fuente, expresion SPDX)

    expression = _field(meta, "License-Expression").strip()
    if expression:
        candidates.append(("License-Expression", expression))
    license_field = _field(meta, "License").strip()
    long_field = ""
    if license_field and license_field.upper() != "UNKNOWN":
        if chr(10) not in license_field and len(license_field) <= 120:
            norm = normalize(license_field)
            if norm:
                candidates.append(
                    ("License (campo)", license_field if norm == license_field else norm)
                )
        else:
            long_field = license_field  # texto completo de licencia en el campo: ultimo recurso
    classifier_ids = [
        _CLASSIFIERS[c] for c in meta.get_all("Classifier") or [] if c in _CLASSIFIERS
    ]
    if classifier_ids:
        candidates.append(("clasificadores", " OR ".join(dict.fromkeys(classifier_ids))))
    sniffed = [sniff_text(f.text) for f in info.files]
    sniffed_ids = [x for x in dict.fromkeys(sniffed) if x]

    key = canonicalize_name(info.name)
    if not candidates and long_field:
        guess = normalize(long_field[:300]) or sniff_text(long_field)
        if guess:
            candidates.append(("License (campo, texto)", guess))
    if not candidates and sniffed_ids:
        candidates.append(("texto del fichero de licencia", sniffed_ids[0]))
    if not candidates and key in MANUAL_LICENSES:
        spdx, where = MANUAL_LICENSES[key]
        candidates.append((f"MANUAL ({where})", spdx))

    if not candidates:
        info.expression, info.source, info.category = (
            "UNKNOWN",
            "sin metadatos ni fichero",
            "unknown",
        )
        return
    # Prioridad: expresion SPDX > campo License > clasificadores > texto del fichero > manual.
    info.source, info.expression = candidates[0]
    info.category = expression_category(_normalize_expression(info.expression))
    # Discrepancias entre fuentes de METADATOS: manda la mas restrictiva solo si es LGPL o peor.
    for source, other in candidates[1:]:
        other_cat = expression_category(_normalize_expression(other))
        if _RANK[other_cat] > _RANK[info.category]:
            info.notes.append(
                f"{source} indica `{other}` ({other_cat}), distinto de `{info.expression}`: revisar"
            )
            if _RANK[other_cat] >= _RANK["lgpl"]:
                info.category = other_cat
                info.expression = f"{info.expression} / {other}"
    # Los ficheros de licencia de paquetes que empaquetan componentes de terceros (numpy, scipy,
    # torch...) citan GPL/LGPL de forma legitima: solo se avisa, no cambia la categoria.
    if info.source != "texto del fichero de licencia":
        extra = sorted({s for s in sniffed_ids if _RANK[_category_of_id(s)] >= _RANK["lgpl"]})
        if extra:
            info.notes.append(
                "su fichero de licencia contiene texto "
                + ", ".join(extra)
                + " (el propio texto LGPL o licencias de componentes de terceros que cita): revisar"
            )


def _normalize_expression(expression: str) -> str:
    """Normaliza cada hoja de una expresion SPDX (``MIT License`` -> ``MIT``)."""
    if re.fullmatch(r"[\w.+\-]+(?:\s+(?:AND|OR|WITH)\s+[\w.+\-]+|\s*[()]\s*)*", expression, re.I):
        return expression
    return normalize(expression) or expression


def classify_all(infos: dict[str, DistInfo], index: dict[str, md.Distribution]) -> None:
    for key, info in infos.items():
        resolve_license(info, index[key])
        if key in NOT_BUNDLED:
            info.notes.append("NO SE DISTRIBUYE: " + NOT_BUNDLED[key])
            info.category = "not-bundled"
            if info.expression == "UNKNOWN":
                info.expression = "no determinada: ver nota"
            continue
        if info.category == "lgpl" and key not in LGPL_REVIEWED:
            info.notes.append(
                "LGPL sin revisar: anadir a LGPL_REVIEWED (modo de enlazado) o sustituir"
            )
            info.category = "lgpl-unreviewed"


def blockers(infos: dict[str, DistInfo]) -> list[str]:
    problems = []
    for _key, info in sorted(infos.items()):
        if info.category in {"blocked", "unknown", "lgpl-unreviewed"}:
            problems.append(f"{info.name} {info.version}: {info.expression} [{info.category}]")
    return problems


def scan_dist(folder: Path) -> list[str]:
    """Binarios prohibidos dentro de una carpeta congelada."""
    problems = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        for pattern, reason in FORBIDDEN_BINARIES:
            if re.search(pattern, path.name, re.IGNORECASE):
                problems.append(f"{path.relative_to(folder)}: {reason}")
    return problems


# --------------------------------------------------------------------------- Markdown

MODELS_SECTION = """\
## Modelos (se descargan en el primer arranque; no van en el instalador)

Los modelos **no se redistribuyen** con Micnaga: la app los descarga desde Hugging Face con URL fijada
y SHA-256 (`micnaga/models.py`). Quien use Micnaga acepta las licencias de cada modelo. Donde la
licencia es **CC-BY-4.0 la atribucion es obligatoria** y se muestra tambien en «Acerca de» y en el
README.

| Modelo | Autor / origen | Licencia | Notas y atribucion |
| --- | --- | --- | --- |
| **Parakeet TDT 0.6B v3** (int8, ONNX; motor opcional) | NVIDIA, `nvidia/parakeet-tdt-0.6b-v3`; conversion ONNX: `istupakov/parakeet-tdt-0.6b-v3-onnx` | **CC-BY-4.0** | Atribucion obligatoria: © NVIDIA. Modelo derivado (conversion ONNX/int8) de terceros. |
| **WeSpeaker voxceleb ResNet34-LM** (ONNX) | WeSpeaker, `Wespeaker/wespeaker-voxceleb-resnet34-LM` | **CC-BY-4.0** (segun la ficha de Hugging Face); codigo WeSpeaker: Apache-2.0 | Atribucion obligatoria: WeSpeaker. Embeddings de hablante para la diarizacion (`diarize`) y las huellas de voz. |
| **Silero VAD** (ONNX) | Silero Team, `snakers4/silero-vad`; conversion: `istupakov/silero-vad-onnx` | MIT | |
| **faster-whisper large-v3-turbo** (por defecto) | `mobiuslabsgmbh/faster-whisper-large-v3-turbo` (conversion CTranslate2 de OpenAI Whisper) | MIT (pesos de Whisper: MIT, OpenAI) | Motor de transcripcion por defecto y de «Mejorar transcripcion». |
| **faster-whisper small / large-v3** (opcionales) | Systran, `Systran/faster-whisper-small`, `Systran/faster-whisper-large-v3` (conversion CTranslate2 de OpenAI Whisper) | MIT (pesos de Whisper: MIT, OpenAI) | Solo si el usuario los elige. |

### Bibliotecas nativas de cancelacion de eco (no son modelos)

| Componente | Origen | Licencia |
| --- | --- | --- |
| `pyaec` (`aec.dll`) | https://github.com/thewh1teagle/aec | MIT. Envuelve **SpeexDSP** (BSD-3-Clause, Xiph.Org). **No se incluye en el instalador** (DLL sin firmar; solo desarrollo). |
| `aec-audio-processing` (`webrtc-audio-processing-2-1.dll`) | enlace de WebRTC Audio Processing (AEC3) | BSD-3-Clause; **WebRTC** (BSD-3-Clause, The WebRTC project authors) incluye abseil (Apache-2.0). |
"""

NATIVE_SECTION = """\
## Componentes nativos y de ejecucion que acompanan al instalador

| Componente | Licencia | Notas |
| --- | --- | --- |
| CPython 3.11 (`python311.dll`) | PSF-2.0 | Va dentro de la carpeta congelada. |
| PyInstaller (bootloader `micnaga*.exe`) | GPL-2.0-or-later **con excepcion de bootloader** | La excepcion permite distribuir el bootloader dentro de aplicaciones congeladas con cualquier licencia (https://pyinstaller.org/en/stable/license.html). PyInstaller no se redistribuye como biblioteca. |
| Microsoft Visual C++ Runtime (`vcruntime140*.dll`, `msvcp140*.dll`) | Terminos de redistribucion de Microsoft (Visual Studio) | Redistribuible; copiado por PyInstaller. |
| OpenSSL 3 (`libcrypto-3-x64.dll`, `libssl-3-x64.dll`) | Apache-2.0 | Los trae el Python empaquetado. |
| SQLite (`sqlite3.dll`) | Dominio publico | Biblioteca de Python. |
| libffi (`libffi-8.dll`) | MIT | |
| libsndfile (`_soundfile_data/libsndfile_x64.dll`, dentro de `soundfile`) | **LGPL-2.1+** (el `COPYING` de la rueda es solo ese texto) | **DLL independiente enlazada dinamicamente** (ver resumen LGPL). Las licencias de los codecs que la DLL pueda enlazar (FLAC, Ogg, Vorbis, Opus, mpg123, LAME) **no vienen en la rueda: sin verificar**. |
| `_portaudiowpatch.cp311-win_amd64.pyd` (PyAudioWPatch; PortAudio con WASAPI loopback) | PyAudioWPatch: Apache-2.0 (verificado en su LICENSE). PortAudio: MIT (**sin verificar**: la rueda no trae su aviso) | Sin DLL aparte; se supone que PortAudio va compilada en la extension. |
| `ctranslate2.dll` + `libiomp5md.dll` (CTranslate2) | MIT (metadatos de ctranslate2) | Su README dice que la rueda puede integrar Intel MKL, oneDNN y OpenBLAS y se incluye `libiomp5md.dll` (OpenMP de Intel). La rueda **no trae avisos de terceros**: las licencias de esos componentes estan **sin verificar** (revisar antes de publicar). `cudnn64_9.dll` (NVIDIA, propietaria) se **excluye** del instalador. |
| `onnxruntime.dll` (ONNX Runtime) | MIT | Con avisos de terceros en `onnxruntime/ThirdPartyNotices.txt` (comprobar que se copia al instalador). |
| SDK de WebView2 en `webview/lib` (`Microsoft.Web.WebView2.Core.dll`, `...WinForms.dll`, `WebView2Loader.dll`; pywebview) | Microsoft (licencia propia del paquete NuGet Microsoft.Web.WebView2) | Licencia **sin verificar**: la rueda de pywebview no la incluye; revisar antes de publicar. pywebview: BSD-3-Clause (metadatos). |
| pythonnet (`pythonnet/runtime/Python.Runtime.dll`) y clr_loader (`ClrLoader.dll`) | MIT | Verificado en metadatos / fichero de licencia. |
| WebView2 Runtime | Microsoft | **No se incluye**: ya viene con Windows 11 (en Windows 10 lo instala Microsoft). |
| Fuentes de la interfaz (`micnaga/ui/web/assets/fonts`): Barlow 400 y 600, Barlow Semi Condensed 700, JetBrains Mono 500 (subconjunto latino, woff2) | SIL OFL 1.1 | Sin modificar salvo el subconjunto y la instancia 500 de JetBrains Mono. Avisos y texto de la licencia en `assets/fonts/OFL.txt` (Barlow) y `assets/fonts/OFL-JetBrainsMono.txt`. |
| FFmpeg / PyAV (`av`) | LGPL/GPL (la rueda de PyAV enlaza libx264/libx265: GPL-2.0+) | **Excluido** del instalador: Micnaga no decodifica ficheros con PyAV; `faster-whisper` recibe arrays y `packaging/stubs/av` sustituye al modulo. |
| NVIDIA cuDNN (`cudnn64_9.dll`, en la rueda de ctranslate2) | Propietaria (NVIDIA) | **Excluido** del instalador (solo sirve con CUDA). |
"""


def _lgpl_section(infos: dict[str, DistInfo]) -> str:
    rows = []
    for key, review in LGPL_REVIEWED.items():
        present = key in infos
        rows.append(
            f"### {infos[key].name if present else key} — {review['licence']}\n\n"
            f"* **Como se enlaza:** {review['linking']}\n"
            f"* **Por que se puede distribuir:** {review['why']}\n"
            f"* **Fuente:** {review['source']}\n"
        )
    rows.append(
        "### libsndfile — LGPL-2.1-or-later (dentro de `soundfile`)\n\n"
        "* **Como se enlaza:** DLL separada `_soundfile_data/libsndfile_x64.dll`, cargada en "
        "ejecucion por `ctypes`/`cffi` (enlazado **dinamico**); no se modifica.\n"
        "* **Por que se puede distribuir:** esta como fichero independiente y sustituible; Micnaga solo "
        "la usa a traves de `soundfile`.\n"
    )
    return "\n".join(rows)


LGPL_SUMMARY = """\
## Resumen de obligaciones LGPL (leer primero)

Micnaga es MIT. Distribuye tres componentes LGPL, que **no contaminan** el codigo MIT siempre que se
cumpla lo siguiente (LGPL-2.1 §6 / LGPL-3.0 §4):

1. **Enlazado dinamico o sustituible:** cada componente LGPL debe poder reemplazarse por otra version
   propia. En el instalador (PyInstaller *onedir*) todos son **ficheros independientes** dentro de la
   carpeta de instalacion: una DLL (`libsndfile`), una extension `.pyd` (`soxr`) o codigo Python suelto
   (`pystray`, entregado como `.py`, no dentro del PYZ). Hay que mantener esa estructura: **no** usar
   `--onefile` ni compilar estos componentes dentro de un ejecutable propio.
2. **Entregar el texto de la licencia** junto con el programa (este fichero y los textos completos de
   abajo; el instalador copia `THIRD_PARTY_LICENSES.md` a la carpeta de instalacion).
3. **Ofrecer el codigo fuente** del componente LGPL (enlaces mas abajo) y **no impedir la
   sustitucion** ni la ingenieria inversa necesaria para depurar el enlazado.
4. **Aviso:** mencionar en «Acerca de»/README que se usa la biblioteca y que es LGPL.

Los componentes LGPL no se modifican en Micnaga.

Otras licencias con matiz: **tqdm** es MPL-2.0 AND MIT (copyleft por fichero; se distribuye sin
modificar, basta conservar el aviso) y **PyInstaller** aporta el bootloader bajo GPL-2.0+ con
excepcion que permite distribuirlo en cualquier aplicacion (ver «Componentes nativos»).
"""


def render_markdown(infos: dict[str, DistInfo], problems: list[str], missing: list[str]) -> str:
    out: list[str] = []
    out.append("# Licencias de terceros — Micnaga\n")
    out.append(
        "> Generado por `scripts/gen_licenses.py` (no editar a mano las secciones de dependencias; las "
        "de modelos y componentes nativos son texto fijo del script). "
        f"Fecha de generacion: {date.today().isoformat()}.\n"
    )
    out.append(
        "El codigo de Micnaga es **MIT** (`LICENSE`). Este fichero lista las licencias de las "
        "dependencias de ejecucion (clausura de `micnaga` + extras `asr`, `diar`, `aec`, `ui`; "
        "sin herramientas de desarrollo) y de los modelos.\n"
    )
    out.append(LGPL_SUMMARY)
    out.append(_lgpl_section(infos))
    if problems:
        out.append("## BLOQUEOS pendientes\n")
        out.extend(f"* {p}" for p in problems)
        out.append("")
    out.append("## Dependencias de Python\n")
    out.append("| Paquete | Version | Licencia | Fuente de la licencia | Categoria |")
    out.append("| --- | --- | --- | --- | --- |")
    for _key, info in sorted(infos.items(), key=lambda kv: kv[0]):
        out.append(
            f"| {info.name} | {info.version} | {info.expression} | {info.source} | {info.category} |"
        )
    out.append("")
    notes = [(i.name, n) for i in infos.values() for n in i.notes]
    if notes:
        out.append("### Notas de revision\n")
        out.extend(f"* **{name}**: {note}" for name, note in sorted(notes))
        out.append("")
    if missing:
        out.append("### Requisitos con marcador cierto que no estan instalados en este entorno\n")
        out.extend(f"* {m}" for m in missing)
        out.append("")
    out.append(NATIVE_SECTION)
    out.append(MODELS_SECTION)
    out.append("## Textos de licencia de las dependencias\n")
    seen: dict[str, str] = {}
    for _key, info in sorted(infos.items(), key=lambda kv: kv[0]):
        out.append(f"### {info.name} {info.version} — {info.expression}\n")
        if info.homepage:
            out.append(f"Origen: {info.homepage}\n")
        if not info.files:
            out.append(
                "_El paquete no incluye fichero de licencia; la licencia procede de los metadatos._\n"
            )
            continue
        for lf in info.files:
            digest = hashlib.sha256(lf.text.encode("utf-8")).hexdigest()
            if digest in seen:
                out.append(f"`{lf.name}`: texto identico al de {seen[digest]}.\n")
                continue
            seen[digest] = f"{info.name} (`{lf.name}`)"
            text = lf.text.strip()
            # Los textos se incluyen SIEMPRE completos (avisos de copyright y copyleft: obligacion).
            fence = "~~~~"
            out.append(f"`{lf.name}`:\n\n{fence}text\n{text}\n{fence}\n")
    return "\n".join(out).replace("\r\n", "\n").rstrip() + "\n"


# --------------------------------------------------------------------------- CLI


def build() -> tuple[dict[str, DistInfo], list[str], list[str]]:
    infos, missing = runtime_closure()
    index = _installed_index()
    classify_all(infos, index)
    return infos, blockers(infos), missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="solo comprueba; no escribe")
    mode.add_argument("--write", action="store_true", help="regenera THIRD_PARTY_LICENSES.md")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument(
        "--dist", type=Path, help="carpeta congelada a escanear (binarios prohibidos)"
    )
    args = parser.parse_args(argv)

    try:
        md.distribution(ROOT_DIST)
    except md.PackageNotFoundError:
        print(f"{ROOT_DIST} no esta instalado en este entorno (pip install -e .)", file=sys.stderr)
        return 2
    infos, problems, missing = build()
    if args.dist:
        if not args.dist.is_dir():
            print(f"no existe {args.dist}", file=sys.stderr)
            return 2
        problems += [f"[dist] {p}" for p in scan_dist(args.dist)]
    if args.write:
        args.output.write_text(
            render_markdown(infos, problems, missing), encoding="utf-8", newline="\n"
        )
        print(f"escrito {args.output} ({len(infos)} dependencias)")
    counts: dict[str, int] = {}
    for info in infos.values():
        counts[info.category] = counts.get(info.category, 0) + 1
    print(
        f"{len(infos)} dependencias de ejecucion: "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )
    for m in missing:
        print(f"aviso: requisito no instalado: {m}", file=sys.stderr)
    if problems:
        print("LICENCIAS INCOMPATIBLES O SIN DETERMINAR:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("OK: ninguna licencia bloquea la distribucion MIT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
