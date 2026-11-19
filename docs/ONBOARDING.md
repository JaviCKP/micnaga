# Micnaga · Puesta en marcha

Todo lo que hace falta para tenerlo funcionando, de principio a fin. Las partes marcadas **(próximamente)**
corresponden a la Secretaria (capa de IA, ver `docs/design/ai_brain.md`), que aún no está construida.

---

## 1. Instalar (1 clic)

1. Abre `Micnaga-Setup-<versión>.exe`.
2. No pregunta nada: barra de progreso y listo. Se instala solo para tu usuario (sin permisos de administrador)
   en `%LOCALAPPDATA%\Programs\Micnaga`, con acceso directo en el menú Inicio.
3. Al terminar, Micnaga se abre sola.

Para actualizar, abre el instalador nuevo: cierra la versión en marcha y la sustituye. Si estaba grabando,
la grabación se recupera al volver a abrirse.

## 2. Primer arranque: los modelos

La primera vez se descargan los modelos de transcripción y de voces (~700 MB). No hay que hacer nada: sale
una ventana con el anillo de progreso, se comprueba cada fichero (SHA-256) y se cierra sola. Después Micnaga
vive en la bandeja, junto al reloj. Si la conexión falla, aparece «Reintentar».

Es la única vez que Micnaga usa internet (salvo la Secretaria, si la activas).

## 3. Que esté siempre encendida

El instalador ya deja Micnaga arrancando al iniciar sesión en Windows. Para comprobarlo o asegurarlo:

1. **Arranque con Windows** — Configuración de Windows → **Aplicaciones → Inicio** → `Micnaga` en **Activado**.
   (Desde Micnaga: Ajustes → General → «Arrancar con Windows».)
2. **Icono siempre visible** — Configuración → **Personalización → Barra de tareas → Otros iconos de la
   bandeja del sistema** → `Micnaga` en **Activado**. Así ves de un vistazo su estado (gris vigilando, rojo
   grabando, ámbar en pausa, violeta procesando).
3. **Que Windows no la duerma** — Configuración → **Sistema → Energía y batería**: si usas «Ahorro de batería»
   agresivo, añade Micnaga como excepción o desactívalo durante las reuniones. Micnaga gasta muy poco
   vigilando (< 0,5 % de CPU).
4. **Permiso de micrófono** — Configuración → **Privacidad y seguridad → Micrófono**: «Permitir que las
   aplicaciones de escritorio accedan al micrófono» en **Activado**.
5. **Notificaciones** — Configuración → **Sistema → Notificaciones** → `Micnaga` activado, para recibir el
   aviso «Lista» cuando una reunión está procesada.

Comprobación rápida: reinicia el equipo; tras iniciar sesión, en unos segundos aparece el icono de Micnaga en
la bandeja.

Si alguna vez la cierras («Salir»), vuelve a abrirla desde el menú Inicio.

## 4. Cómo se usa (no hay que hacer nada)

- Cuando una aplicación abre el micrófono (Meet, Teams, WhatsApp, Zoom… o cualquier otra), Micnaga empieza
  a grabar en provisional a los 20 s y, poco después, pregunta **«¿Estás en una reu?»**. En Meet pregunta casi
  al instante.
- **Sí** → se guarda y se transcribe. **No** → se aparta (se puede recuperar durante 7 días).
- Al colgar, aviso «Reunión terminada» y, al rato, «Lista». Todo queda en la **Biblioteca** (icono de la
  bandeja → Abrir biblioteca) y en `Documentos\Micnaga`.
- Durante la reunión, la pastilla de arriba: marcador, pausa privada (no se graba nada) y parar.

## 5. Conectar Claude (próximamente — Secretaria)

La Secretaria usa **Claude Code** en modo `claude -p` con tu cuenta. Solo se envía el **texto** de la reunión,
nunca el audio.

1. **Instala Claude Code** (si no lo tienes): abre PowerShell y ejecuta el instalador oficial que indica
   https://code.claude.com/docs (en Windows, `irm https://claude.ai/install.ps1 | iex`).
2. **Inicia sesión** con tu cuenta de Claude (Pro/Max/Team): ejecuta `claude`, escribe `/login` y sigue el
   navegador. Comprueba con `claude auth status` → debe decir `loggedIn: true`.
3. En Micnaga → **Ajustes → Secretaria** → «Conectar Claude». Micnaga detecta tu sesión de Claude Code y
   hace una prueba (un título de ejemplo en 2–4 s). Acepta el aviso único: «El texto de tus reuniones se
   envía a Claude (Anthropic)».

Alternativas (solo si no quieres usar la sesión de Claude Code):
- **Token de larga duración**: `claude setup-token` (requiere plan Pro/Max/Team/Enterprise) → pega el token
  en Ajustes → Secretaria → «Usar token». Dura 1 año.
- **Clave de API** de la consola de Anthropic → Ajustes → Secretaria → «Usar clave de API».

Micnaga guarda token y clave cifrados (Administrador de credenciales de Windows), nunca en `config.toml`.
Con la sesión del plan, el uso descuenta de los límites de tu suscripción.

## 6. Conectar Odoo (próximamente — Secretaria v2)

Odoo de Bitnaga: **19.0 autoalojado**, `http://<servidor>:8069`, base de datos **`<base>`** (ver Ajustes de Odoo).

> **Antes de usar claves reales:** el servidor va por HTTP sin cifrar: la clave y los datos del CRM viajarían
> en claro. Poner HTTPS delante (Caddy o nginx + Let's Encrypt, con un dominio) y desactivar que el servidor
> devuelva trazas de error a los clientes de la API. Las claves nunca se guardan en el repositorio.

La Secretaria necesita una cuenta con **lectura de CRM y contactos** (la de un empleado suele ver poco).

### 6.1 Crear la clave (titular de la cuenta, ~2 min)

1. Entrar en Odoo → avatar (arriba a la derecha) → **Mi perfil** (o *Preferencias*).
2. Pestaña **Seguridad de la cuenta** → **Nueva clave de API**.
3. Confirmar la contraseña, descripción `Micnaga`, la caducidad más larga que permita la política (en Odoo 19
   las claves caducan: anota la fecha).
4. Copiar la clave (solo se muestra una vez).
5. Micnaga → **Ajustes → Secretaria → Odoo**: URL, base `<base>`, login (el correo con el que entra) y
   la clave → **Probar**: debe salir su nombre y «solo lectura OK».
6. Para revocarla: misma pestaña → papelera junto a `Micnaga`.

Si los menús no coinciden (depende de los módulos), la opción está en la ficha del usuario, apartado de
seguridad / claves de API.

### 6.2 Adaptar la Secretaria a vuestro Odoo (una vez, con Claude Code)

Se ejecuta en la sesión del titular de la cuenta, dentro del repo `micnaga-app`. La clave va en variables de
entorno de esa terminal (no se escribe en ningún fichero):

```powershell
$env:ODOO_URL="https://<dominio-con-https>"   # o http://<servidor>:8069 si aún no hay HTTPS (desaconsejado)
$env:ODOO_DB="<base>"
$env:ODOO_LOGIN="<correo del usuario>"
$env:ODOO_KEY="<clave de API>"
claude
```

Prompt (pegar tal cual):

```text
Vas a adaptar la «Secretaria» de Micnaga (lee docs/design/ai_brain.md) al Odoo real de Bitnaga.
Credenciales: variables de entorno ODOO_URL, ODOO_DB, ODOO_LOGIN, ODOO_KEY. NUNCA las escribas en ficheros,
logs, commits ni en tus respuestas.

REGLAS DURAS
- Solo LECTURA: únicamente search_read / read / fields_get / search_count / name_search (XML-RPC
  /xmlrpc/2/object o JSON-2). Prohibido create / write / unlink / action_* / message_post / cualquier método
  que cambie datos. Si dudas de si un método escribe, no lo llames y pregúntame.
- Datos personales: en todo lo que escribas a disco, anonimiza nombres, correos y teléfonos de contactos
  reales (usa «Contacto A», «empresa del sector X»). Los nombres de etapas, equipos, productos y campos sí.
- Pregúntame antes de cualquier cosa ambigua.

PASO 1 — Inspección (solo lectura)
1. Versión y módulos instalados relevantes (ir.module.module, state=installed: crm, sale, project, calendar,
   mail, contacts, helpdesk, módulos propios).
2. crm.lead: fields_get completo; destaca los campos x_ / x_studio_ y los que de verdad se usan (rellenos en
   las últimas 50 oportunidades). Etapas (crm.stage) por equipo (crm.team), con su orden y significado.
3. res.partner: campos propios, categorías/etiquetas, cómo se distinguen empresa y persona, duplicados típicos
   (mismo dominio de correo, nombres parecidos).
4. Productos/servicios y tarifas (product.template, product.pricelist) si existe venta.
5. Tipos de actividad (mail.activity.type), usuarios comerciales (res.users con grupo de ventas).
6. Muestra: las 20 oportunidades más recientes (anonimizadas) para entender cómo se escriben nombres,
   importes, notas y en qué etapas viven.

PASO 2 — Genera (ficheros nuevos, sin tocar el resto del código)
a) micnaga/brain/context/bitnaga.md — skill de contexto para la Secretaria: qué es Bitnaga, qué vende,
   clientes tipo, qué significa cada etapa y cuándo se pasa a la siguiente, convenciones de nombres, qué campos
   importan y cuáles ignorar, cómo se apuntan las reuniones en el historial. Máx. ~150 líneas, concreto.
b) micnaga/brain/odoo_schema.json — mapa de modelos y campos REALES que usarán las herramientas de solo lectura
   (buscar_contactos, buscar_empresas, buscar_oportunidades, leer_oportunidad, listar_etapas,
   historial_reciente, actividades_pendientes), con los campos exactos a devolver.
c) micnaga/brain/write_actions.json — acciones de escritura PERMITIDAS (las ejecuta Micnaga tras la
   aprobación del usuario, nunca el modelo): crear_contacto, vincular_a_oportunidad,
   actualizar_oportunidad (etapa, importe, etiquetas), programar_actividad, nota_en_historial; con los campos
   exactos y validaciones (p. ej. etapas válidas por equipo).
d) docs/design/odoo_inspection_report.md — informe breve: qué encontraste, decisiones tomadas, dudas para mí,
   y qué datos no pudiste ver por permisos.
e) micnaga/brain/context/learned.md — vacío salvo la cabecera: aquí escribirá la propia Secretaria lo que
   aprenda en uso (automejora, docs/design/ai_brain.md §5.7). Respeta su formato.

PASO 3 — Verificación
- Ejecuta cada herramienta de solo lectura definida en (b) contra el Odoo real con un caso de ejemplo y
  comprueba que devuelve lo esperado. Sin escrituras.
- Resume en ≤ 10 líneas y para.
```

### 6.3 Prueba rápida con una cuenta de empleado

Mismas variables con la clave del empleado, y este prompt:

```text
Prueba de conectividad de solo lectura contra Odoo (variables ODOO_URL, ODOO_DB, ODOO_LOGIN, ODOO_KEY; no las
escribas en ningún sitio). Autentica con /xmlrpc/2/common authenticate, y luego, SOLO con search_count y
check_access_rights (sin leer contenido), dime para crm.lead, res.partner, sale.order, project.task,
calendar.event y mail.message: si tengo permiso de lectura y cuántos registros veo. Ninguna escritura.
Resume en una tabla y para.
```

### 6.4 Automejora

Una vez conectada, la Secretaria actualiza sola lo que sabe de vuestro negocio (etapas nuevas, campos que
empiezan a usarse, reglas que deduce de tus aprobaciones) en `learned.md`, y repasa el esquema de Odoo cada
semana en solo lectura. En Ajustes → Secretaria → «Lo que ha aprendido» puedes verlo, olvidar líneas o volver
a un día concreto.

## 7. Si algo no va

| Síntoma | Qué mirar |
| --- | --- |
| No aparece el icono tras encender | §3.1 y §3.2; ábrela desde el menú Inicio |
| No detecta una reunión | Que la app de la reunión tenga el micro abierto (icono de micro de Windows abajo a la derecha). Ajustes → Diagnóstico muestra qué ve Micnaga |
| No suena «Ellos» en el popup | Dispositivo de salida por defecto de Windows (Configuración → Sistema → Sonido) |
| No llega el aviso «Lista» | §3.5 (notificaciones) |
| Algo raro | Registro en `%APPDATA%\Micnaga\logs\app.log` |
