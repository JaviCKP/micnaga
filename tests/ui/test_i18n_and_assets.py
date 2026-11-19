"""Textos externalizados: toda clave usada existe en es.json; ajustes completos."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from micnaga.config import Config
from micnaga.ui import i18n, settings_schema
from micnaga.ui.contracts import ModelProgress

WEB = Path(__file__).resolve().parents[2] / "micnaga" / "ui" / "web"
UI = WEB.parent
ES = json.loads((WEB / "i18n" / "es.json").read_text(encoding="utf-8"))

HTML_ATTR = re.compile(r'data-i18n(?:-[a-z-]+)?="([^"]+)"')
JS_KEY = re.compile(r"""\bmic\.t\(\s*["']([^"']+)["'](?!\s*\+)""")  # solo literales, no «"x." + y»
PY_KEY = re.compile(r"""\b(?:i18n\.)?\bt\(\s*["']([a-z_]+(?:\.[a-z_0-9]+)+)["']""")
ERR_KEY = re.compile(r"""\bfail\(\s*["']([a-z_.]+)["']""")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _dynamic_keys() -> set[str]:
    """Claves que el JS compone en tiempo de ejecucion (prefijo + valor)."""
    keys: set[str] = set()
    for path, f in settings_schema.FIELDS.items():
        keys |= {f"settings.f.{path}", f"settings.f.{path}.help"}
        keys |= {f"settings.opt.{path}.{c}" for c in f.choices}
    groups = [g for g, _ in settings_schema.GROUPS] + ["diagnostics", "about"]
    for g in groups:
        keys |= {f"settings.groups.{g}", f"settings.groups.{g}.nav"}
    sections = {f.section for f in settings_schema.FIELDS.values()} - {"startup"}
    keys |= {f"settings.cards.{s}" for s in sections}
    keys |= {"settings.cards.voiceprints", "settings.cards.signals", "settings.cards.licenses"}
    keys |= {f"library.group.{g}" for g in ("today", "yesterday", "week", "earlier")}
    keys |= {f"wizard.{k}_ok" for k in ("mic", "sys")}
    keys |= {f"settings.diag_state.{s}" for s in ("ok", "warn", "off", "fail")}
    keys |= {f"tray.state.{s}" for s in ("watching", "recording", "paused", "processing")}
    keys |= set(ERR_KEY.findall(_read(UI / "bridge.py")))
    keys |= {"settings.err.save_failed", "settings.err.invalid", "settings.err.startup_failed"}
    keys |= {"wizard.err.no_downloader", "wizard.err.generic"}
    # Secretaria: tarjeta de Odoo (CARD_OF por ruta) y textos que el JS elige de un mapa o
    # compone con el estado/el error que devuelve Python.
    keys.add("settings.cards.odoo")
    keys |= {f"secretaria.group.{g}" for g in ("crm", "fix")}
    keys |= {
        f"secretaria.st.{s}"
        for s in (
            "contacts",
            "companies",
            "leads",
            "lead",
            "stages",
            "history",
            "activities",
            "meetings",
        )
    }
    keys |= {f"secretaria.w.{w}" for w in ("similar", "changed", "dup_activity", "unchecked")}
    keys |= {f"secretaria.odoo.{s}" for s in ("clave", "sin_conexion", "sin_herramientas", "error")}
    keys |= {
        f"secretaria.err.{e}"
        for e in (
            "failed",
            "offline",
            "auth",
            "not_found",
            "rate_limit",
            "timeout",
            "output",
            "disabled",
            "unknown",
            "invalid",
            "odoo_incomplete",
            "odoo_auth",
            "odoo_offline",
            "odoo_access",
            "odoo_failed",
        )
    }
    keys |= {"toast.proposals_one", "toast.proposals_other"}
    return keys


def _used_keys() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for p in WEB.glob("*.html"):
        used[p.name] = set(HTML_ATTR.findall(_read(p)))
    for p in WEB.glob("*.js"):
        used[p.name] = set(JS_KEY.findall(_read(p)))
    for p in UI.glob("*.py"):
        used[p.name] = {k for k in PY_KEY.findall(_read(p)) if "." in k}
    return used


def test_every_key_used_in_html_js_and_python_exists() -> None:
    missing = {
        f"{name}: {k}"
        for name, ks in _used_keys().items()
        for k in ks
        if k not in ES and not k.endswith(".")  # «prefijo.» + valor: lo cubre _dynamic_keys
    }
    assert not missing, sorted(missing)


def test_every_dynamically_built_key_exists() -> None:
    missing = sorted(k for k in _dynamic_keys() if k not in ES)
    assert not missing, missing


def test_no_orphan_keys() -> None:
    """Todo lo que hay en es.json se usa en algun sitio (evita basura al traducir)."""
    used = {k for ks in _used_keys().values() for k in ks} | _dynamic_keys()
    # claves que solo se arman con formatos de fecha/plural u otros casos explicitos del JS
    used |= {"fmt.minutes", "fmt.hm", "fmt.hours", "fmt.today", "fmt.yesterday"}
    used |= {
        "library.speakers_one",
        "library.speakers_other",
        "toast.speakers_one",
        "toast.speakers_other",
    }
    used |= {
        "library.pause",
        "library.track_me",
        "library.track_them",
        "pill.resumed_notice",
        "pill.paused_notice",
        "pill.resume",
        "tray.state.watching",
        "settings.window_title",
        "settings.about.license",
        "settings.about.models",
        "settings.about.privacy",
        "tray.pause_watching",
        "end.title_confirmed",
        "end.title_unconfirmed",
        "end.finish",
        "end.save",
        "end.keep",
        "end.discard",
        "toast.error.capture_failed.title",
        "toast.error.capture_failed.body",
    }  # compuestos con un bucle / condicional
    orphans = sorted(k for k in ES if k not in used)
    assert not orphans, orphans


def test_strings_are_non_empty_except_optional_help() -> None:
    empty = [k for k, v in ES.items() if not v.strip() and not k.endswith(".help")]
    assert not empty


def test_placeholders_are_well_formed_and_translatable() -> None:
    for k, v in ES.items():
        assert v.count("{") == v.count("}"), k
    assert i18n.t("toast.minutes", "es", n=5) == "5 min"
    assert i18n.t("missing.key") == "missing.key"
    assert i18n.load("xx") == i18n.load("es")  # idioma desconocido -> por defecto


def test_html_pages_reference_existing_local_files() -> None:
    for page in WEB.glob("*.html"):
        for ref in re.findall(r'(?:src|href)="([^"#?]+)"', _read(page)):
            if ref.startswith(("http:", "https:", "data:")):
                pytest.fail(f"{page.name} references a remote resource: {ref}")  # nada de CDN
            assert (WEB / ref).is_file(), f"{page.name} -> {ref}"


def test_no_remote_urls_in_assets() -> None:
    for p in [*WEB.glob("*.css"), *WEB.glob("*.js"), *WEB.glob("*.html")]:
        text = _read(p)
        for m in re.finditer(r"https?://[^\s\"')]+", text):
            assert m.group(0).startswith("http://www.w3.org/"), (
                f"{p.name}: {m.group(0)}"
            )  # xmlns de SVG


def test_every_html_page_loads_bridge_first_and_declares_lang() -> None:
    for page in WEB.glob("*.html"):
        text = _read(page)
        assert '<html lang="es">' in text
        assert (
            text.index("bridge.js") < text.rindex("<script src=") or text.count("<script src=") == 1
        )


# --------------------------------------------------------------------------- ajustes


def test_every_config_field_has_a_schema_entry() -> None:
    assert set(settings_schema.FIELDS) == set(settings_schema.config_paths())


def test_every_config_field_has_label_and_help_in_spanish() -> None:
    for path in settings_schema.config_paths():
        assert ES.get(f"settings.f.{path}", "").strip(), path
        assert f"settings.f.{path}.help" in ES, path


def test_schema_matches_dataclass_types_and_defaults() -> None:
    cfg = Config()
    for path, spec in settings_schema.FIELDS.items():
        sec, key = path.split(".")
        value = getattr(getattr(cfg, sec), key)
        kinds = {
            bool: {"bool"},
            int: {"int"},
            float: {"float"},
            list: {"list"},
            str: {"text", "choice", "hotkey", "path"},
        }
        assert spec.kind in kinds[type(value)], (path, spec.kind, type(value))
        if spec.kind == "choice":
            assert value in spec.choices, path
        if spec.kind in ("int", "float") and spec.minimum is not None and spec.maximum is not None:
            assert spec.minimum <= value <= spec.maximum, path  # el valor por defecto es valido


def test_modelprogress_is_a_frozen_dataclass() -> None:
    assert dataclasses.is_dataclass(ModelProgress)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ModelProgress("a", "pending").done = 1  # type: ignore[misc]
