"""«Secretaria»: la capa de IA de Micnaga (``docs/design/ai_brain.md``).

Al terminar una reunion, ``claude -p`` lee la transcripcion (solo texto, nunca audio) y devuelve
el acta, correcciones y un plan de escritura para Odoo. El modelo solo tiene herramientas de
**lectura** (servidor MCP propio, ``micnaga.brain.mcp``); las escrituras las ejecuta Micnaga con
codigo determinista y solo tras la aprobacion del usuario (``micnaga.brain.executor``).

Este paquete es ligero al importarse: nada de numpy ni modelos. Modulos:

* ``claude``: lanza ``claude -p`` (stream-json) con un entorno limpio y lee sus eventos.
* ``secrets``: token/clave de Claude y clave de Odoo en el Administrador de credenciales.
* ``odoo`` / ``mcp``: cliente XML-RPC de Odoo y servidor MCP stdio de solo lectura.
* ``secretary``: la pasada completa (acta + correcciones + propuestas + aprendizajes).
* ``plan`` / ``executor``: validacion del plan y ejecucion tras la aprobacion, con deshacer.
* ``learn``: automejora (``learned.md`` validado en local, con historial).
* ``service``: cola serie en el proceso de la app (tras ``job_done``) y API para la UI.
* ``live_title``: titulo provisional hacia el minuto 2 de una grabacion confirmada.
"""

from __future__ import annotations

PROMPT_VERSION = 3
