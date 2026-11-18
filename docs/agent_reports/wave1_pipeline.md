# Ola 1 — Pipeline, cola, salidas, compuertas, glosario, huellas, bench (resumen)

- Etapas en `micnaga/pipeline/stages/s01_gates.py … s11_assemble.py`; marcador `work/NN_name.done.json`; artefactos atómicos. Reanudación probada con caída antes de cada etapa.
- `PipelineRunner(config, deps, db, emit, keep_work, rename, on_stage_start).run(session, job)`; `Deps` inyectables; `MICNAGA_FAKE_ENGINES=1` usa `micnaga.bench.fakes`.
- Worker: `micnaga worker [--once]`, prioridad BELOW_NORMAL, mutex singleton, `requeue_stale`, eventos por tubería (descarta si nadie escucha). `spawn_worker(extra_args, env)` para la app.
- `micnaga process <dir>`: 0 hecho, 3 descartado, 1 fallo.
- Salidas: `micnaga/pipeline/output.py` — `regenerate_from_json`, `rename_speaker(folder, id, name, learn_voiceprint=False)`, `merge_speakers(folder, keep, merge)`, `read_transcript`, `index_meeting`.
- Compuertas: `micnaga/gates.py` (`evaluate_gates`, `compute_gate_metrics`…). Cambio de turno: piezas «yo»/«remoto» en línea temporal conjunta, ignorando < 0,3 s, eco absorbido ensanchando 0,3 s las regiones del sistema.
- Glosario `micnaga/glossary.py` (`<app data>/glossary.txt`, formato `Canónico: variante, variante`); huellas `micnaga/store/voiceprints.py` (DPAPI).
- Bench: `micnaga bench [--synthetic] [--testset DIR]`; formato del set en `docs/testset_format.md`.

## Peticiones de cambios compartidos
1. `Database.set_session_dir(job_id, path)` (ahora hace UPDATE en crudo).
2. `capture.json` `mic.file/system.file` quedan obsoletos tras convertir a FLAC: el visor debe usar las constantes `AUDIO_*_FLAC`.
3. El vigilante puede importar `micnaga.gates`.
4. Enganche en la app para arrancar el worker (`spawn_worker`).

## Decisiones (para DECISIONS.md)
- `last_run.json` del bench en `<app data>/bench/`.
- Etapa 2 rellena la pista corta y avisa si el desfase > 50 ms (no falla).
- Sin AEC disponible o `aec=off`: se salta la etapa 3. Sin diarizador: un solo `S1`.
- AEC también si algún cambio de dispositivo fue a no-auriculares.
- Frases: corte por cambio de hablante, hueco ≥ 0,8 s o puntuación final. Párrafos: < 1,5 s mismo hablante; corte > 45 s o 700 caracteres.
- Eco 2.ª capa: frase «Yo» vs frases remotas solapadas ±0,5 s y palabras remotas de la ventana (ratio y partial_ratio si ≥ 12 caracteres).
- Marcadores en MD resaltados con `==…==`; minutos siguen contando pasada la hora.
- Worker singleton por mutex con nombre. Etapa 4 filtra en bloques de 60 s (1 h pasó de 2,08 GB a ~1,0 GB de pico).
- Etapa 6 sin checkpoint interno: una caída a mitad repite la etapa.
