# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller (onedir) para Micnaga. Uso: ``scripts\build.ps1 -Variant standard|torch``.

Variables de entorno que lee:

``MICNAGA_VARIANT``  ``standard`` (por defecto) o ``torch``.
    * ``standard``: EL producto (un unico instalador). Sin torch; la diarizacion es
      ``micnaga.pipeline.engines.diar.onnx`` (onnxruntime + scipy.cluster, sin binarios extra).
      Sin diarize / silero-vad / wespeakerruntime / scikit-learn / pandas.
      La diarizacion queda desactivada (todo lo remoto es ``S1``) y el instalador cabe en el
      presupuesto de 150 MB de la spec §2.
    * ``torch``: respaldo SOLO para desarrollo (``diarize`` + torch, ~0,8 GB). No se distribuye.

Tres ejecutables en la misma carpeta (comparten ``_internal``):
``micnaga.exe`` (ventana), ``micnaga-cli.exe`` (consola) y ``micnaga-host.exe`` (host de la
extension). Los tres son el mismo ``frozen_entry.py``; el host se reconoce por el nombre.
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

VARIANT = os.environ.get("MICNAGA_VARIANT", "standard").strip().lower()
if VARIANT not in ("standard", "torch"):
    raise SystemExit(f"MICNAGA_VARIANT debe ser standard o torch, no {VARIANT!r}")

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH lo define PyInstaller
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
ICON = ROOT / "micnaga" / "ui" / "web" / "assets" / "micnaga.ico"
NAME = "micnaga"  # carpeta de salida: dist/micnaga (standard) o dist/micnaga-torch

# Modulo generado: le dice al --selftest en que variante esta.
variant_dir = BUILD_DIR / f"variant-{VARIANT}"
variant_dir.mkdir(parents=True, exist_ok=True)
(variant_dir / "micnaga_variant.py").write_text(f'VARIANT = "{VARIANT}"\n', encoding="utf-8")

# --------------------------------------------------------------------------- dependencias
# El nucleo importa casi todo por cadena (``cli.py`` y ``registry.py``), asi que PyInstaller no lo
# ve: se recogen todos los submodulos de micnaga.
hiddenimports = collect_submodules("micnaga") + ["micnaga_variant"]
# Librerias que se importan de forma perezosa dentro de funciones o desde las fabricas.
hiddenimports += [
    "onnx_asr",
    "faster_whisper",
    "aec_audio_processing",
    "pyaec",
    "pyaudiowpatch",
    "win32event",
    "win32api",
    "win32file",
    "win32pipe",
    "win32gui",
    "win32process",
    "pythoncom",
    "pywintypes",
    "comtypes.client",
    "pycaw.pycaw",
    "windows_toasts",
    "scipy.signal",
    # diarizacion por defecto sin torch (clustering aglomerativo con scipy)
    "micnaga.pipeline.engines.diar.onnx",
    "scipy.cluster",
    "scipy.cluster.hierarchy",
    "scipy.cluster.vq",
    "scipy.spatial.distance",
]

datas = []
binaries = []
# Interfaz web (HTML/CSS/JS/i18n/iconos): ``micnaga/ui/*.py`` los busca en ``<carpeta>/web``.
datas += [(str(ROOT / "micnaga" / "ui" / "web"), "micnaga/ui/web")]
datas += [(str(ROOT / "micnaga" / "py.typed"), "micnaga")]
# Secretaria: prompts versionados, contexto base y (si existe) el mapa de campos de Odoo.
datas += [(str(ROOT / "micnaga" / "brain" / "prompts"), "micnaga/brain/prompts")]
datas += [(str(ROOT / "micnaga" / "brain" / "context"), "micnaga/brain/context")]
if (ROOT / "micnaga" / "brain" / "odoo_schema.json").is_file():
    datas += [(str(ROOT / "micnaga" / "brain" / "odoo_schema.json"), "micnaga/brain")]
# Sustituto de PyAV (ver excludes): ``faster_whisper.audio`` hace ``import av`` al importarse.
datas += [(str(ROOT / "packaging" / "stubs" / "av"), "av")]
if VARIANT == "torch":
    # kaldiio (licencia de solo evaluacion de NTT) lo importa wespeakerruntime: se sustituye.
    datas += [(str(ROOT / "packaging" / "stubs" / "kaldiio"), "kaldiio")]
# Licencias (LGPL, CC-BY...): el texto viaja con el programa.
datas += [(str(ROOT / "LICENSE"), "."), (str(ROOT / "THIRD_PARTY_LICENSES.md"), ".")]

# DLL/binarios que los hooks no siempre recogen.
for pkg in ("onnxruntime", "ctranslate2", "aec_audio_processing", "pyaec", "soundfile", "soxr"):
    binaries += collect_dynamic_libs(pkg)
for pkg in ("onnx_asr", "aec_audio_processing", "_soundfile_data", "webview", "faster_whisper"):
    try:
        datas += collect_data_files(pkg)
    except Exception:  # paquete ausente: el --selftest lo dira
        pass
# Avisos de licencia que las ruedas dejan junto al paquete y que deben acompanar a los binarios.
import importlib.util

for pkg, names in {"onnxruntime": ("LICENSE", "ThirdPartyNotices.txt")}.items():
    pkg_spec = importlib.util.find_spec(pkg)
    if pkg_spec and pkg_spec.submodule_search_locations:
        for fname in names:
            notice = Path(pkg_spec.submodule_search_locations[0]) / fname
            if notice.is_file():
                datas += [(str(notice), pkg)]
# ``onnx_asr`` (y otros) consultan su version con importlib.metadata al importarse.
for dist in ("onnx-asr", "onnxruntime", "faster-whisper", "ctranslate2", "tokenizers"):
    try:
        datas += copy_metadata(dist)
    except Exception:  # distribucion ausente en este entorno
        pass
# PyAudioWPatch: portaudio va dentro de ``_portaudiowpatch.pyd`` (sin DLL aparte); si hubiese
# DLL junto al paquete, esto la recoge.
binaries += collect_dynamic_libs("pyaudiowpatch")

# ----------------------------------------------------------------------------- exclusiones
# Herramientas de desarrollo, tests y cosas que ningun modulo de micnaga importa.
excludes = [
    "pytest", "_pytest", "pytest_timeout", "mypy", "mypyc", "ruff", "pyinstaller", "PyInstaller",
    "setuptools", "pkg_resources", "pip", "wheel", "distutils", "tkinter", "turtle",
    "IPython", "jupyter", "notebook", "matplotlib", "numba", "llvmlite", "cv2",
    "jiwer", "pyannote", "pyannote.metrics", "pyannote.core", "pyannote.database",
    "tests", "types_psutil", "types_pywin32",
    # onnxruntime arrastra herramientas de cuantizacion/transformers que no usamos
    "onnxruntime.tools", "onnxruntime.quantization", "onnxruntime.transformers",
    "onnxruntime.training", "onnx",
    # sherpa-onnx esta declarado en el extra ``asr`` pero ningun modulo lo importa
    "sherpa_onnx",
    # PyAV: sus ruedas traen FFmpeg compilado con libx264/libx265 (GPL) y 63 MB de DLL. Micnaga
    # solo pasa arrays a faster-whisper (nunca ficheros de audio), asi que se sustituye por un
    # modulo vacio (packaging/stubs/av) que solo falla si alguien intenta decodificar un fichero.
    "av",
    "hf_xet",  # descargas Xet de huggingface_hub: Micnaga descarga con urllib + SHA-256
]
if VARIANT == "standard":
    excludes += [
        "torch", "torchaudio", "torchgen", "torio", "functorch",
        "diarize", "silero_vad", "wespeakerruntime", "kaldiio",
        "sklearn", "pandas", "sympy", "mpmath", "networkx", "joblib", "threadpoolctl",
        "pydantic", "pydantic_core", "jinja2", "click",
    ]  # fmt: skip
else:
    hiddenimports += ["diarize", "silero_vad", "wespeakerruntime", "sklearn"]
    excludes += ["kaldiio"]
    # silero_vad (via diarize) carga su modelo desde ``silero_vad/data`` junto al paquete.
    datas += collect_data_files("silero_vad")

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "frozen_entry.py")],
    pathex=[str(ROOT), str(variant_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    # pystray es LGPL-3.0: se entrega como ficheros .py sueltos (sustituibles) y no dentro del
    # PYZ. Ver THIRD_PARTY_LICENSES.md.
    module_collection_mode={"pystray": "py"},
)

# DLL que no deben viajar: cuDNN (NVIDIA, propietaria; solo sirve con CUDA, que no usamos) y las de
# depuracion de Windows (ya estan en System32; no son redistribuibles).
DROP_BINARIES = {"cudnn64_9.dll", "dbghelp.dll", "dbgcore.dll"}
a.binaries = [b for b in a.binaries if os.path.basename(b[0]).lower() not in DROP_BINARIES]

pyz = PYZ(a.pure)  # noqa: F821


def make_exe(name: str, *, console: bool) -> "EXE":  # noqa: F821
    return EXE(  # noqa: F821
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=console,
        icon=str(ICON),
        contents_directory="_internal",
    )


exe_gui = make_exe("micnaga", console=False)
exe_cli = make_exe("micnaga-cli", console=True)
exe_host = make_exe("micnaga-host", console=True)

coll = COLLECT(  # noqa: F821
    exe_gui,
    exe_cli,
    exe_host,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=NAME if VARIANT == "standard" else f"{NAME}-torch",
)
