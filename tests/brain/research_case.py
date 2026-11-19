"""Caso sintetico de «investigar primero»: una reunion ANTERIOR con su acta (dos pendientes) y la
SEGUNDA reunion con el mismo cliente, en la que uno se da por hecho y sale una accion nueva.

Datos inventados (Clinica Dental Triana del Odoo falso). Lo usan las pruebas con el ``claude``
falso y la evaluacion con el ``claude -p`` real (``test_real_research.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from micnaga.brain import acta as acta_io
from micnaga.types import EnginesInfo, MeetingInfo, Segment, Speaker, Transcript
from tests.brain.conftest import write_meeting

PRIOR = "2026-11-12_1000_dental_triana_plantillas"
CURRENT = "2026-11-19_1030_reunion"
PROJECT_DENTAL = 8
TASK_TEMPLATES = 302
MAIL_URL = "https://mail.google.com/mail/u/0/#all/stub-thread-presupuesto"

PRIOR_LINES = [
    ("me", "Buenos días Carlos, empezamos con lo de los recordatorios."),
    ("S1", "Hola. Queremos recordatorios de cita por WhatsApp y el presupuesto revisado."),
    ("me", "Te envío el presupuesto revisado el viernes 14."),
    ("me", "Y configuro las plantillas de recordatorio de WhatsApp antes del 21."),
    ("S1", "Perfecto, hablamos la semana que viene."),
]
CURRENT_LINES = [
    ("me", "Hola Carlos, retomamos lo que vimos el día 12."),
    ("S1", "Sí. El presupuesto revisado me llegó el viernes y ya lo aprobó dirección."),
    ("me", "Genial. Las plantillas de recordatorio de WhatsApp las tengo a medias todavía."),
    ("S1", "Vale. Otra cosa: la recepcionista necesita acceso al panel de citas."),
    ("me", "Te preparo un acceso de prueba al panel para recepción para el viernes 21."),
    ("S1", "Estupendo, gracias."),
]


def transcript(lines: list[tuple[str, str]], start: str, *, s1: str = "Carlos Ruiz") -> Transcript:
    segs, t = [], 1.0
    for spk, text in lines:
        segs.append(Segment(t, t + 4, spk, text))
        t += 5
    return Transcript(
        meeting=MeetingInfo(title="Reunión", start=start, duration_s=t),
        speakers=[Speaker("me", "Yo"), Speaker("S1", s1)],
        segments=segs,
        markers=[],
        engines=EnginesInfo(asr="parakeet"),
    )


PRIOR_ACTA: dict[str, Any] = {
    "version": 1,
    "titulo": "Dental Triana · recordatorios por WhatsApp",
    "created": "2026-11-12T10:20:00+01:00",
    "resumen": ["Carlos quiere recordatorios de cita por WhatsApp y el presupuesto revisado."],
    "decisiones": [{"texto": "Recordatorios por WhatsApp", "seg": 1, "t": 6.0}],
    "tareas": [
        {
            "texto": "Enviar el presupuesto revisado a Carlos",
            "responsable_id": "me",
            "responsable": "Yo",
            "es_mia": True,
            "fecha": "2026-11-14",
            "seg": 2,
            "t": 11.0,
        },
        {
            "texto": "Configurar las plantillas de recordatorio de WhatsApp",
            "responsable_id": "me",
            "responsable": "Yo",
            "es_mia": True,
            "fecha": "2026-11-21",
            "seg": 3,
            "t": 16.0,
        },
    ],
    "preguntas_abiertas": [],
    "proximos_pasos": [{"texto": "Hablar la semana que viene", "seg": 4, "t": 21.0}],
    "propuestas": [
        {
            "id": "p1",
            "grupo": "agenda",
            "tipo": "crear_tarea",
            "titulo": "Configurar plantillas de recordatorio WhatsApp",
            "estado": "aplicada",
            "datos": {"nombre": "Configurar plantillas de recordatorio WhatsApp"},
            "deshacer": {"modelo": "project.task", "creado": TASK_TEMPLATES},
        }
    ],
}


def write_library(*, named: bool = True) -> tuple[Path, Path]:
    """Las dos reuniones en la biblioteca de prueba; la anterior con su acta. ``named=False``:
    en la actual el cliente aun es «Hablante 1» (lo normal al analizar)."""
    prior = write_meeting(transcript(PRIOR_LINES, "2026-11-12T10:00:00+01:00"), PRIOR)
    acta_io.write_acta(prior, PRIOR_ACTA)
    s1 = "Carlos Ruiz" if named else "Hablante 1"
    current = write_meeting(transcript(CURRENT_LINES, "2026-11-19T10:30:00+01:00", s1=s1), CURRENT)
    return prior, current


def seed_odoo(fake: Any) -> None:
    """Proyecto del cliente con la tarea de las plantillas ABIERTA (la que no se debe duplicar)."""
    fake.data["project.project"].append(
        {
            "id": PROJECT_DENTAL,
            "name": "Dental Triana · Recordatorios",
            "active": True,
            "allow_timesheets": True,
            "partner_id": 41,
        }
    )
    fake.data["project.task"].append(
        {
            "id": TASK_TEMPLATES,
            "name": "Configurar plantillas de recordatorio WhatsApp",
            "project_id": PROJECT_DENTAL,
            "stage_id": False,
            "state": "01_in_progress",
            "date_deadline": "2026-11-21",
            "user_ids": [7],
        }
    )


def mail_stub_spec(python: str) -> str:
    """Linea de ``secretaria.mcp_servers`` para el correo falso (stdio, solo lectura)."""
    here = Path(__file__).resolve().parent / "stub_mail_mcp.py"
    return json.dumps(
        {
            "name": "correo",
            "command": python,
            "args": [str(here)],
            "tools": ["search_threads", "get_thread"],
        }
    )
