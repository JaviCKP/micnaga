"""Automejora: validacion local, duplicados, contradicciones, historial y consolidacion."""

from __future__ import annotations

from typing import Any

import pytest

from micnaga.brain import learn


def item(fact: str, conf: float = 0.9, source: str = "reunion") -> dict[str, Any]:
    return {"hecho": fact, "fuente": source, "evidencia": "#1", "confianza": conf}


@pytest.mark.parametrize(
    ("fact", "reason"),
    [
        ("Carlos usa carlos@acme.es para todo", "email"),
        ("El móvil de recepción es 600 11 22 33", "phone"),
        ("El presupuesto de Triana es de 6.900 €", "amount"),
        ("La clave es sk-ant-abc123", "secret"),
        ("corto", "length"),
    ],
)
def test_personal_data_amounts_and_secrets_are_rejected(fact: str, reason: str) -> None:
    assert learn.reject_reason(fact) == reason
    out = learn.add_learnings([item(fact)])
    assert out.added == [] and out.rejected == [(fact, reason)]
    assert not learn.learned_path().exists()


def test_add_dedupe_contradiction_forget_and_restore() -> None:
    first = learn.add_learnings(
        [item("Las oportunidades se titulan «Empresa · servicio».", source="crm")],
        today="2026-11-19",
    )
    assert [x.line() for x in first.added] == [
        "- [2026-11-19 · crm] Las oportunidades se titulan «Empresa · servicio»."
    ]
    dup = learn.add_learnings([item("Las oportunidades se titulan «Empresa · servicio»")])
    assert dup.rejected[0][1] == "duplicate"
    contra = learn.add_learnings(
        [item("Las oportunidades no se titulan «Empresa · servicio».")], today="2026-11-20"
    )
    assert contra.added[0].review is True
    low = learn.add_learnings([item("Los martes hay reunión de equipo comercial", conf=0.2)])
    assert low.rejected[0][1] == "low_confidence"
    items = learn.list_learned()
    assert [i.review for i in items] == [False, True]
    ctx = learn.load_context()
    assert "Empresa · servicio" in ctx and "no se titulan" not in ctx  # lo dudoso no entra
    assert "contexto base" in ctx.lower()
    versions = learn.history_versions()
    assert versions  # cada cambio deja copia
    assert learn.forget(1) and len(learn.list_learned()) == 1
    assert not learn.forget(9)
    assert learn.restore(learn.history_versions()[0])
    assert len(learn.list_learned()) == 2
    assert not learn.restore("../../etc")


def test_consolidation_rewrites_compactly_and_revalidates() -> None:
    facts = [item(f"El cliente número {i} prefiere reuniones por la mañana temprano") for i in range(90)]
    # todos casi iguales: la dedupe los frena; se escriben a mano para forzar el tamano
    items = [learn.Learned("2026-11-01", "reunion", f"Hecho estable distinto {chr(65 + i % 26)}{i}")
             for i in range(85)]
    learn._write(items)
    assert learn.needs_consolidation()
    asked: list[str] = []

    def ask(text: str) -> dict[str, Any]:
        asked.append(text)
        return {"hechos": ["Hecho consolidado uno del negocio", "correo a@b.es fuera",
                           "Hecho consolidado dos del negocio"]}

    assert learn.consolidate(ask) == 2
    assert [i.fact for i in learn.list_learned()] == [
        "Hecho consolidado uno del negocio", "Hecho consolidado dos del negocio"
    ]
    assert asked and facts
