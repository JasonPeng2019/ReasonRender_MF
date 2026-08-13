from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from rrc.contract import ModelRole, RunContext
from rrc.model import CodexModel, _spec_output_schema
from rrc.pipeline.prompts import spec_prompt
from rrc.workload import two_task_workload


def test_codex_completion_rejects_missing_cache_write_usage(monkeypatch) -> None:
    stdout = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "artifact"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 3,
                        "cached_input_tokens": 1,
                        "output_tokens": 2,
                        "reasoning_output_tokens": 1,
                        "total_tokens": 5,
                    },
                }
            ),
        )
    )

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="cache-write usage is missing"):
        CodexModel(strong_model="test").complete(
            ModelRole.STRONG,
            "return an artifact",
            RunContext("cold", "task", "owner"),
            "spec",
        )


def test_codex_completion_forwards_the_explicit_closed_environment(monkeypatch) -> None:
    stdout = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 3,
                        "cached_input_tokens": 1,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 2,
                        "reasoning_output_tokens": 1,
                        "total_tokens": 5,
                    },
                }
            ),
        )
    )
    expected = {"HOME": "/closed/home", "PATH": "/usr/bin:/bin"}

    def fake_run(command, **kwargs):
        assert kwargs["env"] == expected
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    completion = CodexModel(strong_model="test", environment=expected).complete(
        ModelRole.STRONG,
        "return an artifact",
        RunContext("cold", "task", "owner"),
        "metadata_fill",
    )
    assert completion.text == "ok"


def test_codex_completion_ignores_workspace_rules_and_is_ephemeral(monkeypatch) -> None:
    captured: list[list[str]] = []
    captured_schema: list[dict[str, Any]] = []
    stdout = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "artifact"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 3,
                        "cached_input_tokens": 1,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 2,
                        "reasoning_output_tokens": 1,
                        "total_tokens": 5,
                    },
                }
            ),
        )
    )

    def fake_run(command, **kwargs):
        assert kwargs["capture_output"] is True and kwargs["text"] is True
        assert kwargs["check"] is False
        captured.append(command)
        schema_path = Path(command[command.index("--output-schema") + 1])
        captured_schema.append(json.loads(schema_path.read_text()))
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    model = CodexModel(strong_model="test-strong")

    completion = model.complete(
        ModelRole.STRONG,
        spec_prompt(two_task_workload()[0]),
        RunContext("cold", "task", "owner"),
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
    assert "values" not in slots
    assert captured_schema[0]["properties"]["tests"]["items"]["enum"] == [
        "def test_behavior():\n    assert return_two(99) == 2"
    ]


def test_non_spec_completion_does_not_request_json_schema(monkeypatch) -> None:
    captured: list[list[str]] = []
    stdout = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "def f(): pass"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 1,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 1,
                        "reasoning_output_tokens": 0,
                        "total_tokens": 2,
                    },
                }
            ),
        )
    )

    def fake_run(command, **kwargs):
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    CodexModel(strong_model="test").complete(
        ModelRole.SMALL,
        "return code",
        RunContext("cold", "task", "owner"),
        "implement",
    )

    assert "--output-schema" not in captured[0]


def test_spec_schema_classifies_every_remaining_controller_binding_as_constant() -> None:
    prompt = (
        'RRC_SHAPE: {"arg_types":["str"],"arity":1,"fields":[]}\n'
        "RRC_SLOT_VALUES: "
        '{"active_value":"approved","delimiter":"~",'
        '"function":"select_names"}'
    )

    schema = _spec_output_schema(prompt)

    assert schema is not None
    slots = schema["properties"]["slots"]["properties"]
    assert slots["identifiers"]["items"]["enum"] == ["select_names"]
    assert slots["constants"]["items"]["enum"] == ["approved", "~"]


def test_codex_completion_records_raw_stage_artifacts(monkeypatch, tmp_path: Path) -> None:
    stdout = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "def f():\n    return 1"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 3,
                        "cached_input_tokens": 1,
                        "cache_write_input_tokens": 1,
                        "output_tokens": 4,
                        "reasoning_output_tokens": 2,
                        "total_tokens": 7,
                    },
                }
            ),
        )
    )

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    artifact_log = tmp_path / "model-events.jsonl"
    model = CodexModel(
        strong_model="strong",
        small_model="strong",
        artifact_log=artifact_log,
    )

    model.complete(
        ModelRole.SMALL,
        "implement this",
        RunContext("warm", "task-7", "owner"),
        "implement",
    )

    record = json.loads(artifact_log.read_text())
    assert record == {
        "arm": "warm",
        "task_id": "task-7",
        "stage": "implement",
        "role": "small",
        "model": "strong",
        "requested_reasoning": "low",
        "requested_service_tier": "priority",
        "prompt": "implement this",
        "response": "def f():\n    return 1",
        "transcript_sha256": __import__("hashlib").sha256(stdout.encode()).hexdigest(),
        "usage": {
            "cached_input_tokens": 1,
            "cache_write_input_tokens": 1,
            "prompt_tokens": 3,
            "completion_tokens": 4,
            "reasoning_output_tokens": 2,
            "total_tokens": 7,
        },
    }
