"""Esquemas JSON de salida (``--json-schema``) y validacion local de lo que devuelve el modelo.

El modelo cita la **evidencia por indice de frase** (``seg``, el ``#n`` que ve en la
transcripcion) y los hablantes por **id** (``S2``, ``me``): Micnaga resuelve en local el instante
(``t``) y el nombre, asi renombrar un hablante no deja el acta obsoleta y no hay marcas de tiempo
inventadas. Las fechas deben ser ISO ``YYYY-MM-DD`` validas; si no, se descartan.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

PROPOSAL_TYPES = (
    "vincular_a_oportunidad",
    "crear_contacto",
    "actualizar_oportunidad",
    "programar_actividad",
    "nota_en_historial",
)
CORRECTION_TYPES = ("hablante", "palabra")
LEARN_SOURCES = ("crm", "reunion", "aprobacion")
MAX_SUMMARY = 5
MAX_ITEMS = 20

_ITEM = {
    "type": "object",
    "properties": {"texto": {"type": "string"}, "seg": {"type": ["integer", "null"]}},
    "required": ["texto", "seg"],
    "additionalProperties": False,
}

_DATOS = {
    "type": "object",
    "description": (
        "Datos de la propuesta. Usa SOLO ids que hayas leido con las herramientas. "
        "crear_contacto: nombre, email, telefono, cargo, empresa_id. "
        "vincular_a_oportunidad / nota_en_historial: oportunidad_id (o partner_id). "
        "actualizar_oportunidad: oportunidad_id, etapa_id, importe, fecha_cierre, "
        "etapa_anterior, importe_anterior. "
        "programar_actividad: oportunidad_id (o partner_id), resumen, fecha."
    ),
    "properties": {
        "oportunidad_id": {"type": ["integer", "null"]},
        "partner_id": {"type": ["integer", "null"]},
        "empresa_id": {"type": ["integer", "null"]},
        "nombre": {"type": ["string", "null"]},
        "email": {"type": ["string", "null"]},
        "telefono": {"type": ["string", "null"]},
        "cargo": {"type": ["string", "null"]},
        "etapa_id": {"type": ["integer", "null"]},
        "etapa_anterior": {"type": ["string", "null"]},
        "importe": {"type": ["number", "null"]},
        "importe_anterior": {"type": ["number", "null"]},
        "fecha_cierre": {"type": ["string", "null"]},
        "resumen": {"type": ["string", "null"]},
        "fecha": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}


def acta_schema(*, with_crm: bool) -> dict[str, Any]:
    """Esquema de la pasada unica (acta + correcciones [+ propuestas CRM] + aprendizajes)."""
    props: dict[str, Any] = {
        "titulo": {"type": "string", "description": "3-8 palabras, concreto, sin fecha"},
        "resumen": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_SUMMARY},
        "decisiones": {"type": "array", "items": _ITEM},
        "tareas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "texto": {"type": "string"},
                    "responsable": {
                        "type": ["string", "null"],
                        "description": "id de hablante (me, S1...) o nombre si no habla",
                    },
                    "fecha": {"type": ["string", "null"], "description": "YYYY-MM-DD resuelta"},
                    "seg": {"type": ["integer", "null"]},
                },
                "required": ["texto", "responsable", "fecha", "seg"],
                "additionalProperties": False,
            },
        },
        "preguntas_abiertas": {"type": "array", "items": _ITEM},
        "proximos_pasos": {"type": "array", "items": _ITEM},
        "correcciones": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string", "enum": list(CORRECTION_TYPES)},
                    "hablante": {"type": ["string", "null"], "description": "id (S1...)"},
                    "de": {"type": ["string", "null"], "description": "texto mal transcrito"},
                    "a": {"type": "string", "description": "nombre o forma correcta"},
                    "seg": {"type": ["integer", "null"]},
                    "motivo": {"type": "string"},
                    "confianza": {"type": "number"},
                },
                "required": ["tipo", "hablante", "de", "a", "seg", "motivo", "confianza"],
                "additionalProperties": False,
            },
        },
        "aprendizajes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "hecho": {"type": "string"},
                    "fuente": {"type": "string", "enum": list(LEARN_SOURCES)},
                    "evidencia": {"type": "string"},
                    "confianza": {"type": "number"},
                },
                "required": ["hecho", "fuente", "evidencia", "confianza"],
                "additionalProperties": False,
            },
        },
    }
    required = [
        "titulo",
        "resumen",
        "decisiones",
        "tareas",
        "preguntas_abiertas",
        "proximos_pasos",
        "correcciones",
        "aprendizajes",
    ]
    if with_crm:
        props["propuestas"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string", "enum": list(PROPOSAL_TYPES)},
                    "titulo": {"type": "string", "description": "una linea: verbo + objeto"},
                    "motivo": {"type": "string"},
                    "seg": {"type": ["integer", "null"]},
                    "confianza": {"type": "number"},
                    "datos": _DATOS,
                },
                "required": ["tipo", "titulo", "motivo", "seg", "confianza", "datos"],
                "additionalProperties": False,
            },
        }
        required.append("propuestas")
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


TITLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string", "description": "3-6 palabras"},
        "confianza": {"type": "number"},
    },
    "required": ["titulo", "confianza"],
    "additionalProperties": False,
}


# ============================================================================== validacion


def clean_text(value: object, max_len: int = 400) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:max_len]


def clean_title(value: object) -> str:
    text = clean_text(value, 90).strip(" .\"'«»")
    return text


def iso_date(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def seg_ref(value: object, n_segments: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value < n_segments else None


def confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def as_list(value: object, limit: int = MAX_ITEMS) -> list[Any]:
    return list(value)[:limit] if isinstance(value, list) else []
