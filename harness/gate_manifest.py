"""Derive focused acceptance commands from a materialized RuleForge manifest."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ManifestError(ValueError):
    """Raised when a manifest cannot deterministically define a gate command."""


@dataclass(frozen=True)
class GateTask:
    task_id: str
    acceptance_cmd: str


_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _render(value: str, slots: Mapping[str, object]) -> str:
    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        item = slots.get(name)
        if not isinstance(item, str) or not item:
            raise ManifestError(f"missing non-empty slot value {name!r}")
        return item

    rendered = _PLACEHOLDER.sub(substitute, value)
    if _PLACEHOLDER.search(rendered):  # defensive if a slot value contained a placeholder.
        raise ManifestError(f"unresolved placeholder in {rendered!r}")
    return rendered


def _task_command(task: object, write_paths: object) -> GateTask:
    if not isinstance(task, dict):
        raise ManifestError("each task must be an object")
    task_id = task.get("task_id")
    slots = task.get("slot_values")
    if not isinstance(task_id, str) or not task_id:
        raise ManifestError("task_id must be a non-empty string")
    if not isinstance(slots, dict):
        raise ManifestError(f"task {task_id!r} slot_values must be an object")
    if not isinstance(write_paths, list) or not write_paths:
        raise ManifestError("generic_packet.write_paths must be a non-empty list")
    rendered_paths: list[str] = []
    for path in write_paths:
        if not isinstance(path, str):
            raise ManifestError(f"task {task_id!r} write_paths must contain strings")
        rendered_paths.append(_render(path, slots))
    focused = [
        path
        for path in rendered_paths
        if path.startswith("tests/test_") and path.endswith("_rule.py")
    ]
    if len(focused) != 1:
        raise ManifestError(
            f"task {task_id!r} must render exactly one tests/test_*_rule.py acceptance path"
        )
    return GateTask(task_id=task_id, acceptance_cmd=f"python -m pytest {focused[0]} -q")


def manifest_tasks(payload: object) -> dict[str, GateTask]:
    """Return task-id to generated acceptance-command mappings from a manifest object."""

    if not isinstance(payload, dict):
        raise ManifestError("manifest must be an object")
    generic = payload.get("generic_packet")
    tasks = payload.get("tasks")
    if not isinstance(generic, dict):
        raise ManifestError("manifest.generic_packet must be an object")
    if not isinstance(tasks, list) or not tasks:
        raise ManifestError("manifest.tasks must be a non-empty list")
    result: dict[str, GateTask] = {}
    for task in tasks:
        generated = _task_command(task, generic.get("write_paths"))
        if generated.task_id in result:
            raise ManifestError(f"duplicate task_id {generated.task_id!r}")
        result[generated.task_id] = generated
    return result


def load_manifest_tasks(path: str | Path) -> dict[str, GateTask]:
    """Load and validate a materialized manifest file."""

    source = Path(path)
    try:
        payload: Any = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"could not read manifest {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {source} is malformed JSON: {exc.msg}") from exc
    return manifest_tasks(payload)
