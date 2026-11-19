# Verificación MIC-01…MIC-16 (rama `fix/review-findings`, 2026-11-19)

Breve, a propósito: qué está comprobado y cómo. **Verificado** = test automático o prueba real
en este PC; **Parcial** = comprobado en condiciones más fáciles que el criterio; **Sin verificar**
= el criterio de aceptación no se ha medido.

| Req. | Estado | Evidencia |
| --- | --- | --- |
| MIC-01 Detección | Verificado (cambiado) | Ruta genérica, cualquier app (`tests/watcher/*`; E2E con la UI real y una fuente de señales guionizada «`fakecall.exe` tiene el micro», no el muestreador real). Meet en Chrome probado en real; Edge/Brave y «< 3 s» sin medir. El popup sale a los 20 s + 20 s por diseño (petición del usuario, DECISIONS §18) |
| MIC-02 Popup | Parcial | El fallo real del usuario («no sale») **no se reprodujo**; hay mitigaciones con tests (`tests/ui/test_host.py`). Ventana WebView2 real con `IsWindowVisible` + captura PrintWindow (`flow_02_popup.png`), con la sesión RDP desconectada: falta que el usuario lo vea en una pantalla activa. Tema claro/oscuro en `tests/ui/shots.py` |
| MIC-03 Dos pistas | Parcial | `tests/capture/*` (sincronía, relleno de silencios); desfase tras 1 h sin medir aquí |
| MIC-04 Compuertas | Cambiado | Por petición del usuario, el «Sí» decide y las compuertas de voz están a 0; lo que no pasa se aparta (no se borra). `tests/pipeline/test_gates.py` mantiene los umbrales v1 |
| MIC-05 Eco | Parcial | `docs/aec_comparison.md` (eco sintético); con altavoces reales sin medir |
| MIC-06 ASR | Parcial | WER 0 % en voz sintética limpia (`docs/engine_measurements.md`); falta set propio de reuniones |
| MIC-07 Diarización | Parcial | `docs/diarization_onnx.md` + post-filtro de hablantes fantasma; DER ≤ 20 % sin medir con reuniones etiquetadas |
| MIC-08 Cola | Verificado | Worker aparte con prioridad baja; supervisor/Finisher siguen al cambiar de carpeta (hallazgo 3, `tests/app/test_jobs_db_switch.py`); E2E real: 2 trabajos `done` |
| MIC-09 Recuperación | Verificado | `tests/capture/test_crash_recovery.py`; marca de «No» síncrona y aparta en el arranque (`tests/app/test_real_recorder.py`) |
| MIC-10 Pastilla | Verificado | Capturas `flow_01/04/06`; «¿Guardar?» en provisional; chip «Deshacer» (`flow_03`) |
| MIC-11 Huellas | Verificado | Cifradas con DPAPI, sin embeddings en claro en disco (hallazgo 4) |
| MIC-12 Marcadores | Verificado | `tests/app/*`, `tests/watcher/*` |
| MIC-13 Pausa privada | Verificado | Tests de captura (el tramo pausado no se escribe); el «No» usa la misma pausa |
| MIC-14 Biblioteca | Verificado | `tests/ui/test_bridge.py` (< 200 ms en la búsqueda de prueba; no con 500 h); sección «Descartadas» (`flow_08`) |
| MIC-15 Visor | Verificado | `tests/ui/*` (clic en frase → audio) |
| MIC-16 Glosario | Parcial | `tests/pipeline/*`; el «≥ 95 %» no se ha medido |

Otras comprobaciones pedidas:

* **Asistente `auto_finish`**: el host lo crea con `auto_finish=True`; al acabar la descarga sin
  fallos se cierra solo, con un fallo se queda abierto (`tests/ui/test_bridge.py::test_wizard_auto_finish_*`).
* **Exclusión de los exe propios**: `OWN_EXE_NAMES` = `micnaga.exe`, `micnaga-cli.exe`,
  `micnaga-host.exe`, los tres que construye `packaging/micnaga.spec`; además el intérprete propio por ruta.
* **Exclusión del pid propio**: `CaptureSessionMicSource` ignora `os.getpid()`
  (`tests/watcher/test_capture_sessions.py::test_own_process_and_excluded_apps_are_ignored`).
* **Salida**: solo «Salir» / fin de sesión / instalador; el resto se registra y recrea la GUI
  (`tests/ui/test_host_exit.py`; recreación probada también con WebView2 real: `flow_09_popup_after_gui_restart.png`).
