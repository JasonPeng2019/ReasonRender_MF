from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / "harness" / "agents"


def definition(arm: str, role: str) -> str:
    return (AGENTS / arm / f"{role}.md").read_text(encoding="utf-8")


def tools(text: str) -> str:
    return next(line.removeprefix("tools: ") for line in text.splitlines() if line.startswith("tools: "))


def test_orchestrator_tools_and_turn_thrift_contract() -> None:
    assert tools(definition("raw", "orchestrator")) == "Task, Bash, Read, Glob"
    for arm in ("contextmesh", "full"):
        text = definition(arm, "orchestrator")
        assert tools(text) == "Task, Bash"
        assert "all four Task calls in one" in text
        assert "one Bash invocation" in text
        assert "25/15" in text
    for arm in ("raw", "contextmesh", "full"):
        assert "TodoWrite" not in definition(arm, "orchestrator")


def test_worker_tool_boundaries_and_read_contract() -> None:
    raw = definition("raw", "worker")
    assert tools(raw) == "Read, Glob, Grep, Write, Edit, Bash"
    assert "READ_EVIDENCE" in raw and "parallel batch" in raw
    assert "TodoWrite" not in raw
    contextmesh = definition("contextmesh", "worker")
    assert tools(contextmesh) == "Write, Edit, Bash, mcp__contextmesh__read"
    assert "exactly once" in contextmesh
    assert "packet_insufficient" in contextmesh
    assert "TodoWrite" not in contextmesh
    full = definition("full", "worker")
    assert tools(full) == "Write, Edit, Bash, mcp__contextmesh__read"
    assert "Repository discovery is unavailable" in full
    assert "packet_insufficient" in full
    assert "TodoWrite" not in full
