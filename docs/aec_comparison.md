# Comparativa de cancelación de eco: WebRTC AEC3 frente a SpeexDSP (MIC-05, spec F1)

**Resultado: gana SpeexDSP (`pyaec`) con filtro de 256 ms, alineación automática del retardo y
preprocesador activado.** Es lo que devuelve `micnaga.pipeline.aec.speex.create()` y va primero en
`AUTO_ORDER` (`("speex", "webrtc")`). WebRTC AEC3 (`aec-audio-processing`) sigue disponible con
`processing.aec = "webrtc"`.

> **Aviso importante.** Todo esto es **sintético**: voces SAPI de Windows (Helena es-ES como voz
> cercana, Zira en-US como lejana) y voces armónicas de `tests/synth.py`, convolucionadas con
> **respuestas al impulso de sala sintéticas** (pico directo + 4 reflexiones + cola de ruido con
> decaimiento exponencial). Una RIR sintética no es una sala real: sirve para comparar motores entre
> sí en igualdad de condiciones, no para prometer una cifra en una oficina. La comparativa debe
> repetirse con las grabaciones reales con altavoces del set de prueba (spec §12) cuando existan
> (`python -m tests.engines.aec_compare` es el punto de partida; `micnaga.bench` debería absorberlo).

## Qué se mide

Escenario de 60 s a 16 kHz (`tests/engines/aec_eval.py`): 0–15 s solo lejano; 15–25 s doble
conversación; 25–35 s solo cercano; 35–50 s solo lejano; 50–60 s doble conversación. El eco entra al
micro con retardos de **30, 90 y 150 ms** (y RT60 0,15 / 0,3 / 0,4 s), a nivel igual a la voz cercana
(0 dB), más una variante con altavoz saturado (`tanh`, alinealidad) y ruido de fondo de −75 dBFS.

| Métrica | Qué es |
| --- | --- |
| ERLE | 10·log10(potencia del micro / potencia de la salida) en los tramos solo-lejano (0–5 s = convergencia, 5–15 s, 35–50 s = ya convergido). Más alto = más eco quitado. |
| DT SDR / DT ganancia | En doble conversación: se ajusta un FIR no causal (65 coeficientes) `salida ≈ h * cercano`. **Ganancia** = potencia de `h*cercano` frente a la de la voz cercana (0 dB = intacta; −10 dB = el AEC se la come). **SDR** = lo que no se explica linealmente (eco residual + artefactos no lineales). Un retardo de 1 muestra o un filtrado de color no cuentan como error. |
| Solo cercano SDR | Lo mismo con la referencia en silencio (no debería tocar la voz limpia). |
| ASR: palabras filtradas | Parakeet transcribe el micro tras el AEC en los 15 s solo-lejano (el micro solo contiene eco): palabras que salen. Sin AEC salen ~40. **Es el «Yo» fantasma de MIC-05** (el segundo filtro, etapa 9, borra frases de «Yo» parecidas al remoto, pero la palabra suelta no siempre). |
| ASR: WER relativo | WER de la transcripción de la salida en doble conversación frente a la de la voz cercana **limpia** (incluye palabras del lejano que se cuelan y palabras del cercano que el AEC borra). Es lo que realmente importa para «Yo». |
| x RT | Veces tiempo real en este PC (un solo hilo). |

## Resultados

Medias sobre los escenarios indicados (una semilla de RIR por escenario).

### Voces SAPI (4 escenarios: retardos 30/90/150 ms + altavoz saturado), eco a 0 dB

| Candidato | ERLE 0–5 s | ERLE 5–15 s | ERLE 35–50 s | DT SDR | DT ganancia | Solo cercano SDR | x RT |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| sin AEC | 0,0 | 0,0 | 0,0 | −0,7 | −0,1 | 54,3 | – |
| webrtc (por defecto del paquete) | 25,7 | 35,6 | 42,6 | −3,9 | **−12,3** | 20,9 | 26 |
| webrtc + alineación | 29,3 | 51,7 | 55,7 | −2,7 | **−10,9** | 20,9 | 19 |
| webrtc + alineación + calentamiento 5 s | 52,1 | 55,6 | 55,8 | −2,6 | −10,7 | 20,9 | 24 |
| speex 128 ms | 7,6 | 12,8 | 13,7 | 7,0 | −4,6 | 28,7 | 50 |
| speex 128 ms + alineación | 11,6 | 21,2 | 23,1 | 15,9 | −4,6 | 28,7 | 44 |
| speex 256 ms + alineación | 12,1 | 23,7 | 28,3 | 20,1 | −4,6 | 28,7 | 32 |
| speex 128 ms + alineación + preproc. | 15,1 | 25,2 | 29,7 | 14,5 | −5,6 | 24,0 | 37 |
| **speex 256 ms + alineación + preproc. (elegido)** | 14,6 | 29,4 | 37,9 | 15,8 | −5,6 | 23,8 | 29 |

### Voces armónicas sintéticas (3 escenarios), eco a 0 dB

| Candidato | ERLE 35–50 s | DT SDR | DT ganancia | Solo cercano SDR |
| --- | ---: | ---: | ---: | ---: |
| webrtc + alineación | 34,5 | −2,3 | −9,6 | 26,1 |
| speex 256 ms + alineación + preproc. | 30,3 | 9,1 | −8,5 | 21,4 |
| speex 256 ms + alineación | 25,8 | 13,8 | −5,1 | 31,7 |

(Las voces armónicas tienen su energía en la fundamental, 110–190 Hz, justo donde Speex atenúa; por eso
penalizan más a Speex que la voz SAPI. Quedan como segunda opinión, no como referencia.)

### Nivel de ASR (voces SAPI, 4 escenarios; sin AEC salen ~40 palabras de eco en 15 s)

Palabras filtradas del lejano / WER relativo del cercano en doble conversación (0 = idéntico a la
transcripción de la voz limpia; 1 = todo mal). Sin AEC el WER relativo sube con el nivel del eco
(0,11 → 0,30 → 0,94) porque el eco tapa al hablante cercano.

| Candidato | Eco −10 dB | Eco 0 dB | Eco +6 dB |
| --- | --- | --- | --- |
| sin AEC | 40 / 0,11 | 41 / 0,30 | 40 / 0,94 |
| webrtc | 0 / **0,35** | 0 / **0,8** | 0 / **0,93** |
| webrtc + alineación | 0 / **0,26** | 0 / **0,7** | 0 / **0,86** |
| speex 256 + alineación | 6,5 / 0,00 | 10 / 0,00 | 10 / 0,02 |
| **speex 256 + alineación + preproc.** | 4 / 0,01 | 1,8 / 0,00 | 0 / 0,02 |

Por retardo (eco 0 dB, speex 256 + alineación + preproc.): palabras filtradas 7 (30 ms), 0 (90 ms),
0 (150 ms); WER relativo 0,0 en los tres. Con el filtro de 128 ms el retardo de 150 ms deja pasar
las 41 palabras (la cola del filtro no llega), aun con alineación: por eso 256 ms.

## Qué dicen los números

1. **AEC3 quita más eco, pero se come al hablante cercano.** ERLE de 40–60 dB frente a 25–40 dB de
   Speex, y cero palabras filtradas. Pero en doble conversación atenúa la voz cercana 5–17 dB (y el
   WER relativo sube a 0,3–0,9: a +6 dB de eco el resultado es *igual de malo que no hacer nada*).
   Speex la deja casi intacta (−5 dB de ganancia, casi todo por la atenuación de graves de la DLL: medido −10 dB entre 100 y 300 Hz con la referencia en silencio) y el WER
   relativo es ≈ 0. Un eco residual lo puede limpiar la etapa 9 (deduplicado por texto contra el
   remoto); **una frase de «Yo» que el cancelador borra no se recupera nunca**. Por eso el criterio
   de elección pesa la doble conversación por encima del ERLE.
2. **El retardo bruto manda.** El eco real llega con 30–150 ms de retardo (acústica + buffers de
   audio; más con Bluetooth). Speex solo cancela lo que cabe en su cola, y AEC3 converge mucho mejor
   si se le acerca la referencia: estimar el retardo con GCC-PHAT y retrasar la referencia
   (`auto_align=True`, margen 20 ms) sube el ERLE de AEC3 de 30 a 56 dB a 90 ms y el de Speex 128 ms
   de 9 a 21 dB. La estimación devuelve 0 (no retrasa) si el pico no destaca (z < 7) para no
   desalinear un eco débil.
3. **El preprocesador de Speex compensa su convergencia lenta** (ERLE +9 dB, palabras filtradas de
   10 a 1,8) a cambio de ~7 dB de fidelidad en la voz limpia (SDR solo-cercano 29 → 24 dB). Para
   ASR el coste es nulo en estas pruebas (WER relativo 0,00).
4. **Velocidad:** ambos van ≥ 19× tiempo real en un hilo (1 h de reunión ≈ 2–3 min); no decide.
5. **Calentamiento previo de AEC3** (pasar 5 s antes de empezar): arregla su convergencia inicial
   (ERLE 0–5 s de 29 a 52 dB) pero no su problema en doble conversación.
6. **Hallazgos de integración** (verificados): la salida de AEC3 va 128 muestras (8 ms) retrasada y
   la del preprocesador de Speex 160 (una trama); los adaptadores lo compensan para que `salida[i]`
   corresponda a `mic[i]` (un desfase de 1 muestra ya cuenta como «distorsión» en un SDR ingenuo y
   desplaza las marcas de tiempo de palabra). AEC3 se ejecuta siempre a 16 kHz (a 32/48 kHz su
   banco de filtros añade retardos distintos).

## Reservas

* **Sala sintética.** AEC3 es el estándar de la industria (Chrome/Meet) y en salas reales con
  doble conversación suele conservar mejor la voz cercana de lo que muestra esta prueba; la cola de
  ruido de mi RIR (estacionaria, 12 % de la energía directa) es el tipo de reverberación tardía que
  su supresor residual castiga más. No hay forma de ajustarlo con el binding (`aec-audio-processing`
  1.0.1 solo expone `enable_aec/ns/agc/vad`). Hay que **repetir con las grabaciones reales con
  altavoces** y, si AEC3 gana allí, basta cambiar `AUTO_ORDER` (una línea) o `processing.aec`.
* **Una semilla de RIR** por escenario; las diferencias de unos pocos dB entre variantes de Speex no
  son concluyentes. Las diferencias AEC3 / Speex en doble conversación (>10 dB, WER relativo
  0,3–0,9 frente a 0,0) sí lo son dentro de esta simulación.
* **Voz cercana y lejana ambas femeninas y continuas** (TTS casi no hace pausas): doble conversación
  casi permanente. Una reunión real tiene mucho menos solapamiento.
* **Licencia de `pyaec`:** el wheel no declara licencia; su repositorio de origen
  (`thewh1teagle/aec`, que enlaza `aec-rs`) es **MIT** y la DLL contiene SpeexDSP (BSD-3): compatible
  con MIT. Anotar ambos en `THIRD_PARTY_LICENSES.md`. `aec-audio-processing` es BSD-3.

## Cómo reproducir

```powershell
$env:MICNAGA_MODELS = "<repo>\.models"        # solo para --asr (Parakeet)
.venv\Scripts\python.exe -m tests.engines.aec_compare --asr [--echo-db -10] [--tts-only] `
    [--only webrtc+align,speex256+align+pre] [--json salida.json]
```

Los tests rápidos de `tests/engines/test_aec.py` cubren troceado en tramas, longitudes, latencia
compensada, estimación de retardo y reducción de eco; `test_models_real.py::test_real_aec_on_tts_echo`
(con modelos) repite un escenario con voz SAPI y exige ERLE > 20 dB a ambos motores.
