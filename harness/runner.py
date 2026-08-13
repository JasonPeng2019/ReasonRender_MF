"""Opt-in Claude arm execution and provider-free Codex pilot planning."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

try:
    from contextmesh.bench.rrc_long_spec_demo import materialize as ruleforge_materialize
except ImportError:  # pragma: no cover - supports direct script invocation.
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    try:
        from contextmesh.bench.rrc_long_spec_demo import materialize as ruleforge_materialize
    except ImportError:
        ruleforge_materialize = None

try:
    from .collect import CollectionError, collect_stream
    from .deepseek_delegate import MODEL as DEEPSEEK_MODEL
    from .deepseek_delegate import command as deepseek_command
    from .gate_manifest import load_manifest_tasks
    from .report import ReportError, render_round, report_round
    from .workspace import materialize_workspace
except ImportError:  # pragma: no cover - supports direct script invocation.
    from collect import CollectionError, collect_stream
    from deepseek_delegate import MODEL as DEEPSEEK_MODEL
    from deepseek_delegate import command as deepseek_command
    from gate_manifest import load_manifest_tasks
    from report import ReportError, render_round, report_round
    from workspace import materialize_workspace


ARMS = ("raw", "contextmesh", "full")
PROVIDER_OPT_IN_ENV = "REASONRENDER_ALLOW_PROVIDER_EXECUTION"
MAX_BUDGET_ENV = "REASONRENDER_MAX_BUDGET_USD"
PROVIDER_TIMEOUT_ENV = "REASONRENDER_PROVIDER_TIMEOUT_SECONDS"
AUTO_COMPACT_TOKEN_LIMIT = 230_000
SAFETY = {"orchestrator": 25, "worker": 15}
NO_CONTEXTMESH_EVENT = "no_contextmesh"
ARM_TOOLS = {
    "raw": ("Task", "Bash", "Read", "Glob", "Grep", "Write", "Edit"),
    "contextmesh": ("Task", "Bash", "Write", "Edit", "mcp__contextmesh__read"),
    "full": ("Task", "Bash", "Write", "Edit", "mcp__contextmesh__read"),
}
DISALLOWED_TOOLS = ("Read", "Glob", "Grep")
PARENT_DISPATCH_SYSTEM_PROMPT = (
    "Parent dispatch protocol: The first parent response must be exactly one Bash tool call "
    "with no text or other tool. Later parent responses before all four background workers are "
    "issued must contain only Task/Agent calls. No waiting, reading, discovery, editing, or "
    "text is permitted until four background workers are issued."
)


@dataclass(frozen=True)
class ArmPlan:
    """Deterministic paths and executable command for one arm."""

    repo_root: Path
    arm: str
    round_id: str
    metrics_root: Path
    run_root: Path
    target: Path
    stream_path: Path
    usage_path: Path
    turns_path: Path
    agents_path: Path
    settings_path: Path
    mcp_config_path: Path | None
    command: list[str]
    safety: dict[str, int] = field(default_factory=lambda: dict(SAFETY))

    @property
    def metrics_dir(self) -> Path:
        return self.run_root

    @property
    def arm_root(self) -> Path:
        return self.run_root

    @property
    def target_path(self) -> Path:
        return self.target

    @property
    def stream(self) -> Path:
        return self.stream_path

    @property
    def usage(self) -> Path:
        return self.usage_path

    @property
    def turns(self) -> Path:
        return self.turns_path

    @property
    def agent_path(self) -> Path:
        return self.agents_path

    @property
    def settings(self) -> Path:
        return self.settings_path

    @property
    def prompt_path(self) -> Path:
        return self.run_root / "prompt.md"

    @property
    def prompt(self) -> Path:
        return self.prompt_path

    @property
    def stderr_path(self) -> Path:
        return self.run_root / "stderr.log"

    @property
    def stderr(self) -> Path:
        return self.stderr_path

    @property
    def manifest_path(self) -> Path:
        return self.run_root / "manifest.json"

    @property
    def manifest(self) -> Path:
        return self.manifest_path

    @property
    def gate_path(self) -> Path:
        return self.run_root / "gate.jsonl"

    @property
    def gate_state_dir(self) -> Path:
        return self.run_root / "gate-state"

    @property
    def cm_path(self) -> Path:
        return self.run_root / "cm.jsonl"

    @property
    def contextmesh_path(self) -> Path:
        return self.cm_path

    @property
    def orchestrator_max_turns(self) -> int:
        return self.safety["orchestrator"]

    @property
    def worker_max_turns(self) -> int:
        return self.safety["worker"]

    @property
    def command_array(self) -> list[str]:
        return list(self.command)


@dataclass(frozen=True)
class CodexInvocation:
    """One provider-disabled Codex pilot invocation and its isolated artifacts."""

    role: str
    model: str
    artifact_root: Path
    target: Path
    stream_path: Path
    final_path: Path
    usage_path: Path
    command: list[str]

    @property
    def target_path(self) -> Path:
        return self.target

    @property
    def stream(self) -> Path:
        return self.stream_path

    @property
    def final(self) -> Path:
        return self.final_path

    @property
    def command_array(self) -> list[str]:
        return list(self.command)


@dataclass(frozen=True)
class CodexPilotPlan:
    """A deterministic one-orchestrator/four-worker Codex dispatch batch."""

    repo_root: Path
    round_id: str
    metrics_root: Path
    run_root: Path
    orchestrator: CodexInvocation
    workers: tuple[CodexInvocation, ...]

    @property
    def orchestrator_plan(self) -> CodexInvocation:
        return self.orchestrator

    @property
    def worker_plans(self) -> tuple[CodexInvocation, ...]:
        return self.workers

    @property
    def worker_batch(self) -> tuple[CodexInvocation, ...]:
        return self.workers

    @property
    def dispatch_batches(self) -> tuple[tuple[CodexInvocation, ...], ...]:
        return (self.workers,)

    @property
    def worker_count(self) -> int:
        return len(self.workers)


def _round_name(round_id: str | int) -> str:
    value = str(round_id)
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or PureWindowsPath(value).drive
    ):
        raise ValueError("round_id must be a single path component")
    return value


def build_arm_plan(
    repo_root: str | Path,
    arm: str,
    round_id: str | int,
    metrics_root: str | Path = "metrics",
) -> ArmPlan:
    """Build a deterministic executable plan without starting a provider."""

    if arm not in ARMS:
        choices = ", ".join(ARMS)
        raise ValueError(f"arm must be one of: {choices}")

    repo = Path(repo_root).resolve()
    metrics = Path(metrics_root)
    if not metrics.is_absolute():
        metrics = repo / metrics
    metrics = metrics.resolve()
    round_name = _round_name(round_id)
    run_root = metrics / round_name / arm
    target = run_root / "target"
    stream_path = run_root / "stream.jsonl"
    usage_path = run_root / "usage.json"
    turns_path = run_root / "turns.jsonl"
    agents_path = run_root / "agents.json"
    settings_path = run_root / "settings.json"
    command = [
        "claude",
        "-p",
        "--verbose",
        "--model",
        "sonnet",
        "--tools",
        ",".join(ARM_TOOLS[arm]),
        "--settings",
        str(settings_path),
        "--max-turns",
        str(SAFETY["orchestrator"]),
        "--max-budget-usd",
        os.environ.get(MAX_BUDGET_ENV) or "20",
        "--output-format",
        "stream-json",
        "--include-hook-events",
        "--dangerously-skip-permissions",
        "--append-system-prompt",
        PARENT_DISPATCH_SYSTEM_PROMPT,
    ]
    mcp_config_path = None
    if arm in {"contextmesh", "full"}:
        mcp_config_path = run_root / "mcp.json"
        command.extend(("--mcp-config", str(mcp_config_path)))
    if arm == "full":
        command.extend(("--disallowed-tools", ",".join(DISALLOWED_TOOLS)))
    return ArmPlan(
        repo_root=repo,
        arm=arm,
        round_id=round_name,
        metrics_root=metrics,
        run_root=run_root,
        target=target,
        stream_path=stream_path,
        usage_path=usage_path,
        turns_path=turns_path,
        agents_path=agents_path,
        settings_path=settings_path,
        mcp_config_path=mcp_config_path,
        command=command,
    )


def _codex_command(model: str, final_path: Path) -> list[str]:
    if model == DEEPSEEK_MODEL:
        return list(deepseek_command(Path(__file__).resolve().parents[1], final_path))
    return [
        "codex",
        "exec",
        "--ignore-user-config",
        "--enable",
        "fast_mode",
        "--model",
        model,
        "--config",
        "model_reasoning_effort=high",
        "--config",
        f"model_auto_compact_token_limit={AUTO_COMPACT_TOKEN_LIMIT}",
        "--config",
        "service_tier=priority",
        "--json",
        "--output-last-message",
        str(final_path),
        "--dangerously-bypass-approvals-and-sandbox",
        "-",
    ]


def _codex_invocation(
    role: str,
    model: str,
    artifact_root: Path,
    target: Path,
) -> CodexInvocation:
    final_path = artifact_root / "final.json"
    return CodexInvocation(
        role=role,
        model=model,
        artifact_root=artifact_root,
        target=target,
        stream_path=artifact_root / "stream.jsonl",
        final_path=final_path,
        usage_path=artifact_root / "usage.json",
        command=_codex_command(model, final_path),
    )


def build_codex_pilot_plan(
    repo_root: str | Path,
    round_id: str | int,
    metrics_root: str | Path = "metrics",
) -> CodexPilotPlan:
    """Build one Terra orchestrator and one isolated batch of four DeepSeek workers."""

    repo = Path(repo_root).resolve()
    metrics = Path(metrics_root)
    if not metrics.is_absolute():
        metrics = repo / metrics
    metrics = metrics.resolve()
    round_name = _round_name(round_id)
    run_root = metrics / round_name / "codex-pilot"
    orchestrator_root = run_root / "orchestrator"
    workers_root = run_root / "workers"
    orchestrator = _codex_invocation(
        "orchestrator",
        "gpt-5.6-terra",
        orchestrator_root,
        orchestrator_root / "target",
    )
    workers = tuple(
        _codex_invocation(
            f"worker-{index:02d}",
            DEEPSEEK_MODEL,
            workers_root / f"worker-{index:02d}",
            workers_root / f"worker-{index:02d}" / "target",
        )
        for index in range(1, 5)
    )
    return CodexPilotPlan(
        repo_root=repo,
        round_id=round_name,
        metrics_root=metrics,
        run_root=run_root,
        orchestrator=orchestrator,
        workers=workers,
    )


build_pilot_plan = build_codex_pilot_plan


Materializer = Callable[[ArmPlan], str | Path]
ProcessRunner = Callable[..., Any]


def _ruleforge_materializer(plan: ArmPlan) -> Path:
    """Run RuleForge's materializer and return only its workspace directory."""

    if ruleforge_materialize is None:
        raise RuntimeError("RuleForge materializer is unavailable")
    materialized = plan.run_root / "materialized"
    if materialized.exists():
        raise FileExistsError(f"materializer output already exists: {materialized}")
    result = ruleforge_materialize(materialized)
    if not isinstance(result, Mapping):
        raise TypeError("RuleForge materializer must return a manifest object")
    workspace_value = result.get("workspace")
    if not isinstance(workspace_value, str):
        raise ValueError("RuleForge manifest must name its workspace")
    workspace = Path(workspace_value)
    if not workspace.is_absolute():
        workspace = materialized / workspace
    manifest_source = materialized / "manifest.json"
    plan.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if manifest_source.is_file():
        shutil.copyfile(manifest_source, plan.manifest_path)
    else:
        plan.manifest_path.write_text(
            json.dumps(dict(result), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if not workspace.is_dir():
        raise NotADirectoryError(f"RuleForge workspace is not a directory: {workspace}")
    return workspace


def prepare_arm(plan: ArmPlan, materializer: Materializer) -> Path:
    """Clone a materializer's workspace into the arm target."""

    plan.run_root.mkdir(parents=True, exist_ok=True)
    if plan.target.exists() or plan.target.is_symlink():
        raise FileExistsError(f"target workspace already exists: {plan.target}")

    source = materializer(plan)
    if source is None:
        raise TypeError("materializer must return an isolated source workspace")
    source_path = Path(source)
    if source_path.resolve() == plan.target.resolve():
        raise ValueError("materializer source must be different from target")
    return materialize_workspace(source_path, plan.target)


def _dry_run_materializer(plan: ArmPlan) -> Path:
    """Compatibility seam used by provider-free tests; it still materializes RuleForge."""

    return _ruleforge_materializer(plan)


def _source_path(plan: ArmPlan, *parts: str) -> Path:
    path = plan.repo_root.joinpath(*parts)
    if path.is_file():
        return path
    return Path(__file__).resolve().parent.joinpath(*parts[1:])


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(dict(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _freeze_mcp_config(plan: ArmPlan) -> None:
    if plan.mcp_config_path is None:
        return
    source = _source_path(plan, "harness", "mcp", "contextmesh.json")
    config = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("ContextMesh MCP config must be an object")
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or not isinstance(servers.get("contextmesh"), dict):
        raise ValueError("ContextMesh MCP config must define contextmesh")
    server = servers["contextmesh"]
    args = server.get("args")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ValueError("ContextMesh MCP config args must be a string list")
    implementation_root = plan.repo_root
    if not (implementation_root / "contextmesh" / "mcp" / "server.py").is_file():
        implementation_root = Path(__file__).resolve().parents[1]
    server["args"] = [
        str(implementation_root / "contextmesh" / "mcp" / "server.py")
        if item.replace("\\", "/") == "contextmesh/mcp/server.py"
        else item
        for item in args
    ]
    plan.mcp_config_path.parent.mkdir(parents=True, exist_ok=True)
    plan.mcp_config_path.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _materialize_worker_agent(plan: ArmPlan) -> Path:
    """Install the maintained arm worker contract in the target workspace."""

    source = _source_path(plan, "harness", "agents", plan.arm, "worker.md")
    text = source.read_text(encoding="utf-8")
    sections = text.split("---\n", 2)
    if len(sections) != 3 or sections[0]:
        raise ValueError(f"worker contract has invalid frontmatter: {source}")

    contract_lines: list[str] = []
    tools: str | None = None
    for line in sections[1].splitlines():
        key, separator, value = line.partition(": ")
        if separator and key == "tools":
            tools = value
        if key not in {"name", "description", "maxTurns", "permissionMode"}:
            contract_lines.append(line)
    if not tools:
        raise ValueError(f"worker contract does not declare tools: {source}")

    worker_path = plan.target / ".claude" / "agents" / "worker.md"
    worker_path.parent.mkdir(parents=True, exist_ok=True)
    body = sections[2].strip()
    frontmatter = [
        "---",
        "name: worker",
        "description: ReasonRender worker agent",
        *contract_lines,
        f"maxTurns: {plan.worker_max_turns}",
        "permissionMode: bypassPermissions",
        "---",
    ]
    worker_path.write_text("\n".join((*frontmatter, "", body, "")), encoding="utf-8")
    return worker_path


def _freeze_run_files(plan: ArmPlan) -> None:
    plan.run_root.mkdir(parents=True, exist_ok=True)
    settings_source = _source_path(plan, "harness", "settings", f"{plan.arm}.json")
    settings = json.loads(settings_source.read_text(encoding="utf-8"))
    hook_commands = (
        ("completion_gate.py", settings["hooks"]["SubagentStop"][0]["hooks"][0]),
        ("block_bash_reads.py", settings["hooks"]["PreToolUse"][0]["hooks"][0]),
    )
    for hook_name, hook in hook_commands:
        hook_path = _source_path(plan, "harness", "hooks", hook_name).resolve()
        if not hook_path.is_file():
            raise FileNotFoundError(f"missing harness hook: {hook_path}")
        hook["command"] = hook["command"].replace(
            f'"$CLAUDE_PROJECT_DIR/harness/hooks/{hook_name}"',
            f'"{hook_path}"',
        )
    plan.settings_path.write_text(
        json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _freeze_mcp_config(plan)

    plan.gate_state_dir.mkdir(parents=True, exist_ok=True)
    plan.gate_path.touch(exist_ok=True)
    plan.cm_path.touch(exist_ok=True)
    _write_json(
        plan.agents_path,
        {
            "artifact_kind": "frozen_evidence",
            "executable": False,
            "label": "frozen evidence",
            "reason": "The executable worker definition is materialized in target/.claude/agents/worker.md; this retained JSON is not passed to Claude.",
        },
    )


def _manifest(plan: ArmPlan) -> Mapping[str, Any]:
    try:
        value = json.loads(plan.manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"missing RuleForge manifest: {plan.manifest_path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid RuleForge manifest: {error.msg}") from error
    if not isinstance(value, dict):
        raise ValueError("RuleForge manifest must be an object")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 4:
        raise ValueError("RuleForge manifest must contain exactly four tasks")
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            raise ValueError(f"RuleForge task {index} must be an object")
        if not isinstance(task.get("task_id"), str) or not isinstance(task.get("text"), str):
            raise ValueError(f"RuleForge task {index} must contain task_id and text")
    return value


def _render_slots(value: Any, slots: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        rendered = value
        for name, slot_value in slots.items():
            rendered = rendered.replace("{" + name + "}", str(slot_value))
        return rendered
    if isinstance(value, list):
        return [_render_slots(item, slots) for item in value]
    if isinstance(value, dict):
        return {key: _render_slots(item, slots) for key, item in value.items()}
    return value


def _task_list(task: Mapping[str, Any], packet: Mapping[str, Any] | None, field: str) -> list[str]:
    values = task.get(field)
    if not isinstance(values, list) and packet is not None:
        values = packet.get(field)
    if not isinstance(values, list):
        return []
    return [str(value) for value in values]


def build_parent_prompt(plan: ArmPlan) -> str:
    """Render a bounded parent prompt from the four retained manifest tasks."""

    manifest = _manifest(plan)
    tasks = manifest["tasks"]
    generic_packet = manifest.get("generic_packet")
    if not isinstance(generic_packet, dict):
        generic_packet = None
    lines = [
        "You are the bounded RuleForge parent orchestrator.",
        f"Work only in the target workspace: {plan.target}",
        "The parent must not edit files, write files, or run implementation commands.",
        "The first parent response must be exactly one Bash tool call with no text or other tool; use it to render all four task packets.",
        "Do not make any other parent Bash call.",
        "Later parent responses before all four background workers are issued may contain only Task/Agent calls; do not wait, read, discover, edit, or emit text.",
        "Starting on the immediately following parent response, dispatch exactly four registered worker Task/Agent children without waiting for any child result.",
        "Make exactly one Task call for each task below; the provider may record those calls in one to four parent tool turns. Do not dispatch any other child or emit a text-only response until all four calls are issued.",
        "Each Task call must use subagent_type=worker, set run_in_background=true, and implement only its assigned task.",
        "Each child Task prompt must begin with its exact `GATE_TASK_ID: <task id>` line below so SubagentStop can resolve the manifest task.",
        f"The parent ceiling is {plan.orchestrator_max_turns} turns; each child ceiling is {plan.worker_max_turns} turns.",
        "Children must apply their declared writes, run focused tests, and return their acceptance evidence.",
        "",
        "Tasks:",
    ]
    if plan.arm != "raw":
        lines[7:7] = [
            "For every child, before any edit or test, call `mcp__contextmesh__read` exactly once on "
            f"`{plan.target / 'ruleforge' / 'evaluator.py'}`. Use that response as the shared "
            "contract; do not use Bash to read that file.",
        ]
    for index, task in enumerate(tasks, start=1):
        slots = task.get("slot_values")
        if not isinstance(slots, dict):
            slots = {}
        rendered_packet = _render_slots(generic_packet, slots) if generic_packet else None
        packet = rendered_packet if isinstance(rendered_packet, dict) else generic_packet
        lines.extend(
            (
                f"{index}. {task['task_id']}",
                f"GATE_TASK_ID: {task['task_id']}",
                "Declared task text:",
                str(task["text"]),
            )
        )
        paths = _task_list(task, packet, "write_paths")
        acceptance = _task_list(task, packet, "acceptance")
        reads = _task_list(task, packet, "read_first")
        if paths:
            lines.append("Declared write paths:")
            lines.extend(f"- {path}" for path in paths)
        if acceptance:
            lines.append("Declared acceptance:")
            lines.extend(f"- {criterion}" for criterion in acceptance)
        if plan.arm == "raw":
            if reads:
                lines.append("Declared source-read list:")
                lines.extend(f"- {path}" for path in reads)
        elif rendered_packet is not None:
            lines.extend(
                (
                    "Task slot values:",
                    json.dumps(slots, sort_keys=True),
                    "Recursively rendered structural generic packet with this task's slot values:",
                    json.dumps(rendered_packet, indent=2, sort_keys=True),
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _prepare_run(plan: ArmPlan, materializer: Materializer, *, require_manifest: bool) -> None:
    prepare_arm(plan, materializer)
    _materialize_worker_agent(plan)
    _freeze_run_files(plan)
    if require_manifest or plan.manifest_path.exists():
        plan.prompt_path.write_text(build_parent_prompt(plan), encoding="utf-8")


def _process_environment(plan: ArmPlan) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "CLAUDE_PROJECT_DIR": str(plan.repo_root),
            "REASONRENDER_ARM": plan.arm,
            "CONTEXTMESH_APP_ID": f"reasonrender-{plan.round_id}-{plan.arm}",
            "REASONRENDER_GATE_MANIFEST": str(plan.manifest_path),
            "REASONRENDER_GATE_STATE_DIR": str(plan.gate_state_dir),
            "REASONRENDER_GATE_LOG_PATH": str(plan.gate_path),
            "CONTEXTMESH_LOG": str(plan.cm_path),
        }
    )
    return environment


def _provider_timeout_seconds() -> int:
    try:
        value = int(os.environ.get(PROVIDER_TIMEOUT_ENV, "600"))
    except ValueError:
        return 600
    return value if value > 0 else 600


def _run_process(command: Sequence[str], *, cwd: Path, input: str, env: Mapping[str, str]) -> Any:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            input=input,
            text=True,
            capture_output=True,
            env=dict(env),
            timeout=_provider_timeout_seconds(),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        return (
            124,
            _text(error.stdout),
            _text(error.stderr) + f"\nprovider arm timed out after {_provider_timeout_seconds()} seconds\n",
        )


def _process_output(result: Any) -> tuple[int, str, str]:
    if isinstance(result, tuple):
        if len(result) != 3:
            raise TypeError("process runner tuple must be (returncode, stdout, stderr)")
        return int(result[0]), _text(result[1]), _text(result[2])
    return int(getattr(result, "returncode")), _text(getattr(result, "stdout", "")), _text(
        getattr(result, "stderr", "")
    )


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else str(value)


def _annotate_usage(plan: ArmPlan, usage: Mapping[str, Any], elapsed_ms: int) -> None:
    annotated = dict(usage)
    annotated.update(
        {
            "provider_arm": plan.arm,
            "run_kind": "provider_arm",
            "wall_clock_ms": elapsed_ms,
        }
    )
    _write_json(plan.usage_path, annotated)


def _validate_gate_ledger(plan: ArmPlan) -> None:
    """Fail closed unless every manifest task has a final passing gate record."""

    expected = set(load_manifest_tasks(plan.manifest_path))
    try:
        lines = plan.gate_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"completion gate ledger is unavailable: {exc}") from exc
    final_by_task: dict[str, Mapping[str, Any]] = {}
    for line_number, line in enumerate(lines, start=1):
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"completion gate ledger line {line_number} is malformed") from exc
        if not isinstance(record, dict):
            raise ValueError(f"completion gate ledger line {line_number} must be an object")
        task_id = record.get("task_id")
        session_id = record.get("session_id")
        verdict = record.get("verdict")
        cycle = record.get("cycle")
        if not isinstance(task_id, str) or task_id not in expected:
            raise ValueError(f"completion gate ledger line {line_number} has an unknown task")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"completion gate ledger line {line_number} lacks a session")
        if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 0:
            raise ValueError(f"completion gate ledger line {line_number} has an invalid cycle")
        if not isinstance(verdict, str):
            raise ValueError(f"completion gate ledger line {line_number} lacks a verdict")
        final_by_task[task_id] = record
    missing = expected.difference(final_by_task)
    if missing:
        raise ValueError("completion gate ledger lacks passing records for every manifest task")
    if any(record["verdict"] != "pass" for record in final_by_task.values()):
        raise ValueError("completion gate ledger has a non-passing final verdict")


def _validate_contextmesh_evidence(plan: ArmPlan) -> None:
    """Require one durable ContextMesh raw-read event for non-raw arms."""

    if plan.arm == "raw":
        return
    try:
        lines = plan.cm_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"ContextMesh evidence is unavailable: {exc}") from exc
    events: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ContextMesh evidence line {line_number} is malformed") from exc
        event = record.get("event") if isinstance(record, dict) else None
        if not isinstance(event, str):
            raise ValueError(f"ContextMesh evidence line {line_number} lacks an event")
        events.add(event)
    if "read_raw" not in events:
        raise ValueError("ContextMesh evidence lacks the required read_raw event")


def _retained_complete_arm(plan: ArmPlan) -> bool:
    """Return whether a prior provider arm is complete enough to preserve."""

    if not plan.stream_path.is_file() or not plan.usage_path.is_file():
        return False
    try:
        usage = json.loads(plan.usage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(usage, dict)
        and usage.get("valid") is True
        and usage.get("run_kind") == "provider_arm"
        and not (plan.run_root / "diagnostic.json").exists()
    )


def _has_retained_attempt(plan: ArmPlan) -> bool:
    """Avoid overwriting any paid or partial provider attempt."""

    return plan.run_root.exists() and any(
        path.exists()
        for path in (
            plan.target,
            plan.stream_path,
            plan.stderr_path,
            plan.usage_path,
            plan.turns_path,
            plan.gate_path,
            plan.cm_path,
            plan.manifest_path,
            plan.prompt_path,
        )
    )


def _is_product_failure(detail: str) -> bool:
    """Reserve a hard stop for a retained traceback in product implementation."""

    normalized = detail.replace("\\", "/").casefold()
    if "traceback" not in normalized:
        return False
    return any(f"/{directory}/" in normalized for directory in ("rrc", "contextmesh"))


def _write_diagnostic(plan: ArmPlan, category: str, detail: str) -> dict[str, str]:
    """Persist an arm outcome without converting it into a rerun request."""

    value = {"status": category, "arm": plan.arm, "detail": detail}
    plan.run_root.mkdir(parents=True, exist_ok=True)
    _write_json(plan.run_root / "diagnostic.json", value)
    return value


def _write_round_progress(
    plan: ArmPlan,
    arm_plans: Mapping[str, ArmPlan],
    diagnostics: Mapping[str, Mapping[str, str]],
) -> None:
    """Leave a resumable ledger when a round has retained degraded evidence."""

    root = plan.metrics_root / plan.round_id
    root.mkdir(parents=True, exist_ok=True)
    _write_json(
        root / "progress.json",
        {
            "status": "blocking_product_error"
            if any(item["status"] == "blocking_product_error" for item in diagnostics.values())
            else "degraded",
            "policy": "retain evidence and continue unfinished arms; do not auto-rerun test, scaffold, or orchestration failures",
            "arms": {
                arm: {
                    **item,
                    **({"root": str(arm_plans[arm].run_root)} if arm in arm_plans else {}),
                }
                for arm, item in diagnostics.items()
            },
        },
    )


def execute_arm(
    plan: ArmPlan,
    *,
    materializer: Materializer | None = None,
    process_runner: ProcessRunner | None = None,
) -> int:
    """Materialize and execute one arm through an injectable process boundary."""

    if os.environ.get(PROVIDER_OPT_IN_ENV) != "1":
        return 2
    _prepare_run(plan, materializer or _ruleforge_materializer, require_manifest=True)
    prompt = plan.prompt_path.read_text(encoding="utf-8")
    if plan.arm == "raw":
        plan.cm_path.write_text('{"event":"no_contextmesh"}\n', encoding="utf-8")
    started = time.perf_counter()
    result = (process_runner or _run_process)(
        plan.command,
        cwd=plan.target,
        input=prompt,
        env=_process_environment(plan),
    )
    returncode, stdout, stderr = _process_output(result)
    plan.stream_path.write_text(stdout, encoding="utf-8")
    plan.stderr_path.write_text(stderr, encoding="utf-8")
    elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
    try:
        usage = collect_stream(plan.stream_path, plan.run_root)
    except CollectionError:
        plan.usage_path.touch(exist_ok=True)
        plan.turns_path.touch(exist_ok=True)
        raise
    _annotate_usage(plan, usage, elapsed_ms)
    _validate_gate_ledger(plan)
    _validate_contextmesh_evidence(plan)
    return 0 if returncode == 0 and usage["valid"] else 2


def _metadata(plan: ArmPlan, prepared_target: Path, *, executed: bool = False) -> dict[str, object]:
    return {
        "module": "M1c-executable-runner",
        "provider_execution": executed,
        "status": "executed" if executed else "prepared",
        "arm": plan.arm,
        "round": plan.round_id,
        "target": str(prepared_target),
        "prompt": str(plan.prompt_path),
        "stream": str(plan.stream_path),
        "stderr": str(plan.stderr_path),
        "usage": str(plan.usage_path),
        "turns": str(plan.turns_path),
        "manifest": str(plan.manifest_path),
        "agents": str(plan.agents_path),
        "settings": str(plan.settings_path),
        "gate": str(plan.gate_path),
        "gate_state": str(plan.gate_state_dir),
        "contextmesh": str(plan.cm_path),
        "mcp_config": None if plan.mcp_config_path is None else str(plan.mcp_config_path),
        "command": plan.command,
        "tools": list(ARM_TOOLS[plan.arm]),
        "safety": plan.safety,
    }


def _report_metadata(
    plan: ArmPlan,
    arm_roots: Mapping[str, ArmPlan],
    result: Mapping[str, Any],
) -> dict[str, object]:
    round_root = plan.metrics_root / plan.round_id
    return {
        "module": "M1c-executable-runner",
        "provider_execution": True,
        "status": "reported",
        "round": plan.round_id,
        "report": str(round_root / "report.json"),
        "report_markdown": str(round_root / "REPORT.md"),
        "arm_roots": {arm: str(item.run_root) for arm, item in arm_roots.items()},
        "result": dict(result),
    }


def _write_round_report(
    plan: ArmPlan,
    arm_roots: Mapping[str, ArmPlan],
) -> dict[str, Any]:
    roots = {arm: item.run_root for arm, item in arm_roots.items()}
    result = report_round(roots)
    rendered = render_round(result)
    round_root = plan.metrics_root / plan.round_id
    round_root.mkdir(parents=True, exist_ok=True)
    _write_json(round_root / "report.json", result)
    (round_root / "REPORT.md").write_text(rendered.rstrip() + "\n", encoding="utf-8")
    return _report_metadata(plan, arm_roots, result)


def _pilot_invocation_metadata(plan: CodexInvocation) -> dict[str, object]:
    return {
        "role": plan.role,
        "model": plan.model,
        "artifact_root": str(plan.artifact_root),
        "stream": str(plan.stream_path),
        "final": str(plan.final_path),
        "target": str(plan.target),
        "usage": str(plan.usage_path),
        "command": plan.command,
    }


def _pilot_metadata(plan: CodexPilotPlan) -> dict[str, object]:
    return {
        "module": "M1b-runner",
        "label": "Codex-pilot",
        "provider_execution": False,
        "status": "planned",
        "round": plan.round_id,
        "topology": "1+4",
        "orchestrators": 1,
        "workers": 4,
        "worker_batches": 1,
        "orchestrator": _pilot_invocation_metadata(plan.orchestrator),
        "worker_plans": [_pilot_invocation_metadata(worker) for worker in plan.workers],
        "claude_cache_metrics": None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run opt-in provider-backed M1c arms")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--metrics-root", default="metrics")
    parser.add_argument("--execute", action="store_true", help=argparse.SUPPRESS)
    modes = parser.add_subparsers(dest="mode", required=True)

    arm_parser = modes.add_parser("arm", help="run one arm")
    arm_parser.add_argument("arm")
    arm_parser.add_argument("round_id")
    arm_parser.add_argument("--execute", action="store_true", default=argparse.SUPPRESS)

    round_parser = modes.add_parser("round", help="run raw, contextmesh, then full")
    round_parser.add_argument("round_id")
    round_parser.add_argument("--arms", nargs=3, default=ARMS)
    round_parser.add_argument("--execute", action="store_true", default=argparse.SUPPRESS)

    pilot_parser = modes.add_parser("pilot", help="plan the Codex pilot")
    pilot_parser.add_argument("round_id")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    process_runner: ProcessRunner | None = None,
    materializer: Materializer | None = None,
) -> int:
    args = _parser().parse_args(argv)
    if args.execute and os.environ.get(PROVIDER_OPT_IN_ENV) != "1":
        print(
            f"--execute is disabled; set {PROVIDER_OPT_IN_ENV}=1 for the explicit provider opt-in",
            file=os.sys.stderr,
        )
        return 2

    if args.mode == "pilot":
        try:
            plan = build_codex_pilot_plan(args.repo_root, args.round_id, args.metrics_root)
        except (ValueError, OSError) as exc:
            print(f"runner error: {exc}", file=os.sys.stderr)
            return 2
        print(json.dumps(_pilot_metadata(plan), sort_keys=True))
        return 0

    arm_plans: dict[str, ArmPlan] = {}
    try:
        arms = (args.arm,) if args.mode == "arm" else tuple(args.arms)
        if args.mode == "round" and sorted(arms) != sorted(ARMS):
            raise ValueError("--arms must be a permutation of raw, contextmesh, full")
        for arm in arms:
            plan = build_arm_plan(args.repo_root, arm, args.round_id, args.metrics_root)
            arm_plans[arm] = plan
        if args.execute:
            diagnostics: dict[str, Mapping[str, str]] = {}
            for arm in arms:
                plan = arm_plans[arm]
                metadata = _metadata(plan, plan.target, executed=True)
                if _retained_complete_arm(plan):
                    metadata["status"] = "retained_complete"
                    print(json.dumps(metadata, sort_keys=True))
                    continue
                if _has_retained_attempt(plan):
                    diagnostic = _write_diagnostic(
                        plan,
                        "degraded",
                        "retained partial provider artifacts are not replayed automatically",
                    )
                    diagnostics[arm] = diagnostic
                    metadata.update(diagnostic)
                    print(json.dumps(metadata, sort_keys=True))
                    continue
                try:
                    status = execute_arm(
                        plan,
                        materializer=materializer,
                        process_runner=process_runner,
                    )
                except (
                    CollectionError,
                    FileExistsError,
                    NotADirectoryError,
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as exc:
                    detail = f"{type(exc).__name__}: {exc}"
                    category = "blocking_product_error" if _is_product_failure(detail) else "degraded"
                    diagnostic = _write_diagnostic(plan, category, detail)
                    diagnostics[arm] = diagnostic
                    metadata.update(diagnostic)
                    print(json.dumps(metadata, sort_keys=True))
                    if category == "blocking_product_error":
                        _write_round_progress(plan, arm_plans, diagnostics)
                        return 2
                    continue
                if status:
                    detail = "provider process returned nonzero or produced invalid retained evidence"
                    try:
                        detail = plan.stderr_path.read_text(encoding="utf-8") or detail
                    except OSError:
                        pass
                    category = "blocking_product_error" if _is_product_failure(detail) else "degraded"
                    diagnostic = _write_diagnostic(plan, category, detail)
                    diagnostics[arm] = diagnostic
                    metadata.update(diagnostic)
                    print(json.dumps(metadata, sort_keys=True))
                    if category == "blocking_product_error":
                        _write_round_progress(plan, arm_plans, diagnostics)
                        return 2
                    continue
                print(json.dumps(metadata, sort_keys=True))
            if args.mode == "round":
                if diagnostics:
                    _write_round_progress(plan, arm_plans, diagnostics)
                    print(json.dumps({"status": "degraded", "round": plan.round_id}, sort_keys=True))
                    return 0
                try:
                    report_metadata = _write_round_report(plan, arm_plans)
                except (OSError, ReportError, TypeError, ValueError) as exc:
                    diagnostics["report"] = {
                        "status": "degraded",
                        "arm": "report",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                    _write_round_progress(plan, arm_plans, diagnostics)
                    print(json.dumps({"status": "degraded", "round": plan.round_id}, sort_keys=True))
                    return 0
                print(json.dumps(report_metadata, sort_keys=True))
        else:
            for arm in arms:
                plan = arm_plans[arm]
                _prepare_run(plan, materializer or _dry_run_materializer, require_manifest=False)
                print(json.dumps(_metadata(plan, plan.target), sort_keys=True))
    except (
        CollectionError,
        ReportError,
        FileExistsError,
        NotADirectoryError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"runner error: {exc}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI.
    raise SystemExit(main())


__all__ = [
    "ARMS",
    "ARM_TOOLS",
    "ArmPlan",
    "CodexInvocation",
    "CodexPilotPlan",
    "Materializer",
    "NO_CONTEXTMESH_EVENT",
    "PROVIDER_OPT_IN_ENV",
    "ProcessRunner",
    "SAFETY",
    "build_arm_plan",
    "build_codex_pilot_plan",
    "build_parent_prompt",
    "build_pilot_plan",
    "execute_arm",
    "main",
    "prepare_arm",
]
