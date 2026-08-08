from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from rrc.contract import ModelRole, RunContext
from rrc.model import CodexModel
from rrc.pipeline.prompts import spec_prompt
from rrc.workload import two_task_workload


def test_codex_completion_ignores_workspace_rules_and_is_ephemeral(monkeypatch) -> None:
    captured: list[list[str]] = []
    captured_schema: list[dict[str, Any]] = []
    stdout = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "artifact"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                }
            ),
        )
    )

    def fake_run(command, *, capture_output, text, check):
        assert capture_output is True and text is True and check is False
        captured.append(command)
        schema_path = Path(command[command.index("--output-schema") + 1])
        captured_schema.append(json.loads(schema_path.read_text()))
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    model = CodexModel(strong_model="test-strong")

    completion = model.complete(
        ModelRole.STRONG,
        spec_prompt(two_task_workload()[0]),
        RunContext("cold", "task"),
        "spec",
    )

    assert completion.text == "artifact"
    assert len(captured) == 1
    assert "--ignore-rules" in captured[0]
    assert "--ephemeral" in captured[0]
    assert "--output-schema" in captured[0]
    slots = captured_schema[0]["properties"]["slots"]["properties"]
    assert captured_schema[0]["properties"]["signature"]["enum"] == [
        "def return_two(value: int) -> int"
    ]
    assert slots["entity"] == {"type": "null"}
    assert slots["identifiers"]["items"]["enum"] == ["return_two"]
    assert slots["types"]["maxItems"] == 0
    assert slots["fields"]["maxItems"] == 0
    assert slots["constants"]["items"]["enum"] == ["2"]
    assert slots["edge_values"]["maxItems"] == 0
    assert captured_schema[0]["properties"]["tests"]["items"]["enum"] == [
        "def test_behavior():\n    assert return_two(99) == 2"
    ]


def test_non_spec_completion_does_not_request_json_schema(monkeypatch) -> None:
    captured: list[list[str]] = []
    stdout = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "def f(): pass"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
            ),
        )
    )

    def fake_run(command, *, capture_output, text, check):
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    CodexModel(strong_model="test").complete(
        ModelRole.SMALL,
        "return code",
        RunContext("cold", "task"),
        "implement",
    )

    assert "--output-schema" not in captured[0]


def test_codex_completion_records_raw_stage_artifacts(monkeypatch, tmp_path: Path) -> None:
    stdout = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "def f():\n    return 1"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
                }
            ),
        )
    )

    def fake_run(command, *, capture_output, text, check):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    artifact_log = tmp_path / "model-events.jsonl"
    model = CodexModel(strong_model="strong", artifact_log=artifact_log)

    model.complete(
        ModelRole.SMALL,
        "implement this",
        RunContext("warm", "task-7"),
        "implement",
    )

    record = json.loads(artifact_log.read_text())
    assert record == {
        "arm": "warm",
        "task_id": "task-7",
        "stage": "implement",
        "role": "small",
        "model": "strong",
        "prompt": "implement this",
        "response": "def f():\n    return 1",
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }
