import json
import subprocess
import sys
from pathlib import Path

import pytest
from harness.collect import CollectionError, collect_stream

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "claude_stream"
REAL_CHILD_STREAM = ROOT / "harness" / "artifacts" / "WP0-disallowed-tools-child" / "stream.jsonl"


def read_turns(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_events(path: Path, events: list[object]) -> None:
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def test_complete_cached_stream_writes_ordered_aggregate_artifacts(tmp_path: Path) -> None:
    output_dir = tmp_path / "cached"

    usage = collect_stream(FIXTURES / "complete_cached.jsonl", output_dir)

    assert usage["schema_version"] == 1
    assert usage["completed"] is True
    assert usage["valid"] is True
    assert usage["invalid_reasons"] == []
    assert [session["session_id"] for session in usage["sessions"]] == [
        "session-a",
        "session-b",
    ]
    assert usage["totals"] == {
        "in_new": 130,
        "cache_read": 90,
        "cache_write": 5,
        "out": 18,
        "turns": 2,
        "tool_calls": 3,
        "tools_per_turn": 1.5,
    }

    turns = read_turns(output_dir / "turns.jsonl")
    assert [turn["turn"] for turn in turns] == [1, 2]
    assert [turn["tool_names"] for turn in turns] == [["Read", "Edit"], ["Bash"]]
    assert json.loads((output_dir / "usage.json").read_text(encoding="utf-8")) == usage


def test_real_task_child_usage_is_counted_once_with_auditable_roles_and_turn(tmp_path: Path) -> None:
    usage = collect_stream(REAL_CHILD_STREAM, tmp_path / "real-child")

    assert usage["valid"] is True
    assert usage["totals"] == {
        "in_new": 8,
        "cache_read": 51167,
        "cache_write": 11861,
        "out": 345,
        "turns": 4,
        "tool_calls": 1,
        "tools_per_turn": 0.25,
    }
    assert [session["role"] for session in usage["sessions"]] == [
        "orchestrator",
        "worker",
    ]
    child = usage["sessions"][1]
    assert child["session_id"] == "worker:toolu_013jmjzKekN46icav7GRtAqE"
    assert child["agent_type"] == "general-purpose"
    assert child["turns"] == 1
    assert child["out"] == 338

    turns = read_turns(tmp_path / "real-child" / "turns.jsonl")
    child_turns = [turn for turn in turns if turn["session_id"] == child["session_id"]]
    assert len(child_turns) == 1
    assert child_turns[0]["source"] == "user.tool_use_result"
    assert child_turns[0]["total_tool_use_count"] == 0


def test_async_task_launch_notification_is_ignored(tmp_path: Path) -> None:
    output_dir = tmp_path / "async-task-launch-output"

    usage = collect_stream(FIXTURES / "async_task_launch.jsonl", output_dir)

    assert usage["valid"] is True
    assert usage["totals"] == {
        "in_new": 1,
        "cache_read": 1,
        "cache_write": 0,
        "out": 1,
        "turns": 1,
        "tool_calls": 0,
        "tools_per_turn": 0.0,
    }
    assert [session["session_id"] for session in usage["sessions"]] == ["parent"]


def test_shared_parent_session_splits_task_children_and_ignores_later_child_total(tmp_path: Path) -> None:
    stream_path = tmp_path / "shared-session.jsonl"
    usage = {"input_tokens": 1, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 2, "output_tokens": 3}
    child_usage = {"input_tokens": 2, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 4, "output_tokens": 5}
    write_events(
        stream_path,
        [
            {"type": "assistant", "session_id": "parent", "message": {"usage": usage, "content": [{"type": "tool_use", "name": "Task"}]}},
            {"type": "system", "subtype": "task_started", "task_id": "agent-1", "tool_use_id": "task-tool-1"},
            {"type": "assistant", "session_id": "parent", "parent_tool_use_id": "task-tool-1", "message": {"usage": child_usage, "content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "tool_use_result": {"agentId": "agent-1", "agentType": "general-purpose", "totalToolUseCount": 99, "usage": {"input_tokens": 99, "cache_read_input_tokens": 99, "cache_creation_input_tokens": 99, "output_tokens": 99}}},
            {"type": "result", "subtype": "success", "session_id": "parent"},
        ],
    )

    result = collect_stream(stream_path, tmp_path / "shared-session-output")

    assert [session["session_id"] for session in result["sessions"]] == ["parent", "worker:task-tool-1"]
    assert [session["role"] for session in result["sessions"]] == ["orchestrator", "worker"]
    assert result["totals"] == {"in_new": 3, "cache_read": 30, "cache_write": 6, "out": 8, "turns": 2, "tool_calls": 2, "tools_per_turn": 1.0}


def test_duplicate_child_record_does_not_double_count_usage(tmp_path: Path) -> None:
    parent = {
        "type": "assistant",
        "session_id": "parent",
        "message": {
            "usage": {
                "input_tokens": 1,
                "cache_read_input_tokens": 1,
                "cache_creation_input_tokens": 0,
                "output_tokens": 1,
            },
            "content": [],
        },
    }
    child = {
        "type": "user",
        "tool_use_result": {
            "agentId": "child",
            "agentType": "general-purpose",
            "totalToolUseCount": 2,
            "usage": {
                "input_tokens": 3,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 4,
                "output_tokens": 5,
            },
        },
    }
    stream_path = tmp_path / "duplicate-child.jsonl"
    write_events(
        stream_path,
        [
            parent,
            child,
            child,
            {"type": "system", "subtype": "task_notification", "usage": {"total_tokens": 999}},
            {"type": "result", "subtype": "success", "session_id": "parent"},
        ],
    )

    usage = collect_stream(stream_path, tmp_path / "duplicate-child-output")

    assert usage["totals"] == {
        "in_new": 4,
        "cache_read": 1,
        "cache_write": 4,
        "out": 6,
        "turns": 2,
        "tool_calls": 2,
        "tools_per_turn": 1.0,
    }


def test_malformed_child_usage_is_rejected_with_a_line_number(tmp_path: Path) -> None:
    stream_path = tmp_path / "invalid-child.jsonl"
    write_events(
        stream_path,
        [
            {
                "type": "user",
                "tool_use_result": {
                    "agentId": "child",
                    "agentType": "general-purpose",
                    "totalToolUseCount": 1,
                    "usage": {
                        "input_tokens": 1,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
            }
        ],
    )

    with pytest.raises(CollectionError, match=r"line 1: output_tokens must be a non-negative integer"):
        collect_stream(stream_path, tmp_path / "invalid-child-output")


def test_zero_cache_stream_writes_invalid_artifacts_and_cli_fails(tmp_path: Path) -> None:
    output_dir = tmp_path / "zero-cache"
    command = [
        sys.executable,
        str(ROOT / "harness" / "collect.py"),
        str(FIXTURES / "complete_zero_cache.jsonl"),
        str(output_dir),
    ]

    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 2
    usage = json.loads((output_dir / "usage.json").read_text(encoding="utf-8"))
    assert usage["completed"] is True
    assert usage["valid"] is False
    assert "zero_cache_read" in usage["invalid_reasons"]
    assert (output_dir / "turns.jsonl").exists()


def test_incomplete_stream_is_invalid_but_still_writes_artifacts(tmp_path: Path) -> None:
    output_dir = tmp_path / "incomplete"

    usage = collect_stream(FIXTURES / "incomplete.jsonl", output_dir)

    assert usage["completed"] is False
    assert usage["valid"] is False
    assert usage["invalid_reasons"] == ["incomplete"]
    assert (output_dir / "usage.json").exists()
    assert (output_dir / "turns.jsonl").exists()


def test_malformed_stream_reports_one_based_line_number(tmp_path: Path) -> None:
    with pytest.raises(CollectionError, match=r"line 2: malformed JSON"):
        collect_stream(FIXTURES / "malformed.jsonl", tmp_path / "malformed")


def test_non_object_event_reports_line_number(tmp_path: Path) -> None:
    stream_path = tmp_path / "non-object.jsonl"
    write_events(stream_path, [[], {"type": "result", "subtype": "success"}])

    with pytest.raises(CollectionError, match=r"line 1: event must be a JSON object"):
        collect_stream(stream_path, tmp_path / "non-object-output")


@pytest.mark.parametrize(
    ("usage", "message"),
    [
        ({"input_tokens": 1}, "cache_read_input_tokens must be a non-negative integer"),
        (
            {
                "input_tokens": "one",
                "cache_read_input_tokens": 1,
                "cache_creation_input_tokens": 0,
                "output_tokens": 1,
            },
            "input_tokens must be a non-negative integer",
        ),
    ],
)
def test_missing_or_invalid_usage_is_rejected(
    tmp_path: Path, usage: dict[str, object], message: str
) -> None:
    stream_path = tmp_path / "invalid-usage.jsonl"
    write_events(
        stream_path,
        [
            {
                "type": "assistant",
                "session_id": "session-a",
                "message": {"usage": usage, "content": []},
            }
        ],
    )

    with pytest.raises(CollectionError, match=rf"line 1: assistant message\.usage|{message}"):
        collect_stream(stream_path, tmp_path / "invalid-usage-output")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not-a-list", "assistant message.content must be a list"),
        ([{"type": "tool_use"}], "tool_use block 1 name must be a string"),
    ],
)
def test_malformed_content_or_tool_name_is_rejected(
    tmp_path: Path, content: object, message: str
) -> None:
    stream_path = tmp_path / "invalid-content.jsonl"
    write_events(
        stream_path,
        [
            {
                "type": "assistant",
                "session_id": "session-a",
                "message": {
                    "usage": {
                        "input_tokens": 1,
                        "cache_read_input_tokens": 1,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 1,
                    },
                    "content": content,
                },
            }
        ],
    )

    with pytest.raises(CollectionError, match=rf"line 1: {message}"):
        collect_stream(stream_path, tmp_path / "invalid-content-output")


def test_success_result_must_reference_an_observed_session(tmp_path: Path) -> None:
    stream_path = tmp_path / "unknown-result-session.jsonl"
    write_events(
        stream_path,
        [
            {
                "type": "assistant",
                "session_id": "session-a",
                "message": {
                    "usage": {
                        "input_tokens": 1,
                        "cache_read_input_tokens": 1,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 1,
                    },
                    "content": [],
                },
            },
            {"type": "result", "subtype": "success", "session_id": "session-b"},
        ],
    )

    with pytest.raises(
        CollectionError,
        match=r"line 2: successful result session_id 'session-b' was not observed",
    ):
        collect_stream(stream_path, tmp_path / "unknown-result-session-output")


def test_sessionless_success_requires_one_observed_session(tmp_path: Path) -> None:
    stream_path = tmp_path / "ambiguous-result-session.jsonl"
    assistant = {
        "type": "assistant",
        "message": {
            "usage": {
                "input_tokens": 1,
                "cache_read_input_tokens": 1,
                "cache_creation_input_tokens": 0,
                "output_tokens": 1,
            },
            "content": [],
        },
    }
    write_events(
        stream_path,
        [
            {**assistant, "session_id": "session-a"},
            {**assistant, "session_id": "session-b"},
            {"type": "result", "subtype": "success"},
        ],
    )

    with pytest.raises(
        CollectionError,
        match=r"line 3: successful result without session_id requires exactly one observed assistant session",
    ):
        collect_stream(stream_path, tmp_path / "ambiguous-result-session-output")


def test_later_failed_result_invalidates_an_earlier_success(tmp_path: Path) -> None:
    stream_path = tmp_path / "later-failure.jsonl"
    write_events(
        stream_path,
        [
            {
                "type": "assistant",
                "session_id": "session-a",
                "message": {
                    "usage": {
                        "input_tokens": 1,
                        "cache_read_input_tokens": 1,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 1,
                    },
                    "content": [],
                },
            },
            {"type": "result", "subtype": "success", "session_id": "session-a"},
            {"type": "result", "subtype": "error", "is_error": True},
        ],
    )

    usage = collect_stream(stream_path, tmp_path / "later-failure-output")

    assert usage["completed"] is True
    assert usage["valid"] is False
    assert usage["invalid_reasons"] == ["result_not_success"]


def test_success_subtype_with_error_flag_is_invalid(tmp_path: Path) -> None:
    stream_path = tmp_path / "contradictory-result.jsonl"
    write_events(
        stream_path,
        [
            {
                "type": "assistant",
                "session_id": "session-a",
                "message": {
                    "usage": {
                        "input_tokens": 1,
                        "cache_read_input_tokens": 1,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 1,
                    },
                    "content": [],
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "terminal_reason": "api_error",
            },
        ],
    )

    usage = collect_stream(stream_path, tmp_path / "contradictory-result-output")

    assert usage["completed"] is True
    assert usage["valid"] is False
    assert usage["invalid_reasons"] == ["result_not_success"]


@pytest.mark.parametrize(
    ("fixture_name", "expected_status"),
    [
        ("complete_cached.jsonl", 0),
        ("complete_zero_cache.jsonl", 2),
        ("malformed.jsonl", 2),
    ],
)
def test_cli_returns_exact_success_and_failure_statuses(
    tmp_path: Path, fixture_name: str, expected_status: int
) -> None:
    command = [
        sys.executable,
        str(ROOT / "harness" / "collect.py"),
        str(FIXTURES / fixture_name),
        str(tmp_path / fixture_name.removesuffix(".jsonl")),
    ]

    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == expected_status
