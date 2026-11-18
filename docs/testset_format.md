# Formato del set de prueba (`MICNAGA_TESTSET`)

El set vive **fuera del repositorio** y nunca se sube (spec, sección 12). `micnaga bench` lo lee de
la carpeta indicada por la variable de entorno `MICNAGA_TESTSET` (o de `--testset DIR`).

El banco de pruebas **no modifica el set**: copia cada caso a una carpeta temporal (con su propia
base de datos SQLite) y procesa la copia, porque el pipeline borra las sesiones descartadas y
renombra las terminadas.

```
MICNAGA_TESTSET/
  reunion-01-auriculares/
    audio_yo.flac          # pista del micro, mono 16 kHz
    audio_remoto.flac      # pista del sistema, mono 16 kHz
    capture.json           # metadatos de captura (formato de micnaga.types.CaptureMeta)
    reference.json         # opcional: transcripción de referencia corregida a mano
    expect.json            # opcional: expectativas para los casos trampa
  reunion-02-altavoces/
  trampa-nota-de-voz/
  ...
```

Un **caso** es cualquier subcarpeta que contenga `capture.json`.

## `capture.json`

El mismo que escribe el grabador (`CaptureMeta`). Lo que usa el banco de pruebas:

* `mic.file` / `system.file`: nombres de los ficheros de audio (por defecto `audio_yo.flac` y
  `audio_remoto.flac`; también se admiten WAV).
* `output_kind`: `headphones` | `speakers` | `unknown` (decide si se aplica la cancelación de eco).
* `confirmed`: `true` si el usuario pulsó «Grabar» (solo exige la compuerta de duración).
* `meet_title`, `started_at`, `markers`...

Para los casos trampa, deja `confirmed: false`: lo que se mide es justamente que las compuertas los
descarten.

## `reference.json` (opcional)

Transcripción de referencia en el **esquema de `transcripcion.json`** (spec, sección 9):
`speakers`, `segments` (con `start`, `end`, `speaker`, `text`) y, si se quiere, `words`. El
hablante del usuario es `"me"`; los demás `"S1"`, `"S2"`...

Con referencia se calculan:

| Métrica | Cómo |
| --- | --- |
| WER | `jiwer` sobre el texto de todos los segmentos ordenados por tiempo (minúsculas, sin puntuación, con acentos) |
| DER | `pyannote.metrics` `DiarizationErrorRate` (collar 0,25 s) solo sobre los hablantes remotos (`speaker != "me"`) |
| Frases de otros como «Yo» | fracción de segmentos de referencia no-`me` que en la salida aparecen como `me` (solape ≥ 50 % del segmento y similitud de texto ≥ 0,6) |

Sin `reference.json` el caso solo aporta tiempos, RAM, CPU y estado.

## `expect.json` (opcional, casos trampa)

```json
{"discard": true}
```

* `"discard": true`: el caso **debe** ser descartado por las compuertas. Si el pipeline lo procesa
  se cuenta un **falso positivo** (objetivo: 0).
* `"discard": false`: es una reunión real; si se descarta se cuenta como «reunión real descartada».

Casos trampa recomendados (spec, sección 12): nota de voz de WhatsApp Web de 2 min, prueba de
micro de 30 s, Meet con el micro silenciado durante 1 h, vídeo de YouTube sonando con Meet abierto,
cambio de auriculares a altavoces a mitad (este último es una reunión real: `output_kind` +
`device_changes` en `capture.json`).

## Modo sintético

`micnaga bench --synthetic` genera sesiones sintéticas (`micnaga.bench.synthetic`) en una carpeta
temporal con este mismo formato (más `synthetic_script.json`, que leen los motores falsos), así que
el banco de pruebas funciona de punta a punta sin set de prueba. Usa **motores falsos** por defecto:
el informe lo advierte y sus cifras no son mediciones de calidad.

## Salida

El informe Markdown se escribe en `--out` (por defecto `<datos de la app>/bench/report-<modo>.md`) y
la última ejecución se guarda por modo en `<datos de la app>/bench/last_run.json` (`--last-run`
para otra ruta, `--no-save` para no actualizarla) para la comparativa con la ejecución anterior.
