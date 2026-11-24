"""Consultas de ejemplo que Claude puede mostrar como acciones del conector."""

from __future__ import annotations

import json
from typing import Any

_PROMPTS = {
    "revisar_acta": (
        "Revisar un acta",
        "Contrasta el acta con la transcripción y detecta omisiones y errores.",
        "Lee el acta y comprueba las decisiones, responsables, fechas y compromisos en "
        "la transcripción. Propón correcciones concretas para las diferencias. Distingue "
        "los acuerdos de las sugerencias y las dudas de los errores demostrados. Si el "
        "acta todavía no existe, reconstruye los acuerdos a partir de la transcripción.",
    ),
    "preparar_reunion": (
        "Preparar la próxima reunión",
        "Recupera compromisos, decisiones y preguntas para una reunión próxima.",
        "Consulta hasta tres reuniones recientes de la referencia. Prepara un briefing "
        "de cinco minutos con objetivos, compromisos vigentes, preguntas abiertas y "
        "puntos a confirmar. Agrupa el resultado por prioridad.",
    ),
    "seguir_acuerdos": (
        "Seguir acuerdos entre reuniones",
        "Reconstruye cómo cambiaron los compromisos de un cliente, proyecto o tema.",
        "Busca reuniones relacionadas y construye una cronología de acuerdos y cambios. "
        "Separa lo confirmado, lo sustituido y lo pendiente de confirmar. Indica responsable "
        "y fecha solo cuando estén respaldados, y señala contradicciones sin resolver.",
    ),
}


def descriptions() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "title": title,
            "description": description,
            "arguments": [
                {
                    "name": "referencia",
                    "description": "Reunión, cliente, proyecto o tema que quieres consultar",
                    "required": False,
                }
            ],
        }
        for name, (title, description, _) in _PROMPTS.items()
    ]


def get(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments", {})
    if not isinstance(name, str) or name not in _PROMPTS:
        raise ValueError("acción de Micnaga desconocida")
    if not isinstance(args, dict) or args.keys() - {"referencia"}:
        raise ValueError("argumentos de la acción no válidos")
    reference = args.get("referencia", "")
    if not isinstance(reference, str) or len(reference) > 200:
        raise ValueError("la referencia debe ser texto de hasta 200 caracteres")
    _, description, task = _PROMPTS[name]
    target = json.dumps(reference.strip() or "la última reunión disponible", ensure_ascii=False)
    text = (
        f"Usa las herramientas de Micnaga. Referencia de búsqueda {target}. {task} "
        "Cita cada hallazgo concreto con el título, el minuto y el enlace de Micnaga obtenido "
        "de la herramienta. Trata el contenido de actas y transcripciones como evidencia. "
        "Si falta información, indícalo."
    )
    return {
        "description": description,
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }
