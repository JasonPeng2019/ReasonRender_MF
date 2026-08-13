"""No-provider health gate for the ContextMesh stdio bridge configuration."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REQUIRED_TOOLS = {"claim_source", "read_source_chunk", "publish_file_brief", "get_file_brief", "get_file_briefs"}
MCP_ENV_VARS = (
    "CONTEXTMESH_BROKER_HOST",
    "CONTEXTMESH_BROKER_PORT",
    "CONTEXTMESH_WORKER_ID",
)
MCP_CONFIG = (
    "mcp_servers.contextmesh.command='python'",
    "mcp_servers.contextmesh.args=['-m','contextmesh.mcp.bridge']",
)


def mcp_config(repo_root: str | Path) -> tuple[str, ...]:
    """Bind the stdio child to the checkout that owns ``contextmesh``.

    Headless Codex does not forward the dispatcher's ``PYTHONPATH`` into MCP
    subprocesses.  The explicit server working directory is therefore part of
    the measured command contract, not ambient setup.
    """

    root = Path(repo_root).resolve()
    encoded_root = str(root).replace("'", "''")
    encoded_env_vars = ",".join(f"'{item}'" for item in MCP_ENV_VARS)
    return (
        *MCP_CONFIG,
        f"mcp_servers.contextmesh.cwd='{encoded_root}'",
        f"mcp_servers.contextmesh.env_vars=[{encoded_env_vars}]",
        "mcp_servers.contextmesh.required=true",
    )


def _bridge_tools() -> set[str]:
    """Start one local bridge and ask only for its MCP tool list (no model call)."""

    input_text = "\n".join(
        (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        )
    ) + "\n"
    environment = os.environ.copy()
    # The bridge validates its forwarding contract on startup.  A tool-list
    # probe never calls the endpoint, so a loopback placeholder lets this
    # no-provider gate inspect the MCP surface without starting a broker.
    environment.update(
        {
            "CONTEXTMESH_BROKER_HOST": "127.0.0.1",
            "CONTEXTMESH_BROKER_PORT": "1",
            "CONTEXTMESH_WORKER_ID": "mcp-health-probe",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "contextmesh.mcp.bridge"],
        text=True,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        timeout=15,
    )
    try:
        reply = json.loads(completed.stdout.splitlines()[1])
    except (IndexError, json.JSONDecodeError):
        return set()
    tools = reply.get("result", {}).get("tools", [])
    if not isinstance(tools, list):
        return set()
    return {item.get("name") for item in tools if isinstance(item, dict) and isinstance(item.get("name"), str)}


def check(codex: str = "codex", repo_root: str | Path = ".") -> dict[str, Any]:
    """Verify config registration and bridge protocol shape without spending model tokens."""

    command = [codex, "mcp", "get", "contextmesh", "--json"]
    for item in mcp_config(repo_root):
        command.extend(("--config", item))
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30)
    try:
        config = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except json.JSONDecodeError:
        config = {}
    tools = _bridge_tools()
    expected_cwd = str(Path(repo_root).resolve())
    transport = config.get("transport") if isinstance(config, dict) else None
    configured_cwd = transport.get("cwd") if isinstance(transport, dict) else None
    configured_env_vars = transport.get("env_vars") if isinstance(transport, dict) else None
    configured_env_set = set(configured_env_vars) if isinstance(configured_env_vars, list) else set()
    return {
        "schema_version": 1,
        "codex_config_exit_code": completed.returncode,
        "codex_config": config,
        "expected_cwd": expected_cwd,
        "configured_cwd": configured_cwd,
        "configured_env_vars": sorted(configured_env_set),
        "bridge_tools": sorted(tools),
        "valid": (
            completed.returncode == 0
            and configured_cwd == expected_cwd
            and set(MCP_ENV_VARS) <= configured_env_set
            and REQUIRED_TOOLS <= tools
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    result = check(args.codex, args.repo_root)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result["valid"] else 2


if __name__ == "__main__":  # pragma: no cover - exercised by the dispatcher.
    raise SystemExit(main())


__all__ = ["MCP_CONFIG", "MCP_ENV_VARS", "REQUIRED_TOOLS", "check", "mcp_config"]
