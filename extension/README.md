# Extensión de navegador de Micnaga (F4, opcional)

Señal «en llamada» **exacta** para Google Meet (Chrome, Edge, Brave). Mientras detecta una llamada,
sustituye a las demás señales del vigilante (spec §5, última fila). Diseño *fail-open*: la extensión solo
puede **añadir** una llamada positiva; si no está, el host no corre o sus heurísticas de DOM se rompen,
`available()` es `False` y el vigilante sigue con sus señales nativas.

* Manifest V3; permisos mínimos: `nativeMessaging` y acceso solo a `https://meet.google.com/*`.
* **Sin red**: ni `fetch`, ni sockets, ni analítica (hay un test que lo comprueba). Habla únicamente con el
  host local `es.micnaga.host` por *native messaging*.
* Mensaje: `{"state": "in_call" | "not_in_call", "title": "...", "code": "abc-defg-hij", "tab": 123}`.
* Las heurísticas del DOM de Meet están **aisladas** en `detect.js` (marcadas como frágiles y **no
  verificadas contra una llamada real de Meet**; solo con un DOM simulado). Si Meet cambia, se toca solo ese
  fichero (p. ej. `LEAVE_LABELS`, el `aria-label` del botón de colgar).

## Cómo funciona

```
content.js ──sendMessage──▶ background.js ──connectNative──▶ micnaga-host (stdio: uint32 LE + JSON)
                                                              │ escribe, de forma atómica y con latido,
                                                              ▼
                     %APPDATA%\Micnaga\extension\host-<pid>.json  ◀── FileExtensionSource (vigilante)
```

* El host (`micnaga/watcher/extension.py`) borra su fichero al cerrarse el navegador: **el código de
  reunión solo está en disco mientras dura la llamada**.
* `FileExtensionSource.available()` es `True` solo si hay una llamada en curso en un host vivo y reciente
  (proceso existente y fichero refrescado hace < 20 s); sin llamada, o con host muerto, es `False`. Cada pestaña en llamada caduca a los 90 s sin latido (la extensión manda
  uno cada 5 s).

## Instalación (manual, nada se ejecuta solo)

1. **Extensión:** `chrome://extensions` (o `edge://extensions`, `brave://extensions`) → *Modo de
   desarrollador* → *Cargar descomprimida* → carpeta `extension\`. Su ID es fijo (campo `key` de
   `manifest.json`): `npfamnnohhilpnfalmlamccajpdandca`.
2. **Host:** registra el host en el navegador (escribe en `HKCU`; no necesita administrador):

   ```powershell
   # ver qué haría, sin escribir nada:
   .\scripts\register_native_host.ps1 -WhatIf
   # desarrollo (usa el venv del repo, extension\native-host\micnaga-host-dev.bat):
   .\scripts\register_native_host.ps1
   # instalado:
   .\scripts\register_native_host.ps1 -HostPath "$env:LOCALAPPDATA\Programs\Micnaga\micnaga-host.exe"
   # quitar:
   .\scripts\register_native_host.ps1 -Unregister
   ```

   El script escribe el manifiesto en `%APPDATA%\Micnaga\extension\es.micnaga.host.json` y crea la clave
   `HKCU\Software\{Google\Chrome | Microsoft\Edge | BraveSoftware\Brave-Browser}\NativeMessagingHosts\es.micnaga.host`
   (valor por defecto = ruta del manifiesto). El desinstalador de Micnaga borra esas claves.
3. **App:** el vigilante debe construirse con la fuente (la integración de `micnaga/app.py` lo decide;
   este módulo **no** se engancha solo):

   ```python
   from micnaga.watcher.extension import create_extension_source

   watcher = Watcher(..., extension=create_extension_source())  # kwarg `extension`
   ```

## Pendiente / límites conocidos

* Meet **no** se ha probado en vivo (sin cuenta/navegador con Meet en el entorno de desarrollo).
* `SignalSampler._sample` descarta **todas** las demás señales (también la vía genérica de apps con micro)
  mientras `available()` es `True`, es decir, durante una llamada de Meet detectada por la extensión: una
  llamada simultánea de Teams/Zoom no se detectaría en ese intervalo. Conviene que el muestreador conserve
  `mic_apps` y use la extensión solo en lugar de la vía Meet.
* Al empezar una llamada, las señales nativas pueden detectarla antes (5 s) sin código y la extensión
  después con código: la clave de reunión (`code` frente a `nocode:<navegador>`) podría cambiar a mitad de
  sesión; comprobar en la integración.
* Si se publica en la Chrome Web Store el ID cambia (habría que actualizar `allowed_origins`).
