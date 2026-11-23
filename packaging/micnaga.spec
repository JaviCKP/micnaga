# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller (onedir) para Micnaga. Uso: ``scripts\build.ps1``.

Un unico producto (un unico instalador). Incluye la diarizacion ``diarize`` (torch CPU, silero_vad,
wespeakerruntime, scikit-learn), que es el unico diarizador. Sin CUDA. Torch se recorta (ver
``TRIM_*`` y ``excludes``): solo se quitan subpaquetes que una diarizacion real NO carga
(comprobado volcando ``sys.modules`` tras ``diarize`` + ``embed`` y repitiendo la diarizacion con
esos modulos bloqueados; ver DECISIONS.md §31). Si se toca la lista, repetir esa prueba y la del
ejecutable congelado (``micnaga-cli.exe --selftest`` y ``--selftest-diarize <wav>``).

Tres ejecutables en la misma carpeta (comparten ``_internal``):
``micnaga.exe`` (ventana), ``micnaga-cli.exe`` (consola) y ``micnaga-host.exe`` (host de la
extension). Los tres son el mismo ``frozen_entry.py``; el host se reconoce por el nombre.

``noarchive=True``: los modulos Python van como ``.pyc`` sueltos en ``_internal`` y los tres
``.exe`` los COMPARTEN. Con el PYZ (por defecto) cada ``.exe`` llevaba su propia copia de todo el
codigo Python (3 x 51 MiB con torch).
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

VARIANT = "standard"  # unico producto; el --selftest lo comprueba
_requested = os.environ.get("MICNAGA_VARIANT", VARIANT).strip().lower()
if _requested not in ("standard", "torch"):  # "torch" = alias antiguo de lo mismo
    raise SystemExit(f"MICNAGA_VARIANT ya no existe (unico producto), no {_requested!r}")

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH lo define PyInstaller
BUILD_DIR = ROOT / "build"
ICON = ROOT / "micnaga" / "ui" / "web" / "assets" / "micnaga.ico"
NAME = "micnaga"  # carpeta de salida: dist/micnaga

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
    "scipy.cluster",
    "scipy.cluster.hierarchy",
    "scipy.cluster.vq",
    "scipy.spatial.distance",
    # diarizacion (unico motor): diarize + torch CPU; ``embed`` usa torchaudio.compliance.kaldi
    "diarize",
    "silero_vad",
    "silero_vad.data",  # paquete de recursos (importlib.resources) del modelo .jit
    "wespeakerruntime",
    "sklearn",
    "torchaudio.compliance.kaldi",
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
# Sustitutos (ver excludes): ``faster_whisper.audio`` hace ``import av`` al importarse y
# ``wespeakerruntime`` hace ``import kaldiio`` (licencia de solo evaluacion de NTT).
datas += [(str(ROOT / "packaging" / "stubs" / "av"), "av")]
datas += [(str(ROOT / "packaging" / "stubs" / "kaldiio"), "kaldiio")]
# silero_vad (via diarize) carga ``silero_vad/data/silero_vad.jit`` (``load_silero_vad()`` por
# defecto). Las otras seis variantes ONNX del paquete (~11 MiB) no se usan.
datas += collect_data_files("silero_vad", includes=["data/silero_vad.jit"])
# Licencias (LGPL, CC-BY...): el texto viaja con el programa.
datas += [(str(ROOT / "LICENSE"), "."), (str(ROOT / "THIRD_PARTY_LICENSES.md"), ".")]

# DLL/binarios que los hooks no siempre recogen. ``pyaec`` (aec.dll, sin firmar) NO viaja.
for pkg in ("onnxruntime", "ctranslate2", "aec_audio_processing", "soundfile", "soxr"):
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
for dist in ("onnx-asr", "onnxruntime", "faster-whisper", "ctranslate2", "tokenizers", "diarize"):
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
    "kaldiio",  # licencia de solo evaluacion: sustituido por packaging/stubs/kaldiio
    # pyaec (SpeexDSP): su ``aec.dll`` no esta firmada y bloqueo la instalacion en un PC.
    "pyaec",
    # Pillow: solo se usan Image/ImageDraw y el guardado ICO/PNG (iconos de la bandeja). Fuera
    # los codecs AVIF/WebP, FreeType, LittleCMS y Tk (~11 MiB de .pyd sin firmar).
    "PIL._avif", "PIL._webp", "PIL._imagingft", "PIL._imagingcms", "PIL._imagingtk",
    "PIL.ImageTk", "PIL.ImageQt",
    # Arrastrados por torch/sklearn pero no cargados por la diarizacion (comprobado).
    "pandas", "sympy", "mpmath", "networkx", "jinja2", "markupsafe", "click",
    # pygments solo lo usa la CLI de httpx2 (``httpx2._main``, importada a demanda).
    "pygments", "httpx2._main",
]  # fmt: skip
# torch CPU: compilador (inductor/dynamo), exportacion ONNX, NumPy-on-torch, lazy tensors y
# entrenamiento distribuido. Ninguno se carga al diarizar ni al extraer huellas (comprobado).
TRIM_TORCH = [
    "torch._inductor", "torch._dynamo", "torch.onnx", "torch._numpy", "torch._lazy",
    "torch.contrib", "torch.utils.hipify", "torch.utils.benchmark", "torch.utils.tensorboard",
    "torch.distributed.fsdp", "torch.distributed.tensor", "torch.distributed.elastic",
    "torch.distributed.checkpoint", "torch.distributed.pipelining", "torch.distributed._shard",
    "torch.distributed._tools", "torch.distributed.optim",
    "torchaudio.prototype",
]  # fmt: skip
# ``import torch`` carga ``torch.testing._internal.logging_tensor`` (y su paquete); el resto de
# ``torch.testing._internal`` (~10 MiB de utilidades de tests) no se usa.
_torch_spec = importlib.util.find_spec("torch")
if _torch_spec and _torch_spec.submodule_search_locations:
    _internal = Path(_torch_spec.submodule_search_locations[0]) / "testing" / "_internal"
    TRIM_TORCH += [
        f"torch.testing._internal.{p.stem}"
        for p in sorted(_internal.iterdir())
        if p.stem not in ("__init__", "logging_tensor", "__pycache__")
        and (p.suffix == ".py" or (p / "__init__.py").is_file())
    ]
# scikit-learn: diarize solo usa cluster, decomposition, metrics, mixture y preprocessing.
TRIM_SKLEARN = [
    "sklearn.ensemble", "sklearn.datasets", "sklearn.tests", "sklearn.neural_network",
    "sklearn.gaussian_process", "sklearn.semi_supervised", "sklearn.feature_extraction",
    "sklearn.feature_selection", "sklearn.impute", "sklearn.inspection", "sklearn.compose",
    "sklearn.cross_decomposition",
]  # fmt: skip
excludes += TRIM_TORCH + TRIM_SKLEARN

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "frozen_entry.py")],
    pathex=[str(ROOT), str(variant_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    # Modulos como .pyc sueltos en _internal, compartidos por los tres .exe (ver docstring).
    noarchive=True,
    # pystray es LGPL-3.0: se entrega como ficheros .py sueltos (sustituibles). Ver
    # THIRD_PARTY_LICENSES.md.
    module_collection_mode={"pystray": "py"},
)

# DLL que no deben viajar: cuDNN (NVIDIA, propietaria; solo sirve con CUDA, que no usamos), las de
# depuracion de Windows (ya estan en System32; no son redistribuibles) y aec.dll (pyaec).
DROP_BINARIES = {"cudnn64_9.dll", "dbghelp.dll", "dbgcore.dll", "aec.dll"}
# ``torch/bin`` solo trae ``protoc.exe`` (compilador de protobuf) y copias de DLL que ya estan en
# ``torch/lib`` (de donde las carga torch).
DROP_PREFIXES = ("torch/bin/", "torch\\bin\\")


def _keep(dest: str) -> bool:
    return os.path.basename(dest).lower() not in DROP_BINARIES and not dest.startswith(DROP_PREFIXES)


a.binaries = [b for b in a.binaries if _keep(b[0])]
a.datas = [d for d in a.datas if _keep(d[0])]

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
    name=NAME,
)

# ------------------------------------------------------------------ pyc de modulos con fuente
# Los paquetes que se recogen tambien con su fuente (torch y torchaudio: su hook usa ``pyz+py``,
# TorchScript necesita el .py) quedan como ``X.py`` + ``X.pyc`` en la misma carpeta. Python carga
# SIEMPRE el ``.py`` antes que un ``.pyc`` hermano y, como el congelado no escribe bytecode,
# recompilaria ~1300 modulos de torch en cada arranque del worker. Se mueve cada ``.pyc`` a
# ``__pycache__/X.cpython-311.pyc`` en modo «unchecked hash» (PEP 552): Python lo usa sin mirar la
# fecha del ``.py`` (que cambia al instalar). Mismo tamano; el ``.py`` sigue ahi para TorchScript.
import py_compile
import sys

_internal_dir = Path(DISTPATH) / NAME / "_internal"  # noqa: F821 - DISTPATH lo define PyInstaller
_relocated = 0
for _py in _internal_dir.rglob("*.py"):
    _pyc = _py.with_suffix(".pyc")
    if not _pyc.is_file():
        continue  # p. ej. pystray (solo fuente, LGPL) o los stubs
    py_compile.compile(
        str(_py),
        cfile=str(_py.parent / "__pycache__" / f"{_py.stem}.{sys.implementation.cache_tag}.pyc"),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    _pyc.unlink()
    _relocated += 1
print(f"micnaga.spec: {_relocated} .pyc movidos a __pycache__ (unchecked-hash)")
