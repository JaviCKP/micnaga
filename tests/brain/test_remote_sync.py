from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from micnaga import holding, paths
from micnaga.integration import remote_sync
from micnaga.meeting_links import meeting_link, parse_meeting_link
from micnaga.store.db import Database, MeetingRecord

URL = "https://reuniones.example.com/mcp"
ISSUER = "https://empresa.authkit.app"


def _meeting(db: Database, root: Path, name: str, *, processing: bool = False) -> int:
    folder = root / name
    folder.mkdir()
    (folder / paths.TRANSCRIPT_JSON).write_text(
        json.dumps(
            {
                "speakers": [{"id": "me", "name": "Rosa"}],
                "segments": [
                    {
                        "start": 74,
                        "end": 78,
                        "text": "La revisión es el jueves",
                        "text_original": "La revisión es el jueves, confirmado",
                        "speaker": "me",
                        "words": [{"w": "no exportar los tokens del ASR"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (folder / paths.AUDIO_ME_FLAC).write_bytes(b"solo-en-el-equipo")
    job = db.enqueue_job(folder) if processing else None
    return db.upsert_meeting(
        MeetingRecord(name, name, "2026-11-01T10:00:00", 130, job_id=job.id if job else None)
    )


def test_remote_mcp_survives_lost_ack_and_enforces_workspace_permissions(
    tmp_path: Path,
    tmp_storage: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwt = pytest.importorskip("jwt")
    pytest.importorskip("mcp")
    from cryptography.hazmat.primitives.asymmetric import rsa
    from starlette.testclient import TestClient

    from remote.server import create_app
    from remote.store import Store

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True)
    jwk.update(kid="workos-test", alg="RS256", use="sig")
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _self: {"keys": [jwk]})

    store = Store(tmp_path / "service" / "test.sqlite3")
    _, device_key = store.add_device("bitnaga")
    _, other_key = store.add_device("otro")
    store.grant("bitnaga", "user_javi", allow=True)
    store.grant("otro", "user_other", allow=True)
    root = tmp_storage.resolve()
    root.mkdir()
    with Database(root / paths.DB_FILENAME) as db:
        local_id = _meeting(db, root, "Revision PQC")
        _meeting(db, root, "En proceso", processing=True)
        _meeting(db, root, "Descartada")
        holding.mark_discarded(root / "Descartada")

    def bearer(subject: str = "user_javi", **claims: Any) -> dict[str, str]:
        token = jwt.encode(
            {
                "iss": ISSUER,
                "sub": subject,
                "aud": URL,
                "exp": int(time.time()) + 60,
                **claims,
            },
            rsa_key,
            algorithm="RS256",
            headers={"kid": "workos-test"},
        )
        return {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}

    with TestClient(create_app(store, URL, ISSUER), base_url=URL.removesuffix("/mcp")) as client:
        revisions: list[int] = []

        def rpc(method: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
            response = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params,
                },
            )
            assert response.status_code == 200, response.text
            return response.json()["result"]

        def transport(url: str, key: str, data: bytes | None = None) -> tuple[int, bytes]:
            headers = {"Authorization": "Bearer " + key}
            if data is None:
                r = client.get(urlsplit(url).path, headers=headers)
            else:
                assert b"solo-en-el-equipo" not in data and b"no exportar" not in data
                revisions.append(json.loads(data)["revision"])
                r = client.put(urlsplit(url).path, headers=headers, content=data)
                if len(revisions) == 1:
                    assert r.status_code == 204
                    raise OSError("respuesta perdida tras guardar")
            return r.status_code, r.content

        monkeypatch.setattr(remote_sync, "_request", transport)
        publisher = remote_sync.RemoteSync(lambda: root, lambda: False)
        status = publisher.connect(URL, device_key)
        assert status["workspace"] == "bitnaga" and device_key not in repr(status)
        assert publisher._connection is not None
        with pytest.raises(OSError, match="respuesta perdida"):
            publisher.scan(publisher._connection)

        publisher = remote_sync.RemoteSync(lambda: root, lambda: False)
        connection = publisher._connection
        assert connection is not None and connection.url == URL
        assert publisher.scan(connection) == ("ready", 1)
        assert revisions == [1, 1]
        assert publisher.scan(connection) == ("ready", 0)

        response = client.post("/mcp")
        assert response.status_code == 401
        assert "/.well-known/oauth-protected-resource/mcp" in response.headers["www-authenticate"]
        meta = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert meta["authorization_servers"] == [ISSUER] and meta["resource"] == URL
        assert client.post("/mcp", headers=bearer(aud=URL + "-otra")).status_code == 401
        assert client.post("/mcp", headers=bearer(exp=int(time.time()) - 20)).status_code == 401
        assert client.post("/mcp", headers=bearer("sin-permiso")).status_code == 401
        assert (
            client.post("/mcp", headers={"Authorization": "Bearer " + device_key}).status_code
            == 401
        )
        assert client.get("/api/v1/connection", headers=bearer()).status_code == 401

        assert (
            rpc(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
                bearer(),
            )["serverInfo"]["name"]
            == "Micnaga · Reuniones"
        )
        assert {t["name"] for t in rpc("tools/list", {}, bearer())["tools"]} == {
            "buscar_reuniones",
            "leer_acta",
            "leer_transcripcion",
        }
        assert "revisar_acta" in {p["name"] for p in rpc("prompts/list", {}, bearer())["prompts"]}

        def tool(name: str, args: dict[str, Any], who: str = "user_javi") -> dict[str, Any]:
            return rpc("tools/call", {"name": name, "arguments": args}, bearer(who))

        search = tool("buscar_reuniones", {"texto": "confirmado"})["structuredContent"]
        assert len(search["reuniones"]) == 1
        hit = search["reuniones"][0]
        remote_id = hit["reunion_id"]
        assert (hit["inicio_s"], hit["version"], hit["enlace"]) == (
            74,
            "original",
            meeting_link(root, local_id, 74) + f"&d={connection.device}",
        )
        citation = parse_meeting_link(hit["enlace"])
        assert citation is not None and citation.device_id == connection.device
        assert publisher.owns_citation(root, citation.library_id, citation.device_id)
        assert not publisher.owns_citation(root, citation.library_id, "f" * 32)
        assert not tool("buscar_reuniones", {}, "user_other")["structuredContent"]["reuniones"]
        assert tool("leer_transcripcion", {"reunion_id": remote_id}, "user_other")["isError"]
        quote = tool("leer_transcripcion", {"reunion_id": remote_id, "desde_s": 74})
        assert quote["structuredContent"]["segmentos"][0]["texto"] == "La revisión es el jueves"

        (root / "Revision PQC" / "acta.json").write_text(
            json.dumps(
                {
                    "resumen": ["Seguimiento el jueves"],
                    "propuestas": [{"confidencial": "dato fuera del acta"}],
                }
            ),
            encoding="utf-8",
        )
        assert publisher.scan(connection) == ("ready", 1)
        assert revisions[-1] == 2
        minutes = tool("leer_acta", {"reunion_id": remote_id})["structuredContent"]["acta"]
        assert "Seguimiento el jueves" in minutes and "dato fuera del acta" not in minutes
        target = f"/api/v1/meetings/{connection.library}/{local_id}"
        assert (
            client.put(
                target,
                json={"revision": 1, "document": None},
                headers={"Authorization": "Bearer " + device_key},
            ).status_code
            == 409
        )
        assert (
            client.put(
                target,
                json={"revision": 20, "document": None},
                headers={"Authorization": "Bearer " + other_key},
            ).status_code
            == 204
        )
        assert not tool("leer_acta", {"reunion_id": remote_id})["isError"]
        with Database(root / paths.DB_FILENAME) as db:
            assert db.delete_meeting("Revision PQC")
        assert publisher.scan(connection) == ("ready", 1)
        assert revisions[-1] == 3
        assert not tool("buscar_reuniones", {"texto": "confirmado"})["structuredContent"][
            "reuniones"
        ]
        store.grant("bitnaga", "user_javi", allow=False)
        assert client.post("/mcp", headers=bearer()).status_code == 401
