# DECISIONS.md — Ola 0 (cimientos)

Registro de decisiones no cubiertas por `docs/SPEC.md`, resultados de instalación y convenciones
compartidas. Todo lo marcado «verificado» se comprobó instalando e importando en este PC
(Windows 11, Python 3.11.15, venv en `.venv`, fecha 2026-11-18).

## 0. Avisos importantes (leer primero)

1. **`diarize` arrastra torch.** `diarize 0.1.2` (Apache-2.0) depende de `torch<2.9` +
   `torchaudio<2.9` + `silero-vad` (pip) + `scikit-learn` + `scipy` + `wespeakerruntime`. En este
   PC se instaló `torch 2.8.0+cpu` (`.venv\Lib\site-packages\torch` ≈ **1,3 GB**). Esto choca con
   el presupuesto de «instalador < 150 MB sin modelos» (spec §2) y con «una sola librería pesada
   cargada a la vez». Se mantiene instalado porque es el diarizador por defecto de la spec y los
   agentes posteriores no pueden instalar nada, pero:
   * Está **confinado al extra `diar`**. Ningún módulo del núcleo, de `asr`, de la captura ni del
     VAD puede importar `torch`, `torchaudio`, `silero_vad` ni `diarize` a nivel de módulo
     (hay un test: `tests/test_import_hygiene.py`).
   * Opciones a evaluar en F2 con el banco de pruebas: (a) dejar `diarize` en un proceso/venv
     aparte solo para la etapa 7; (b) sustituirlo por embeddings ONNX (WeSpeaker/3D-Speaker vía
     onnxruntime) + clustering propio con scikit-learn, sin torch; (c) pyannote (también torch).
   * `diarize.diarize(audio_path, *, min_speakers, max_speakers, num_speakers)` **solo acepta una
     ruta a fichero** (no un array). El adaptador `diar/diarize.py` debe escribir un WAV temporal
     en `work/` y convertir `DiarizeResult.segments` (`.start/.end/.speaker` = `SPEAKER_00`...) a
     `Turn("S1"...)`. Además `diarize` ejecuta su propio VAD Silero (torch).
2. **VAD sin torch.** El paquete pip `silero-vad` depende de torch. El VAD de la etapa 5 debe cargar
   el modelo **ONNX** de Silero directamente con `onnxruntime` (sin importar `silero_vad`). El fichero
   `.onnx` se descarga con los demás modelos (SHA-256 fijado).
3. **Paquetes presentes solo por transitividad** (no los declara el núcleo; no usarlos fuera de lo
   indicado): `torch`, `torchaudio`, `silero-vad`, `wespeakerruntime`, `scikit-learn`, `pydantic`
   (todos vía `diarize`, solo dentro de `diar`); `huggingface-hub`, `av`, `ctranslate2`, `tokenizers`
   (vía `faster-whisper`; usar solo dentro de `asr`); `pandas` (vía `pyannote.metrics`, solo bench);
   `winrt-*` (vía `windows-toasts`, solo ui). `scipy` SÍ está declarado en el núcleo (filtro paso alto).
   Si un agente necesita otra librería, debe pedirla; no hay instalaciones posteriores.
4. **WER/DER:** `meeteval` (spec §12) **no se pudo instalar**: compila una extensión Cython y no hay
   compilador MSVC (`Microsoft Visual C++ 14.0 or greater is required`). Alternativa instalada y
   verificada: `pyannote.metrics 4.2.0` (DER) y `jiwer 4.0.0` (WER), sin torch. Van en los extras
   `bench` y `dev`.
5. **El audio de este PC no se enumera desde el proceso de pruebas:** `PyAudioWPatch` importa bien
   pero `PyAudio().get_device_info_generator()` no devolvió dispositivos y
   `get_default_wasapi_loopback()` lanzó `OSError -9996` al ejecutarse desde el agente. Los tests de
   captura real deben ir marcados `@pytest.mark.realdevice` y ejecutarse en la sesión interactiva del usuario.

## 1. Resultados de instalación (verificado)

| Paquete | Resultado | Versión | Notas |
| --- | --- | --- | --- |
| numpy | OK | 2.4.6 | |
| scipy | OK | 1.17.1 | declarado en núcleo |
| soxr | OK | 1.1.0 | |
| soundfile | OK | 0.14.0 | FLAC/WAV incluidos (libsndfile en la rueda) |
| PyAudioWPatch | OK | 0.2.12.9 | importa; ver aviso 5 |
| pywin32 | OK | 312 | |
| pycaw | OK | 20260927 | |
| comtypes | OK | 1.5.0 | |
| psutil | OK | 7.2.2 | |
| onnxruntime | OK | 1.32.0 | CPU |
| rapidfuzz | OK | 3.14.6 | |
| tomli-w | OK | 1.2.0 | añadido al núcleo (escritor de TOML; la lectura usa `tomllib`) |
| onnx-asr | OK | 0.12.0 | MIT; extra `[cpu]` (onnxruntime). No se usa `[hub]`: los modelos se descargan con URL + SHA-256 propios |
| sherpa-onnx | OK | 2.0.1 | Apache-2.0 (+ `sherpa-onnx-core`) |
| faster-whisper | OK | 1.2.1 | MIT; arrastra `ctranslate2 4.9.0`, `av`, `huggingface-hub`, `tokenizers` |
| diarize | OK | 0.1.2 | Apache-2.0 (comprobado en `licenses/LICENSE`); **arrastra torch 2.8.0+cpu**, ver §0 |
| pywebview | OK | 6.2.1 | BSD-3; arrastra `pythonnet`, `clr-loader` |
| pystray | OK | 0.19.5 | |
| windows-toasts | OK | 1.3.1 | arrastra `winrt-*` |
| Pillow | OK | 13.0.0 | |
| pyannote.audio | **NO instalado a propósito** | (4.1.0 resolvería) | `pip --dry-run` resuelve con pytorch-lightning, torchmetrics, torchcodec... Solo extra `pyannote` |
| meeteval | **FALLA** | — | necesita compilador C (Cython); alternativa `pyannote.metrics` |
| ruff / mypy / pytest / pytest-timeout / jiwer / pyannote.metrics / pyinstaller / types-* | OK | 0.17.5 / 2.4.1 / 9.2.0 / 2.4.0 / 4.0.0 / 4.2.0 / 6.22.3 | `types-psutil`, `types-pywin32`, `types-Pillow` |

### Cancelación de eco (candidatos probados)

| Paquete | Resultado | Decisión |
| --- | --- | --- |
| `aec-audio-processing 1.0.1` | **OK** (rueda `cp311-win_amd64` con `webrtc-audio-processing-2-1.dll`; BSD-3-Clause). Se creó `AudioProcessor(enable_aec=True, enable_ns=False, enable_agc=False, enable_vad=False)` y se procesó una trama. | **`webrtc`** (AEC3). API: `AudioProcessor.set_stream_format(16000,1)`, `set_reverse_stream_format(16000,1)`, `process_reverse_stream(bytes_int16_10ms)` + `process_stream(bytes_int16_10ms) -> bytes`; trama = 160 muestras a 16 kHz; `set_stream_delay(ms)` disponible. Desactivar NS/AGC/VAD (spec §8.4: nada de supresión agresiva). |
| `pyaec 1.0.1` | **OK**: `Aec(160, 1600, 16000, True).cancel_echo(list, list)` funciona (rueda con `aec.dll`; la DLL contiene cadenas `speex`, ocho apariciones, así que es un envoltorio de SpeexDSP). **Licencia: sin declarar en los metadatos → «instala, licencia sin verificar».** | **`speex`**, pendiente de verificar licencia antes de empaquetar (spec §11 prohíbe licencias incompatibles). Su wrapper Python convierte listas por trama (lento): el adaptador debe llamar a `pyaec.lib.AecCancelEcho` con ctypes y buffers `int16` contiguos. |
| `speexdsp 0.1.1` | **FALLA**: no hay ruedas; el build desde fuente falla (`command 'swig.exe' failed`) | descartado |
| `webrtc-audio-processing 0.1.3` | **FALLA**: sin ruedas; build desde fuente falla (`ValueError: list.remove(x)` en su `setup.py`; tampoco hay compilador C) | descartado |
| `webrtc-noise-gain` | sin rueda Windows (solo manylinux) | descartado (además es NS/AGC, no AEC) |
| `pyspeexdsp` | no existe en PyPI | — |
| `livekit 1.3.0` (APM) | se instaló y `rtc.AudioProcessingModule(echo_cancellation=True)` se creó, pero arrastra todo el SDK de LiveKit; **desinstalado** | descartado (dependencia desproporcionada) |

La elección final entre `webrtc` y `speex` se hace en F1 con el banco de pruebas (cambiar `AUTO_ORDER`
en `micnaga/pipeline/aec/__init__.py`).

## 2. Contratos compartidos y convenciones

* **Layout:** paquete plano `micnaga/` en la raíz del repo (como el árbol de spec §4), descubrimiento
  explícito (`[tool.setuptools.packages.find] include = ["micnaga*"]`), datos de paquete `ui/web/**/*`
  y `py.typed`. Versión dinámica desde `micnaga.__version__`.
* **Instalación en desarrollo:** `uv pip install --python .venv\Scripts\python.exe -e ".[asr,diar,aec,ui,dev]"`
  (comprobado: resuelve a lo ya instalado; `uv pip check` limpio).
* **Audio en memoria:** `AudioArray = NDArray[np.float32]`, mono, [-1, 1]; `sr` aparte. Conversión a int16 /
  tramas de 10 ms: responsabilidad del adaptador (AEC).
* **Tiempos:** segundos desde el inicio del audio grabado (sin las pausas privadas). `Marker.t`,
  `Word.s/e`, `Turn`, `Segment` comparten esa línea de tiempo. `capture.json → pauses[].at_s` es la
  posición del corte en esa línea y `wall_s` lo que duró la pausa en tiempo real.
* **Hablantes:** `"me"` (Yo) y `"S1"`, `"S2"`... Los diarizadores deben normalizar a ese esquema.
* **`types.py` incluye además** `CaptureMeta` (`capture.json`, escribe `capture/`, lee `pipeline/`; admite
  campos extra en `extra`), `PIPELINE_STAGES` (11 nombres; `jobs.stage` = índice 1-based de la última etapa
  *iniciada*) y `Transcript` (esquema exacto de `transcripcion.json` v1; `Speaker.voiceprint_match` se omite
  si es `None`). `Word.p` tiene valor por defecto 1,0.
* **Registro perezoso de motores** (`micnaga/registry.py`): `LazyRegistry` mapea `nombre -> "modulo:funcion"`.
  Fábricas: `create(config: Config) -> Protocol` en
  `micnaga.pipeline.engines.asr.{parakeet,whisper}`, `micnaga.pipeline.engines.diar.{diarize,pyannote}`,
  `micnaga.pipeline.aec.{webrtc,speex}`. Si el módulo no existe aún o falta una dependencia se lanza
  `EngineUnavailableError` (causa en `__cause__`); un nombre desconocido, `UnknownEngineError`.
  `processing.aec`: `auto` (webrtc→speex, el primero que cargue) | `webrtc` | `speex` | `off` (`None`).
* **Capacidad opcional de huellas:** `SpeakerEmbedder` es un Protocol aparte (`isinstance(d, SpeakerEmbedder)`),
  no un método opcional de `Diarizer` (incómodo bajo `mypy --strict`).
* **Rutas** (`micnaga/paths.py`): `MICNAGA_HOME` (datos de app, por defecto `%APPDATA%\Micnaga`),
  `MICNAGA_STORAGE` (raíz de reuniones + `micnaga.db`, gana sobre `[storage] root`), `MICNAGA_MODELS`
  (modelos, gana sobre `[storage] models_dir`, por defecto `<datos de app>\models`). Se leen **en cada
  llamada**, jamás al importar; el módulo no crea carpetas (`ensure_dir` explícito). Para desarrollo con los
  modelos del repo: `MICNAGA_MODELS=<repo>\.models` (carpeta ignorada por git).
* **Config** (`config.toml`): además de la spec §10 se añadieron `detection.poll_interval_s=1`,
  `detection.end_after_s=15`, `capture.sync_log_interval_s=10`, `capture.flac_level=5`, tabla `[vad]`
  (`min_speech_ms=250`, `min_silence_ms=500`, `pad_ms=200`, `max_block_s=30`), `processing.aec="auto"`,
  `processing.low_priority=true`, `processing.echo_similarity=0.8`, `processing.merge_gap_s=1.5`,
  `processing.glossary=[]`, `storage.models_dir=""`, `[ui] theme="system" language="es" sound=false`,
  `[privacy] voiceprints_enabled=false voiceprint_threshold=0.75 remind_notice=true`,
  `[startup] run_at_login=true`. **`voiceprint_threshold=0.75` es un valor provisional** a calibrar en F4.
  Claves desconocidas se ignoran; tipos incorrectos o enumerados inválidos lanzan `ConfigError`. La escritura
  no conserva comentarios.
* **Base de datos** (`micnaga/store/db.py`): SQLite WAL + `busy_timeout` 10 s + `foreign_keys`; tablas `jobs`,
  `meetings`, `segments`, `segments_fts` (FTS5 de contenido externo, `unicode61 remove_diacritics 2`, comprobado
  que «reunion» encuentra «reunión»), `discard_log(date, reason)` y `PRAGMA user_version`. Transiciones
  atómicas con `BEGIN IMMEDIATE`; `claim_next_job` es seguro entre procesos (test con 4 hilos/conexiones).
  `requeue_stale()` devuelve a `pending` lo que quedó `processing` tras un cierre (a `failed` tras 3 intentos).
  No se creó tabla `ignored_meetings`: «Ahora no» es estado en memoria hasta que se cuelga (no se persiste el
  código de reunión; privacidad). `session_dir` se guarda como cadena tal cual se pasa.
  Una instancia de `Database` por hilo.
* **Eventos** (`micnaga/events.py`): `job_started`, `job_progress`, `job_done`, `job_failed`, `job_discarded`;
  una línea JSON UTF-8 por evento (`encode_line`/`decode_line`). `pipe_name()` deriva el nombre de la tubería
  de `MICNAGA_HOME` (tests e instancias de desarrollo no se pisan). El transporte lo implementan `pipeline/` y `app`.
* **Logs:** `setup_logging("<proceso>")` → RotatingFileHandler 1 MB × 5 en `<datos de app>\logs\<proceso>.log`.
  Sin red. Hay que llamarlo explícitamente.
* **Tests:** `tests/conftest.py` fija `MICNAGA_HOME/STORAGE/MODELS` (y `APPDATA`) a carpetas temporales con un
  fixture autouse. Excepción: con `MICNAGA_RUN_MODELS=1` y `MICNAGA_MODELS` ya definido se respeta esa carpeta
  de modelos real. Marcadores: `models` (necesita `MICNAGA_RUN_MODELS=1`), `slow` (`MICNAGA_RUN_SLOW=1` o
  `MICNAGA_RUN_MODELS=1`), `realdevice` (`MICNAGA_RUN_REALDEVICE=1`). Audio sintético en `tests/synth.py`
  (`from tests.synth import sine, noise, speech_like, speech_bursts, add_echo, write_wav...`). Los subdirectorios
  de tests nuevos deben llevar `__init__.py`.
* **mypy:** `strict = true`, `files = ["micnaga"]`; las librerías sin tipos están en
  `[[tool.mypy.overrides]] ignore_missing_imports`. **ruff:** línea 100, reglas `E F W I UP B SIM C4 RUF`.

* **Descartes y privacidad:** `Database.discard_job` anota solo fecha + motivo en `discard_log` y además
  sustituye `jobs.session_dir` por `"discarded:<id>"` (la carpeta lleva fecha y título). El llamador debe
  guardar la ruta original antes de descartar: `discard_job` **no borra** la carpeta.
* **Canal de eventos:** la UI/app es el servidor de la tubería y el worker el cliente; el worker conecta
  perezosamente y descarta el evento sin error si nadie escucha. La fuente de verdad es `jobs` en SQLite.
* **Nombres de fichero** de la carpeta de reunión como constantes en `paths.py` (`TRANSCRIPT_*`,
  `AUDIO_ME_FLAC`, `AUDIO_REMOTE_FLAC`, `CAPTURE_JSON`, WAV de trabajo `AUDIO_*_WAV`).
* **Aislamiento de tests por sesión:** `pytest_configure` fija las variables a una carpeta temporal de sesión
  (cubre fixtures de módulo/sesión y la recopilación) y `isolated_env` las renueva por test. Con
  `MICNAGA_RUN_MODELS=1` y sin `MICNAGA_MODELS`, los modelos van a `<repo>/.models` (compartido).
  `timeout = 60` global; los tests con modelos usan `@pytest.mark.timeout(600)`.
* **`types-Pillow` retirado:** Pillow 13 trae `py.typed`; los stubs 10.2 lo habrían pisado.
* Todos los campos `float` de la config tienen valor por defecto `float` (si no, `2.5` se rechazaba como
  «no entero»); hay tests.
* `create_echo_canceller("auto")` registra con WARNING cada candidato omitido.

## 3. Convención de despacho del CLI (para no editar `cli.py`)

`micnaga <comando> [args...]` solo reconoce el comando y llama a la función indicada pasándole el resto de
`argv` **sin parsear**; cada módulo define su propio `argparse` (incluido `--help`):

```python
def run(argv: list[str]) -> int: ...  # código de salida
```

| Comando | Función | Dueño |
| --- | --- | --- |
| `record` | `micnaga.capture.cli:run` | capture/ |
| `process` | `micnaga.pipeline.cli:run_process` | pipeline/ |
| `worker` | `micnaga.pipeline.cli:run_worker` | pipeline/ |
| `bench` | `micnaga.bench.cli:run` (también `python -m micnaga.bench`) | bench/ |
| `app` | `micnaga.app:run` (stub que lanza `NotImplementedError`) | app.py |

El módulo se importa de forma perezosa. «not implemented» (código 2) solo cuando falta *ese* módulo (o un
paquete `micnaga.*` que lo contiene) o la función lanza `NotImplementedError`; cualquier otro `ImportError`
(dependencia ausente, bug) se propaga con su traza.

## 4. Pendiente / fuera de alcance de la ola 0

* No se descargaron modelos. No se escribió nada en el registro ni en `%APPDATA%\Micnaga` / `Documentos\Micnaga`.
* No se hizo commit.
* `THIRD_PARTY_LICENSES.md`: generado por `scripts/gen_licenses.py` (ver §14).
* Licencia de `pyaec` verificada: MIT (repo `thewh1teagle/aec`, ver §14).

---

# Ola 1 y siguientes — decisiones por módulo

Fusionado desde `docs/agent_reports/wave1_*.md` (los informes siguen ahí con el detalle de API).

## 5. Desviaciones de la spec y puertas no verificadas (leer primero)

1. **Diseño de detección cambiado por el usuario (vía genérica).** La spec §5/§6 define Meet
   (micro del navegador + pestaña de Meet estables 5 s) → popup. Por petición expresa del usuario se
   añadió una **vía genérica para cualquier app con el micro** (`[detection] generic_apps=true`,
   `mic_grace_s=20`, `popup_after_recording_s=20`, `exclude_apps=[]`): grabación provisional a los
   20 s de uso continuo del micro y popup 20 s después (20 s + 20 s). Meet conserva la vía rápida
   (5 s) y una sesión genérica de un navegador se «asciende» a Meet si aparece su pestaña. **Esto se
   aparta de §5/§6** (cuya detección es solo Meet y cuyo popup sale a los 5 s). Nuevo estado
   `RECORDING_PRE_POPUP` (ver `docs/state_machine.md`). Consecuencia conocida: apps de dictado
   (p. ej. `handy.exe`) inician una grabación provisional que las compuertas descartan; se pueden
   excluir en `exclude_apps`.
2. **Popup < 3 s (§2) frente a 5 s estables (§5).** La spec es contradictoria. Se usa
   `detection.confirm_after_s` (5 s ⇒ popup a los ~5–6 s). Para cumplir §2 basta ponerlo a 2 (a costa
   de más falsos arranques que las compuertas descartarán).
3. **Puertas de fase (§14) NO verificadas.** Requieren grabaciones reales del usuario, el equipo de
   referencia (i5-1245U, 16 GB) o una semana de uso real, y no se pudo: F0 («1 h grabada con desfase
   < 50 ms; matar el proceso pierde ≤ 2 s»: solo con audio sintético y backend simulado), F1 (WER
   ≤ 12 %, < 5 % de eco, factor ≤ 0,4: solo TTS español de SAPI y escenarios sintéticos; medidas en
   un i7-12700F, no en el i5-1245U), F2 (DER ≤ 20 %: sin set real, umbrales de diarización sin
   calibrar), F3 («cero falsos positivos en los casos trampa; una semana de uso real») y F4
   («buscar en 500 h en < 200 ms»; «compañero reconocido a la segunda reunión»). **Las fases se
   construyeron en secuencia igualmente.** Pendiente del usuario: `MICNAGA_RUN_REALDEVICE=1`,
   set de prueba real (`MICNAGA_TESTSET`, `docs/testset_format.md`) y uso diario.
4. **Torch frente al presupuesto de 150 MB (§2).** `diarize` arrastra torch + torchaudio (~1,3 GB en
   disco, ~340 MB de RSS solo por importarlo) y hasta el embedding (WeSpeaker) usa
   `torchaudio.compliance.kaldi`, de modo que no hay diarización sin torch. Decisión de empaquetado:
   **dos variantes**: `lite` (por defecto, sin torch: sin diarización, todo lo remoto es `S1`) y
   `full` (con `diarize` + torch, >1 GB, distribución aparte). Ver §13.
5. **Una sola cuenta atrás del popup.** La UI (JS, 15 s, pausa con hover) y el vigilante llevan cada
   una la suya. Manda el vigilante (`Watcher.popup_remaining_s`, se congela al perder señal o en
   pausa privada); la UI solo la muestra y cierra con `close_popup`. Lo resuelve la integración en
   `micnaga/app.py`.

## 6. Captura (`micnaga/capture`)

1. Huecos medidos entre llegadas de paquetes (no contra un reloj acumulado); umbrales 0,1 s sistema /
   0,25 s micro; relleno de silencio en reposo hasta `now − 0,5 s`.
2. Marcas de sincronía: en cada ventana de 10 s el paquete con menor `clock − samples/sr`; anclas en
   inicio, pausas y parada. Deriva de 1,5 s/h corregida a < 10 ms en test (`capture.sync.align_tracks`).
3. La pausa privada termina 0,3 s después de `resume()` (guarda) para no filtrar audio latente.
4. Flush cada 1,75 s con reescritura de cabecera + fsync (pérdida medida al matar: 1,3–1,6 s).
5. FLAC nivel 5 = `compression_level 5/8`; la conversión se verifica bit a bit antes de borrar el WAV.
   `capture.json mic.file/system.file` quedan obsoletos tras convertir: el visor usa las constantes
   `AUDIO_*_FLAC` de `paths.py`. **Quien confirme o descarte debe llamar a `finalize_session`.**
6. Recuperación: nº de muestras por tamaño de fichero; dueño comprobado por pid + hora de creación.
7. Cambio de dispositivo: sondeo cada 250 ms; si el nuevo no aparece, se reinicia PortAudio.
8. Micro = captura de comunicaciones por nombre; loopback = salida por defecto (eConsole).
9. `output_kind`: `speakers` si alguna vez hubo altavoces; `headphones` solo si todo fue auriculares.
   Bluetooth manos libres cuenta como auriculares (factor de forma o nombre).
10. PortAudio en modo callback, búferes de 100 ms. Fuente caída → pista en ceros + reintento cada 1 s;
    si fallan las dos al inicio, `start()` falla y limpia (también cierra el backend: ni
    `PyAudio` ni hilo colgando).
10b. **Un solo hilo para PortAudio por `WasapiBackend`** (PyAudioWPatch issue #23: algunos
    controladores fallan si los objetos de PortAudio se usan desde varios hilos). `PyAudio()`,
    enumeración/resolución, `pa.open`, `stop_stream`/`close`, `terminate` y el reinicio por
    `DeviceNotFound` van a un hilo daemon propio (`wasapi.PortAudioThread`, con COM); el
    `Recorder` y los protocolos de `sources.py` no cambian. Esperas acotadas (abrir 3 s, cerrar
    1,5 s: un reenganche, cerrar + abrir, cabe en el `JOIN_TIMEOUT_S` de 5 s del grabador); con
    el hilo colgado las aperturas fallan en el acto, los cierres se encolan igualmente (el flujo
    se para en cuanto el hilo se libera) y `close()` no espera. El backend es reutilizable tras
    `close()` (el hilo vuelve a arrancar si el anterior ya salió). **No confirmable sin
    conectar/desconectar dispositivos reales**: arreglo de diseño probado con un `PyAudio` falso
    que registra el hilo de cada llamada. Pendiente: `Pa_Initialize` cuenta referencias en
    todo el proceso; si el `LevelMonitor` del asistente (otro backend) sigue abierto cuando
    empieza una grabación (`_on_levels_monitor` solo lo arranca si no se graba, pero nada lo
    para al empezar a grabar), el reinicio de PortAudio no refrescaría la lista.
11. RAM medida como memoria privada. `app.py` debe fijar `OPENBLAS_NUM_THREADS=1` antes de importar numpy.
12. **Dispositivos reales no verificados:** desde la shell de los agentes PyAudioWPatch ve 0 dispositivos
    y COM no encuentra endpoints; nada se grabó de verdad. Medido en sintético: 2,2 % de un núcleo, 43 MB.

## 7. Motores, VAD, AEC y modelos (`pipeline/engines`, `pipeline/aec`, `models.py`)

1. Parakeet vía `onnx-asr` int8 local: 15–19× tiempo real y pico de 1,24 GB (i7-12700F). Whisper small:
   ~5×, 0,79 GB. Silero VAD en ONNX sin torch: ~250×. Diarize: 12–19×, pico de 1,28 GB en 60 min; torch no
   se libera en `close()` (para devolver memoria, etapa 7 en subproceso). WER sobre TTS español
   (SAPI Helena, **no** es el set real): 0 % en ambos motores.
2. **AEC ganador: Speex (`pyaec`)** con 256 ms de cola + alineación de retardo GCC-PHAT + preprocesador
   Speex activo: conserva la voz propia en doble habla (AEC3 la suprime). `AUTO_ORDER=("speex","webrtc")`;
   AEC interno a 16 kHz. Detalle en `docs/aec_comparison.md` (solo escenarios sintéticos).
3. La diarización automática (`n_speakers=None`) sobresegmenta; mitigado con tope `max_speakers=8`, fusión
   por coseno ≥ 0,55 y absorción de turnos < 10 s. 33 s y 5 min → 2 hablantes; 60 min en mosaico → 4.
   Umbrales sin calibrar con voces reales.
4. Manifiesto de modelos en código (`micnaga/models.py`): URLs de Hugging Face con commit fijado y
   SHA-256 calculados; solo https a `huggingface.co` / `*.hf.co`. Por defecto 699 MB (Parakeet, Silero,
   WeSpeaker); Whisper small +486 MB opcional; Whisper medium fuera. WeSpeaker se redirige a
   `<models>/wespeaker` (nunca `~/.wespeaker`).
5. Fin de palabra: tope de 0,32 s tras el último token. Adaptador pyannote escrito pero **no verificado**
   (no instalado).

## 8. Pipeline, cola, salidas, compuertas, glosario, huellas y bench

1. Etapas `s01_gates … s11_assemble` con marcador `work/NN_name.done.json` y artefactos atómicos;
   reanudación probada con caída antes de cada etapa. La etapa 6 no tiene checkpoint interno: una caída
   a mitad repite la etapa. `jobs.stage` = última etapa *iniciada*.
2. Worker: proceso aparte (`micnaga worker [--once]`), prioridad BELOW_NORMAL, singleton por mutex con
   nombre (derivado de `MICNAGA_HOME`), `requeue_stale` al empezar, eventos por tubería (se descartan en
   silencio si nadie escucha). `spawn_worker(extra_args, env)` lo lanza; en ejecutable congelado usa
   `<exe> worker`. `micnaga process <dir>`: 0 hecho, 3 descartado, 1 fallo, 2 uso.
3. La etapa 2 rellena la pista corta y avisa si el desfase > 50 ms (no falla). Sin AEC disponible o con
   `aec=off` se salta la etapa 3; sin diarizador queda un solo `S1`. Se aplica AEC también si algún
   cambio de dispositivo fue a no-auriculares. La etapa 4 filtra en bloques de 60 s (1 h bajó de 2,08 GB
   a ~1,0 GB de pico).
4. Compuertas (`micnaga/gates.py`, las usa también el vigilante): cambio de turno = piezas «yo»/«remoto»
   en línea temporal conjunta, ignorando < 0,3 s, con el eco absorbido ensanchando 0,3 s las regiones
   del sistema.
5. Frases: corte por cambio de hablante, hueco ≥ 0,8 s o puntuación final. Párrafos: < 1,5 s del mismo
   hablante; corte a > 45 s o 700 caracteres. Eco de 2.ª capa: frase «Yo» frente a frases remotas
   solapadas ±0,5 s y palabras remotas de la ventana (`ratio` y `partial_ratio` si ≥ 12 caracteres).
6. Marcadores en el MD resaltados con `==…==`; los minutos siguen contando pasada la hora.
7. Glosario (`<datos>/glossary.txt`, formato `Canónico: variante, variante`); huellas con DPAPI
   (`store/voiceprints.py`). `last_run.json` del bench en `<datos>/bench/`.
8. Peticiones de cambios compartidos de la ola: `Database.set_session_dir(job_id, path)` (hoy UPDATE en
   crudo), enganche de `spawn_worker` en la app y permiso para que el vigilante importe `micnaga.gates`.

## 9. Vigilante y atajos (`micnaga/watcher`, `hotkeys.py`)

Máquina pura `WatcherStateMachine.step(event, now) -> list[Action]` + fachada `Watcher` con hilo de
sondeo de 1 s (0,25 s con popup abierto). Coste medido (i7-12700F): reposo 0,234 % de un núcleo (con la
vía genérica), peor caso forzado 0,29 %.

1. El timeout del popup y el fin de reunión valen ambos 15 s: la cuenta atrás del popup se congela
   mientras falte una señal obligatoria (y con hover o pausa privada).
2. Fin de reunión a los 15 s desde el primer tick sin señal; un corte de 10 s no termina.
3. Micro y pestaña Meet deben ser del mismo navegador. Reunión sin código: clave de ignorado
   `nocode:<navegador>`; los títulos salen solo del título de ventana.
4. La sesión pertenece a su código: otra pestaña Meet no la mantiene viva.
5. Parar a mano ignora las reuniones vivas (no se vuelve a preguntar); con `on_timeout=discard` también.
   «Grabar ahora» adopta la reunión visible si la hay; si no, abre una sesión manual que solo termina con
   parar. «Suspender» no corta una grabación en curso: se aplica al terminar.
6. Entrada de micro «colgada»: se exige un proceso vivo con ese exe (y creado antes de
   `LastUsedTimeStart`). Se leen también apps empaquetadas (`ConsentStore\microphone\<PackageFamilyName>`)
   y se excluye al propio intérprete / `micnaga.exe`.
7. Pausa privada durante el popup: permitida; congela la cuenta atrás.
8. Vía genérica: «Ahora no» ignora `app:<clave>` hasta 15 s con el micro liberado; una reunión genérica que
   acaba con el popup abierto queda provisional (decide la compuerta); `user_stop` ignora todas las apps con
   micro; con varias apps gana la de uso continuo más largo; «Grabar ahora» sin Meet no adopta apps.
   Contrato con el controlador: en genérico `show_pill(provisional=True)` va ANTES del popup; tras un
   timeout genérico no hay `show_pill` nuevo; `hide_pill()` puede seguir a `discard_recording()`;
   `show_popup(code=None, monitor_hint=None)` → la UI usa el monitor principal o la ventana en primer plano.
9. **No verificado:** Brave; una llamada real (si silenciar Meet libera el micro, 15 s en silencio terminan
   la sesión); la señal de audio saliente (sin endpoint de audio en este PC).

## 10. Interfaz (`micnaga/ui`)

1. Sin sombra exterior en popup y pastilla (ventana ajustada); anillo interior de 1 px. Pastilla siempre
   oscura. Mica desactivado por defecto (no verificado visualmente).
2. Popup arriba a la derecha del área de trabajo del monitor del navegador (margen 24 px); pastilla arriba
   al centro. Tras «Grabar» o timeout el host convierte el popup en pastilla por sí mismo.
3. Con `WS_EX_NOACTIVATE`, Enter/Esc solo llegan si el popup tiene foco; un clic en la tarjeta lo activa
   temporalmente. «No molestar» por aproximación (`SHQueryUserNotificationState`).
4. Audio de la biblioteca: dos `<audio>` sincronizados (no WebAudio), corrección de deriva cada 400 ms.
   Servidor local `media.py` en 127.0.0.1 con token por proceso (páginas + FLAC con Range).
5. El toast usa el AUMID de `cmd.exe` mientras no haya acceso directo propio: el instalador crea el acceso
   directo del menú Inicio con `AppUserModelID` propio (ver §13) y se pasa a `toast.py` (`aumid=`).
6. Guardar una huella exige la opción activada + casilla de consentimiento + nombre, nunca «Yo».
7. `ui/startup.py` es el único código que escribe `HKCU\...\Run` (valor `Micnaga`, comando
   `"<exe>" app`); se niega bajo pytest o `MICNAGA_UI_DEMO=1`.
8. Coste de WebView2: cada ventana oculta añade un proceso de renderizado; comprobar contra los 150 MB.
9. **No verificado:** transparencia y esquinas reales, animación de vuelo, Mica, no robar foco frente a
   Meet real, toast real, clics del menú de bandeja.

## 12. Integración (`micnaga/app.py`, `micnaga/integration/`)

1. **Cableado.** `micnaga.app.run` (ligero: sin numpy/torch al importar; fija `OPENBLAS/OMP/MKL_NUM_THREADS=1`
   antes de nada, mutex `Local\micnaga-app-<hash de MICNAGA_HOME>`, log `app.log`, `ensure_config`) construye
   `integration.application.Application`, que une `UiHost`, `AppController` (implementa `WatcherController`),
   `Watcher`, `HotkeyManager`, `Finisher` (hilo serie: FLAC + encolado + borrado), `WorkerSupervisor`,
   `EventsServer` (tubería, la app es el servidor) y `AppLibraryOps`. Todo es inyectable (tests sin
   dispositivos, registro ni procesos reales). `--exit-after S` sale por el mismo camino que «Salir».
2. **Una sola cuenta atrás del popup: la del vigilante.** El JS recibe un `timeout_s` enorme y ni su
   «timeout» (`on_popup_decision("timeout")` se ignora) ni su reloj mandan. La UI dibuja la barra con el
   método de puente `PopupApi.popup_state() -> {remaining_s, total_s, paused}`, alimentado por
   `Watcher.popup_remaining_s` + `Watcher.popup_frozen` (nuevo: congelada por ratón, pausa privada o
   señal caída). `close_popup(reason)` → `accept_popup` / `dismiss_popup` / `expire_popup` / `hide_popup`.
   `expire_popup` llama a `micnaga.popup.expire()` si el JS lo define y, si en 0,8 s el popup sigue
   igual, lo resuelve en Python (vuelo directo a la pastilla, o ocultar con `on_timeout=discard`).
3. **Pastilla sin duplicar.** `UiHost.show_pill` es idempotente (si ya está visible solo actualiza
   «provisional»); mientras un popup está `showing`/`morphing` la pastilla espera al aterrizaje de la
   tarjeta, con red de seguridad de 2 s (`PILL_FLUSH_S`): si el JS no contesta sale igualmente (nunca
   grabación sin pastilla). Con la pastilla ya visible (ruta genérica) no hay vuelo: el popup solo se va.
4. **Metadatos antes de parar.** `Recorder.set_meeting_info/set_confirmed` solo persisten mientras graba:
   el controlador los fija ANTES de `stop()`. `stop()` (hasta 5 s) corre en un hilo corto por parada, y
   FLAC + `enqueue_job` en el `Finisher` (cada hilo con su propia `Database`). Sin grabador (falló
   `start`) confirmar/parar/descartar son no-ops, no se encola nada y no se muestra pastilla.
5. **«Salir» nunca descarta.** Para el vigilante, para y cierra la grabación activa (FLAC + cola; confirmada
   según el grabador) y espera al `Finisher`; si el cierre se interrumpe, el barrido de arranque la recoge.
6. **Arranque.** Bandeja primero; después, en hilos: BD (se crea antes de que la abran varios hilos: dos
   primeras aperturas simultáneas daban `database is locked`), precarga de numpy/captura, servidor de
   eventos, supervisor, recuperación, atajos y vigilante. Sin modelos → asistente (descarga con
   `micnaga.models.download_models`, progreso por modelo) y el worker no se lanza hasta que estén.
   Medido: bandeja a 1,46–1,84 s desde el inicio del proceso (i7-12700F, caché caliente). numpy entra
   (~90 ms) por `micnaga.types`/`store.db`; nada más pesado (test de higiene).
7. **Recuperación al arrancar.** `recover_sessions` (estado `recording`) + barrido de carpetas `stopped`/
   `recovered`/`finalized` sin trabajo en SQLite y sin `transcripcion.json` (la app murió entre parar y
   encolar). Las provisionales pasan por las compuertas.
8. **Worker con `--once`.** La app lanza `micnaga worker --once`: sale al vaciar la cola, así cada tanda
   empieza en un proceso limpio (torch/diarize no devuelven memoria con `close()`). El `WorkerSupervisor`
   vigila: si un job llega cuando el worker está saliendo (`spawn_worker → None`) lo relanza al terminar;
   backoff exponencial si el worker muere al arrancar. El worker sobrevive a la app (sigue procesando).
9. **Tubería de eventos.** Servidor con E/S solapada (parable en cualquier momento), 1 instancia siempre
   escuchando, solo clientes locales (`PIPE_REJECT_REMOTE_CLIENTS`), líneas mal formadas/enormes
   descartadas. `JobDone` → toast «Lista» con `Path(session_dir).name`; `JobStarted`/BD ocupada → azul;
   `JobFailed`/`JobDiscarded` → log (+ `last_job_error` en el diagnóstico de Ajustes).
10. **Biblioteca.** `AppLibraryOps` sobre `pipeline.output` y `VoiceprintStore`; huellas solo con
    `privacy.voiceprints_enabled`, nunca «Yo», y solo si la reunión guardó embeddings. El visor usa
    `AUDIO_ME_FLAC/AUDIO_REMOTE_FLAC` (no los nombres de `capture.json`, obsoletos tras el FLAC).
11. **Registro.** La app no escribe `HKCU\...\Run` al arrancar: solo al terminar el asistente de primer
    arranque (`startup.set_run_at_login`, con su guardia anti-pytest/demo y `MICNAGA_NO_AUTOSTART=1`).
12. **Ajustes guardados en caliente:** solo se re-registran los atajos; el resto (detección, motor...)
    rige al reiniciar la app.
13. **Cambios fuera de alcance:** `Database.set_session_dir` (el runner la usa), `Watcher.popup_frozen`,
    `UiHost` (popup_state, expire_popup, show_pill idempotente/diferida), `PopupApi.popup_state`,
    `ui/tray.py` (el menú no arrancaba: pystray exige acciones con 0-2 parámetros sin valores por defecto).

## 13. Empaquetado (`packaging/`, `scripts/build.ps1`)

PyInstaller *onedir* + Inno Setup (spec §11). Todo reproducible con `scripts\build.ps1 [-Variant lite|full]`
(`build/` y `dist/` están en `.gitignore`).

1. **Dos variantes** (resuelve el choque torch ↔ 150 MB, ver §5.4): **`lite`** (por defecto; sin torch,
   `diarize`, `silero-vad`, `wespeakerruntime`, scikit-learn, pandas, sympy...; **sin diarización**: todo lo
   remoto es `S1`) y **`full`** (con `diarize` + torch). Es lo más simple que cumple el presupuesto: una
   «descarga opcional de diarización» exigiría un cargador de paquetes fuera del congelado. Medido (este PC,
   Python 3.11, PyInstaller 6.22.3):

   | Variante | En disco | Instalador (estimación LZMA sólido) |
   | --- | --- | --- |
   | `lite` | 315,0 MiB | **104,3 MiB** (< 150 MiB) |
   | `full` | 784,2 MiB | **276,0 MiB** (> 150 MiB: distribución aparte) |

   La estimación es de `scripts/measure_dist.py` (un flujo LZMA de toda la carpeta); **`iscc` no estaba
   instalado, así que el instalador real no se compiló ni se midió.** Mayores partidas de `lite`:
   ctranslate2 61 MiB, scipy 49+19 MiB, onnxruntime 44 MiB, numpy 20 MiB, 3 × exe 16 MiB (cada `.exe` lleva su propio PYZ; en `full` son 3 × 50 MiB, mejorable con `MERGE`).
2. **Tres ejecutables en la misma carpeta** (comparten `_internal`; todos son `packaging/frozen_entry.py`):
   `micnaga.exe` (ventana, sin consola; el de la clave `Run`, el acceso directo y `worker_command()`:
   `micnaga.exe app` / `micnaga.exe worker`), `micnaga-cli.exe` (consola: `record`, `process`, `bench`,
   `--version`, `--selftest`) y `micnaga-host.exe` (host de la extensión; Chrome lo llama solo con
   `chrome-extension://<id>/`, así que se reconoce por el nombre del ejecutable). En `micnaga.exe`
   `sys.stdout/stderr` son `None`: el punto de entrada los sustituye por `os.devnull`.
3. **`--selftest [fichero]`** (`micnaga-cli.exe`): importa todos los módulos de `micnaga` y las bibliotecas
   nativas (onnxruntime, soundfile, soxr, PyAudioWPatch, ambos AEC, onnx-asr, faster-whisper, webview,
   pystray, toasts...), comprueba que `lite` NO trae torch y `full` sí, y que los assets web existen.
   Resultado: `lite` 133 importaciones OK, `full` 133 OK. No abre ventanas ni toca el registro ni la red.
4. **Exclusiones por licencia/tamaño** (el instalador no puede llevar GPL ni binarios propietarios):
   * `av` (PyAV): su FFmpeg trae **libx264/libx265 (GPL-2.0+)** y 63 MiB; `faster_whisper` hace `import av`
     pero solo lo usa para decodificar ficheros (Micnaga le pasa arrays) ⇒ se sustituye por
     `packaging/stubs/av` (módulo que falla con claridad si alguien lo usa de verdad).
   * `kaldiio` (en `full`, vía `diarize → wespeakerruntime`): su licencia es de **solo evaluación y prohíbe
     redistribuir** (§14) ⇒ stub `packaging/stubs/kaldiio` (solo lo usa `extract_embedding_kaldiio`, que
     Micnaga no llama).
   * `cudnn64_9.dll` (NVIDIA, propietaria, solo sirve con CUDA), `dbghelp.dll`/`dbgcore.dll` (depuración de
     Windows), `hf_xet` (descargas Xet; Micnaga descarga con urllib + SHA-256), `sherpa-onnx` (declarado en
     el extra `asr` pero **ningún módulo lo importa**), `onnx*` de herramientas de cuantización, tests y
     herramientas de desarrollo.
   * `scripts/gen_licenses.py --check --dist <carpeta>` falla si reaparece cualquiera de esos binarios.
5. **Componentes LGPL sustituibles:** `pystray` (LGPL-3.0, Python puro) se entrega como `.py` sueltos
   (`module_collection_mode={"pystray": "py"}`; verificado en `dist/micnaga/_internal/pystray/*.py`);
   `soxr` (`.pyd`) y `libsndfile` (DLL) son ficheros independientes. **No usar `--onefile`.**
6. **Inno Setup (`packaging/micnaga.iss`, sin compilar):** instalación por usuario (`PrivilegesRequired=lowest`,
   `{localappdata}\Programs\Micnaga`); acceso directo del menú Inicio con `AppUserModelID`
   **`es.micnaga.Micnaga`** y argumento `app`; tarea opcional «Arrancar con Windows» (valor `Micnaga` de
   `HKCU\...\Run` = `"<exe>" app`, el mismo que escribe `ui/startup.py`); el desinstalador borra siempre la
   clave `Run` (aunque se activase desde Ajustes) y las claves del host de la extensión, y **pregunta**
   antes de borrar `%APPDATA%\Micnaga` y `Documentos\Micnaga` (nunca en desinstalación silenciosa; no toca una
   carpeta de reuniones personalizada). **Pendiente de la app:** pasar `aumid="es.micnaga.Micnaga"` a
   `ui/toast.py` y fijar el mismo AUMID en el proceso (`SetCurrentProcessExplicitAppUserModelID`).
7. WebView2 no se empaqueta (viene con Windows 11; en Windows 10 lo instala Microsoft). Sin firma de código
   (spec §13 recomienda firmar para evitar bloqueos de antivirus/SmartScreen): pendiente.
8. No se creó `ico`/recursos de versión nuevos: se usa `micnaga/ui/web/assets/micnaga.ico`.

## 14. Licencias de terceros (`THIRD_PARTY_LICENSES.md`, `scripts/gen_licenses.py`)

El script calcula la clausura de ejecución de `micnaga` (+ extras `asr`, `diar`, `aec`, `ui`; sin dev/bench/
pyannote), lee metadatos y ficheros de licencia reales y **bloquea** (salida 1) GPL/AGPL/desconocida y toda
LGPL no revisada en `LGPL_REVIEWED`. Un test (`tests/packaging/test_licenses.py`) lo ejecuta en modo `--check`.
Resultado: 75 dependencias; 70 permisivas, 1 MPL-2.0 (tqdm), 2 LGPL revisadas, 2 no distribuidas.

1. **LGPL:** `soxr` (LGPL-2.1+; libsoxr compilada *dentro* del `.pyd`, estática pero en un fichero
   independiente y sustituible en onedir), `pystray` (LGPL-3.0; Python puro, entregado suelto) y
   `libsndfile` (LGPL-2.1+; DLL dinámica dentro de `soundfile`). Obligaciones (dinamismo/sustitución, texto de
   licencia, oferta de fuentes, no modificar) al principio del fichero. **Decisión a confirmar por el
   dueño:** si se prefiere cero LGPL en el binario, sustituir `soxr` por `scipy.signal.resample_poly` (BSD-3).
2. **Hallazgos graves (resueltos por exclusión):** (a) las ruedas de `av` traen FFmpeg con x264/x265 (GPL);
   (b) **`kaldiio` 2.18.1 es «Software License Agreement for Evaluation» de NTT** (uso interno, prohíbe
   distribuir; confirmado en el paquete y en el repo upstream `nttcslab-sp/kaldiio`) y lo arrastra
   `diarize → wespeakerruntime`; (c) `ctranslate2` trae `cudnn64_9.dll` (NVIDIA, propietaria).
   Sin esas exclusiones el instalador **no** se podría publicar bajo MIT.
3. **`pyaec`:** el wheel no declara licencia ni la incluye; se fijó **MIT** a mano comprobando el
   `LICENSE` del repo `thewh1teagle/aec` (copyright thewh1teagle, 2025). Envuelve SpeexDSP (BSD-3).
4. Casos con metadatos pobres resueltos por el texto del fichero: `clr_loader` (MIT), `Windows-Toasts`
   (Apache-2.0); por clasificadores: `scipy`, `colorama`, `tokenizers`, `torchaudio`... (BSD / Apache).
   Numpy, scipy, pillow, pywin32, torch... citan GPL/LGPL en sus ficheros de licencia por componentes de terceros
   que empaquetan: se anota «revisar» pero no bloquea (las expresiones SPDX de los metadatos mandan).
5. Fuera del árbol de Python y documentado a mano en el fichero: bootloader de PyInstaller (GPL-2.0+ **con
   excepción** que permite distribuirlo), runtime de VC++, OpenSSL, SQLite, OpenMP de Intel (`libiomp5md.dll`,
   en ctranslate2), WebView2 (no se incluye) y los **modelos** (CC-BY-4.0: Parakeet, WeSpeaker, pyannote;
   MIT: Silero, Whisper/Systran) con la atribución exigida.
6. **Sin verificar (revisar antes de publicar):** la rueda de ctranslate2 dice que puede integrar Intel MKL/oneDNN
   y trae `libiomp5md.dll` (OpenMP de Intel) sin ningún aviso de terceros; el aviso de PortAudio dentro de
   `_portaudiowpatch.pyd`; las licencias de los codecs que enlace `libsndfile_x64.dll` (el `COPYING` de la
   rueda es solo el texto LGPL-2.1); y la licencia del SDK de WebView2 (`webview/lib/Microsoft.Web.WebView2.*`,
   `WebView2Loader.dll`) que lleva pywebview. `THIRD_PARTY_LICENSES.md` los marca «sin verificar».
7. Los textos de licencia se incluyen **completos** (sin recortar) en `THIRD_PARTY_LICENSES.md` (≈1 MB): los
   avisos de copyright de numpy/scipy/etc. y los textos LGPL son obligación de distribución. `micnaga.spec` copia
   también `onnxruntime/LICENSE` y `ThirdPartyNotices.txt` junto al paquete.

## 15. Extensión de navegador (F4, `extension/`, `micnaga/watcher/extension.py`)

1. **Diseño:** MV3; el *content script* de `meet.google.com` detecta «en llamada» (botón de colgar por su
   `aria-label` localizado: heurística **frágil y no verificada en vivo**, aislada en `extension/detect.js`),
   título (del `document.title`) y código (de la URL) y manda `{state, title, code, tab}` al service worker,
   que lo reenvía por `connectNative("es.micnaga.host")`. Permisos: solo `nativeMessaging` y
   `https://meet.google.com/*`; sin red (test).
2. **Host nativo** (`micnaga-host.exe` / `python -m micnaga.watcher.extension`): protocolo `uint32 LE + JSON`;
   escribe `<datos>\extension\host-<pid>.json` (atómico, con reintentos ante `PermissionError`, latido cada
   5 s). El lector `FileExtensionSource` implementa `ExtensionSignalSource`: `available()` solo es `True`
   **mientras hay una llamada en curso** en un host vivo (pid + hora de creación, fichero refrescado hace
   < 20 s); cada pestaña caduca a los 90 s sin latido. **Decisión *fail-open*:** la extensión solo puede
   *añadir* una llamada positiva; si sus heurísticas de DOM se rompen (nunca detecta llamada) o el host no
   corre, `available()` es `False` y el vigilante nativo sigue mandando. (Con `available()` = «host vivo»,
   unas heurísticas rotas habrían silenciado todas las señales nativas.) `host_alive()` queda para diagnóstico.
   El service worker abre el puerto nativo solo ante mensajes de Meet (cerrar una pestaña cualquiera no
   arranca el host) y no hay manejador `tabs.onUpdated` (sin permiso `tabs` no se vería la navegación fuera de
   Meet: lo cubren `pagehide` y la caducidad por latido). Un fichero por host (un host por navegador/perfil). El host deduce `browser` (`chrome.exe`...)
   subiendo por sus procesos padre (la extensión no puede saberlo).
3. **Desviación de privacidad:** el código y título de la reunión se escriben en ese fichero **mientras dura
   la llamada** (se borra al colgar, al cerrar el navegador y se limpian los de hosts muertos), aunque
   §2 de este documento dice que no se persiste el código de reunión.
4. El ID de la extensión es fijo (clave pública en `manifest.json` → `npfamnnohhilpnfalmlamccajpdandca`) para
   que el manifiesto del host lleve un `allowed_origins` estable. La clave privada **no** se conserva (solo
   hace falta para empaquetar/publicar; con la Web Store el ID cambiaría).
5. **Registro:** `scripts/register_native_host.ps1` (con `-WhatIf`) escribe el manifiesto en
   `%APPDATA%\Micnaga\extension` y las claves `HKCU\...\NativeMessagingHosts\es.micnaga.host` de
   Chrome/Edge/Brave; **no se ejecutó**. Lo desinstala el desinstalador de Inno Setup.
6. **Para la integración:** `Watcher(..., extension=create_extension_source())`. Ojo: `SignalSampler._sample`
   descarta **todas** las demás señales —también la vía genérica— mientras `available()` es `True` (durante
   una llamada de Meet detectada por la extensión); en ese intervalo una llamada de Teams/Zoom no se detectaría.
   Además, la clave de reunión (`code` frente a `nocode:<navegador>`) podría cambiar a mitad de sesión si las
   señales nativas ganan la carrera de los 5 s. Se recomienda que el muestreador
   mantenga la vía genérica (`mic_apps`) y use la extensión solo en lugar de la vía Meet.

## 16. Instalador de un clic, un solo producto (sustituye a §13.1 y §13.6)

1. **Un solo instalador** `Micnaga-Setup-<version>.exe` con el programa completo: variante de build `standard`
   (sin torch; diarización `processing.diarization = "onnx"`, módulo `diar/onnx.py`, onnxruntime + scipy.cluster;
   faster-whisper incluido para la opción de Ajustes). La variante `torch` (diarize + torch) queda solo como
   respaldo de desarrollo (`MICNAGA_VARIANT=torch`, carpeta `dist/micnaga-torch`) y no se distribuye. Las
   antiguas `lite`/`full` ya no existen. Los modelos (~700 MB) se descargan solos en el primer arranque.
   **Medido: instalador real 68,8 MiB** (carpeta congelada 317 MiB, 139 importaciones en `--selftest`).
2. **Un clic:** sin páginas de bienvenida, carpeta, grupo, resumen, tareas ni final; solo la barra de progreso;
   al terminar lanza `micnaga.exe app` (también con `/VERYSILENT`). Por usuario, sin UAC. Actualizar = ejecutar el
   nuevo encima: `PrepareToInstall` cierra `micnaga.exe`/`micnaga-host.exe` (taskkill; parada segura por la
   recuperación de §6) y espera a que desaparezca el mutex de `micnaga/app.py`. Se calcula en [Code] (no vale
   `AppMutex`: el nombre lleva un hash de la ruta de datos); `GetSHA1OfString` coincide con `hashlib` en rutas
   ASCII (comprobado compilando y ejecutando un instalador de prueba que solo escribe un fichero); con nombres de
   usuario no ASCII se pierde la espera, no el cierre.
3. **Arranque con Windows SIEMPRE activo** (HKCU Run, valor `Micnaga`, `"<exe>" app`, mismo que `ui/startup.py`):
   lo escribe el instalador (al actualizar solo si ya existía, para respetar a quien lo apagó). El asistente de
   primer arranque no lo pregunta ni lo pisa (`_on_first_run_done` ya no toca el registro). La app congelada
   reescribe el valor al arrancar si falta o apunta a otra ruta y `startup.run_at_login` es true
   (`ensure_run_at_login`); Ajustes lo apaga (config + registro). Se ejecuta al iniciar sesión (no antes).
4. **Primer arranque sin decisiones:** `UiHost.open_wizard` llama a `WizardApi.start_download()` y el
   `WizardApi(auto_finish=True)` ejecuta «terminar» al acabar la descarga (cierra el asistente; la app queda en
   la bandeja). Un fallo no cierra: queda el reintento.
5. **AUMID** `es.micnaga.Micnaga` (`ui/toast.AUMID`): `ToastNotifier(aumid=default_aumid())` y
   `SetCurrentProcessExplicitAppUserModelID` al crear `Application`, **solo en la app congelada** (en desarrollo no
   hay acceso directo con ese AUMID y los toasts no saldrían).
6. **Pendiente:** el instalador NO se ha ejecutado en este PC (escribiría Run y menú Inicio); probar instalación,
   actualización y desinstalación a mano. `Disable*Page=yes` sin ninguna página visible debería arrancar la
   instalación sola: confirmarlo en la prueba real.

## 17. Calidad de transcripción y de hablantes (rama `fix/transcript-quality`)

Origen: grabación real de 200 s (diálogo de 2 personas en un vídeo de YouTube, micro callado) procesada con
Parakeet + `onnx`. Prioridad del usuario: **ni hablantes fantasma, ni hablantes que no existen, ni muchos
hablantes cuando hay pocos**.

1. **Números del ASR** (`asr/parakeet.py`): el vocabulario de Parakeet v3 no tiene dígitos con marca de palabra;
   un número llega como token `▁` suelto (`" "`) + dígitos. Un token solo de espacio abre ahora la palabra
   siguiente («a las 8 y 50», no «a las8 y50»). Whisper no tenía el fallo (palabras ya separadas).
2. **Título** (`capture/cli.py`): `micnaga record --title T` escribe `meet_title` en `capture.json`; de ahí salen el
   título de la transcripción y el nombre final de la carpeta. Sin `--title`, `meet_title` queda vacío.
3. **Participantes**: solo hablantes con al menos un segmento (también «Yo»: un micro callado no es participante).
4. **Motor de diarización: se mantiene `onnx` por defecto.** Se evaluó `sherpa-onnx` `OfflineSpeakerDiarization`
   (pyannote segmentation-3.0 + embeddings CAM++/ERes2Net de 3D-Speaker, WeSpeaker ResNet34-LM o NeMo TitaNet
   small, clustering rápido; sin torch) contra `onnx` y `diarize` (tabla en `docs/diarization_onnx.md`). Con el
   umbral recomendado por sherpa-onnx (0,5) da **15 hablantes en bruto para 2 voces** en la grabación real y
   sobre-segmenta los diálogos TTS (DER 4,7–51 %). Ajustando el umbral a mano sobre estos mismos datos
   (ERes2Net o TitaNet a 0,8) acierta el número de hablantes en los cinco casos tras el post-filtro, igual que
   `onnx`, pero con un DER 3–15 veces mayor en todos los casos con referencia (tts2 30 %, tts3 8–10 %,
   tts4 6,5–18 %, mixto 7–8,5 %, frente a ~2 % de `onnx`), incluso con `num_clusters` fijado (el error es de la
   segmentación, no del clustering; las voces sintéticas pueden perjudicarla). En la grabación real solo empata
   con `onnx`. Es 4–7 veces más
   lento. **No se adopta con la evidencia actual**; re-evaluar con reuniones reales (banco F2) con
   `tests/engines/diar_compare.py`. Sus modelos quedan fijados en `models.CANDIDATE_MANIFEST` (la app nunca los
   descarga) y el adaptador es solo de evaluación (`tests/engines/sherpa_diar.py`). `pyannote` no se pudo
   evaluar (no instalado).
5. **Post-filtro de hablantes fantasma** (`engines/diar/postfilter.py`, etapa 7, todos los motores): un hablante
   con < 3 s de voz o un único turno de < 4 s nunca se reporta por separado; se absorbe en el más parecido
   (coseno de embeddings, solo en memoria) o en el del turno más cercano, nunca se absorbe al dominante, y se
   renumera `S1..Sn`. Va antes de calcular las huellas (nunca se guarda la de un fantasma). **Invierte a propósito**
   la política de `onnx._merge_small` / `diarize.merge_similar_speakers` de conservar un hablante pequeño «si no
   se parece a nadie»: un participante real que solo dice una frase corta queda atribuido a otro (se corrige
   fusionando/renombrando a mano); un hablante inventado es peor. Umbrales elegidos por la especificación del
   usuario (~2–3 s), no calibrados.
6. **Frases y fronteras de turno:** (a) la etapa 8 recorre cada pista en orden temporal: un cambio de hablante
   remoto siempre corta la frase (antes una frase de S1 podía «saltar» por encima de la respuesta de S2);
   (b) un cambio de hablante a mitad de oración y sin pausa se mueve al fin de oración más cercano (≤ 0,6 s,
   ≤ 4 palabras, el turno que cede conserva ≥ 2 palabras); (c) en `onnx` la frontera entre ventanas de distinta
   etiqueta va al instante más silencioso entre sus centros en vez de al punto medio. En la grabación real (c)
   sola corrige 3 fragmentos y estropea 1 («llegado a la vida.»), que (b) arregla; juntas, el acuerdo palabra a
   palabra con una referencia aproximada por tono sube de 85,1 % a 88,6 %.
7. **Huellas:** la etapa 7 anota `info.embedder` (`embedding_name` del motor o su `name`) y la etapa 10 compara
   las huellas en ese espacio; para los motores actuales es el mismo nombre que antes (compatibles).
## 17. Secretaria (`micnaga/brain`, diseño `docs/design/ai_brain.md`)

1. **Dónde corre.** En el proceso de la app (no es la etapa 12): `BrainService` (hilo serie, E/S pura) arranca con
   `job_done`; tabla propia `secretary_runs` y FTS propio `acta_fts` en `micnaga.db`, creados con `IF NOT EXISTS`
   (sin tocar `SCHEMA_VERSION`: una versión sin Secretaria abre la misma base). Reintentos con espera creciente
   (1 min, 5 min, 30 min, 2 h; `rate_limit` hasta su `resetsAt`; `auth`/`not_found` solo con «Reintentar»). Al
   arrancar: `running` → `pending` y se recogen las reuniones de las últimas 24 h sin acta. Con
   `secretaria.enabled = false` todo es inerte y el aviso «Lista» sale como antes.
2. **Aviso.** El toast «Lista» espera a la pasada hasta 120 s y añade «· N propuestas»; si tarda más, sale sin el
   número. Si la pasada falla y hay título provisional, se usa ese (si el actual es genérico).
3. **`claude -p`.** `--output-format stream-json --verbose --json-schema`, `--tools ""`, `--strict-mcp-config`,
   `--mcp-config <fichero>`, `--allowedTools mcp__micnaga__*` (+ las herramientas listadas de cada servidor
   extra), `--permission-mode dontAsk`, `--setting-sources project`, `--disable-slash-commands`,
   `--no-session-persistence`, `--system-prompt-file`, desde una carpeta temporal vacía; nunca `--bare` ni
   `--safe-mode`. Entorno del hijo **por lista blanca**: nunca se heredan `ANTHROPIC_*`/`CLAUDE_CODE_*`
   (comprobado con una clave falsa en el entorno: `apiKeySource: none`). Objeto de trabajo de Windows con
   «matar al cerrar» + árbol de procesos muerto al vencer el tiempo. `claude.cmd` de npm se resuelve a
   `node cli.js` (un `.cmd` sin shell destroza el JSON).
4. **El modelo cita evidencias por índice de frase** (`#n`) y hablantes por id; Micnaga resuelve el instante y el
   nombre. Fechas ISO validadas; correcciones de palabra solo si el texto aparece tal cual y no hay cifras.
5. **Qué sale marcado lo decide Micnaga** (`brain/plan.py`): vincular/nota/actividad si el registro existe al
   releerlo; crear contacto si la búsqueda propia no encuentra parecidos (≥ 0,9) y el email/teléfono se dijeron en
   la reunión; etapa/importe si los valores «anteriores» que leyó el modelo coinciden con los actuales. Si el
   servidor MCP no aparece `connected` en `system/init`, las propuestas de CRM se descartan.
6. **Escrituras** (`brain/executor.py`), siempre tras «Aplicar»: revalida antes de escribir (`changed_in_odoo`),
   repite la búsqueda de duplicados, guarda ids creados y valores anteriores para «Deshacer» por línea; reaplicar
   no duplica. La nota al historial es `message_post` con subtipo nota (no envía correo).
7. **Secretos**: Administrador de credenciales (`win32cred`): `Micnaga/claude-token`, `Micnaga/anthropic-key`,
   `Micnaga/odoo`. La clave de Odoo la lee el propio servidor MCP; ni el modelo ni la línea de comandos de
   `claude` la ven. Los tests usan un almacén en memoria o `MICNAGA_SECRETS_FILE` (nunca el real).
8. **Contexto y automejora**: `micnaga/brain/context/*.md` (paquete) + `<datos>/secretaria/context/*.md` +
   `<datos>/secretaria/learned.md` (fuera del programa: sobrevive a las actualizaciones; el design lo situaba en
   `micnaga/brain/context/`). Historial en `<datos>/secretaria/history/`.
9. **Fuentes MCP extra**: `secretaria.mcp_servers`, una línea JSON por servidor
   (`{"name","command","args","env","tools"}`), validada en `Config.validate`; solo se autorizan las `tools`
   listadas.
10. **Título en directo** (se aparta de SPEC §1.2, aprobado en design §8.3): a partir de 120 s grabados de una
    sesión confirmada, un proceso aparte (`micnaga brain live-asr`, BELOW_NORMAL, 2 hilos) transcribe los primeros
    90 s; Haiku propone el título (se descarta con confianza < 0,4). Se salta con ahorro de batería o CPU > 70 %.
    Se vigila solo `controller.session_dir` + `capture.json` (sin tocar el controlador). **Medido (i7-12700F,
    Parakeet, podcast real):** hijo 7,2 s de reloj, 19 s de CPU (≈ 2,6 núcleos de media: onnxruntime usa más
    hilos que los 2 de cálculo), pico 1,0 GB de RAM; Haiku 1,7–2,1 s. Sin medir en el i5-1245U.
11. **Medidas reales de `claude -p`** (sesión Max de este PC): título Haiku 1,7 s; acta Sonnet de la reunión real
    de 3 min 5,3 s; reunión de negocio sintética + Odoo falso 16,1 s, 7 turnos, 5 consultas.
12. **Sin verificar**: Odoo 19 real (nombre del método de permisos —se usa `check_access_rights` con
    `search_count` de respaldo— y si `mail.activity` exige `res_model_id`: se envía cuando `ir.model` es legible);
    JSON-2; el comportamiento con `claude` instalado por npm; los tests `test_bridge` del Run real fallan en un PC
    donde la app de desarrollo ya escribió `HKCU\...\Run` (no es de esta rama).



## 18. Flujo Sí/No recuperable, popup de fin y «Descartadas» (rama `fix/review-findings`)

**Desviación de la spec §5 pedida expresamente por el usuario** tras una prueba real (el popup no
le salía y una reunión sin respuesta se perdía). Diagrama y reglas: `docs/state_machine.md`.

1. **Flujo** (cualquier app con el micro, sin listas blancas): 20 s de micro en uso → grabación
   provisional + pastilla → popup a los 20 s. «Sí» = confirmada (solo la compuerta de duración,
   `gates.min_duration_s = 30`). «No» = se pausa al instante, chip «Descartada · Deshacer» 8 s
   (`detection.undo_s`) y, sin deshacer, la carpeta va a `_descartadas`. Sin respuesta = sigue
   grabando en provisional (la pastilla ofrece «¿Guardar?») y se decide al final.
2. **Popup de fin** (mismo cristal que el popup): el micro libre `detection.end_confirm_s` (2 s)
   abre «Reunión terminada» (Terminar / Seguir grabando; termina solo a los
   `detection.end_popup_timeout_s` = 10 s) o, sin confirmar, «¿La guardo?» (Guardar / Descartar; sin
   respuesta se aparta). La grabación sigue mientras se decide y luego se **recorta** al momento en
   que se soltó el micro (`capture.json → trim_end_samples`, aplicado e idempotente en
   `finalize_session`). `detection.end_after_s` queda solo como tiempo de olvido de lo ignorado.
3. **Compuertas relajadas por defecto**: `min_remote_speech_s = 0`, `min_turns = 0` (antes 30 s y
   6 turnos), y `gates.require_confirmed = true`: la etapa 1 no transcribe nada sin un «Sí»/«Guardar».
   Lo que no pasa una compuerta se **aparta**, no se borra. Migración de config v1 → v2
   (`[meta] version`): solo se cambian los valores que seguían en los defaults antiguos; los que el
   usuario tocó se respetan.
4. **«Descartadas» y privacidad.** Nada se borra sin un «No» explícito, y el «No» es recuperable:
   `<reuniones>/_descartadas/<carpeta>/` con `held.json` (motivo `declined` / `unanswered` /
   `unconfirmed` / `gates` / `interrupted` / `suspended` / `failed` y fecha). Se purga a los
   `storage.discarded_keep_days` (7, mínimo 1) días al arrancar; la biblioteca lista, recupera
   (vuelve a la raíz, `confirmed = true`, se encola) o borra ya. **Coste de privacidad asumido por
   el usuario**: el audio de algo que dijo «No» existe en disco hasta 7 días (local, nunca
   transcrito, nunca sale del equipo). Se documenta en el README.
5. **Durabilidad del «No»**: al pulsarlo se escribe de forma síncrona (fsync) la marca
   `.micnaga-discard.json` en la carpeta; si la app muere durante el «Deshacer» o antes de apartar,
   el arranque la aparta en vez de transcribirla. Una sesión sin confirmar al salir con «Salir» también
   se aparta (`unanswered`; `interrupted` si la app murió); una confirmada se encola.
6. **Popup que no salía (A)**: no se reprodujo en este PC; se corrigieron las causas plausibles:
   overlays visibles al arrancar (pywebview hace `form.Show()` al navegar en ventanas
   transparentes), revelado con `requestAnimationFrame` (no corre en un WebView2 ocluido → ahora
   `setTimeout`), y una comprobación a 1,2 s de que la ventana está visible (si no, se re-presenta y
   queda en el log). Un fallo al abrir la captura ya no deja el vigilante colgado (aviso + vuelta a
   vigilar).
7. **Salida de la app**: solo «Salir», el fin de sesión de Windows o el instalador. **Un**
   mecanismo reproducido: con la app lanzada desde una consola, un Ctrl+C de esa consola llega al
   manejador SIGINT de pywebview y la app sale «limpiamente»; `app.ConsoleGuard` lo ignora (y lo
   registra). No explica todo: la app real corre con `pythonw` (sin consola) y sus reinicios del
   19/11 no dejan ninguna línea de cierre ni evento de fallo en el visor de eventos, así que
   murió de forma abrupta por una causa aún desconocida. Por eso `faulthandler` escribe ahora en
   `logs/crash.log`. Si el bucle de la GUI termina por cualquier otra
   cosa se registra el motivo y se recrean las ventanas sin tocar la grabación (máx. 5 en 10 min).
   Toda salida deja `shutting down (reason: …)` en el log.


## 19. Secretaria en el Odoo de negocio: capacidades, horas, tareas y agenda (rama `feat/secretaria-odoo`)

Basada en `integ/all` (popup nuevo + Secretaria). Verificado contra el Odoo 19.0 real del usuario
**solo en lectura** (cuenta de empleado) y contra el Odoo falso para todo lo que escribe.

1. **Perfil de capacidades** (`brain/capabilities.py`): al pulsar «Probar» y una vez al día (tic
   del servicio), módulos instalados (`ir.module.module`) + `check_access_rights(read|create|
   write)` de 10 modelos conocidos (verificado: el método existe en Odoo 19 y responde sin lanzar
   con `raise_exception=False`). Nunca se prueba escribiendo. Se guarda por conexión (url + base +
   usuario) en `<datos>/secretaria/odoo_capacidades.json`: la cuenta del jefe tendrá el suyo. **Se
   decide por permisos, no por módulos** (`crm` está instalado pero `crm.lead` no es legible para
   esta cuenta). De él salen las herramientas MCP ofrecidas (`MICNAGA_TOOLS`), los tipos de
   propuesta del esquema y el resumen de Ajustes («Partes de horas · Tareas · Calendario ·
   Actividades · Gastos · Contactos (lectura)», el real de hoy).
2. **Notas y actividades solo donde se puede escribir**: `message_post` y `mail.activity` exigen
   escritura sobre el registro (`_mail_post_access` es `write` por defecto). Destinos por orden:
   tarea > oportunidad > proyecto > contacto, filtrados por el perfil; si el contacto es de solo
   lectura (caso real) la nota cae en la tarea/proyecto citado o no se propone.
3. **Propuestas por defecto tras una reunión** (cambio de alcance pedido por el usuario): el acta
   como **nota interna** (`mail.mt_note`, nunca comentario: no avisa a seguidores) en el registro
   más relevante; `project.task` por cada acción concreta del usuario en un proyecto **existente**
   (asignada a él, con fecha límite si se dijo; si hay una parecida abierta sale sin marcar);
   `mail.activity` para recordatorios con fecha; `calendar.event` solo si se acordó una próxima
   reunión. **No hay propuesta automática de fichar el tiempo de la reunión** (se quitó a
   petición del usuario).
4. **Eventos sin invitaciones**: `brain/odoo_calendar.py` es una pieza pequeña y aislada
   (`event_values`, `create_event`, `update_event`, siempre con contexto `no_mail_to_attendees`)
   que también puede llamar la capa de Google Calendar (rama `feat/secretaria-google`). Por
   defecto el único asistente es el propio usuario; los contactos que se mencionan salen como
   invitados **apagados** que el usuario activa uno a uno.
5. **Horas solo a petición**: `registrar_horas` solo si en la reunión se pide («tengo que fichar…»,
   «apúntame N horas a X») o desde «Rellenar partes de horas» (bandeja). Una propuesta del modelo
   con varias `fechas` se expande en una línea por día; cada línea pasa la validación local
   (`timesheets.validate_line`): fecha no futura, 0,25–12 h en cuartos, ≤ 10 h/día contando lo que
   ya hay en Odoo (salvo petición explícita), proyecto activo con partes de horas, tarea del
   proyecto y abierta; una línea igual ya registrada se salta (reaplicar no duplica). Solo sale
   marcada una petición explícita de un día citada en la reunión; el relleno por patrón, nunca.
   Se crea sin `employee_id`/`user_id`: Odoo pone los del usuario (un empleado no lee
   `hr.employee`).
6. **Días que faltan** (`brain/timesheets.py`): L–V menos festivos **nacionales** calculables
   (fijos + Viernes Santo) y los días marcados «Libre» en el panel. Los autonómicos/locales no se
   calculan: un día sin horas **entre** días con horas es «posible libre» y no se propone (el
   lunes 2-11-2026 real). Incompleto = menos de ¾ de la jornada típica (mediana).
7. **Patrón**: franjas que se repiten ≥ 60 % de los días de las últimas 4 semanas (proyecto +
   tarea + tramo «(9:00-13:30)» de la descripción, o proyecto + horas). Va a `learned.md` como una
   línea calculada por Micnaga (`learn.upsert_prefixed`, solo nombres de proyecto y franjas).
8. **Panel «Partes de horas»** (biblioteca, `horas.js`; bandeja «Rellenar partes de horas»; botón
   reloj en la biblioteca): días plegables con total, líneas editables (horas, proyecto,
   descripción con las variantes habituales), solo marcadas las que tienen evidencia (reunión
   grabada en Micnaga o evento del calendario de Odoo, que sustituyen a la franja del patrón que
   solapan). Intro aplica; cada aplicación es un lote con «Deshacer» (`horas.json`). Aviso del
   viernes por la tarde si la semana tiene huecos: `secretaria.horas_nudge`, **apagado por
   defecto**.
9. **Doble cerrojo de solo lectura**: `OdooClient(read_only=True)` rechaza `create/write/unlink/
   message_post` antes de salir a la red; lo usan el servidor MCP, la validación del plan y las
   pruebas reales. Las pruebas reales (`tests/brain/test_real_odoo.py`) pasan además por un proxy
   XML-RPC local de solo lectura con la clave real **solo en memoria** (el hijo MCP usa una clave
   ficticia), y comprueban que no hubo ni un intento de escritura.
10. **Medido (real, 19-11-2026)**: capacidades y días que faltan correctos (11 laborables del
    5-11 al 19-11; 2-11 «posible libre»); `claude -p` real con las herramientas reales: 11–13 s,
    5 turnos, `mis_partes_de_horas` → `proyectos` → `tareas`; propuso 44 líneas (4 por día, 8 h/día,
    siguiendo el patrón), el evento del martes 24 a las 10:00 y, en dos de tres pasadas, las dos
    tareas con fecha (el modelo no es determinista en eso).
11. **Sin verificar**: ninguna escritura contra el Odoo real (por diseño); `hr.expense` (se envía
    `total_amount_currency` + `payment_mode`, sin producto); si `project.project` admite notas de
    un usuario sin escritura (no se ofrece); calendario real vacío (sin eventos que probar).
12. **Revisión libre, items uniformes** (petición del usuario): cada propuesta es un item
    `{id, grupo, tipo, titulo, motivo, seg, datos, checked, aviso, estado}`; al servirla se le
    añade `campos` (esquema editable por tipo, `plan.EDIT_FIELDS`: clave, tipo de control,
    lista de opciones y valor actual). Un único editor en la UI (lápiz por item) pinta los campos;
    las listas (`proyectos`, `destinos`, `tipos_actividad`, `etapas`) se leen una vez al final de
    la pasada (`acta.opciones`). Cada cambio vuelve a la forma de salida del modelo
    (`plan.to_input` + `apply_changes`) y pasa por **la misma validación** que la propuesta
    original (`plan.rebuild`); `Executor.replace(id, version)` sustituye el item conservando su
    `id` (es el punto de entrada previsto para «corrígelo con un mensaje»). El cuerpo de la nota
    es editable (por defecto, el acta en texto) y se publica escapado.

## 20. Secretaria con Google: Gmail, Calendar y Drive (rama `feat/secretaria-google`)

Código nuevo en `micnaga/brain/google/`; cambios pequeños y aditivos en el runner, `secretary`,
`executor`, `service`, `bridge`, la configuración y la interfaz. Guía:
`docs/ONBOARDING.md` §7.

1. **Sin tokens de Google.** Micnaga usa los conectores oficiales de la cuenta de claude.ai del
   usuario; toda llamada, de lectura o de escritura, pasa por `claude -p`. Solo con la sesión de
   Claude Code (con token o clave, los conectores no existen; sin verificar con un token real).
2. **Qué ve cada `claude -p`** (medido, Claude Code 2.1.327):
   - `--strict-mcp-config` **oculta** los conectores de claude.ai. Con Google activo no se usa.
   - Sin él, la conexión MCP es **no bloqueante**: con `--setting-sources project` el `system/init`
     sale a ~0,6 s, sin conectores, y la sesión no los tiene. `MCP_CONNECTION_NONBLOCKING=0` (variable
     no documentada, leída del binario) hace esperar ~1,3 s y aparecen. Se mantiene
     `--setting-sources project`: así no se cargan los ganchos ni los servidores MCP del usuario.
   - **Lista blanca real**, `--settings {"allowedMcpServers": [...]}`: los conectores por URL
     (`https://gmailmcp.googleapis.com/*`, `calendarmcp`, `drivemcp`) y los servidores propios por
     nombre (`micnaga` y los de `secretaria.mcp_servers`). El resto no se carga: otros conectores
     de claude.ai y los servidores MCP del usuario, locales o remotos (comprobado en `system/init`).
   - `--disallowedTools` quita la herramienta de la sesión, también por servidor (`mcp__<servidor>`).
     Se deniegan todas las escrituras de Google, las lecturas que el análisis no usa, los servicios
     desactivados y los servidores ajenos vistos en la última detección.
   - Tras cada pasada, un conector que no sale `connected` en `system/init` ve descartado lo suyo.
3. **Detección** (`google/detect.py`): `claude -p` con la lista blanca, **cortado al llegar
   `system/init`** (1,4-1,5 s, cero tokens). Respaldo y nombres de servidores ajenos: `claude mcp
   list` (5-6 s). Caché en `<datos>/secretaria/google.json`; «Comprobar» va en un hilo.
4. **Análisis de solo lectura.** Herramientas permitidas: Gmail `search_threads`/`get_thread`;
   Calendar `list_events`/`search_events`/`get_event`/`suggest_time`; Drive
   `search_files`/`read_file_content`/`get_file_metadata`. Instrucciones con topes (1-3 búsquedas
   por servicio, `pageSize ≤ 5`, fragmentos `BRIEF`, máximo 2 lecturas completas) y +12 turnos.
   Salida `google` (con `--json-schema`): `enlaces`, `contrastes`, `reunion_siguiente`, `correo`.
   Validación local: solo se aceptan las URLs de Google que aparecen tal cual en los resultados de
   las herramientas; los correos de asistentes, solo si se vieron (evento, Gmail o transcripción);
   «Contrastes» solo con fuente y si lo dicho ≠ lo escrito.
5. **Contexto al grabar** (`google/context.py`): tras el «Sí» (grabación confirmada), un hilo pide
   `list_events` de ahora ± 15 min. Elige en Python el que lleva el código de Meet de la pestaña, o
   el más cercano. Título → pastilla y título provisional (y el título en directo con Haiku se
   omite); asistentes → candidatos para nombrar hablantes y buscar en Odoo. Fallos silenciosos,
   tope de 45 s. Medido: 3,5 s.
6. **Propuestas** (grupo `google`):
   - `evento_google`: **siempre** `addGoogleMeetUrl: true`. Asistentes desmarcados; con alguno
     marcado, `notificationLevel=ALL` y la revisión dice «Se enviará invitación a X»; sin ninguno,
     `NONE`. Sin hora acordada, huecos de `suggest_time` y la propuesta sale desmarcada.
   - `correo_google`: `create_draft` con firma; «Enviar directamente» (`send_message`) nunca sale
     elegido. `secretaria.google_email_mode = draft | off`.
   - `acta_drive`: Google Doc (HTML) en `google_drive_folder_id`; desmarcado.
   - Con Calendar activo, un `crear_evento` de Odoo (§19) se convierte en `evento_google`
     (`analysis.route_odoo_events`, con prueba sintética): todo evento pasa primero por Google.
     `Secretary.analyze` llama a `gplan.finish(...)` después de `plan.build_crm` y antes de
     `plan.number`. Sin Calendar, `crear_evento` sigue yendo a Odoo (`odoo_calendar`).
7. **Escrituras tras «Aplicar»** (`google/apply.py`, `calls.py`, `guard.py`):
   - Una ejecución `claude -p` (Haiku) por propuesta, con la lista blanca de ese conector y el
     resto de sus herramientas denegadas. La herramienta **no** va en `--allowedTools`: el único
     permiso lo da el guardián (`allow` del gancho).
   - La entrada exacta se construye en Python desde los datos ya editados, nunca desde la salida
     del modelo.
   - **Gancho `PreToolUse`** en `--settings`: `micnaga hook-guard --plan <json>`.
     - Se ejecuta antes de cada llamada, también en `-p`, y deniega aunque `--allowedTools`
       permita la herramienta (medido, deja `permission_denials`).
     - Solo deja pasar una llamada cuyo nombre y entrada normalizada coinciden con lo aprobado. Al
       dejarla pasar devuelve `updatedInput` = la entrada aprobada (medido: se ejecuta la del
       gancho, no la del modelo).
     - Cada aprobación es de un solo uso (recibo `O_EXCL`).
     - Medido: si la herramienta está en `--allowedTools`, Claude Code deja **pasar** la llamada
       cuando el gancho sale con 1, no existe (127) o se pasa de tiempo. Sin `--allowedTools`, esos
       mismos fallos dejan la llamada **denegada** (`dontAsk`) y un `allow` del gancho sí la
       ejecuta: por eso solo autoriza el guardián (falla cerrado; prueba real con un gancho roto).
       Además el guardián sale con 2 ante cualquier error, se prueba antes de cada ejecución
       (`self_test`) y toda escritura con éxito sin recibo se marca `unguarded`.
     - En Windows lo ejecuta Git Bash: rutas con `/` y entre comillas (con espacios, probado). Su
       salida es JSON solo ASCII: en `cp1252` un «·» rompía el `updatedInput` (bug encontrado en
       las pruebas).
   - **Idempotencia**: `google_writes` en `micnaga.db` (`intent` antes de lanzar, `done` con ids y
     enlaces, `unknown` si la llamada salió y no se supo el resultado). El recibo del guardián
     decide: sin recibo, la llamada no llegó a ejecutarse y se puede repetir. Con recibo, solo se
     repite si se **comprueba** que no existe: eventos, buscando su marca `ref` (`list_events` +
     `fullText`). Borradores, correos y ficheros no se pueden comprobar: se paran con «Puede que
     ya esté hecho · revísalo». Editar la propuesta (otra `revision`, otra clave) permite
     reintentar a propósito.
   - Si la misma reunión llega como `reunion_siguiente` y como `crear_evento` de Odoo (misma
     hora), solo queda la de Google.
   - Ids y enlaces salen de `tool_use_result.structuredContent` del stream.
8. **Espejo en Odoo** (`google/mirror.py`): `calendar.event` con el mismo título y hora (UTC), el Meet
   en `videocall_location` (o en la descripción si el campo no existe), el enlace de Google y
   `google_event_id: <id>` en la descripción. Solo el usuario interno como asistente. Contexto
   `no_mail_to_attendees`, `mail_notrack`, `mail_create_nolog`, `tracking_disable`. Deduplica por
   `google_odoo` y por la descripción. Si `ir.module.module` tiene `google_calendar` instalado, no
   hace nada. Un fallo del espejo no deshace el evento. Su contexto amplía
   `odoo_calendar.NO_MAIL_CONTEXT` (§19), así que ambos caminos comparten la regla «sin correos».
9. **Propuestas editables**: un solo camino, el de §19.12. Los items de Google entran en él:
   - `google/items.py` describe sus campos con la misma forma que `plan.EDIT_FIELDS` (`clave`,
     `tipo`, `valor`, más `etiqueta`) y añade los tipos que Odoo no usa: `datetime`, `choice`,
     `emails` y `people` (asistentes; «on» = se invita).
   - `Executor.edit` / `Executor.replace` derivan el grupo `google` a `items.edit` /
     `items.replace`: validan, **conservan el id**, suben `revision` (otra clave de idempotencia)
     y marcan el item.
   - En la interfaz, el editor de `secretaria.js` pinta todos los items y guarda cada cambio;
     `items.js` solo aporta los controles de esos cuatro tipos, el aviso «Se enviará invitación
     a …», los enlaces de resultado y «Contrastes».
10. **Medidas reales** (sesión Max de este PC):
    - `list_events` 0,6 s, `search_threads` 1,0 s, `search_files` 0,8 s dentro de una ejecución.
    - Una llamada guardada entera: 3,2-3,7 s.
    - Pasada sobre una reunión sintética con los tres servicios: 11,5 s, 4 turnos, 2 lecturas
      (Gmail y Drive), unos 0,04 $ equivalentes.
    - Sin hora acordada: 12,2 s, 5 turnos, más `suggest_time`, que devolvió 3 huecos.
11. **Escrituras reales autorizadas** (una vez, por el camino de «Aplicar» con el guardián): un
    evento de prueba sin invitados (`NONE`, con Meet, comprobado con `get_event`, borrado), su
    espejo en Odoo (con `unlink` permitido: creado, comprobado, borrado) y un borrador a la propia
    dirección (comprobado con `get_draft`, borrado). Se hicieron con la herramienta aún en
    `--allowedTools`. El cambio a «solo autoriza el guardián» se verificó después con lecturas
    reales por el mismo camino y con la prueba del gancho roto.
12. **Sin verificar**:
    - Con token o clave de API.
    - Un «Contraste» real: no había documento discrepante y no se crean documentos de prueba.
    - El modo congelado `micnaga-cli.exe hook-guard`.
    - La lista de servidores ajenos que se deniegan explícitamente sale de la última
      «Comprobar» (Ajustes la lanza sola la primera vez). Mientras tanto la lista blanca ya
      impide que se carguen.

## 21. Investigar primero y un acta más lista (`micnaga/brain/research.py`)

La Secretaria investiga antes de escribir, siempre en solo lectura y con topes, y el acta cita lo
que trae de fuera. El prompt pasa a la v3.

1. **Herramientas locales nuevas** en el servidor `micnaga`, siempre disponibles, también sin Odoo:
   - `reuniones_con(texto)`: reuniones anteriores con esa persona o empresa (por participantes,
     título o acta), de la más reciente a la más antigua, con cuántos pendientes dejó su acta.
   - `leer_acta(carpeta)`: el acta completa de una de ellas: resumen, decisiones, tareas,
     próximos pasos, lo que seguía abierto y las propuestas aplicadas, con el id creado en Odoo.
   - La reunión en análisis nunca cuenta como anterior (`MICNAGA_CURRENT`). `leer_acta` solo lee
     carpetas que cuelgan directamente de la biblioteca.
2. **Candidatas calculadas en Python** (`related`): las reuniones con acta que comparten
   participantes con esta. Los participantes salen de los hablantes con nombre y de los
   asistentes del evento de Calendar. Van en la sección «Investigación» del pedido, con un
   orden de trabajo:
   - Primer turno, todo a la vez: `leer_acta`, la empresa en Odoo y una búsqueda de correo.
   - Después: las tareas abiertas del proyecto, el historial y la oportunidad.
   - En total, 2-3 turnos. `max_turns` sube a 40 con Odoo y a 20 sin él.
3. **Acta**:
   - `contexto`: una línea, solo si aporta.
   - `desde_la_ultima`: cada pendiente de la reunión anterior con su estado (hecho, abierto o
     desconocido) y su prueba (frase de esta reunión o fuente).
   - `fuentes`.
   - En la vista, una fila discreta «Fuentes» (Acta 12 nov · Gmail · Odoo · Drive), que también
     recoge los `enlaces` de Google: un solo sitio para todo.
4. **Nada de fuera sin fuente** (`validate` con `Seen`):
   - Un acta se cita solo si existe, no es la actual y el modelo la vio (en un resultado de
     herramienta o entre las candidatas).
   - Las URLs de Gmail, Drive y Calendar tienen que aparecer tal cual en un resultado.
   - Un registro de Odoo (`modelo:id`) solo si su `"id"` salió en un resultado; su enlace va al
     Odoo configurado.
   - «Hecho» o «abierto» sin prueba pasa a «desconocido».
   - Un `contexto` sin ninguna fuente válida se descarta.
   - `open_link` abre solo Google o el Odoo configurado.
5. **Tareas**:
   - Antes de `crear_tarea`, el modelo mira las tareas abiertas y las propuestas de la reunión
     anterior. Si la tarea ya existe, propone una nota en esa tarea en lugar de duplicarla.
   - La tarea va en el proyecto del cliente o del tema, con una descripción breve (qué, por qué y
     de dónde sale) y la fecha que se dijo en la reunión o en un correo.
   - Lo abierto de la vez anterior se arrastra.
   - Sigue funcionando el aviso de tarea parecida de §19.
6. **Progreso**: los `tool_use` del stream ya llegaban a la vista. Ahora se muestran como
   «Revisando actas anteriores…», «Leyendo Gmail…», «Buscando en Drive…» y «Mirando el
   calendario…», además de los textos de Odoo de siempre.
7. **Evaluación real** (`tests/brain/test_real_research.py`, `MICNAGA_RUN_CLAUDE=1`), todo
   sintético: el acta anterior con dos pendientes, el Odoo falso con la tarea de plantillas
   abierta y un correo falso (servidor MCP stub, sin Gmail real).
   - El resultado: el acta cita la reunión anterior y marca el presupuesto como «hecho», con el
     correo como prueba. Deja las plantillas «abierto», con la tarea de Odoo como prueba.
   - No duplica la tarea existente: propone una nota en ella. Crea la acción nueva en el proyecto
     del cliente, con fecha y descripción.
   - Dos variantes:
     - Cliente ya nombrado: 12,8-14,3 s de `claude`, 6-9 turnos, unos 0,03-0,07 $ equivalentes y
       1,7-1,9 k tokens de salida.
     - Cliente aún «Hablante 1», lo normal al analizar: no hay candidatas, así que el modelo lo
       busca con `reuniones_con` por el nombre que se dice. 17-18 s, 10 turnos, unos 0,04 $.
   - Hasta que el brief no dijo «**siempre** una búsqueda de correo», el modelo no buscaba en el
     correo.
   - Gmail y Drive reales devuelven `viewUrl` en el texto del resultado (comprobado: solo nombres
     de campo). `Seen` recibe además el `structuredContent` y deshace `\/` y `\u0026`.
   - Pasada real con Google y la investigación a la vez (transcripción sintética): 9,2 s, 4
     turnos, `reuniones_con` y una búsqueda real en Gmail.
8. **Acta de relleno**: una vez, el modelo real devolvió «titulo: x» con todo vacío. Ahora una
   reunión con contenido cuyo acta llega sin resumen y con un título de relleno se analiza otra
   vez; si se repite, `ClaudeOutputError` (la cola reintenta luego) y nunca se guarda vacía.
9. **Candidatas**: solo con el mismo nombre y primer apellido (o el mismo nombre si solo hay uno).
   Así un «Carlos» de otro cliente no cuenta. El propio usuario (`self` en el evento) no cuenta.
10. **Sin verificar**: la investigación con Gmail real sobre un cliente real (solo con el correo
    stub y una reunión sintética) y la línea de progreso con datos reales en la vista (solo los
    textos, con el `claude` falso).

## 22. Corregir con un mensaje y una Secretaria más libre (rama `feat/secretaria-ux`)

Petición del usuario: poder decirle a cualquier propuesta «no, mejor esto» y que lo haga, todo
bonito, simple y sin texto de relleno.

1. **Revisión de un item** (`brain/refine.py`, `Executor.revise`/`revert`): un `claude -p` corto
   de **solo lectura** con las mismas herramientas que el análisis (servidor `micnaga` según el
   perfil de Odoo; con Google, solo sus lecturas y la misma lista blanca, sin
   `GoogleRun.extend`). Entrada: el mensaje, el item en la forma del modelo, sus campos, las
   opciones ya conocidas (proyectos, destinos, etapas), «Hoy es jueves 2026-11-19», el acta y
   ±8 frases alrededor de la prueba. `--json-schema` cerrado a los campos de ese tipo
   (`cambiado`, `nota`, `datos` con solo lo que cambia).
2. **Lo que vuelve se filtra otra vez en Python**: claves de ese tipo; correos nuevos solo si se
   vieron (item, reunión, mensaje o resultado de herramienta); `explicito`, la marca y los
   invitados ya activos se conservan del item; nadie nuevo entra invitado y un borrador nunca
   pasa a envío. Después, la validación de siempre (`Executor.replace`): mismo `id`, `revision`
   sube (ahora también en Odoo), versión anterior en `previa` para «Deshacer» de un paso (que
   también sube `revision`: la clave de Google la incluye). Si el item cambió mientras se
   pensaba, `stale`; si no cambia nada o el modelo no puede, una línea con su `nota`.
3. **La lista entera**: `cambiar` / `quitar` (queda «descartada») / `anadir` (misma validación
   que el análisis: `plan.build_crm`, y los constructores de Google; con Calendar, todo evento es
   de Google). Lo nuevo sale **sin marcar**, con ids nuevos que no se reutilizan (`ultimo_id`).
   «Deshacer» devuelve la lista anterior si no se aplicó nada desde entonces.
4. **Una sola ejecución** (`run_once`, `max_turns` 6, 90 s): un reintento ciego duplicaría la
   espera. Funciona sin Odoo (correcciones y Google; un item de Odoo dice «Odoo no está
   conectado» sin llamar a Claude) y sin Google.
5. **Medido** (real, Sonnet, Odoo falso, `tests/brain/test_real_refine.py`): «mejor para el
   jueves y ponla en el proyecto RegulAI» → fecha 26-11 y RegulAI (que no estaba en las opciones:
   lo buscó con `proyectos`), 3 turnos, **3,9-4,3 s** de reloj, ninguna escritura.
6. **Interfaz**: tarjetas con glifo por tipo, una línea clara y otra discreta (el `motivo` pasa
   a `title`), casilla propia, ✦ «Corregir con un mensaje» que abre una línea de cristal en la
   tarjeta (Intro envía, Esc cierra), brillo violeta mientras piensa con el progreso real,
   fundido al terminar y lo cambiado iluminado una vez; «Deshacer». Barra fija arriba:
   «Pide un cambio…» para la lista, «Deshacer» y «Aplicar (n) ↵». Aplicado: ✓ y «Ver en Odoo»
   (`/web#id=…&model=…`, el único prefijo de Odoo que abre `open_link`). Fallido: la causa en
   rojo y «Reintentar» (vuelve a «Aplicar»; deshabilitado si Google no sabe si se hizo). Acta:
   chips de estado en «Desde la última vez», «Contrastes» en dos líneas, «Fuentes» como chips y
   la línea viva con el rastro de dónde ha mirado. Horas: «Marcar todo», casilla por día, sin
   «como siempre» repetido, campos sin borde hasta pasar el ratón, «Libre» al pasar. Ajustes:
   Claude, Odoo y Google con la misma fila de estado arriba, un punto junto a cada título y
   «Qué puede hacer» también para Google. Sin `requestAnimationFrame` (WebView2 ocluido, §18.6);
   todo respeta «Efectos de animación» y `prefers-reduced-motion`.
7. **Quitado**: «Nadie recibe invitación salvo que lo actives» (el aviso sale solo cuando alguien
   está invitado), «Sin invitados · con Meet», «patrón de tus últimas semanas», la línea de
   teclas del panel de horas y «Pulsa Probar para ver qué puede hacer».
8. **Capturas reales** (`python -m tests.ui.real_shots`): ventanas pywebview/WebView2 de una
   instancia aislada (carpetas y secretos temporales, Odoo y `claude` falsos), `PrintWindow` del
   HWND de Micnaga. `docs/screenshots/secretaria_ux_*.png`, claro y oscuro; antes y después de
   una corrección: `refine_before` / `refine_thinking` / `refine_after`.
9. **Sin verificar**: la revisión con Google real (la lista blanca y las lecturas son las del
   análisis, ya medidas en §20) y con el `claude` real dentro de la ventana (las capturas usan el
   falso; la latencia real es la del punto 5).

## 23. Investigar siempre, transcripción limpia con Haiku y «Acta completa» (rama `feat/brain-cleanup`)

Origen: una reunión real de 47 min con 2 hablantes. El otro hablante seguía como «Hablante 1» y
no había evento de Calendar. El acta salió en 9 s, con 2 turnos y 0 herramientas (Odoo
`conectado`): el modelo **decidió** no investigar. El prompt pasa a la v4.

1. **Investigar siempre** (`research.extract`, `prior_by_terms`, `first_calls`, `missing`):
   - **Candidatos sacados de la conversación** (heurística en Python, sin modelo):
     - nombres dichos en voz alta («gracias, Pedro», «soy Marta»);
     - entidades: palabras con mayúscula fuera del inicio de frase, siglas y el título;
     - temas: palabras largas frecuentes.
   - **Reuniones candidatas también por esos términos**, no solo por participantes.
   - **El brief lleva las llamadas exactas del primer turno, con sus argumentos**:
     - `buscar_en_reuniones("…")` y `reuniones_con("…")`;
     - con Odoo, `buscar_empresas` / `buscar_contactos` / `proyectos`;
     - con Gmail o la herramienta de correo, una búsqueda `"X" OR "Y" newer_than:90d`.
   - **Un recordatorio corto al final del pedido**, después de la transcripción larga.
   - **Si aun así falta una categoría** (actas, Odoo o correo), se repite **una vez** con
     «Falta investigar». Va en la misma repetición que el acta de relleno de §21.8.
     - Solo se exige lo que estaba `connected` en `system/init`.
     - El correo vale por Gmail o por un servidor extra de `secretaria.mcp_servers`.
     - Un primer borrador exigía siempre Gmail en cuanto había un servidor extra, y la pasada
       real de §21.7 se repetía siempre: 63 s frente a los 30 s de ahora para los dos casos.
   - **Tiempo acotado:** solo si quedan más de 60 s de `budget_s` (420 s) y con el tiempo que
     quede.
   - `buscar_en_reuniones` excluye la reunión actual y, si la frase entera no da nada, busca
     palabra a palabra.
2. **Hablantes sin nombre:**
   - El brief trae pistas: «#12: `me` llama «Carlos» a alguien (solo puede ser `S1`)».
   - Una propuesta `hablante` solo sobrevive si **cada palabra** del nombre (nombre y
     apellidos) se dijo en la reunión, salió en una herramienta, está en el evento o entre los
     participantes de las reuniones candidatas (`research.name_seen`). Un «Carlos Ruiz» cuando
     solo se dijo «Carlos» no pasa.
   - La validación `Seen` de §21 no cambia.
3. **Limpieza con Haiku** (`brain/cleanup.py`, prompt corto `prompts/limpieza.md`):
   - **Cuándo:** después del ASR y la diarización y antes del acta. La llama
     `Secretary.clean` desde `BrainService._process`; no toca `pipeline/`.
   - **La llamada:** `claude -p --model haiku` sin herramientas ni MCP, con `--json-schema`
     `{cambios: [{ids, texto}]}`.
   - **Ventanas:** de 5 párrafos (≤ 3000 caracteres) con 2 párrafos anteriores de solo lectura,
     para poder unir en el borde y mantener nombres. 24 en paralelo, 60 s por ventana y 90 s en
     total.
   - **Vocabulario:**
     - el glosario, `learned.md` y el contexto propio;
     - `secretaria/vocabulario.json`: nombres de Odoo vistos en pasadas anteriores (proyectos,
       empresas, contactos), que se rellena al terminar cada acta;
     - los asistentes del evento, los hablantes con nombre y los participantes de otras
       reuniones;
     - los términos que se repiten en esta.
4. **La seguridad está en Python** (`check`):
   - **Comparación:** tokens normalizados (minúsculas, sin tildes ni puntuación). Cada número,
     dicho o escrito, es un token con su valor: «cuatrocientos cincuenta mil» = «450.000»,
     «tres coma cinco» = «3,5», «veinte por ciento» = «20 %».
   - **Gratis:** puntuación (también los puntos falsos dentro del párrafo), mayúsculas, tildes,
     cifras y palabras mal partidas o pegadas («micro soft» → «Microsoft»).
   - **Cuentan como sustitución:**
     - una palabra cambiada, que tiene que parecerse a lo oído (≥ 0,5);
     - una sigla deletreada («pe cu ce» → «PQC») o una palabra del vocabulario (≥ 0,4) cuestan
       1;
     - el total ≤ max(2, 0,35 × tokens).
   - **Se rechazan:**
     - más de 2 palabras añadidas o quitadas (sin quitar muletillas ni añadir frases);
     - cifras, meses o días que no se dijeron;
     - un cambio de longitud > 30 %.
   - **Uniones de párrafos** (petición del usuario: el ASR pone puntos falsos y parte frases):
     mismo hablante, ids seguidos, pausas ≤ 1,5 s, ≤ 4 párrafos y ≤ 1600 caracteres; nunca se
     parte un párrafo.
   - **Al guardar se vuelve a leer la transcripción:** lo que la biblioteca cambió mientras
     tanto no se pisa.
5. **El original se conserva:**
   - `Segment.text_original` (opcional, `types.py`) y `merged_from` en las uniones. El JSON de
     siempre no cambia si no hay limpieza.
   - Las palabras se realinean con el texto nuevo (`remap_words`): mismos tiempos donde la
     palabra es la misma y tramo repartido donde cambió. `" ".join(w.w) == text` sigue valiendo
     para el SRT y para el prompt.
   - Se regeneran MD y SRT y se reindexa `segments_fts`, que busca el texto nuevo.
   - Lo que se oyó, en los párrafos con alguna palabra cambiada, va a `original_fts` (tabla
     propia, renombrada con la carpeta). La búsqueda de la biblioteca encuentra las dos formas.
   - Una vez por reunión (`limpieza.json`, con las cifras).
     - Sin Claude se salta en silencio y sin marca, así que se reintenta con la pasada
       siguiente.
     - Si falla alguna ventana (límite de uso, tiempo), se guarda lo corregido sin marca y la
       pasada siguiente lo completa; el `text_original` de la primera vez se conserva.
   - Ajuste «Corregir transcripción con IA» (`secretaria.clean_transcript`, encendido; solo
     actúa con la Secretaria activada).
   - **Límite conocido:** «Fusionar hablantes» (`pipeline.merge_segments`) rehace los segmentos
     sin `text_original`. No se toca `pipeline/`.
6. **Interfaz:**
   - Las palabras corregidas llevan un subrayado punteado muy suave; al pasar el ratón,
     «Se oyó: …». La puntuación, las tildes y las mayúsculas no se marcan.
   - Un «Original» pequeño en la cabecera muestra el texto del ASR.
   - La fase «Corrigiendo la transcripción…» aparece en la línea viva.
7. **«Acta completa»** (`brain/full_acta.py`, `prompts/acta_completa.md`, `fullacta.js`):
   - **Solo a petición:** botón junto a «Rehacer» en la tarjeta del acta.
   - **La llamada:** un `claude -p` de solo lectura con el modelo del acta y
     `--include-partial-messages` (el texto llega en streaming a la vista).
     - **Entrada:** la transcripción limpia; el acta breve con `fuentes` / `contexto` /
       `desde_la_ultima`, para no repetir la investigación; hablantes, fecha y duración;
       `learned.md`.
     - **Herramientas:** solo las locales de la biblioteca (`MICNAGA_TOOLS` vacío), con un tope
       de 8 turnos.
   - **Formato:** Markdown en estilo de acta formal: encabezado, orden del día, una sección por
     tema con `[mm:ss]`, decisiones con motivo, acuerdos y tareas, preguntas, próximos pasos y
     fuentes. Unas 450 palabras por cada 12 min.
   - **Validación como en §21:**
     - URLs no vistas → texto;
     - actas inexistentes o no vistas, `modelo:id` no leídos y `[mm:ss]` fuera de la reunión →
       fuera.
   - **Dónde se guarda:** `acta_completa.md` + `acta_completa.json`, con la huella de la
     transcripción: «desactualizada» si cambió. También se indexa en `acta_fts`.
   - **Vista:** de documento dentro del panel. Cada `[mm:ss]` reproduce ese momento; acciones
     Copiar, Abrir archivo y Regenerar. La segunda vez abre al instante (caché).
8. **Medido** (sesión Max de este PC; coste en dólares equivalentes de `claude -p`):
   - **Pruebas reales sintéticas:**
     - limpieza (`test_real_cleanup.py`, 10 párrafos con errores sembrados): 3 pasadas de 3,
       6,5-8,4 s y unos 0,0013 $. Arregla la sigla, el nombre del cliente, «cuatrocientos» y el
       punto falso, y une la frase partida; la frase rara pero correcta queda intacta.
       - Antes de esas tres, una pasada en la que Haiku dejó «cuatrocientos» en letra; desde
         entonces el prompt lleva el ejemplo.
     - «Acta completa» (`test_real_full_acta.py`): 10,3 s, 528 palabras, 10 secciones,
       0,028 $.
   - **La reunión real de 47 min** (copia en carpetas temporales, configuración y secretos
     reales en solo lectura, sin escrituras en Odoo ni en Google; Google apagado en esta
     configuración):
     - **Limpieza:** 216 párrafos en 44 ventanas.
       - 40,6 s con 24 en paralelo (ventana mediana 14,6 s, máxima 27,1 s); 0,077 $.
       - 100 párrafos corregidos, 1 unión y 21 cambios rechazados: 8 sustituciones poco
         parecidas, 7 uniones con pausa > 1,5 s, 5 cifras no dichas y 1 palabra quitada.
       - Con 8 en paralelo eran 80 s; con 16, 57-60 s.
     - **Acta:** 17,2 s, 7 turnos y 5 herramientas (`buscar_en_reuniones`, `reuniones_con` ×2,
       `buscar_contactos`, `proyectos`); 0,135 $. Antes: 0 herramientas.
     - **Acta completa:** 39,4 s, 2173 palabras, 16 secciones, 66 marcas de tiempo; 0,15 $.
9. **De dónde salen los puntos falsos** (diagnóstico; no se toca `pipeline/`):
   - La etapa 6 transcribe **por bloque de VAD**, y Parakeet puntúa cada bloque por separado:
     un corte de VAD a mitad de frase deja un «.» y la mayúscula siguiente.
   - La etapa 8 (`phrases.build_phrases`) corta la frase en **cada** fin de frase del ASR.
   - La etapa 11 (`merge_segments`) vuelve a unir las del mismo hablante con pausa < 1,5 s,
     salvo que el párrafo ya dure ≥ 45 s o tenga ≥ 700 caracteres y la palabra anterior termine
     en punto.
   - **Recuento en la reunión real** (45 fronteras entre párrafos del mismo hablante):
     - 19 con pausa < 1,5 s, todas por el tope de longitud de un párrafo que acaba en «.»;
     - 26 por una pausa ≥ 1,5 s;
     - además, 286 «x. Y» y 7 «x. y» dentro de los párrafos.
10. **Sin verificar:**
    - La investigación con Gmail real (Google está apagado en la configuración real).
    - El «Acta completa» y el subrayado dentro de la ventana WebView2: las capturas
      (`python -m tests.ui.shots --only cleanup`) son de Chromium.
    - 24 `claude -p` a la vez en el portátil i5-1245U (memoria y límites de uso).

## 24. Procesado en vivo y procesado rápido al colgar (rama `feat/live-pipeline`)

Origen: una reunión real de 47 min (2 hablantes, auriculares) tardó 18,5 min en estar lista tras
colgar en el PC del usuario (i7-12700F); el ASR solo usaba ~3 núcleos.

1. **Por qué el ASR iba lento: hilos de onnxruntime en espera activa + prioridad baja.** Medido
   con el codificador de Parakeet (`asr_profile`, tramo de 2 min, 6 hilos, BELOW_NORMAL): con el
   equipo libre la decodificación TDT (bucle Python) es solo el 5-7 % del tiempo; el codificador
   manda. Con el equipo ocupado (procesos normales en la CPU) los hilos de onnxruntime que esperan
   girando son desalojados y cada operación paralela espera al más lento: **101 s** frente a
   **9,7 s** con la espera activa desactivada (`session.intra_op.allow_spinning = 0`). Con el
   equipo libre la velocidad es la misma y usa la mitad de CPU. `engines/ort_options.py` lo aplica
   a Parakeet, WeSpeaker y Silero. Nada de esto se ve en un banco con el equipo libre.
2. **Descartado con medidas:** pasar varios bloques juntos por el codificador (`recognize` con
   lista: el relleno cuesta más de lo que ahorra), más hilos (6/8/10 iguales: el codificador int8
   no escala) y un valor `threads = "auto"` (mismo motivo; no se añade). Micro y sistema **en
   paralelo**: ver punto 3.

## 25. Acabado visual (rama `feat/visual-finish`)

1. **Pulsación con tokens.** `--dur-press` (60 ms), `--press-scale` (0,97, botones y chips),
   `--press-scale-sm` (0,92, controles del tamaño de un icono y casillas) y `--press-scale-lg`
   (0,99, filas). Sustituyen los 0,86 / 0,9 / 0,94 / 0,95 / 0,98 sueltos; con movimiento reducido
   valen 1. `.tr-btn` gana estado pulsado; la rotación del icono de «rehacer» y `sec-pop` (sin uso)
   se quitan.
2. **Las tarjetas de la Secretaria se revelan una vez por reunión.** `secretaria.js` reconstruye el
   panel en cada `render()` (y cada segundo mientras trabaja), así que la entrada de la tarjeta se
   repetía: la línea en vivo «parpadeaba» cada segundo. `motion.js` marca `.seen` la tarjeta de un
   tipo ya mostrado antes de pintar. El acta se lee por secciones (cabecera, contexto, resumen,
   bloques) a `--stagger`.
3. **«Aplicar».** La casilla cae con un 4 % de rebote y deja un anillo fino que se disuelve; la
   fila toma un lavado violeta que se vacía (1,1 s). Una vez por fila, en cascada; sin confeti.
4. **Asistente.** Sin cajas dentro de caja: las dos comprobaciones de audio son una sola tira
   partida por una línea; más aire (44 px); el estado en `--text-2`; el limbo enmascarado a la
   mitad inferior (nada tramado detrás del texto); el escalonado ya no cuenta la marca oculta.
5. **Contraste AA en claro.** `--text-3` #66666f → #5e5e67 (4,43 → 5,0 sobre la fila
   seleccionada) y `--warm` #c4501b → #ad4312 (4,19 → 5,26 sobre el fondo). Nuevo `--warm-ink`
   (blanco en claro, casi negro en oscuro) para los glifos sobre cálido: «grabar», la fallida, el
   botón del popup.
6. **Coste del limbo medido en WebView2** (biblioteca abierta, quieta, ventana visible; suma de
   CPU de los `msedgewebview2.exe` hijos del proceso, 20 s por toma, 20 núcleos lógicos): con el
   limbo 9,2 % y 5,8 % de un núcleo; parado (`data-motion="off"`) 0,0 % y 0,3 %. Es decir,
   ~6-9 % de un núcleo (~0,3-0,5 % del equipo), y solo con la ventana enfocada.
