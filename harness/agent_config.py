"""Render the reviewed Markdown agent definitions into Claude --agents JSON."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class AgentDefinitionError(ValueError):
    """Raised when a small harness agent definition is malformed."""


def _definition(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AgentDefinitionError(f"{path} has no frontmatter")
    _, frontmatter, body = text.split("---\n", 2)
    values = dict(line.split(": ", 1) for line in frontmatter.splitlines() if ": " in line)
    name = values.get("name")
    tools = values.get("tools")
    if name != path.stem or not tools:
        raise AgentDefinitionError(f"{path} has invalid name or tools")
    return {"description": f"ReasonRender {name} agent", "prompt": body.strip(), "tools": [tool.strip() for tool in tools.split(",")]}


def agents_json(root: str | Path, arm: str) -> dict[str, dict[str, Any]]:
    """Return the explicit custom-agent JSON accepted by Claude's --agents flag."""

    directory = Path(root) / "harness" / "agents" / arm
    if arm not in {"raw", "contextmesh", "full"}:
        raise AgentDefinitionError(f"unknown arm {arm!r}")
    return {role: _definition(directory / f"{role}.md") for role in ("orchestrator", "worker")}
