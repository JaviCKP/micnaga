"""Interfaces estrechas entre la UI y el resto de la app.

La UI no importa ``capture``, ``watcher`` ni ``pipeline``: el integrador (``app.py``) les pasa
funciones que cumplen estos contratos. Todo es opcional y tiene un no-op por defecto, de modo
que cada superficie funciona sola (modo demo, tests).

Convenciones
------------
* **Niveles**: ``LevelsProvider() -> (mic, sistema)`` con amplitud *lineal* 0..1 (pico o RMS).
  La conversion a escala perceptual (dBFS) se hace en la UI. Se tolera que el proveedor
  devuelva un objeto con ``.mic``/``.system`` o un ``dict``; ver ``normalize_levels``.
* **Monitor**: ``MonitorHint`` es un rectangulo ``(izq, arriba, der, abajo)`` en pixeles fisicos
  (como ``win32gui.GetWindowRect`` del navegador), un ``hwnd`` entero, o ``None`` (monitor
  del cursor).
* Todas las llamadas a ``UiHost`` son seguras desde cualquier hilo; los callbacks se invocan
  desde hilos de pywebview (nunca desde el hilo de la GUI en el caso de las llamadas JS).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

from micnaga.config import Config

Rect: TypeAlias = tuple[int, int, int, int]
MonitorHint: TypeAlias = Rect | int | None
LevelsProvider: TypeAlias = Callable[[], object]

TrayState: TypeAlias = Literal["watching", "recording", "paused", "processing"]
PopupDecision: TypeAlias = Literal["record", "dismiss", "timeout"]
EndDecision: TypeAlias = Literal["finish", "keep", "save", "discard"]
PopupClosed: TypeAlias = Literal["morphed", "leaving", "fading"]


def normalize_levels(raw: object) -> tuple[float, float]:
    """Acepta ``(mic, sys)``, ``[mic, sys]``, objeto con ``.mic/.system`` o dict -> floats 0..1."""

    def clamp(v: object) -> float:
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
        if f != f:  # NaN
            return 0.0
        return max(0.0, min(1.0, f))

    if isinstance(raw, Mapping):
        return clamp(raw.get("mic", 0.0)), clamp(raw.get("system", raw.get("sys", 0.0)))
    if isinstance(raw, (tuple, list)) and len(raw) >= 2:
        return clamp(raw[0]), clamp(raw[1])
    mic = getattr(raw, "mic", None)
    sys_ = getattr(raw, "system", getattr(raw, "sys", None))
    if mic is not None or sys_ is not None:
        return clamp(mic), clamp(sys_)
    return 0.0, 0.0


def _noop(*_args: object, **_kwargs: object) -> None:
    return None


@dataclass
class UiCallbacks:
    """Lo que la UI notifica al resto de la app. Todos opcionales (no-op por defecto)."""

    # popup (seccion 6)
    on_popup_decision: Callable[[PopupDecision], None] = _noop
    """El usuario eligio (o expiro el tiempo): ``record`` | ``dismiss`` | ``timeout``."""
    on_popup_hover: Callable[[bool], None] = _noop
    """Raton encima: la cuenta atras se pausa. Para que el vigilante pause su propio reloj."""
    on_popup_closed: Callable[[str], None] = _noop
    on_end_decision: Callable[[EndDecision], None] = _noop
    """Popup de fin: ``finish``/``keep`` (confirmada) o ``save``/``discard`` (sin confirmar)."""
    on_undo: Callable[[], None] = _noop
    """«Deshacer» del aviso «Descartada»."""
    # pastilla (MIC-10/12/13)
    on_marker: Callable[[], None] = _noop
    on_pause_toggle: Callable[[bool], None] = _noop
    """``True`` = el usuario pide pausa privada; ``False`` = reanudar."""
    on_stop: Callable[[], None] = _noop
    on_pill_moved: Callable[[int, int], None] = _noop
    # bandeja (seccion 10)
    on_record_now: Callable[[], None] = _noop
    on_pause_watching: Callable[[int], None] = _noop
    """Minutos de vigilancia en pausa (60)."""
    on_resume_watching: Callable[[], None] = _noop
    on_open_library: Callable[[], None] = _noop
    on_open_settings: Callable[[], None] = _noop
    on_quit: Callable[[], None] = _noop
    # notificacion «Lista»
    on_open_meeting: Callable[[str], None] = _noop
    """Carpeta de la reunion cuyo toast se pulso (``Abrir``)."""
    # ajustes / asistente
    on_settings_saved: Callable[[Any], None] = _noop
    on_first_run_done: Callable[[], None] = _noop
    on_levels_monitor: Callable[[bool], None] = _noop
    """El asistente (paso 3) pide abrir/cerrar los dispositivos solo para medir niveles."""


@dataclass(frozen=True)
class Voiceprint:
    """Huella de voz guardada (MIC-11). Se identifica por nombre de persona."""

    name: str
    created: str = ""
    meetings: int = 0


@runtime_checkable
class LibraryOps(Protocol):
    """Operaciones de biblioteca que implementa ``pipeline`` (la UI solo las invoca).

    Los nombres coinciden con los que expone el agente de pipeline. ``folder`` es el nombre de
    la carpeta de la reunion (relativo a la raiz de reuniones).
    """

    def rename_speaker(self, folder: str, speaker_id: str, name: str) -> None: ...

    def merge_speakers(self, folder: str, keep_id: str, merge_id: str) -> None: ...

    def regenerate_from_json(self, folder: str) -> None: ...

    def list_voiceprints(self) -> list[Voiceprint]: ...

    def save_voiceprint(self, folder: str, speaker_id: str) -> None: ...

    def delete_voiceprint(self, name: str) -> None: ...


class HeldOps(Protocol):
    """«Descartadas» (audio apartado sin transcribir): listar, recuperar (se transcribe como
    reunion confirmada) y borrar ya. Lo implementa la integracion."""

    def list_held(self) -> list[dict[str, Any]]: ...

    def recover_held(self, name: str) -> None: ...

    def delete_held(self, name: str) -> None: ...


ModelState: TypeAlias = Literal[
    "pending", "downloading", "verifying", "verified", "failed", "skipped"
]


@dataclass(frozen=True)
class ModelProgress:
    """Evento de progreso de ``download_models`` (el integrador adapta ``micnaga.models``)."""

    name: str
    state: ModelState
    done: int = 0
    total: int = 0
    """Bytes (``total`` 0 = desconocido)."""
    message: str = ""


DownloadModels: TypeAlias = Callable[[Callable[[ModelProgress], None]], None]
"""``download_models(progress_cb)`` bloquea hasta terminar; lanza si falla algo."""


@dataclass(frozen=True)
class Diagnostic:
    """Una senal del vigilante (panel «Diagnostico» de Ajustes)."""

    label: str
    state: Literal["ok", "warn", "off", "fail"]
    detail: str = ""


DiagnosticsProvider: TypeAlias = Callable[[], list[Diagnostic]]


@dataclass
class AudioCheck:
    """Resultado de la comprobacion de micro/sistema del asistente (paso 3)."""

    mic_ok: bool = False
    system_ok: bool = False
    details: dict[str, str] = field(default_factory=dict)


class ConfigStore(Protocol):
    """Lectura/escritura de ``config.toml`` (inyectable para no tocar el real)."""

    def load(self) -> Config: ...

    def save(self, config: Config) -> None: ...
