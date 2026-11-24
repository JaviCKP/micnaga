"""Punto de entrada de los ejecutables congelados (PyInstaller).

Un solo script sirve para los tres ``.exe`` de la carpeta (comparten ``_internal``):

``micnaga.exe``       ventana (sin consola). Es el que usan el acceso directo y la clave ``Run``:
                      ``micnaga.exe app``. ``worker_command()`` lanza ``micnaga.exe worker``.
``micnaga-cli.exe``   consola: ``record``, ``process``, ``bench``, ``--version``, ``--selftest``.
``micnaga-host.exe``  host de *native messaging* de la extension del navegador (stdio). Se
                      reconoce por el nombre del ejecutable: Chrome lo llama solo con
                      ``chrome-extension://<id>/`` como argumento, asi que no admite subcomandos.

``--selftest [FICHERO]`` importa todos los modulos de ``micnaga`` y las librerias nativas que
usa, carga el VAD de la diarizacion (torch), calcula fbank con torchaudio, pinta los iconos de la
bandeja y sale con codigo distinto de 0 si algo falla (informe JSON a ``FICHERO`` o a stdout).
No abre ventanas, no toca el registro ni la red.

``--selftest-diarize WAV [N]`` diariza de verdad ``WAV`` (y extrae huellas) con el modelo WeSpeaker
de ``MICNAGA_MODELS``; ``N`` fija el numero de hablantes (la rama de clustering con varios).
Prueba manual de que el recorte de torch/sklearn del instalador no rompe nada.
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
    "soxr",
    "soundfile",
    "onnxruntime",
    "pyaudiowpatch",
    "win32api",
    "win32cred",
    "win32event",
    "win32timezone",
    "pycaw.pycaw",
    "comtypes",
    "psutil",
    "rapidfuzz",
    "tomli_w",
    "onnx_asr",
    "faster_whisper",
    "ctranslate2",
    "aec_audio_processing",
    "webview",
    "clr",  # pythonnet: lo carga el backend de pywebview al arrancar la primera ventana
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "pystray",
    "windows_toasts",
    "PIL.Image",
)

# Diarizacion (unico motor: diarize + torch CPU).
DIAR_IMPORTS = (
    "torch",
    "torchaudio",
    "torchaudio.compliance.kaldi",
    "diarize",
    "wespeakerruntime",
    "silero_vad",
    "sklearn.cluster",
    "sklearn.mixture",
)
# No deben viajar en el instalador (``aec.dll`` de pyaec no esta firmada; ver micnaga.spec).
BANNED_IMPORTS = ("pyaec",)

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


def _check_diarization_runtime() -> None:
    """Lo que la diarizacion necesita de torch sin modelos descargados: el VAD Silero (TorchScript,
    dentro del paquete ``silero_vad``) y las fbank de torchaudio que usa ``embed``."""
    import numpy as np
    import silero_vad
    import torch
    import torchaudio.compliance.kaldi as kaldi

    model = silero_vad.load_silero_vad()
    rng = np.random.default_rng(0)
    wave = torch.from_numpy(rng.standard_normal(16000).astype(np.float32) * 0.01)
    with torch.no_grad():
        prob = float(model(wave[:512], 16000))
    if not 0.0 <= prob <= 1.0:
        raise RuntimeError(f"silero probability out of range: {prob}")
    feats = kaldi.fbank(wave.unsqueeze(0) * 32768, num_mel_bins=80, sample_frequency=16000)
    if tuple(feats.shape)[1] != 80:
        raise RuntimeError(f"unexpected fbank shape {tuple(feats.shape)}")


def _check_tray_icons() -> None:
    """Iconos de la bandeja con el Pillow recortado: dibujar y guardar como ICO (pystray)."""
    from pystray._util import serialized_image

    from micnaga.ui import icons

    for state in ("watching", "recording", "paused", "processing"):
        img = icons.tray_icon(state)
    with serialized_image(img, "ICO") as path:
        if os.path.getsize(path) <= 0:
            raise RuntimeError("empty ICO")


def _run_selftest_diarize(argv: list[str]) -> int:
    """Diarizacion real de un WAV (prueba manual del ejecutable congelado)."""
    if not argv:
        print("uso: micnaga-cli.exe --selftest-diarize FICHERO.wav [HABLANTES]", file=sys.stderr)
        return 2
    import time

    import soundfile as sf

    from micnaga.config import Config
    from micnaga.pipeline.engines.diar import create_diarizer

    audio, sr = sf.read(argv[0], dtype="float32", always_2d=False)
    n_speakers = int(argv[1]) if len(argv) > 1 else None
    t0 = time.perf_counter()
    diarizer = create_diarizer(Config(), "diarize")
    try:
        turns = diarizer.diarize(audio, sr, n_speakers)
        embed = getattr(diarizer, "embed", None)
        vectors = embed(audio, sr, turns) if callable(embed) else {}
    finally:
        diarizer.close()
    report = {
        "seconds": round(len(audio) / sr, 1),
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "turns": len(turns),
        "speakers": sorted({t.speaker for t in turns}),
        "embeddings": {k: len(v) for k, v in vectors.items()},
    }
    print(json.dumps(report, indent=2))
    return 0 if turns else 1


def selftest() -> dict[str, object]:
    import importlib
    import importlib.util
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
    for name in THIRD_PARTY_IMPORTS + DIAR_IMPORTS:
        attempt(name)

    variant = "unknown"
    with contextlib.suppress(ImportError):
        variant = importlib.import_module("micnaga_variant").VARIANT  # generado por la build
    frozen = bool(getattr(sys, "frozen", False))
    for name in BANNED_IMPORTS:
        if frozen and importlib.util.find_spec(name) is not None:
            failures[f"banned-module-present: {name}"] = "must not ship"
    for check in (_check_diarization_runtime, _check_tray_icons):
        try:
            check()
            ok.append(check.__name__)
        except BaseException as exc:
            failures[check.__name__] = f"{type(exc).__name__}: {exc}"

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
    if args and args[0] == "--selftest-diarize":
        return _run_selftest_diarize(args[1:])
    from micnaga.cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(main())
