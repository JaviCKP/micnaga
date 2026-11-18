# Micnaga — Especificación para el agente de IA

Nov 18, 2026 · @Javier González

## 1. Misión y reglas para el agente

Construye Micnaga: una app de Windows, open source y de uso personal, que detecta cuándo estás en una reunión de Google Meet, graba tu micro y el audio del sistema en pistas separadas y, al terminar, genera una transcripción con hablantes identificados, todo en local y en CPU.

Este documento es la especificación completa. Léelo entero antes de escribir código y trabaja fase a fase (sección 14). Cada funcionalidad tiene un ID (`MIC-xx`) y criterios de aceptación; una fase solo se da por cerrada cuando todos sus criterios se cumplen.

**Equipo de referencia.** Intel Core i5-1245U (2 núcleos P + 8 E, 12 hilos, AVX2, sin AVX-512), 16 GB de RAM, gráfica integrada Intel UHD sin VRAM útil, Windows 11 de 64 bits, 238 GB de SSD. Todo debe funcionar bien aquí.

### Principios (no negociables)

1. **Local y privado.** Ningún audio ni texto sale del equipo. La única conexión permitida es la descarga de modelos en el primer arranque, desde URLs fijadas con hash SHA-256. Sin telemetría, sin analítica, sin cuentas.
2. **Ligero mientras grabas.** Durante la reunión Micnaga solo captura y escribe a disco: menos del 3 % de CPU y menos de 150 MB de RAM. Nada de IA en directo.
3. **Pesado solo después, y con educación.** El procesado corre en segundo plano, con prioridad baja e hilos limitados, para que el portátil siga siendo usable.
4. **Nunca perder una grabación.** Si algo falla (cierre, cuelgue, apagado), el audio ya escrito debe poder recuperarse y procesarse al siguiente arranque.
5. **Nunca procesar lo que no es una reunión.** Una nota de voz, una prueba de micro o una llamada de 20 segundos se descartan solas (sección 5).
6. **Bonito.** La interfaz es parte del producto, no un añadido: animaciones suaves, tema claro y oscuro, tipografía cuidada.
7. **Sustituible.** Motores de transcripción y diarización detrás de interfaces, para cambiarlos sin tocar el resto.

### Reglas de trabajo para el agente

- Python 3.11+, tipado con type hints y `mypy --strict` en el núcleo; formateo con `ruff`.
- No añadas dependencias fuera de la sección 11 sin justificarlo en el PR.
- Toda decisión no cubierta aquí: elige la opción más simple, déjala anotada en `DECISIONS.md` y sigue.
- Cada módulo con tests unitarios; los de audio usan ficheros de prueba sintéticos, nunca grabaciones reales del usuario.
- Nada de rutas absolutas en el código: todo vía configuración (sección 10).

## 2. Requisitos y presupuestos

Micnaga se mide contra estos límites en el equipo de referencia; si un cambio los rompe, no se fusiona.

| Métrica | Límite | Cómo se mide |
| --- | --- | --- |
| CPU en reposo (vigilando) | < 0,5 % | Media de 10 min en el Administrador de tareas |
| CPU grabando | < 3 % | Media durante 30 min de grabación |
| RAM grabando | < 150 MB | Memoria privada del proceso |
| RAM procesando (pico) | < 3 GB | Un solo modelo cargado a la vez |
| Tiempo de procesado | ≤ 0,4 × duración | 1 h de reunión en ≤ 24 min, enchufado |
| Arranque hasta la bandeja | < 2 s | Desde el inicio de sesión de Windows |
| Aparición del popup | < 3 s | Desde que Meet empieza a usar el micro |
| Disco por hora de reunión | ≈ 60–100 MB | Dos pistas FLAC mono 16 kHz |
| Tamaño del instalador | < 150 MB sin modelos | Modelos descargados aparte (\~1 GB) |

### Requisitos funcionales

- Detectar reuniones de Google Meet en Chrome, Edge y Brave sin que el usuario haga nada.
- Preguntar con un popup antes de confirmar la grabación (sección 6).
- Grabar micro y audio del sistema en pistas separadas y sincronizadas.
- Descartar automáticamente lo que no sea una reunión real.
- Transcribir en español con puntuación; tolerar mezcla con inglés.
- Etiquetar al usuario como «Yo» y separar al resto en «Hablante 1, 2…».
- Funcionar con auriculares o con altavoces (eliminación de eco).
- Permitir renombrar hablantes y recordar sus voces entre reuniones.

### Requisitos no funcionales

- Windows 10 22H2 y Windows 11, x64.
- Recuperación ante cierres inesperados sin perder audio.
- Accesible: navegable con teclado, contraste AA, compatible con lector de pantalla.
- Interfaz y transcripciones en español por defecto; textos de la UI externalizados para traducir.

## 3. Catálogo de funcionalidades

Micnaga tiene 16 funcionalidades: 9 esenciales que forman el núcleo y 7 que lo convierten en una herramienta de verdad. Las marcadas con ★ son propuestas nuevas sobre la idea original.

| ID | Funcionalidad | Prioridad | Fase | Criterio de aceptación |
| --- | --- | --- | --- | --- |
| MIC-01 | Detección automática de Meet | Esencial | F3 | El popup sale en < 3 s en Chrome, Edge y Brave; cero avisos con Meet abierto y el micro sin usar |
| MIC-02 | Popup «¿Estás en una reu?» | Esencial | F3 | Cumple la sección 6 al completo, en tema claro y oscuro |
| MIC-03 | Grabación en dos pistas sincronizadas | Esencial | F0 | Desfase entre pistas < 50 ms tras 1 h; silencios del sistema rellenados |
| MIC-04 | Compuertas anti falsos positivos | Esencial | F3 | Una nota de voz de 2 min se descarta; una reunión real de 4 min se procesa |
| MIC-05 | Eliminación de eco con altavoces | Esencial | F1 | Con altavoces, < 5 % de frases de otros atribuidas a «Yo» |
| MIC-06 | Transcripción local en CPU | Esencial | F1 | WER ≤ 12 % en el set de prueba propio en español |
| MIC-07 | Diarización de la pista remota | Esencial | F2 | DER ≤ 20 % en reuniones de 2 a 5 personas |
| MIC-08 | Cola de procesado en segundo plano | Esencial | F1 | Procesar no sube la CPU por encima del 60 % ni congela el equipo |
| MIC-09 | Recuperación tras cierre inesperado | Esencial | F0 | Matar el proceso a mitad de grabación no pierde más de 2 s de audio |
| MIC-10 | Pastilla flotante «Grabando» | Importante | F3 | Visible siempre que se graba; arrastrable; clic para pausar o parar |
| MIC-11 | ★ Huellas de voz | Importante | F4 | Un compañero habitual sale con su nombre a partir de la segunda reunión |
| MIC-12 | ★ Marcadores con atajo global | Importante | F3 | Ctrl+Alt+M marca el momento; sale resaltado en la transcripción |
| MIC-13 | ★ Pausa privada | Importante | F3 | Ctrl+Alt+P pausa; el tramo pausado no existe en disco |
| MIC-14 | ★ Biblioteca y buscador | Importante | F4 | Búsqueda de texto completo en < 200 ms sobre 500 h de reuniones |
| MIC-15 | ★ Visor con audio sincronizado | Importante | F4 | Clic en una frase y suena desde ese punto |
| MIC-16 | ★ Glosario personal | Importante | F2 | Nombres propios y siglas del glosario salen bien escritos en ≥ 95 % de casos |

### Detalle de las propuestas nuevas

- **Huellas de voz (MIC-11).** Al renombrar «Hablante 2» como «Laura», Micnaga guarda su embedding de voz cifrado en local. En reuniones siguientes compara cada hablante con la base y propone el nombre si la similitud supera un umbral. Borrable por persona.
- **Marcadores (MIC-12).** Durante la reunión, un atajo global deja una marca; en la transcripción ese tramo sale destacado y aparece en un índice al principio. Ideal para «esto me lo apunto».
- **Pausa privada (MIC-13).** Si tienes que hablar de algo que no debe quedar grabado, pausas con un atajo; la pastilla cambia a ámbar y no se escribe nada.
- **Biblioteca (MIC-14) y visor (MIC-15).** Una ventana con todas tus reuniones, buscador instantáneo y una vista de transcripción donde cada frase es clicable y reproduce el audio.
- **Glosario personal (MIC-16).** Lista de nombres de compañeros, proyectos y siglas que el modelo suele escribir mal. Se aplica como corrección tras la transcripción y como palabras clave si el motor lo admite.

## 4. Arquitectura

Micnaga son dos procesos: uno ligero que vigila, pregunta y graba, y otro de trabajo que procesa después y con prioridad baja, comunicados por una cola persistente en SQLite.

&#91;embedded content: arquitectura · 2 procesos, 6 módulos\]

El proceso de trabajo avisa a la interfaz por una tubería local cuando una reunión está lista, y la interfaz muestra el aviso. Si el proceso de trabajo falla, la app sigue grabando con normalidad.

### Módulos

```
micnaga/
  app.py              ← entrada, icono de bandeja, ciclo de vida
  watcher/            ← señales de reunión y máquina de estados (sección 5)
  capture/            ← WASAPI, sincronía, escritura en bloques (sección 7)
  pipeline/
    stages/           ← una etapa por fichero (sección 8)
    engines/asr/      ← parakeet.py, whisper.py (misma interfaz)
    engines/diar/     ← diarize.py, pyannote.py
    aec/              ← webrtc.py, speex.py
  ui/web/             ← popup.html, pill.html, library/ (HTML/CSS/JS)
  store/              ← config.toml, SQLite, huellas de voz cifradas
  bench/              ← banco de pruebas (sección 12)
```

### Contratos entre módulos

- `AsrEngine.transcribe(audio: np.ndarray, sr: int) -> list[Word]` y `Diarizer.diarize(audio, sr, n_speakers: int | None) -> list[Turn]`.
- La tabla `jobs` de SQLite guarda estado y etapa de cada sesión: `pending → processing(stage=n) → done | failed | discarded`.
- Toda la comunicación UI ↔ trabajo son eventos JSON pequeños: `job_progress`, `job_done`, `job_failed`.

## 5. Detección de reunión y compuertas

Micnaga empieza a grabar en cuanto detecta una reunión, pero solo procesa la sesión si al cerrarla pasa tres compuertas; así una nota de voz o una prueba de micro nunca llegan al pipeline.

### Señales (sondeo cada 1 s, coste despreciable)

| Señal | Cómo se lee | Peso |
| --- | --- | --- |
| Navegador usando el micro | Registro `HKCU\...\CapabilityAccessManager\ConsentStore\microphone\NonPackaged\<navegador>`: `LastUsedTimeStop = 0` significa en uso | Obligatoria |
| Pestaña de Meet abierta | UI Automation sobre la barra de pestañas del navegador: nombre que empieza por «Meet» o contiene un código `xxx-xxxx-xxx` | Obligatoria |
| Audio saliendo del navegador | Sesión de audio activa del proceso del navegador (pycaw) | Refuerzo |
| Extensión de navegador (F4) | Mensaje por *native messaging* con estado «en llamada», título y código de la reunión | Sustituye a las demás cuando está instalada |

La detección se dispara con las dos obligatorias estables durante 5 s y se da por terminada cuando cualquiera de ellas cae durante 15 s seguidos.

&#91;embedded content: máquina de estados · 8 estados, 1 decisión\]

Si no respondes al popup, la grabación sigue como provisional y las compuertas deciden. «Ahora no» ignora esa reunión concreta (por su código) hasta que cuelgues; no vuelve a preguntar aunque cambies de pestaña.

### Compuertas anti falsos positivos

Al cerrar, una sesión solo pasa a la cola si cumple las tres (configurables en `[gates]`):

1. **Duración ≥ 3 min** de grabación efectiva, sin contar pausas privadas.
2. **Voz remota ≥ 30 s** detectada por VAD en la pista del sistema. Una nota de voz solo tiene voz en el micro, así que cae aquí.
3. **≥ 6 cambios de turno** entre micro y sistema: en una reunión se habla por turnos; en un vídeo de YouTube con Meet abierto, no.

Una sesión confirmada con «Grabar» solo exige la primera compuerta: si dices que es una reunión, Micnaga te cree. Las descartadas se borran sin procesar y quedan en el log solo con fecha y motivo.

## 6. Popup «¿Estás en una reu?»

El popup es la cara de Micnaga: aparece a los 5 s de detectar una reunión, pregunta una sola vez y, si aceptas, se transforma con una animación en la pastilla de grabación.

&#91;embedded content: maqueta del popup · 400 × 232 px\]

La grabación provisional empieza en el mismo instante en que sale el popup, así que pensarte la respuesta no te hace perder el saludo inicial.

### Comportamiento

| Acción | Qué pasa | Animación |
| --- | --- | --- |
| Aparece | Ventana sin marco, siempre encima, en el monitor donde está el navegador | Sube 16 px y aparece con fundido, 220 ms |
| Grabar (clic o Enter) | La sesión pasa a confirmada | El botón muestra ✓ durante 300 ms; la tarjeta se encoge y vuela hasta la pastilla, 400 ms |
| Ahora no (clic o Esc) | Se para la grabación, se borra lo grabado y se ignora esa reunión | Baja y se desvanece, 180 ms |
| Sin respuesta en 15 s | Sigue grabando como provisional; las compuertas deciden | Se convierte en pastilla con la etiqueta «provisional» |
| La reunión acaba antes de responder | Se descarta todo | Se desvanece, 180 ms |
| Ratón encima | La cuenta atrás se pausa | La barra deja de avanzar |

### Detalles de implementación

- Ventana WebView2 de 400 × 232 px, fondo transparente y esquinas de 12 px; estilo `WS_EX_NOACTIVATE` para no quitar el foco a Meet ni interrumpir lo que escribes.
- Sonido opcional muy suave (desactivado por defecto); con «No molestar» de Windows activo, aparece igual pero en silencio.
- Los niveles son medidores reales de los dos canales a 20 fps: si «Ellos» no se mueve, sabes que algo falla antes de empezar.
- Accesible: foco visible en los botones, `role="alertdialog"` para el lector de pantalla y contraste AA en ambos temas.

### La pastilla de grabación

Cápsula de 200 × 40 px, centrada arriba y arrastrable: punto rojo que late cada 1,2 s, cronómetro y dos mini medidores de nivel. Al pasar el ratón se ensancha y muestra tres botones: marcador, pausa privada y parar. En pausa privada se vuelve ámbar y el cronómetro se congela. Sin ratón encima baja al 60 % de opacidad para no molestar.

## 7. Captura de audio

El grabador escribe dos pistas independientes (micro y sistema), cada bloque con su marca de tiempo de un reloj común, para que el resto del sistema pueda alinearlas al milisegundo.

### Fuentes

- **Sistema:** WASAPI loopback del dispositivo de salida por defecto, con PyAudioWPatch. Si el usuario cambia de dispositivo a mitad de reunión (se pone los auriculares), el grabador se reengancha al nuevo en < 1 s y lo anota en los metadatos.
- **Micro:** el dispositivo de entrada por defecto de comunicaciones (`eCommunications`), que es el que usa Meet.
- **Mejora futura:** captura solo del proceso del navegador (loopback por proceso, Windows 10 2004+), para no grabar notificaciones ni música. Se deja detrás de una opción en F4.

### Tres trampas que el código debe resolver

1. **El loopback no entrega datos en silencio.** Si no suena nada, WASAPI no manda paquetes. Cada bloque se sella con `QueryPerformanceCounter` al llegar; si entre dos bloques hay un hueco, se rellena con ceros de la duración exacta.
2. **Relojes distintos.** Micro y tarjeta de sonido derivan entre sí (hasta 1–2 s por hora). Se registra cada 10 s la pareja (muestras escritas, reloj común) de cada pista; al cerrar, se remuestrea la pista que deriva para que ambas casen. Objetivo: desfase < 50 ms tras 1 h.
3. **Formatos variables.** El loopback suele venir a 48 kHz estéreo float32. Se mezcla a mono y se remuestrea a 16 kHz en el propio hilo de captura con `soxr` (calidad «HQ»), que es lo que esperan los modelos.

### Escritura a disco

- Formato de trabajo: WAV PCM 16 bits mono 16 kHz, escrito en bloques de 2 s con `flush` real. Así un cierre inesperado pierde como máximo 2 s (MIC-09).
- Al confirmar la sesión, se convierte a FLAC (compresión nivel 5) y se borran los WAV.
- Junto al audio, `capture.json` con: dispositivos, tipo de salida (auriculares o altavoces), marcas de sincronía, pausas privadas y marcadores.

### Detección de auriculares

Con pycaw se lee `PKEY_AudioEndpoint_FormFactor` del dispositivo de salida. `Headphones` o `Headset` cuentan como auriculares; `Speakers` o desconocido, como altavoces. Los auriculares Bluetooth en modo manos libres también cuentan como auriculares. El dato decide si se aplica la eliminación de eco (sección 8).

## 8. Pipeline de procesado

El procesado es una cadena de 11 etapas que corre al terminar la reunión; cada etapa guarda su resultado en `work/` y es idempotente, así que si algo se interrumpe se reanuda desde la última etapa terminada.

1. **Compuertas finales.** Se vuelven a comprobar las condiciones de reunión real (sección 5) sobre el audio completo. Si no pasan, la sesión se descarta y se borra.
2. **Alineación.** Se aplica la corrección de deriva registrada en `capture.json` y se verifica que ambas pistas duran lo mismo (± 50 ms).
3. **Eliminación de eco (solo con altavoces).** Cancelador adaptativo usando la pista del sistema como referencia para limpiar la del micro: WebRTC AEC3 si hay binarios para Windows; si no, el de SpeexDSP. El agente compara ambos en F1 y deja el ganador.
4. **Acondicionado.** Filtro paso alto a 80 Hz y normalización de volumen por pista. Sin supresión de ruido agresiva: los modelos de transcripción rinden peor con audio sobreprocesado.
5. **Detección de voz.** Silero VAD (ONNX) por pista: mínimo 250 ms de voz, 500 ms de silencio para cortar y 200 ms de margen. Los tramos se agrupan en bloques de hasta 30 s.
6. **Transcripción.** Parakeet TDT 0.6B v3 en int8 vía `onnx-asr` o `sherpa-onnx`, con marcas de tiempo por palabra. Alternativa configurable: faster-whisper (`small` o `medium`, int8, `language="es"`). Hasta 6 hilos y prioridad «por debajo de lo normal».
7. **Diarización de la pista remota.** Librería `diarize` (Apache 2.0, solo CPU, sin token) como opción por defecto; pyannote community-1 como alternativa.
8. **Asignación.** Cada palabra de la pista remota va al hablante con el que más se solapa en el tiempo. Toda palabra del micro es «Yo».
9. **Eco, segunda capa.** Se descarta cualquier frase de «Yo» que coincida en tiempo con una remota y se le parezca en ≥ 80 % (`rapidfuzz`). Red de seguridad para lo que el cancelador no quite.
10. **Nombres y glosario.** Se comparan los embeddings de cada hablante con las huellas guardadas (MIC-11) y se aplica el glosario personal (MIC-16).
11. **Montaje y salida.** Se unen frases consecutivas del mismo hablante separadas por < 1,5 s, se forman párrafos y se escriben los ficheros de la sección 9.

### Gestión de memoria y CPU

- Un modelo cargado a la vez: se carga, se usa, se libera y se llama a `gc.collect()` antes del siguiente.
- El proceso de trabajo es separado del de la UI, con `BELOW_NORMAL_PRIORITY_CLASS`, para que un fallo en el procesado no tumbe la app.
- Hilos configurables (`processing.threads`, por defecto 6), dejando núcleos libres para el usuario.

## 9. Salidas y estructura de carpetas

Cada reunión es una carpeta autocontenida en `Documentos\Micnaga\`, con un Markdown legible para personas y un JSON completo para máquinas.

```
Documentos\Micnaga\
  2026-11-18_1030_revision-semanal\
    transcripcion.md      ← lo que lee el usuario
    transcripcion.srt     ← subtítulos
    transcripcion.json    ← fuente de verdad
    audio_yo.flac
    audio_remoto.flac
    capture.json          ← metadatos de captura
  micnaga.db              ← índice SQLite (FTS5) de todas las reuniones
```

### transcripcion.md

```markdown
# Revisión semanal — 18/11/2026 10:30 (47 min)

Participantes: Yo, Laura, Hablante 2

## Marcadores
- [12:04] Decidir proveedor antes del viernes

## Transcripción
**[00:00:12] Yo:** Buenos días, ¿me oís bien?
**[00:00:15] Laura:** Perfecto. Empezamos por lo del despliegue...
```

### transcripcion.json (esquema)

```json
{
  "version": 1,
  "meeting": {"title": "Revisión semanal", "start": "2026-11-18T10:30:00+01:00", "duration_s": 2820,
              "source": "meet", "meet_code": "abc-defg-hij", "output_device": "headphones"},
  "speakers": [{"id": "me", "name": "Yo"}, {"id": "S1", "name": "Laura", "voiceprint_match": 0.84}],
  "segments": [{"start": 12.3, "end": 14.1, "speaker": "me", "text": "Buenos días, ¿me oís bien?",
                "words": [{"w": "Buenos", "s": 12.3, "e": 12.6, "p": 0.97}]}],
  "markers": [{"t": 724.0, "label": "Decidir proveedor antes del viernes"}],
  "engines": {"asr": "parakeet-tdt-0.6b-v3-int8", "diarization": "diarize-x.y"}
}
```

El JSON es la fuente de verdad: Markdown y SRT se regeneran a partir de él cuando el usuario renombra un hablante o corrige un texto. El nombre de la carpeta sale del título de la reunión (pestaña de Meet), pasado a minúsculas sin acentos ni espacios.

## 10. Interfaz y configuración

Micnaga vive en la bandeja del sistema y solo muestra ventanas cuando hace falta: el popup al detectar una reunión, la pastilla mientras graba y la biblioteca cuando el usuario la abre.

### Superficies

| Superficie | Cuándo aparece | Qué hace |
| --- | --- | --- |
| Icono de bandeja | Siempre | Estado por color: gris vigilando, rojo grabando, ámbar en pausa, azul procesando; menú con «Grabar ahora», «Pausar vigilancia 1 h», «Abrir biblioteca», «Ajustes», «Salir» |
| Popup «¿Estás en una reu?» | Al detectar una reunión | Sección 6 |
| Pastilla flotante | Mientras graba | Punto rojo que late, cronómetro, nivel de los dos canales, botones de pausa y parar; arrastrable y semitransparente si no tiene el ratón encima |
| Notificación «Lista» | Al terminar el procesado | Toast nativo de Windows: «Revisión semanal · 47 min · 3 hablantes», con botón «Abrir» |
| Biblioteca | A petición | Lista de reuniones, buscador, visor con audio sincronizado, renombrado de hablantes |
| Ajustes | A petición | Todas las opciones de `config.toml` con interfaz gráfica |

### Lenguaje visual

- Tipografía Segoe UI Variable; esquinas de 12 px; sombras suaves de una sola capa.
- Fondo con efecto Mica/acrílico de Windows 11 (degradado a color sólido en Windows 10).
- Un único color de acento (rojo coral para «grabar») y el resto en neutros; respeta el tema claro/oscuro del sistema.
- Animaciones de 180–240 ms con curva `cubic-bezier(0.2, 0.8, 0.2, 1)`; desactivadas si Windows tiene «Efectos de animación» apagado.

### config.toml (valores por defecto)

```toml
[detection]
browsers = ["chrome.exe", "msedge.exe", "brave.exe"]
confirm_after_s = 5          # señales estables antes del popup
popup_timeout_s = 15
on_timeout = "provisional"   # provisional | discard

[gates]
min_duration_s = 180
min_remote_speech_s = 30
min_turns = 6

[capture]
sample_rate = 16000
chunk_flush_s = 2

[processing]
asr_engine = "parakeet"      # parakeet | whisper
whisper_model = "small"
diarization = "diarize"      # diarize | pyannote
threads = 6

[storage]
root = "~/Documents/Micnaga"

[hotkeys]
marker = "ctrl+alt+m"
private_pause = "ctrl+alt+p"
```

## 11. Stack, dependencias y empaquetado

Núcleo en Python 3.11 y la interfaz en HTML/CSS dentro de WebView2 (ya incluido en Windows 11), que da animaciones y efectos «súper chulos» sin cargar un framework gráfico pesado.

| Capa | Elección | Motivo |
| --- | --- | --- |
| Captura | `PyAudioWPatch`, `soxr`, `soundfile` | WASAPI loopback, remuestreo de calidad, WAV/FLAC |
| Sistema Windows | `pywin32`, `pycaw`, `psutil` | Ventanas, registro, tipo de dispositivo, batería, prioridad |
| Atajos globales | `RegisterHotKey` vía `pywin32` | Sin permisos de administrador ni hooks de teclado |
| VAD | Silero VAD (ONNX) | Ligero y preciso en CPU |
| Transcripción | `onnx-asr` o `sherpa-onnx` + Parakeet TDT 0.6B v3 int8; `faster-whisper` como alternativa | Rápido en CPU, español con puntuación |
| Diarización | `diarize`; `pyannote.audio` community-1 como alternativa | Sin GPU ni token |
| Eco | WebRTC AEC3 o SpeexDSP | Se elige en F1 por resultados |
| Texto | `rapidfuzz` | Deduplicado de eco y glosario |
| UI | `pywebview` (WebView2) + `pystray` | Popup, pastilla y biblioteca en HTML/CSS; icono de bandeja |
| Notificaciones | `windows-toasts` | Toasts nativos con botones |
| Datos | SQLite con FTS5 (incluido en Python) | Biblioteca y buscador |
| Empaquetado | PyInstaller (onedir) + Inno Setup | Instalador clásico con desinstalador |

### Instalación y arranque

- El instalador no lleva modelos. En el primer arranque, un asistente de 3 pasos descarga los modelos (\~1 GB) con barra de progreso, verifica sus SHA-256 y comprueba micro y audio del sistema.
- Arranque con Windows mediante la clave `HKCU\...\Run`, activado por defecto y desactivable en Ajustes.
- Datos de usuario en `%APPDATA%\Micnaga` (configuración, huellas de voz, logs) y reuniones en `Documentos\Micnaga`.

### Licencias

- Código de Micnaga: MIT.
- Parakeet TDT 0.6B v3 y pyannote community-1 son CC-BY-4.0: atribución obligatoria en «Acerca de» y en el README.
- El agente genera `THIRD_PARTY_LICENSES.md` con la licencia de cada dependencia y modelo, y bloquea cualquier dependencia con licencia incompatible con MIT.

## 12. Pruebas y benchmarks

La calidad se mide con un set de prueba propio y un banco de pruebas automático; ninguna cifra de esta especificación se da por buena hasta medirla en el equipo de referencia.

### Set de prueba

- **10 grabaciones de reuniones reales** (con permiso de los participantes), de 5 a 60 min, con 2 a 5 personas; la mitad con auriculares y la mitad con altavoces.
- **Transcripción de referencia** corregida a mano para 3 de ellas, con hablantes etiquetados.
- **Casos trampa:** nota de voz de WhatsApp Web de 2 min, prueba de micro en Meet de 30 s, Meet abierto con el micro silenciado durante 1 h, vídeo de YouTube sonando con Meet abierto, cambio de auriculares a altavoces a mitad.
- El set vive fuera del repositorio (`MICNAGA_TESTSET`) y nunca se sube.

### Métricas

| Métrica | Objetivo | Herramienta |
| --- | --- | --- |
| WER en español | ≤ 12 % | `jiwer` contra las referencias |
| DER (pista remota) | ≤ 20 % | `pyannote.metrics` o `meeteval` |
| Frases de otros como «Yo» (altavoces) | < 5 % | Script propio sobre las referencias |
| Falsos positivos de detección | 0 en los casos trampa | Test de integración |
| Factor de procesado | ≤ 0,4 × duración | `bench.py` enchufado y con batería |

### Banco de pruebas

`python -m micnaga.bench` procesa el set, mide tiempos por etapa, picos de RAM y CPU, y escribe un informe en Markdown con la comparativa contra la ejecución anterior. Se lanza en cada fase y antes de cada versión, y sus resultados deciden entre motores (Parakeet o Whisper, `diarize` o pyannote, WebRTC o SpeexDSP).

## 13. Riesgos, privacidad y aspectos legales

El mayor riesgo no es técnico sino de uso: grabar a otras personas y guardar huellas de su voz exige avisar y, en el trabajo, respetar las normas de la organización.

### Riesgos técnicos

| Riesgo | Impacto | Mitigación |
| --- | --- | --- |
| Meet cambia el título de la pestaña o su comportamiento | Detección rota | Señales redundantes (sección 5) y extensión opcional como fuente principal en F4 |
| Procesado más lento de lo previsto en el i5-1245U | Esperas largas | Motor Whisper `small` como plan B; benchmarks desde F1 |
| Eco mal cancelado con altavoces | Frases atribuidas a «Yo» por error | Dos capas (AEC + deduplicado por texto) |
| Diarización floja con muchas personas | Hablantes mezclados | Renombrado y fusión manual de hablantes en el visor |
| Cambio de dispositivo de audio a mitad | Hueco o fallo de captura | Reenganche automático en < 1 s, anotado en metadatos |
| Antivirus o políticas corporativas bloquean la app | No arranca en el equipo de trabajo | Binario firmado si es posible; comprobar antes con el equipo de sistemas |

### Privacidad por diseño

- Todo en local; ni una petición de red salvo la descarga de modelos.
- Huellas de voz cifradas con DPAPI de Windows (ligadas a la cuenta del usuario) y borrables por persona.
- La pastilla de grabación siempre visible mientras se graba: nunca hay grabación silenciosa.

### Aspectos legales (orientativo, no es asesoramiento jurídico)

- En España, grabar una conversación en la que participas es en general lícito, pero difundir su contenido puede no serlo.
- Fuera del uso estrictamente doméstico, como en reuniones de trabajo, aplica el RGPD: conviene avisar a los asistentes al empezar y seguir la política interna de la organización.
- Las huellas de voz usadas para identificar a personas son datos biométricos, una categoría especial en el RGPD. Por eso MIC-11 viene desactivada por defecto y la app recuerda pedir consentimiento antes de guardar la huella de un compañero.
- El popup incluye un recordatorio discreto y desactivable: «Recuerda avisar de que grabas».

## 14. Roadmap por fases

Cinco fases, cada una entregable y usable por sí sola; el agente no empieza una fase hasta que la anterior pasa su puerta de salida.

### F0 — Grabador fiable

- [ ] CLI `micnaga record` que graba micro y sistema en dos WAV con marcas de tiempo (MIC-03)
- [ ] Relleno de silencios del loopback y corrección de deriva
- [ ] Escritura en bloques de 2 s y recuperación al arrancar (MIC-09)
- [ ] Detección de auriculares o altavoces en `capture.json`

Puerta: 1 h grabada con desfase < 50 ms; matar el proceso pierde ≤ 2 s.

### F1 — Transcripción «Yo / Ellos»

- [ ] Pipeline etapas 2–6 y 8–9, sin diarización
- [ ] Comparativa de AEC (WebRTC frente a SpeexDSP) y elección (MIC-05)
- [ ] Parakeet y faster-whisper detrás de la misma interfaz (MIC-06)
- [ ] Cola en proceso aparte, prioridad baja (MIC-08)
- [ ] Salidas Markdown, SRT y JSON
- [ ] `micnaga.bench` con el set de prueba

Puerta: WER ≤ 12 %, < 5 % de eco como «Yo», factor ≤ 0,4.

### F2 — Hablantes

- [ ] Diarización de la pista remota con `diarize` y alternativa pyannote (MIC-07)
- [ ] Asignación de palabras a hablantes
- [ ] Glosario personal (MIC-16)

Puerta: DER ≤ 20 % en el set de prueba.

### F3 — Automático y bonito

- [ ] Vigilante y máquina de estados (MIC-01, sección 5)
- [ ] Popup «¿Estás en una reu?» (MIC-02, sección 6)
- [ ] Compuertas anti falsos positivos (MIC-04)
- [ ] Pastilla flotante, marcadores y pausa privada (MIC-10, MIC-12, MIC-13)
- [ ] Icono de bandeja, ajustes, instalador y asistente de primer arranque

Puerta: cero falsos positivos en los casos trampa; una semana de uso real sin intervención manual.

### F4 — Biblioteca

- [ ] Biblioteca con buscador FTS5 y visor con audio sincronizado (MIC-14, MIC-15)
- [ ] Renombrado de hablantes y huellas de voz opcionales (MIC-11)
- [ ] Extensión de navegador opcional como señal de detección principal

Puerta: buscar en 500 h en < 200 ms; un compañero habitual reconocido a la segunda reunión.
