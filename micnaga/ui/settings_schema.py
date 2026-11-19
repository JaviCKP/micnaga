"""Esquema del formulario de Ajustes, derivado de los dataclasses de ``micnaga.config``.

Cada campo de ``Config`` debe estar aqui (hay un test que lo comprueba) y tener su etiqueta y su
ayuda en ``es.json`` (``settings.f.<seccion>.<clave>`` y ``.help``), asi «todas las opciones de
config.toml» tiene demostracion automatica.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Literal

from micnaga.config import Config

Kind = Literal["bool", "int", "float", "text", "choice", "list", "hotkey", "path"]


@dataclass(frozen=True)
class FieldSpec:
    path: str
    kind: Kind
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str = ""
    advanced: bool = False
    restart: bool = False
    """El cambio solo se aplica al reiniciar la app (se muestra una nota)."""

    @property
    def section(self) -> str:
        return self.path.split(".", 1)[0]

    @property
    def key(self) -> str:
        return self.path.split(".", 1)[1]


def _f(path: str, kind: Kind, **kw: Any) -> FieldSpec:
    return FieldSpec(path, kind, **kw)


#: Grupos (pestañas) de Ajustes, en orden. Cada uno lista las rutas de sus campos.
GROUPS: tuple[tuple[str, tuple[FieldSpec, ...]], ...] = (
    (
        "general",
        (
            _f("startup.run_at_login", "bool"),
            _f("ui.theme", "choice", choices=("system", "light", "dark")),
            _f("ui.language", "choice", choices=("es",)),
            _f("ui.sound", "bool"),
            _f("hotkeys.marker", "hotkey", restart=True),
            _f("hotkeys.private_pause", "hotkey", restart=True),
        ),
    ),
    (
        "detection",
        (
            _f("detection.browsers", "list"),
            _f("detection.confirm_after_s", "float", minimum=1, maximum=60, step=1, unit="s"),
            _f("detection.popup_timeout_s", "float", minimum=5, maximum=120, step=1, unit="s"),
            _f("detection.on_timeout", "choice", choices=("provisional", "discard")),
            _f("detection.generic_apps", "bool"),
            _f("detection.exclude_apps", "list"),
            _f("detection.mic_grace_s", "float", minimum=0, maximum=300, step=1, unit="s"),
            _f(
                "detection.popup_after_recording_s",
                "float",
                minimum=0,
                maximum=300,
                step=1,
                unit="s",
            ),
            _f("detection.end_confirm_s", "float", minimum=0.5, maximum=30, step=0.5, unit="s"),
            _f(
                "detection.end_popup_timeout_s",
                "float",
                minimum=3,
                maximum=120,
                step=1,
                unit="s",
            ),
            _f("detection.undo_s", "float", minimum=3, maximum=60, step=1, unit="s"),
            _f(
                "detection.end_after_s",
                "float",
                minimum=3,
                maximum=120,
                step=1,
                unit="s",
                advanced=True,
            ),
            _f(
                "detection.poll_interval_s",
                "float",
                minimum=0.25,
                maximum=10,
                step=0.25,
                unit="s",
                advanced=True,
            ),
            _f("gates.min_duration_s", "float", minimum=0, maximum=3600, step=10, unit="s"),
            _f(
                "gates.min_remote_speech_s",
                "float",
                minimum=0,
                maximum=600,
                step=5,
                unit="s",
                advanced=True,
            ),
            _f("gates.min_turns", "int", minimum=0, maximum=100, step=1, advanced=True),
            _f("gates.require_confirmed", "bool", advanced=True),
        ),
    ),
    (
        "processing",
        (
            _f("processing.asr_engine", "choice", choices=("parakeet", "whisper")),
            _f("processing.whisper_model", "choice", choices=("tiny", "base", "small", "medium")),
            _f("processing.hq_transcribe", "bool", advanced=True),
            _f(
                "processing.hq_model",
                "choice",
                choices=("large-v3", "large-v3-turbo"),
                advanced=True,
            ),
            _f("processing.diarization", "choice", choices=("onnx", "diarize", "pyannote")),
            _f("processing.aec", "choice", choices=("auto", "webrtc", "speex", "off")),
            _f("processing.threads", "int", minimum=1, maximum=32, step=1),
            _f("processing.low_priority", "bool"),
            _f("processing.live", "bool"),
            _f("processing.glossary", "list"),
            _f(
                "processing.echo_similarity",
                "float",
                minimum=0,
                maximum=1,
                step=0.05,
                advanced=True,
            ),
            _f(
                "processing.merge_gap_s",
                "float",
                minimum=0,
                maximum=10,
                step=0.1,
                unit="s",
                advanced=True,
            ),
            _f(
                "vad.min_speech_ms",
                "int",
                minimum=50,
                maximum=2000,
                step=10,
                unit="ms",
                advanced=True,
            ),
            _f(
                "vad.min_silence_ms",
                "int",
                minimum=50,
                maximum=3000,
                step=10,
                unit="ms",
                advanced=True,
            ),
            _f("vad.pad_ms", "int", minimum=0, maximum=1000, step=10, unit="ms", advanced=True),
            _f("vad.max_block_s", "float", minimum=5, maximum=120, step=1, unit="s", advanced=True),
        ),
    ),
    (
        "storage",
        (
            _f("storage.root", "path"),
            _f("storage.models_dir", "path", advanced=True),
            _f("storage.discarded_keep_days", "int", minimum=1, maximum=90, step=1, unit="d"),
            _f(
                "capture.sample_rate",
                "int",
                minimum=8000,
                maximum=48000,
                step=1000,
                unit="Hz",
                advanced=True,
                restart=True,
            ),
            _f(
                "capture.chunk_flush_s",
                "float",
                minimum=0.5,
                maximum=10,
                step=0.5,
                unit="s",
                advanced=True,
            ),
            _f(
                "capture.sync_log_interval_s",
                "float",
                minimum=1,
                maximum=60,
                step=1,
                unit="s",
                advanced=True,
            ),
            _f("capture.flac_level", "int", minimum=0, maximum=8, step=1, advanced=True),
        ),
    ),
    (
        "secretaria",
        (
            _f("secretaria.enabled", "bool"),
            _f("secretaria.auth", "choice", choices=("session", "token", "api_key")),
            _f("secretaria.odoo_url", "text"),
            _f("secretaria.odoo_db", "text"),
            _f("secretaria.odoo_login", "text"),
            _f("secretaria.clean_transcript", "bool"),
            _f("secretaria.horas_nudge", "bool"),
            _f("secretaria.live_title", "bool", advanced=True),
            _f(
                "secretaria.model_acta",
                "choice",
                choices=("sonnet", "opus", "haiku"),
                advanced=True,
            ),
            _f("secretaria.model_title", "choice", choices=("haiku", "sonnet"), advanced=True),
            _f("secretaria.mcp_servers", "list", advanced=True),
            _f("secretaria.google_gmail", "bool"),
            _f("secretaria.google_calendar", "bool"),
            _f("secretaria.google_drive", "bool"),
            _f("secretaria.google_email_mode", "choice", choices=("draft", "off"), advanced=True),
            _f("secretaria.google_calendar_id", "text", advanced=True),
            _f("secretaria.google_drive_folder_id", "text", advanced=True),
        ),
    ),
    (
        "privacy",
        (
            _f("privacy.remind_notice", "bool"),
            _f("privacy.voiceprints_enabled", "bool"),
            _f("privacy.voiceprint_threshold", "float", minimum=0, maximum=1, step=0.01),
        ),
    ),
)

FIELDS: dict[str, FieldSpec] = {f.path: f for _g, fields in GROUPS for f in fields}


INTERNAL_SECTIONS = frozenset({"meta"})
"""Secciones de ``config.toml`` que no son ajustes (``[meta] version``)."""


def config_paths() -> list[str]:
    """Todas las rutas ``seccion.clave`` de ``Config`` (la fuente de verdad)."""
    cfg = Config()
    out: list[str] = []
    for sec in dataclasses.fields(cfg):
        if sec.name in INTERNAL_SECTIONS:
            continue
        section = getattr(cfg, sec.name)
        out.extend(f"{sec.name}.{f.name}" for f in dataclasses.fields(section))
    return out


def schema_for_js() -> list[dict[str, Any]]:
    """Descripcion serializable para el formulario (grupos -> campos)."""
    groups: list[dict[str, Any]] = []
    for gid, fields in GROUPS:
        groups.append(
            {
                "id": gid,
                "fields": [
                    {
                        "path": f.path,
                        "kind": f.kind,
                        "choices": list(f.choices),
                        "min": f.minimum,
                        "max": f.maximum,
                        "step": f.step,
                        "unit": f.unit,
                        "advanced": f.advanced,
                        "restart": f.restart,
                    }
                    for f in fields
                ],
            }
        )
    return groups


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Mezcla ``patch`` sobre ``base`` seccion a seccion (devuelve una copia)."""
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out
