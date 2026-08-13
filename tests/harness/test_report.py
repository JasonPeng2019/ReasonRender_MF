from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness.report import ReportError, arm_report, render_round, report_round


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def arm(
    root: Path,
    name: str,
    *,
    passes: int,
    cache_read: int = 20,
    run_kind: str = "fixture",
    cm_event: str = "packet_insufficient",
) -> Path:
    path = root / name
    provider_arm = run_kind == "provider_arm"
    (path / "stream.jsonl").parent.mkdir(parents=True, exist_ok=True)
    stream_records = [{"type": "result"}]
    if provider_arm:
        stream_records = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Agent",
                            "input": {"run_in_background": True},
                        }
                        for _ in range(4)
                    ],
                },
            }
        ]
    (path / "stream.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in stream_records), encoding="utf-8"
    )
    orchestrator_tool_calls = 4 if provider_arm else 1
    worker_tool_calls = 0 if provider_arm else 2
    write(
        path / "usage.json",
        {
            "valid": True,
            "completed": True,
            "run_kind": run_kind,
            "provider_arm": name if run_kind == "provider_arm" else None,
            "wall_clock_ms": 250,
            "totals": {
                "in_new": 10,
                "cache_read": cache_read,
                "cache_write": 4,
                "out": 5,
                "turns": 2,
                "tool_calls": orchestrator_tool_calls + worker_tool_calls,
            },
            "sessions": [
                {"role": "orchestrator", "in_new": 3, "cache_read": 4, "cache_write": 1, "out": 2, "turns": 1, "tool_calls": orchestrator_tool_calls},
                {"session_id": "worker-1", "in_new": 7, "cache_read": cache_read - 4, "cache_write": 3, "out": 3, "turns": 1, "tool_calls": worker_tool_calls},
            ],
        },
    )
    gates = [{"session_id": "a", "task_id": "first", "cycle": 0, "verdict": "pass"}]
    if passes > 1:
        gates.extend(
            [
                {"session_id": "b", "task_id": "retried", "cycle": 1, "verdict": "block"},
                {"session_id": "b", "task_id": "retried", "cycle": 1, "verdict": "pass"},
            ]
        )
    (path / "gate.jsonl").write_text("".join(json.dumps(item) + "\n" for item in gates), encoding="utf-8")
    (path / "cm.jsonl").write_text(json.dumps({"event": cm_event}) + "\n", encoding="utf-8")
    if provider_arm:
        (path / "turns.jsonl").write_text(
            json.dumps(
                {
                    "role": "orchestrator",
                    "tool_names": ["Agent"] * 4,
                    "tool_calls": 4,
                }
            )
            + "\n",
            encoding="utf-8",
        )
    return path


def test_report_suppresses_provider_comparison_after_non_background_stream_dispatch(
    tmp_path: Path,
) -> None:
    raw = arm(tmp_path, "raw", passes=1, run_kind="provider_arm")
    full = arm(tmp_path, "full", passes=1, run_kind="provider_arm")
    records = [json.loads(line) for line in (raw / "stream.jsonl").read_text().splitlines()]
    records[0]["message"]["content"][0]["input"]["run_in_background"] = False
    (raw / "stream.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    result = report_round({"raw": raw, "full": full})

    assert result["comparison"] is None
    assert any(
        "run_in_background:true" in failure
        for failure in result["arms"]["raw"]["validation"]["failures"]
    )


def test_report_suppresses_provider_comparison_after_pre_dispatch_parent_text(
    tmp_path: Path,
) -> None:
    raw = arm(tmp_path, "raw", passes=1, run_kind="provider_arm")
    full = arm(tmp_path, "full", passes=1, run_kind="provider_arm")
    records = [json.loads(line) for line in (raw / "stream.jsonl").read_text().splitlines()]
    records.insert(
        0,
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Dispatching the children."}],
            },
        },
    )
    (raw / "stream.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    result = report_round({"raw": raw, "full": full})

    assert result["comparison"] is None
    assert any(
        "pre-dispatch parent text-only" in failure
        for failure in result["arms"]["raw"]["validation"]["failures"]
    )


def test_report_accepts_four_split_background_parent_dispatch_records(tmp_path: Path) -> None:
    raw = arm(tmp_path, "raw", passes=1, run_kind="provider_arm")
    full = arm(tmp_path, "full", passes=1, run_kind="provider_arm")
    passing_record = json.loads((raw / "stream.jsonl").read_text().splitlines()[0])
    split_records = [
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [tool_use]},
        }
        for tool_use in passing_record["message"]["content"]
    ]
    (raw / "stream.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in split_records), encoding="utf-8"
    )

    result = report_round({"raw": raw, "full": full})

    assert result["comparison"] == {"raw": 22.0, "full": 22.0}
    assert result["arms"]["raw"]["validation"]["stream_task_dispatches"] == 4
    assert result["arms"]["raw"]["validation"]["background_task_dispatches"] == 4
    assert result["arms"]["raw"]["validation"]["stream_parent_dispatch_records"] == 4
    assert result["arms"]["raw"]["validation"]["stream_dispatch_batch_sizes"] == [1, 1, 1, 1]
    assert "stream batches=1+1+1+1" in render_round(result)


def test_report_suppresses_comparison_after_text_before_four_split_dispatches(
    tmp_path: Path,
) -> None:
    raw = arm(tmp_path, "raw", passes=1, run_kind="provider_arm")
    full = arm(tmp_path, "full", passes=1, run_kind="provider_arm")
    passing_record = json.loads((raw / "stream.jsonl").read_text().splitlines()[0])
    split_records = [
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [tool_use]},
        }
        for tool_use in passing_record["message"]["content"]
    ]
    split_records.insert(
        1,
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Still working."}],
            },
        },
    )
    (raw / "stream.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in split_records), encoding="utf-8"
    )

    result = report_round({"raw": raw, "full": full})

    assert result["comparison"] is None
    assert any(
        "pre-dispatch parent text-only" in failure
        for failure in result["arms"]["raw"]["validation"]["failures"]
    )


def test_report_suppresses_comparison_for_unequal_completion(tmp_path: Path) -> None:
    result = report_round({"raw": arm(tmp_path, "raw", passes=2), "full": arm(tmp_path, "full", passes=1)})

    assert result["comparison"] is None
    assert result["suppression_reason"] == "gate pass counts differ across arms"
    assert "Billable comparison" not in render_round(result)
    assert "Comparison suppressed" in render_round(result)
    assert result["arms"]["raw"]["contextmesh_events"]["packet_insufficient"] == 1


def test_report_emits_weighted_headline_and_role_costs_when_completion_matches(tmp_path: Path) -> None:
    result = report_round(
        {
            "raw": arm(tmp_path, "raw", passes=2, run_kind="provider_arm"),
            "full": arm(
                tmp_path,
                "full",
                passes=2,
                run_kind="provider_arm",
                cm_event="digest_gate_hit",
            ),
        },
        output_ratio=2.0,
    )

    raw = result["arms"]["raw"]
    assert result["completion_equal"] is True
    assert result["comparison"] == {"raw": 27.0, "full": 27.0}
    assert raw["turns"] == 2
    assert raw["tool_calls"] == 4
    assert raw["tools_per_turn"] == 2.0
    assert raw["wall_clock_ms"] == 250
    assert raw["gate_ledger"] == {
        "passed_first_try": 1,
        "passed_after_cycles": {1: 1},
        "passed": 2,
        "failed": 0,
        "timed_out": 0,
        "unresolved": 0,
    }
    assert raw["role_billable"] == {"orchestrator": 8.65, "workers": 18.35}
    assert raw["contextmesh_events"]["digest_gate_timeout"] == 0
    assert "Billable comparison: raw=27.00, full=27.00" in render_round(result)


def test_report_suppresses_full_comparison_after_invalid_f2_turn_evidence(tmp_path: Path) -> None:
    arm(tmp_path, "raw", passes=1, run_kind="provider_arm")
    arm(tmp_path, "full", passes=1, run_kind="provider_arm")
    full_path = tmp_path / "full"
    (full_path / "turns.jsonl").write_text(
        "\n".join(
            json.dumps({"role": "orchestrator", "tool_names": ["Agent"] * 5})
            for _ in range(1)
        )
        + "\n",
        encoding="utf-8",
    )
    result = report_round(
        {
            "raw": tmp_path / "raw",
            "full": full_path,
        }
    )

    full = result["arms"]["full"]
    assert result["comparison"] is None
    assert "provider validation failed" in result["suppression_reason"]
    assert full["validation"]["publishable"] is False
    assert any(
        "F2 requires exactly four task calls across one to four orchestrator turns" in failure
        for failure in full["validation"]["failures"]
    )
    assert "Comparison suppressed" in render_round(result)


def test_report_suppresses_full_comparison_after_repeated_hash_read(tmp_path: Path) -> None:
    full = arm(
        tmp_path,
        "full",
        passes=1,
        run_kind="provider_arm",
        cm_event="digest_gate_hit",
    )
    with (full / "cm.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": "read_raw", "hash": "same"}) + "\n")
        stream.write(json.dumps({"event": "read_raw", "hash": "same"}) + "\n")

    result = report_round(
        {
            "raw": arm(tmp_path, "raw", passes=1, run_kind="provider_arm"),
            "full": full,
        }
    )

    assert result["comparison"] is None
    assert result["arms"]["full"]["validation"]["redundant_read_raw"] == 1


def test_report_retains_low_orchestrator_tool_rate_without_invalidating_f2(tmp_path: Path) -> None:
    raw = arm(tmp_path, "raw", passes=1, run_kind="provider_arm")
    full = arm(
        tmp_path,
        "full",
        passes=1,
        run_kind="provider_arm",
        cm_event="digest_gate_hit",
    )
    usage = json.loads((raw / "usage.json").read_text(encoding="utf-8"))
    usage["sessions"][0]["tool_calls"] = 2
    usage["totals"]["tool_calls"] = 2
    write(raw / "usage.json", usage)

    result = report_round({"raw": raw, "full": full})

    assert result["comparison"] == {"raw": 22.0, "full": 22.0}
    assert result["arms"]["raw"]["validation"]["orchestrator_tools_per_turn"] == 2.0


def test_report_accepts_contextmesh_provider_comparison_after_split_f2(tmp_path: Path) -> None:
    raw = arm(tmp_path, "raw", passes=1, run_kind="provider_arm")
    contextmesh = arm(tmp_path, "contextmesh", passes=1, run_kind="provider_arm")
    (contextmesh / "turns.jsonl").write_text(
        "\n".join(
            json.dumps({"role": "orchestrator", "tool_names": ["Task"] * 2})
            for _ in range(2)
        )
        + "\n",
        encoding="utf-8",
    )

    result = report_round({"raw": raw, "contextmesh": contextmesh})

    assert result["comparison"] == {"raw": 22.0, "contextmesh": 22.0}
    assert result["arms"]["contextmesh"]["validation"]["task_turns"] == 2


def test_report_keeps_f3_full_only_for_a_valid_contextmesh_provider(tmp_path: Path) -> None:
    result = report_round(
        {
            "raw": arm(tmp_path, "raw", passes=1, run_kind="provider_arm"),
            "contextmesh": arm(tmp_path, "contextmesh", passes=1, run_kind="provider_arm"),
        },
        output_ratio=2.0,
    )

    assert result["comparison"] == {"raw": 27.0, "contextmesh": 27.0}
    assert result["arms"]["contextmesh"]["validation"]["publishable"] is True


def test_report_includes_retained_summarizer_usage_in_billable_total(tmp_path: Path) -> None:
    path = arm(tmp_path, "contextmesh", passes=1, run_kind="provider_arm")
    with (path / "cm.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "event": "summarizer_usage",
                    "in_new": 3,
                    "cache_read": 20,
                    "cache_write": 2,
                    "out": 5,
                }
            )
            + "\n"
        )

    report = arm_report(path, output_ratio=2.0)

    assert report["agent_billable"] == 27.0
    assert report["summarizer_usage"] == {
        "in_new": 3,
        "cache_read": 20,
        "cache_write": 2,
        "out": 5,
    }
    assert report["summarizer_billable"] == 17.5
    assert report["billable"] == 44.5


def test_fixture_evidence_never_presents_a_provider_economics_comparison(tmp_path: Path) -> None:
    result = report_round({"raw": arm(tmp_path, "raw", passes=1), "full": arm(tmp_path, "full", passes=1)})

    assert result["comparison"] is None
    assert result["suppression_reason"] == "not all arms are provider-arm measurements"


def test_report_suppresses_provider_comparison_after_contextmesh_fail_open(tmp_path: Path) -> None:
    result = report_round(
        {
            "raw": arm(tmp_path, "raw", passes=1, run_kind="provider_arm", cm_event="no_contextmesh"),
            "contextmesh": arm(tmp_path, "contextmesh", passes=1, run_kind="provider_arm", cm_event="errors"),
        }
    )

    assert result["comparison"] is None
    assert result["suppression_reason"] == "ContextMesh recorded fail-open errors"
    assert result["arms"]["contextmesh"]["contextmesh_events"]["errors"] == 1


def test_report_suppresses_provider_comparison_after_summarizer_failure(tmp_path: Path) -> None:
    result = report_round(
        {
            "raw": arm(tmp_path, "raw", passes=1, run_kind="provider_arm", cm_event="no_contextmesh"),
            "contextmesh": arm(
                tmp_path,
                "contextmesh",
                passes=1,
                run_kind="provider_arm",
                cm_event="summarizer_failed",
            ),
        }
    )

    assert result["comparison"] is None
    assert result["suppression_reason"] == "ContextMesh recorded fail-open errors"


def test_report_accepts_the_explicit_raw_no_contextmesh_marker(tmp_path: Path) -> None:
    report = arm_report(arm(tmp_path, "raw", passes=1, cm_event="no_contextmesh"))

    assert report["contextmesh_events"]["no_contextmesh"] == 1


def test_report_accepts_digest_storage_evidence(tmp_path: Path) -> None:
    report = arm_report(arm(tmp_path, "full", passes=1, cm_event="digest_stored"))

    assert report["contextmesh_events"]["digest_stored"] == 1


def test_report_rejects_unknown_contextmesh_events(tmp_path: Path) -> None:
    path = arm(tmp_path, "raw", passes=1)
    (path / "cm.jsonl").write_text(
        '{"event":"packet_insufficient"}\n{"event":"unknown"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ReportError, match="unknown ContextMesh event"):
        arm_report(path)


def test_report_rejects_missing_raw_stream_or_invalid_cache_evidence(tmp_path: Path) -> None:
    path = arm(tmp_path, "raw", passes=1)
    (path / "stream.jsonl").unlink()
    with pytest.raises(ReportError, match="raw stream.jsonl"):
        arm_report(path)

    path = arm(tmp_path, "bad-cache", passes=1, cache_read=-1)
    with pytest.raises(ReportError, match="cache_read"):
        arm_report(path)


def test_report_rejects_malformed_raw_stream_evidence(tmp_path: Path) -> None:
    path = arm(tmp_path, "raw", passes=1)
    (path / "stream.jsonl").write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ReportError, match="invalid JSONL"):
        arm_report(path)


def test_report_rejects_empty_or_non_reconciling_session_evidence(tmp_path: Path) -> None:
    path = arm(tmp_path, "raw", passes=1)
    usage = json.loads((path / "usage.json").read_text(encoding="utf-8"))
    usage["sessions"] = []
    write(path / "usage.json", usage)
    with pytest.raises(ReportError, match="non-empty"):
        arm_report(path)

    path = arm(tmp_path, "mismatched", passes=1)
    usage = json.loads((path / "usage.json").read_text(encoding="utf-8"))
    usage["sessions"][1]["out"] -= 1
    write(path / "usage.json", usage)
    with pytest.raises(ReportError, match="does not reconcile"):
        arm_report(path)
