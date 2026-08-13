from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import harness.mcp_model_probe as mcp_model_probe


def test_probe_command_is_the_required_headless_deepseek_contract(tmp_path: Path) -> None:
    command = mcp_model_probe.command(tmp_path / "final.md", ("code_mode",))

    assert command[1:3] == ("-m", "harness.qwen_delegate")
    assert command[command.index("--qwen-bin") + 1] == "qwen"
    assert "--contextmesh" in command
    assert str(tmp_path / "final.md") in command
    assert "mcp__contextmesh__claim_source" in mcp_model_probe.PROMPT
    assert "Do not list MCP resources" in mcp_model_probe.PROMPT


def test_probe_requires_a_real_contextmesh_tool_event_and_marker(tmp_path: Path, monkeypatch) -> None:
    def fake_run(command, **kwargs):
        kwargs["stdout"].write('{"type":"assistant","message":{"content":[{"type":"tool_use","name":"mcp__contextmesh__claim_source","input":{"brief_id":"__contextmesh_model_surface_probe__"}}]}}\n')
        kwargs["stderr"].write("")
        assert kwargs["env"]["CONTEXTMESH_BROKER_HOST"] == "127.0.0.1"
        assert kwargs["env"]["CONTEXTMESH_BROKER_PORT"] == "1"
        Path(command[command.index("--final") + 1]).write_text("TOOL_SURFACE_SEEN\n", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mcp_model_probe.subprocess, "run", fake_run)
    result = mcp_model_probe.run(tmp_path, tmp_path / "probe")

    assert result["valid"] is True
    assert result["saw_contextmesh_tool_call"] is True
    assert Path(result["final"]).is_absolute()


def test_probe_rejects_reasoning_that_only_names_the_tool(tmp_path: Path, monkeypatch) -> None:
    def fake_run(command, **kwargs):
        kwargs["stdout"].write(
            '{"type":"item.completed","item":{"type":"reasoning","text":'
            '"Trying contextmesh claim_source now"}}\n'
        )
        kwargs["stderr"].write("")
        Path(command[command.index("--final") + 1]).write_text(
            "TOOL_SURFACE_SEEN\n", encoding="utf-8"
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mcp_model_probe.subprocess, "run", fake_run)
    result = mcp_model_probe.run(tmp_path, tmp_path / "probe")

    assert result["valid"] is False
    assert result["saw_contextmesh_tool_call"] is False
