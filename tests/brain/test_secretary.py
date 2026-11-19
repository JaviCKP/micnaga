"""La pasada completa con un ``claude`` falso (que llama de verdad al MCP) y un Odoo falso."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from micnaga import paths
from micnaga.brain import acta as acta_io
from micnaga.brain import learn, secretary
from micnaga.brain import store as bstore
from micnaga.brain.executor import Executor
from micnaga.brain.odoo import client_from_config
from micnaga.brain.secretary import Secretary, is_generic_title
from micnaga.config import Config
from micnaga.glossary import glossary_path
from micnaga.pipeline import output
from micnaga.store.db import open_database
from tests.brain import canned
from tests.brain.conftest import FakeClaude, odoo_config, read_log
from tests.brain.fake_odoo import KEY, FakeOdoo


def run(cfg: Config, folder: Path, **kw: Any) -> tuple[Any, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []
    res = Secretary(cfg).analyze(folder, on_status=seen.append, today="2026-11-19", **kw)
    return res, seen


def by(proposals: list[dict[str, Any]], tipo: str) -> list[dict[str, Any]]:
    return [p for p in proposals if p["tipo"] == tipo]


def test_full_pass_with_odoo(fake_claude: FakeClaude, fake_odoo: FakeOdoo, meeting: Path) -> None:
    log = fake_claude({"structured": canned.structured(), "calls": canned.CALLS})
    cfg = odoo_config(Config(), fake_odoo)
    res, seen = run(cfg, meeting)
    data = read_log(log)

    # El modelo vio las 8 herramientas de solo lectura y nada mas; la clave no viajo.
    assert data["mcp_tools"] == [
        "buscar_contactos",
        "buscar_empresas",
        "buscar_oportunidades",
        "leer_oportunidad",
        "listar_etapas",
        "historial_reciente",
        "actividades_pendientes",
        "buscar_en_reuniones",
    ]
    assert all(r["isError"] is False for r in data["mcp_results"])
    assert data["mcp_exit"] == 0
    blob = json.dumps(data)
    assert KEY not in blob
    argv = data["argv"]
    assert argv[argv.index("--allowedTools") + 1] == "mcp__micnaga__*"
    assert "#8 [" in data["stdin"] and "⟨Odu?⟩" in data["stdin"]
    assert "jueves 2026-11-19" in data["stdin"]
    assert "Conectado (solo lectura)" in data["stdin"]
    # Analizar solo lee Odoo.
    assert fake_odoo.write_calls == []
    # Estado en vivo: las herramientas que uso.
    tools = [s.get("tool") for s in seen if s.get("fase") == "herramienta"]
    assert tools[:2] == ["buscar_empresas", "buscar_contactos"]
    assert seen[-1] == {"fase": "lista"}

    acta = res.acta
    assert acta["titulo"] == "Dental Triana: alcance ampliado"
    assert res.renamed and res.folder.name == "2026-11-19_1030_dental-triana-alcance-ampliado"
    assert not meeting.exists() and (res.folder / acta_io.ACTA_JSON).is_file()
    assert (res.folder / acta_io.ACTA_MD).read_text(encoding="utf-8").startswith("# Dental")
    tr = output.read_transcript(res.folder)
    assert tr.meeting.title == "Dental Triana: alcance ampliado"
    assert acta["decisiones"][0]["t"] == tr.segments[4].start
    tareas = acta["tareas"]
    assert tareas[0]["es_mia"] and tareas[0]["fecha"] == "2026-11-20"
    assert tareas[1]["responsable"] == "Hablante 2" and not tareas[1]["es_mia"]
    assert tareas[2]["fecha"] is None and tareas[2]["t"] is None  # fecha y seg invalidos

    props = acta["propuestas"]
    corr = [p for p in props if p["grupo"] == "correcciones"]
    assert [(p["tipo"], p["datos"].get("a")) for p in corr] == [
        ("hablante", "Marta Gil"),
        ("hablante", "Carlos Ruiz"),
        ("palabra", "Odoo"),
        ("palabra", "Triana"),
    ]
    assert all(p["checked"] for p in corr)
    crm = [p for p in props if p["grupo"] == "crm"]
    link = by(crm, "vincular_a_oportunidad")
    assert len(link) == 1 and link[0]["checked"] and link[0]["datos"]["id"] == 205  # 999 fuera
    upd = by(crm, "actualizar_oportunidad")[0]
    assert upd["checked"] and upd["datos"]["anterior"]["importe"] == 5400.0
    assert upd["datos"]["etapa"] == "Negociación"
    marta, carlos = by(crm, "crear_contacto")
    assert marta["checked"] and marta["datos"]["email"] == "marta@dentaltriana.es"
    assert marta["datos"]["telefono"] is None  # no se dicto en la reunion
    assert marta["datos"]["empresa"] == "Clínica Dental Triana"
    assert not carlos["checked"] and carlos["aviso"] == "parecido_existe"
    act = by(crm, "programar_actividad")[0]
    assert act["checked"] and act["datos"]["fecha"] == "2026-11-20"
    assert res.proposals == len(props) == 9
    assert [p["id"] for p in props] == [f"p{i}" for i in range(1, 10)]

    # Aprendizajes: el correo se rechaza, el hecho de negocio se guarda.
    learned = learn.list_learned()
    assert [x.fact for x in learned] == [
        "Las reuniones con Dental Triana suelen incluir a recepción."
    ]
    assert acta["aprendizajes"] == {"anadidos": 1, "rechazados": 1}

    # Indice: una sola fila de la reunion (renombrada) y el acta en el FTS propio.
    with open_database() as db:
        bstore.ensure_schema(db)
        folders = [m.folder for m in db.list_meetings()]
        assert folders == [res.folder.name]
        hits = bstore.search_actas(db, "alcance ampliado")
        assert hits and hits[0].folder == res.folder.name

    # Mismo contenido -> no se repite la llamada.
    again = Secretary(cfg).analyze(res.folder)
    assert again.cached and Path(str(log) + ".count").read_text() == "1"


def test_without_odoo_there_are_no_crm_proposals(
    fake_claude: FakeClaude, meeting: Path, secrets_file: Path
) -> None:
    log = fake_claude({"structured": canned.structured(with_crm=True)})
    res, _ = run(Config(), meeting)
    data = read_log(log)
    schema = json.loads(data["argv"][data["argv"].index("--json-schema") + 1])
    assert "propuestas" not in schema["properties"]
    assert "Sin conexión a Odoo" in data["stdin"]
    assert data["mcp_tools"] == ["buscar_en_reuniones"]
    assert all(p["grupo"] == "correcciones" for p in res.acta["propuestas"])
    assert res.acta["odoo"] == {"estado": "sin_configurar"}


def test_mcp_not_connected_drops_crm_proposals(
    fake_claude: FakeClaude, fake_odoo: FakeOdoo, meeting: Path
) -> None:
    fake_claude({"structured": canned.structured(), "mcp_status": {"micnaga": "failed"}})
    res, _ = run(odoo_config(Config(), fake_odoo), meeting)
    assert res.acta["odoo"] == {"estado": "sin_herramientas"}
    assert all(p["grupo"] == "correcciones" for p in res.acta["propuestas"])


def test_bad_odoo_key_still_produces_the_acta(
    fake_claude: FakeClaude, fake_odoo: FakeOdoo, meeting: Path
) -> None:
    from micnaga.brain import secrets

    secrets.put(secrets.ODOO_KEY, "caducada")
    fake_claude({"structured": canned.structured(with_crm=False)})
    res, _ = run(odoo_config(Config(), fake_odoo), meeting)
    assert res.acta["odoo"] == {"estado": "clave"}
    assert res.acta["titulo"]


def test_user_titles_are_never_overwritten(
    fake_claude: FakeClaude, meeting: Path, secrets_file: Path
) -> None:
    from micnaga.brain.secretary import retitle_meeting

    def with_db(fn: Any) -> Any:
        with open_database() as db:
            bstore.ensure_schema(db)
            return fn(db)

    named, _ = retitle_meeting(meeting, "Revisión semanal", with_db)
    fake_claude({"structured": canned.structured(with_crm=False)})
    res, _ = run(Config(), named)
    assert not res.renamed and res.folder == named
    assert output.read_transcript(named).meeting.title == "Revisión semanal"


@pytest.mark.parametrize(
    ("title", "generic"),
    [
        ("Reunión", True),
        ("Meet – abc-defg-hij", True),
        ("", True),
        ("Teams", True),
        ("WhatsApp", True),
        ("handy.exe", True),
        ("Revisión semanal", False),
    ],
)
def test_generic_titles(title: str, generic: bool) -> None:
    assert is_generic_title(title) is generic


# ----------------------------------------------------------------------------- aplicar


def test_apply_then_undo_everything(
    fake_claude: FakeClaude, fake_odoo: FakeOdoo, meeting: Path
) -> None:
    fake_claude({"structured": canned.structured(), "calls": canned.CALLS[:1]})
    cfg = odoo_config(Config(), fake_odoo)
    res, _ = run(cfg, meeting)
    folder = res.folder
    props = res.acta["propuestas"]
    checked = [p["id"] for p in props if p["checked"]]

    def odoo() -> Any:
        c = client_from_config(
            cfg.secretaria.odoo_url, cfg.secretaria.odoo_db, cfg.secretaria.odoo_login
        )
        c.authenticate()
        return c

    acta = Executor(folder, odoo=odoo).apply(checked)
    states = {p["id"]: p["estado"] for p in acta["propuestas"]}
    assert all(states[i] == "aplicada" for i in checked), acta["propuestas"]
    assert not any(states[p["id"]] == "aplicada" for p in props if not p["checked"])

    tr = output.read_transcript(folder)
    names = {s.id: s.name for s in tr.speakers}
    assert names["S2"] == "Marta Gil" and names["S1"] == "Carlos Ruiz"
    text = " ".join(s.text for s in tr.segments)
    assert "Odoo" in text and "Odu" not in text and "Triana" in text
    assert "Marta Gil" in (folder / paths.TRANSCRIPT_MD).read_text(encoding="utf-8")
    gl = glossary_path().read_text(encoding="utf-8")
    assert "Odoo: Odu" in gl and "Triana: Riana" in gl
    # Odoo: contacto creado, oportunidad actualizada, nota y actividad.
    partners = {p["name"]: p for p in fake_odoo.data["res.partner"]}
    assert partners["Marta Gil"]["parent_id"] == 41
    lead = fake_odoo.data["crm.lead"][0]
    assert (lead["stage_id"], lead["expected_revenue"]) == (3, 6900)
    notes = [m for m in fake_odoo.data["mail.message"] if m["res_id"] == 205]
    note = next(str(m["body"]) for m in notes if "alcance ampliado" in str(m["body"]))
    # los hablantes ya renombrados en esa misma aplicacion salen con su nombre en la nota
    assert "Marta Gil: Lista de plantillas" in note and "Hablante 2" not in note
    assert acta_io.read_acta(folder)["tareas"][1]["responsable"] == "Marta Gil"  # type: ignore[index]
    assert "Marta Gil: Lista" in (folder / acta_io.ACTA_MD).read_text(encoding="utf-8")
    acts = [a for a in fake_odoo.data["mail.activity"] if a.get("summary", "").startswith("Enviar")]
    assert acts and acts[0]["res_model_id"] == 300 and acts[0]["user_id"] == 7

    # Reaplicar no duplica.
    n_writes = len(fake_odoo.write_calls)
    Executor(folder, odoo=odoo).apply(checked)
    assert len(fake_odoo.write_calls) == n_writes

    undone = Executor(folder, odoo=odoo).undo(checked)
    assert all(p["estado"] == "deshecha" for p in undone["propuestas"] if p["id"] in checked)
    assert "Marta Gil" not in {p["name"] for p in fake_odoo.data["res.partner"]}
    lead = fake_odoo.data["crm.lead"][0]
    assert (lead["stage_id"], lead["expected_revenue"]) == (2, 5400.0)
    assert not [a for a in fake_odoo.data["mail.activity"] if a["id"] != 900]
    tr = output.read_transcript(folder)
    assert {s.id: s.name for s in tr.speakers}["S2"] == "Hablante 2"
    assert "Odu" in " ".join(s.text for s in tr.segments)


def test_changed_in_odoo_is_not_overwritten(
    fake_claude: FakeClaude, fake_odoo: FakeOdoo, meeting: Path
) -> None:
    fake_claude({"structured": canned.structured()})
    cfg = odoo_config(Config(), fake_odoo)
    res, _ = run(cfg, meeting)
    upd = by(res.acta["propuestas"], "actualizar_oportunidad")[0]
    fake_odoo.data["crm.lead"][0]["expected_revenue"] = 6100.0  # alguien lo cambio entretanto

    def odoo() -> Any:
        return client_from_config(
            cfg.secretaria.odoo_url, cfg.secretaria.odoo_db, cfg.secretaria.odoo_login
        )

    acta = Executor(res.folder, odoo=odoo).apply([upd["id"]])
    p = next(x for x in acta["propuestas"] if x["id"] == upd["id"])
    assert p["estado"] == "fallida" and p["error"] == "changed_in_odoo"
    assert fake_odoo.data["crm.lead"][0]["expected_revenue"] == 6100.0


def test_hand_edited_text_drops_the_correction_silently(
    fake_claude: FakeClaude, meeting: Path, secrets_file: Path
) -> None:
    import dataclasses

    fake_claude({"structured": canned.structured(with_crm=False)})
    res, _ = run(Config(), meeting)
    tr = output.read_transcript(res.folder)
    segs = [dataclasses.replace(s, text=s.text.replace("Odu", "Odoo")) for s in tr.segments]
    output.write_transcript(res.folder, dataclasses.replace(tr, segments=segs))
    odu = next(p for p in res.acta["propuestas"] if p["datos"].get("de") == "Odu")
    acta = Executor(res.folder).apply([odu["id"]])
    assert next(p for p in acta["propuestas"] if p["id"] == odu["id"])["estado"] == "descartada"


def test_extra_mcp_servers_are_allow_listed(
    fake_claude: FakeClaude, meeting: Path, secrets_file: Path
) -> None:
    cfg = Config()
    cfg.secretaria.mcp_servers = [
        json.dumps(
            {
                "name": "drive",
                "command": "npx",
                "args": ["drive-mcp"],
                "env": {"A": "1"},
                "tools": ["search", "read"],
            }
        )
    ]
    servers, allowed = secretary.extra_mcp_servers(cfg)
    assert servers["drive"]["command"] == "npx"
    assert allowed == ["mcp__drive__search", "mcp__drive__read"]
    log = fake_claude(
        {"structured": canned.structured(with_crm=False), "mcp_status": {"drive": "failed"}}
    )
    run(cfg, meeting)
    data = read_log(log)
    argv = data["argv"]
    assert argv[argv.index("--allowedTools") + 1] == (
        "mcp__micnaga__*,mcp__drive__search,mcp__drive__read"
    )
    assert set(data["mcp_config"]) == {"micnaga", "drive"}
