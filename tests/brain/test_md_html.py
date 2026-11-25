"""Markdown del «Acta completa» -> HTML para Drive (mismo subconjunto que la vista)."""

from __future__ import annotations

from micnaga.brain import md_html
from micnaga.brain.google import apply as gapply

SAMPLE = r"""# Acta: Demo <script>

**Fecha:** jueves 19 de noviembre
**Hora:** 17:08

## Orden del día

1. Uno
2. Dos

## 1. Tema

Texto con **negrita**, *cursiva* y `código` [01:25]–[01:48].
Sigue el mismo párrafo.

- Punto [09:30]
  - Subpunto
- Otro

> Una cita

## Acuerdos y tareas

| Responsable | Tarea | Fecha |
|---|---|---|
| Javier | Preparar checklist \| redes | Sin fecha |
| Manu | Base central |

[enlace](https://example.com/a?b=1&c=2) y [interno](javascript:alert(1))
"""


def test_blocks_and_escaping() -> None:
    out = md_html.markdown_to_html(SAMPLE)
    assert "<h1>Acta: Demo &lt;script&gt;</h1>" in out
    assert "<p><b>Fecha:</b> jueves 19 de noviembre<br><b>Hora:</b> 17:08</p>" in out
    assert "<ol><li>Uno</li><li>Dos</li></ol>" in out
    assert "<b>negrita</b>" in out and "<i>cursiva</i>" in out and "<code>código</code>" in out
    assert "[01:25]–[01:48]. Sigue el mismo párrafo.</p>" in out
    assert "<ul><li>Punto [09:30]<ul><li>Subpunto</li></ul></li><li>Otro</li></ul>" in out
    assert "<blockquote><p>Una cita</p></blockquote>" in out
    assert "<th>Responsable</th><th>Tarea</th><th>Fecha</th>" in out
    assert "<td>Preparar checklist | redes</td>" in out
    assert "<td>Base central</td><td></td></tr>" in out  # celdas que faltan, vacias
    assert '<a href="https://example.com/a?b=1&amp;c=2">enlace</a>' in out
    assert "javascript" not in out.split("interno")[0][-40:]  # solo http(s) se enlaza
    assert "<script>" not in out


def test_drive_uploads_the_full_acta_when_there_is_one() -> None:
    acta = {"titulo": "Breve", "resumen": ["Resumen corto"]}
    short = gapply.drive_input({"titulo": "Acta · X"}, acta)
    assert "Resumen corto" in short["textContent"]
    full = gapply.drive_input({"titulo": "Acta · X"}, acta, "## Orden del día\n\n1. Uno\n")
    assert (
        full["textContent"]
        == "<html><body><h2>Orden del día</h2><ol><li>Uno</li></ol></body></html>"
    )
    assert full["contentMimeType"] == "text/html"
    assert gapply.drive_timeout(full) == 120.0 + len(full["textContent"]) / 120.0
    assert gapply.drive_timeout({"textContent": "x" * 10**6}) == 480.0
