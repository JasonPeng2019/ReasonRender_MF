from __future__ import annotations

import json
from pathlib import Path

from harness.four_worker_plan import build_overlap_ledger, freeze_worker_plans, manifest_sha256
from harness.source_policy import direct_overlap_reads, direct_unlisted_source_reads, observed_local_read_set


def test_absolute_windows_command_cannot_hide_an_overlap_read(tmp_path: Path) -> None:
    entry = next(
        item
        for item in build_overlap_ledger(freeze_worker_plans(), manifest_sha256(freeze_worker_plans()))
        if item.canonical_path == "ruleforge/evaluator.py"
    )
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"command": r"Get-Content C:\\work\\ruleforge\\evaluator.py"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert [item.canonical_path for item in direct_overlap_reads(stream, "worker-02", [entry])] == [
        "ruleforge/evaluator.py"
    ]


def test_unlisted_direct_source_read_is_rejected_for_any_arm(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"command": "Get-Content ruleforge/domain.py; Get-Content ruleforge/registry.py"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    violations = direct_unlisted_source_reads(
        stream,
        "worker-04",
        ("ruleforge/domain.py",),
        ("ruleforge/domain.py", "ruleforge/registry.py"),
    )

    assert [item.canonical_path for item in violations] == ["ruleforge/registry.py"]


def test_failed_sourceless_path_attempt_is_not_an_actual_source_read(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "command": "Get-Content ruleforge/registry.py",
                    "exit_code": 1,
                    "status": "failed",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert not direct_unlisted_source_reads(
        stream,
        "worker-01",
        ("ruleforge/domain.py",),
        ("ruleforge/domain.py", "ruleforge/registry.py"),
    )
    assert observed_local_read_set(stream, "worker-01", ("ruleforge/registry.py",))["observed_local_read_paths"] == []


def test_observed_local_read_set_retains_only_declared_direct_source_reads(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join(
            (
                json.dumps({"type": "item.completed", "item": {"command": "Get-Content ruleforge/domain.py"}}),
                json.dumps({"type": "item.completed", "item": {"command": "Get-Content ruleforge/registry.py"}}),
                json.dumps({"type": "item.completed", "item": {"command": "python -m pytest -q"}}),
            )
        ),
        encoding="utf-8",
    )

    result = observed_local_read_set(stream, "worker-01", ("ruleforge/domain.py",))

    assert result["worker_id"] == "worker-01"
    assert result["allowed_local_read_paths"] == ["ruleforge/domain.py"]
    assert result["observed_local_read_paths"] == ["ruleforge/domain.py"]
    assert result["valid"] is True


def test_qwen_completed_shell_read_is_retained_as_source_evidence(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join(
            (
                json.dumps({
                    "type": "assistant",
                    "message": {"content": [{
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "run_shell_command",
                        "input": {"command": "Get-Content ruleforge/domain.py"},
                    }]},
                }),
                json.dumps({
                    "type": "user",
                    "message": {"content": [{
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "is_error": False,
                        "content": "Exit code: 0",
                    }]},
                }),
            )
        ) + "\n",
        encoding="utf-8",
    )

    result = observed_local_read_set(stream, "worker-01", ("ruleforge/domain.py",))

    assert result["observed_local_read_paths"] == ["ruleforge/domain.py"]


def test_qwen_failed_shell_read_is_not_source_evidence(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join(
            (
                json.dumps({
                    "type": "assistant",
                    "message": {"content": [{
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "run_shell_command",
                        "input": {"command": "Get-Content ruleforge/registry.py"},
                    }]},
                }),
                json.dumps({
                    "type": "user",
                    "message": {"content": [{
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "is_error": False,
                        "content": "Command exited with code: 1",
                    }]},
                }),
            )
        ) + "\n",
        encoding="utf-8",
    )

    result = observed_local_read_set(stream, "worker-01", ("ruleforge/registry.py",))

    assert result["observed_local_read_paths"] == []


def test_qwen_native_read_file_is_retained_as_source_evidence(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join(
            (
                json.dumps({
                    "type": "assistant",
                    "message": {"content": [{
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "read_file",
                        "input": {"file_path": r"C:\work\ruleforge\domain.py"},
                    }]},
                }),
                json.dumps({
                    "type": "user",
                    "message": {"content": [{
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "is_error": False,
                        "content": "source text containing Exit code: 1 is still a successful native read",
                    }]},
                }),
            )
        ) + "\n",
        encoding="utf-8",
    )

    result = observed_local_read_set(stream, "worker-01", ("ruleforge/domain.py",))
    violations = direct_unlisted_source_reads(
        stream,
        "worker-01",
        (),
        ("ruleforge/domain.py",),
    )

    assert result["observed_local_read_paths"] == ["ruleforge/domain.py"]
    assert [item.canonical_path for item in violations] == ["ruleforge/domain.py"]


def test_qwen_failed_native_read_file_is_not_source_evidence(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join(
            (
                json.dumps({
                    "type": "assistant",
                    "message": {"content": [{
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "read_file",
                        "input": {"file_path": r"C:\missing\ruleforge\registry.py"},
                    }]},
                }),
                json.dumps({
                    "type": "user",
                    "message": {"content": [{
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "is_error": True,
                        "content": "File not found",
                    }]},
                }),
            )
        ) + "\n",
        encoding="utf-8",
    )

    result = observed_local_read_set(stream, "worker-01", ("ruleforge/registry.py",))

    assert result["observed_local_read_paths"] == []


def test_qwen_native_grep_search_is_retained_as_source_evidence(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join(
            (
                json.dumps({
                    "type": "assistant",
                    "message": {"content": [{
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "grep_search",
                        "input": {
                            "pattern": "operational.control_129",
                            "path": r"C:\work\ruleforge\policy_catalog.py",
                        },
                    }]},
                }),
                json.dumps({
                    "type": "user",
                    "message": {"content": [{
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "is_error": False,
                        "content": "Found 1 match",
                    }]},
                }),
            )
        ) + "\n",
        encoding="utf-8",
    )

    result = observed_local_read_set(stream, "worker-01", ("ruleforge/policy_catalog.py",))

    assert result["observed_local_read_paths"] == ["ruleforge/policy_catalog.py"]
