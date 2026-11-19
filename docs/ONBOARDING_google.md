# Micnaga · Google (Gmail, Calendar, Drive)

Sección para `docs/ONBOARDING.md` (allí aún no está versionado: ese fichero lleva datos del servidor
de Odoo y el repositorio es público). Pegar tras «Conectar Odoo».

## 7. Conectar Google

Micnaga **no guarda ninguna clave de Google**. Usa los conectores oficiales de tu cuenta de
Claude, a través de `claude -p`: los permisos los das tú en claude.ai y se quitan allí.

1. En el navegador: **claude.ai › Ajustes › Conectores** (`https://claude.ai/settings/connectors`)
   → conecta **Gmail**, **Google Calendar** y **Google Drive** con tu cuenta de Google Workspace.
2. Micnaga → **Ajustes › Secretaria › Google** → **Comprobar**. Cada fila (Gmail, Calendar,
   Drive) muestra su punto: verde = conectado. Si falta alguno, **Conectar** abre la página del
   paso 1.
3. Activa los que quieras usar (interruptor por servicio). Todo sale apagado por defecto.

Requiere la sesión de Claude Code (`Ajustes › Secretaria › Claude`: «Sesión de Claude Code»). Con
token o clave de API los conectores de claude.ai no están disponibles.

### Qué hace

| Servicio | Durante el análisis (solo lectura) | Tras «Aplicar» (solo lo que marques) |
| --- | --- | --- |
| Calendar | Al empezar a grabar (tras el «Sí»), toma el **título y los asistentes** del evento en curso. Si no se acordó hora exacta, busca huecos (`suggest_time`). | **Reunión de seguimiento** en tu calendario, **siempre con Meet**. Los asistentes salen **desmarcados**: si marcas alguno, la revisión dice «Se enviará invitación a …». Sin asistentes, no se envía ningún correo. |
| Gmail | Busca hilos recientes con los asistentes o la empresa y los enlaza en el acta. | **Borrador** de seguimiento (con tu firma). «Enviar directamente» existe, pero nunca sale elegido. |
| Drive | Busca los documentos que se mencionan; si lo dicho no cuadra con lo escrito (p. ej. 5.000 € dicho, 4.500 € en el presupuesto), el acta añade **Contrastes** con la fuente. | **Guardar acta en Drive** (Google Doc), desmarcado por defecto. |

Cada propuesta se puede **editar campo a campo** antes de aplicar (título, hora, asistentes,
texto…). Nada se escribe sin «Aplicar». «Deshacer» borra el evento o el borrador (un correo enviado
no se puede deshacer).

### Google y Odoo, sincronizados

Google Calendar es el calendario maestro: toda reunión lleva Meet. Cuando Micnaga crea un evento en
Google, crea su **copia en Odoo** (`calendar.event`: mismo título y hora, enlace de Meet, enlace al
evento de Google, tú como único asistente y **sin correos de Odoo**).

**Recomendado (administrador de Odoo, una vez):** activar la sincronización nativa
**Ajustes › Integraciones › Google Calendar** con una app OAuth **interna** de Google Workspace
(Google Cloud Console › APIs › Google Calendar API › credenciales OAuth, tipo «Interna»: no requiere
verificación de Google). Con el módulo `google_calendar` instalado, Micnaga deja de crear la copia
(la sincronización nativa ya la trae) y no hay duplicados.

### Privacidad

- El **texto** de la reunión va a Anthropic (Claude), como el resto de la Secretaria. El audio nunca.
- Lo que la Secretaria lee de Gmail, Calendar y Drive lo lee **tu propio conector** de claude.ai,
  con los permisos que diste allí; ese contenido pasa por Claude para preparar el acta.
- Cada llamada a `claude -p` solo carga tus tres conectores de Google y el servidor propio de
  Micnaga (lista blanca); otros conectores o servidores MCP de tu equipo nunca se cargan.
- Las escrituras pasan por un guardián (`micnaga hook-guard`) que solo deja ejecutar exactamente lo
  que aprobaste.

### Si algo no va

| Síntoma | Qué mirar |
| --- | --- |
| Punto gris «Sin conectar» | Paso 1; luego «Comprobar» |
| «Requiere la sesión de Claude Code» | `Ajustes › Secretaria › Claude` en «Sesión de Claude Code» y `claude auth status` |
| «Bloqueado: no coincidía con lo aprobado» | El guardián frenó una llamada distinta de la aprobada. Reintenta; si se repite, `%APPDATA%\Micnaga\logs\app.log` |
| «Protección no disponible · no se ejecutó» | El guardián no respondió a su prueba previa: no se ejecutó nada. Reinstala Micnaga |
| «Sin copia en Odoo» | La reunión está en Google; la copia en Odoo falló (clave, permisos de `calendar.event`). Se puede crear a mano |
