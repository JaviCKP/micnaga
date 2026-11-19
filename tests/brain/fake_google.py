"""Un ``claude -p`` falso para las llamadas de Google: ejecuta el guardian REAL como lo haria
Claude Code (``PreToolUse`` por stdin) y simula los conectores en memoria. Nada sale del equipo.

``FakeGoogleRunner(model_input=...)``: ``model_input(tool, exact)`` decide que entrada «escribe
el modelo» (por defecto, la exacta del prompt); asi se prueba que el guardian deniega lo que no
coincide y que lo que se ejecuta es la entrada aprobada (``updatedInput``).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from typing import Any

from micnaga.brain.claude import ClaudeRequest, ClaudeResult, ToolResult

_PLAN = re.compile(r'--plan "([^"]+)"')


class FakeGoogle:
    """Estado de los «conectores»: eventos, borradores y ficheros creados/borrados."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.drafts: dict[str, dict[str, Any]] = {}
        self.files: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.listed: list[dict[str, Any]] = []
        self._n = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def execute(self, tool: str, inp: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, inp))
        name = tool.split("__")[-1]
        if name == "create_event":
            eid = self._id("ev")
            ev = {
                "id": eid,
                "summary": inp["summary"],
                "description": inp.get("description", ""),
                "htmlLink": f"https://www.google.com/calendar/event?eid={eid}",
                "conferenceUrl": "https://meet.google.com/abc-defg-hij"
                if inp.get("addGoogleMeetUrl")
                else None,
                "start": {"dateTime": inp["startTime"]},
                "end": {"dateTime": inp["endTime"]},
                "attendees": inp.get("attendees", []),
            }
            self.events[eid] = ev
            return ev
        if name == "delete_event":
            self.events.pop(inp["eventId"])
            return {}
        if name == "get_event":
            return self.events[inp["eventId"]]
        if name == "list_events":
            evs = list(self.events.values()) + self.listed
            if inp.get("fullText"):
                evs = [e for e in evs if inp["fullText"] in e.get("description", "")]
            return {"events": evs}
        if name == "create_draft":
            did = self._id("dr")
            self.drafts[did] = inp
            return {
                "id": did,
                "threadId": "th1",
                "viewUrl": f"https://mail.google.com/#drafts/{did}",
            }
        if name == "delete_draft":
            self.drafts.pop(inp["draftId"])
            return {}
        if name == "send_message":
            self.sent.append(inp)
            return {"id": "m1", "threadId": "th1"}
        if name == "create_file":
            fid = self._id("f")
            self.files[fid] = inp
            return {"id": fid, "viewUrl": f"https://docs.google.com/document/d/{fid}"}
        if name == "trash_file":
            self.files.pop(inp["fileId"])
            return {}
        raise AssertionError(f"unexpected tool {tool}")


def exact_from_prompt(prompt: str) -> dict[str, Any]:
    line = prompt.split("\n")[1]
    data: dict[str, Any] = json.loads(line)
    return data


class FakeGoogleRunner:
    def __init__(
        self,
        google: FakeGoogle | None = None,
        *,
        model_input: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        raise_after_call: Exception | None = None,
        retry: bool = True,
    ) -> None:
        self.retry = retry
        self.google = google or FakeGoogle()
        self.model_input = model_input or (lambda _t, exact: exact)
        self.raise_after_call = raise_after_call
        self.requests: list[ClaudeRequest] = []
        self.hook_outputs: list[dict[str, Any]] = []

    def _hook(self, req: ClaudeRequest, tool: str, given: dict[str, Any], n: int) -> dict[str, Any]:
        assert req.settings is not None
        cmd = req.settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        m = _PLAN.search(cmd)
        assert m, cmd
        from pathlib import Path

        from micnaga.brain.google import policy

        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": given,
            "tool_use_id": f"toolu_{n}",
        }
        proc = subprocess.run(
            policy.guard_argv(Path(m.group(1))),
            input=json.dumps(payload).encode(),
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        out: dict[str, Any] = json.loads(proc.stdout)["hookSpecificOutput"]
        self.hook_outputs.append(out)
        return out

    def run_once(self, req: ClaudeRequest, on_event: Any = None) -> ClaudeResult:
        self.requests.append(req)
        assert not req.strict_mcp and req.allowed_tools == []  # solo el guardian autoriza
        tool = re.search(r"`([^`]+)`", req.prompt).group(1)  # type: ignore[union-attr]
        exact = exact_from_prompt(req.prompt)
        results: list[ToolResult] = []
        denials: list[str] = []
        for n in range(2):  # el modelo reintenta una vez si se le deniega
            given = self.model_input(tool, exact) if n == 0 else exact
            out = self._hook(req, tool, given, n)
            if out["permissionDecision"] != "allow":
                denials.append(tool)
                results.append(
                    ToolResult(f"toolu_{n}", tool, True, out["permissionDecisionReason"])
                )
                if n == 0 and given != exact and self.retry:
                    continue  # el guardian le dijo la entrada exacta: reintenta
                break
            data = self.google.execute(tool, out.get("updatedInput", given))
            if self.raise_after_call is not None:
                raise self.raise_after_call
            results.append(ToolResult(f"toolu_{n}", tool, False, json.dumps(data), data))
            break
        return ClaudeResult(
            structured=None,
            text="hecho",
            cost_usd=0.0,
            usage={},
            duration_s=0.1,
            num_turns=2,
            model="fake",
            mcp_servers={},
            tool_results=results,
            denials=denials,
        )

    run = run_once
