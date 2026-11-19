"""Cliente de Odoo (contra un Odoo falso) y servidor MCP de solo lectura."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from micnaga.brain import mcp
from micnaga.brain.odoo import OdooClient, OdooConfig, OdooError, normalize_name
from micnaga.config import Config
from tests.brain.conftest import odoo_config
from tests.brain.fake_odoo import DB, KEY, LOGIN, FakeOdoo


def client(srv: FakeOdoo, key: str = KEY) -> OdooClient:
    return OdooClient(OdooConfig(srv.url, DB, LOGIN, key), timeout_s=5)


def test_probe_and_reads(fake_odoo: FakeOdoo) -> None:
    c = client(fake_odoo)
    info = c.probe()
    assert info["user"] == "Javier" and info["uid"] == 7
    assert info["version"] == "19.0-fake"
    assert all(info["readable"].values())
    companies = c.search_partners("Dental Triana S.L.", companies=True)
    assert companies[0]["id"] == 41 and companies[0]["es_empresa"]
    people = c.search_partners("carlos ruiz")
    assert people[0]["nombre"] == "Carlos Ruiz Ortega"
    assert people[0]["empresa"] == {"id": 41, "nombre": "Clínica Dental Triana"}
    assert c.search_partners("Marta Gil") == []
    leads = c.search_leads("Triana")
    assert leads[0]["id"] == 205 and leads[0]["etapa"]["nombre"] == "Propuesta"
    assert c.search_leads(partner_id=41)[0]["id"] == 205
    lead = c.read_lead(205)
    assert lead is not None and lead["importe"] == 5400.0
    assert lead["actividades_pendientes"][0]["resumen"] == "Llamar a Carlos"
    assert "5.400" in lead["historial"][0]["texto"]
    assert [s["nombre"] for s in c.stages()] == ["Nuevo", "Propuesta", "Negociación", "Ganado"]
    assert fake_odoo.write_calls == []


def test_errors_are_named_and_never_carry_the_key(fake_odoo: FakeOdoo) -> None:
    with pytest.raises(OdooError) as bad:
        client(fake_odoo, key="wrong").authenticate()
    assert bad.value.kind == "auth" and "wrong" not in str(bad.value)
    fake_odoo.denied.add("crm.lead")
    assert client(fake_odoo).can_read("crm.lead") is False
    assert client(fake_odoo).can_read("res.partner") is True
    with pytest.raises(OdooError) as denied:
        client(fake_odoo).search_leads("x")
    assert denied.value.kind == "access"
    offline = OdooClient(OdooConfig("http://127.0.0.1:9", DB, LOGIN, KEY), timeout_s=2)
    with pytest.raises(OdooError) as off:
        offline.authenticate()
    assert off.value.kind == "offline"
    with pytest.raises(OdooError):
        OdooClient(OdooConfig("ftp://x", DB, LOGIN, KEY))
    with pytest.raises(OdooError):
        client(fake_odoo).call("res.partner", "action_archive", [[1]])


def test_normalize_strips_accents_and_company_suffixes() -> None:
    assert normalize_name("Clínica Dental Triana, S.L.") == "clinica dental triana"
    assert normalize_name("ACME SA") == "acme"


# ----------------------------------------------------------------------------- MCP


def rpc(server: mcp.Server, method: str, params: dict[str, Any] | None = None) -> Any:
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    assert reply is not None
    return reply


def test_mcp_exposes_exactly_the_read_only_tools(fake_odoo: FakeOdoo) -> None:
    server = mcp.Server(mcp.Tools(client(fake_odoo)))
    init = rpc(server, "initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert init["serverInfo"]["name"] == "micnaga" and "tools" in init["capabilities"]
    tools = rpc(server, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == list(mcp.ALL_TOOLS)
    assert len(tools) == 10 and all(t["annotations"]["readOnlyHint"] for t in tools)
    out = rpc(server, "tools/call", {"name": "buscar_contactos", "arguments": {"texto": "Carlos"}})
    data = json.loads(out["result"]["content"][0]["text"])
    assert data[0]["id"] == 57 and out["result"]["isError"] is False
    bad = rpc(server, "tools/call", {"name": "crear_contacto", "arguments": {}})
    assert bad["result"]["isError"] is True
    missing = rpc(server, "tools/call", {"name": "leer_oportunidad", "arguments": {}})
    assert missing["result"]["isError"] is True
    assert rpc(server, "nope")["error"]["code"] == -32601
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert fake_odoo.write_calls == []


def test_mcp_without_odoo_only_has_the_library_tool() -> None:
    server = mcp.Server(mcp.Tools(None))
    tools = rpc(server, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["buscar_en_reuniones", "reuniones_con", "leer_acta"]


def test_odoo_down_mid_call_is_a_tool_error_not_a_crash(fake_odoo: FakeOdoo) -> None:
    c = client(fake_odoo)
    c.authenticate()
    fake_odoo.stop()
    server = mcp.Server(mcp.Tools(c))
    out = rpc(server, "tools/call", {"name": "listar_etapas", "arguments": {}})["result"]
    assert out["isError"] is True and out["content"][0]["text"] == "Odoo no responde"


def test_mcp_stdio_process_reads_key_itself_and_exits_on_eof(
    fake_odoo: FakeOdoo, secrets_file: Path, meeting: Path
) -> None:
    from micnaga.brain.secretary import mcp_command, mcp_env

    cfg = odoo_config(Config(), fake_odoo)
    env = mcp_env(cfg)
    assert KEY not in json.dumps(env)  # la clave no viaja en el entorno
    assert env["MICNAGA_STORAGE"] == os.environ["MICNAGA_STORAGE"]
    proc = subprocess.Popen(mcp_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "leer_oportunidad", "arguments": {"id": 205}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "buscar_en_reuniones", "arguments": {"texto": "plantillas"}},
        },
    ]
    payload = b"".join(json.dumps(m).encode() + b"\n" for m in msgs) + b"garbage\n"
    out, _ = proc.communicate(payload, timeout=30)
    replies = [json.loads(line) for line in out.decode("utf-8").splitlines()]
    assert proc.returncode == 0
    assert [r["id"] for r in replies] == [1, 2, 3, 4, None]
    assert len(replies[1]["result"]["tools"]) == 10
    lead = json.loads(replies[2]["result"]["content"][0]["text"])
    assert lead["nombre"].startswith("CRM + recordatorios")
    found = json.loads(replies[3]["result"]["content"][0]["text"])
    assert found["frases"][0]["carpeta"] == meeting.name
    assert replies[4]["error"]["code"] == -32700
    assert fake_odoo.write_calls == []
    assert sys.executable  # el hijo usa este mismo interprete
