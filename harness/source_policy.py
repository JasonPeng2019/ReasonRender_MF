"""Evidence gate for direct overlapping-source reads in retained worker streams."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from harness.four_worker_plan import OverlapLedgerEntry

_READ_COMMAND = re.compile(
    r"\b(get-content|cat|grep|grep_search|rg|sed|type|gc|read_file|read_text|read_bytes|open\s*\()\b",
    re.IGNORECASE,
)
_EXIT_CODE = re.compile(
    r"(?:exit(?:ed)?(?:\s+with)?(?:\s+code)?|exit_code|exitCode)\s*[:=]?\s*(-?\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SourcePolicyViolation:
    worker_id: str
    canonical_path: str
    command: str


def _completed_read_command(line: str) -> str | None:
    """Return a successful recorded source-read command, if the stream has one.

    A sourceless worker view intentionally lets a model attempt an unavailable
    ``.py`` path. That failed command is not a source read and must not turn an
    otherwise valid retained cohort into an artificial policy violation.
    Legacy fixture events do not carry an exit code, so their absence remains
    compatible with the original evidence format.
    """

    try:
        event = json.loads(line)
        item = event.get("item", {})
        command = item.get("command") if event.get("type") == "item.completed" else None
        exit_code = item.get("exit_code") if isinstance(item, dict) else None
    except json.JSONDecodeError:
        return None
    if not isinstance(command, str) or not _READ_COMMAND.search(command):
        return None
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        return None
    return command


def _completed_read_commands(lines: Iterable[str]) -> tuple[str, ...]:
    """Return successful Codex and Qwen reads from one retained stream."""

    completed: list[str] = []
    pending_qwen: dict[str, tuple[str, bool]] = {}
    for line in lines:
        command = _completed_read_command(line)
        if command is not None:
            completed.append(command)
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        if event.get("type") == "assistant":
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "tool_use":
                    continue
                value = part.get("input")
                tool_id = part.get("id")
                if not isinstance(tool_id, str) or not isinstance(value, dict):
                    continue
                tool_name = part.get("name")
                if tool_name == "run_shell_command":
                    qwen_command = value.get("command")
                    if isinstance(qwen_command, str) and _READ_COMMAND.search(qwen_command):
                        pending_qwen[tool_id] = (qwen_command, True)
                elif tool_name == "read_file":
                    source_path = value.get("file_path", value.get("path"))
                    if isinstance(source_path, str):
                        pending_qwen[tool_id] = (f"read_file {json.dumps(source_path)}", False)
                elif tool_name == "grep_search":
                    source_path = value.get("path")
                    pattern = value.get("pattern", "")
                    if isinstance(source_path, str) and isinstance(pattern, str):
                        pending_qwen[tool_id] = (
                            f"grep_search {json.dumps(pattern)} {json.dumps(source_path)}",
                            False,
                        )
        elif event.get("type") == "user":
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "tool_result":
                    continue
                tool_id = part.get("tool_use_id")
                pending = pending_qwen.pop(tool_id, None) if isinstance(tool_id, str) else None
                if pending is None or part.get("is_error") is True:
                    continue
                qwen_command, is_shell = pending
                if is_shell:
                    rendered = json.dumps(part.get("content"), ensure_ascii=False)
                    exit_match = _EXIT_CODE.search(rendered)
                    if exit_match is not None and int(exit_match.group(1)) != 0:
                        continue
                completed.append(qwen_command)
    return tuple(completed)


def overlapping_paths(entries: Iterable[OverlapLedgerEntry]) -> frozenset[str]:
    return frozenset(entry.canonical_path.replace("\\", "/") for entry in entries)


def direct_overlap_reads(
    stream_path: str | Path, worker_id: str, entries: Iterable[OverlapLedgerEntry]
) -> tuple[SourcePolicyViolation, ...]:
    """Return invalid direct raw-overlap reads retained in a worker's Codex JSONL stream."""

    paths = overlapping_paths(entries)
    violations: list[SourcePolicyViolation] = []
    for command in _completed_read_commands(
        Path(stream_path).read_text(encoding="utf-8", errors="replace").splitlines()
    ):
        portable = re.sub(r"/{2,}", "/", command.replace("\\", "/"))
        for path in paths:
            offset = portable.find(path)
            if offset >= 0 and (offset == 0 or portable[offset - 1] != "!"):
                violations.append(SourcePolicyViolation(worker_id, path, command))
    return tuple(violations)


def direct_unlisted_source_reads(
    stream_path: str | Path,
    worker_id: str,
    allowed_paths: Iterable[str],
    candidate_paths: Iterable[str],
) -> tuple[SourcePolicyViolation, ...]:
    """Return direct source reads not declared for this worker's packet.

    The raw arm may read its declared sources directly, while a non-raw arm may
    read only its declared local sources.  Neither arm may silently discover an
    extra implementation pattern and thereby make the comparison asymmetric.
    """

    allowed = {path.replace("\\", "/") for path in allowed_paths}
    candidates = {path.replace("\\", "/") for path in candidate_paths} - allowed
    violations: list[SourcePolicyViolation] = []
    for command in _completed_read_commands(
        Path(stream_path).read_text(encoding="utf-8", errors="replace").splitlines()
    ):
        portable = re.sub(r"/{2,}", "/", command.replace("\\", "/"))
        for path in candidates:
            if path in portable:
                violations.append(SourcePolicyViolation(worker_id, path, command))
    return tuple(violations)


def observed_local_read_set(
    stream_path: str | Path,
    worker_id: str,
    allowed_paths: Iterable[str],
) -> dict[str, object]:
    """Retain the direct source reads actually visible in one worker stream.

    This is deliberately an evidence extractor rather than a source-access
    guard: the runner persists its result beside the exact delivered packet,
    while the existing policy checks reject prohibited paths.  Commands that
    do not identify a declared source candidate are irrelevant to this set.
    """

    allowed = tuple(sorted({path.replace("\\", "/") for path in allowed_paths}))
    observed: dict[str, set[str]] = {path: set() for path in allowed}
    path = Path(stream_path)
    if path.is_file():
        for command in _completed_read_commands(
            path.read_text(encoding="utf-8", errors="replace").splitlines()
        ):
            portable = re.sub(r"/{2,}", "/", command.replace("\\", "/"))
            for candidate in allowed:
                if candidate in portable:
                    observed[candidate].add(command)
    rows = [
        {"canonical_path": candidate, "commands": sorted(commands)}
        for candidate, commands in observed.items()
        if commands
    ]
    return {
        "schema_version": 1,
        "worker_id": worker_id,
        "allowed_local_read_paths": list(allowed),
        "observed_local_read_paths": [row["canonical_path"] for row in rows],
        "observed_commands": rows,
        "valid": True,
    }


def require_no_direct_overlap_reads(
    stream_path: str | Path, worker_id: str, entries: Iterable[OverlapLedgerEntry]
) -> None:
    """Reject a ContextMesh/full arm whose worker bypassed its broker brief."""

    violations = direct_overlap_reads(stream_path, worker_id, entries)
    if violations:
        detail = ", ".join(f"{item.worker_id}:{item.canonical_path}" for item in violations)
        raise ValueError(f"invalid direct overlapping-source read evidence: {detail}")


__all__ = [
    "SourcePolicyViolation",
    "direct_overlap_reads",
    "direct_unlisted_source_reads",
    "overlapping_paths",
    "observed_local_read_set",
    "require_no_direct_overlap_reads",
]
