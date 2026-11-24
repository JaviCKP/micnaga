# Biblioteca de Micnaga en Claude

El conector consulta actas y transcripciones desde Claude en la web, el móvil y el escritorio. Una vez sincronizada una reunión, se puede consultar con el PC de grabación apagado. Las citas incluyen el título, el fragmento y el segundo de la reunión. El enlace `micnaga://` abre ese punto del audio en el PC vinculado que publicó la reunión.

## Configurar el servidor una vez

1. Elige un equipo Linux con Docker Compose y un dominio público cuyo DNS apunte al servidor. Abre los puertos 80 y 443 para que Caddy gestione el certificado. En un servicio que ya termina HTTPS, publica únicamente el puerto 8000 a través de su proxy y conserva la cabecera `Host` original.
2. Activa [WorkOS AuthKit para MCP](https://workos.com/docs/authkit/mcp) e invita a los usuarios. Habilita el inicio de sesión con Google Workspace si lo usáis. Añade `https://<dominio>/mcp` como **Resource Indicator** y márcalo como predeterminado si hay clientes que omiten `resource`. Autoriza la identidad publicada de Claude o el registro dinámico según vuestro entorno. En el panel de AuthKit obtén el ID `user_…` de cada persona a la que quieras dar acceso.
3. En el directorio `remote`, copia `.env.example` a `.env`, configura el dominio y el emisor AuthKit y ejecuta:

   ```sh
   docker compose up -d --build
   curl --fail https://<dominio>/health
   docker compose exec app python -m remote grant bitnaga user_<id>
   docker compose exec app python -m remote create-device bitnaga
   ```

   Haz un `grant` por usuario y biblioteca. Una cuenta puede tener acceso a varias. Genera una clave por PC y guárdala en un gestor de secretos hasta que el usuario vincule ese equipo. La clave del equipo sirve para subir documentos y queda guardada por usuario en el Administrador de credenciales de Windows. Claude inicia sesión con OAuth y cuenta con herramientas de lectura.

## Vincular un PC y conectar Claude

1. En **Micnaga → Ajustes → Privacidad → Biblioteca en Claude**, introduce la URL `https://<dominio>/mcp` y la clave del equipo. Pulsa **Activar sincronización**. Se publican las reuniones terminadas de la biblioteca seleccionada en Micnaga, incluidas las anteriores, y las revisiones posteriores. Se envían el texto del acta, las frases, sus tiempos y sus hablantes. Audio, capturas, huellas de voz y propuestas internas de la Secretaria quedan en el PC.
2. Pulsa **Conectar con Claude**. Añade un conector personalizado llamado «Micnaga · Reuniones» con esa misma URL e inicia sesión. En Team o Enterprise, una persona con permisos de administración añade el conector a la organización antes de que los miembros se conecten. [Guía oficial de Claude](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
3. Prueba con «Busca la última reunión sobre PQC, recupera los acuerdos y cita el fragmento exacto con su minuto». Puedes usar también las acciones **Revisar un acta**, **Preparar la próxima reunión** y **Seguir acuerdos entre reuniones** en los clientes MCP que exponen acciones.

Micnaga comprueba los cambios en segundo plano y reintenta un envío pendiente aunque se haya cerrado la app. Si cambias de carpeta de almacenamiento o sustituyes su índice, se pausa el envío hasta que vincules esa biblioteca. Desconectar un PC conserva el texto que ya publicó.

## Operación

- Mantén el volumen `meetings` en el servidor y configura copias de seguridad cifradas. Para una copia en caliente usa la API `sqlite3.Connection.backup` desde el contenedor o una instantánea consistente del volumen. Una copia de solo el fichero principal con el servicio en marcha puede omitir cambios del WAL. El despliegue usa una instancia de la aplicación por volumen.
- Para retirar acceso de lectura usa `docker compose exec app python -m remote revoke bitnaga user_<id>`. Para retirar la clave de subida usa `revoke-device <id-del-equipo>`. Si también quieres retirar sus reuniones de la biblioteca remota, usa `purge-device <id-del-equipo> --confirm` y aplica la política de retención a las copias de seguridad. Los archivos locales siguen en el PC.
- Para actualizar el servidor con una copia del repositorio al día, ejecuta `docker compose up -d --build`. Conserva el volumen. El paquete local de Claude Desktop se sigue generando como `dist/micnaga/Micnaga-Meetings.mcpb` al compilar la aplicación.

El flujo de publicación de GitHub genera el instalador, `SHA256SUMS.txt` y la extensión `.mcpb` al etiquetar una versión estable `vX.Y.Z`. Configura los secretos de Actions `MICNAGA_SIGN_PFX_BASE64` y `MICNAGA_SIGN_PFX_PASSWORD` con el certificado de publicación de Windows. El flujo valida la firma antes de publicar.

### Verificación en desarrollo

```sh
uv pip install -r remote/requirements.txt
python -m pytest -q tests/brain/test_remote_sync.py tests/brain/test_mcp_library.py
```
