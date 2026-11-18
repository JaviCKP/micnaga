"""Tipos de datos del vigilante: lecturas de senales, pestanas de Meet y reunion detectada."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MonitorHint:
    """Monitor donde esta la ventana del navegador (para colocar el popup).

    Las coordenadas son de escritorio virtual (pueden ser negativas en monitores a la izquierda).
    """

    handle: int
    rect: tuple[int, int, int, int]  # left, top, right, bottom
    work_area: tuple[int, int, int, int]
    device: str = ""
    primary: bool = False


def meeting_key(code: str | None, browser: str | None) -> str:
    return code if code else f"nocode:{browser or ''}"


@dataclass(frozen=True)
class MicApp:
    """Una aplicacion que esta usando el micro (ruta generica, sin mirar Meet)."""

    key: str  # clave de identidad: "app:<exe en minusculas o PackageFamilyName>"
    label: str  # nombre amigable ("Teams", "Zoom", "navegador", "handy.exe")
    exe: str | None = None  # basename del exe (apps no empaquetadas)
    package: str | None = None  # PackageFamilyName (apps empaquetadas)
    is_browser: bool = False  # esta en ``detection.browsers``


@dataclass(frozen=True)
class MeetTab:
    """Una pestana de Meet vista en un navegador."""

    browser: str  # basename del exe en minusculas: "chrome.exe"
    title: str | None  # solo del titulo de VENTANA (pestana activa); las de UIA van sin titulo
    code: str | None  # "abc-defg-hij" si aparece
    raw_name: str = ""  # nombre completo de la pestana / ventana (para diagnostico)
    active: bool = False  # es la pestana activa de su ventana (viene del titulo de ventana)
    hwnd: int | None = None
    monitor: MonitorHint | None = None

    @property
    def key(self) -> str:
        """Identidad de la reunion: el codigo; sin codigo, una clave unica por navegador.

        El titulo NO entra en la clave: los nombres de pestana de UIA llevan decoraciones
        localizadas que no controlamos, y la misma pestana saldria con dos claves distintas.
        """
        return meeting_key(self.code, self.browser)


@dataclass(frozen=True)
class MeetingInfo:
    """Datos de la reunion que viajan al controlador (carpeta, JSON, popup)."""

    title: str | None = None
    code: str | None = None
    browser: str | None = None
    monitor: MonitorHint | None = None
    manual: bool = False
    generic_app: str | None = None  # clave "app:..." si la sesion nacio de la ruta generica

    @property
    def key(self) -> str:
        if self.generic_app:
            return self.generic_app
        return meeting_key(self.code, self.browser)

    @classmethod
    def from_tab(cls, tab: MeetTab) -> MeetingInfo:
        return cls(title=tab.title, code=tab.code, browser=tab.browser, monitor=tab.monitor)


@dataclass(frozen=True)
class SignalSnapshot:
    """Estado de todas las senales en un instante (lo que consume la maquina de estados)."""

    mic_browsers: frozenset[str] = frozenset()  # navegadores usando el micro
    meet_tabs: tuple[MeetTab, ...] = ()  # pestanas de Meet abiertas (solo se miran con micro)
    audio_out_browsers: frozenset[str] = frozenset()  # refuerzo: audio saliendo del navegador
    source: str = "native"  # "native" | "extension"
    mic_apps: frozenset[MicApp] = frozenset()  # TODAS las apps con el micro (ruta generica)

    def live_tabs(self) -> tuple[MeetTab, ...]:
        """Pestanas de Meet cuyo navegador esta usando el micro (las dos obligatorias a la vez)."""
        return tuple(t for t in self.meet_tabs if t.browser in self.mic_browsers)


@dataclass(frozen=True)
class SignalReading:
    """Resultado de diagnostico de una fuente (vista «diagnostico» de ajustes)."""

    name: str
    active: bool
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    cost_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "active": self.active,
            "detail": self.detail,
            "error": self.error,
            "cost_ms": round(self.cost_ms, 3),
        }
