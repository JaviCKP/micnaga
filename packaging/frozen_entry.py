"""Punto de entrada de los ejecutables congelados (PyInstaller).

Un solo script sirve para los tres ``.exe`` de la carpeta (comparten ``_internal``):

``micnaga.exe``       ventana (sin consola). Es el que usan el acceso directo y la clave ``Run``:
                      ``micnaga.exe app``. ``worker_command()`` lanza ``micnaga.exe worker``.
``micnaga-cli.exe``   consola: ``record``, ``process``, ``bench``, ``--version``, ``--selftest``.
``micnaga-host.exe``  host de *native messaging* de la extension del navegador (stdio). Se
                      reconoce por el nombre del ejecutable: Chrome lo llama solo con
                      ``chrome-extension://<id>/`` como argumento, asi que no admite subcomandos.

``--selftest [FICHERO]`` importa todos los modulos de ``micnaga`` y las librerias nativas que
usa y sale con codigo distinto de 0 si algo falla (informe JSON a ``FICHERO`` o a stdout).
No abre ventanas, no toca el registro ni la red.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys

HOST_EXE_PREFIX = "micnaga-host"

# Librerias de terceros que el ejecutable congelado debe poder importar.
THIRD_PARTY_IMPORTS = (
    "numpy",
    "scipy.signal",
    "scipy.cluster.hierarchy",
    "micnaga.pipeline.engines.diar.onnx",
    "soxr",
    "soundfile",
    "onnxruntime",
    "pyaudiowpatch",
    "win32api",
    "win32event",
    "pycaw.pycaw",
    "comtypes",
    "psutil",
    "rapidfuzz",
    "tomli_w",
    "onnx_asr",
    "faster_whisper",
    "ctranslate2",
    "aec_audio_processing",
    "pyaec",
    "webview",
    "clr",  # pythonnet: lo carga el backend de pywebview al arrancar la primera ventana
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "pystray",
    "windows_toasts",
    "PIL.Image",
)

# Solo en la variante de desarrollo «torch» (diarize + torch). En «standard» deben faltar.
FULL_ONLY_IMPORTS = ("torch", "torchaudio", "diarize", "wespeakerruntime", "silero_vad")

# Se saltan al importar en bloque: son puntos de entrada con efectos al importarse.
SKIP_MODULES = ("micnaga.__main__", "micnaga.bench.__main__", "micnaga.ui.demo")


def _fix_std_streams() -> None:
    """En un ejecutable «windowed» ``sys.stdout``/``stderr``/``stdin`` valen ``None``."""
    for name, mode in (("stdout", "w"), ("stderr", "w"), ("stdin", "r")):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, mode, encoding="utf-8"))  # noqa: SIM115


def _check_assets() -> list[str]:
    errors: list[str] = []
    from micnaga.ui import media

    for rel in ("popup.html", "pill.html", "library.html", "settings.html", "wizard.html"):
        if not (media.WEB_ROOT / rel).is_file():
            errors.append(f"missing web asset: {rel}")
    if not (media.WEB_ROOT / "assets" / "micnaga.ico").is_file():
        errors.append("missing web asset: assets/micnaga.ico")
    return errors


def selftest() -> dict[str, object]:
    import importlib
    import pkgutil

    import micnaga

    failures: dict[str, str] = {}
    ok: list[str] = []

    def attempt(name: str) -> None:
        try:
            importlib.import_module(name)
            ok.append(name)
        except BaseException as exc:  # queremos TODO fallo de importacion
            failures[name] = f"{type(exc).__name__}: {exc}"

    for module in pkgutil.walk_packages(micnaga.__path__, "micnaga."):
        if module.name not in SKIP_MODULES:
            attempt(module.name)
    for name in THIRD_PARTY_IMPORTS:
        attempt(name)

    variant = "unknown"
    with contextlib.suppress(ImportError):
        variant = importlib.import_module("micnaga_variant").VARIANT  # generado por la build
    full_present: list[str] = []
    for name in FULL_ONLY_IMPORTS:
        try:
            importlib.import_module(name)
            full_present.append(name)
        except BaseException:
            pass
    if variant == "standard" and full_present:
        failures["standard-variant-contains"] = ", ".join(full_present)
    if variant == "torch" and len(full_present) != len(FULL_ONLY_IMPORTS):
        failures["torch-variant-missing"] = ", ".join(set(FULL_ONLY_IMPORTS) - set(full_present))

    if variant == "torch" and "silero_vad" in full_present:
        import silero_vad

        data_dir = os.path.join(os.path.dirname(silero_vad.__file__), "data")
        if not os.path.isfile(os.path.join(data_dir, "silero_vad.onnx")):
            failures["silero_vad-data-missing"] = data_dir

    asset_errors = _check_assets()
    for err in asset_errors:
        failures[err] = "asset"
    return {
        "version": micnaga.__version__,
        "variant": variant,
        "frozen": bool(getattr(sys, "frozen", False)),
        "imported_ok": len(ok),
        "failures": failures,
    }


def _run_selftest(argv: list[str]) -> int:
    report = selftest()
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if argv:
        with open(argv[0], "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)
    return 1 if report["failures"] else 0


def main(argv: list[str] | None = None) -> int:
    _fix_std_streams()
    args = list(sys.argv[1:] if argv is None else argv)
    exe_name = os.path.basename(sys.executable if getattr(sys, "frozen", False) else sys.argv[0])
    if exe_name.lower().startswith(HOST_EXE_PREFIX):
        from micnaga.watcher.extension import host_main

        return host_main()
    if args and args[0] == "--selftest":
        return _run_selftest(args[1:])
    from micnaga.cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(main())
