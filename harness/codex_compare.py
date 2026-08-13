"""Prepare and run the guarded Codex ContextMesh + RRCv2 comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:  # Support the documented ``python harness/codex_compare.py`` entry point.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextmesh.bench.rrc_long_spec_demo import materialize as ruleforge_materialize
from contextmesh.mcp.shared_broker import MAX_PLAN_SCOPED_SOURCE_VIEW_BYTES

from harness.collect_codex import CollectionError, collect_codex_pilot
from harness.deepseek_delegate import MODEL as DEEPSEEK_MODEL
from harness.deepseek_delegate import command as deepseek_command
from harness.four_worker_plan import (
    WorkerPlan,
    build_overlap_ledger,
    freeze_worker_plans,
    manifest_sha256,
)
from harness.mcp_health import mcp_config
from harness.mcp_model_probe import run as run_mcp_model_probe
from harness.rrc_hit import RRCManifestError, warm_rrc_cache
from harness.source_policy import direct_overlap_reads, direct_unlisted_source_reads
from harness.terra_plans import (
    TerraPlanError,
    load_terra_plans,
    plan_set_sha256,
    write_task_request,
)
from harness.workload_preflight import require_capacity, source_mass
from harness.workspace import materialize_worker_workspace, materialize_workspace

ARMS = ("raw", "contextmesh", "full")
TERRA_MODEL = "gpt-5.6-terra"
AUTO_COMPACT_TOKEN_LIMIT = 230_000
WORKER_MODEL = DEEPSEEK_MODEL
PRIMARY_FIELDS = ("input_tokens", "output_tokens", "reasoning_output_tokens")
CACHE_FIELDS = ("cached_input_tokens", "cache_write_input_tokens")
MARGINAL_FIELDS = ("input_new_tokens", "output_tokens", "reasoning_output_tokens")
MIN_COMPONENT_REDUCTION = 0.15
MIN_FULL_REDUCTION = 0.15
# A peer may need to wait while a Luna reads and publishes a large owned
# overlap.  This stays below the dispatcher's 15-minute bounded arm window.
BROKER_WAIT_TIMEOUT_MS = 600_000
RUNNER_SOURCE_FILES = (
    "harness/codex_compare.py",
    "harness/deepseek_delegate.py",
    ".codex/delegates/deepseek.toml",
    ".codex/delegates/deepseek-model-catalog.json",
    "harness/collect_codex.py",
    "harness/four_worker_plan.py",
    "harness/mcp_health.py",
    "harness/mcp_model_probe.py",
    "harness/terra_plans.py",
    "harness/worker_packets.py",
    "harness/workload_preflight.py",
    "harness/workspace.py",
    "harness/rrc_hit.py",
    "harness/rrc_stage.py",
    "harness/staged_local_proof.py",
    "harness/staged_codex.py",
    "harness/staged_workload.py",
    "contextmesh/bench/rrc_long_spec_demo.py",
    "contextmesh/mcp/advance_stage.py",
    "contextmesh/mcp/bridge.py",
    "contextmesh/mcp/broker_service.py",
    "contextmesh/mcp/file_brief.py",
    "contextmesh/mcp/shared_broker.py",
    "rrc/orchestrator_runtime.py",
    "rrc/store.py",
)
WORKSPACE_INSTRUCTIONS = """# Measured RuleForge task workspace

This is an isolated disposable task target. Work only inside this directory.
Do not run Git commands, inspect parent directories, or inspect unrelated
repository files. Do not use `git diff`, `git status`, or `git show`.
The harness exposes direct source bodies only where your frozen packet permits
them. Other runtime dependencies are sourceless bytecode. Do not inspect,
disassemble, or recover those files; use the coordinator plan and, in non-raw
arms, ContextMesh briefs. After the requested focused test passes, return the
final handoff immediately.
"""


class ComparisonError(ValueError):
    """Raised for an invalid comparison configuration or retained artifact."""


@dataclass(frozen=True)
class Invocation:
    role: str
    model: str
    root: Path
    target: Path
    prompt: Path
    stream: Path
    stderr: Path
    final: Path
    cm_log: Path
    contextmesh_app_id: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class ArmPlan:
    arm: str
    cohort: str
    repo_root: Path
    root: Path
    parent: Invocation
    terra_source: Invocation
    terra_source_arm: str
    workers: tuple[Invocation, ...]
    dispatch: Path
    child_records: Path
    broker_ledger: Path
    broker_port_file: Path
    broker_log: Path
    broker_state: Path
    terra_evidence: Path
    rrc_hit: Path
    rrc_warm: Path
    terra_plan_root: Path
    terra_request: Path
    dispatch_ready: Path
    mcp_health: Path
    mcp_model_probe: Path
    dispatch_error: Path


@dataclass(frozen=True)
class RoundPlan:
    repo_root: Path
    metrics_root: Path
    round_id: str
    root: Path
    arms: tuple[ArmPlan, ...]
    workload_root: Path
    comparability_fingerprint: str
    worker_plans: tuple[WorkerPlan, ...]
    cohort: str


def _round_name(value: str | int) -> str:
    name = str(value)
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ComparisonError("round_id must be one safe path component")
    return name


def _worker_python() -> str:
    """Use the same interpreter for hidden runtime modules and Luna test commands."""

    return shutil.which("python") or sys.executable


def _runner_source_snapshot(repo: Path) -> dict[str, Any]:
    """Fingerprint code shared by the parent and detached dispatcher.

    A paid round must not mix a long-lived parent's imported code with a later
    edit that a detached dispatcher imports from disk.
    """

    files: dict[str, str] = {}
    for relative in RUNNER_SOURCE_FILES:
        path = repo / relative
        if not path.is_file():
            raise ComparisonError(f"required runner source is missing: {relative}")
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"schema_version": 1, "files": files, "sha256": _canonical_hash(files)}


def _runner_source_path(plan: RoundPlan) -> Path:
    return plan.root / "runner-source.json"


def _runner_source_is_frozen(plan: RoundPlan) -> tuple[bool, str | None]:
    path = _runner_source_path(plan)
    try:
        retained = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
        return False, f"missing or unreadable runner source snapshot: {error}"
    current = _runner_source_snapshot(plan.repo_root)
    if retained != current:
        return False, "runner source changed after round preparation; retain this attempt and prepare a fresh round"
    return True, None


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _worker_contract_sha256(plans: Sequence[WorkerPlan]) -> str:
    """Hash the fixed task contract without substituting a coordinator plan."""

    return manifest_sha256(plans)


def _config_fingerprint(worker_manifest_hash: str, cohort: str) -> str:
    return _canonical_hash(
        {
            "topology": {"parents": 1, "workers": 4, "dispatch_batches": 1},
            "parent": {"model": TERRA_MODEL, "reasoning": "high", "auto_compact": AUTO_COMPACT_TOKEN_LIMIT},
            "worker": {"model": WORKER_MODEL, "reasoning": "high", "provider": "ollama", "auto_compact": AUTO_COMPACT_TOKEN_LIMIT},
            "common_exec": {
                "ignore_user_config": True,
                "fast_mode": True,
                "service_tier": "priority",
                "bypass": True,
                "json": True,
            },
            "worker_manifest_hash": worker_manifest_hash,
            "cohort": cohort,
            "contextmesh": "one-arm-local-broker-with-per-worker-stdio-bridges",
        }
    )


def _codex_command(
    *,
    model: str,
    reasoning: str,
    final: Path,
    contextmesh_bridge: Path | None,
) -> tuple[str, ...]:
    if model == WORKER_MODEL:
        repo = contextmesh_bridge.parents[2] if contextmesh_bridge is not None else Path(__file__).resolve().parents[1]
        return deepseek_command(repo, final, extra_config=mcp_config(repo) if contextmesh_bridge is not None else ())
    command = [
        "codex",
        "exec",
        "--ignore-user-config",
        "--enable",
        "fast_mode",
        "--model",
        model,
        "--config",
        f"model_reasoning_effort={reasoning}",
        "--config",
        f"model_auto_compact_token_limit={AUTO_COMPACT_TOKEN_LIMIT}",
        "--config",
        "service_tier=priority",
    ]
    if contextmesh_bridge is not None:
        for item in mcp_config(contextmesh_bridge.parents[2]):
            command.extend(("--config", item))
    command.extend(
        (
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "--output-last-message",
            str(final),
            "-",
        )
    )
    return tuple(command)


def build_round_plan(
    repo_root: str | Path, round_id: str | int, metrics_root: str | Path = "metrics", cohort: str = "core"
) -> RoundPlan:
    repo = Path(repo_root).resolve()
    metrics = Path(metrics_root)
    if not metrics.is_absolute():
        metrics = repo / metrics
    metrics = metrics.resolve()
    name = _round_name(round_id)
    root = metrics / name / "codex-comparison"
    bridge = repo / "contextmesh" / "mcp" / "bridge.py"
    broker = repo / "contextmesh" / "mcp" / "broker_service.py"
    if not bridge.is_file() or not broker.is_file():
        raise ComparisonError("missing ContextMesh shared broker or worker bridge")
    worker_plans = freeze_worker_plans(cohort)
    worker_manifest_hash = manifest_sha256(worker_plans)
    plans: list[ArmPlan] = []
    contextmesh_app_id = f"codex-comparison-{name}"
    for arm in ARMS:
        arm_root = root / arm
        uses_contextmesh = arm != "raw"
        parent_root = arm_root / "orchestrator"
        parent = Invocation(
            role="orchestrator",
            model=TERRA_MODEL,
            root=parent_root,
            target=arm_root / "terra-worktree",
            prompt=parent_root / "prompt.md",
            stream=parent_root / "stream.jsonl",
            stderr=parent_root / "stderr.log",
            final=parent_root / "final.md",
            cm_log=parent_root / "cm.jsonl",
            contextmesh_app_id=contextmesh_app_id,
            command=_codex_command(
                model=TERRA_MODEL,
                reasoning="high",
                final=parent_root / "final.md",
                contextmesh_bridge=None,
            ),
        )
        workers = tuple(
            Invocation(
                role=f"worker-{index:02d}",
                model=WORKER_MODEL,
                root=arm_root / "workers" / f"worker-{index:02d}",
                target=arm_root / "worktrees" / f"worker-{index:02d}",
                prompt=arm_root / "workers" / f"worker-{index:02d}" / "prompt.md",
                stream=arm_root / "workers" / f"worker-{index:02d}" / "stream.jsonl",
                stderr=arm_root / "workers" / f"worker-{index:02d}" / "stderr.log",
                final=arm_root / "workers" / f"worker-{index:02d}" / "final.md",
                cm_log=arm_root / "workers" / f"worker-{index:02d}" / "cm.jsonl",
                contextmesh_app_id=contextmesh_app_id,
                command=_codex_command(
                    model=WORKER_MODEL,
                    reasoning="high",
                    final=arm_root / "workers" / f"worker-{index:02d}" / "final.md",
                    contextmesh_bridge=bridge if uses_contextmesh else None,
                ),
            )
            for index in range(1, 5)
        )
        plans.append(
            ArmPlan(
                arm=arm,
                cohort=cohort,
                repo_root=repo,
                root=arm_root,
                parent=parent,
                terra_source=parent,
                terra_source_arm=arm,
                workers=workers,
                dispatch=arm_root / "dispatch.ps1",
                child_records=arm_root / "children.json",
                broker_ledger=arm_root / "broker" / "ledger.json",
                broker_port_file=arm_root / "broker" / "port.json",
                broker_log=arm_root / "broker" / "events.jsonl",
                broker_state=arm_root / "broker" / "state",
                terra_evidence=parent_root / "terra-evidence.json",
                rrc_hit=parent_root / "rrc-hit.json",
                rrc_warm=parent.target / ".rrc-cache" / "rrc-warm.json",
                terra_plan_root=parent.target / ".terra-plans",
                terra_request=parent.target / ".terra-plan-request.json",
                dispatch_ready=arm_root / "dispatch-ready.json",
                mcp_health=arm_root / "broker" / "mcp-health.json",
                mcp_model_probe=root / "mcp-eligibility-probe",
                dispatch_error=arm_root / "dispatch-error.txt",
            )
        )
    # Raw and ContextMesh must dispatch the identical retained raw-Terra plan.
    # Otherwise a coordinator hallucination or a better plan can masquerade as
    # a worker-source-sharing effect. Full remains the separate RRC-rendered
    # plan alternative whose Terra cost is compared against this raw planner.
    raw = next(item for item in plans if item.arm == "raw")
    contextmesh_index = next(index for index, item in enumerate(plans) if item.arm == "contextmesh")
    contextmesh = plans[contextmesh_index]
    plans[contextmesh_index] = replace(
        contextmesh,
        terra_source=raw.parent,
        terra_source_arm="raw",
        terra_plan_root=raw.terra_plan_root,
    )
    return RoundPlan(
        repo_root=repo,
        metrics_root=metrics,
        round_id=name,
        root=root,
        arms=tuple(plans),
        workload_root=root / "shared-workload",
        comparability_fingerprint=_config_fingerprint(worker_manifest_hash, cohort),
        worker_plans=worker_plans,
        cohort=cohort,
    )


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _terra_preflight_paths(plans: Sequence[WorkerPlan]) -> tuple[str, ...]:
    return tuple(sorted({path for plan in plans for path in plan.initial_read_paths}))


def _terra_prompt(arm: ArmPlan, plans: Sequence[WorkerPlan]) -> str:
    """Give the measured Terra process an evidence-producing coordinator task."""

    dispatch = (
        "$dispatchProcess = Start-Process -FilePath powershell.exe -ArgumentList "
        f"'-NoProfile','-ExecutionPolicy','Bypass','-File','{arm.dispatch}' -WindowStyle Hidden"
    )
    if arm.arm == "full":
        return (
            "You are the one Terra coordinator for this measured four-worker run. Do not reconstruct plans and do not "
            "read any RuleForge source file. The generic RRC cache was warmed and verified before this measured coordinator began. "
            "First run the exact local EverOS-backed RRCv2 lookup command below once; it must retain a valid HIT artifact. Then run the cache renderer command once; it writes four resolved coordinator "
            "plan artifacts. Do not inspect "
            "or copy a generic packet into a worker prompt. Then start the prepared dispatcher exactly once; it starts "
            "all four Luna workers in one batch. Do not edit source or delegate. End your final handoff with the exact marker "
            "`TERRA_RRC_HIT_RENDER <cache_key from the retained artifact>`.\n\n"
            f"Run exactly: python -m harness.rrc_hit --workspace . --output {arm.rrc_hit}\n"
            f"Then run exactly: python -m harness.terra_plans render-rrc --workspace . --output {arm.terra_plan_root} --cohort {arm.cohort}\n\n"
            + dispatch
            + "\n"
        )
    paths = _terra_preflight_paths(plans)
    fingerprint = hashlib.sha256("\n".join(paths).encode("utf-8")).hexdigest()
    return (
        "You are the one Terra coordinator for this measured four-worker run. Before dispatching workers, perform "
        "the raw planning preflight yourself: read every declared RuleForge source path below from this Terra worktree. "
        "Then create four concrete JSON coordinator plans at the exact plan_output paths listed in the request artifact. Each must match "
        "the exact schema in that artifact and contain source-derived facts and ordered steps for that task. The request is a "
        "task contract, not a plan; do not reuse a prewritten packet. Do not edit RuleForge source or delegate. After writing "
        "all four plans, start the prepared dispatcher exactly once; it starts all four Luna workers in one batch. End your final handoff with the exact marker "
        f"`TERRA_RAW_PREFLIGHT {fingerprint}`.\n\n"
        + "Declared preflight paths:\n"
        + "\n".join(f"- {path}" for path in paths)
        + "\n\n"
        + f"Coordinator plan request: {arm.terra_request}\n"
        + f"Plan output directory: {arm.terra_plan_root}\n\n"
        + dispatch
        + "\n"
    )


def _dispatch_script(arm: ArmPlan) -> str:
    entries: list[dict[str, Any]] = []
    for worker in arm.workers:
        entries.append(
            {
                "role": worker.role,
                "prompt": str(worker.prompt),
                "stream": str(worker.stream),
                "stderr": str(worker.stderr),
                "target": str(worker.target),
                "cm_log": str(worker.cm_log),
                "contextmesh_app_id": worker.contextmesh_app_id,
                "worker_id": worker.role,
                "command": list(worker.command[1:-1]),
            }
        )
    plans = json.dumps(entries, separators=(",", ":")).replace("'", "''")
    record = str(arm.child_records).replace("'", "''")
    repo = str(arm.repo_root).replace("'", "''")
    broker_ledger = str(arm.broker_ledger).replace("'", "''")
    broker_port = str(arm.broker_port_file).replace("'", "''")
    broker_log = str(arm.broker_log).replace("'", "''")
    broker_state = str(arm.broker_state).replace("'", "''")
    arm_root = str(arm.root).replace("'", "''")
    terra_plan_root = str(arm.terra_plan_root).replace("'", "''")
    dispatch_ready = str(arm.dispatch_ready).replace("'", "''")
    mcp_health = str(arm.mcp_health).replace("'", "''")
    dispatch_error = str(arm.dispatch_error).replace("'", "''")
    broker_enabled = "$true" if arm.arm != "raw" else "$false"
    return f'''$ErrorActionPreference = 'Stop'
$plans = '{plans}' | ConvertFrom-Json
$children = @()
$codex = (Get-Command codex -ErrorAction Stop).Source
$broker = $null
$brokerEndpoint = $null
try {{
    $env:PYTHONPATH = '{repo}'
    try {{
        & python -m harness.terra_plans prepare-dispatch --arm '{arm.arm}' --arm-root '{arm_root}' --terra-plan-root '{terra_plan_root}' --cohort '{arm.cohort}'
        if ($LASTEXITCODE -ne 0) {{ throw 'Terra coordinator plans could not be prepared for dispatch' }}
        if (-not (Test-Path -LiteralPath '{dispatch_ready}')) {{ throw 'Terra coordinator plans did not produce dispatch readiness evidence' }}
    }} catch {{
        [System.IO.File]::WriteAllText('{dispatch_error}', $_.Exception.Message, (New-Object System.Text.UTF8Encoding($false)))
        throw
    }}
    if ({broker_enabled}) {{
        $brokerPortFile = '{broker_port}'
        $brokerStdout = Join-Path (Split-Path -Parent $brokerPortFile) 'broker.stdout.log'
        $brokerStderr = Join-Path (Split-Path -Parent $brokerPortFile) 'broker.stderr.log'
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $brokerPortFile) | Out-Null
        $brokerArgs = @('-m', 'contextmesh.mcp.broker_service', '--source-root', '{str(arm.parent.target).replace("'", "''")}', '--ledger', '{broker_ledger}', '--state-dir', '{broker_state}', '--log', '{broker_log}', '--port-file', $brokerPortFile, '--wait-timeout-ms', '{BROKER_WAIT_TIMEOUT_MS}')
        $broker = Start-Process -FilePath python -ArgumentList $brokerArgs -WorkingDirectory '{repo}' -RedirectStandardOutput $brokerStdout -RedirectStandardError $brokerStderr -WindowStyle Hidden -PassThru
        for ($attempt = 0; $attempt -lt 50 -and -not (Test-Path -LiteralPath $brokerPortFile); $attempt++) {{ Start-Sleep -Milliseconds 100 }}
        if (-not (Test-Path -LiteralPath $brokerPortFile)) {{ throw 'ContextMesh broker did not publish its port file' }}
        $brokerEndpoint = Get-Content -Raw -LiteralPath $brokerPortFile | ConvertFrom-Json
        $env:CONTEXTMESH_BROKER_HOST = $brokerEndpoint.host
        $env:CONTEXTMESH_BROKER_PORT = [string]$brokerEndpoint.port
        $env:CONTEXTMESH_WORKER_ID = 'worker-01'
        & python -m harness.mcp_health --output '{mcp_health}' --codex $codex --repo-root '{repo}'
        if ($LASTEXITCODE -ne 0) {{
            [System.IO.File]::WriteAllText('{dispatch_error}', 'ContextMesh MCP bridge health gate failed before worker launch', (New-Object System.Text.UTF8Encoding($false)))
            throw 'ContextMesh MCP bridge health gate failed before worker launch'
        }}
    }}
    foreach ($plan in $plans) {{
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $plan.stream) | Out-Null
        $previousLog = $env:CONTEXTMESH_LOG
        $previousAppId = $env:CONTEXTMESH_APP_ID
        $previousHost = $env:CONTEXTMESH_BROKER_HOST
        $previousPort = $env:CONTEXTMESH_BROKER_PORT
        $previousWorker = $env:CONTEXTMESH_WORKER_ID
        $env:CONTEXTMESH_LOG = $plan.cm_log
        $env:CONTEXTMESH_APP_ID = $plan.contextmesh_app_id
        $env:REASONRENDER_ARM = '{arm.arm}'
        $env:PYTHONPATH = '{repo}'
        if ($null -ne $brokerEndpoint) {{
            $env:CONTEXTMESH_BROKER_HOST = $brokerEndpoint.host
            $env:CONTEXTMESH_BROKER_PORT = [string]$brokerEndpoint.port
            $env:CONTEXTMESH_WORKER_ID = $plan.worker_id
        }}
        try {{
            $process = Start-Process -FilePath $codex -ArgumentList $plan.command -WorkingDirectory $plan.target -RedirectStandardInput $plan.prompt -RedirectStandardOutput $plan.stream -RedirectStandardError $plan.stderr -WindowStyle Hidden -PassThru
        }} finally {{
            $env:CONTEXTMESH_LOG = $previousLog
            $env:CONTEXTMESH_APP_ID = $previousAppId
            $env:CONTEXTMESH_BROKER_HOST = $previousHost
            $env:CONTEXTMESH_BROKER_PORT = $previousPort
            $env:CONTEXTMESH_WORKER_ID = $previousWorker
        }}
        $children += [pscustomobject]@{{ role = $plan.role; pid = $process.Id; process = $process }}
    }}
    foreach ($child in $children) {{ $child.process.WaitForExit() }}
    $streamDeadline = [DateTime]::UtcNow.AddMinutes(15)
    $streamCompleted = $false
    while ([DateTime]::UtcNow -lt $streamDeadline) {{
        $completedStreams = @($plans | Where-Object {{
            (Test-Path -LiteralPath $_.stream) -and (Select-String -LiteralPath $_.stream -Pattern '"type":"turn.completed"' -Quiet)
        }})
        if ($completedStreams.Count -eq $plans.Count) {{ $streamCompleted = $true; break }}
        Start-Sleep -Milliseconds 250
    }}
    $records = @($children | ForEach-Object {{
        [pscustomobject]@{{ role = $_.role; pid = $_.pid; exit_code = $_.process.ExitCode; exit_observed = ($null -ne $_.process.ExitCode); stream_completed = $streamCompleted }}
    }})
    [System.IO.File]::WriteAllText('{record}', ($records | ConvertTo-Json -Depth 4), (New-Object System.Text.UTF8Encoding($false)))
}} finally {{
    if ($null -ne $broker -and -not $broker.HasExited) {{ Stop-Process -Id $broker.Id -Force }}
}}
'''


def prepare_round(plan: RoundPlan) -> dict[str, Any]:
    """Create one shared workload and three isolated target copies without providers."""

    if plan.root.exists():
        raise FileExistsError(f"comparison artifact already exists: {plan.root}")
    plan.workload_root.mkdir(parents=True)
    manifest = ruleforge_materialize(plan.workload_root, plan.cohort)
    manifest_path = plan.workload_root / "manifest.json"
    workspace = plan.workload_root / "workspace"
    if not manifest_path.is_file() or not workspace.is_dir():
        raise ComparisonError("RuleForge materializer did not produce manifest.json and workspace")
    try:
        mass = source_mass(workspace, plan.worker_plans)
        require_capacity(mass)
    except ValueError as error:
        raise ComparisonError(str(error)) from error
    _write_json(plan.root / "workload-source-mass.json", mass)
    tasks = manifest.get("tasks") if isinstance(manifest, Mapping) else None
    if not isinstance(tasks, list) or len(tasks) != 4 or not all(isinstance(task, Mapping) for task in tasks):
        raise ComparisonError("RuleForge manifest must contain exactly four task objects")
    if tuple(str(task["task_id"]) for task in tasks) != tuple(plan.task_id for plan in plan.worker_plans):
        raise ComparisonError("frozen worker plans do not match the materialized RuleForge task order")
    workload_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    runner_source = _runner_source_snapshot(plan.repo_root)
    _write_json(_runner_source_path(plan), runner_source)
    common = {
        "round": plan.round_id,
        "cohort": plan.cohort,
        "workload_manifest": str(manifest_path),
        "workload_manifest_sha256": workload_hash,
        "comparability_fingerprint": plan.comparability_fingerprint,
        "worker_contract_sha256": _worker_contract_sha256(plan.worker_plans),
        "topology": "1+4",
        "marginal_compute": "input_new_tokens + output_tokens + reasoning_output_tokens",
        "gross_compute": "input_tokens + output_tokens + reasoning_output_tokens",
        "cache_reporting": "cached_input_tokens and cache_write_input_tokens are reported separately and do not enter marginal compute",
        "runner_source_sha256": runner_source["sha256"],
        "worker_python": _worker_python(),
    }
    for arm in plan.arms:
        terra_target = materialize_workspace(workspace, arm.parent.target)
        if terra_target != arm.parent.target:
            raise ComparisonError("workspace materialization returned an unexpected target")
        (terra_target / "AGENTS.md").write_text(WORKSPACE_INSTRUCTIONS, encoding="utf-8")
        write_task_request(arm.terra_request, plan.worker_plans)
        if arm.arm != "raw":
            arm.broker_ledger.parent.mkdir(parents=True, exist_ok=True)
        overlap_paths = {
            entry.canonical_path
            for entry in build_overlap_ledger(plan.worker_plans, manifest_sha256(plan.worker_plans))
        }
        for worker, worker_plan in zip(arm.workers, plan.worker_plans, strict=True):
            raw_sources = worker_plan.initial_read_paths if arm.arm == "raw" else tuple(
                path for path in worker_plan.initial_read_paths if path not in overlap_paths
            )
            target = materialize_worker_workspace(
                workspace,
                worker.target,
                raw_source_paths=raw_sources,
                owned_write_paths=worker_plan.owned_write_paths,
                python_executable=_worker_python(),
            )
            if target != worker.target:
                raise ComparisonError("worker workspace materialization returned an unexpected target")
            (target / "AGENTS.md").write_text(WORKSPACE_INSTRUCTIONS, encoding="utf-8")
            worker.root.mkdir(parents=True, exist_ok=True)
        arm.parent.root.mkdir(parents=True, exist_ok=True)
        arm.dispatch.write_text(_dispatch_script(arm), encoding="utf-8")
        arm.parent.prompt.write_text(_terra_prompt(arm, plan.worker_plans), encoding="utf-8")
        _write_json(
            arm.root / "comparison.json",
            {
                **common,
                "arm": arm.arm,
                "target": str(arm.parent.target),
                "terra_evidence": str(arm.terra_evidence),
                "rrc_hit": str(arm.rrc_hit) if arm.arm == "full" else None,
                "rrc_cache_warm": str(arm.rrc_warm) if arm.arm == "full" else None,
                "parent": _invocation_metadata(arm.parent),
                "terra_plan_source_arm": arm.terra_source_arm,
                "terra_plan_source_stream": str(arm.terra_source.stream),
                "workers": [_invocation_metadata(worker) for worker in arm.workers],
            },
        )
    _write_json(plan.root / "comparison.json", {**common, "arms": list(ARMS)})
    return {**common, "arms": list(ARMS)}


def prepare_or_resume_round(plan: RoundPlan) -> dict[str, Any]:
    """Prepare a fresh round, or retain a usable partial round without rewriting it."""

    if not plan.root.exists():
        return {"resumed": False, "prepared": True, **prepare_round(plan)}
    root_metadata = plan.root / "comparison.json"
    arm_metadata = [arm.root / "comparison.json" for arm in plan.arms]
    if root_metadata.is_file() and all(path.is_file() for path in arm_metadata):
        frozen, diagnostic = _runner_source_is_frozen(plan)
        return {"resumed": True, "prepared": frozen, "root": str(plan.root), "diagnostic": diagnostic}
    return {
        "resumed": True,
        "prepared": False,
        "root": str(plan.root),
        "diagnostic": "retained comparison scaffolding is incomplete; no existing artifact was overwritten",
    }


def _invocation_metadata(invocation: Invocation) -> dict[str, Any]:
    return {
        "role": invocation.role,
        "model": invocation.model,
        "prompt": str(invocation.prompt),
        "stream": str(invocation.stream),
        "stderr": str(invocation.stderr),
        "final": str(invocation.final),
        "contextmesh_log": str(invocation.cm_log),
        "contextmesh_app_id": invocation.contextmesh_app_id,
        "command": list(invocation.command),
    }


def _stream_integrity(path: Path) -> tuple[bool, str | None]:
    """Require one or more completed turns before a retained stream can be collected."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        return False, f"stream unavailable: {error}"
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
    if encoding == "utf-8" and b"\x00" in raw:
        return False, "stream contains NUL-byte corruption"
    try:
        lines = raw.decode(encoding).splitlines()
    except UnicodeDecodeError as error:
        return False, f"stream is not {encoding}: {error}"
    if not lines:
        return False, "stream is empty"
    terminal = 0
    for number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return False, f"stream line {number} is not JSON"
        if isinstance(event, Mapping) and event.get("type") == "turn.completed":
            terminal += 1
    return (terminal >= 1, None if terminal >= 1 else "stream lacks a turn.completed event")


def _run_parent(arm: ArmPlan) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.update(
        {
            "CONTEXTMESH_LOG": str(arm.parent.cm_log),
            "CONTEXTMESH_APP_ID": arm.parent.contextmesh_app_id,
            "CONTEXTMESH_SUMMARIZER_COMMAND": "deterministic",
            "REASONRENDER_ARM": arm.arm,
            "CONTEXTMESH_RRC_PACKET": "1" if arm.arm == "full" else "0",
            "PYTHONPATH": str(arm.repo_root),
        }
    )
    started = time.monotonic()
    try:
        with arm.parent.stream.open("w", encoding="utf-8") as stdout, arm.parent.stderr.open(
            "w", encoding="utf-8"
        ) as stderr:
            completed = subprocess.run(
                arm.parent.command,
                cwd=arm.parent.target,
                input=arm.parent.prompt.read_text(encoding="utf-8"),
                text=True,
                stdout=stdout,
                stderr=stderr,
                env=environment,
                timeout=900,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return {
            "exit_code": None,
            "timed_out": True,
            "wall_seconds": round(time.monotonic() - started, 3),
        }
    except OSError as exc:
        return {
            "exit_code": None,
            "orchestration_error": f"{type(exc).__name__}: {exc}",
            "wall_seconds": round(time.monotonic() - started, 3),
        }
    dispatch_deadline = time.monotonic() + 900
    while time.monotonic() < dispatch_deadline:
        if arm.dispatch_error.is_file():
            return {
                "exit_code": completed.returncode,
                "wall_seconds": round(time.monotonic() - started, 3),
                "dispatch_completed": False,
                "orchestration_error": arm.dispatch_error.read_text(encoding="utf-8", errors="replace"),
            }
        try:
            records = json.loads(arm.child_records.read_text(encoding="utf-8"))
            if (
                isinstance(records, list)
                and len(records) == 4
                and all(
                    isinstance(record, Mapping) and record.get("stream_completed") is True
                    for record in records
                )
                and all(_stream_integrity(worker.stream)[0] for worker in arm.workers)
            ):
                return {
                    "exit_code": completed.returncode,
                    "wall_seconds": round(time.monotonic() - started, 3),
                    "dispatch_completed": True,
                }
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        time.sleep(0.25)
    return {
        "exit_code": completed.returncode,
        "wall_seconds": round(time.monotonic() - started, 3),
        "dispatch_completed": False,
        "dispatch_timed_out": True,
    }


def _run_paired_dispatch(arm: ArmPlan) -> dict[str, Any]:
    """Run ContextMesh's dispatcher without launching a second Terra model."""

    environment = os.environ.copy()
    environment.update(
        {
            "CONTEXTMESH_APP_ID": arm.parent.contextmesh_app_id,
            "CONTEXTMESH_SUMMARIZER_COMMAND": "deterministic",
            "PYTHONPATH": str(arm.repo_root),
            "REASONRENDER_ARM": arm.arm,
        }
    )
    started = time.monotonic()
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(arm.dispatch)],
            cwd=arm.parent.target,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "exit_code": None,
            "mode": "paired_raw_plan",
            "source_arm": "raw",
            "timed_out": True,
            "wall_seconds": round(time.monotonic() - started, 3),
        }
    except OSError as exc:
        return {
            "exit_code": None,
            "mode": "paired_raw_plan",
            "source_arm": "raw",
            "orchestration_error": f"{type(exc).__name__}: {exc}",
            "wall_seconds": round(time.monotonic() - started, 3),
        }
    stdout = getattr(completed, "stdout", "")
    stderr = getattr(completed, "stderr", "")
    if isinstance(stdout, str) and stdout:
        (arm.root / "dispatcher.stdout.log").write_text(stdout, encoding="utf-8")
    if isinstance(stderr, str) and stderr:
        (arm.root / "dispatcher.stderr.log").write_text(stderr, encoding="utf-8")
    error = arm.dispatch_error.read_text(encoding="utf-8", errors="replace") if arm.dispatch_error.is_file() else None
    return {
        "exit_code": completed.returncode,
        "mode": "paired_raw_plan",
        "source_arm": "raw",
        "dispatch_completed": completed.returncode == 0 and error is None,
        "orchestration_error": error,
        "wall_seconds": round(time.monotonic() - started, 3),
    }


def _run_existing_dispatch(arm: ArmPlan, resume_root: Path) -> dict[str, Any]:
    """Launch only an arm's retained dispatcher; its Terra stream is never replayed."""

    environment = os.environ.copy()
    environment.update(
        {
            "CONTEXTMESH_APP_ID": arm.parent.contextmesh_app_id,
            "CONTEXTMESH_SUMMARIZER_COMMAND": "deterministic",
            "PYTHONPATH": str(arm.repo_root),
            "REASONRENDER_ARM": arm.arm,
        }
    )
    started = time.monotonic()
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(arm.dispatch)],
            cwd=arm.parent.target,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "exit_code": None,
            "mode": "retained_dispatch_resume",
            "timed_out": True,
            "wall_seconds": round(time.monotonic() - started, 3),
        }
    except OSError as exc:
        return {
            "exit_code": None,
            "mode": "retained_dispatch_resume",
            "orchestration_error": f"{type(exc).__name__}: {exc}",
            "wall_seconds": round(time.monotonic() - started, 3),
        }
    stdout = getattr(completed, "stdout", "")
    stderr = getattr(completed, "stderr", "")
    if isinstance(stdout, str) and stdout:
        (resume_root / "dispatcher.stdout.log").write_text(stdout, encoding="utf-8")
    if isinstance(stderr, str) and stderr:
        (resume_root / "dispatcher.stderr.log").write_text(stderr, encoding="utf-8")
    error = arm.dispatch_error.read_text(encoding="utf-8", errors="replace") if arm.dispatch_error.is_file() else None
    return {
        "exit_code": completed.returncode,
        "mode": "retained_dispatch_resume",
        "dispatch_completed": completed.returncode == 0 and error is None,
        "orchestration_error": error,
        "wall_seconds": round(time.monotonic() - started, 3),
    }


def _owned_briefs_are_published(arm: ArmPlan, worker: Invocation) -> bool:
    """Whether every overlap this Luna owns was durably published before resume."""

    try:
        ledger = json.loads(arm.broker_ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(ledger, list):
        return False
    expected = {
        item.get("brief_id")
        for item in ledger
        if isinstance(item, Mapping) and item.get("source_owner") == worker.role and isinstance(item.get("brief_id"), str)
    }
    if not expected:
        return False
    published: set[str] = set()
    for path in arm.broker_state.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            brief_id = payload.get("brief_id")
            binding = payload.get("brief", {}).get("binding", {})
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if isinstance(brief_id, str) and isinstance(binding, Mapping) and binding.get("source_owner") == worker.role:
            published.add(brief_id)
    return expected <= published


def _resume_worker_prompt(arm: ArmPlan, worker: Invocation) -> str:
    """Continue one retained Luna session after a harness-only interruption."""

    owner_clause = (
        "Every brief you own is already published and restored; do not call claim_source, read_source_chunk, or "
        "publish_file_brief again. "
        if _owned_briefs_are_published(arm, worker)
        else ""
    )
    return (
        "The previous turn was interrupted by the measurement harness, not by product code. "
        "Resume this exact retained task in the existing worktree now. The ContextMesh broker has been restarted "
        "from its already validated persisted briefs; do not repeat completed raw reads, do not restart the task, "
        "and do not inspect any source outside your frozen packet. If you previously returned "
        f"CONTEXTMESH_BRIEF_UNAVAILABLE, {owner_clause}retry the remaining packet-authorized broker step. Complete the implementation "
        "and its focused acceptance command, then return the normal final handoff."
    )


def _resume_workers_script(
    arm: ArmPlan, resume_root: Path, workers: Sequence[tuple[Invocation, str]]
) -> Path:
    """Write a one-broker continuation launcher for interrupted Luna sessions."""

    entries: list[dict[str, Any]] = []
    resumed_roles = {worker.role for worker, _thread_id in workers}
    for worker, thread_id in workers:
        worker_root = resume_root / worker.role
        worker_root.mkdir(parents=True, exist_ok=True)
        prompt = worker_root / "resume.md"
        resume_final = worker_root / "final.md"
        prompt.write_text(_resume_worker_prompt(arm, worker), encoding="utf-8")
        command = list(worker.command[2:-1])
        try:
            output_index = command.index("--output-last-message")
        except ValueError as error:  # Every measured Codex invocation must retain a handoff.
            raise ComparisonError(f"worker command lacks --output-last-message: {worker.role}") from error
        del command[output_index : output_index + 2]
        entries.append(
            {
                "role": worker.role,
                "thread_id": thread_id,
                "prompt": str(prompt),
                "resume_stream": str(worker_root / "stream.jsonl"),
                "resume_stderr": str(worker_root / "stderr.log"),
                "resume_final": str(resume_final),
                "stream": str(worker.stream),
                "stderr": str(worker.stderr),
                "final": str(worker.final),
                "target": str(worker.target),
                "cm_log": str(worker.cm_log),
                "contextmesh_app_id": worker.contextmesh_app_id,
                "worker_id": worker.role,
                "command": ["exec", "resume", *command, "--output-last-message", str(resume_final), thread_id, "-"],
            }
        )
    retained = [
        {"role": worker.role, "thread_id": _stream_thread_id(worker.stream)}
        for worker in arm.workers
        if worker.role not in resumed_roles
    ]
    plans = json.dumps(entries, separators=(",", ":")).replace("'", "''")
    retained_json = json.dumps(retained, separators=(",", ":")).replace("'", "''")
    repo = str(arm.repo_root).replace("'", "''")
    broker_ledger = str(arm.broker_ledger).replace("'", "''")
    broker_port = str(arm.broker_port_file).replace("'", "''")
    broker_log = str(arm.broker_log).replace("'", "''")
    broker_state = str(arm.broker_state).replace("'", "''")
    record = str(arm.child_records).replace("'", "''")
    script = resume_root / "resume-workers.ps1"
    script.write_text(
        f'''$ErrorActionPreference = 'Stop'
$plans = '{plans}' | ConvertFrom-Json
$retained = '{retained_json}' | ConvertFrom-Json
$children = @()
$codex = (Get-Command codex -ErrorAction Stop).Source
$broker = $null
try {{
    $env:PYTHONPATH = '{repo}'
    $brokerPortFile = '{broker_port}'
    Remove-Item -LiteralPath $brokerPortFile -Force -ErrorAction SilentlyContinue
    $brokerStdout = Join-Path (Split-Path -Parent $brokerPortFile) 'broker.resume.stdout.log'
    $brokerStderr = Join-Path (Split-Path -Parent $brokerPortFile) 'broker.resume.stderr.log'
    $brokerArgs = @('-m', 'contextmesh.mcp.broker_service', '--source-root', '{str(arm.parent.target).replace("'", "''")}', '--ledger', '{broker_ledger}', '--state-dir', '{broker_state}', '--log', '{broker_log}', '--port-file', $brokerPortFile, '--wait-timeout-ms', '{BROKER_WAIT_TIMEOUT_MS}')
    $broker = Start-Process -FilePath python -ArgumentList $brokerArgs -WorkingDirectory '{repo}' -RedirectStandardOutput $brokerStdout -RedirectStandardError $brokerStderr -WindowStyle Hidden -PassThru
    for ($attempt = 0; $attempt -lt 50 -and -not (Test-Path -LiteralPath $brokerPortFile); $attempt++) {{ Start-Sleep -Milliseconds 100 }}
    if (-not (Test-Path -LiteralPath $brokerPortFile)) {{ throw 'ContextMesh resume broker did not publish its port file' }}
    $brokerEndpoint = Get-Content -Raw -LiteralPath $brokerPortFile | ConvertFrom-Json
    foreach ($plan in $plans) {{
        $env:CONTEXTMESH_LOG = $plan.cm_log
        $env:CONTEXTMESH_APP_ID = $plan.contextmesh_app_id
        $env:REASONRENDER_ARM = '{arm.arm}'
        $env:CONTEXTMESH_BROKER_HOST = $brokerEndpoint.host
        $env:CONTEXTMESH_BROKER_PORT = [string]$brokerEndpoint.port
        $env:CONTEXTMESH_WORKER_ID = $plan.worker_id
        $process = Start-Process -FilePath $codex -ArgumentList $plan.command -WorkingDirectory $plan.target -RedirectStandardInput $plan.prompt -RedirectStandardOutput $plan.resume_stream -RedirectStandardError $plan.resume_stderr -WindowStyle Hidden -PassThru
        $children += [pscustomobject]@{{ role = $plan.role; pid = $process.Id; process = $process; plan = $plan }}
    }}
    foreach ($child in $children) {{ $child.process.WaitForExit() }}
    foreach ($child in $children) {{
        if (Test-Path -LiteralPath $child.plan.resume_stream) {{ Get-Content -Raw -LiteralPath $child.plan.resume_stream | Add-Content -LiteralPath $child.plan.stream -NoNewline }}
        if (Test-Path -LiteralPath $child.plan.resume_stderr) {{ Get-Content -Raw -LiteralPath $child.plan.resume_stderr | Add-Content -LiteralPath $child.plan.stderr -NoNewline }}
        if (Test-Path -LiteralPath $child.plan.resume_final) {{ Copy-Item -LiteralPath $child.plan.resume_final -Destination $child.plan.final -Force }}
    }}
    $records = @($children | ForEach-Object {{
        [pscustomobject]@{{ role = $_.role; pid = $_.pid; thread_id = $_.plan.thread_id; resumed = $true; exit_code = $_.process.ExitCode; stream_completed = (Select-String -LiteralPath $_.plan.stream -Pattern '"type":"turn.completed"' -Quiet) }}
    }})
    $records += @($retained | ForEach-Object {{
        [pscustomobject]@{{ role = $_.role; thread_id = $_.thread_id; retained_complete = $true; stream_completed = $true }}
    }})
    [System.IO.File]::WriteAllText('{record}', ($records | ConvertTo-Json -Depth 4), (New-Object System.Text.UTF8Encoding($false)))
}} finally {{
    if ($null -ne $broker -and -not $broker.HasExited) {{ Stop-Process -Id $broker.Id -Force }}
}}
''',
        encoding="utf-8",
    )
    return script


def _run_existing_worker_resume(
    arm: ArmPlan, resume_root: Path, workers: Sequence[tuple[Invocation, str]]
) -> dict[str, Any]:
    """Continue only interrupted Luna sessions, preserving their original streams."""

    script = _resume_workers_script(arm, resume_root, workers)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=arm.parent.target,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "exit_code": None,
            "mode": "retained_worker_resume",
            "timed_out": True,
            "wall_seconds": round(time.monotonic() - started, 3),
        }
    except OSError as exc:
        return {
            "exit_code": None,
            "mode": "retained_worker_resume",
            "orchestration_error": f"{type(exc).__name__}: {exc}",
            "wall_seconds": round(time.monotonic() - started, 3),
        }
    if completed.stdout:
        (resume_root / "dispatcher.stdout.log").write_text(completed.stdout, encoding="utf-8")
    if completed.stderr:
        (resume_root / "dispatcher.stderr.log").write_text(completed.stderr, encoding="utf-8")
    return {
        "exit_code": completed.returncode,
        "mode": "retained_worker_resume",
        "resumed_workers": [worker.role for worker, _thread_id in workers],
        "dispatch_completed": completed.returncode == 0,
        "wall_seconds": round(time.monotonic() - started, 3),
    }


def _summarizer_totals(paths: Sequence[Path]) -> dict[str, int]:
    totals = {field: 0 for field in (*PRIMARY_FIELDS, *CACHE_FIELDS, "input_new_tokens")}
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, Mapping) or event.get("event") != "summarizer_codex_usage":
                continue
            for field in totals:
                if field == "input_new_tokens":
                    continue
                value = event.get(field, 0)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    totals[field] += value
    totals["input_new_tokens"] = totals["input_tokens"] - totals["cached_input_tokens"]
    return totals


def _contextmesh_evidence(broker_log: Path, entries: Sequence[Any]) -> bool:
    """Require the exact owner-claim / publish / peer-serve flow for every overlap."""

    if not entries or not broker_log.is_file():
        return False
    events: list[Mapping[str, Any]] = []
    for line in broker_log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, Mapping):
            events.append(event)
    published_ids = {event.get("brief_id") for event in events if event.get("event") == "brief_published"}
    if any(
        event.get("event") == "brief_incomplete"
        or (event.get("event") == "brief_missing" and event.get("brief_id") not in published_ids)
        for event in events
    ):
        return False
    if _peer_brief_size_violations(events):
        return False
    for entry in entries:
        claims = [
            event
            for event in events
            if event.get("event") == "source_claim_raw"
            and event.get("brief_id") == entry.brief_id
            and event.get("owner_id") == entry.source_owner
        ]
        published = [
            event
            for event in events
            if event.get("event") == "brief_published"
            and event.get("brief_id") == entry.brief_id
            and event.get("owner_id") == entry.source_owner
        ]
        if len(claims) != 1 or len(published) != 1:
            return False
        for peer in entry.peer_workers:
            served = [
                event
                for event in events
                if event.get("event") == "brief_served"
                and event.get("brief_id") == entry.brief_id
                and event.get("peer_id") == peer
            ]
            # A retained Codex session can retry a peer fetch after a separate
            # harness failure. Repeated summary delivery is counted in its
            # stream, but it is never a second raw reader or product failure.
            if not served:
                return False
    return True


def _peer_brief_size_violations(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Detect a payload over the fact-derived cap retained at publication."""

    violations: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != "brief_published":
            continue
        raw_size, brief_size, peer_cap = event.get("raw_size"), event.get("brief_size"), event.get("max_peer_payload_bytes")
        if (
            not isinstance(raw_size, int)
            or isinstance(raw_size, bool)
            or raw_size < 1
            or not isinstance(brief_size, int)
            or isinstance(brief_size, bool)
            or brief_size < 0
            or not isinstance(peer_cap, int)
            or isinstance(peer_cap, bool)
            or peer_cap < 1
            or brief_size > peer_cap
        ):
            violations.append(
                {
                    "brief_id": event.get("brief_id"),
                    "raw_size": raw_size,
                    "brief_size": brief_size,
                    "maximum_peer_bytes": peer_cap if isinstance(peer_cap, int) and peer_cap > 0 else None,
                }
            )
    return violations


def _plan_scoped_large_source_evidence(events: Sequence[Mapping[str, Any]], entries: Sequence[Any]) -> bool:
    """Require the known generated catalog to use the compact owner source view."""

    catalog = next((entry for entry in entries if entry.canonical_path == "ruleforge/policy_catalog.py"), None)
    if catalog is None:
        return False
    claims = [
        event
        for event in events
        if event.get("event") == "source_claim_raw"
        and event.get("brief_id") == catalog.brief_id
        and event.get("owner_id") == catalog.source_owner
    ]
    if len(claims) != 1:
        return False
    claim = claims[0]
    raw_size, owner_view_size = claim.get("raw_size"), claim.get("owner_view_size")
    return (
        isinstance(raw_size, int)
        and not isinstance(raw_size, bool)
        and raw_size > MAX_PLAN_SCOPED_SOURCE_VIEW_BYTES
        and isinstance(owner_view_size, int)
        and not isinstance(owner_view_size, bool)
        and 0 < owner_view_size <= MAX_PLAN_SCOPED_SOURCE_VIEW_BYTES
        and claim.get("owner_view_kind") == "plan_scoped_excerpt"
        and claim.get("source_chunk_count") == 1
    )


def _brief_contract_failures(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Classify a broker deadline or cap that prevents a required brief."""

    repairs: dict[object, list[str]] = {}
    recovered = {event.get("brief_id") for event in events if event.get("event") == "brief_published"}
    for event in events:
        if event.get("event") != "brief_repair_requested":
            continue
        reason = event.get("reason")
        if isinstance(reason, str):
            repairs.setdefault(event.get("brief_id"), []).append(reason)
    failures: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != "brief_missing" or event.get("reason") != "timeout":
            continue
        brief_id = event.get("brief_id")
        if brief_id in recovered:
            # A retained-session continuation later published this exact bound
            # brief. The timeout is harness evidence, not an unrecovered product
            # protocol failure that may block a later arm.
            continue
        reasons = repairs.get(brief_id, [])
        if any(reason.startswith("brief_incomplete: summary exceeds") for reason in reasons):
            reason = "summary budget prevented required brief publication"
        else:
            reason = "owner brief was not published before the broker deadline"
        failures.append({"brief_id": brief_id, "reason": reason})
    return failures


def _packet_insufficient_count(paths: Sequence[Path]) -> int:
    count = 0
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, Mapping) and event.get("event") == "packet_insufficient":
                count += 1
    return count


def _stream_commands(path: Path) -> tuple[str, ...]:
    commands: list[str] = []
    if not path.is_file():
        return ()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
            item = event.get("item", {})
            command = item.get("command") if event.get("type") == "item.completed" else None
        except json.JSONDecodeError:
            continue
        if isinstance(command, str):
            commands.append(re.sub(r"/{2,}", "/", command.replace("\\", "/")))
    return tuple(commands)


def _source_candidates(target: Path) -> list[str]:
    """List text and sourceless variants of every source candidate in a worker view."""

    candidates: set[str] = set()
    for path in target.rglob("*.py"):
        if "__pycache__" not in path.parts:
            candidates.add(path.relative_to(target).as_posix())
    for path in target.rglob("*.pyc"):
        if "__pycache__" not in path.parts:
            relative = path.relative_to(target).as_posix()
            candidates.add(relative)
            candidates.add(relative[:-1])
    return sorted(candidates)


def _source_view_valid(target: Path, raw_sources: Sequence[str], owned_paths: Sequence[str]) -> tuple[bool, dict[str, Any]]:
    """Ensure only permitted source bodies were exposed in the worker worktree."""

    expected_raw = {path.replace("\\", "/") for path in raw_sources}
    allowed_text = expected_raw | {path.replace("\\", "/") for path in owned_paths}
    actual_text = {
        path.relative_to(target).as_posix()
        for path in target.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    try:
        recorded = json.loads((target / ".harness-source-view.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        recorded = {}
    recorded_raw = recorded.get("raw_source_paths") if isinstance(recorded, Mapping) else None
    valid = (
        isinstance(recorded_raw, list)
        and set(recorded_raw) == expected_raw
        and expected_raw <= actual_text
        and actual_text <= allowed_text
    )
    return valid, {
        "expected_raw_source_paths": sorted(expected_raw),
        "actual_text_source_paths": sorted(actual_text),
        "unexpected_text_source_paths": sorted(actual_text - allowed_text),
        "missing_raw_source_paths": sorted(expected_raw - actual_text),
    }


def _terra_evidence(arm: ArmPlan, plans: Sequence[WorkerPlan]) -> dict[str, Any]:
    """Verify retained Terra evidence for raw reconstruction or full RRC HIT/render."""

    source = arm.terra_source
    commands = _stream_commands(source.stream)
    final = source.final.read_text(encoding="utf-8", errors="replace") if source.final.is_file() else ""
    paths = _terra_preflight_paths(plans)
    try:
        terra_plans = load_terra_plans(arm.terra_plan_root, plans)
    except TerraPlanError as error:
        return {"valid": False, "mode": "coordinator_plan", "reason": str(error)}
    if arm.arm == "full":
        try:
            hit = json.loads(arm.rrc_hit.read_text(encoding="utf-8"))
            key = hit["cache_key"]
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError) as error:
            return {"valid": False, "mode": "rrc_hit_render", "reason": f"missing RRC hit: {error}"}
        packet_text = json.dumps(
            [
                {"task_id": plan.task_id, "steps": plan.plan_steps, "facts": plan.source_facts}
                for plan in terra_plans
            ],
            sort_keys=True,
        )
        raw_reads = [path for path in paths if any(path in command for command in commands)]
        hit_task_ids = hit.get("task_ids")
        matches = hit.get("lookup", {}).get("matches") if isinstance(hit.get("lookup"), Mapping) else None
        matched_task_ids = [item.get("task_id") for item in matches if isinstance(item, Mapping)] if isinstance(matches, list) else []
        task_ids_are_strings = isinstance(hit_task_ids, list) and all(isinstance(task_id, str) for task_id in hit_task_ids)
        matched_ids_are_strings = all(isinstance(task_id, str) for task_id in matched_task_ids)
        valid = (
            hit.get("event") == "rrc_hit"
            and isinstance(key, str)
            and task_ids_are_strings
            and set(hit_task_ids) == {plan.task_id for plan in plans}
            and len(hit_task_ids) == len(plans)
            and hit.get("schema_version") == 2
            and isinstance(hit.get("template_external_ref"), str)
            and isinstance(hit.get("cache_warm"), Mapping)
            and hit["cache_warm"].get("event") == "rrc_cache_warm"
            and hit["cache_warm"].get("backend") == "local_everos+rrc_runtime"
            and isinstance(hit.get("lookup"), Mapping)
            and hit["lookup"].get("backend") == "local_everos+rrc_runtime"
            and isinstance(matches, list)
            and len(matches) == len(plans)
            and matched_ids_are_strings
            and set(matched_task_ids) == {plan.task_id for plan in plans}
            and len(terra_plans) == 4
            and "{" + "domain" + "}" not in packet_text
            and any("harness.rrc_hit" in command for command in commands)
            and any("harness.terra_plans render-rrc" in command for command in commands)
            and f"TERRA_RRC_HIT_RENDER {key}" in final
            and not raw_reads
        )
        return {
            "valid": valid,
            "mode": "rrc_hit_render",
            "plan_source_arm": arm.terra_source_arm,
            "cache_key": key,
            "packet_count": len(terra_plans),
            "parent_raw_source_reads": raw_reads,
        }
    fingerprint = hashlib.sha256("\n".join(paths).encode("utf-8")).hexdigest()
    read_like = ("get-content", "cat", "type", "read_text", "read_bytes", "open(")
    observed = [
        path
        for path in paths
        if any(path in command and any(token in command.casefold() for token in read_like) for command in commands)
    ]
    valid = len(observed) == len(paths) and len(terra_plans) == 4 and f"TERRA_RAW_PREFLIGHT {fingerprint}" in final
    return {
        "valid": valid,
        "mode": "raw_preflight_reconstruction",
        "plan_source_arm": arm.terra_source_arm,
        "required_paths": list(paths),
        "observed_paths": observed,
        "coordinator_plan_count": len(terra_plans),
        "marker": fingerprint,
    }


def _quality_gate(arm: ArmPlan) -> dict[str, Any]:
    test_path = arm.root / "tests.json"
    test_results: dict[str, dict[str, Any]] = {}
    for worker, worker_plan in zip(arm.workers, freeze_worker_plans(arm.cohort), strict=True):
        command = worker_plan.acceptance_cmd.split()
        if command and command[0] == "python":
            command[0] = _worker_python()
        try:
            completed = subprocess.run(
                command,
                cwd=worker.target,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=180,
                check=False,
            )
            test_results[worker.role] = {
                "command": worker_plan.acceptance_cmd,
                "exit_code": completed.returncode,
                "output_tail": completed.stdout[-4000:],
            }
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout if isinstance(exc.stdout, str) else ""
            test_results[worker.role] = {
                "command": worker_plan.acceptance_cmd,
                "exit_code": None,
                "timed_out": True,
                "output_tail": output[-4000:],
            }
        except OSError as exc:
            test_results[worker.role] = {
                "command": worker_plan.acceptance_cmd,
                "exit_code": None,
                "orchestration_error": f"{type(exc).__name__}: {exc}",
                "output_tail": "",
            }
    _write_json(test_path, {"workers": test_results})
    child_records: list[Mapping[str, Any]] = []
    try:
        raw_records = json.loads(arm.child_records.read_text(encoding="utf-8"))
        if isinstance(raw_records, list):
            child_records = [item for item in raw_records if isinstance(item, Mapping)]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    handoffs = [arm.terra_source.final, *(worker.final for worker in arm.workers)]
    overlap_policy_violations = []
    scope_policy_violations = []
    source_views: dict[str, dict[str, Any]] = {}
    try:
        contracts = freeze_worker_plans(arm.cohort)
        observed_terra_plans = load_terra_plans(arm.terra_plan_root, contracts)
        entries = build_overlap_ledger(
            contracts,
            manifest_sha256(contracts),
            observed_terra_plans,
        )
    except TerraPlanError:
        entries = ()
        observed_terra_plans = ()
    expected_delivery_plan = plan_set_sha256(observed_terra_plans) if entries else None
    try:
        dispatch_ready = json.loads(arm.dispatch_ready.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        dispatch_ready = {}
    overlap_paths = {entry.canonical_path for entry in entries}
    for worker, contract in zip(arm.workers, contracts, strict=True):
        raw_sources = contract.initial_read_paths if arm.arm == "raw" else tuple(
            path for path in contract.initial_read_paths if path not in overlap_paths
        )
        source_view_ok, source_view = _source_view_valid(worker.target, raw_sources, contract.owned_write_paths)
        source_views[worker.role] = {"valid": source_view_ok, **source_view}
        if not worker.stream.is_file():
            continue
        candidate_paths = _source_candidates(worker.target)
        if arm.arm == "raw":
            allowed_paths = (*contract.initial_read_paths, *contract.owned_write_paths)
        else:
            allowed_paths = (
                *(path for path in contract.initial_read_paths if path not in overlap_paths),
                *contract.owned_write_paths,
            )
            overlap_policy_violations.extend(direct_overlap_reads(worker.stream, worker.role, entries))
        scope_policy_violations.extend(
            direct_unlisted_source_reads(worker.stream, worker.role, allowed_paths, candidate_paths)
        )
    policy_violations = [*overlap_policy_violations, *scope_policy_violations]
    broker_events: list[Mapping[str, Any]] = []
    if arm.broker_log.is_file():
        for line in arm.broker_log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, Mapping):
                broker_events.append(event)
    peer_brief_size_violations = _peer_brief_size_violations(broker_events)
    brief_contract_failures = _brief_contract_failures(broker_events)
    plan_scoped_large_source = _plan_scoped_large_source_evidence(broker_events, entries) if arm.arm != "raw" else True
    terra = _terra_evidence(arm, freeze_worker_plans(arm.cohort))
    stream_integrity = {
        invocation.role: _stream_integrity(invocation.stream)
        for invocation in (arm.terra_source, *arm.workers)
    }
    if arm.arm == "raw":
        mcp_health = {"valid": True, "mode": "not_configured"}
        mcp_model_probe = {"valid": True, "mode": "not_configured"}
    else:
        try:
            mcp_health = json.loads(arm.mcp_health.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            mcp_health = {"valid": False, "reason": "missing MCP health evidence"}
        try:
            mcp_model_probe = json.loads((arm.mcp_model_probe / "result.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            mcp_model_probe = {"valid": False, "reason": "missing MCP model-surface evidence"}
    gates = {
        "five_handoffs": all(path.is_file() and bool(path.read_text(encoding="utf-8", errors="replace").strip()) for path in handoffs),
        "four_child_records": len(child_records) == 4
        and {record.get("role") for record in child_records} == {worker.role for worker in arm.workers}
        and all(
            record.get("stream_completed") is True
            and (
                (isinstance(record.get("pid"), int) and record["pid"] > 0)
                or (record.get("retained_complete") is True and isinstance(record.get("thread_id"), str))
            )
            for record in child_records
        ),
        "tests_passed": all(result["exit_code"] == 0 for result in test_results.values()),
        "contextmesh_evidence": arm.arm == "raw" or _contextmesh_evidence(arm.broker_log, entries),
        "peer_brief_size": arm.arm == "raw" or not peer_brief_size_violations,
        "brief_contract": arm.arm == "raw" or not brief_contract_failures,
        "plan_scoped_large_source": plan_scoped_large_source,
        "no_direct_overlap_reads": not overlap_policy_violations,
        "declared_source_scope": not scope_policy_violations,
        "source_view": all(item["valid"] for item in source_views.values()),
        "terra_evidence": bool(terra["valid"]),
        "delivery_plan": dispatch_ready.get("delivery_plan_sha256") == expected_delivery_plan,
        "paired_raw_plan": arm.arm != "contextmesh" or arm.terra_source_arm == "raw",
        "stream_integrity": all(ok for ok, _reason in stream_integrity.values()),
        "mcp_health": mcp_health.get("valid") is True,
        "mcp_model_surface": mcp_model_probe.get("valid") is True,
    }
    return {
        "passed": all(gates.values()),
        "gates": gates,
        "child_records": child_records,
        "test_results": test_results,
        "terra_evidence": terra,
        "stream_integrity": {
            role: {"valid": ok, "reason": reason} for role, (ok, reason) in stream_integrity.items()
        },
        "mcp_health": mcp_health,
        "mcp_model_probe": mcp_model_probe,
        "source_views": source_views,
        "peer_brief_size_violations": peer_brief_size_violations,
        "brief_contract_failures": brief_contract_failures,
        "plan_scoped_large_source": plan_scoped_large_source,
        "source_policy_violations": [
            {"worker_id": item.worker_id, "canonical_path": item.canonical_path, "command": item.command}
            for item in policy_violations
        ],
    }


def _empty_usage(reason: str) -> dict[str, Any]:
    totals = {field: 0 for field in (*PRIMARY_FIELDS, *CACHE_FIELDS)}
    totals.update({"input_new_tokens": 0, "turns": 0})
    return {
        "schema_version": 1,
        "provider": "codex",
        "label": "Codex-pilot",
        "topology": "1+4",
        "orchestrators": 1,
        "workers": 4,
        "valid": False,
        "invalid_roles": ["collector"],
        "collector_error": reason,
        "totals": totals,
        "orchestrator": {},
        "worker_streams": [],
    }


def _product_code_failure(arm: ArmPlan, extra_messages: Sequence[str] = ()) -> bool:
    """Only host RRCv2/ContextMesh failures are allowed to stop future arms."""

    product_roots = tuple(
        str((arm.repo_root / name).resolve()).casefold().replace("/", "\\")
        for name in ("contextmesh", "rrc")
    )
    fragments = list(extra_messages)
    for path in (arm.terra_source.stderr, *(worker.stderr for worker in arm.workers)):
        if path.is_file():
            fragments.append(path.read_text(encoding="utf-8", errors="replace"))
    for fragment in fragments:
        text = fragment.casefold().replace("/", "\\")
        if ("traceback" in text or "exception" in text) and (
            any(root in text for root in product_roots)
        ):
            return True
    return False


def _configuration_is_product_failure(message: str) -> bool:
    text = message.casefold()
    return "contextmesh/mcp/server.py" in text or "contextmesh\\mcp\\server.py" in text or "rrc/" in text


def _finalize_arm(
    arm: ArmPlan, parent_run: Mapping[str, Any], warm_error: str | None = None
) -> dict[str, Any]:
    """Collect and retain one arm after its coordinator or dispatcher completed."""
    collection_error: str | None = None
    try:
        usage = collect_codex_pilot(
            arm.terra_source.stream, [worker.stream for worker in arm.workers], arm.root / "usage"
        )
    except (CollectionError, OSError, ValueError) as exc:
        collection_error = f"{type(exc).__name__}: {exc}"
        usage = _empty_usage(collection_error)
    cm_paths = [arm.terra_source.cm_log, *(worker.cm_log for worker in arm.workers)]
    stream_paths = [arm.terra_source.stream, *(worker.stream for worker in arm.workers)]
    nested = _summarizer_totals(cm_paths)
    packet_insufficient = _packet_insufficient_count([*cm_paths, *stream_paths])
    totals = dict(usage["totals"])
    for field, value in nested.items():
        totals[field] = int(totals.get(field, 0)) + value
    gross = sum(int(totals[field]) for field in PRIMARY_FIELDS)
    marginal = sum(int(totals.get(field, 0)) for field in MARGINAL_FIELDS)
    cache = sum(int(totals[field]) for field in CACHE_FIELDS)
    orchestrator_totals = usage.get("orchestrator", {}).get("totals", {})
    worker_streams = usage.get("worker_streams", ())
    orchestrator_marginal = sum(int(orchestrator_totals.get(field, 0)) for field in MARGINAL_FIELDS)
    worker_marginal = sum(
        sum(int(stream.get("totals", {}).get(field, 0)) for field in MARGINAL_FIELDS)
        for stream in worker_streams
        if isinstance(stream, Mapping)
    )
    quality = _quality_gate(arm)
    _write_json(arm.terra_evidence, quality["terra_evidence"])
    product_error = warm_error is not None or _product_code_failure(
        arm,
        [message for message in (collection_error, warm_error) if message is not None],
    ) or bool(quality["peer_brief_size_violations"] or quality["brief_contract_failures"])
    valid = bool(usage["valid"] and parent_run["exit_code"] == 0 and quality["passed"])
    result = {
        "arm": arm.arm,
        "terra_plan_source_arm": arm.terra_source_arm,
        "orchestrator_usage_source_arm": arm.terra_source_arm,
        "parent_run": parent_run,
        "valid_streams": usage["valid"],
        "quality": quality,
        "valid": valid,
        "status": "blocking_product_error" if product_error else "complete" if valid else "degraded",
        "blocking_product_error": product_error,
        "diagnostics": [message for message in (collection_error, warm_error) if message is not None],
        "usage": {"invocations": usage, "nested_contextmesh_summarizers": nested, "totals": totals},
        "packet_insufficient": packet_insufficient,
        "primary_compute": marginal,
        "marginal_compute": marginal,
        "gross_compute": gross,
        "orchestrator_marginal_compute": orchestrator_marginal,
        "worker_marginal_compute": worker_marginal,
        "cache_tokens": cache,
    }
    _write_json(arm.root / "result.json", result)
    return result


def execute_arm(arm: ArmPlan) -> dict[str, Any]:
    """Execute one prepared arm and retain diagnostics without automatic retries."""

    _write_json(
        arm.root / "attempt.json",
        {
            "schema_version": 1,
            "arm": arm.arm,
            "status": "started",
            "stream_paths": [str(arm.terra_source.stream), *(str(worker.stream) for worker in arm.workers)],
        },
    )
    warm_error: str | None = None
    if arm.arm == "full":
        try:
            warm_rrc_cache(arm.parent.target, arm.rrc_warm)
        except (OSError, RRCManifestError, ValueError) as exc:
            warm_error = f"RRC cache warm failed: {type(exc).__name__}: {exc}"
    if arm.arm == "contextmesh":
        parent_run = _run_paired_dispatch(arm)
    elif warm_error is not None:
        parent_run = {"exit_code": 2, "mode": "rrc_cache_warm", "error": warm_error}
    else:
        parent_run = _run_parent(arm)
    return _finalize_arm(arm, parent_run, warm_error)


def _has_primary_metric(result: Mapping[str, Any]) -> bool:
    value = result.get("primary_compute")
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _has_component_metrics(result: Mapping[str, Any]) -> bool:
    return all(
        isinstance(result.get(field), int) and not isinstance(result[field], bool) and result[field] >= 0
        for field in ("orchestrator_marginal_compute", "worker_marginal_compute")
    )


def _reduction(baseline: int, candidate: int) -> float | None:
    if baseline <= 0:
        return None
    return round((baseline - candidate) / baseline, 6)


def compare_results(plan: RoundPlan, results: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Report retained evidence, distinguishing strict proof from degraded direction."""

    missing = [arm for arm in ARMS if arm not in results]
    identities: list[Mapping[str, Any]] = []
    for arm in plan.arms:
        try:
            identities.append(json.loads((arm.root / "comparison.json").read_text(encoding="utf-8")))
        except (FileNotFoundError, json.JSONDecodeError):
            identities.append({})
    identity_matches = all(
        identity.get("comparability_fingerprint") == plan.comparability_fingerprint
        and identity.get("workload_manifest_sha256") == identities[0].get("workload_manifest_sha256")
        and identity.get("worker_contract_sha256") == _worker_contract_sha256(plan.worker_plans)
        for identity in identities
    )
    metrics_available = not missing and all(
        _has_primary_metric(result) and _has_component_metrics(result) for result in results.values()
    )
    blocking_arms = [
        name for name, result in results.items() if bool(result.get("blocking_product_error", False))
    ]
    degraded_arms = [name for name, result in results.items() if not bool(result.get("valid", False))]
    comparable = bool(metrics_available and identity_matches and not blocking_arms)
    raw_primary = int(results.get("raw", {}).get("primary_compute", 0))
    contextmesh_primary = int(results.get("contextmesh", {}).get("primary_compute", 0))
    full_primary = int(results.get("full", {}).get("primary_compute", 0))
    raw_worker = int(results.get("raw", {}).get("worker_marginal_compute", 0))
    contextmesh_worker = int(results.get("contextmesh", {}).get("worker_marginal_compute", 0))
    raw_orchestrator = int(results.get("raw", {}).get("orchestrator_marginal_compute", 0))
    full_orchestrator = int(results.get("full", {}).get("orchestrator_marginal_compute", 0))
    contextmesh_worker_reduction = _reduction(raw_worker, contextmesh_worker)
    rrc_orchestrator_reduction = _reduction(raw_orchestrator, full_orchestrator)
    full_reduction = _reduction(raw_primary, full_primary)
    contextmesh_wins = bool(
        comparable
        and contextmesh_worker_reduction is not None
        and contextmesh_worker_reduction >= MIN_COMPONENT_REDUCTION
    )
    rrc_wins = bool(
        comparable
        and rrc_orchestrator_reduction is not None
        and rrc_orchestrator_reduction >= MIN_COMPONENT_REDUCTION
    )
    full_beats_contextmesh = bool(
        comparable
        and full_primary < contextmesh_primary
        and full_reduction is not None
        and full_reduction >= MIN_FULL_REDUCTION
    )
    full_wins = comparable and full_primary < raw_primary
    tiered_win = contextmesh_wins and rrc_wins and full_beats_contextmesh and full_wins
    strict_proof = tiered_win and not degraded_arms
    if blocking_arms:
        status = "blocking_product_error"
    elif not comparable:
        status = "evidence_incomplete"
    elif strict_proof:
        status = "proved"
    elif tiered_win:
        status = "directional_tiered_win"
    elif full_wins and (contextmesh_primary < raw_primary or full_beats_contextmesh):
        status = "partial_hierarchy"
    else:
        status = "optimization_required"
    report = {
        "round": plan.round_id,
        "comparable": comparable,
        "status": status,
        "strict_requirement": (
            "ContextMesh reduces Luna marginal compute by at least 15%; RRC reduces Terra marginal "
            "compute by at least 15%; and full marginal compute is at least 15% below raw and below ContextMesh"
        ),
        "strict_proof": strict_proof,
        "claude_eligible": strict_proof,
        "rerun_policy": "retain partial evidence and resume unfinished arms; never auto-rerun for test, scaffolding, or orchestration failures",
        "primary_compute_definition": "input_new_tokens + output_tokens + reasoning_output_tokens",
        "gross_compute_definition": "input_tokens + output_tokens + reasoning_output_tokens",
        "minimum_component_reduction": MIN_COMPONENT_REDUCTION,
        "minimum_full_reduction": MIN_FULL_REDUCTION,
        "arms": {arm: dict(results[arm]) for arm in results},
        "full_beats_raw": full_wins,
        "contextmesh_beats_raw": contextmesh_wins,
        "rrc_beats_raw_terra": rrc_wins,
        "full_beats_contextmesh": full_beats_contextmesh,
        "tiered_full_win": tiered_win,
        "quality_degraded_arms": degraded_arms,
        "blocking_product_error_arms": blocking_arms,
        "raw_primary_compute": raw_primary,
        "contextmesh_primary_compute": contextmesh_primary,
        "full_primary_compute": full_primary,
        "raw_worker_marginal_compute": raw_worker,
        "contextmesh_worker_marginal_compute": contextmesh_worker,
        "raw_orchestrator_marginal_compute": raw_orchestrator,
        "contextmesh_orchestrator_marginal_compute": raw_orchestrator,
        "full_orchestrator_marginal_compute": full_orchestrator,
        "contextmesh_worker_reduction": contextmesh_worker_reduction,
        "rrc_orchestrator_reduction": rrc_orchestrator_reduction,
        "full_marginal_reduction": full_reduction,
    }
    _write_json(plan.root / "report.json", report)
    return report


def _retained_result(arm: ArmPlan) -> Mapping[str, Any] | None:
    path = arm.root / "result.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        return _incomplete_arm_result(arm, f"retained result cannot be read: {type(exc).__name__}: {exc}")
    if isinstance(value, Mapping):
        return value
    return _incomplete_arm_result(arm, "retained result is not a JSON object")


def _mcp_probe_result(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads((path / "result.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, Mapping) else None


def _resumable_dispatch(arm: ArmPlan) -> bool:
    """True only when Terra finished but its four Lunas never started."""

    retained = _retained_result(arm)
    if retained is None or bool(retained.get("valid")) or bool(retained.get("blocking_product_error")):
        return False
    if not arm.terra_source.stream.is_file() or not any(arm.terra_plan_root.glob("*.json")):
        return False
    return not any(path.exists() for worker in arm.workers for path in (worker.stream, worker.final))


def _stream_thread_id(path: Path) -> str | None:
    """Read the durable Codex session ID without accepting a malformed stream."""

    try:
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
    return None


def _worker_needs_resume(worker: Invocation) -> bool:
    """Identify a stopped turn or an explicit no-fallback ContextMesh handoff."""

    stream_complete = _stream_integrity(worker.stream)[0]
    final = worker.final.read_text(encoding="utf-8", errors="replace") if worker.final.is_file() else ""
    return not stream_complete or "CONTEXTMESH_BRIEF_UNAVAILABLE" in final


def _resumable_worker_sessions(arm: ArmPlan) -> tuple[tuple[Invocation, str], ...]:
    """Return only retained ContextMesh Luna sessions that need continuation."""

    retained = _retained_result(arm)
    if (
        arm.arm == "raw"
        or retained is None
        or bool(retained.get("valid"))
        or bool(retained.get("blocking_product_error"))
        or not arm.terra_source.stream.is_file()
        or not any(arm.terra_plan_root.glob("*.json"))
        or not arm.broker_ledger.is_file()
    ):
        return ()
    sessions: list[tuple[Invocation, str]] = []
    for worker in arm.workers:
        if not _worker_needs_resume(worker):
            continue
        thread_id = _stream_thread_id(worker.stream)
        if thread_id is None:
            return ()
        sessions.append((worker, thread_id))
    return tuple(sessions)


def _next_resume_root(arm: ArmPlan) -> Path:
    root = arm.root / "resumptions"
    number = 1
    while (root / f"dispatch-{number}").exists():
        number += 1
    destination = root / f"dispatch-{number}"
    destination.mkdir(parents=True)
    return destination


def resume_retained_dispatch(arm: ArmPlan) -> dict[str, Any]:
    """Resume an unlaunched worker batch from retained Terra evidence only."""

    if not _resumable_dispatch(arm):
        raise ComparisonError(f"arm is not eligible for dispatcher-only resume: {arm.arm}")
    resume_root = _next_resume_root(arm)
    for path in (arm.root / "attempt.json", arm.root / "result.json", arm.dispatch_error):
        if path.is_file():
            shutil.copy2(path, resume_root / f"previous-{path.name}")
    if arm.dispatch_error.is_file():
        arm.dispatch_error.unlink()
    _write_json(
        resume_root / "resume.json",
        {
            "schema_version": 1,
            "arm": arm.arm,
            "mode": "retained_dispatch_resume",
            "terra_stream": str(arm.terra_source.stream),
            "terra_plan_root": str(arm.terra_plan_root),
            "reason": "previous attempt launched zero workers; reuse retained Terra plan without replaying Terra",
        },
    )
    _refresh_pending_dispatch(arm)
    result = _finalize_arm(arm, _run_existing_dispatch(arm, resume_root))
    result["resumed_dispatch"] = True
    result["resume_artifact"] = str(resume_root)
    _write_json(arm.root / "result.json", result)
    return result


def resume_retained_workers(arm: ArmPlan) -> dict[str, Any]:
    """Continue only interrupted Luna sessions; never replay Terra or a completed Luna."""

    workers = _resumable_worker_sessions(arm)
    if not workers:
        raise ComparisonError(f"arm is not eligible for worker-session resume: {arm.arm}")
    resume_root = _next_resume_root(arm)
    for path in (arm.root / "attempt.json", arm.root / "result.json", arm.dispatch_error):
        if path.is_file():
            shutil.copy2(path, resume_root / f"previous-{path.name}")
    for worker in arm.workers:
        worker_root = resume_root / worker.role
        worker_root.mkdir(parents=True, exist_ok=True)
        for path in (worker.stream, worker.stderr, worker.final):
            if path.is_file():
                shutil.copy2(path, worker_root / f"previous-{path.name}")
    if arm.dispatch_error.is_file():
        arm.dispatch_error.unlink()
    _write_json(
        resume_root / "resume.json",
        {
            "schema_version": 1,
            "arm": arm.arm,
            "mode": "retained_worker_resume",
            "terra_stream": str(arm.terra_source.stream),
            "terra_plan_root": str(arm.terra_plan_root),
            "resumed_workers": [worker.role for worker, _thread_id in workers],
            "retained_workers": [worker.role for worker in arm.workers if worker.role not in {item[0].role for item in workers}],
            "reason": "reuse retained Codex sessions and persisted owner briefs after a harness interruption",
        },
    )
    result = _finalize_arm(arm, _run_existing_worker_resume(arm, resume_root, workers))
    result["resumed_workers"] = [worker.role for worker, _thread_id in workers]
    result["resume_artifact"] = str(resume_root)
    _write_json(arm.root / "result.json", result)
    return result


def _ensure_mcp_probe(plan: RoundPlan) -> Mapping[str, Any]:
    """Run exactly one retained, source-free Luna MCP eligibility probe per round."""

    output = next(arm.mcp_model_probe for arm in plan.arms if arm.arm != "raw")
    retained = _mcp_probe_result(output)
    if retained is not None:
        return retained
    if any((output / name).exists() for name in ("stream.jsonl", "stderr.log", "final.md")):
        return {"valid": False, "reason": "retained MCP eligibility attempt lacks a valid result; do not replay it"}
    try:
        result = run_mcp_model_probe(plan.workload_root / "workspace", output)
    except (OSError, subprocess.SubprocessError) as error:
        result = {"valid": False, "reason": f"MCP eligibility probe failed: {type(error).__name__}: {error}"}
    _write_json(output / "result.json", result)
    return result


def _started_attempt(arm: ArmPlan) -> bool:
    """A live or interrupted attempt is immutable; never launch into its streams again."""

    if (arm.root / "attempt.json").is_file() or arm.child_records.is_file():
        return True
    return any(
        path.exists()
        for path in (arm.parent.stream, arm.parent.stderr, *(worker.stream for worker in arm.workers), *(worker.stderr for worker in arm.workers))
    )


def _incomplete_arm_result(arm: ArmPlan, diagnostic: str) -> dict[str, Any]:
    return {
        "arm": arm.arm,
        "valid": False,
        "valid_streams": False,
        "status": "degraded",
        "blocking_product_error": False,
        "diagnostics": [diagnostic],
    }


def _refresh_pending_dispatch(arm: ArmPlan) -> None:
    """Apply launcher fixes only to an unfinished arm; retained work is immutable."""

    arm.dispatch.write_text(_dispatch_script(arm), encoding="utf-8")
    arm.parent.prompt.write_text(_terra_prompt(arm, freeze_worker_plans(arm.cohort)), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("round_id")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--metrics-root", default="metrics")
    parser.add_argument("--cohort", choices=("core", "growth"), default="core")
    parser.add_argument("--arms", nargs=3, default=ARMS)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume-dispatch-only", action="store_true")
    parser.add_argument("--resume-workers", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if tuple(sorted(args.arms)) != tuple(sorted(ARMS)):
        print("comparison error: --arms must be a permutation of raw, contextmesh, full", file=sys.stderr)
        return 2
    try:
        plan = build_round_plan(args.repo_root, args.round_id, args.metrics_root, args.cohort)
        if not args.execute:
            print(json.dumps({"status": "planned", "root": str(plan.root), "fingerprint": plan.comparability_fingerprint}, sort_keys=True))
            return 0
        preparation = prepare_or_resume_round(plan)
        by_arm = {arm.arm: arm for arm in plan.arms}
        if args.resume_dispatch_only or args.resume_workers:
            resume_results: dict[str, Mapping[str, Any]] = {}
            resume_blocked_by_product: str | None = None
            resumed_names: list[str] = []
            for name in ARMS:
                arm = by_arm[name]
                if resume_blocked_by_product is not None:
                    resume_results[name] = _incomplete_arm_result(
                        arm,
                        f"not resumed: confirmed product-code failure in {resume_blocked_by_product}",
                    )
                    continue
                if args.resume_dispatch_only and _resumable_dispatch(arm):
                    result = resume_retained_dispatch(arm)
                    resume_results[name] = result
                    resumed_names.append(name)
                elif args.resume_workers and _resumable_worker_sessions(arm):
                    result = resume_retained_workers(arm)
                    resume_results[name] = result
                    resumed_names.append(name)
                else:
                    retained = _retained_result(arm)
                    resume_results[name] = retained or _incomplete_arm_result(
                        arm, "no retained arm result available for the requested resume mode"
                    )
                if resume_results[name].get("blocking_product_error") is True:
                    resume_blocked_by_product = name
            report = compare_results(plan, resume_results)
            report["execution"] = {
                "resumed": True,
                "resumed_dispatch_only": resumed_names if args.resume_dispatch_only else [],
                "resumed_workers": resumed_names if args.resume_workers else [],
                "runner_source_changed_since_prepare": not bool(preparation["prepared"]),
            }
            _write_json(plan.root / "report.json", report)
            print(json.dumps(report, sort_keys=True))
            return 2 if report["blocking_product_error_arms"] else 0
        results: dict[str, Mapping[str, Any]] = {}
        retained_names = [name for name, arm in by_arm.items() if _retained_result(arm) is not None]
        pending_non_raw = any(
            arm.arm != "raw" and _retained_result(arm) is None and not _started_attempt(arm)
            for arm in plan.arms
        )
        probe = _ensure_mcp_probe(plan) if preparation["prepared"] and pending_non_raw else None
        blocked_by_product: str | None = None
        # The paired ContextMesh arm consumes Raw's retained Terra artifact, so
        # launch order is a measurement invariant rather than a caller choice.
        for name in ARMS:
            arm = by_arm[name]
            if blocked_by_product is not None:
                results[name] = _incomplete_arm_result(
                    arm,
                    f"not launched: confirmed product-code failure in {blocked_by_product}; retain and repair it first",
                )
                continue
            retained = _retained_result(arm)
            if retained is not None:
                results[name] = retained
                if retained.get("blocking_product_error") is True:
                    blocked_by_product = name
                continue
            if _started_attempt(arm):
                results[name] = _incomplete_arm_result(
                    arm,
                    "retained attempt has execution evidence but no result; do not relaunch into its streams",
                )
                continue
            if not preparation["prepared"]:
                results[name] = _incomplete_arm_result(arm, str(preparation["diagnostic"]))
                continue
            if arm.arm != "raw" and (probe is None or probe.get("valid") is not True):
                reason = "missing MCP eligibility result" if probe is None else str(probe.get("reason", "MCP eligibility probe failed"))
                results[name] = _incomplete_arm_result(arm, reason)
                continue
            frozen, diagnostic = _runner_source_is_frozen(plan)
            if not frozen:
                results[name] = _incomplete_arm_result(arm, str(diagnostic))
                continue
            _refresh_pending_dispatch(arm)
            result = execute_arm(arm)
            results[name] = result
            if result["blocking_product_error"]:
                blocked_by_product = name
        report = compare_results(plan, results)
        report["execution"] = {
            "resumed": bool(preparation["resumed"]),
            "retained_arms": retained_names,
        }
        _write_json(plan.root / "report.json", report)
        print(json.dumps(report, sort_keys=True))
        return 2 if report["blocking_product_error_arms"] else 0
    except ComparisonError as exc:
        if _configuration_is_product_failure(str(exc)):
            print(f"comparison product error: {exc}", file=sys.stderr)
            return 2
        print(f"comparison diagnostic: {exc}", file=sys.stderr)
        return 0
    except (FileExistsError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"comparison diagnostic: {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
