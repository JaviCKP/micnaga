"""Manifiestos de la extension y del host, y garantias de privacidad del codigo JS."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from micnaga.watcher import extension as ext

ROOT = Path(__file__).resolve().parents[2]
EXT_DIR = ROOT / "extension"
JS_FILES = ("detect.js", "content.js", "background.js")


def _manifest() -> dict[str, object]:
    data = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_manifest_v3_minimal_permissions() -> None:
    m = _manifest()
    assert m["manifest_version"] == 3
    assert m["permissions"] == ["nativeMessaging"]
    assert m["host_permissions"] == ["https://meet.google.com/*"]
    assert "optional_permissions" not in m and "optional_host_permissions" not in m
    assert "externally_connectable" not in m and "web_accessible_resources" not in m
    assert "<all_urls>" not in json.dumps(m)


def test_manifest_scripts_exist_and_only_run_on_meet() -> None:
    m = _manifest()
    worker = m["background"]
    assert isinstance(worker, dict)
    assert (EXT_DIR / str(worker["service_worker"])).is_file()
    scripts = m["content_scripts"]
    assert isinstance(scripts, list) and len(scripts) == 1
    entry = scripts[0]
    assert entry["matches"] == ["https://meet.google.com/*"]
    assert entry["js"] == ["detect.js", "content.js"]  # detect antes que content
    for name in entry["js"]:
        assert (EXT_DIR / name).is_file()


def test_extension_has_no_network_code() -> None:
    forbidden = re.compile(
        r"\bfetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon|EventSource|importScripts|eval\s*\(|"
        r"https?://(?!meet\.google\.com)",
    )
    for name in JS_FILES:
        text = (EXT_DIR / name).read_text(encoding="utf-8")
        # los comentarios pueden citar URLs; se quitan antes de comprobar
        code = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.DOTALL)
        assert not forbidden.search(code), f"{name} contiene codigo de red"


def test_manifest_key_gives_stable_extension_id() -> None:
    key = str(_manifest()["key"])
    ext_id = ext.extension_id_from_key(key)
    assert re.fullmatch(r"[a-p]{32}", ext_id)
    assert ext.extension_id_from_key(key) == ext_id


def test_extension_id_algorithm_known_vector() -> None:
    # SHA-256 de b"" empieza por e3b0c442 98fc1c14... -> nibbles e,3,b,0,c,4,4,2 -> o,d,l,a,m,e,e,c
    import base64

    assert ext.extension_id_from_key(base64.b64encode(b"").decode()).startswith("odlameec")


def test_host_manifest_matches_template_and_has_valid_origin() -> None:
    ext_id = ext.extension_id_from_key(str(_manifest()["key"]))
    built = ext.build_host_manifest(r"C:\Micnaga\micnaga-host.exe", ext_id)
    template = (EXT_DIR / "native-host" / "es.micnaga.host.json.template").read_text(
        encoding="utf-8"
    )
    filled = template.replace("@HOST_PATH@", r"C:\\Micnaga\\micnaga-host.exe").replace(
        "@EXTENSION_ID@", ext_id
    )
    assert json.loads(filled) == built
    assert built["name"] == ext.HOST_NAME == "es.micnaga.host"
    assert built["type"] == "stdio"
    assert built["allowed_origins"] == [f"chrome-extension://{ext_id}/"]


def test_host_manifest_rejects_bad_extension_id() -> None:
    with pytest.raises(ValueError):
        ext.build_host_manifest("x.exe", "short")
    with pytest.raises(ValueError):
        ext.build_host_manifest("x.exe", "z" * 32)  # fuera de a-p


def test_registry_keys_are_hkcu_relative_and_named_after_the_host() -> None:
    assert set(ext.NATIVE_HOST_REGISTRY_KEYS) == {"chrome", "edge", "brave"}
    for key in ext.NATIVE_HOST_REGISTRY_KEYS.values():
        assert key.endswith(r"NativeMessagingHosts\es.micnaga.host")
        assert not key.upper().startswith(("HKEY", "HKLM", "HKCU"))


def test_registration_script_never_runs_by_default_and_supports_whatif() -> None:
    text = (ROOT / "scripts" / "register_native_host.ps1").read_text(encoding="utf-8")
    assert "SupportsShouldProcess" in text
    assert "ShouldProcess" in text
    # toda escritura al registro esta tras ShouldProcess
    for line in text.splitlines():
        if "New-Item -Path $keys" in line or "Set-ItemProperty" in line:
            idx = text.index(line)
            assert "ShouldProcess" in text[max(0, idx - 400) : idx]


# ----------------------------------------------------------------------------- detect.js con node

NODE = shutil.which("node")

NODE_HARNESS = r"""
const detect = require(process.argv[2]);
function doc(title, labels) {
  return {
    title,
    querySelectorAll: () => labels.map((l) => ({ getAttribute: () => l })),
  };
}
const cases = JSON.parse(process.argv[3]);
const out = cases.map((c) => detect.detect(doc(c.title, c.labels), { pathname: c.path }));
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node no esta instalado")
def test_detect_js_heuristics(tmp_path: Path) -> None:
    harness = tmp_path / "harness.js"
    harness.write_text(NODE_HARNESS, encoding="utf-8")
    cases = [
        # en llamada, titulo con nombre de reunion
        {"title": "Revisión semanal - Meet", "labels": ["Leave call"], "path": "/abc-defg-hij"},
        # en llamada en espanol, titulo = codigo -> sin titulo
        {
            "title": "Meet - abc-defg-hij",
            "labels": ["Salir de la llamada"],
            "path": "/abc-defg-hij",
        },
        # vestibulo: sin boton de colgar
        {"title": "Meet", "labels": ["Join now", "Turn off camera"], "path": "/abc-defg-hij"},
        # pagina de inicio de Meet
        {"title": "Google Meet", "labels": ["Leave call"], "path": "/"},
        # codigo con query
        {"title": "Meet", "labels": ["Anruf verlassen"], "path": "/abc-defg-hij/"},
        # ruta no valida
        {"title": "Meet", "labels": ["Leave call"], "path": "/landing"},
    ]
    result = subprocess.run(
        [str(NODE), str(harness), str(EXT_DIR / "detect.js"), json.dumps(cases)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    out = json.loads(result.stdout)
    assert out[0] == {"state": "in_call", "title": "Revisión semanal", "code": "abc-defg-hij"}
    assert out[1] == {"state": "in_call", "title": None, "code": "abc-defg-hij"}
    assert out[2] == {"state": "not_in_call", "title": None, "code": "abc-defg-hij"}
    assert out[3] == {"state": "not_in_call", "title": None, "code": None}
    assert out[4]["state"] == "in_call" and out[4]["code"] == "abc-defg-hij"
    assert out[5] == {"state": "not_in_call", "title": None, "code": None}


def test_service_worker_does_not_start_host_on_unrelated_tab_events() -> None:
    code = (EXT_DIR / "background.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", code, flags=re.DOTALL)
    assert "tabs.onUpdated" not in code  # sin permiso "tabs" nunca veria navegaciones fuera de Meet
    removed = code[code.index("tabs.onRemoved") :]
    assert "false)" in removed.replace(" ", "")  # solo si el puerto ya esta abierto
    listener = code[code.index("runtime.onMessage") : code.index("tabs.onRemoved")]
    assert "true)" in listener.replace(" ", "")  # un mensaje de Meet si abre el host
