"""Dispositivos de Windows: puntos finales por defecto y tipo de salida (auriculares/altavoces).

Todo el acceso a COM (``comtypes``/``pycaw``) esta aqui y se importa **de forma perezosa**; la
logica pura (:func:`classify_output`, :func:`find_form_factor`) se prueba sin Windows.

Auriculares o altavoces (seccion 7)
-----------------------------------
Se lee ``PKEY_AudioEndpoint_FormFactor`` ({1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E}, pid 0) del
dispositivo de salida (``EndpointFormFactor``: 1 = Speakers, 3 = Headphones, 5 = Headset...):

* ``Headphones`` (3) o ``Headset`` (5) -> ``"headphones"``.
* ``Speakers`` (1) -> ``"speakers"``.
* Cualquier otro (HDMI, SPDIF, linea, desconocido) -> ``"unknown"``; el pipeline lo trata como
  altavoces.
* **Bluetooth manos libres.** Windows crea un punto final distinto para el perfil HFP/HSP
  («Auriculares (xxx Hands-Free AG Audio)», «Manos libres»). Suele traer ``Headset``/
  ``Headphones``, pero algunos controladores lo marcan como ``Speakers``; por eso, si el nombre
  del dispositivo contiene «hands-free» / «manos libres» / «headset» / «headphone» / «auricular»
  / «casco», cuenta como auriculares aunque el factor de forma diga otra cosa. Un altavoz
  Bluetooth en modo manos libres se clasificaria mal como auriculares (se acepta).
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Iterator, Mapping
from typing import Any

from micnaga.capture.sources import EndpointInfo, EndpointSnapshot
from micnaga.types import OutputKind

log = logging.getLogger(__name__)

# EDataFlow / ERole (mmdeviceapi.h)
E_RENDER = 0
E_CAPTURE = 1
E_CONSOLE = 0
E_MULTIMEDIA = 1
E_COMMUNICATIONS = 2

# EndpointFormFactor (mmdeviceapi.h)
FF_SPEAKERS = 1
FF_HEADPHONES = 3
FF_HEADSET = 5

FORM_FACTOR_GUID = "{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E}"
HEADSET_NAME_RE = re.compile(
    r"hands[\s-]?free|manos\s+libres|headset|headphone|auricular|casco", re.IGNORECASE
)


def classify_output(form_factor: int | None, name: str = "") -> OutputKind:
    """Tipo de salida a partir del factor de forma y del nombre (ver el docstring del modulo)."""
    if HEADSET_NAME_RE.search(name or ""):
        return "headphones"
    if form_factor in (FF_HEADPHONES, FF_HEADSET):
        return "headphones"
    if form_factor == FF_SPEAKERS:
        return "speakers"
    return "unknown"


def find_form_factor(properties: Mapping[str, Any]) -> int | None:
    """Extrae el factor de forma del diccionario de propiedades de ``pycaw.AudioDevice``.

    Las claves tienen la forma ``"{GUID} pid"``; se compara sin distinguir mayusculas. Un valor
    no entero (``"19:?"`` si el tipo de variante no se soporta) devuelve ``None``.
    """
    for key, value in properties.items():
        k = str(key).strip().upper()
        if k.startswith(FORM_FACTOR_GUID) and k.split()[-1] == "0":
            if isinstance(value, bool):
                return None
            if isinstance(value, int):
                return value
            return None
    return None


@contextlib.contextmanager
def com_initialized() -> Iterator[None]:
    """``CoInitialize`` en el hilo actual (obligatorio para usar comtypes fuera del principal)."""
    import comtypes

    ok = False
    try:
        comtypes.CoInitialize()  # type: ignore[no-untyped-call]
        ok = True
    except OSError:  # ya inicializado con otro modelo de hilos: se puede seguir usando
        pass
    try:
        yield
    finally:
        if ok:
            with contextlib.suppress(Exception):
                comtypes.CoUninitialize()  # type: ignore[no-untyped-call]


def _enumerator() -> Any:
    """Enumerador nuevo (barato). No se cachea: ``com_initialized`` puede cerrar COM entre
    llamadas y un puntero COM guardado dejaria de ser valido."""
    from pycaw.utils import AudioUtilities

    return AudioUtilities.GetDeviceEnumerator()


def default_endpoint_id(flow: int, role: int) -> str | None:
    """Id del dispositivo por defecto (barato: pensado para sondear cada 250 ms)."""
    try:
        dev = _enumerator().GetDefaultAudioEndpoint(flow, role)
        return str(dev.GetId()) if dev is not None else None
    except Exception:
        return None


def default_endpoint(flow: int, role: int) -> EndpointInfo | None:
    """Id, nombre y tipo (solo salidas) del dispositivo por defecto, o ``None`` si no hay."""
    from pycaw.utils import AudioUtilities

    try:
        raw = _enumerator().GetDefaultAudioEndpoint(flow, role)
    except Exception as exc:
        log.warning("no default endpoint (flow=%s role=%s): %s", flow, role, exc)
        return None
    if raw is None:
        return None
    dev = AudioUtilities.CreateDevice(raw)
    if dev is None:
        return None
    name = str(dev.FriendlyName or "")
    kind: OutputKind = "unknown"
    if flow == E_RENDER:
        kind = classify_output(find_form_factor(dev.properties), name)
    return EndpointInfo(id=str(dev.id), name=name, output_kind=kind)


def snapshot() -> EndpointSnapshot:
    """Salida por defecto (``eConsole``) y micro de comunicaciones (``eCommunications``)."""
    with com_initialized():
        return EndpointSnapshot(
            render=default_endpoint(E_RENDER, E_CONSOLE),
            capture=default_endpoint(E_CAPTURE, E_COMMUNICATIONS),
        )


def poll_ids() -> EndpointSnapshot:
    """Como :func:`snapshot` pero solo con los ids (sin nombres): el sondeo de 250 ms."""
    with com_initialized():
        r = default_endpoint_id(E_RENDER, E_CONSOLE)
        c = default_endpoint_id(E_CAPTURE, E_COMMUNICATIONS)
    return EndpointSnapshot(
        render=EndpointInfo(r, "") if r else None,
        capture=EndpointInfo(c, "") if c else None,
    )
