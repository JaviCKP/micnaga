"""Sustituto de kaldiio incluido en la variante «full» del instalador de Micnaga.

La licencia de kaldiio (NTT, «Software License Agreement for Evaluation») prohibe redistribuirlo.
Solo lo importa ``wespeakerruntime`` y solo lo usa en ``extract_embedding_kaldiio``, que Micnaga no
llama (calcula las fbank con torchaudio y usa el modelo ONNX directamente). Cualquier uso real falla
con un mensaje claro.
"""

__version__ = "0.0-micnaga-stub"


class KaldiioNotBundledError(AttributeError):
    """AttributeError: ``hasattr``/``getattr(.., None)`` siguen funcionando."""


def __getattr__(name: str) -> object:
    if name.startswith("__"):
        raise AttributeError(name)
    raise KaldiioNotBundledError(
        f"kaldiio no se distribuye con Micnaga (licencia de evaluacion); se pidio kaldiio.{name}"
    )
