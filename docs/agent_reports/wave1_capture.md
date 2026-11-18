# Ola 1 — Captura F0 (resumen)

- `from micnaga.capture import Recorder, RecorderState, RecorderEvent, RecorderStatus, CaptureError`
- `Recorder(config, *, backend, clock_ns, on_event, ...)`: `start(session_dir, *, confirmed=False, meet_title, meet_code)`, `pause()`, `resume()`, `add_marker(label)`, `levels()` (RMS 0..1, sin bloqueo), `peaks()`, `elapsed_s()`, `stop()` (idempotente, sin FLAC), `set_confirmed()`, `set_meeting_info()`, `state`, `status`. De un solo uso.
- `RecorderEvent.kind`: state, device_change, source_error, source_recovered (desde hilos del grabador).
- Recuperación/FLAC: `recover_sessions(root)`, `finalize_session(session_dir, flac_level=5)`, `find_sessions(root, states)`. `capture.json extra.state`: recording → stopped → finalized, o recording → recovered. **Quien confirme o descarte debe llamar a `finalize_session`.**
- Sincronía: `micnaga.capture.sync.align_tracks(mic, system, meta)`; deriva 1,5 s/h corregida a < 10 ms en test.
- CLI: `micnaga record [--out DIR] [--seconds N] [--confirm] [--title T] [--no-finalize] [--synthetic]`, `micnaga record --recover`.
- Medida: `python -m micnaga.capture.measure [--seconds 30] [--real]`. Sintético: 2,2 % de un núcleo, 43 MB.

## Dispositivos reales
Desde la shell de los agentes PyAudioWPatch ve 0 dispositivos y COM no encuentra endpoints. Nada grabado. Pendiente que el usuario ejecute:
`$env:MICNAGA_RUN_REALDEVICE=1; .venv\Scripts\python -m pytest tests/capture/test_realdevice.py -s`

## Decisiones (para DECISIONS.md)
1. Huecos medidos entre llegadas (no contra reloj acumulado); umbrales 0,1 s sistema / 0,25 s micro; relleno de silencio en reposo hasta now−0,5 s.
2. Marcas de sincronía: en cada ventana de 10 s el paquete con menor `clock − samples/sr`; anclas en inicio, pausas y parada.
3. Pausa privada termina 0,3 s después de `resume()` (guarda) para no filtrar audio latente.
4. Flush cada 1,75 s con reescritura de cabecera y fsync (pérdida medida 1,3–1,6 s al matar).
5. FLAC nivel 5 = `compression_level 5/8`; conversión verificada bit a bit antes de borrar el WAV.
6. Recuperación: muestras por tamaño de fichero; dueño comprobado por pid + hora de creación.
7. Cambio de dispositivo: sondeo cada 250 ms; si no aparece, reinicia PortAudio.
8. Micro = captura de comunicaciones por nombre; loopback = salida por defecto (eConsole).
9. `output_kind`: speakers si alguna vez hubo altavoces; headphones solo si todo fue auriculares.
10. Bluetooth manos libres = auriculares (factor de forma o nombre).
11. PortAudio en modo callback, búferes de 100 ms.
12. Fuente caída → pista en ceros + reintento cada 1 s; si fallan ambas al inicio, `start()` falla y limpia.
13. RAM medida como memoria privada; `app.py` debe fijar `OPENBLAS_NUM_THREADS=1` antes de importar numpy.
