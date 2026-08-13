from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.gate_manifest import ManifestError, load_manifest_tasks, manifest_tasks


def manifest(tasks: list[dict], write_paths: list[str] | None = None) -> dict:
    return {
        "generic_packet": {"write_paths": write_paths or ["ruleforge/rules/{domain}.py", "tests/test_{domain}_rule.py"]},
        "tasks": tasks,
    }


def task(task_id: str = "ruleforge-payment", domain: str = "payment") -> dict:
    return {"task_id": task_id, "slot_values": {"domain": domain}}


def test_load_manifest_renders_one_command_per_task(tmp_path: Path) -> None:
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(manifest([task(), task("ruleforge-user", "user")])), encoding="utf-8")

    tasks = load_manifest_tasks(source)

    assert tasks["ruleforge-payment"].acceptance_cmd == "python -m pytest tests/test_payment_rule.py -q"
    assert tasks["ruleforge-user"].acceptance_cmd == "python -m pytest tests/test_user_rule.py -q"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (manifest([{"task_id": "bad", "slot_values": {}}]), "missing non-empty slot"),
        (manifest([task()], ["ruleforge/rules/{domain}.py"]), "exactly one"),
        (manifest([task()], ["tests/test_{domain}_rule.py", "tests/test_other_rule.py"]), "exactly one"),
    ],
)
def test_manifest_errors_are_loud(payload: dict, message: str) -> None:
    with pytest.raises(ManifestError, match=message):
        manifest_tasks(payload)


def test_duplicate_task_id_is_rejected() -> None:
    with pytest.raises(ManifestError, match="duplicate task_id"):
        manifest_tasks(manifest([task(), task()]))
