# Ola 2 — Integración (resumen)

- `micnaga app` → `micnaga.app:run` (ligero; BLAS a 1 hilo; mutex de instancia única; `--exit-after S`).
- `micnaga/integration/`: `controller.py` (AppController = WatcherController), `jobs.py` (Finisher: FLAC + encolar; WorkerSupervisor), `events_server.py` (tubería), `library_ops.py`, `recovery.py`, `levels.py`, `application.py`.
- Cuenta atrás única: la del vigilante. UI con timeout de 24 h; `PopupApi.popup_state() -> {remaining_s, total_s, paused}`; JS debe definir `micnaga.popup.expire()` (fallback Python 0,8 s).
- Worker lanzado con `--once` (proceso nuevo por lote → libera torch).
- HKCU Run solo al terminar el asistente de primer arranque (`MICNAGA_NO_AUTOSTART=1` lo evita).
- Cambios fuera de su zona: `Database.set_session_dir`; `popup_frozen` en el vigilante; `host.py` (popup_state, expire_popup, show_pill idempotente); `bridge.py` (`popup_state`); `tray.py` (bug real: el menú lanzaba ValueError y mataba la bandeja).

## Medidas (i7-12700F, NO el equipo de referencia)
- Arranque a bandeja: 1,46–1,84 s.
- E2E con modelos reales sobre TTS (Helena «Yo» + Zira remota, 215 s): WER «Yo» 0 %, 0/21 frases remotas como «Yo»; factor 0,17–0,19; pico ~1,0 GB RSS / 1,4 GB privada. Nota de voz de 2 min → descartada. No hubo control sin AEC.

## Huecos conocidos
- Cambios de detección/motores en Ajustes se aplican al reiniciar (atajos sí en caliente).
- Si `Recorder.start` falla (sin dispositivos), el usuario no ve mensaje, solo log.
- Ctrl+Alt+M está ocupado por otra app en este PC.
- Sin verificar: transparencia/animaciones reales, foco sobre Meet real, toast real, RAM de WebView2.
