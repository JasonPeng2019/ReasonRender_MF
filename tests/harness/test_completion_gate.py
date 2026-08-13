from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from harness.gate_manifest import GateTask
from harness.hooks._gate_state import GateState, save_state
from harness.hooks.completion_gate import evaluate


TASKS = {"ruleforge-payment": GateTask("ruleforge-payment", "python -m pytest tests/test_payment_rule.py -q")}


def payload(tmp_path: Path, text: str = "Task ruleforge-payment") -> dict[str, object]:
    transcript = tmp_path / "transcript.txt"
    transcript.write_text(text, encoding="utf-8")
    return {"session_id": "session-a", "transcript_path": str(transcript), "stop_hook_active": True, "cwd": str(tmp_path)}


def logs(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_success_allows_and_writes_gate_record(tmp_path: Path) -> None:
    result = evaluate(payload(tmp_path), TASKS, state_dir=tmp_path / "state", log_path=tmp_path / "gate.jsonl", runner=lambda _cmd, _cwd: (0, "ok"), clock=lambda: 10)

    assert result == {"decision": "approve", "verdict": "pass"}
    record = logs(tmp_path / "gate.jsonl")[0]
    assert record["verdict"] == "pass"
    assert record["cmd"] == TASKS["ruleforge-payment"].acceptance_cmd


def test_failure_blocks_with_bounded_output_and_persists_cycle(tmp_path: Path) -> None:
    result = evaluate(payload(tmp_path), TASKS, state_dir=tmp_path / "state", log_path=tmp_path / "gate.jsonl", runner=lambda _cmd, _cwd: (7, "x" * 2500), clock=lambda: 10)

    assert result["decision"] == "block"
    assert "exit=7" in result["reason"]
    assert TASKS["ruleforge-payment"].acceptance_cmd in result["reason"]
    assert "x" * 2000 in result["reason"]
    assert logs(tmp_path / "gate.jsonl")[0]["cycle"] == 1


def test_cycle_exhaustion_allows_after_three_blocks(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    log = tmp_path / "gate.jsonl"
    for _ in range(3):
        assert evaluate(payload(tmp_path), TASKS, state_dir=state_dir, log_path=log, runner=lambda _cmd, _cwd: (1, "bad"), clock=lambda: 10)["decision"] == "block"

    exhausted = evaluate(payload(tmp_path), TASKS, state_dir=state_dir, log_path=log, runner=lambda _cmd, _cwd: (0, "unused"), clock=lambda: 10)
    assert exhausted == {"decision": "approve", "verdict": "fail"}
    assert [item["verdict"] for item in logs(log)] == ["block", "block", "block", "fail"]


def test_wall_timeout_allows_without_running_command(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    save_state(state_dir, "session-a", GateState(started_at=0, cycles=1))
    result = evaluate(payload(tmp_path), TASKS, state_dir=state_dir, log_path=tmp_path / "gate.jsonl", runner=lambda _cmd, _cwd: (_ for _ in ()).throw(AssertionError("must not run")), clock=lambda: 901)
    assert result == {"decision": "approve", "verdict": "fail_timeout"}


def test_unknown_or_ambiguous_task_allows_unresolved(tmp_path: Path) -> None:
    unknown = evaluate(payload(tmp_path, "Task unknown"), TASKS, state_dir=tmp_path / "state", log_path=tmp_path / "unknown.jsonl", clock=lambda: 0)
    ambiguous_tasks = {**TASKS, "ruleforge-user": GateTask("ruleforge-user", "python -m pytest tests/test_user_rule.py -q")}
    ambiguous = evaluate(payload(tmp_path, "ruleforge-payment ruleforge-user"), ambiguous_tasks, state_dir=tmp_path / "state", log_path=tmp_path / "ambiguous.jsonl", clock=lambda: 0)
    assert unknown == {"decision": "approve", "verdict": "unresolved"}
    assert ambiguous == {"decision": "approve", "verdict": "unresolved"}


def test_child_transcript_resolves_before_ambiguous_parent_transcript(tmp_path: Path) -> None:
    tasks = {
        **TASKS,
        "ruleforge-user": GateTask("ruleforge-user", "python -m pytest tests/test_user_rule.py -q"),
    }
    parent = tmp_path / "parent.txt"
    child = tmp_path / "child.txt"
    parent.write_text("ruleforge-payment ruleforge-user", encoding="utf-8")
    child.write_text("GATE_TASK_ID: ruleforge-payment", encoding="utf-8")
    value = payload(tmp_path, "unused")
    value["transcript_path"] = str(parent)
    value["agent_transcript_path"] = str(child)

    result = evaluate(
        value,
        tasks,
        state_dir=tmp_path / "state",
        log_path=tmp_path / "gate.jsonl",
        runner=lambda _cmd, _cwd: (0, "ok"),
        clock=lambda: 10,
    )

    assert result == {"decision": "approve", "verdict": "pass"}
    assert logs(tmp_path / "gate.jsonl")[0]["task_id"] == "ruleforge-payment"


def test_initial_stop_hook_payload_can_block(tmp_path: Path) -> None:
    stopped = payload(tmp_path)
    stopped["stop_hook_active"] = False
    result = evaluate(stopped, TASKS, state_dir=tmp_path / "state", log_path=tmp_path / "gate.jsonl", runner=lambda _cmd, _cwd: (1, "must not run"), clock=lambda: 0)
    assert result["decision"] == "block"


def test_direct_script_bootstraps_repo_import_from_isolated_cwd(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "generic_packet": {"write_paths": ["tests/test_{domain}_rule.py"]},
                "tasks": [{"task_id": "ruleforge-payment", "slot_values": {"domain": "payment"}}],
            }
        ),
        encoding="utf-8",
    )
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("ruleforge-payment", encoding="utf-8")
    focused = tmp_path / "tests" / "test_payment_rule.py"
    focused.parent.mkdir()
    focused.write_text("def test_payment_rule():\n    assert True\n", encoding="utf-8")
    log = tmp_path / "gate.jsonl"
    hook = Path(__file__).resolve().parents[2] / "harness" / "hooks" / "completion_gate.py"

    result = subprocess.run(
        [sys.executable, str(hook), "--manifest", str(manifest), "--state-dir", str(tmp_path / "state"), "--log-path", str(log)],
        cwd=tmp_path,
        input=json.dumps({"session_id": "direct-script", "transcript_path": str(transcript), "cwd": str(tmp_path)}),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"decision": "approve"}
    assert logs(log)[0]["verdict"] == "pass"
