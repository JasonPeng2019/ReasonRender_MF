"""Collect deterministic usage data from a Claude stream-json JSONL file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class CollectionError(ValueError):
    """Raised when a stream event does not satisfy the collector contract."""


_USAGE_FIELDS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)
_CHILD_FIELDS = ("agentId", "agentType", "totalToolUseCount")
_METRIC_FIELDS = (
    "in_new",
    "cache_read",
    "cache_write",
    "out",
    "turns",
    "tool_calls",
)


def _error(line_number: int, message: str) -> CollectionError:
    return CollectionError(f"line {line_number}: {message}")


def _non_negative_int(value: Any, field: str, line_number: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error(line_number, f"{field} must be a non-negative integer")
    return value


def _assistant_turn(event: dict[str, Any], line_number: int) -> tuple[str, dict[str, int], list[str]]:
    session_id = event.get("session_id")
    if not isinstance(session_id, str):
        raise _error(line_number, "assistant session_id must be a string")

    message = event.get("message")
    if not isinstance(message, dict):
        raise _error(line_number, "assistant message must be an object")
    usage = message.get("usage")
    if not isinstance(usage, dict):
        raise _error(line_number, "assistant message.usage must be an object")

    raw_usage = {
        field: _non_negative_int(usage.get(field), field, line_number)
        for field in _USAGE_FIELDS
    }
    content = message.get("content")
    if not isinstance(content, list):
        raise _error(line_number, "assistant message.content must be a list")

    tool_names: list[str] = []
    for block_number, block in enumerate(content, start=1):
        if not isinstance(block, dict):
            raise _error(line_number, f"content block {block_number} must be an object")
        if block.get("type") == "tool_use":
            name = block.get("name")
            if not isinstance(name, str):
                raise _error(line_number, f"tool_use block {block_number} name must be a string")
            tool_names.append(name)

    return (
        session_id,
        {
            "in_new": raw_usage["input_tokens"],
            "cache_read": raw_usage["cache_read_input_tokens"],
            "cache_write": raw_usage["cache_creation_input_tokens"],
            "out": raw_usage["output_tokens"],
        },
        tool_names,
    )


def _child_turn(
    event: dict[str, Any], line_number: int
) -> tuple[str, str, dict[str, int], int] | None:
    """Read a Claude Task result embedded in a parent user event."""

    if "tool_use_result" not in event:
        return None
    result = event.get("tool_use_result")
    if result is None:
        return None
    if not isinstance(result, dict):
        return None
    if (
        "agentId" in result
        and result.get("isAsync") is True
        and result.get("status") == "async_launched"
    ):
        return None
    if not any(field in result for field in _CHILD_FIELDS):
        return None

    missing = [field for field in (*_CHILD_FIELDS, "usage") if field not in result]
    if missing:
        raise _error(
            line_number,
            "user tool_use_result is missing required fields: " + ", ".join(missing),
        )
    agent_id = result["agentId"]
    agent_type = result["agentType"]
    if not isinstance(agent_id, str):
        raise _error(line_number, "tool_use_result.agentId must be a string")
    if not isinstance(agent_type, str):
        raise _error(line_number, "tool_use_result.agentType must be a string")
    tool_count = _non_negative_int(
        result["totalToolUseCount"], "totalToolUseCount", line_number
    )
    usage = result["usage"]
    if not isinstance(usage, dict):
        raise _error(line_number, "tool_use_result.usage must be an object")
    raw_usage = {
        field: _non_negative_int(usage.get(field), field, line_number)
        for field in _USAGE_FIELDS
    }
    return (
        agent_id,
        agent_type,
        {
            "in_new": raw_usage["input_tokens"],
            "cache_read": raw_usage["cache_read_input_tokens"],
            "cache_write": raw_usage["cache_creation_input_tokens"],
            "out": raw_usage["output_tokens"],
        },
        tool_count,
    )


def _empty_metrics() -> dict[str, int]:
    return {field: 0 for field in _METRIC_FIELDS}


def _metrics_with_ratio(metrics: dict[str, int]) -> dict[str, int | float]:
    result: dict[str, int | float] = {field: metrics[field] for field in _METRIC_FIELDS}
    result["tools_per_turn"] = (
        metrics["tool_calls"] / metrics["turns"] if metrics["turns"] else 0.0
    )
    return result


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def collect_stream(stream_path: str | Path, output_dir: str | Path) -> dict[str, Any]:
    """Validate a stream and write deterministic usage and turn artifacts."""
    stream_file = Path(stream_path)
    sessions: dict[str, dict[str, int]] = {}
    session_roles: dict[str, str] = {}
    session_agent_types: dict[str, str] = {}
    assistant_session_ids: set[str] = set()
    child_records: dict[str, tuple[str, int, tuple[int, ...]]] = {}
    task_agent_sessions: dict[str, str] = {}
    turns: list[dict[str, Any]] = []
    completed = False
    successful_result = False
    failed_result = False

    try:
        raw_stream = stream_file.read_bytes()
        encoding = "utf-16" if raw_stream.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        stream_lines = raw_stream.decode(encoding).splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CollectionError(f"could not read stream {stream_file}: {exc}") from exc

    for line_number, line in enumerate(stream_lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _error(line_number, f"malformed JSON ({exc.msg})") from exc
        if not isinstance(event, dict):
            raise _error(line_number, "event must be a JSON object")

        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "task_started":
            agent_id = event.get("task_id")
            parent_tool_use_id = event.get("tool_use_id")
            if isinstance(agent_id, str) and isinstance(parent_tool_use_id, str) and parent_tool_use_id:
                task_agent_sessions[agent_id] = f"worker:{parent_tool_use_id}"
        elif event_type == "assistant":
            session_id, usage, tool_names = _assistant_turn(event, line_number)
            assistant_session_ids.add(session_id)
            parent_tool_use_id = event.get("parent_tool_use_id")
            if parent_tool_use_id:
                if not isinstance(parent_tool_use_id, str):
                    raise _error(line_number, "assistant parent_tool_use_id must be a string")
                logical_session_id = f"worker:{parent_tool_use_id}"
                role = "worker"
            else:
                logical_session_id = session_id
                role = "orchestrator"
            metrics = sessions.setdefault(logical_session_id, _empty_metrics())
            metrics["in_new"] += usage["in_new"]
            metrics["cache_read"] += usage["cache_read"]
            metrics["cache_write"] += usage["cache_write"]
            metrics["out"] += usage["out"]
            metrics["turns"] += 1
            metrics["tool_calls"] += len(tool_names)
            session_roles.setdefault(logical_session_id, role)
            turns.append(
                {
                    "session_id": logical_session_id,
                    "role": role,
                    "turn": len(turns) + 1,
                    "in_new": usage["in_new"],
                    "cache_read": usage["cache_read"],
                    "cache_write": usage["cache_write"],
                    "out": usage["out"],
                    "tool_names": tool_names,
                    "tool_calls": len(tool_names),
                }
            )
        elif event_type == "user":
            child = _child_turn(event, line_number)
            if child is None:
                continue
            agent_id, agent_type, usage, tool_count = child
            signature = (
                agent_type,
                tool_count,
                tuple(usage[field] for field in ("in_new", "cache_read", "cache_write", "out")),
            )
            prior = child_records.get(agent_id)
            if prior is not None:
                if prior != signature:
                    raise _error(
                        line_number,
                        f"duplicate child agentId {agent_id!r} has conflicting usage",
                    )
                continue
            logical_session_id = task_agent_sessions.get(agent_id, agent_id)
            if logical_session_id in sessions and session_roles.get(logical_session_id) != "worker":
                raise _error(
                    line_number,
                    f"child agentId {agent_id!r} collides with an orchestrator session",
                )
            if logical_session_id in sessions:
                child_records[agent_id] = signature
                continue
            metrics = sessions.setdefault(logical_session_id, _empty_metrics())
            metrics["in_new"] += usage["in_new"]
            metrics["cache_read"] += usage["cache_read"]
            metrics["cache_write"] += usage["cache_write"]
            metrics["out"] += usage["out"]
            metrics["turns"] += 1
            metrics["tool_calls"] += tool_count
            session_roles[logical_session_id] = "worker"
            session_agent_types[logical_session_id] = agent_type
            child_records[agent_id] = signature
            turns.append(
                {
                    "session_id": logical_session_id,
                    "role": "worker",
                    "agent_type": agent_type,
                    "source": "user.tool_use_result",
                    "turn": len(turns) + 1,
                    "in_new": usage["in_new"],
                    "cache_read": usage["cache_read"],
                    "cache_write": usage["cache_write"],
                    "out": usage["out"],
                    "tool_names": [],
                    "tool_calls": tool_count,
                    "total_tool_use_count": tool_count,
                }
            )
        elif event_type == "result":
            completed = True
            result_is_success = (
                event.get("subtype") == "success" and event.get("is_error") is not True
            )
            if result_is_success:
                result_session_id = event.get("session_id")
                if result_session_id is None:
                    if len(assistant_session_ids) != 1:
                        raise _error(
                            line_number,
                            "successful result without session_id requires exactly one "
                            f"observed assistant session; found {len(assistant_session_ids)}; "
                            "add session_id",
                        )
                elif not isinstance(result_session_id, str):
                    raise _error(
                        line_number,
                        "successful result session_id must be a string matching an "
                        "observed assistant session",
                    )
                elif result_session_id not in assistant_session_ids:
                    raise _error(
                        line_number,
                        f"successful result session_id {result_session_id!r} was not "
                        "observed in an assistant event",
                    )
                successful_result = True
            else:
                failed_result = True
                successful_result = False

    total_metrics = _empty_metrics()
    session_entries: list[dict[str, Any]] = []
    for session_id, metrics in sessions.items():
        entry: dict[str, Any] = {
            "session_id": session_id,
            "role": session_roles.get(session_id, "orchestrator"),
        }
        if session_id in session_agent_types:
            entry["agent_type"] = session_agent_types[session_id]
        entry.update(_metrics_with_ratio(metrics))
        session_entries.append(entry)
        for field in _METRIC_FIELDS:
            total_metrics[field] += metrics[field]

    for turn_number, turn in enumerate(turns, start=1):
        turn["turn"] = turn_number

    invalid_reasons: list[str] = []
    if not completed:
        invalid_reasons.append("incomplete")
    elif failed_result or not successful_result:
        invalid_reasons.append("result_not_success")
    if completed and total_metrics["cache_read"] == 0:
        invalid_reasons.append("zero_cache_read")

    usage = {
        "schema_version": 1,
        "completed": completed,
        "valid": not invalid_reasons,
        "invalid_reasons": invalid_reasons,
        "sessions": session_entries,
        "totals": _metrics_with_ratio(total_metrics),
    }

    destination = Path(output_dir)
    try:
        destination.mkdir(parents=True, exist_ok=True)
        _write_json(destination / "usage.json", usage)
        with (destination / "turns.jsonl").open("w", encoding="utf-8", newline="\n") as turns_file:
            for turn in turns:
                turns_file.write(json.dumps(turn, sort_keys=True, separators=(",", ":")))
                turns_file.write("\n")
    except OSError as exc:
        raise CollectionError(f"could not write collection artifacts in {destination}: {exc}") from exc

    return usage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stream_path")
    parser.add_argument("output_dir")
    args = parser.parse_args(argv)
    try:
        usage = collect_stream(args.stream_path, args.output_dir)
    except CollectionError as exc:
        print(f"CollectionError: {exc}", file=sys.stderr)
        return 2
    return 0 if usage["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
