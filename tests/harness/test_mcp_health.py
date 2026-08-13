from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import harness.mcp_health as mcp_health


def test_bridge_lists_the_three_bound_contextmesh_tools(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXTMESH_BROKER_HOST", "127.0.0.1")
    monkeypatch.setenv("CONTEXTMESH_BROKER_PORT", "1")
    monkeypatch.setenv("CONTEXTMESH_WORKER_ID", "worker-01")

    assert mcp_health.REQUIRED_TOOLS <= mcp_health._bridge_tools()


def test_health_requires_codex_registration_and_the_complete_bridge_surface(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_health.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"command": "python", "transport": {"cwd": str(Path(".").resolve()), "env_vars": list(mcp_health.MCP_ENV_VARS)}}
            ),
            stderr="",
        ),
    )
    monkeypatch.setattr(mcp_health, "_bridge_tools", lambda: set(mcp_health.REQUIRED_TOOLS))

    assert mcp_health.check("codex") ["valid"] is True

    monkeypatch.setattr(mcp_health, "_bridge_tools", lambda: {"claim_source"})
    assert mcp_health.check("codex")["valid"] is False


def test_health_rejects_a_registered_bridge_without_the_checkout_cwd(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_health.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps({"transport": {"cwd": None}}), stderr=""),
    )
    monkeypatch.setattr(mcp_health, "_bridge_tools", lambda: set(mcp_health.REQUIRED_TOOLS))

    assert mcp_health.check("codex")["valid"] is False


def test_headless_mcp_config_sets_the_checkout_as_the_server_working_directory(tmp_path) -> None:
    config = mcp_health.mcp_config(tmp_path)

    assert config[:2] == mcp_health.MCP_CONFIG
    assert config[2] == f"mcp_servers.contextmesh.cwd='{tmp_path.resolve()}'"
    assert config[3] == "mcp_servers.contextmesh.env_vars=['CONTEXTMESH_BROKER_HOST','CONTEXTMESH_BROKER_PORT','CONTEXTMESH_WORKER_ID']"
    assert config[4] == "mcp_servers.contextmesh.required=true"
