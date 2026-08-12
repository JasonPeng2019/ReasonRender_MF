#!/usr/bin/env python3
"""Nonsecret native-hook probe used only by the one-shot RRCv2 capability matrix."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

MAX_INPUT = 2 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[2]
CALL_ROOT = (
    ROOT / ".generated/state/rrcv2-convergence/capability/calls/cap-08-root-strong-medium-native"
)


def _append(payload: dict[str, object]) -> None:
    path = CALL_ROOT / "hook-events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    fd = os.open(
        path,
        os.O_APPEND
        | os.O_CREAT
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
        0o600,
    )
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("hook evidence is not regular")
        os.fchmod(fd, 0o600)
        view = memoryview(line)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise RuntimeError("short hook evidence write")
            view = view[written:]
    finally:
        os.close(fd)


def handle(payload: dict[str, object]) -> dict[str, object] | None:
    _append(payload)
    event = payload.get("hook_event_name")
    if event == "PreToolUse" and payload.get("tool_name") == "spawn_agent":
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            raise RuntimeError("spawn input is absent")
        expected = {
            "agent_type": "worker",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "low",
            "service_tier": "priority",
            "fork_context": False,
        }
        for field, value in expected.items():
            if tool_input.get(field) != value:
                raise RuntimeError(f"spawn {field} differs from capability contract")
        message = tool_input.get("message")
        if not isinstance(message, str) or "RRCV2_WORKER_CAPABILITY" not in message:
            raise RuntimeError("spawn assignment sentinel is absent")
        updated = dict(tool_input)
        updated["message"] = message + "\nCAPABILITY_PRETOOL_REWRITE"
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": updated,
            }
        }
    if event == "SubagentStart":
        return {
            "hookSpecificOutput": {
                "hookEventName": "SubagentStart",
                "additionalContext": "CAPABILITY_SUBAGENT_START_CONTEXT",
            }
        }
    if event == "PostToolUse" and payload.get("tool_name") == "multi_agent_v1wait_agent":
        tool_input = payload.get("tool_input")
        if isinstance(tool_input, dict) and tool_input.get("capability_synthetic") is True:
            response = payload.get("tool_response")
            if not isinstance(response, dict) or not isinstance(response.get("status"), dict):
                raise RuntimeError("synthetic wait response is absent")
            rows = []
            for agent_id, state in sorted(response["status"].items()):
                if not isinstance(agent_id, str) or not isinstance(state, dict):
                    raise RuntimeError("synthetic wait row is malformed")
                if "completed" in state:
                    rows.append(
                        {"agent_id": agent_id, "state": "accepted", "receipt": state["completed"]}
                    )
                else:
                    rows.append({"agent_id": agent_id, "state": "pending"})
            replacement = {"v": 1, "kind": "rrcv2_wait_capability", "results": rows}
            return {
                "continue": False,
                "stopReason": json.dumps(
                    replacement, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            }
    return None


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        return 2
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        result = handle(payload)
    except Exception as exc:
        print(json.dumps({"continue": False, "stopReason": f"capability hook failure: {exc}"}))
        return 0
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
