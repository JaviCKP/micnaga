"""Reconocimiento de pestanas de Meet por nombre (cadenas reales de este PC + casos de Meet)."""

from __future__ import annotations

import pytest

from micnaga.watcher.titles import ParsedMeetName, parse_meet_name


@pytest.mark.parametrize(
    ("raw", "title", "code"),
    [
        # Titulos de ventana (pestana activa)
        ("Meet - abc-defg-hij - Google Chrome", None, "abc-defg-hij"),
        ("Meet – abc-defg-hij - Google Chrome", None, "abc-defg-hij"),
        ("Meet: Revisión semanal - Google Chrome", "Revisión semanal", None),
        ("Revisión semanal - Meet - Google Chrome", "Revisión semanal", None),
        ("Meet - abc-defg-hij - Perfil 1: Microsoft\u200b Edge", None, "abc-defg-hij"),
        ("Meet - abc-defg-hij - Brave", None, "abc-defg-hij"),
        ("Google Meet", None, None),
        ("Meet", None, None),
        # Nombres de TabItem de UIA con las decoraciones observadas
        ("Uso de memoria de Meet - abc-defg-hij: 85,3 MB", None, "abc-defg-hij"),
        ("Pestaña inactiva: Meet - abc-defg-hij - Memoria liberada: 75,0 MB", None, "abc-defg-hij"),
        ("Meet - abc-defg-hij: en suspensión - Uso de memoria - 52,3 MB", None, "abc-defg-hij"),
        ("Uso de memoria de Meet: Revisión semanal: 140 MB", "Revisión semanal", None),
        ("Meet - abc-defg-hij - Reproduciendo audio", None, "abc-defg-hij"),
        ("Meet - abc-defg-hij - Audio playing", None, "abc-defg-hij"),
        # Solo el codigo (spec: «o contiene un codigo xxx-xxxx-xxx»)
        ("Sala abc-defg-hij", "Sala", "abc-defg-hij"),
        ("abc-defg-hij", None, "abc-defg-hij"),
        # Ingles / otras variantes de separador
        ("Meet | Weekly sync", "Weekly sync", None),
        ("Weekly sync | Google Meet", "Weekly sync", None),
    ],
)
def test_meet_names_are_recognised(raw: str, title: str | None, code: str | None) -> None:
    assert parse_meet_name(raw) == ParsedMeetName(title, code)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "Nueva pestaña - Google Chrome",
        "PQC-SaaS – Kanvas - Google Chrome",
        "Uso de memoria de pqc migracion - YouTube: 739 MB",
        "Pestaña inactiva: PQC-SaaS – Kanvas - Memoria liberada: 75,0 MB",
        "Nueva pestaña: en suspensión - Uso de memoria - 52,3 MB",
        "Meetup - eventos cerca de ti",
        "Meeting notes - Google Docs",
        "Meet the team - Acme",
        "Cómo usar Meet para clases",
        "web-site-for-sale - tienda",  # 3-4-3 pero seguido de mas letras: no es un codigo
        "ABC-DEFG-HIJ",  # los codigos de Meet son minusculas
        "ab-defg-hij",
    ],
)
def test_other_tabs_are_not_meet(raw: str) -> None:
    assert parse_meet_name(raw) is None


def test_code_is_not_taken_from_the_middle_of_a_longer_token() -> None:
    assert parse_meet_name("xabc-defg-hijk") is None
