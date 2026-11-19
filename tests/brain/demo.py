"""Reunion de negocio sintetica + acta de ejemplo (tests y capturas de la Secretaria)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from micnaga.types import EnginesInfo, Marker, MeetingInfo, Segment, Speaker, Transcript, Word

BUSINESS_LINES = [
    ("me", "Hola, buenos días. Gracias por conectaros."),
    ("S1", "Buenos días Javier. Te presento a Marta, que lleva la recepción de la clínica."),
    ("S2", "Hola, soy Marta Gil. Mi correo es marta@dentaltriana.es por si me mandas cosas."),
    ("me", "Perfecto Marta. Carlos, ¿cómo lo veis después de la propuesta?"),
    ("S1", "Dirección aprueba meter el panel de ventas en la fase uno."),
    ("S1", "El total sube a unos seis mil novecientos euros."),
    ("me", "Entonces te mando el presupuesto revisado antes del viernes."),
    ("S2", "Yo os paso la lista de plantillas de WhatsApp el martes."),
    ("S1", "Queremos que esté integrado con Odu y con la sede de Riana."),
    ("me", "Hacemos la demo el tres de diciembre con datos reales."),
    ("S1", "¿Y las respuestas de pacientes fuera de horario cómo quedan?"),
    ("me", "Lo miro y os digo. Gracias a los dos."),
]


def business_transcript(
    start: str = "2026-11-19T10:30:00+01:00", *, title: str = "Reunión", spacing: float = 1.5
) -> Transcript:
    segs = []
    t = 2.0
    for spk, text in BUSINESS_LINES:
        words = []
        wt = t
        for w in text.split():
            p = 0.3 if w.strip(".,") in ("Odu", "Riana") else 0.95
            words.append(Word(w, round(wt, 2), round(wt + 0.3, 2), p))
            wt += 0.35
        segs.append(Segment(round(t, 2), round(wt, 2), spk, text, words))
        t = wt + spacing
    return Transcript(
        meeting=MeetingInfo(title=title, start=start, duration_s=round(t, 1)),
        speakers=[Speaker("me", "Yo"), Speaker("S1", "Hablante 1"), Speaker("S2", "Hablante 2")],
        segments=segs,
        markers=[Marker(20.0, "precio")],
        engines=EnginesInfo(asr="parakeet"),
    )


def write_demo_meeting(root: Path) -> str:
    """Reunion «Dental Triana» con su acta y sus propuestas comprobadas contra un Odoo falso."""
    from micnaga.brain import PROMPT_VERSION, plan
    from micnaga.brain import acta as acta_io
    from micnaga.brain import store as bstore
    from micnaga.brain.odoo import OdooClient, OdooConfig
    from micnaga.pipeline import output
    from micnaga.store.db import Database
    from tests.brain import canned
    from tests.brain.fake_odoo import DB, KEY, LOGIN, FakeOdoo

    start = (datetime.now().astimezone() - timedelta(hours=1)).replace(microsecond=0)
    tr = business_transcript(
        start.isoformat(), title="Dental Triana: alcance ampliado", spacing=24.0
    )
    name = f"{start:%Y-%m-%d_%H%M}_dental-triana-alcance-ampliado"
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    output.write_transcript(folder, tr)
    data = canned.structured()
    data["tareas"] = [t for t in data["tareas"] if t["responsable"] != "Pepe"]
    srv = FakeOdoo().start()
    try:
        client = OdooClient(OdooConfig(srv.url, DB, LOGIN, KEY))
        client.authenticate()
        acta = plan.build_acta(data, tr)
        props = plan.build_corrections(data["correcciones"], tr)
        props += plan.build_crm(data["propuestas"], tr, client)
    finally:
        srv.stop()
    plan.number(props)
    acta.update(
        version=1,
        prompt_version=PROMPT_VERSION,
        model="sonnet",
        propuestas=props,
        odoo={"estado": "conectado"},
        cost_usd=0.05,
        duration_s=21.0,
    )
    acta_io.write_acta(folder, acta)
    with Database(root / "micnaga.db") as db:
        output.index_meeting(db, folder, tr)
        bstore.ensure_schema(db)
        bstore.index_acta(db, name, acta["titulo"], acta_io.fts_text(acta))
        bstore.enqueue(db, name)
        bstore.finish(db, name, proposals=acta_io.pending_count(acta))
    return name


FOLLOWUP_LINES = [
    ("me", "Buenas. ¿Cómo van las sesiones con los técnicos del ayuntamiento?"),
    ("S1", "Bien, pero piden más ejercicios prácticos para la próxima semana."),
    ("me", "Vale, preparo un cuaderno de ejercicios para el viernes."),
    ("S1", "¿Nos vemos el martes a las diez para revisar los materiales?"),
    ("me", "Hecho. Y apúntame dos horas hoy a Atlas, que estuve con la migración."),
    ("me", "Ah, y tengo que fichar los días hasta hoy, llevo desde el día cuatro sin apuntar."),
]


def write_followup_meeting(root: Path, client: object, profile: dict[str, object]) -> str:
    """Reunion de seguimiento con propuestas de negocio ya comprobadas contra el Odoo falso:
    nota en una tarea, tarea nueva, evento (invitados apagados) y horas pedidas."""
    from datetime import date

    from micnaga.brain import PROMPT_VERSION, plan
    from micnaga.brain import acta as acta_io
    from micnaga.brain import store as bstore
    from micnaga.pipeline import output
    from micnaga.store.db import Database
    from tests.brain.fake_odoo import PROJECT_ATLAS, PROJECT_TRAINING

    start = (datetime.now().astimezone() - timedelta(minutes=40)).replace(microsecond=0)
    segs = []
    t = 3.0
    for spk, text in FOLLOWUP_LINES:
        segs.append(Segment(t, t + 6.0, spk, text, []))
        t += 22.0
    tr = Transcript(
        meeting=MeetingInfo(
            title="Seguimiento de la formación", start=start.isoformat(), duration_s=t
        ),
        speakers=[Speaker("me", "Yo"), Speaker("S1", "Lucía Pérez")],
        segments=segs,
        markers=[],
        engines=EnginesInfo(asr="parakeet"),
    )
    name = f"{start:%Y-%m-%d_%H%M}_seguimiento-de-la-formacion"
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    output.write_transcript(folder, tr)
    today = date.today()
    missing = ["2026-11-05", "2026-11-06", "2026-11-09"]
    raw: list[dict[str, object]] = [
        {"tipo": "nota_en_historial", "datos": {"tarea_id": 300}, "seg": 0},
        {
            "tipo": "crear_tarea",
            "datos": {
                "proyecto_id": PROJECT_TRAINING,
                "nombre": "Cuaderno de ejercicios prácticos",
                "fecha": "2026-11-20",
            },
            "seg": 2,
        },
        {
            "tipo": "crear_evento",
            "datos": {
                "nombre": "Revisión de materiales",
                "fecha": "2026-11-24",
                "hora_inicio": "10:00",
                "duracion_h": 1,
                "asistentes_ids": [58],
            },
            "seg": 3,
        },
        {
            "tipo": "registrar_horas",
            "datos": {
                "proyecto_id": PROJECT_ATLAS,
                "fechas": [today.isoformat()],
                "horas": 2,
                "descripcion": "Migración de informes",
                "explicito": True,
            },
            "seg": 4,
        },
        *(
            {
                "tipo": "registrar_horas",
                "datos": {"proyecto_id": pid, "fechas": missing, "horas": h, "descripcion": d},
                "seg": 5,
            }
            for pid, h, d in (
                (PROJECT_TRAINING, 4.5, "Formación a técnicos (9:00-13:30)"),
                (PROJECT_TRAINING, 0.5, "Reunión de seguimiento (13:30-14:00)"),
                (PROJECT_TRAINING, 2.0, "Mejora de materiales (15:00-17:00)"),
                (PROJECT_ATLAS, 1.0, "Desarrollo de Atlas"),
            )
        ),
    ]
    for r in raw:
        r.update(titulo=str(r["tipo"]), motivo="Lo dijo en la reunión", confianza=0.9)
    props = plan.build_crm(raw, tr, client, profile=profile, today=today)
    plan.number(props)
    acta = {
        "titulo": tr.meeting.title,
        "resumen": [
            "Las sesiones van bien; piden más ejercicios prácticos.",
            "Revisión de materiales el martes a las 10:00.",
        ],
        "decisiones": [],
        "tareas": [
            {
                "texto": "Cuaderno de ejercicios prácticos",
                "responsable_id": "me",
                "responsable": "Yo",
                "es_mia": True,
                "fecha": "2026-11-20",
                "seg": 2,
                "t": segs[2].start,
            }
        ],
        "preguntas_abiertas": [],
        "proximos_pasos": [{"texto": "Revisión de materiales", "seg": 3, "t": segs[3].start}],
        "version": 1,
        "prompt_version": PROMPT_VERSION,
        "model": "sonnet",
        "propuestas": props,
        "odoo": {"estado": "conectado"},
    }
    acta_io.write_acta(folder, acta)
    with Database(root / "micnaga.db") as db:
        output.index_meeting(db, folder, tr)
        bstore.ensure_schema(db)
        bstore.index_acta(db, name, acta["titulo"], acta_io.fts_text(acta))
        bstore.enqueue(db, name)
        bstore.finish(db, name, proposals=acta_io.pending_count(acta))
    return name
