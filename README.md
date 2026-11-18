# Micnaga

App de Windows, de código abierto y uso personal, que detecta reuniones de Google Meet, graba tu micro y el
audio del sistema en pistas separadas y, al terminar, genera una transcripción con hablantes identificados.
Todo en local y en CPU: ningún audio ni texto sale del equipo.

Estado: **en construcción** (cimientos). Especificación completa en [`docs/SPEC.md`](docs/SPEC.md);
decisiones y resultados de instalación en [`DECISIONS.md`](DECISIONS.md).

## Desarrollo

```powershell
uv pip install --python .venv\Scripts\python.exe -e ".[asr,diar,aec,ui,dev]"
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy --strict micnaga
.venv\Scripts\python.exe -m pytest -q
```

Variables de entorno para no tocar carpetas reales (los tests las fijan solas):

| Variable | Qué es | Por defecto |
| --- | --- | --- |
| `MICNAGA_HOME` | datos de la app (config, logs, huellas) | `%APPDATA%\Micnaga` |
| `MICNAGA_STORAGE` | reuniones y `micnaga.db` | `~/Documents/Micnaga` |
| `MICNAGA_MODELS` | modelos descargados | `<datos de la app>\models` |

Tests marcados: `MICNAGA_RUN_MODELS=1` (modelos), `MICNAGA_RUN_SLOW=1`, `MICNAGA_RUN_REALDEVICE=1` (micro/altavoces reales).

## Licencias y atribuciones

Código de Micnaga: [MIT](LICENSE).

Modelos (se descargan en el primer arranque, no se redistribuyen con el código):

* **NVIDIA Parakeet TDT 0.6B v3** — licencia CC-BY-4.0. Atribución: © NVIDIA, modelo
  `nvidia/parakeet-tdt-0.6b-v3` (conversión ONNX int8 según el origen indicado en la descarga).
* **pyannote speaker-diarization community-1** (alternativa opcional) — licencia CC-BY-4.0. Atribución:
  pyannote.audio / pyannoteAI.

La lista completa de licencias de dependencias irá en `THIRD_PARTY_LICENSES.md`.

## Aviso de uso

Grabar a otras personas y guardar huellas de su voz exige avisarlas y respetar las normas de tu organización
(RGPD). Micnaga muestra siempre una pastilla de grabación y recuerda avisar a los asistentes.
