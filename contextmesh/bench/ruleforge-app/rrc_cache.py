"""Load one generic packet and render one task binding in memory."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SLOT_NAMES = (
    "domain",
    "source_field",
    "rule_name",
    "comparator",
    "error_code",
    "expected_value",
)


def _render(value: Any, slots: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        for name in SLOT_NAMES:
            value = value.replace("{" + name + "}", slots[name])
        return value
    if isinstance(value, list):
        return [_render(item, slots) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, slots) for key, item in value.items()}
    return value


def load_rendered_packet(workspace: str | Path, task_id: str) -> dict[str, Any]:
    cache = Path(workspace) / ".rrc-cache"
    template = json.loads((cache / "template.json").read_text(encoding="utf-8"))
    index = json.loads((cache / "bindings.json").read_text(encoding="utf-8"))
    return _render(template, index["bindings"][task_id])
