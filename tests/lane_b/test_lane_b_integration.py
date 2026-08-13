from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from rrc.contract import ModelRole, RunContext, Slots, Spec
from rrc.everos import EverOSClient
from rrc.memory import task_case_shape
from rrc.model import CodexModel, parse_codex_jsonl
from rrc.pipeline.template import templatize
from rrc.store import SQLiteTemplateStore
from rrc.workload import two_task_workload


class FakeEverOS(EverOSClient):
    def __init__(self) -> None:
        super().__init__("http://fake.invalid")
        self.indexed_ref: str | None = None
        self.indexed_shapes: list[str] = []
        self.search_args: list[tuple[str, int | None, float | None]] = []
        self.wait_calls = 0

    def index(self, case_shape: str, external_ref: str) -> None:
        self.indexed_ref = external_ref
        self.indexed_shapes.append(case_shape)

    def search(
        self,
        case_shape: str,
        *,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> list[tuple[str, float]]:
        self.search_args.append((case_shape, top_k, min_score))
        return [] if self.indexed_ref is None else [(self.indexed_ref, 0.91)]

    def wait_for_index(self, timeout: float = 30.0, poll_interval: float = 0.5) -> None:
        del timeout, poll_interval
        self.wait_calls += 1


def _spec_json() -> str:
    return json.dumps(
        {
            "plan": "Implement return_two and always return 2.",
            "signature": "def return_two(value: int) -> int",
            "contract": "Return the constant 2 for every integer input.",
            "tests": ["def test_behavior():\n    assert return_two(99) == 2"],
            "slots": {
                "entity": None,
                "identifiers": ["return_two"],
                "types": [],
                "fields": [],
                "constants": ["2"],
                "edge_values": [],
            },
        },
        separators=(",", ":"),
    )


def test_lane_b_store_round_trips_the_canonical_template_bundle(tmp_path: Path) -> None:
    store = SQLiteTemplateStore(tmp_path / "templates.sqlite")
    template = templatize(
        Spec(
            "Implement return_two and always return 2.",
            "def return_two(value: int) -> int",
            "Return the constant 2 for every integer input.",
            ("def test_behavior():\n    assert return_two(99) == 2",),
            Slots(identifiers=("return_two",), constants=("2",)),
        ),
        slot_values=(("function", "return_two"), ("number", "2")),
        primary="return_two",
    )
    store.put(template)
    assert store.get(template.external_ref) == template


def test_case_shape_removes_values_but_is_stable_across_instances() -> None:
    first, second = two_task_workload()
    first_shape = task_case_shape(first)
    second_shape = task_case_shape(second)

    assert first_shape == second_shape
    assert "return_two" not in first_shape
    assert "return_three" not in second_shape
    assert "RRC_SLOT_VALUES" not in first_shape


def test_codex_jsonl_parser_preserves_split_usage() -> None:
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
                        "input_tokens": 11,
                        "cached_input_tokens": 0,
                        "output_tokens": 7,
                        "reasoning_output_tokens": 0,
                        "total_tokens": 18,
                    },
                }
            ),
        )
    )

    text, usage = parse_codex_jsonl(stdout)

    assert text == "artifact"
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (11, 7, 18)


def test_codex_jsonl_parser_rejects_tools_errors_or_duplicate_finals() -> None:
    base = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "one"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    mutations = (
        [*base[:2], {"type": "item.completed", "item": {"type": "command_execution"}}, *base[2:]],
        [*base[:3], base[2], base[3]],
        [*base[:2], {"type": "error", "message": "failed"}, *base[2:]],
        [*base, base[3]],
    )
    for rows in mutations:
        with pytest.raises(ValueError):
            parse_codex_jsonl("\n".join(json.dumps(row) for row in rows))


def test_codex_model_uses_empty_stage_cwd_stdin_and_frozen_zero_tool_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        cwd_value = kwargs["cwd"]
        assert isinstance(cwd_value, (str, Path))
        cwd = Path(cwd_value)
        captured.update(
            argv=argv,
            input=kwargs.get("input"),
            cwd=cwd,
            children=tuple(cwd.iterdir()),
        )
        stdout = "\n".join(
            json.dumps(row)
            for row in (
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "artifact"},
                },
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 2,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 1,
                        "reasoning_output_tokens": 0,
                    },
                },
            )
        )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    result = CodexModel(executable="codex-test").complete(
        ModelRole.SMALL, "PROMPT", RunContext("cold", "task-1", "owner"), "implement"
    )
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert result.text == "artifact"
    assert captured["input"] == "PROMPT"
    assert captured["children"] == ()
    assert "--ignore-user-config" in argv
    assert argv.count("--disable") == 3
    assert 'model_reasoning_effort="low"' in argv
    assert 'service_tier="priority"' in argv
    assert argv[-1] == "-"
