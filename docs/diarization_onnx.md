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
   hasta el punto medio con la siguiente (solapes resueltos por punto medio, sin solapes en la salida),
   fusión de segmentos contiguos del mismo hablante (hueco ≤ 0,5 s) y etiquetas `S1..Sn` por primera aparición.
7. **`embed()`** (`SpeakerEmbedder`, MIC-11): media L2-normalizada de embeddings de ventanas de 3 s (hasta 20)
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
* Un participante real que casi no habla (< 6 s) y suena parecido a otro (coseno ≥ 0,30) queda absorbido.
* **No hay detección de solapes**: el habla simultánea se asigna a un solo hablante; las ventanas que
  cruzan un cambio de hablante tienen un embedding mezclado (error de frontera de hasta ~0,4 s).
* No se probó con idiomas distintos de es/en, ni con más de 4 voces, ni con audio de volumen muy bajo.
* Velocidad medida en un i7-12700F; en el i5-1245U de referencia espere bastante menos (el cómputo es el
  ONNX ResNet34, escala con hilos). El motor `diarize` en el mismo PC fue 7–26×.
* Memoria: a 60 min el motor aporta ~400 MB y `linkage` está acotado a 3000 puntos (~70 MB de distancias).
  A diferencia de `diarize`, no hay torch que no se pueda descargar del proceso.
