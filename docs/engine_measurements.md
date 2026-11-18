# Medidas de los motores (velocidad y memoria)

> **Qué es y qué no es.** Medido en **este PC de desarrollo: Intel Core i7-12700F (8 P + 4 E, 20 hilos),
> 32 GB de RAM, Windows 11 — NO el equipo de referencia i5-1245U (2 P + 8 E) de la spec.** Con
> `processing.threads = 6` (el valor por defecto). El audio es **voz sintética SAPI** (Helena es-ES,
> Zira en-US) repetida hasta 5 o 60 minutos, no grabaciones de reuniones; la velocidad del ASR depende
> poco del contenido, la de la diarización algo más. Durante las medidas **otros cuatro agentes
> estaban compilando y ejecutando tests en la misma máquina**, de ahí que dos pasadas seguidas
> difieran hasta un 25 %. Hay que repetir todo en el i5-1245U antes de dar por buenos los objetivos
> de la spec (§2: RAM procesando < 3 GB, factor ≤ 0,4).

Reproducir (cada motor en un proceso nuevo, para que el pico de RAM no se contamine):

```powershell
$env:MICNAGA_MODELS = "<repo>\.models"
.venv\Scripts\python.exe -m tests.engines.measure_engines --all --markdown docs\engine_measurements_run.md
```

El ASR se alimenta como lo hará el pipeline: Silero VAD → bloques de ≤ 30 s → `transcribe`. «Factor»
= tiempo de proceso / duración del audio (el objetivo de la spec es ≤ 0,4 para **todo** el pipeline).
«RSS pico» = `peak_wset` del proceso (incluye Python, numpy y el audio de prueba en memoria).

## Resultados (dos pasadas)

| Motor | Audio | Carga | Proceso (pasada 1 / 2) | x tiempo real (1 / 2) | Factor (1 / 2) | RSS pico (1 / 2) | Notas |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Silero VAD (ONNX) | 5 min | 0,15 s | 1,2 / 1,3 s | 256 / 237 | 0,004 | 133 / 131 MB | 90 regiones |
| **Parakeet TDT 0.6B v3 int8** (`onnx-asr`) | 5 min | 2,5–3,1 s | 16,0 / 20,1 s | **18,7 / 14,9** | 0,053 / 0,067 | **1246 / 1244 MB** (816 MB con el modelo cargado y en reposo) | 11 bloques, 658 palabras |
| faster-whisper `small` int8 | 5 min | 1,6–2,5 s | 55,5 / 61,3 s | **5,4 / 4,9** | 0,185 / 0,204 | **792 / 791 MB** | mismas 658 palabras |
| `diarize` (n_speakers = 2) | 5 min | 0,1 s | 23,1 / 24,8 s | **13,0 / 12,1** | 0,077 / 0,083 | **489 / 490 MB** | 2 hablantes; `embed()` de los 2 en 2,1 s |
| `diarize` (automático) | **60 min** | 0,1 s | 188,7 / 218,3 s | **19,1 / 16,5** | 0,052 / 0,061 | **1283 / 1280 MB** | 4 «hablantes» para 2 voces (ver abajo) |
| AEC `webrtc` (+ alineación) | 1 min | – | 2,7 / 1,9 s | 22 / 31 | 0,045 / 0,032 | 226 / 225 MB | |
| AEC `speex` (256 ms + alineación + preproc.) | 1 min | – | 3,7 / 1,7 s | 16 / 36 | 0,062 / 0,028 | 225 / 226 MB | |

Con los modelos de `pytest` (21 s de voz SAPI, incluye el calentamiento): Parakeet 11,2×,
Whisper 3,8×.

## Qué dicen

* **Memoria:** un solo motor a la vez nunca pasa de ~1,3 GB (Parakeet 1,25 GB; diarización 1 h
  1,28 GB, de los que ~285 MB son del propio arnés de medida: el audio de 60 min en float32). Muy por
  debajo de los 3 GB de la spec. La diarización **no** libera memoria al cerrarse: torch queda cargado
  en el proceso (≈ 340 MB solo por `import diarize`); si hay que devolverla, ejecutar la etapa en un
  subproceso. El RSS al terminar tras `close()` baja a ~115–160 MB para Parakeet/Whisper (el modelo
  se libera) y se queda en ~400–660 MB para `diarize` (torch).
* **Velocidad (estimación, no medida, para 1 h de reunión en este PC):** VAD de 2 pistas ≈ 0,5 min;
  Parakeet ≈ 3–4 min por pista con voz continua (menos con pausas, que el VAD recorta);
  diarización ≈ 3–3,5 min; AEC ≈ 1–2 min. Total ≈ 11–14 min = factor 0,19–0,23 **en este i7**. El
  i5-1245U es varias veces más lento en hilo único y tiene 2 núcleos rápidos: el presupuesto de 0,4
  solo queda holgado si la relación es < 1,6–2,3×; **hay que medirlo allí**. Whisper `small` (factor
  ≈ 0,2 por pista) casi triplica el coste del ASR: plan B, no por defecto.
* **Whisper vs Parakeet en este audio:** Parakeet es 3–4× más rápido; ambos dan 0 % de WER en la
  voz SAPI limpia (ver abajo; no es el set de la spec). Con pausas y voces reales habrá diferencias
  que solo el banco de pruebas puede medir.
* **Diarización automática (honesto):** la estimación de número de hablantes de la librería
  (GMM-BIC) **sobre-segmenta voz sintética limpia**: 13 «hablantes» para 2 voces en 33 s con
  `max_speakers=20`, 6 con el tope de 8. El adaptador añade una fusión de centroides WeSpeaker
  (coseno ≥ 0,55; 0,45 para hablantes con < 10 s de voz; sin calibrar con voces reales): da **2**
  en el diálogo de 33 s y en el de 5 min, pero **4 en el audio de 60 min** (el mismo diálogo de ~3 min
  repetido 18 veces; no se investigó cuáles son los grupos sobrantes: en una prueba de 20 min con
  el umbral anterior (0,65) sobraba un grupo con coseno 0,58–0,59 respecto al grupo grande). Con
  `n_speakers` conocido el resultado es fiable (exactitud de trama 97 % en el diálogo de 2 voces).
  Conclusión: umbrales a calibrar en F2 con reuniones reales y banco de pruebas; mientras tanto el
  pipeline debería pasar `n_speakers` cuando lo sepa y la UI permitir fusionar hablantes (spec §13).

## Calidad sobre voz sintética (NO es el set de la spec §12)

Texto de 21 s con puntuación, tildes, «¿…?» y «¡…!» dicho por la voz SAPI Helena; WER con `jiwer`
(minúsculas, sin puntuación) frente al texto conocido:

| Motor | WER | Observaciones |
| --- | ---: | --- |
| Parakeet TDT 0.6B v3 int8 | 0,0 % | puntuación y mayúsculas incluidas; «¿Me oís bien todos?» bien; omite el «¡» de «¡Perfecto…!» |
| faster-whisper small | 0,0 % | idem |

Es voz limpia, sin ruido, sin solapes y de una sola voz: el WER real de reuniones será peor. El
objetivo de la spec (≤ 12 %) solo se puede evaluar con el set propio.

Diarización sobre un diálogo TTS de dos voces distintas (Helena es-ES / Zira en-US, 8 turnos, 33 s):
2 hablantes con `n_speakers=2` y exactitud de trama del 97 % (el resto son huecos al inicio/fin de
turno); embeddings de hablante: coseno **0,92** entre mitades de la misma voz y **0,30** entre voces
distintas.
