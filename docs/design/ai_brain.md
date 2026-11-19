# Micnaga — «Secretaria»: la capa de IA (propuesta)

19 nov 2026 · Propuesta para decidir, no hay código. Maquetas: [`ai_brain_mocks/secretaria.html`](ai_brain_mocks/secretaria.html).
Todo lo marcado **(medido)** se probó hoy en este PC (i7-12700F, Claude Code 2.1.327, sesión del plan Max) con
transcripciones **sintéticas**; nunca con reuniones reales. Lo marcado **(sin verificar)** no lo pude comprobar.

## 0. Resumen en 30 segundos

- **Qué es:** al colgar, una secretaria lee la reunión entera, consulta tu Odoo y te deja **el acta + una lista
  corta de propuestas** (crear contacto, mover la oportunidad, programar tu próxima actividad, corregir nombres mal
  transcritos). Tú apruebas con **un toque**. Nada se escribe sin ti.
- **Cómo:** `claude -p` con tu sesión de Claude Code (ya detectada en este PC: cero configuración) y un puñado de
  **herramientas de solo lectura** que Micnaga le presta (Odoo + tu biblioteca). Las escrituras las hace Micnaga,
  no el modelo, y solo tras tu aprobación.
- **Es viable hoy:** prototipo con un Odoo falso → 6 consultas al CRM, acta, 5 propuestas correctas y 4
  correcciones en **20 s** (§3.1). Un título con Haiku tarda **2,4–3,7 s**.
- **Camino:** v1 acta + título + correcciones → v2 Odoo (la pieza central) → v3 memoria («pregunta a tus reuniones»,
  contexto antes de la reunión).
- **Necesito de ti:** 5 respuestas (§8). La que puede bloquear: qué Odoo tienes (versión, hosting y plan).

---

## 1. Principio

> **Cuelgas y, cuando miras, la reunión ya está entendida y tu CRM está a un toque de estar al día.**

Micnaga deja de ser una grabadora que transcribe y pasa a ser alguien que **estuvo en la reunión, conoce tu
negocio y tu CRM, y te prepara el trabajo de después**.

### El momento mágico

1. **Minuto 2 de la llamada.** Pasas el ratón por la pastilla: junto al cronómetro pone
   «Dental Triana · presupuesto CRM». No has escrito nada.
2. **Cuelgas.** La pastilla desaparece. No haces nada más.
3. **Unos minutos después**, un único aviso: *«Dental Triana: alcance ampliado · 24 min · 5 propuestas»*.
4. **Lo abres.** Arriba, el acta: cinco frases, decisiones, tus tareas con fecha. Cada línea lleva una marca
   `01:17`; al pulsarla **suena ese momento**.
5. **Debajo, las propuestas**, ya marcadas las seguras:
   - «Hablante 1 es **Carlos Ruiz** (gerente, ya está en Odoo)».
   - «**Marta Gil** no está en el CRM. ¿La creo en Clínica Dental Triana? Dio su correo en 01:35».
   - «Subir la oportunidad a **6.900 €** y pasarla a **Negociación** (estaba en Propuesta, 5.400 €)».
   - «Actividad para ti: **enviar presupuesto revisado, viernes 20**».
   - «"Odu" → **Odoo** · "Riana" → **Triana**».
6. **Pulsas Intro.** Las líneas se vuelven ✓ una a una. El acta queda en el historial de la oportunidad, Marta
   existe, tu actividad está en Odoo, la transcripción dice «Carlos» y «Odoo», y la próxima vez Micnaga ya sabe
   quiénes son. Si algo no te cuadra: «Deshacer».

Lo que hace que parezca magia: **comprobó antes de preguntar** (buscó a Marta, encontró la oportunidad, leyó el
historial), **enseña la prueba** (marca de tiempo + valor anterior) y **aprende** de cada aprobación.

---

## 2. Privacidad (corto)

- **Un único permiso, al activar:** «El texto de tus reuniones se envía a Claude (Anthropic) para entenderlo.
  **El audio nunca sale de este equipo.**» Un interruptor en Ajustes › Secretaria. Sin más avisos.
- **Qué sale:** texto de la transcripción, nombres de hablantes, glosario, y lo que la secretaria lea de Odoo
  durante ese análisis. **Nunca:** audio, huellas de voz, la clave de Odoo.
- **A dónde:** Anthropic (vía `claude -p`) y tu propio Odoo. Micnaga no añade telemetría. Claude Code envía por
  su cuenta métricas de uso e informes de error (sin contenido, según su doc); Micnaga lo lanzará con
  `DISABLE_TELEMETRY=1` y `DISABLE_ERROR_REPORTING=1` (medido: el arranque informa `analytics_disabled: true`).
- **Excepción por reunión:** en la biblioteca, «No analizar» por reunión; la pausa privada ya existente sigue
  dejando ese tramo fuera de todo.
- **Dato útil** ([data usage](https://code.claude.com/docs/en/data-usage)): con plan Pro/Max, Anthropic entrena
  con tus datos **solo si ese ajuste está activo** (retención 5 años; apagado, 30 días) y se cambia en
  `claude.ai/settings/data-privacy-controls`. Con clave de API (condiciones comerciales) no entrena; 30 días.
- **RGPD:** el texto contiene datos de terceros; el recordatorio «avisa de que grabas» sigue igual. Para uso con
  clientes, la vía con condiciones comerciales (API o plan Team) es la base más limpia. Orientativo, no jurídico.
- **Documentos que habría que actualizar si apruebas** (no los he tocado): `README.md` «Principios de privacidad»,
  `docs/SPEC.md` §1.1 («ningún texto sale») y §13 («ni una petición de red»). Nueva redacción propuesta:
  *«El audio nunca sale. El texto solo sale si activas la Secretaria, y solo hacia Claude y tu Odoo.»*

---

## 3. Lo que funciona en otros (y lo que molesta)

| Patrón | Quién | Lo que tomo |
| --- | --- | --- |
| **Sin bot**: captura el audio del equipo, nadie «entra» a la llamada | Granola, Jamie, Notion ([reseña](https://zackproser.com/blog/granola-ai-review), [Jamie, blog propio](https://www.meetjamie.ai/blog/bot-free-ai-note-takers)) | Micnaga ya es así. Es lo más querido del sector; el rechazo a los bots es fuerte ([Bloomberg](https://www.bloomberg.com/news/articles/2025-02-14/ai-notetakers-in-meetings-are-making-me-queasy), [UC Today](https://www.uctoday.com/security-compliance-risk/ai-meeting-bots-controls-microsoft-zoom-google/)) |
| **Resumen + tareas listos al colgar**, y poder preguntar después | Fathom: lo más citado en [G2](https://www.g2.com/products/fathom-video) y [Capterra](https://www.capterra.com/p/276054/Fathom/reviews/) es precisión, recap rápido, «Ask Fathom», tareas y puesta en marcha simple | Acta sin pedirla; «pregunta a tus reuniones» |
| **Citas enlazadas a la fuente** | Granola Chat (citas en línea, [docs](https://docs.granola.ai/help-center/getting-more-from-your-notes/chatting-with-your-meetings)); Fathom (saltar al momento) | Cada línea del acta y cada propuesta lleva su `mm:ss` que reproduce el audio. Es lo que da confianza |
| **CRM**: resumen como nota/tarea en el contacto u oportunidad; tareas reales opcionales | [Fathom→Salesforce](https://www.fathom.ai/salesforce-integration-admin-guide), [Fireflies→HubSpot](https://guide.fireflies.ai/articles/4037978139-How+to+Integrate+HubSpot+with+Fireflies) | Acta al historial de la oportunidad + actividades. Ellos emparejan **por email del asistente**; Micnaga no tiene emails → emparejar por lo que se dice (§4.2) |
| **Fallo típico del CRM-sync**: registro equivocado, duplicados, tareas que se quedan dentro del resumen, sincronía de un solo sentido ([Coffee, blog de un competidor](https://coffee.ai/articles/fireflies-crm-sync-limitations-2026)) | Fireflies/Fathom | Buscar antes de crear, revisar antes de escribir, y **leer** el CRM para dar contexto (dos sentidos) |
| **Memoria de voces**: nombras una vez y lo recuerda | Jamie (blog propio) | Micnaga ya tiene huellas; la secretaria añade «sé quién es por lo que dice» |
| **Personas, empresas y «brief» previo** | Granola ([updates](https://granola.ai/updates)) | Contexto antes de la reunión (v3), sacado de Odoo y de tus actas |
| **Plantillas y «recetas»** | Granola (29+ plantillas, [blog](https://www.granola.ai/blog/meeting-recipes-repeatable-formats)) | **No** las copio: un solo formato de acta + una caja «instrucciones para tu secretaria» |
| **Recap con capítulos, menciones y tareas** | [Teams](https://support.microsoft.com/office/meeting-recap-in-microsoft-teams-c2e3a0fe-504f-4b2c-bf85-504938f110ef) (exige Premium/Copilot) | Decisiones y tareas como índice navegable, igual que los marcadores |
| **Resumen de llamada en Notas** con botón «Resumen» y aviso audible de grabación | [Apple](https://support.apple.com/es-us/121583) | El resumen vive junto a la transcripción, no en otra app |

**Lo que molesta (a evitar):** bots que se unen solos (queja repetida sobre Fathom en G2 y sobre Read.ai); notas compartidas
por enlace público por defecto (Granola, abril 2026; lo cuenta [un blog competidor](https://blog.buildbetter.ai/do-they-own-your-data-granola-ai-privacy-policy-reviewed/) citando a The Verge); entrenar con tus datos por defecto; el acta
como «verdad» sin revisión (varias reseñas de Granola insisten: es un buen primer borrador, no un registro
infalible).

**Sin verificar** (no lo uso como argumento): «resumen en 30 s» de Fathom; que el resumen de Apple sea en el
dispositivo; cómo nombran hablantes Krisp y Bluedot; detalles de Otter, Zoom AI Companion, tl;dv y Read.ai (solo
encontré comparativas de competidores).

### 3.1 Lo que he comprobado de `claude -p`

Fuentes: [headless](https://code.claude.com/docs/en/headless), [CLI](https://code.claude.com/docs/en/cli-reference),
[autenticación](https://code.claude.com/docs/en/authentication), [Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview).

| Tema | Hecho |
| --- | --- |
| Modo | `claude -p` lee el texto por **stdin** (tope 10 MB), devuelve con `--output-format json` un objeto con `result`, `structured_output`, `total_cost_usd`, `usage`, `is_error`; `stream-json --verbose` emite eventos; **(medido)** con `--json-schema` se ven las llamadas a herramientas una a una (sirve para «Buscando a Marta en Odoo…») y el resultado final trae `structured_output`. Código de salida ≠ 0 si falla |
| Salida con esquema | `--json-schema '<schema>'` → `structured_output` validado. **(medido)** funciona también tras usar herramientas |
| Modelo | `--model haiku` / `sonnet` / `opus` (alias; hoy resuelven a `claude-haiku-5-5`, `claude-sonnet-5-5`). Usar **alias**, no IDs fijos |
| Herramientas | `--tools ""` quita todas las internas (Bash, ficheros, web). Las MCP se dan con `--mcp-config` + `--strict-mcp-config` y se autorizan con `--allowedTools "mcp__micnaga__*"`; `--permission-mode dontAsk` deniega todo lo demás sin preguntar. Topes: `--max-turns`, `--max-budget-usd` |
| Aislamiento | `--safe-mode` (sin CLAUDE.md, plugins, hooks ni MCP; **mantiene el login**) para llamadas sin herramientas. **(medido)** `--safe-mode` también anula `--mcp-config`; para el agente con herramientas funciona `--setting-sources project --disable-slash-commands` desde una carpeta vacía. Lo que vi en el arranque: solo las herramientas MCP (más la interna de salida estructurada), 0 skills, 0 comandos, ningún evento de hooks y 4 plugins internos `cc-plugin-*`. **No comprobé** si se carga un `CLAUDE.md` de usuario ni la memoria automática (hay ruta de memoria, pero sin herramientas de ficheros no puede escribirla) |
| `--bare` | Arranque mínimo, pero **no lee el login de suscripción**: exige `ANTHROPIC_API_KEY`. La doc avisa de que será el modo por defecto de `-p` «en una versión futura» → riesgo para la vía de suscripción; hay que fijar flags y tener plan B |
| **Autenticación** | Orden real: `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_API_KEY` (**en `-p` se usa siempre si existe**) → `apiKeyHelper` → `CLAUDE_CODE_OAUTH_TOKEN` → login de `/login`. Para suscripción hay dos caminos: **(a)** el login ya hecho en este PC (`claude auth status` → `loggedIn: true, claude.ai, max` **(medido)**); **(b)** `claude setup-token` → token OAuth de **1 año**, solo inferencia, que se pasa en `CLAUDE_CODE_OAUTH_TOKEN` (requiere Pro/Max/Team/Enterprise). Con clave de consola: `ANTHROPIC_API_KEY` |
| Facturación | Artículo de ayuda vigente ([enlace](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)): con login del plan, `claude -p` **consume los límites de tu suscripción**; el cambio anunciado para separarlo se **pausó** (15 jun 2026). Desde el 7 oct 2026 Max/Team incluyen crédito mensual de API utilizable con una clave de la consola vinculada. **Puede volver a cambiar** |
| Aviso | La doc del Agent SDK dice que terceros no pueden ofrecer «login de claude.ai» en sus productos sin aprobación. Micnaga no ofrece login: invoca **tu** Claude Code. Para uso propio lo veo correcto; si Micnaga se distribuye a otros, la vía recomendable para ellos es clave de API. No soy jurista (§8.2) |
| ¿Agent SDK? | Es una librería que lanza el mismo binario; añade herramientas en proceso y *callbacks* de aprobación. Hoy no compensa: otra dependencia dentro del congelado de PyInstaller y la misma autenticación. `claude -p` por subproceso cubre todo lo propuesto. Se deja detrás de una interfaz por si acaso |

**Medidas (sintético, este PC):**

| Llamada | Modelo | Entrada | Tiempo total | Coste equivalente API* |
| --- | --- | --- | --- | --- |
| Título (5 frases) | haiku | ~0,2k tokens | **2,4–3,7 s** | 0,0005–0,0008 $ |
| Acta sola, reunión larga | sonnet | 9.500 palabras ≈ 31k tokens | **23 s** | 0,15 $ |
| Acta sola, reunión larga | haiku | ídem | 27 s (más lenta y más verbosa) | 0,009 $ |
| **Secretaria con 6 consultas a Odoo (falso)** | sonnet | reunión corta | **20 s**, 8 turnos | 0,05 $ |

\* Estimación del cliente; con suscripción no se cobra aparte, descuenta de tus límites. Precios de referencia
(caché de 3 nov 2026): Haiku 0,10/0,50 $, Sonnet 2/10 $, Opus 4/20 $ por millón de tokens. Falta medir en el
i5-1245U y con una reunión real de 1 h + Odoo real (estimo 40–90 s).

### 3.2 Odoo

Fuente: [External API, Odoo 19](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html).

- **Dos protocolos:** XML-RPC (`/xmlrpc/2/common` + `/object`, clave de API en lugar de contraseña; sirve en
  14→19, y Python lo trae de serie) y **JSON-2** (Odoo 19+: `POST /json/2/<modelo>/<método>`,
  `Authorization: bearer <clave>`, `X-Odoo-Database`). XML-RPC/JSON-RPC se retiran en **Odoo 22 (otoño 2028) /
  Online 21.1 (invierno 2027)** según la página oficial (una búsqueda decía «Odoo 20»; me quedo con la oficial).
- **Clave de API:** Preferencias › Seguridad de la cuenta › Nueva clave. En la doc de 19: **máximo 3 meses** →
  hay que diseñar el estado «clave caducada». En 16–18 **(sin verificar)**.
- **Odoo Online:** la API externa solo está en el plan **Custom**; no en One App Free ni Standard. **Puede
  bloquear** (§8.1). Odoo.sh y on-premise: sin esa limitación.
- **Modelos** (a verificar contra tu instancia): `res.partner` (personas/empresas), `crm.lead` (oportunidades;
  `stage_id`, `expected_revenue`, `date_deadline`), `crm.stage`, `mail.activity` + `mail.activity.type`
  (tu próxima actividad), nota interna en el historial con `message_post` (subtipo nota, no envía correo),
  `ir.attachment` (adjuntar `acta.md`). `calendar.event` y `project.task`: fuera de v2.
- **MCP de Odoo de terceros:** existe [`mcp-server-odoo`](https://pypi.org/project/mcp-server-odoo/) (ivnvxd;
  XML-RPC, y JSON-2 en 19+). Es genérico (CRUD sobre cualquier modelo), su modo normal pide instalar un módulo en
  Odoo y el modo sin módulo se salta los permisos. No lo recomiendo como base (§5.2).

### 3.3 Gmail y Drive

Google publicó servidores MCP remotos oficiales de Workspace (Gmail: buscar, leer, crear borrador; Drive: listar,
leer) en **vista previa para desarrolladores** desde mayo de 2026 ([guía](https://developers.google.com/workspace/guides/configure-mcp-servers),
[anuncio](https://workspaceupdates.googleblog.com/2026/05/agent-tools-and-security-updates-for-workspace-developers.html)).
Exigen proyecto de Google Cloud + cliente OAuth. **No en v1 ni v2:** el 90 % del valor está en Odoo + tus actas;
el correo de seguimiento se resuelve con un botón «Copiar seguimiento» (cero integración). Reevaluar en v3.

---

## 4. Qué construir (6 piezas, por orden de valor)

Las piezas 1–3 son **una sola pasada** de la secretaria y **una sola pantalla** de revisión.

### 4.1 Acta ★ (v1)

**Qué hace.** Título definitivo, resumen de 3–5 frases, decisiones, tareas (responsable, fecha, «es mía»),
preguntas abiertas y próximos pasos. Cada línea con su `mm:ss`.
**Por qué gusta.** Es lo que todos valoran, pero aquí cada frase se puede **oír**. Las tareas «mías» se separan
de las de otros.
**UX.** Pestaña **Acta** junto a **Transcripción** en el visor (por defecto si existe). Las decisiones y tareas
entran también en el índice superior, como los marcadores. Tus marcadores (Ctrl+Alt+M) se pasan al modelo como
«esto le importó al usuario» (la idea de Granola de partir de tus notas, sin tener que escribir). Botón
«Copiar seguimiento» (correo breve listo para pegar). El buscador encuentra también texto de las actas.

```
┌ DENTAL TRIANA: ALCANCE AMPLIADO ───────────────────── 19 nov · 24 min ┐
│ [Acta] Transcripción                       ● Yo  ● Carlos  ● Marta    │
│                                                                        │
│ Dirección aprueba meter el panel de ventas en la fase 1; el total      │
│ sube a ~6.900 €. Firman la semana que viene si el presupuesto llega    │
│ el viernes. Demo el 3 de diciembre con datos de Triana.                │
│                                                                        │
│ DECISIONES                                                             │
│  · Panel de ventas entra en fase 1 (~6.900 €)                  01:17 ▸ │
│  · Migración de contactos pasa a fase 2                        00:45 ▸ │
│ TAREAS                                                                 │
│  ○ Yo — Enviar presupuesto revisado            vie 20 nov      01:17 ▸ │
│  ○ Marta — Lista de plantillas                 mar 24 nov      01:35 ▸ │
│ ABIERTO                                                                │
│  ? Respuestas de pacientes fuera de horario                            │
│                                              [Copiar seguimiento]      │
└────────────────────────────────────────────────────────────────────────┘
```

**Cómo.** Parte de la pasada única de §5. Modelo `sonnet`. Las palabras con baja confianza del ASR (`Word.p`:
Parakeet y Whisper ya la calculan) se marcan en el texto que ve el modelo (`⟨Odu?⟩`) para que sepa dónde dudar.
**Si falla.** Sin red / sin Claude / límite agotado: la reunión se entrega como hoy; en la pestaña Acta, un
estado tranquilo «Sin conexión · Reintentar». Se reintenta sola al volver la red (3 intentos, espera creciente).
Nunca retrasa ni rompe la transcripción.

### 4.2 Propuestas para Odoo ★★ (v2, la pieza central)

**Qué hace.** Tras leer la reunión y **consultar el CRM**, propone lo que haría una buena secretaria:

| Propuesta | Antes comprueba | Al aprobar, Micnaga… |
| --- | --- | --- |
| **Vincular** la reunión a una oportunidad/contacto | busca empresa, personas y oportunidades abiertas | publica el acta como **nota interna** en su historial (+ `acta.md` adjunto) |
| **Crear contacto** («Marta Gil no está en el CRM») | búsqueda aproximada por nombre, email, teléfono y empresa; enseña los parecidos | crea `res.partner` bajo la empresa correcta |
| **Crear oportunidad** | que no exista una abierta para esa empresa | crea `crm.lead` con contacto, importe y etapa |
| **Actualizar oportunidad** (etapa, importe, fecha) | lee valor actual e historial | escribe solo esos campos; guarda el valor anterior |
| **Actividad para ti** (tus tareas con fecha) | actividades ya pendientes, para no duplicar | crea `mail.activity`; puede marcar como hecha una vencida que la reunión resolvió |

Las tareas **de otros** no se convierten en actividades tuyas: van en la nota.

**Emparejar sin emails.** Micnaga no sabe quién asistió (no hay invitación). Fuentes: nombres dichos en voz
alta, hablantes ya nombrados (huellas), título de Meet, correos/teléfonos dictados. La herramienta de búsqueda
hace `ilike` por trozos en Odoo y ordena en local con `rapidfuzz` (ya es dependencia), quitando acentos y
sufijos («SL», «S.A.»). Cada emparejamiento **confirmado se recuerda** (alias → id de Odoo, voz ↔ contacto,
reunión recurrente ↔ oportunidad), así la segunda vez no pregunta.

**UX: una lista, un toque.** Panel bajo el acta. Una línea por propuesta: verbo + objeto + prueba. Intro = aplicar las marcadas.
**Qué sale marcado lo decide Micnaga, no el modelo:** por tipo (vincular, nota y actividades propias: sí; crear
contacto u oportunidad y cambiar etapa o importe: solo si la comprobación propia de Micnaga —repetir la búsqueda,
releer el registro— coincide con lo que dice la propuesta). La «confianza» del modelo solo sirve para ordenar y
para mostrar el motivo.

```
 PROPUESTAS · Odoo                                        5 de 5   [Aplicar ↵]
 ☑ Vincular a «CRM + recordatorios WhatsApp · Dental Triana»          00:33 ▸
 ☑ Oportunidad: 5.400 € → 6.900 € · Propuesta → Negociación           01:17 ▸
 ☑ Crear contacto: Marta Gil · recepción · Clínica Dental Triana      01:35 ▸
     no existe · parecido más cercano: ninguno          [Editar] [Es otra persona…]
 ☑ Actividad: enviar presupuesto revisado · vie 20 nov                01:17 ▸
 ☑ Actividad: demo con datos de Triana · jue 3 dic                    01:47 ▸
 CORRECCIONES
 ☑ Hablante 1 → Carlos Ruiz (gerente, en Odoo)
 ☑ Hablante 2 → Marta Gil
 ☑ «Odu» → Odoo (1)   ☑ «Riana» → Triana (1)
```

Estados: **analizando** (líneas vivas: «Leyendo la reunión… Buscando a Marta Gil en Odoo… Leyendo la
oportunidad…», salen de los eventos de herramientas) → **propuestas** → **aplicando** (✓ una a una) →
**aplicado** («Ver en Odoo ↗» por línea, «Deshacer» global durante la sesión) → **sin propuestas** («Nada que
actualizar en Odoo», sin drama).

**Seguridad de escritura.** El modelo **no tiene herramientas de escritura**. Devuelve un plan; Micnaga lo
ejecuta con código determinista, tras tu aprobación: revalida cada registro justo antes (si cambió en Odoo, lo
avisa en vez de pisar), repite la búsqueda de duplicados antes de crear, y guarda los ids creados y los valores
anteriores para poder deshacer. La transcripción y los textos de Odoo se tratan como **contenido no fiable**
(alguien puede decir «ignora tus instrucciones» en una llamada). Eso no lo hace imposible de engañar: una
propuesta manipulada podría llegar a la lista. Las defensas son que no puede escribir, que el marcado por
defecto no depende de él, que cada línea enseña valor anterior y prueba, y que todo se puede deshacer.
**Si falla.** Odoo caído o clave caducada: el acta llega igual; el panel dice «Odoo no responde · las propuestas
se prepararán cuando vuelva» o «La clave de Odoo ha caducado · Renovar» (abre Ajustes). Un fallo a mitad de
aplicar deja claro qué líneas se aplicaron y cuáles no; reintentar no duplica (cada escritura lleva el id de la
reunión).

### 4.3 Correcciones: la máquina se corrige a sí misma ★ (v1 básico, v2 con CRM)

**Qué hace.** Usa el contexto (lo que se dice, el glosario, el CRM) para detectar:
- **Hablantes mal o sin nombrar:** «Hablante 2 es Marta: Carlos la presenta en 00:15 y ella da su correo».
- **Errores del ASR** en nombres, productos y siglas: «Odu» → Odoo, «Riana» → Triana (hay una sede Triana en el CRM).
- **Cifras dudosas:** no las cambia; las señala con ▸ para que oigas («¿5.400 o 6.400?»).

**Al aprobar, alimenta al sistema:** renombra al hablante (`rename_speaker` ya existe: regenera MD/SRT y reindexa), corrige el texto en
`transcripcion.json` (operación nueva «corregir texto», sobre el mismo camino de guardado y reindexado), añade la variante al glosario
(`Odoo: Odu`) para que no vuelva a pasar, y —solo si las huellas están activadas— ofrece guardar la voz de la
persona recién identificada (con la misma casilla de consentimiento de hoy; es dato biométrico).
**UX.** En la misma lista (bloque «Correcciones»). Aprobadas, la transcripción cambia a la vista con un
resaltado breve. Todo reversible desde un registro de correcciones.
**Si falla.** Si el texto ya fue editado a mano y no coincide, esa corrección se descarta en silencio.

### 4.4 Título que se pone solo (v1)

**Qué hace.** (a) **Provisional**, hacia el minuto 2: Haiku lee las primeras frases y propone 3–6 palabras.
(b) **Definitivo**, con el acta. Un título escrito por ti nunca se pisa. En Meet, si la pestaña ya trae un nombre
real (evento de calendario) se respeta y el de IA pasa a subtítulo.
**Por qué gusta.** Para Teams/WhatsApp/apps desconocidas hoy no hay título; y en la biblioteca deja de haber
filas «Meet – abc-defg-hij».
**UX.** En la pastilla al pasar el ratón, tras el cronómetro, entrando con un fundido. En el aviso final y en
la biblioteca.
**Cómo.** `claude -p --safe-mode --model haiku --tools ""`, esquema `{title, confidence}`; 2,4–3,7 s (medido).
**Ojo — se aparta de SPEC §1.2 («nada de IA en directo»).** El texto del minuto 2 no existe aún: hay que
transcribir ~90 s ya escritos en disco con una ráfaga de ASR en un proceso aparte, prioridad baja, 2 hilos
(estimo 5–15 s de CPU y ~1,2 GB de RAM temporales; sin medir en el i5). Salvaguardas: una sola vez por reunión,
se salta con batería baja o CPU alta, interruptor propio. Alternativa sin desviación: solo el título definitivo
al terminar (§8.3).
**Si falla.** No pasa nada: queda el título de hoy.

### 4.5 Pregunta a tus reuniones (v3)

**Qué hace.** En el buscador de la biblioteca escribes una pregunta: «¿qué le prometí a Carlos?», «¿en qué
quedamos con el precio del panel?». Respuesta de 2–4 frases con **citas que reproducen el audio**.
**UX.** El mismo campo de búsqueda: si parece pregunta (o pulsas Tab), responde encima de los resultados. Sin
ventana de chat, sin historial de conversaciones.
**Cómo.** La secretaria con las herramientas locales `buscar_reuniones` (el FTS5 que ya existe) y `leer_acta` /
`leer_tramo`; las actas hacen de índice barato. Sin base vectorial. `sonnet`; estimo 5–15 s.
**Si falla.** Sin IA, el buscador funciona como hoy.

### 4.6 Contexto antes de la reunión (v3)

**Qué hace.** Cuando la secretaria reconoce con quién hablas (título de Meet, reunión recurrente ya vinculada, o
el título provisional), la pastilla ofrece un punto violeta: al pasar, tres líneas — etapa e importe de la
oportunidad, **lo que prometiste la última vez** y lo que quedó abierto.
**Por qué gusta.** Es «lo que no sabías que querías»: entrar a la llamada sabiendo lo pendiente.
**UX.** Nunca se abre sola ni tapa nada. Silencioso durante la reunión, como manda la dirección de diseño.
**Cómo.** Sale de `acta.json` anteriores + una lectura a Odoo; sin LLM cuando la reunión ya está vinculada.
**Si falla.** No aparece el punto.

### Lo que NO construiría (y por qué)

| No | Por qué |
| --- | --- |
| Transcripción o sugerencias en directo, «coaching» | Rompe «ligero mientras grabas» y distrae; nadie lo echa de menos al día siguiente |
| Escrituras automáticas en Odoo | Un CRM ensuciado no se perdona. Revisión de un toque siempre (§8.4) |
| Correos automáticos a los asistentes | Micnaga es personal: lo que sale hacia otros lo envías tú («Copiar seguimiento») |
| Biblioteca de plantillas de acta | 1.500 opciones. Un formato bueno + una caja de instrucciones |
| Ventana de chat con historial | Otra app dentro de la app. La pregunta vive en el buscador |
| Analítica de conversación (tiempo de palabra, sentimiento) | Vistosa, inútil para un equipo pequeño |
| Base vectorial / embeddings | FTS5 + actas + un agente bastan a esta escala |
| Tareas de proyecto, calendario, multi-CRM | Odoo CRM primero y bien; lo demás, cuando duela |
| Gmail/Drive ahora | Coste de configuración alto (Google Cloud + OAuth) para poco valor añadido hoy |
| LLM local | En CPU, la calidad no llega para acta + razonamiento sobre CRM |

---

## 5. Arquitectura

### 5.1 Encaje en lo que ya existe

```
grabación → cola → worker (11 etapas, intactas) → transcripcion.json ─┐
                                                                       │ evento job_done
app (proceso ligero)                                                   ▼
  └─ Secretaria (hilo serie, como Finisher) ── lanza ──► claude -p (subproceso, sin ventana)
        │   lee: transcripcion.json, glosario, memoria             │ stdin: reunión + contexto
        │   escribe: acta.json, acta.md                            │ MCP stdio
        │                                                          ▼
        │                                        micnaga-cli tools  (solo lectura)
        │                                           ├─ Odoo  (XML-RPC / JSON-2)
        │                                           └─ biblioteca local (SQLite FTS5, actas)
        └─ tras tu aprobación ──► Ejecutor (Python determinista) ──► Odoo (escrituras)
                                                              └──► transcripción, glosario, huellas
```

- **No es la etapa 12.** Es un trabajo aparte que arranca con `job_done`. La transcripción nunca depende de la
  red. Vive en el **proceso de la app**: es E/S pura (un subproceso y HTTP), sin numpy ni modelos.
- **Un solo aviso.** El toast «Lista» espera a la secretaria hasta **2 min** (mi estimación para 1 h + Odoo real
  es 40–90 s, sin medir); si no llega, sale como hoy, sin contador de propuestas, y estas aparecen después en la
  biblioteca (punto violeta en la fila). Ese tope se ajusta con medidas reales.
- **Cola propia:** tabla `secretary_runs(meeting_id, kind, state, attempts, error, updated_at)` en `micnaga.db`;
  reintentos con espera creciente; al arrancar se recogen las reuniones de las últimas 24 h sin acta. Las
  antiguas, a mano («Analizar»), para no gastar sin querer.

### 5.2 Cómo se le dan las herramientas a `claude -p`

| Opción | A favor | En contra | Veredicto |
| --- | --- | --- | --- |
| **A. Servidor MCP propio y mínimo** (`micnaga-cli tools`, stdio, lo lanza `claude` con `--mcp-config`) | Solo lectura **por construcción**; 6–8 herramientas hechas a medida (devuelven justo lo útil, en español); la clave de Odoo la lee el propio servidor del almacén de Windows y **nunca pasa por Claude**; mismo binario, sin dependencias; probado hoy | Hay que escribirlo (~300 líneas; el protocolo stdio es simple) | **Recomendada** |
| B. MCP de Odoo de terceros | Ya existe | Genérico (CRUD de todo, incluye escribir), más turnos y tokens, pide módulo en Odoo o salta permisos, otra dependencia y otra superficie de riesgo | No |
| C. Sin herramientas: Micnaga busca y le pasa los resultados | Lo más seguro y barato | Micnaga tendría que adivinar qué buscar; pierde «prueba variantes, lee el historial, decide» | Solo como modo degradado |
| D. Agent SDK con herramientas en proceso | Aprobaciones en vivo | Dependencia nueva en el congelado; no hace falta: las escrituras ya van fuera del modelo | Más adelante, si hiciera falta |

**Herramientas de lectura** (todas con tope de resultados y de tamaño):
`buscar_contactos(texto)` · `leer_contacto(id)` (con historial reciente) · `buscar_oportunidades(texto|partner_id)` ·
`leer_oportunidad(id)` (etapa, importe, actividades, últimas notas) · `buscar_reuniones(texto)` ·
`leer_acta(reunión)` · `leer_tramo(reunión, desde, hasta)`.
Lo estático (etapas del embudo, tipos de actividad, usuarios, tu empresa) **no** es herramienta: va en un
«paquete de contexto» que se refresca una vez al día; ahorra turnos.

**Leer libre / escribir con permiso** queda así: leer = herramientas MCP; escribir = **no existe como
herramienta**; es un plan JSON que ejecuta Micnaga tras tu toque.

### 5.3 La llamada

```
claude -p --model sonnet --tools "" --strict-mcp-config --mcp-config <json>
       --allowedTools "mcp__micnaga__*" --permission-mode dontAsk
       --setting-sources project --disable-slash-commands --no-session-persistence
       --max-turns 25 --max-budget-usd 0.50
       --output-format stream-json --verbose --json-schema <esquema> --system-prompt-file <prompt>
       < reunión por stdin
```

- **Entrada (stdin, UTF-8):** cabecera (fuente, fecha, duración, título), hablantes con sus nombres actuales,
  glosario, marcadores, paquete de contexto de Odoo, «Sobre mi negocio» (texto libre) y memoria de
  emparejamientos; después la transcripción `[mm:ss] Hablante: texto`. Una hora ≈ 30k tokens: cabe entera, sin
  trocear.
- **Salida (`structured_output`), esquema resumido:**

```json
{ "titulo": "…", "resumen": ["…"],
  "decisiones": [{"texto": "…", "t": "01:17"}],
  "tareas": [{"texto": "…", "responsable": "…", "es_mia": true, "fecha": "2026-11-20", "t": "01:17"}],
  "preguntas_abiertas": ["…"], "proximos_pasos": ["…"],
  "propuestas_crm": [{"tipo": "vincular_reunion|crear_contacto|crear_oportunidad|actualizar_oportunidad|crear_actividad",
                      "titulo": "una línea", "motivo": "…", "evidencia_t": "01:35", "confianza": 0.9,
                      "datos": {"…ids existentes, valores nuevos y anteriores…": 0}}],
  "correcciones": [{"tipo": "hablante|palabra|glosario|cifra_dudosa", "de": "Odu", "a": "Odoo",
                    "partner_id": null, "motivo": "…", "confianza": 0.9}] }
```

  Micnaga valida cada `datos` contra un esquema **por tipo** antes de enseñarlo; lo que no valida, no se muestra.
- **Prompts:** ficheros versionados en `micnaga/brain/prompts/*.md` (`secretaria.md`, `titulo.md`,
  `pregunta.md`), con número de versión.
- **Mecánica en Windows:** texto por stdin (el límite de línea de comandos es ~32k), UTF-8 forzado en ambos
  sentidos (medido: llega bien con acentos), `CREATE_NO_WINDOW`, carpeta de trabajo vacía propia, ruta de
  `claude` resuelta una vez y guardada, tiempo máximo 180 s y matar el árbol de procesos al vencer.
- **Entorno limpio:** se construye a mano; se quitan `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_BASE_URL` y `CLAUDE_CODE_USE_*` heredados (una `ANTHROPIC_API_KEY` suelta ganaría en silencio y
  facturaría a otra cuenta) salvo que hayas elegido esa vía; se añaden `DISABLE_TELEMETRY=1` y
  `DISABLE_ERROR_REPORTING=1`.
- **Interfaz sustituible** (`BrainClient`): hoy `ClaudeCliClient`; mañana, si hiciera falta, API directa. Es la
  misma idea que los motores de ASR.

### 5.4 Caché, reintentos y coste

- **Caché:** `acta.json` guarda `hash` de la transcripción + versión de prompt + modelo. Mismo hash → no se
  repite. Renombrar un hablante no invalida el acta (se reetiqueta en local).
- **«Rehacer»** en el menú de la reunión (p. ej. tras fusionar hablantes). Una sola acción, sin opciones.
- **Topes:** `--max-budget-usd` y `--max-turns` por pasada; como mucho 1 pasada automática por reunión; título
  provisional 1 vez. Ajustes › Diagnóstico muestra «hoy: 4 análisis · ≈ 0,31 $ equivalentes».
- **Errores con nombre:** el JSON trae `is_error` y los reintentos de API su categoría (`rate_limit`,
  `authentication_failed`, `billing_error`…): se traducen a tres estados de UI —«Sin conexión», «Claude no está
  conectado», «Límite alcanzado, se reintenta a las HH:MM»—, nunca a un volcado técnico.

### 5.5 Dónde se guarda

```
Documentos\Micnaga\2026-11-19_1030_dental-triana\
  transcripcion.json   ← sin cambios de esquema (las correcciones aprobadas se aplican al texto)
  acta.json            ← fuente de verdad: acta + propuestas (con estado e ids de Odoo) + registro de
                          correcciones (para deshacer) + hash/modelo/versión/coste
  acta.md              ← legible; es lo que se publica en Odoo
%APPDATA%\Micnaga\
  secretaria\memoria.json   ← alias → id de Odoo, voz ↔ contacto, reunión recurrente ↔ oportunidad
  secretaria\contexto.json  ← etapas, tipos de actividad, usuarios (caché diaria)
  secretaria\negocio.md     ← «Sobre mi negocio» (tu texto)
```

`micnaga.db`: tabla `secretary_runs`, y el texto del acta entra en el índice FTS para que el buscador lo encuentre.

### 5.6 Secretos

- **Administrador de credenciales de Windows** (`win32cred`, pywin32 ya es dependencia; cifra con DPAPI, ligado
  a tu cuenta): `Micnaga/odoo` (clave de API) y, solo si lo usas, `Micnaga/claude` (token o clave).
- **Nunca** en `config.toml`, ni en logs, ni en la línea de comandos. En `config.toml` solo lo no secreto
  (`[secretary] enabled`, `odoo_url`, `odoo_db`, `odoo_user`, `model`).
- El token de Claude viaja únicamente como variable de entorno del subproceso. La clave de Odoo la lee el propio
  proceso de herramientas; Claude no la ve.

---

### 5.7 Automejora: la skill se actualiza sola

La skill de contexto (`micnaga/brain/context/bitnaga.md`, generada al conectar Odoo) no es estática. Cada vez
que la Secretaria trabaja, puede descubrir cosas nuevas; las guarda **sola**, sin pedir permiso, en una capa
aparte (`learned.md`) que se carga junto a la skill en cada llamada.

**Qué aprende** (hechos del negocio, nunca datos personales sueltos):
- **Del CRM al explorarlo:** una etapa o un equipo nuevo, un campo `x_` que empieza a usarse, una etiqueta,
  un producto o tarifa nueva, una convención de nombres («las oportunidades se titulan "Empresa · servicio"»).
- **De tus aprobaciones y rechazos:** si siempre rechazas «mover a Negociación» cuando no hay importe, aprende
  la regla; si corriges «Odu → Odoo», va al glosario.
- **De las correcciones de hablantes:** quién suele estar en las reuniones de cada cliente.

**Cómo, para que no se estropee:**
1. Al final de cada pasada, la respuesta de `claude -p` incluye un bloque opcional `aprendizajes` (JSON:
   `hecho`, `fuente` (`crm` / `aprobación` / `reunión`), `evidencia` (id de registro o marca de tiempo),
   `confianza`).
2. Micnaga lo valida en local: descarta lo que contenga correos, teléfonos, importes concretos de un cliente o
   secretos; descarta duplicados y contradicciones con la skill base (si contradice, se marca como
   «a revisar» en vez de aplicarse).
3. Se añade a `learned.md` con fecha y fuente. Cada aprendizaje que contradiga uno anterior lo sustituye.
4. **Consolidación**: cuando `learned.md` pasa de ~80 líneas, una llamada a Sonnet lo reescribe compacto
   (fusiona, quita lo obsoleto) y, si algo es estable desde hace tiempo, lo propone para la skill base.
5. **Historial y deshacer**: cada cambio queda versionado (copia con fecha en `brain/context/history/`).
   En Ajustes → Secretaria → «Lo que ha aprendido» se ve la lista, con «Olvidar» por línea y «Volver a como
   estaba el día X». Sin más opciones.
6. Además, una vez por semana (o cuando un aviso de Odoo devuelva un campo/etapa desconocido), un
   «re-reconocimiento» ligero de solo lectura compara el esquema real con `odoo_schema.json` y actualiza el
   mapa de campos solo.

Así la skill mejora con el uso sin que tengas que tocar nada, y siempre puedes ver y deshacer lo aprendido.

## 6. Ajustes: puesta en marcha en 30 segundos

Una sección nueva, **Secretaria**. Tres tarjetas, cada una con un punto de estado (verde / ámbar / gris):

```
 SECRETARIA                                                    [● activada]
 ┌ Claude ──────────────────────────────────────────────────────────────┐
 │ ● Conectado · plan Max (detectado en este equipo)          [Probar] │
 └──────────────────────────────────────────────────────────────────────┘
 ┌ Odoo ────────────────────────────────────────────────────────────────┐
 │ Dirección  [ https://miempresa.odoo.com        ]                     │
 │ Clave      [ ••••••••••••••••••••••            ]  ?                  │
 │ ● Conectado como Javier · base «miempresa» · caduca en 82 días       │
 └──────────────────────────────────────────────────────────────────────┘
 ┌ Sobre mi negocio ────────────────────────────────────────────────────┐
 │ [ Bitnaga: implantamos Odoo y automatizaciones para pymes…         ] │
 └──────────────────────────────────────────────────────────────────────┘
 ▸ Avanzado
```

**Flujo:** activas el interruptor → una frase de permiso (§2) → Claude ya sale verde si hay sesión → pegas URL y
clave de Odoo → «Conectado como…» (la base y el usuario se deducen solos cuando se puede; el campo «base» solo
aparece si hay varias). Listo. La secretaria funciona sin Odoo (solo acta y correcciones).

- **Si Claude Code no está o no hay sesión:** la tarjeta ofrece «Pegar token» (el de `claude setup-token`) o
  «Usar clave de API», con el `?` explicando cada uno en una línea. Si no está instalado, enlace a instalarlo.
- **Avanzado (plegado):** calidad «Normal / Máxima» (sonnet / opus), título en directo sí/no, instrucciones para
  tu secretaria (texto libre: «las actas, en tú; importes sin IVA»), usuario de Odoo si no coincide.
- **Nada más.** Ni plantillas, ni mapeos de campos, ni reglas.

---

## 7. Hoja de ruta: tres pasos pequeños

### v1 — «Acta» (sin Odoo)
Cliente `claude -p`, sección Secretaria, pasada única sin CRM, pestaña Acta, panel de revisión con correcciones,
título provisional y definitivo, «Copiar seguimiento».
**Se acepta si:**
- Con Claude Code ya iniciado, activar la Secretaria es **un interruptor**; «Probar» responde en < 5 s.
- Tras una reunión de 30–60 min, el aviso «Lista» trae título propio y, al abrir, el acta está ahí; cada
  decisión y tarea reproduce su momento al pulsarla.
- Con hablantes sin nombrar que se presentan en la llamada, propone sus nombres; al aprobar, la transcripción y
  el buscador los reflejan, y el glosario gana las variantes corregidas.
- Sin internet, la reunión se entrega exactamente como hoy y el acta aparece sola al volver la red.
- *(Solo si apruebas §8.3.)* Hacia el minuto 2–3, la pastilla muestra un título razonable sin que el audio de
  la llamada se resienta (comprobado en el i5 de referencia).

### v2 — «Secretaria» (Odoo)
Conexión a Odoo, herramientas de lectura, propuestas, ejecutor con revalidación y deshacer, memoria de
emparejamientos.
**Se acepta si:**
- Pegar URL y clave → «Conectado como…» en < 30 s; con clave caducada, un mensaje claro y un botón.
- Reunión con un cliente que **ya existe**: propone vincular a la oportunidad correcta y, al aprobar, el acta
  aparece como nota interna en su historial y tus tareas como actividades con fecha.
- Persona nueva: propone crearla **solo** tras no encontrarla, mostrando los parecidos; nunca crea duplicados en
  10 reuniones de prueba.
- Cambio de etapa/importe: muestra valor anterior y nuevo; «Deshacer» lo devuelve.
- Segunda reunión con las mismas personas: los hablantes salen nombrados y la oportunidad vinculada sin preguntar.
- Nada cambia en Odoo sin tu toque (verificable en el registro de Odoo); Odoo caído no afecta al acta.

### v3 — «Memoria»
Pregunta a tus reuniones, contexto antes de la reunión, y decidir Gmail/Drive con datos de uso.
**Se acepta si:**
- «¿Qué le prometí a Carlos?» en el buscador devuelve la respuesta con citas reproducibles en < 15 s.
- Al entrar en una reunión recurrente ya vinculada, la pastilla ofrece el punto de contexto con la etapa y lo
  pendiente de la última vez, sin abrir nada por sí sola.

---

## 8. Preguntas abiertas (con mi recomendación)

1. **¿Qué Odoo tienes: versión, y Online / Odoo.sh / propio? ¿Qué plan?** En Odoo Online la API externa exige
   plan Custom; sin ella v2 no es posible tal cual. *Recomendación:* dímelo antes de empezar v2; crear un usuario
   normal (no administrador) para ti con su clave, y empezar por XML-RPC (vale de 14 a 19) dejando JSON-2
   preparado. *Si resulta ser Online sin plan Custom:* o cambias de plan/hosting, o v2 sale en modo manual (la
   secretaria prepara las mismas propuestas sin leer el CRM y cada línea ofrece «Copiar» y «Abrir en Odoo»):
   útil, pero sin comprobación de duplicados ni historial, que es media magia.
2. **¿Con qué cuenta de Claude?** *Recomendación:* usar la sesión de Claude Code ya iniciada (plan Max, cero
   configuración) y dejar «token» y «clave de API» como alternativas. Asumir que la política puede cambiar
   (ya se anunció y pausó una vez) y que `--bare` puede pasar a ser el modo por defecto: por eso la interfaz
   sustituible. Si algún día repartes Micnaga a otras personas, que usen clave de API.
3. **¿Título en directo (minuto 2), sabiendo que rompe «nada de IA en directo»?** *Recomendación:* sí, con las
   salvaguardas de §4.4 y medido en el i5; si la medida sale mal, solo título definitivo.
4. **¿Siempre revisión, o aplicar solo lo seguro?** *Recomendación:* siempre revisión en v2. Tras un mes, ofrecer
   «aplicar siempre» **solo** para la nota en el historial y tus actividades; crear contactos y mover etapas,
   nunca sin toque.
5. **¿Gmail/Drive entran?** *Recomendación:* no hasta v3. Primero «Copiar seguimiento». Si luego lo quieres, el
   primer paso sería «dejar un borrador en Gmail» con el MCP oficial de Google (hoy en vista previa).

---

## Anexo: el prototipo de hoy (para que juzgues la calidad)

Reunión sintética de 16 frases (Javier, «Hablante 1», «Hablante 2») con dos errores de ASR plantados («Odu»,
«Riana») y un Odoo falso con la empresa, dos contactos y una oportunidad en «Propuesta» por 5.400 €. La
secretaria (sonnet, 20 s) hizo por su cuenta:

```
buscar_contactos("Dental Triana") · buscar_contactos("Marta Gil") · buscar_contactos("Carlos")   ← en paralelo
buscar_oportunidades("Triana") · leer_oportunidad(205) · listar_etapas()
```

y devolvió: título «Dental Triana: alcance ampliado con panel de ventas y presupuesto revisado (6.900 €)»;
5 decisiones y 4 tareas con marca de tiempo y fechas resueltas («antes del viernes» → 2026-11-20); propuestas:
vincular a la oportunidad 205 (0,95), subir a 6.900 € y pasar a Negociación (0,85), crear a Marta Gil con su
correo bajo la empresa 41 tras buscarla y no encontrarla (0,90), dos actividades para Javier; correcciones:
Hablante 1 → Carlos Ruiz Ortega (id 57), Hablante 2 → Marta Gil, «Odu» → Odoo, «Riana» → Triana. No propuso
nada inventado. Lo que **no** hizo: cerrar la actividad vencida «Llamar a Carlos» (habrá que pedírselo en el
prompt). Es una sola muestra y es sintética: falta probar con reuniones reales, largas y con un Odoo de verdad.
