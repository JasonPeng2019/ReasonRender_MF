from __future__ import annotations

import pytest

from harness.agent_config import AgentDefinitionError, agents_json


def test_agent_config_maps_all_arm_definitions_to_claude_json() -> None:
    config = agents_json(".", "full")

    assert set(config) == {"orchestrator", "worker"}
    assert config["orchestrator"]["tools"] == ["Task", "Bash"]
    assert config["worker"]["tools"] == ["Write", "Edit", "Bash", "mcp__contextmesh__read"]
    assert "packet_insufficient" in config["worker"]["prompt"]


def test_unknown_arm_is_rejected() -> None:
    with pytest.raises(AgentDefinitionError, match="unknown arm"):
        agents_json(".", "other")
