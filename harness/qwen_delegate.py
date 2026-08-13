"""Headless Qwen Code runner for the Ollama DeepSeek worker contract."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

MODEL = "deepseek-v4-flash:0731-cloud"
BASE_URL = "http://localhost:11434/v1"
CONTEXT_WINDOW = 1_048_576
AUTO_COMPACT_TOKEN_LIMIT = 230_000
AUTO_COMPACT_THRESHOLD = AUTO_COMPACT_TOKEN_LIMIT / CONTEXT_WINDOW
CONTEXTMESH_TOOLS = (
    "claim_source",
    "read_source_chunk",
    "publish_file_brief",
    "get_file_brief",
    "get_file_briefs",
)

# Keep the measured worker a direct 1+4 topology and avoid loading large tool
# schemas that cannot help a sealed RuleForge task packet.
DISABLED_TOOLS = (
    "agent",
    "list_agents",
    "task_stop",
    "send_message",
    "create_sub_session",
    "monitor",
    "get_goal",
    "update_goal",
    "read_mcp_resource",
    "skill",
    "zoom_image",
    "notebook_edit",
    "todo_write",
    "enter_worktree",
    "exit_worktree",
    "web_fetch",
    "record_artifact",
    "cron_create",
    "cron_list",
    "cron_delete",
    "loop_wakeup",
    "computer_use__bring_to_front",
    "computer_use__check_for_update",
    "computer_use__check_permissions",
    "computer_use__click",
    "computer_use__double_click",
    "computer_use__drag",
    "computer_use__end_session",
    "computer_use__get_accessibility_tree",
    "computer_use__get_agent_cursor_state",
    "computer_use__get_config",
    "computer_use__get_cursor_position",
    "computer_use__get_recording_state",
    "computer_use__get_screen_size",
    "computer_use__get_window_state",
    "computer_use__hotkey",
    "computer_use__kill_app",
    "computer_use__launch_app",
    "computer_use__list_apps",
    "computer_use__list_windows",
    "computer_use__move_cursor",
    "computer_use__page",
    "computer_use__press_key",
    "computer_use__replay_trajectory",
    "computer_use__right_click",
    "computer_use__scroll",
    "computer_use__set_agent_cursor_enabled",
    "computer_use__set_agent_cursor_motion",
    "computer_use__set_agent_cursor_style",
    "computer_use__set_config",
    "computer_use__set_value",
    "computer_use__start_recording",
    "computer_use__start_session",
    "computer_use__stop_recording",
    "computer_use__type_text",
    "computer_use__zoom",
)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def settings(repo: Path, *, contextmesh: bool) -> dict[str, Any]:
    """Return isolated Qwen settings for one retained worker session."""

    value: dict[str, Any] = {
        "general": {
            "preventSystemSleep": False,
            "terminalBell": False,
        },
        "security": {"auth": {"selectedType": "openai"}},
        "model": {
            "name": MODEL,
            "reasoningEffort": "high",
            "generationConfig": {
                "contextWindowSize": CONTEXT_WINDOW,
                "timeout": 300_000,
                "maxRetries": 1,
            },
            "skipStartupContext": True,
        },
        "context": {"autoCompactThreshold": AUTO_COMPACT_THRESHOLD},
        "tools": {"approvalMode": "yolo", "disabled": list(DISABLED_TOOLS)},
        "experimental": {"agentTeam": False, "todoStopGuard": False},
        "memory": {"enableAutoSkill": False, "enableTeamMemory": False},
    }
    if not contextmesh:
        return value
    required = {
        name: os.environ.get(name, "")
        for name in (
            "CONTEXTMESH_BROKER_HOST",
            "CONTEXTMESH_BROKER_PORT",
            "CONTEXTMESH_WORKER_ID",
        )
    }
    if not all(required.values()):
        raise ValueError("ContextMesh Qwen worker requires broker host, port, and worker id")
    value["mcpServers"] = {
        "contextmesh": {
            "command": sys.executable,
            "args": ["-m", "contextmesh.mcp.bridge"],
            "cwd": str(repo),
            "env": required,
            "trust": True,
            "includeTools": list(CONTEXTMESH_TOOLS),
        }
    }
    return value


def _qwen_prefix(qwen_bin: str) -> tuple[str, ...]:
    resolved = shutil.which(qwen_bin) or qwen_bin
    path = Path(resolved)
    if path.suffix.lower() not in {".cmd", ".bat"}:
        return (resolved,)
    roots = (path.parent.parent, path.parent.parent / "qwen-code")
    for root in roots:
        node = root / "node" / "node.exe"
        cli = root / "lib" / "cli-entry.js"
        if node.is_file() and cli.is_file():
            return (str(node), str(cli))
    raise FileNotFoundError(f"could not resolve the Qwen Node launcher behind {path}")


def qwen_command(qwen_bin: str, resume_session_id: str | None) -> tuple[str, ...]:
    command = [
        *_qwen_prefix(qwen_bin),
        "--yolo",
        "--auth-type",
        "openai",
        "--model",
        MODEL,
        "--openai-api-key",
        "ollama",
        "--openai-base-url",
        BASE_URL,
        "--output-format",
        "stream-json",
    ]
    if resume_session_id is not None:
        command.extend(("--resume", resume_session_id))
    return tuple(command)


def _valid_result(event: Mapping[str, Any]) -> bool:
    result = event.get("result")
    return (
        event.get("type") == "result"
        and event.get("subtype") == "success"
        and event.get("is_error") is not True
        and isinstance(result, str)
        and not result.lstrip().startswith("[API Error:")
    )


def run(
    repo: Path,
    final: Path,
    *,
    contextmesh: bool,
    resume_session_id: str | None,
    qwen_bin: str,
) -> int:
    """Run Qwen once, retaining its raw JSONL on stdout for the parent harness."""

    prompt = sys.stdin.read()
    if not prompt.strip():
        raise ValueError("Qwen worker prompt is empty")
    worker_home = final.resolve().with_suffix(".qwen-home")
    _write_json(worker_home / "settings.json", settings(repo.resolve(), contextmesh=contextmesh))
    environment = os.environ.copy()
    environment.update(
        {
            "QWEN_HOME": str(worker_home),
            "QWEN_CODE_LEGACY_MCP_BLOCKING": "1",
        }
    )
    process = subprocess.Popen(
        qwen_command(qwen_bin, resume_session_id),
        cwd=Path.cwd(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(prompt)
    process.stdin.close()
    final_event: Mapping[str, Any] | None = None
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, Mapping) and event.get("type") == "result":
            final_event = event
    code = process.wait()
    if final_event is not None and _valid_result(final_event):
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_text(str(final_event["result"]).rstrip() + "\n", encoding="utf-8")
        return code
    return code if code else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--final", type=Path, required=True)
    parser.add_argument("--contextmesh", action="store_true")
    parser.add_argument("--resume-session-id")
    parser.add_argument("--qwen-bin", default=os.environ.get("STAGED_QWEN_BIN", "qwen"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    try:
        return run(
            args.repo,
            args.final,
            contextmesh=args.contextmesh,
            resume_session_id=args.resume_session_id,
            qwen_bin=args.qwen_bin,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"QwenDelegateError: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AUTO_COMPACT_THRESHOLD",
    "AUTO_COMPACT_TOKEN_LIMIT",
    "BASE_URL",
    "CONTEXT_WINDOW",
    "MODEL",
    "qwen_command",
    "run",
    "settings",
]
