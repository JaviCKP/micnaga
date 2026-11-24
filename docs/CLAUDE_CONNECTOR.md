# Reuniones de Micnaga en Claude

El conector permite buscar reuniones, consultar sus actas y leer sus transcripciones desde Claude Desktop o Claude Code en Windows. Al instalarlo, Claude puede consultar todas las reuniones terminadas e indexadas de la biblioteca configurada en Micnaga.

El conector se ejecuta en tu PC, lee la biblioteca local y funciona con la Secretaria apagada. El instalador de Micnaga incluye la extensión. Cuando consultas una reunión en Claude, el texto devuelto puede formar parte de esa conversación y procesarse en Anthropic. Desactiva o desinstala el conector para impedir consultas posteriores. Las conversaciones anteriores conservan el texto que ya recibieron.

## Claude Desktop

1. En el menú Inicio de Windows, busca **Micnaga - Extensión local de Claude**. El acceso directo selecciona `Micnaga-Meetings.mcpb` en la carpeta donde instalaste Micnaga. Para probar desde este checkout, usa `dist\claude\Micnaga-Meetings-local.mcpb` con la ruta del ejecutable ya preseleccionada.
2. En Claude Desktop, abre **Ajustes → Aplicación de escritorio → Extensiones → Administrar → Ajustes avanzados**. En algunas versiones, **Extensiones** aparece directamente en Ajustes. Pulsa **Instalar extensión…**, elige el fichero `.mcpb` y confirma la instalación.
3. Deja vacío **Ejecutable de Micnaga** si usaste la ubicación predeterminada. Si instalaste en otra carpeta, selecciona la extensión e introduce la ruta absoluta de su `micnaga-cli.exe`.
4. En un chat, abre **+ → Conectores** y selecciona **Micnaga · Reuniones**. Prueba con «Busca en Micnaga una reunión y dime su título». Si Claude solicita permiso de herramienta, revisa la solicitud y apruébala.

En una organización de Claude Team o Enterprise, la política del administrador puede exigir que la extensión esté autorizada. El paquete usa el entorno Node.js incluido en Claude Desktop. [Guía de extensiones locales de Claude Desktop](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)

## Claude Code

Ejecuta desde el proyecto de Claude Code en el que quieras habilitar el conector. Para una instalación estándar:

```powershell
$cli = Join-Path $env:LOCALAPPDATA 'Programs\Micnaga\micnaga-cli.exe'
claude mcp add --transport stdio --scope local micnaga-meetings -- "$cli" tools --library
claude mcp get micnaga-meetings
```

Usa la ruta de tu instalación si es distinta. Para un checkout de desarrollo ya instalado en una `.venv`, puedes usar `$cli = (Resolve-Path '.\.venv\Scripts\micnaga.exe').Path`. Al abrir Claude Code, ejecuta `/mcp` para ver el estado. [Documentación MCP de Claude Code](https://code.claude.com/docs/en/mcp)

## Consultas y enlaces

- `buscar_reuniones` encuentra reuniones terminadas por texto o fechas. Devuelve sus identificadores para las demás llamadas.
- `leer_acta` consulta el acta si está disponible para esa reunión.
- `leer_transcripcion` consulta el texto por tramos y permite pedir un intervalo concreto.
- `abrir_en_micnaga` abre el reproductor local en el segundo indicado.

Prueba «Revisa el acta de la última reunión. Compara las decisiones y fechas con la transcripción y cita el minuto de cada diferencia». Los clientes compatibles con acciones MCP también muestran **Revisar un acta**, **Preparar la próxima reunión** y **Seguir acuerdos entre reuniones**.

Las respuestas pueden incluir enlaces `micnaga://reunion/…`. En Windows, el instalador de Micnaga registra el protocolo. Si Claude bloquea el enlace, pídele «Abre esa cita en Micnaga». La herramienta `abrir_en_micnaga` abre la aplicación de este checkout o reenvía la cita a una instancia compatible que ya esté abierta.

## Compilar el paquete desde el código fuente

`scripts\build.ps1` genera el conector para distribución junto al ejecutable congelado. Para probar el paquete desde un checkout local, con la instalación editable ya preparada:

```powershell
.\.venv\Scripts\python.exe scripts\build_mcpb.py --output dist\claude\Micnaga-Meetings-local.mcpb --local-cli .venv\Scripts\micnaga.exe
```

El paquete de prueba preselecciona el ejecutable de ese checkout y contiene su ruta absoluta. Genera el paquete para distribución sin `--local-cli`.
