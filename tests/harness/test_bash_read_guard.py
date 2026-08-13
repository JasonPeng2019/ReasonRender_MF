from __future__ import annotations

import io
import json
import sys

import pytest
from harness.hooks.block_bash_reads import main


def payload(command: object) -> dict[str, object]:
    return {
        "cwd": "C:\\workspace",
        "hook_event_name": "PreToolUse",
        "session_id": "session-a",
        "tool_input": {"command": command, "description": "test command"},
        "tool_name": "Bash",
    }


def invoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: object,
    arm: str | None = "full",
) -> tuple[int, str, str]:
    if arm is None:
        monkeypatch.delenv("REASONRENDER_ARM", raising=False)
    else:
        monkeypatch.setenv("REASONRENDER_ARM", arm)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(value if isinstance(value, str) else json.dumps(value))
    )
    code = main()
    output = capsys.readouterr()
    return code, output.out, output.err


@pytest.mark.parametrize(
    "command",
    [
        "Get-Content -Raw harness/agents/full/worker.md",
        "cat harness/agents/full/worker.md",
        "sed -n '1p' harness/agents/full/worker.md",
        "grep -n tools harness/agents/full/worker.md",
        "rg -n tools harness/agents/full/worker.md",
        "python -c \"print(open('harness/agents/full/worker.md').read())\"",
        "python3 -c \"from pathlib import Path; print(Path('harness/agents/full/worker.md').read_text())\"",
        "echo $(cat README.md)",
    ],
)
def test_full_arm_blocks_read_like_bash(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, stdout, stderr = invoke(monkeypatch, capsys, payload(command))

    assert code == 0
    assert stderr == ""
    decision = json.loads(stdout)
    assert decision == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Full-arm Bash read guard blocked a read-like command because Read is unavailable: "
                + command
            ),
        }
    }


@pytest.mark.parametrize(
    "command",
    [
        "echo hook-probe",
        "git status --short",
        "python -m pytest tests/harness/test_bash_read_guard.py -q",
        "python -m py_compile harness/hooks/block_bash_reads.py",
        "mkdir -p artifacts",
        "printf 'cat is mentioned as text\\n'",
    ],
)
def test_ordinary_bash_commands_are_silent(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert invoke(monkeypatch, capsys, payload(command)) == (0, "", "")


@pytest.mark.parametrize("arm", [None, "raw", "contextmesh", "FULL", "full-worker"])
def test_non_full_arms_are_silent(
    arm: str | None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert invoke(monkeypatch, capsys, payload("cat README.md"), arm) == (0, "", "")


@pytest.mark.parametrize(
    "value", ["not json", "[]", {}, {"tool_name": "Bash"}, payload(None), payload(42)]
)
def test_malformed_or_missing_command_payloads_fail_open(
    value: object, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert invoke(monkeypatch, capsys, value) == (0, "", "")
