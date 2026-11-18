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
* `THIRD_PARTY_LICENSES.md` (spec §11) se generará cuando las dependencias estén cerradas.
* Licencia de `pyaec` sin verificar (ver §1).
