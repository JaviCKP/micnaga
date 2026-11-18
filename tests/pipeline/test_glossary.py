from __future__ import annotations

from pathlib import Path

from micnaga import paths
from micnaga.config import Config
from micnaga.glossary import Glossary, glossary_path, normalize, parse_entries
from micnaga.types import Word

TEXT = """
# comentario
Laura
Pedro
SQL: sequel, ese cu ele
Kubernetes: kubernetis
Microsoft
San Francisco
González
"""


def glossary() -> Glossary:
    return Glossary(parse_entries(TEXT.splitlines()))


def test_parse_entries() -> None:
    entries = parse_entries(TEXT.splitlines())
    assert [e.canonical for e in entries] == [
        "Laura",
        "Pedro",
        "SQL",
        "Kubernetes",
        "Microsoft",
        "San Francisco",
        "González",
    ]
    assert entries[2].variants == ("sequel", "ese cu ele")


def test_normalize_ignores_case_accents_and_punctuation() -> None:
    assert normalize("Gonzalez,") == normalize("GONZÁLEZ") == "gonzalez"


def test_names_and_acronyms_are_fixed() -> None:
    g = glossary()
    assert g.apply_text("ayer hablé con laura y con gonzalez") == (
        "ayer hablé con Laura y con González"
    )
    assert g.apply_text("la base de datos sql es lenta") == "la base de datos SQL es lenta"
    assert g.apply_text("usamos sequel para eso") == "usamos SQL para eso"
    assert g.apply_text("un cluster de cubernetes") == "un cluster de Kubernetes"
    assert g.apply_text("trabaja en micro soft ahora") == "trabaja en Microsoft ahora"
    assert g.apply_text("viaje a san francisco") == "viaje a San Francisco"
    assert g.apply_text("la consulta ese cu ele falla") == "la consulta SQL falla"


def test_punctuation_is_preserved() -> None:
    g = glossary()
    assert g.apply_text("¿Laura? No, laura.") == "¿Laura? No, Laura."
    assert g.apply_text("(sql), sequel;") == "(SQL), SQL;"


def test_common_words_are_not_overcorrected() -> None:
    g = glossary()
    for text in (
        "pero vamos a verlo con calma",  # pero ~ Pedro
        "la lara dijo que si",  # lara ~ Laura
        "esto es muy sol y mas sal",  # sol/sal ~ SQL
        "el pedido llegó tarde, no pedro",  # pedido ~ Pedro
        "los cuberos y el cubo",  # cubo ~ Kubernetes (lejano)
        "microscopio y micrófono",
        "gonzo",
    ):
        corrected = g.apply_text(text)
        expected = "el pedido llegó tarde, no Pedro" if "pedro" in text else text
        assert corrected == expected


def test_does_not_cross_sentence_punctuation() -> None:
    g = Glossary(parse_entries(["San Francisco"]))
    assert g.apply_text("vi a san. francisco llegó") == "vi a san. francisco llegó"


def test_apply_words_keeps_times_and_punctuation() -> None:
    g = glossary()
    words = [
        Word("Hola", 0.0, 0.3, 0.9),
        Word("laura,", 0.3, 0.7, 0.8),
        Word("micro", 1.0, 1.3, 0.7),
        Word("soft.", 1.3, 1.8, 0.9),
    ]
    out = g.apply_words(words)
    assert [w.w for w in out] == ["Hola", "Laura,", "Microsoft."]
    assert out[1].s == 0.3 and out[1].e == 0.7
    assert (out[2].s, out[2].e) == (1.0, 1.8)  # las palabras fundidas abarcan ambos tiempos
    assert out[2].p == (0.7 + 0.9) / 2


def test_apply_words_same_token_count_keeps_each_word() -> None:
    g = glossary()
    out = g.apply_words([Word("san", 0, 1), Word("francisco!", 1, 2)])
    assert [w.w for w in out] == ["San", "Francisco!"]


def test_no_entries_is_a_no_op() -> None:
    g = Glossary()
    assert g.apply_text("lo que sea") == "lo que sea"
    assert [w.w for w in g.apply_words([Word("x", 0, 1)])] == ["x"]


def test_hotwords_are_the_canonical_forms() -> None:
    assert glossary().hotwords()[:3] == ["Laura", "Pedro", "SQL"]


def test_load_from_app_data_and_config(tmp_home: Path) -> None:
    paths.ensure_dir(tmp_home)
    glossary_path().write_text("Zoltan\nAcme: akme\n", encoding="utf-8")
    cfg = Config()
    cfg.processing.glossary = ["Parakeet"]
    g = Glossary.load(config=cfg)
    assert sorted(g.hotwords()) == ["Acme", "Parakeet", "Zoltan"]
    assert g.apply_text("el modelo parakeet de akme") == "el modelo Parakeet de Acme"
    assert len(Glossary.load(path=tmp_home / "nada.txt")) == 0
