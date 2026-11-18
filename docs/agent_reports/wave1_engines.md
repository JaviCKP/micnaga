# Ola 1 — Motores, VAD, AEC, modelos (resumen)

- ASR Parakeet (`onnx-asr`, int8, local): 15–19× tiempo real en i7-12700F, pico 1,24 GB. Whisper small: ~5×, 0,79 GB. Silero VAD ONNX sin torch: ~250×. Diarize: 12–19×, pico 1,28 GB en 60 min (torch no se libera en `close()`; para devolver memoria, etapa 7 en subproceso).
- WER en TTS español (SAPI Helena, NO es el set real): Parakeet 0 %, Whisper 0 %.
- Diarización automática (`n_speakers=None`) sobresegmenta; mitigado (tope 8, fusión por coseno ≥ 0,55, absorción < 10 s). 33 s y 5 min → 2 hablantes; 60 min en mosaico → 4. Umbrales sin calibrar con voces reales.
- AEC ganador: **Speex (pyaec)** 256 ms + alineación de retardo GCC-PHAT + preprocesador; conserva la voz propia en doble habla (AEC3 la suprime). `AUTO_ORDER=("speex","webrtc")`. Detalle: `docs/aec_comparison.md`. Solo escenarios sintéticos.
- Modelos: manifiesto en `micnaga/models.py`, URLs de Hugging Face con commit fijado, SHA-256 calculados. Por defecto 699 MB (Parakeet, Silero, WeSpeaker); Whisper small +486 MB opcional. Descarga solo https a huggingface.co / *.hf.co. WeSpeaker se redirige a `<models>/wespeaker` (nunca `~/.wespeaker`).
- pyannote: adaptador escrito, NO verificado (no instalado).
- Licencias para THIRD_PARTY_LICENSES: Parakeet CC-BY-4.0; WeSpeaker voxceleb ONNX CC-BY-4.0 (código Apache-2.0); Silero MIT; faster-whisper/Systran MIT; pyaec MIT + SpeexDSP BSD-3.

## Decisiones
Manifiesto en código; `max_speakers` 8 en automático; Whisper medium fuera; AEC interno a 16 kHz; preprocesador Speex activo; fin de palabra tope 0,32 s tras el último token.
