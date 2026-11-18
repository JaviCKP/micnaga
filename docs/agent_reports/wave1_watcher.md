# Ola 1 — Vigilante y atajos (resumen del informe del agente)

- Código: `micnaga/watcher/` (model, titles, sources, registry_mic, meet_tabs, winapi, audio_out, sampler, machine, controller, watcher), `micnaga/hotkeys.py`. Diagrama: `docs/state_machine.md`.
- Máquina pura `WatcherStateMachine.step(event, now) -> list[Action]`; fachada `Watcher` con hilo de sondeo 1 s (0,25 s con popup).
- Controlador a implementar por la app: `micnaga.watcher.WatcherController` — `start_recording(*, provisional, meeting)`, `show_popup(*, title, code, monitor_hint, timeout_s)`, `close_popup(*, reason)`, `confirm_recording()`, `discard_recording()`, `stop_and_finalize(*, confirmed, meeting)`, `show_pill(*, provisional, paused)`, `hide_pill()`, `pause_recording()`, `resume_recording()`.
- Entradas al Watcher: `popup_answer(accept)`, `popup_hover(bool)`, `record_now()`, `user_stop()`, `toggle_private_pause()`, `set_private_pause(bool)`, `suspend(duration_s=3600)`, `resume_watching()`; `diagnostics()`, `on_state_change`, `popup_remaining_s` (autoritativo).
- Coste medido (i7-12700F): reposo 0,026 % de un núcleo; peor caso forzado 0,29 % de un núcleo.
- Hotkeys: `HotkeyManager.from_config(cfg.hotkeys, marker=..., private_pause=...)`, `start()` devuelve `HotkeyResult` por atajo (ocupado = reportado, no excepción).

## Decisiones (para DECISIONS.md)
1. Popup: §2 (< 3 s) choca con §5 (5 s estables). Se usa `detection.confirm_after_s` (5 s ⇒ ~5–6 s). Para cumplir < 3 s, poner 2.
2. Timeout del popup y fin de reunión valen ambos 15 s: la cuenta atrás del popup se congela mientras falte una señal obligatoria (y con hover o pausa privada).
3. Fin a los 15 s desde el primer tick sin señal; un corte de 10 s no termina.
4. Micro y pestaña Meet deben ser del mismo navegador.
5. Reunión sin código: clave de ignorado `nocode:<navegador>`; títulos solo de títulos de ventana.
6. La sesión pertenece a su código; otra pestaña Meet no la mantiene viva.
7. Parar a mano ignora las reuniones vivas para no volver a preguntar; con `on_timeout=discard` también se ignora.
8. «Grabar ahora» adopta la reunión visible si la hay; si no, sesión manual que solo termina con parar.
9. Suspender no corta una grabación; aplica al terminar.
10. Entrada de micro «colgada»: se exige proceso vivo con ese exe.
11. Pausa privada durante el popup: permitida, congela la cuenta atrás.

## No verificado aquí
Brave; una llamada real (si silenciar Meet libera el micro, 15 s silenciado termina la sesión); señal de audio saliente (sin endpoint en este PC).

## Seguimiento: vía genérica (cualquier app con micro) — pedida por el usuario
- `[detection]`: `generic_apps=true`, `mic_grace_s=20`, `popup_after_recording_s=20`, `exclude_apps=[]`.
- Lee también apps empaquetadas (`ConsentStore\microphone\<PackageFamilyName>`). Excluye el propio intérprete/`micnaga.exe`. «En uso» exige proceso vivo creado antes de `LastUsedTimeStart`.
- Nuevo estado `RECORDING_PRE_POPUP`. Grabación provisional a los 20 s de micro; popup 20 s después. Meet sigue por la vía rápida (5 s) y una sesión genérica del navegador se «asciende» a Meet.
- Controlador: `show_pill(provisional=True)` ANTES del popup en la vía genérica (popup y pastilla a la vez); tras timeout genérico no hay `show_pill` nuevo; `hide_pill()` puede seguir a `discard_recording()`; en genérico `show_popup(code=None, monitor_hint=None)` → la UI usa monitor principal o ventana en primer plano.
- Decisiones: «Ahora no» ignora `app:<clave>` hasta 15 s de micro liberado; reunión genérica que acaba con popup abierto → provisional (compuertas); `user_stop` ignora todas las apps con micro; con varias apps gana la de uso continuo más largo; «Grabar ahora» sin Meet no adopta apps.
- Coste en reposo: 0,234 % de un núcleo.
- Limitación: apps de dictado (p. ej. handy.exe) arrancan grabación provisional; las compuertas la descartan; se pueden excluir.
