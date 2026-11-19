"""Salida estructurada de ejemplo (la que devolveria el modelo) para la reunion sintetica."""

from __future__ import annotations

from typing import Any

CALLS: list[dict[str, Any]] = [
    {"tool": "buscar_empresas", "args": {"texto": "Dental Triana"}},
    {"tool": "buscar_contactos", "args": {"texto": "Marta Gil"}},
    {"tool": "buscar_oportunidades", "args": {"texto": "Triana"}},
    {"tool": "leer_oportunidad", "args": {"id": 205}},
    {"tool": "listar_etapas", "args": {}},
    {"tool": "buscar_en_reuniones", "args": {"texto": "Triana"}},
]


def structured(*, with_crm: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "titulo": "Dental Triana: alcance ampliado",
        "resumen": [
            "Dirección aprueba el panel de ventas en la fase 1.",
            "El total sube a unos 6.900 €.",
            "Demo el 3 de diciembre con datos reales.",
        ],
        "decisiones": [{"texto": "Panel de ventas en fase 1", "seg": 4}],
        "tareas": [
            {
                "texto": "Enviar presupuesto revisado",
                "responsable": "me",
                "fecha": "2026-11-20",
                "seg": 6,
            },
            {
                "texto": "Lista de plantillas de WhatsApp",
                "responsable": "S2",
                "fecha": "2026-11-24",
                "seg": 7,
            },
            {
                "texto": "Tarea con fecha mala",
                "responsable": "Pepe",
                "fecha": "el viernes",
                "seg": 999,
            },
        ],
        "preguntas_abiertas": [{"texto": "Respuestas fuera de horario", "seg": 10}],
        "proximos_pasos": [{"texto": "Demo el 3 de diciembre", "seg": 9}],
        "correcciones": [
            {
                "tipo": "hablante",
                "hablante": "S2",
                "de": None,
                "a": "Marta Gil",
                "seg": 2,
                "motivo": "Se presenta en 00:15",
                "confianza": 0.95,
            },
            {
                "tipo": "hablante",
                "hablante": "S1",
                "de": None,
                "a": "Carlos",
                "seg": 3,
                "motivo": "Javier le llama Carlos",
                "confianza": 0.9,
            },
            {
                "tipo": "hablante",
                "hablante": "me",
                "de": None,
                "a": "Javier",
                "seg": 1,
                "motivo": "no se toca",
                "confianza": 0.9,
            },
            {
                "tipo": "palabra",
                "hablante": None,
                "de": "Odu",
                "a": "Odoo",
                "seg": 8,
                "motivo": "producto",
                "confianza": 0.9,
            },
            {
                "tipo": "palabra",
                "hablante": None,
                "de": "Riana",
                "a": "Triana",
                "seg": 8,
                "motivo": "sede",
                "confianza": 0.8,
            },
            {
                "tipo": "palabra",
                "hablante": None,
                "de": "Zzyzx",
                "a": "Algo",
                "seg": 8,
                "motivo": "no aparece",
                "confianza": 0.8,
            },
            {
                "tipo": "palabra",
                "hablante": None,
                "de": "seis mil",
                "a": "6000",
                "seg": 5,
                "motivo": "cifras no",
                "confianza": 0.8,
            },
        ],
        "aprendizajes": [
            {
                "hecho": "Las reuniones con Dental Triana suelen incluir a recepción.",
                "fuente": "reunion",
                "evidencia": "#1",
                "confianza": 0.8,
            },
            {
                "hecho": "Marta usa marta@dentaltriana.es",
                "fuente": "reunion",
                "evidencia": "#2",
                "confianza": 0.9,
            },
        ],
    }
    if with_crm:
        data["propuestas"] = [
            {
                "tipo": "vincular_a_oportunidad",
                "titulo": "Vincular a CRM + WhatsApp · Triana",
                "motivo": "Hablan de su propuesta",
                "seg": 3,
                "confianza": 0.95,
                "datos": {"oportunidad_id": 205},
            },
            {
                "tipo": "actualizar_oportunidad",
                "titulo": "Subir a 6.900 € y pasar a Negociación",
                "motivo": "Aprueban el panel",
                "seg": 5,
                "confianza": 0.85,
                "datos": {
                    "oportunidad_id": 205,
                    "etapa_id": 3,
                    "importe": 6900,
                    "etapa_anterior": "Propuesta",
                    "importe_anterior": 5400,
                },
            },
            {
                "tipo": "crear_contacto",
                "titulo": "Crear contacto Marta Gil",
                "motivo": "No está en el CRM",
                "seg": 2,
                "confianza": 0.9,
                "datos": {
                    "nombre": "Marta Gil",
                    "email": "marta@dentaltriana.es",
                    "telefono": "600111222",
                    "cargo": "Recepción",
                    "empresa_id": 41,
                },
            },
            {
                "tipo": "crear_contacto",
                "titulo": "Crear contacto Carlos Ruiz Ortega",
                "motivo": "duplicado",
                "seg": 1,
                "confianza": 0.5,
                "datos": {"nombre": "Carlos Ruiz Ortega", "empresa_id": 41},
            },
            {
                "tipo": "programar_actividad",
                "titulo": "Enviar presupuesto revisado · vie 20",
                "motivo": "Lo dijo Javier",
                "seg": 6,
                "confianza": 0.9,
                "datos": {
                    "oportunidad_id": 205,
                    "resumen": "Enviar presupuesto revisado",
                    "fecha": "2026-11-20",
                },
            },
            {
                "tipo": "vincular_a_oportunidad",
                "titulo": "Vincular a una inventada",
                "motivo": "x",
                "seg": 1,
                "confianza": 0.9,
                "datos": {"oportunidad_id": 999},
            },
        ]
    return data
