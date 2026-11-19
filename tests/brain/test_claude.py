"""``claude -p`` como subproceso: flujo stream-json, entorno, errores, reintentos y tiempos."""

from __future__ import annotations

import json
import time
from pathlib import Path

import psutil
import pytest

from micnaga.brain import claude as cl
from micnaga.brain.claude import ClaudeRequest, ClaudeRunner, ToolCall
from tests.brain.conftest import FakeClaude, read_log

SCHEMA = {"type": "object", "properties": {"titulo": {"type": "string"}}}


def _stream(*events: object) -> list[str]:
    return [json.dumps(e) for e in events]


INIT = {
    "type": "system",
    "subtype": "init",
    "model": "m",
    "apiKeySource": "none",
    "mcp_servers": [{"name": "micnaga", "status": "connected"}],
}
RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "{}",
    "structured_output": {"titulo": "x"},
    "total_cost_usd": 0.01,
    "duration_ms": 900,
    "num_turns": 2,
}


# ----------------------------------------------------------------------------- parse_stream


def test_stream_reports_tool_calls_and_skips_structured_output() -> None:
    seen: list[ToolCall] = []
    lines = _stream(
        INIT,
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "voy"},
                    {
                        "type": "tool_use",
                        "name": "mcp__micnaga__buscar_contactos",
                        "input": {"texto": "Marta"},
                    },
                ]
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "[]"}]}},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning"}},
        {"type": "something_new", "x": 1},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "StructuredOutput", "input": {"titulo": "x"}}
                ]
            },
        },
        RESULT,
    )
    res = cl.parse_stream(["not json at all", *lines, "{partial"], seen.append)
    assert [c.name for c in seen] == ["mcp__micnaga__buscar_contactos"]
    assert seen[0].server == "micnaga" and seen[0].tool == "buscar_contactos"
    assert seen[0].input == {"texto": "Marta"}
    assert res.structured == {"titulo": "x"}
    assert res.mcp_servers == {"micnaga": "connected"}
    assert res.cost_usd == 0.01 and res.num_turns == 2 and res.duration_s == 0.9


def test_stream_without_result_is_an_output_error() -> None:
    with pytest.raises(cl.ClaudeOutputError):
        cl.parse_stream(_stream(INIT, {"type": "assistant", "message": {}}))
    with pytest.raises(cl.ClaudeOutputError):
        cl.parse_stream(["garbage", '{"type": "res'])


def test_rejected_rate_limit_carries_reset_time() -> None:
    lines = _stream(
        INIT,
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "rejected", "resetsAt": 1795222800},
        },
        {"type": "result", "subtype": "success", "is_error": True, "result": "limit"},
    )
    with pytest.raises(cl.ClaudeRateLimitError) as exc:
        cl.parse_stream(lines)
    assert exc.value.resets_at == 1795222800


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Invalid API key · Please run /login", "auth"),
        ("getaddrinfo ENOTFOUND api.anthropic.com", "offline"),
        ("You've hit your usage limit", "rate_limit"),
        ("weird", "failed"),
    ],
)
def test_error_results_are_classified(text: str, kind: str) -> None:
    lines = _stream(
        INIT, {"type": "result", "subtype": "success", "is_error": True, "result": text}
    )
    with pytest.raises(cl.ClaudeError) as exc:
        cl.parse_stream(lines)
    assert exc.value.kind == kind


# ----------------------------------------------------------------------------- entorno


def test_env_is_an_allow_list_and_drops_inherited_keys() -> None:
    base = {
        "PATH": "C:\\x",
        "SystemRoot": "C:\\Windows",
        "USERPROFILE": "C:\\u",
        "ANTHROPIC_API_KEY": "sk-leak",
        "ANTHROPIC_AUTH_TOKEN": "t",
        "ANTHROPIC_BASE_URL": "u",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_OAUTH_TOKEN": "old",
        "RANDOM": "x",
    }
    env = cl.build_env("session", base=base)
    assert env["PATH"] == "C:\\x" and env["SYSTEMROOT"] == "C:\\Windows"
    assert not any(
        k.startswith(("ANTHROPIC", "CLAUDE_CODE_"))
        for k in env
        if k != "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY"
    )
    assert "RANDOM" not in env
    assert env["DISABLE_TELEMETRY"] == "1" and env["DISABLE_ERROR_REPORTING"] == "1"
    tok = cl.build_env("token", secret="oat-1", base=base)
    assert tok["CLAUDE_CODE_OAUTH_TOKEN"] == "oat-1" and "ANTHROPIC_API_KEY" not in tok
    key = cl.build_env("api_key", secret="sk-mine", base=base)
    assert key["ANTHROPIC_API_KEY"] == "sk-mine" and "CLAUDE_CODE_OAUTH_TOKEN" not in key
    with pytest.raises(cl.ClaudeAuthError):
        cl.build_env("token", secret=None, base=base)


def test_find_claude_prefers_native_and_resolves_npm_shims(tmp_path: Path) -> None:
    home = tmp_path / "home"
    native = home / ".local" / "bin" / "claude.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"")
    assert cl.find_claude({"USERPROFILE": str(home), "PATH": ""}) == [str(native)]
    native.unlink()
    npm = tmp_path / "npm"
    script = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    script.parent.mkdir(parents=True)
    script.write_text("")
    (npm / "claude.cmd").write_text("@echo off")
    (npm / "node.exe").write_bytes(b"")
    env = {"USERPROFILE": str(home), "PATH": "", "APPDATA": str(tmp_path)}
    assert cl.find_claude(env) == [str(npm / "node.exe"), str(script)]
    assert cl.find_claude({"USERPROFILE": str(home), "PATH": ""}) is None
    assert cl.find_claude({cl.ENV_CLAUDE_EXE: str(tmp_path / "nope.exe")}) is None


# ----------------------------------------------------------------------------- con el falso


def test_runner_end_to_end_with_fake(
    fake_claude: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = fake_claude({"structured": {"titulo": "Hola"}})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    res = ClaudeRunner().run(
        ClaudeRequest(
            prompt="reunión con acentos: ñandú",
            system_prompt="sp",
            model="haiku",
            schema=SCHEMA,
            max_turns=3,
        )
    )
    assert res.structured == {"titulo": "Hola"}
    assert res.cost_usd == pytest.approx(0.0123)
    data = read_log(log)
    argv = data["argv"]
    assert argv[:3] == ["-p", "--model", "haiku"]
    for flag in (
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--verbose",
        "--json-schema",
        "--system-prompt-file",
    ):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--bare" not in argv and "--safe-mode" not in argv
    assert data["stdin"] == "reunión con acentos: ñandú"
    assert data["anthropic_api_key"] is None
    assert list(Path(data["cwd"]).iterdir()) == [] if Path(data["cwd"]).exists() else True


def test_runner_retries_once_on_transient_errors(fake_claude: FakeClaude) -> None:
    log = fake_claude({"structured": {"titulo": "ok"}, "fail_first": True})
    res = ClaudeRunner(retry_delay_s=0).run(ClaudeRequest(prompt="x", system_prompt="s"))
    assert res.structured == {"titulo": "ok"}
    assert Path(str(log) + ".count").read_text() == "2"


@pytest.mark.parametrize(
    ("error", "kind", "calls"),
    [
        ("auth", "auth", "1"),
        ("rate_limit", "rate_limit", "1"),
        ("exit1", "offline", "2"),
        ("garbled", "output", "2"),
        ("max_turns", "failed", "2"),
    ],
)
def test_runner_named_errors(fake_claude: FakeClaude, error: str, kind: str, calls: str) -> None:
    log = fake_claude({"error": error})
    with pytest.raises(cl.ClaudeError) as exc:
        ClaudeRunner(retry_delay_s=0).run(ClaudeRequest(prompt="x", system_prompt="s"))
    assert exc.value.kind == kind
    assert Path(str(log) + ".count").read_text() == calls


def test_runner_timeout_kills_the_process_tree(fake_claude: FakeClaude) -> None:
    fake_claude({"sleep": 30, "structured": {}})
    before = {p.pid for p in psutil.Process().children(recursive=True)}
    t0 = time.monotonic()
    with pytest.raises(cl.ClaudeTimeoutError):
        ClaudeRunner(retries=0).run(ClaudeRequest(prompt="x", system_prompt="s", timeout_s=1.5))
    assert time.monotonic() - t0 < 10
    time.sleep(0.3)
    after = {p.pid for p in psutil.Process().children(recursive=True)}
    assert after <= before


def test_not_installed_is_named(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(cl.ENV_CLAUDE_EXE, str(tmp_path / "missing.exe"))
    with pytest.raises(cl.ClaudeNotFoundError):
        ClaudeRunner().run(ClaudeRequest(prompt="x", system_prompt="s"))


def test_auth_status_and_token_mode(fake_claude: FakeClaude) -> None:
    fake_claude({})
    assert ClaudeRunner().auth_status()["loggedIn"] is True
    runner = ClaudeRunner(auth="token", secret_getter=lambda _t: None)
    assert runner.auth_status() == {"loggedIn": False, "authMethod": "token"}
    log = fake_claude({"structured": {"titulo": "t"}})
    ClaudeRunner(auth="token", secret_getter=lambda _t: "oat-secret").run(
        ClaudeRequest(prompt="x", system_prompt="s")
    )
    data = read_log(log)
    assert data["oauth_token"] == "oat-secret" and data["anthropic_api_key"] is None
    assert "oat-secret" not in json.dumps(data["argv"])
