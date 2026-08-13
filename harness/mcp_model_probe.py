"""Model-facing admission test for the headless ContextMesh MCP configuration.

This is deliberately separate from measured task-worker compute.  A successful
A direct bridge ``tools/list`` call only proves the launcher can parse
configuration; this probe proves that the configured Qwen/DeepSeek worker
session receives and invokes the named tool.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Sequence

from harness.deepseek_delegate import command as deepseek_command
from harness.mcp_health import mcp_config

PROMPT = """Do not read files or run shell commands. Your first action MUST be
the ContextMesh MCP tool call named `mcp__contextmesh__claim_source` with
arguments `{"brief_id":"__contextmesh_model_surface_probe__"}`. Do not list MCP resources,
do not perform tool discovery, and do not reply with text before that call. The deliberately invalid id should produce an MCP-visible endpoint
error; that still proves the tool surface is present. Then reply exactly
`TOOL_SURFACE_SEEN`. Reply exactly `TOOL_SURFACE_MISSING` only if the
`mcp__contextmesh__claim_source` tool itself is unavailable."""
EXPECTED_MARKER = "TOOL_SURFACE_SEEN"


def _saw_contextmesh_claim(stream_text: str) -> bool:
    """Return whether the runner emitted an actual ContextMesh tool-call event.

    Model reasoning can contain tool names even when the provider rejected its
    attempted call syntax.  Admission must therefore inspect JSONL item events,
    not search the transcript text.  Either the start or completion event is
    enough: this probe deliberately points at an unavailable broker, so the
    completion is expected to contain an endpoint error.
    """

    for line in stream_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item")
        if (
            event.get("type") in {"item.started", "item.completed"}
            and isinstance(item, dict)
            and item.get("type") == "mcp_tool_call"
            and item.get("server") == "contextmesh"
            and item.get("tool") == "claim_source"
        ):
            return True
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if event.get("type") != "assistant" or not isinstance(content, list):
            continue
        if any(
            isinstance(part, dict)
            and part.get("type") == "tool_use"
            and part.get("name") == "mcp__contextmesh__claim_source"
            for part in content
        ):
            return True
    return False


def command(final: Path, enabled_features: Sequence[str] = ()) -> tuple[str, ...]:
    repo = Path(__file__).resolve().parents[1]
    return deepseek_command(
        repo,
        final,
        extra_config=mcp_config(repo),
        enabled_features=enabled_features,
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def run(workspace: Path, output: Path, enabled_features: Sequence[str] = ()) -> dict[str, Any]:
    """Run one bounded Qwen/DeepSeek call and retain admission proof."""

    workspace, output = workspace.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stream, stderr, final = output / "stream.jsonl", output / "stderr.log", output / "final.md"
    environment = os.environ.copy()
    environment["CONTEXTMESH_WORKER_ID"] = "mcp-model-surface-probe"
    # The eligibility call must prove the model can see and invoke the bridge,
    # not read a task source or depend on an arm's broker.  The deliberately
    # invalid claim is expected to reach an unavailable endpoint and return an
    # MCP-visible error after tool discovery.
    environment.setdefault("CONTEXTMESH_BROKER_HOST", "127.0.0.1")
    environment.setdefault("CONTEXTMESH_BROKER_PORT", "1")
    with stream.open("w", encoding="utf-8") as stdout, stderr.open("w", encoding="utf-8") as error:
        completed = subprocess.run(
            command(final, enabled_features),
            cwd=workspace,
            input=PROMPT,
            text=True,
            stdout=stdout,
            stderr=error,
            env=environment,
            check=False,
            timeout=120,
        )
    stream_text, final_text = _read(stream), _read(final)
    saw_tool = _saw_contextmesh_claim(stream_text)
    valid = completed.returncode == 0 and EXPECTED_MARKER in final_text and saw_tool
    return {
        "schema_version": 1,
        "command": list(command(final, enabled_features)),
        "enabled_features": list(enabled_features),
        "exit_code": completed.returncode,
        "stream": str(stream),
        "stderr": str(stderr),
        "final": str(final),
        "saw_contextmesh_tool_call": saw_tool,
        "final_marker": final_text.strip(),
        "valid": valid,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--enable-feature", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run(args.workspace, args.output, args.enable_feature)
    except (OSError, subprocess.SubprocessError) as error:
        result = {"schema_version": 1, "valid": False, "error": f"{type(error).__name__}: {error}"}
    (args.output / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result.get("valid") is True else 2


if __name__ == "__main__":  # pragma: no cover - exercised through the dispatcher.
    raise SystemExit(main())
