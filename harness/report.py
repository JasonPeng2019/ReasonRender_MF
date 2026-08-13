"""Produce validity-first three-arm reports from retained harness artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


class ReportError(ValueError):
    """Raised when an arm cannot support an auditable report."""


USAGE_FIELDS = ("in_new", "cache_read", "cache_write", "out", "turns", "tool_calls")
CM_EVENTS = (
    "no_contextmesh",
    "digest_hit",
    "read_raw",
    "digest_stored",
    "digest_rejected",
    "digest_trimmed",
    "directory_listing",
    "digest_gate_wait",
    "digest_gate_hit",
    "digest_gate_timeout",
    "packet_insufficient",
    "summarizer_usage",
    "summarizer_failed",
    "summarizer_timeout",
    "summarizer_invalid_json",
    "summarizer_unavailable",
    "errors",
)
TASK_TOOLS = frozenset(("Task", "Agent"))


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ReportError(f"missing required artifact: {path}") from error
    except json.JSONDecodeError as error:
        raise ReportError(f"invalid JSON artifact {path}: {error.msg}") from error
    if not isinstance(value, dict):
        raise ReportError(f"JSON artifact must be an object: {path}")
    return value


def _read_jsonl(path: Path) -> list[Mapping[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise ReportError(f"missing required artifact: {path}") from error
    result: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ReportError(f"invalid JSONL in {path} line {line_number}: {error.msg}") from error
        if not isinstance(value, dict):
            raise ReportError(f"JSONL object required in {path} line {line_number}")
        result.append(value)
    return result


def _count(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReportError(f"{label} must be a non-negative integer")
    return value


def _usage_counts(value: Mapping[str, Any], label: str) -> dict[str, int]:
    missing = [field for field in USAGE_FIELDS if field not in value]
    if missing:
        raise ReportError(f"{label} is missing required fields: {', '.join(missing)}")
    return {field: _count(value[field], f"{label}.{field}") for field in USAGE_FIELDS}


def _billable(counts: Mapping[str, int], output_ratio: float) -> float:
    return (
        counts["in_new"]
        + 0.1 * counts["cache_read"]
        + 1.25 * counts["cache_write"]
        + output_ratio * counts["out"]
    )


def _gate_ledger(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not records:
        raise ReportError("gate.jsonl must contain one terminal gate record per task")
    final_by_task: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(records):
        verdict = record.get("verdict")
        if not isinstance(verdict, str):
            raise ReportError(f"gate.jsonl record {index + 1} has no string verdict")
        task = record.get("task_id")
        session = record.get("session_id")
        if not isinstance(task, str) or not isinstance(session, str):
            raise ReportError(f"gate.jsonl record {index + 1} lacks task_id or session_id")
        _count(record.get("cycle"), f"gate.jsonl record {index + 1} cycle")
        final_by_task[task] = record

    after_cycles: dict[int, int] = {}
    first_try = failed = timed_out = unresolved = 0
    for record in final_by_task.values():
        verdict = record["verdict"]
        if verdict == "pass":
            cycle = _count(record["cycle"], "gate cycle")
            if cycle == 0:
                first_try += 1
            else:
                after_cycles[cycle] = after_cycles.get(cycle, 0) + 1
        elif verdict == "fail":
            failed += 1
        elif verdict == "fail_timeout":
            timed_out += 1
        elif verdict == "unresolved":
            unresolved += 1
        elif verdict == "block":
            raise ReportError("gate.jsonl has a task without a terminal verdict")
        else:
            raise ReportError(f"unsupported terminal gate verdict: {verdict}")
    return {
        "passed_first_try": first_try,
        "passed_after_cycles": after_cycles,
        "passed": first_try + sum(after_cycles.values()),
        "failed": failed,
        "timed_out": timed_out,
        "unresolved": unresolved,
    }


def _role_costs(
    sessions: object,
    totals: Mapping[str, int],
    output_ratio: float,
) -> dict[str, float]:
    split = {"orchestrator": 0.0, "workers": 0.0}
    if not isinstance(sessions, list) or not sessions:
        raise ReportError("usage.sessions must be a non-empty list")
    session_totals = {field: 0 for field in USAGE_FIELDS}
    for index, session in enumerate(sessions, start=1):
        if not isinstance(session, dict):
            raise ReportError(f"usage.sessions[{index}] must be an object")
        role = session.get("role", session.get("session_id", "worker"))
        if not isinstance(role, str):
            raise ReportError(f"usage.sessions[{index}] role must be a string")
        bucket = "orchestrator" if "orchestrator" in role.lower() else "workers"
        counts = _usage_counts(session, f"usage.sessions[{index}]")
        for field in USAGE_FIELDS:
            session_totals[field] += counts[field]
        split[bucket] += _billable(counts, output_ratio)
    if session_totals != dict(totals):
        raise ReportError("usage.sessions does not reconcile with usage.totals")
    return split


def _summarizer_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Aggregate retained digest-child usage without inventing a session."""
    totals = {"in_new": 0, "cache_read": 0, "cache_write": 0, "out": 0}
    for index, record in enumerate(records, start=1):
        if record.get("event") != "summarizer_usage":
            continue
        for field in totals:
            totals[field] += _count(record.get(field), f"cm.jsonl record {index}.{field}")
    return totals


def _provider_stream_validation(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate four background worker launches from the retained provider stream."""

    task_dispatches = 0
    parent_dispatch_records = 0
    background_dispatches = 0
    pre_dispatch_text_only = 0
    malformed_tool_uses = 0
    dispatch_batch_sizes: list[int] = []
    for record in records:
        if record.get("type") != "assistant" or record.get("parent_tool_use_id") not in (None, ""):
            continue
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        tool_blocks = [
            block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        malformed_tool_uses += sum(not isinstance(block.get("name"), str) for block in tool_blocks)
        task_blocks = [
            block
            for block in tool_blocks
            if isinstance(block.get("name"), str) and block.get("name") in TASK_TOOLS
        ]
        if task_blocks:
            parent_dispatch_records += 1
            dispatch_batch_sizes.append(len(task_blocks))
            for block in task_blocks:
                task_dispatches += 1
                task_input = block.get("input")
                if isinstance(task_input, dict) and task_input.get("run_in_background") is True:
                    background_dispatches += 1
        elif not tool_blocks and any(
            isinstance(block, dict) and block.get("type") == "text" for block in content
        ) and task_dispatches < 4:
            pre_dispatch_text_only += 1

    failures: list[str] = []
    if malformed_tool_uses:
        failures.append(
            "F2 cannot classify parent tool dispatch input in stream.jsonl "
            f"({malformed_tool_uses} malformed tool-use record(s))"
        )
    if task_dispatches == 0:
        failures.append(
            "F2 cannot prove background task launches: stream.jsonl has no parent Agent/Task dispatch records"
        )
    elif background_dispatches != task_dispatches:
        failures.append(
            "F2 requires run_in_background:true on every parent Agent/Task dispatch "
            f"(background {background_dispatches}/{task_dispatches})"
        )
    if task_dispatches != 4:
        failures.append(
            "F2 stream evidence must contain four parent Agent/Task dispatch records "
            f"(found {task_dispatches})"
        )
    if pre_dispatch_text_only:
        failures.append(
            "F2 requires zero pre-dispatch parent text-only assistant responses "
            f"(found {pre_dispatch_text_only})"
        )
    return {
        "failures": failures,
        "stream_task_dispatches": task_dispatches,
        "stream_parent_dispatch_records": parent_dispatch_records,
        "background_task_dispatches": background_dispatches,
        "pre_dispatch_text_only": pre_dispatch_text_only,
        "stream_dispatch_batch_sizes": dispatch_batch_sizes,
    }


def _provider_validation(
    root: Path,
    usage: Mapping[str, Any],
    events: Mapping[str, int],
    cm_records: Sequence[Mapping[str, Any]],
    stream_records: Sequence[Mapping[str, Any]],
    *,
    full: bool,
) -> dict[str, Any]:
    """Validate retained F2 evidence for every provider arm and F3 for full."""

    failures: list[str] = []
    stream_validation = _provider_stream_validation(stream_records)
    failures.extend(stream_validation["failures"])
    orchestrator_turns = 0
    orchestrator_tools = 0
    sessions = usage.get("sessions")
    if isinstance(sessions, list):
        for session in sessions:
            if (
                isinstance(session, dict)
                and str(session.get("role", "")).lower() == "orchestrator"
            ):
                orchestrator_turns += int(session.get("turns", 0))
                orchestrator_tools += int(session.get("tool_calls", 0))
    tools_per_turn = orchestrator_tools / orchestrator_turns if orchestrator_turns else 0.0

    task_calls = 0
    task_turns = 0
    try:
        turns = _read_jsonl(root / "turns.jsonl")
    except ReportError as error:
        failures.append(f"F2 turn evidence is unavailable: {error}")
    else:
        for turn in turns:
            if turn.get("role") != "orchestrator":
                continue
            tool_names = turn.get("tool_names")
            if not isinstance(tool_names, list):
                continue
            calls = sum(isinstance(name, str) and name in TASK_TOOLS for name in tool_names)
            if calls:
                task_turns += 1
                task_calls += calls
        if task_calls != 4 or not 1 <= task_turns <= 4:
            failures.append(
                "F2 requires exactly four task calls across one to four orchestrator turns "
                f"(found {task_calls} calls across {task_turns} turns)"
            )

    digest_gate_hit = events["digest_gate_hit"]
    redundant_reads = 0
    if full:
        seen_hashes: set[str] = set()
        unhashed_reads = 0
        for record in cm_records:
            if (record.get("event") or record.get("type")) != "read_raw":
                continue
            digest_hash = record.get("hash")
            if not isinstance(digest_hash, str) or not digest_hash:
                unhashed_reads += 1
                continue
            if digest_hash in seen_hashes:
                redundant_reads += 1
            seen_hashes.add(digest_hash)
        if unhashed_reads:
            failures.append(
                f"F3 cannot prove read_raw deduplication for {unhashed_reads} event(s) without a hash"
            )
        if redundant_reads:
            failures.append(f"F3 recorded {redundant_reads} redundant repeated-hash read_raw event(s)")

    return {
        "publishable": not failures,
        "failures": failures,
        "orchestrator_tools_per_turn": tools_per_turn,
        "task_calls": task_calls,
        "task_turns": task_turns,
        "stream_task_dispatches": stream_validation["stream_task_dispatches"],
        "stream_parent_dispatch_records": stream_validation["stream_parent_dispatch_records"],
        "background_task_dispatches": stream_validation["background_task_dispatches"],
        "pre_dispatch_text_only": stream_validation["pre_dispatch_text_only"],
        "stream_dispatch_batch_sizes": stream_validation["stream_dispatch_batch_sizes"],
        "digest_gate_hit": digest_gate_hit,
        "redundant_read_raw": redundant_reads,
    }


def arm_report(path: str | Path, *, output_ratio: float = 1.0) -> dict[str, Any]:
    """Summarize one arm, rejecting missing evidence instead of inventing it."""

    if output_ratio < 0:
        raise ValueError("output_ratio must be non-negative")
    root = Path(path)
    stream = root / "stream.jsonl"
    if not stream.is_file() or stream.stat().st_size == 0:
        raise ReportError(f"raw stream.jsonl is required and must be non-empty: {stream}")
    stream_records = _read_jsonl(stream)
    if not stream_records:
        raise ReportError(f"raw stream.jsonl must contain at least one JSON object: {stream}")
    usage = _read_json(root / "usage.json")
    if usage.get("valid") is not True or usage.get("completed") is not True:
        raise ReportError(f"usage.json is not a valid completed provider run: {root / 'usage.json'}")
    totals_value = usage.get("totals")
    if not isinstance(totals_value, dict):
        raise ReportError("usage.totals must be an object")
    totals = _usage_counts(totals_value, "usage.totals")
    wall_clock = usage.get("wall_clock_ms", totals_value.get("wall_clock_ms"))
    wall_clock_ms = None if wall_clock is None else _count(wall_clock, "wall_clock_ms")
    ledger = _gate_ledger(_read_jsonl(root / "gate.jsonl"))
    cm_records = _read_jsonl(root / "cm.jsonl")
    if not cm_records:
        raise ReportError("cm.jsonl must retain ContextMesh evidence, including an explicit no-event marker")
    events = {event: 0 for event in CM_EVENTS}
    for index, record in enumerate(cm_records, start=1):
        event = record.get("event") or record.get("type")
        if not isinstance(event, str) or event not in events:
            raise ReportError(
                f"cm.jsonl record {index} has unknown ContextMesh event: {event!r}"
            )
        events[event] += 1
    summarizer = _summarizer_counts(cm_records)
    agent_billable = _billable(totals, output_ratio)
    summarizer_billable = (
        summarizer["in_new"]
        + 0.1 * summarizer["cache_read"]
        + 1.25 * summarizer["cache_write"]
        + output_ratio * summarizer["out"]
    )
    validation = {"publishable": True, "failures": []}
    if usage.get("run_kind") == "provider_arm":
        validation = _provider_validation(
            root,
            usage,
            events,
            cm_records,
            stream_records,
            full=root.name == "full" or usage.get("provider_arm") == "full",
        )

    return {
        "arm": root.name,
        "raw_stream": str(stream),
        "turns": totals["turns"],
        "tool_calls": totals["tool_calls"],
        "tools_per_turn": totals["tool_calls"] / totals["turns"] if totals["turns"] else 0.0,
        "wall_clock_ms": wall_clock_ms,
        "agent_billable": agent_billable,
        "summarizer_usage": summarizer,
        "summarizer_billable": summarizer_billable,
        "billable": agent_billable + summarizer_billable,
        "run_kind": usage.get("run_kind", "unclassified"),
        "gate_ledger": ledger,
        "gate_passes": ledger["passed"],
        "role_billable": _role_costs(usage.get("sessions"), totals, output_ratio),
        "contextmesh_events": events,
        "validation": validation,
    }


def report_round(arms: Mapping[str, str | Path], *, output_ratio: float = 1.0) -> dict[str, Any]:
    """Return an auditable round report; suppress comparison on unequal completion."""

    if not arms:
        raise ValueError("at least one arm is required")
    reports = {name: arm_report(path, output_ratio=output_ratio) for name, path in arms.items()}
    pass_counts = {name: report["gate_passes"] for name, report in reports.items()}
    equal_completion = len(set(pass_counts.values())) <= 1
    provider_measurement = all(report["run_kind"] == "provider_arm" for report in reports.values())
    contextmesh_errors = any(
        any(
            report["contextmesh_events"].get(event, 0) > 0
            for event in (
                "errors",
                "summarizer_failed",
                "summarizer_timeout",
                "summarizer_invalid_json",
                "summarizer_unavailable",
            )
        )
        for report in reports.values()
    )
    suppression_reason = None
    if not equal_completion:
        suppression_reason = "gate pass counts differ across arms"
    elif not provider_measurement:
        suppression_reason = "not all arms are provider-arm measurements"
    elif contextmesh_errors:
        suppression_reason = "ContextMesh recorded fail-open errors"
    else:
        invalid_arms = [
            name for name, report in reports.items() if not report["validation"]["publishable"]
        ]
        if invalid_arms:
            details = "; ".join(
                f"{name}: {', '.join(reports[name]['validation']['failures'])}"
                for name in invalid_arms
            )
            suppression_reason = f"provider validation failed ({details})"
    return {
        "arms": reports,
        "completion_equal": equal_completion,
        "comparison": {name: report["billable"] for name, report in reports.items()} if suppression_reason is None else None,
        "suppression_reason": suppression_reason,
    }


def render_round(result: Mapping[str, Any]) -> str:
    """Render the headline evidence without printing a void comparison."""

    lines = ["arm | turns | tools | tools/turn | wall ms | billable | gate (first/after/fail/timeout/unresolved)"]
    for name, report in result["arms"].items():
        ledger = report["gate_ledger"]
        after = sum(ledger["passed_after_cycles"].values())
        wall = report["wall_clock_ms"] if report["wall_clock_ms"] is not None else "unknown"
        role_costs = report["role_billable"]
        cm = report["contextmesh_events"]
        lines.append(
            f"{name} | {report['turns']} | {report['tool_calls']} | {report['tools_per_turn']:.2f} | "
            f"{wall} | {report['billable']:.2f} | {ledger['passed_first_try']}/{after}/{ledger['failed']}/{ledger['timed_out']}/{ledger['unresolved']}"
        )
        lines.append(
            f"  roles: orchestrator={role_costs['orchestrator']:.2f}, "
            f"workers={role_costs['workers']:.2f}, summarizer={report['summarizer_billable']:.2f}"
        )
        validation = report["validation"]
        batches = "+".join(
            str(size) for size in validation.get("stream_dispatch_batch_sizes", [])
        )
        lines.append(
            f"  Dispatch: stream batches={batches or 'none'}, "
            f"orchestrator task turns={validation.get('task_turns', 0)}"
        )
        lines.append("  ContextMesh: " + ", ".join(f"{event}={cm[event]}" for event in CM_EVENTS))
    if result["comparison"] is None:
        lines.append(f"Comparison suppressed: {result['suppression_reason']}.")
    else:
        values = ", ".join(f"{name}={value:.2f}" for name, value in result["comparison"].items())
        lines.append(f"Billable comparison: {values}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm", nargs="+", help="name=artifact-directory")
    parser.add_argument("--output-ratio", type=float, default=1.0)
    args = parser.parse_args(argv)
    try:
        arms = dict(item.split("=", 1) for item in args.arm if "=" in item)
        if len(arms) != len(args.arm):
            raise ValueError("each arm must be NAME=ARTIFACT_DIRECTORY")
        print(render_round(report_round(arms, output_ratio=args.output_ratio)))
    except (ReportError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI is a thin adapter.
    raise SystemExit(main())


__all__ = ["CM_EVENTS", "ReportError", "arm_report", "render_round", "report_round"]
