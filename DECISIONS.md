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
    si fallan las dos al inicio, `start()` falla y limpia.
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

