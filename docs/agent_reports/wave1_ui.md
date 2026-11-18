# Ola 1 — Interfaz (resumen)

- `micnaga/ui/host.py: UiHost(config, callbacks=UiCallbacks(...), levels, library_ops, download_models, diagnostics, config_store, registry, backdrop="solid", tray=True)`.
- `run(on_ready)` bloquea el hilo principal (pywebview); `on_ready` corre en un hilo auxiliar → arrancar ahí vigilante, captura, cola. Bandeja con `pystray.run_detached()`.
- Métodos: `show_popup(title, code, monitor_hint, *, timeout_s, on_timeout)`, `hide_popup()`, `accept_popup()`, `dismiss_popup()`, `show_pill(*, elapsed_s, provisional, monitor_hint)`, `hide_pill()`, `set_pill_paused`, `set_pill_provisional`, `sync_pill`, `set_levels_provider(fn)`, `set_processing`, `set_watching_paused`, `notify_ready(folder, title, duration_s, speakers)`, `open_library(folder, seek_s)`, `open_settings()`, `open_wizard()`, `apply_config(cfg)`, `quit()`.
- Callbacks `UiCallbacks`: `on_popup_decision("record"|"dismiss"|"timeout")`, `on_popup_hover`, `on_marker`, `on_pause_toggle(bool)`, `on_stop`, `on_record_now`, `on_pause_watching(60)`, `on_open_meeting(folder)`, `on_settings_saved`, `on_first_run_done`, `on_levels_monitor(bool)`, `on_pill_moved`.
- Protocolos en `micnaga/ui/contracts.py`: `LibraryOps` (rename_speaker, merge_speakers, regenerate_from_json, list/save/delete_voiceprint), `download_models(progress_cb)` con `ModelProgress`, proveedor de diagnóstico.
- Tras «Grabar» o timeout, el host convierte el popup en pastilla por sí mismo.
- Servidor local `media.py` en 127.0.0.1 con token por proceso (páginas + FLAC con Range).
- `startup.py` es el único que escribe `HKCU\...\Run`; se niega bajo pytest o `MICNAGA_UI_DEMO=1`.
- Demo: `python -m micnaga.ui.demo popup|pill|library|settings|wizard|toast|tray`.

## CONFLICTO a resolver en integración
La UI tiene su propia cuenta atrás del popup (JS, 15 s, pausa con hover) y avisa `on_popup_decision("timeout")`; el vigilante también lleva la suya (autoritativa, se congela al perder señal o en pausa privada). Debe haber UNA: el vigilante manda; la UI solo muestra `Watcher.popup_remaining_s` y cierra con `close_popup`.

## Decisiones (para DECISIONS.md)
- Sin sombra exterior en popup/pastilla (ventana justa); anillo interior de 1 px. Pastilla siempre oscura.
- Popup arriba a la derecha del área de trabajo del monitor del navegador (margen 24 px); pastilla arriba al centro.
- Con `WS_EX_NOACTIVATE`, Enter/Esc solo llegan si el popup tiene foco; clic en la tarjeta lo activa temporalmente.
- «No molestar» es por aproximación (`SHQueryUserNotificationState`).
- Audio de la biblioteca: dos `<audio>` sincronizados (no WebAudio), corrección de deriva cada 400 ms.
- Toast con el AUMID de cmd.exe; el instalador debe crear acceso directo con AppUserModelID propio.
- Mica desactivado por defecto (no verificado visualmente).
- Guardar huella exige opción activada + casilla de consentimiento + nombre, nunca «Yo».
- Coste de WebView2: cada ventana oculta añade un proceso de renderizado; comprobar contra 150 MB.

## No verificado
Transparencia y esquinas reales, animación de vuelo, Mica, no robar foco frente a Meet real, toast real, clics del menú de bandeja.
