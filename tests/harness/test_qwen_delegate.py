from __future__ import annotations

from pathlib import Path

import pytest
from harness.qwen_delegate import (
    AUTO_COMPACT_THRESHOLD,
    AUTO_COMPACT_TOKEN_LIMIT,
    CONTEXT_WINDOW,
    MODEL,
    qwen_command,
    settings,
)


def test_qwen_command_pins_yolo_ollama_model_and_exact_resume_session() -> None:
    initial = qwen_command("qwen", None)
    resumed = qwen_command("qwen", "session-123")

    assert "--yolo" in initial
    assert initial[initial.index("--model") + 1] == MODEL
    assert initial[initial.index("--openai-base-url") + 1] == "http://localhost:11434/v1"
    assert "--resume" not in initial
    assert resumed[-2:] == ("--resume", "session-123")


def test_qwen_settings_pin_context_compaction_and_disable_nested_workers(tmp_path: Path) -> None:
    value = settings(tmp_path, contextmesh=False)

    assert value["model"]["reasoningEffort"] == "high"
    assert value["model"]["generationConfig"]["contextWindowSize"] == CONTEXT_WINDOW
    assert value["context"]["autoCompactThreshold"] == pytest.approx(AUTO_COMPACT_THRESHOLD)
    assert AUTO_COMPACT_THRESHOLD == AUTO_COMPACT_TOKEN_LIMIT / CONTEXT_WINDOW
    assert value["general"]["preventSystemSleep"] is False
    assert value["general"]["terminalBell"] is False
    assert "agent" in value["tools"]["disabled"]
    assert "create_sub_session" in value["tools"]["disabled"]
    assert "mcpServers" not in value


def test_qwen_contextmesh_settings_forward_the_exact_worker_endpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONTEXTMESH_BROKER_HOST", "127.0.0.1")
    monkeypatch.setenv("CONTEXTMESH_BROKER_PORT", "45123")
    monkeypatch.setenv("CONTEXTMESH_WORKER_ID", "worker-03")

    server = settings(tmp_path, contextmesh=True)["mcpServers"]["contextmesh"]

    assert server["cwd"] == str(tmp_path)
    assert server["env"] == {
        "CONTEXTMESH_BROKER_HOST": "127.0.0.1",
        "CONTEXTMESH_BROKER_PORT": "45123",
        "CONTEXTMESH_WORKER_ID": "worker-03",
    }
    assert "claim_source" in server["includeTools"]


def test_qwen_contextmesh_settings_fail_without_a_complete_endpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CONTEXTMESH_BROKER_HOST", raising=False)
    monkeypatch.delenv("CONTEXTMESH_BROKER_PORT", raising=False)
    monkeypatch.delenv("CONTEXTMESH_WORKER_ID", raising=False)

    with pytest.raises(ValueError, match="requires broker host"):
        settings(tmp_path, contextmesh=True)


def test_raw_settings_keep_repository_discovery_tools(tmp_path: Path) -> None:
    value = settings(tmp_path, contextmesh=False)

    disabled = set(value["tools"]["disabled"])
    for tool in ("list_directory", "grep_search", "glob"):
        assert tool not in disabled


def test_contextmesh_settings_keep_repository_discovery_tools(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONTEXTMESH_BROKER_HOST", "127.0.0.1")
    monkeypatch.setenv("CONTEXTMESH_BROKER_PORT", "45123")
    monkeypatch.setenv("CONTEXTMESH_WORKER_ID", "worker-03")

    value = settings(tmp_path, contextmesh=True)

    disabled = set(value["tools"]["disabled"])
    for tool in ("list_directory", "grep_search", "glob"):
        assert tool not in disabled
    assert "read_file" not in disabled
    assert "write_file" not in disabled
    assert "edit" not in disabled
    assert "run_shell_command" not in disabled
    assert "mcpServers" in value


def test_contextmesh_settings_are_a_superset_of_raw_disabled_tools(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONTEXTMESH_BROKER_HOST", "127.0.0.1")
    monkeypatch.setenv("CONTEXTMESH_BROKER_PORT", "45123")
    monkeypatch.setenv("CONTEXTMESH_WORKER_ID", "worker-03")

    raw = settings(tmp_path, contextmesh=False)
    contextmesh = settings(tmp_path, contextmesh=True)

    assert set(raw["tools"]["disabled"]) <= set(contextmesh["tools"]["disabled"])
