"""Sustituto de PyAV incluido en el instalador de Micnaga.

faster-whisper hace ``import av`` al importarse, pero solo lo usa para decodificar ficheros de
audio (``faster_whisper.audio.decode_audio``). Micnaga siempre le pasa arrays de numpy, asi que
PyAV (y su FFmpeg con libx264/libx265, licencia GPL) no se distribuye. Cualquier uso real de PyAV
falla con un mensaje claro.
"""

__version__ = "0.0-micnaga-stub"


class PyAVNotBundledError(AttributeError):
    """AttributeError: ``hasattr``/``getattr(.., None)`` siguen funcionando."""


def __getattr__(name: str) -> object:
    if name.startswith("__"):
        raise AttributeError(name)
    raise PyAVNotBundledError(
        f"PyAV no esta incluido en Micnaga (se pidio av.{name}); faster-whisper solo recibe arrays"
    )
