# Diarización sin torch (`onnx`)

Motor `onnx` (`micnaga/pipeline/engines/diar/onnx.py`), por defecto desde ahora
(`processing.diarization = "onnx"`). Solo `numpy`, `scipy` y `onnxruntime`: **sin torch, torchaudio,
scikit-learn, `diarize`, `wespeakerruntime` ni `kaldiio`** (esta última tiene licencia no redistribuible).
Misma carpeta de modelos y mismo ONNX (WeSpeaker ResNet34-LM) que el adaptador `diarize`, que sigue
disponible como alternativa (`diarize`, ~1,3 GB de torch).

## Método

1. **VAD**: `micnaga.pipeline.vad.detect_speech` (Silero ONNX) con `min_silence_ms=300`, `pad_ms=100`.
2. **Ventanas**: 1,5 s con salto de 0,75 s dentro de cada tramo de voz (un tramo corto da una sola
   ventana; si el último salto deja > 0,2 s sin cubrir se añade una ventana alineada al final).
3. **fbank en numpy**: 80 mel, 25/10 ms, hamming, preénfasis 0,97, quitar DC por trama, FFT 512, filtros
   triangulares kaldi (20 Hz–Nyquist), `log(max(x, eps))`, y CMN por ventana. Se calcula **una vez por tramo**
   y cada ventana es un corte de tramas (las ventanas empiezan en múltiplos de 10 ms, así que es idéntico a
   calcular el fbank del audio de la ventana; hay un test). Verificado contra `torchaudio.compliance.kaldi.fbank`:
   diferencia máxima 1,7e-4 en el fbank y **coseno 1,0000 entre embeddings** (`wespeakerruntime`, mismo audio;
   test con modelos `test_real_embedding_matches_wespeakerruntime_on_same_audio`, umbral 0,99).
4. **Embeddings**: ONNX por lotes de 32 ventanas, `intra_op_num_threads = processing.threads`.
5. **Clustering** (`scipy.cluster.hierarchy`): enlace medio, métrica coseno.
   * `n_speakers=None`: corte a distancia **0,60** (tope 8 hablantes) → fusión de clusters con coseno de
     centroides ≥ 0,55 (el enlace medio deja sueltas variantes de una misma voz; apareció un 4.º «hablante» en
     60 min sin este paso) → reasignación al centroide (k-means coseno, ≤ 3 iteraciones) → fusión de clusters
     con < 6 s de voz en el más parecido si su coseno ≥ 0,30 (un hablante pequeño pero distinto se conserva).
   * `n_speakers=k`: corte del árbol a `min(k, ventanas)` clusters, sin fusiones.
   * Más de 3000 ventanas: se agrupa una submuestra uniforme y el resto se asigna al centroide más cercano
     (la memoria de `linkage` es O(n²)).
6. **Turnos**: suavizado de ventanas aisladas, cada ventana «posee» desde el punto medio con la anterior
   hasta el punto medio con la siguiente (solapes resueltos por punto medio, sin solapes en la salida); en un
   **cambio de hablante** la frontera va al instante más silencioso (energía en tramas de 10 ms, media móvil de
   50 ms) entre los centros de las dos ventanas; fusión de segmentos contiguos del mismo hablante (hueco ≤ 0,5 s)
   y etiquetas `S1..Sn` por primera aparición.
7. **Post-filtro de la etapa 7** (`engines/diar/postfilter.py`, igual para todos los motores): un hablante con
   < 3 s de voz o un único turno de < 4 s se absorbe en el más parecido (coseno de `embed()`) o en el del turno
   más cercano; después la etapa 8 mueve los cambios de hablante a mitad de oración al fin de oración más
   cercano (≤ 0,6 s) y corta las frases en cada cambio de turno.
8. **`embed()`** (`SpeakerEmbedder`, MIC-11): media L2-normalizada de embeddings de ventanas de 3 s (hasta 20)
   sobre el audio concatenado de cada hablante, igual que el adaptador `diarize`: las huellas son
   intercambiables (mismo modelo, mismo fbank, mismo CMN por ventana).

## Evaluación (SOLO voz sintética)

Voces SAPI/OneCore de Windows: Helena (es-ES), Zira (en-US), Pablo y Laura (es-ES, OneCore, accesibles por
SAPI con su token). Diálogos con turnos de 1–3 frases, huecos 0,3–1,2 s, sin solapes, sin ruido
(`python -m tests.engines.diar_eval --engine onnx --minutes 5 --speakers 3`). DER con `pyannote.metrics`,
collar 0,25 s, `skip_overlap=False`. PC: i7-12700F (**no** el i5-1245U de referencia), 6 hilos configurados;
una ejecución por celda. «Pico» = pico del working set del proceso **incluyendo el audio del diálogo
en memoria** (60 min = 220 MB de float32; el motor añade ~400 MB sobre eso con onnx y ~1,2 GB con diarize).

`n_speakers=None` (ruta real del pipeline):

| Audio | Voces | Motor | Hablantes estimados | DER | Velocidad | Pico RAM |
| --- | --- | --- | --- | --- | --- | --- |
| 1 min | 2 | onnx | 2 | 2,8 % | 44× | 292 MB |
| 1 min | 2 | diarize | 2 | 4,6 % | 7,5× | 451 MB |
| 1 min | 3 | onnx | 3 | 2,7 % | 49× | 283 MB |
| 1 min | 3 | diarize | 3 | 3,1 % | 7,6× | 456 MB |
| 5 min | 2 | onnx | 2 | 2,1 % | 58–61× | 314 MB |
| 5 min | 2 | diarize | 2 | 5,5 % | 15,4× | 487 MB |
| 5 min | 3 | onnx | 3 | 2,1 % | 57–59× | 308 MB |
| 5 min | 3 | diarize | 3 | 1,9 % | 16,5× | 492 MB |
| 5 min | 4 | onnx | 4 | 1,2 % | 58–62× | 309 MB |
| 60 min | 3 | onnx | 3 | 1,9 % | 63× (57 s) | 684 MB |
| 60 min | 3 | diarize | 3 | 3,3 % | 26× (137 s) | 1530 MB |

`n_speakers` conocido (5 min, onnx): 2 voces DER 2,1 %, 3 voces DER 2,1 % (igual que el automático).

Los 60 min se ejecutaron una sola vez por motor y no se afinó sobre ellos (el único cambio posterior, la
fusión por centroides, vino de un diagnóstico a 60 min con el motor sin ella: un cluster de 28 s con coseno
0,73 respecto a otro; el resultado de 60 min de la tabla es el posterior al arreglo; los de 1 y 5 min no cambiaron).
El DER restante es sobre todo error de frontera (resolución de ventana 0,75 s, sin solapes ni ruido en los datos).

Calibración: en 5 min / 3 voces, el coseno entre ventanas de la misma voz tiene media 0,74 (p5 0,58) y entre voces
distintas media 0,23 (p95 0,37); las fusiones finales del árbol ocurren a distancia 0,72–0,80 y las internas
llegan a 0,48, de modo que 0,60 está en el centro de un hueco amplio.

## Caveats (leer antes de fiarse de los números)

* **Las voces TTS son irrealmente distintas y consistentes.** Con voces reales (misma sala, mismo micro,
  emociones, ruido, canal de videollamada comprimido) los coseno intra-hablante bajan y los inter-hablante suben.
  Los umbrales (0,60 / 0,55 / 0,30 / 6 s) son un punto de partida razonable, **no una calibración**:
  hay que recalibrar con el banco de reuniones reales (F2). Riesgos esperables: sobre-segmentación si la
  voz varía mucho (mitigada por la fusión por centroides y por clusters pequeños) y fusión de dos voces
  parecidas del mismo sexo y canal (subir el umbral de distancia a la baja).
* Un participante real que casi no habla (< 3 s en total, o una sola intervención de < 4 s) queda absorbido
  por otro hablante **siempre** (post-filtro de la etapa 7, a propósito: prioridad «ningún hablante fantasma»);
  dentro del motor, un cluster de < 6 s que se parece a otro (coseno ≥ 0,30) también.
* **No hay detección de solapes**: el habla simultánea se asigna a un solo hablante; las ventanas que
  cruzan un cambio de hablante tienen un embedding mezclado (error de frontera de hasta ~0,4 s).
* No se probó con idiomas distintos de es/en, ni con más de 4 voces, ni con audio de volumen muy bajo.
* Velocidad medida en un i7-12700F; en el i5-1245U de referencia espere bastante menos (el cómputo es el
  ONNX ResNet34, escala con hilos). El motor `diarize` en el mismo PC fue 7–26×.
* Memoria: a 60 min el motor aporta ~400 MB y `linkage` está acotado a 3000 puntos (~70 MB de distancias).
  A diferencia de `diarize`, no hay torch que no se pueda descargar del proceso.

## Comparativa de motores y post-filtro (2026-11-19, rama `fix/transcript-quality`)

Motivo: grabación real de 200 s (diálogo de 2 personas de un vídeo de YouTube por la pista del sistema; micro
callado) con un «Hablante 3» que solo decía «No» al final y varias líneas con las dos voces mezcladas.
Herramienta: `python -m tests.engines.diar_compare` (mismos casos para todos los motores, resultado crudo del motor
y tras el post-filtro de la etapa 7). PC i7-12700F, 6 hilos.

Casos: **tts2/tts3/tts4** = diálogos SAPI de 5 min con 2/3/4 voces (DER, collar 0,25 s); **mixto** = diálogo de 2
voces de ~1 min + 70 s de silencio + ráfaga de ruido de 0,5 s + 10 s de silencio (la referencia es solo el
diálogo); **podcast** = la grabación real (pista del sistema acondicionada por la etapa 4), **sin referencia**.
Celda: hablantes (crudo→tras post-filtro si cambia) · DER tras post-filtro.

| Motor | tts2 | tts3 | tts4 | mixto | podcast (real, 2 voces) | Velocidad (tts3, sin carga) |
| --- | --- | --- | --- | --- | --- | --- |
| `onnx` antes (sin post-filtro) | 2 · 2,1 % | 3 · 2,1 % | 4 · 1,2 % | 2 · 2,3 % | **3** (fantasma con 1,2 s) | 69× |
| **`onnx` + post-filtro (por defecto)** | **2 · 2,1 %** | **3 · 2,1 %** | **4 · 1,2 %** | **2 · 2,3 %** | **3→2** | **69×** |
| `diarize` (torch) + post-filtro | 2 · 2,4 % (crudo 5,5 %) | 3 · 1,5 % | 4 · 0,7 % | **1** · 36,7 % | 2 | 18× |
| sherpa-onnx CAM++ (umbral 0,5) | 3 · 33,6 % | 5 · 25,4 % | 7→6 · 6,1 % | 2 · 5,0 % | **15→7** | 18× |
| sherpa-onnx WeSpeaker R34-LM (0,5) | 2 · 23,3 % | 4 · 15,3 % | 4 · 4,7 % | 2 · 6,4 % | 7→4 | 13× |
| sherpa-onnx ERes2Net (0,5) | 3 · 51,1 % | 5 · 16,1 % | 6 · 10,4 % | 2 · 8,5 % | 13→9 | 9× |
| sherpa-onnx TitaNet small (0,5) | 3 · 49,2 % | 4 · 15,7 % | 6 · 17,0 % | 2 · 7,1 % | 14→9 | 18× |
| `pyannote` | no evaluable (pyannote.audio no instalado) | | | | | |

Barrido del umbral de sherpa-onnx (`FastClusteringConfig.threshold`; 0,5 es el valor por defecto que recomienda
sherpa-onnx; mayor = menos hablantes). Velocidades de este barrido no válidas (dos barridos en paralelo):

| Embeddings | Umbral | tts2 | tts3 | tts4 | mixto | podcast |
| --- | --- | --- | --- | --- | --- | --- |
| sherpa-3dspeaker-campplus-en | 0,5 | 3 · 33,6 % | 5 · 25,4 % | 7→6 · 6,1 % | 2 · 5,0 % | 15→7 |
| sherpa-3dspeaker-campplus-en | 0,7 | 3 · 33,5 % | 3 · 9,7 % | 4 · 5,9 % | 2 · 5,0 % | 7→4 |
| sherpa-3dspeaker-campplus-en | 0,8 | 2 · 20,6 % | 3 · 9,7 % | 4 · 5,9 % | 2 · 5,0 % | 6→3 |
| sherpa-3dspeaker-campplus-en | 0,9 | 2 · 20,6 % | 3 · 9,7 % | 3 · 19,3 % | 1 · 35,6 % | 4→2 |
| sherpa-3dspeaker-campplus-en | 1,0 | 2 · 20,6 % | 2 · 34,8 % | 3 · 19,3 % | 1 · 35,6 % | 3→2 |
| sherpa-3dspeaker-campplus-en | 1,1 | 2 · 20,6 % | 2 · 34,8 % | 2 · 45,1 % | 1 · 35,6 % | 3→2 |
| sherpa-wespeaker-resnet34-lm | 0,5 | 2 · 23,3 % | 4 · 15,3 % | 4 · 4,7 % | 2 · 6,4 % | 7→4 |
| sherpa-wespeaker-resnet34-lm | 0,7 | 2 · 23,3 % | 3 · 6,3 % | 4 · 4,7 % | 1 · 35,6 % | 4→2 |
| sherpa-wespeaker-resnet34-lm | 0,8 | 2 · 23,3 % | 2 · 36,4 % | 2 · 44,7 % | 1 · 35,6 % | 3→2 |
| sherpa-wespeaker-resnet34-lm | 0,9 | 1 · 51,6 % | 2 · 36,4 % | 1 · 71,1 % | 1 · 35,6 % | 2 |
| sherpa-wespeaker-resnet34-lm | 1,0 | 1 · 51,6 % | 1 · 66,7 % | 1 · 71,1 % | 1 · 35,6 % | 1 |
| sherpa-wespeaker-resnet34-lm | 1,1 | 1 · 51,6 % | 1 · 66,7 % | 1 · 71,1 % | 1 · 35,6 % | 1 |
| sherpa-3dspeaker-eres2net-en | 0,5 | 3 · 51,1 % | 5 · 16,1 % | 6 · 10,4 % | 2 · 8,5 % | 13→9 |
| sherpa-3dspeaker-eres2net-en | 0,7 | 2 · 30,3 % | 3 · 9,8 % | 4 · 6,5 % | 2 · 8,5 % | 6→3 |
| sherpa-3dspeaker-eres2net-en | 0,8 | 2 · 30,3 % | 3 · 9,8 % | 4 · 6,5 % | 2 · 8,5 % | 5→2 |
| sherpa-3dspeaker-eres2net-en | 0,9 | 2 · 30,3 % | 2 · 34,8 % | 4 · 6,5 % | 1 · 35,6 % | 4→3 |
| sherpa-3dspeaker-eres2net-en | 1,0 | 2 · 30,3 % | 2 · 34,8 % | 2 · 44,9 % | 1 · 35,6 % | 4→3 |
| sherpa-3dspeaker-eres2net-en | 1,1 | 2 · 30,3 % | 1 · 66,7 % | 1 · 71,1 % | 1 · 35,6 % | 2 |
| sherpa-nemo-titanet-small | 0,5 | 3 · 49,2 % | 4 · 15,7 % | 6 · 17,0 % | 2 · 7,1 % | 14→9 |
| sherpa-nemo-titanet-small | 0,7 | 2 · 30,6 % | 4 · 15,7 % | 5 · 18,6 % | 2 · 7,1 % | 7→3 |
| sherpa-nemo-titanet-small | 0,8 | 2 · 30,6 % | 3 · 7,6 % | 4 · 17,9 % | 2 · 7,1 % | 3→2 |
| sherpa-nemo-titanet-small | 0,9 | 2 · 30,6 % | 3 · 7,6 % | 3 · 31,4 % | 1 · 35,6 % | 3→2 |
| sherpa-nemo-titanet-small | 1,0 | 1 · 51,6 % | 2 · 36,4 % | 2 · 44,9 % | 1 · 35,6 % | 3→2 |
| sherpa-nemo-titanet-small | 1,1 | 1 · 51,6 % | 1 · 66,7 % | 1 · 71,1 % | 1 · 35,6 % | 3→2 |

Con `num_clusters=2` fijado, sherpa CAM++ sigue en 20,6 % de DER en tts2 (casi todo confusión): el error está en
la segmentación local (pyannote segmentation-3.0 no separa bien estas voces sintéticas), no en el clustering.

**Grabación real.** Sin referencia manual. Como indicador **aproximado** se etiquetó cada palabra con F0 (mediana
de autocorrelación) grave < 190 Hz ≤ aguda (una voz masculina y otra femenina) y se midió el acuerdo palabra a
palabra con asignación 1:1 (113–114 palabras con F0 fiable; el propio indicador tiene errores: palabras
enfáticas, «¡Hola, Marta!» aguda en la voz grave). Turnos solos: `onnx` antes 85,0 %, `onnx` con frontera en la
pausa 85,8 %, `diarize` 86,7 %, sherpa con el umbral ajustado a mano (CAM++ 0,9, ERes2Net 0,8, TitaNet 0,8,
WeSpeaker 0,7) 86,7 %, sherpa con su umbral por defecto (CAM++ 0,5) 51,3 %. Pipeline completo (palabras de la
etapa 8): antes 85,1 % con 3 hablantes remotos + «Yo»; ahora **88,6 % con 2 hablantes**.

**Decisión: `onnx` sigue por defecto** (DECISIONS.md §17.4). Es el más exacto en todos los casos con referencia
(DER ~2 %, 3–15 veces menos que sherpa-onnx con cualquier umbral) y el más rápido, y con el post-filtro da el
número correcto de hablantes en los cinco casos. sherpa-onnx con su configuración recomendada (0,5) sobre-segmenta
la grabación real (15 hablantes en bruto); con un umbral elegido a mano sobre estos mismos datos (ERes2Net o
TitaNet a 0,8) también acierta el número de hablantes en los cinco casos tras el post-filtro, pero con mucho más
DER y empatando con `onnx` en la grabación real. No se adopta con la evidencia actual (una sola grabación real y
voces sintéticas que pueden perjudicar a su segmentación): re-evaluar con reuniones reales. `diarize` junta las
dos voces del caso mixto (1 hablante) y necesita torch.

Notas: el caso mixto no produjo un fantasma con ningún motor (el silencio y la ráfaga no llegan a hablante); el
post-filtro se demuestra con la grabación real («No» de 0,65 s a los 3:09) y con los tests unitarios. Los
modelos de sherpa-onnx evaluados están fijados en `models.CANDIDATE_MANIFEST` (la app nunca los descarga).
