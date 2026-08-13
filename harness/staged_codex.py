"""Prepare, but never implicitly spend, the direct three-stage Codex cohort."""

from __future__ import annotations

import asyncio
import argparse
import hashlib
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contextmesh.bench.rrc_long_spec_demo import materialize
from contextmesh.mcp.broker_service import ledger_payload
from contextmesh.mcp.shared_broker import SharedBrokerClient
from harness.collect_codex import CollectionError, collect_codex_stream
from harness.deepseek_delegate import MODEL as DEEPSEEK_MODEL
from harness.deepseek_delegate import command as deepseek_command
from harness.four_worker_plan import OverlapLedgerEntry, TerraPlan, WorkerPlan, manifest_sha256
from harness.mcp_health import check as check_mcp_health, mcp_config
from harness.mcp_model_probe import run as run_mcp_model_probe
from harness.rrc_hit import RRCManifestError, warm_rrc_cache
from harness.rrc_stage import record_stage_plan_state
from harness.source_policy import (
    SourcePolicyViolation,
    direct_overlap_reads,
    direct_unlisted_source_reads,
    observed_local_read_set,
)
from harness.staged_workload import INDEPENDENT_COHORT_STARTS, StageWorkload, staged_overlap_ledgers, staged_workloads, write_stage_manifest
from harness.terra_plans import TerraPlanError, load_terra_plans, plan_set_sha256, write_task_request
from harness.worker_packets import render_worker_packet, worker_prompt_from_packet
from harness.workload_preflight import require_capacity, source_mass
from harness.workspace import materialize_worker_workspace, materialize_workspace

ARMS = ("raw", "contextmesh", "full")
TERRA_MODEL = "gpt-5.6-terra"
AUTO_COMPACT_TOKEN_LIMIT = 230_000
WORKER_MODEL = DEEPSEEK_MODEL
BROKER_WAIT_TIMEOUT_MS = 600_000
STAGE_TIMEOUT_SECONDS = 900
RUNNER_SOURCE_FILES = (
    "harness/staged_codex.py", "harness/deepseek_delegate.py", "harness/qwen_delegate.py", ".codex/delegates/deepseek.toml", ".codex/delegates/deepseek-model-catalog.json", "harness/staged_workload.py", "harness/terra_plans.py", "harness/worker_packets.py",
    "harness/rrc_hit.py", "harness/rrc_stage.py", "harness/collect_codex.py", "harness/mcp_health.py", "harness/mcp_model_probe.py", "harness/source_policy.py", "harness/workspace.py",
    "contextmesh/mcp/shared_broker.py", "contextmesh/mcp/broker_service.py", "contextmesh/mcp/bridge.py", "rrc/store.py",
    "rrc/everos.py", "rrc/orchestrator_contract.py", "rrc/orchestrator_policy.py", "rrc/orchestrator_runtime.py", "contextmesh/bench/rrc_long_spec_demo.py",
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
The task packet is self-contained; do not read optional guidance files.
Do not spawn subagents, teams, or nested sessions; you are one of exactly four
direct measured workers.
"""


@dataclass(frozen=True, slots=True)
class StagedInvocation:
    role: str
    model: str
    command: tuple[str, ...]
    target: Path
    prompt: Path
    stream: Path
    final: Path


def _command(model: str, final: Path, *, bridge: bool, repo: Path) -> tuple[str, ...]:
    if model == WORKER_MODEL:
        return _worker_command(final, bridge=bridge, repo=repo)
    command = [
        os.environ.get("STAGED_CODEX_BIN", "codex"), "exec", "--ignore-user-config", "--enable", "fast_mode", "--model", model,
        "--config", "model_reasoning_effort=high", "--config", f"model_auto_compact_token_limit={AUTO_COMPACT_TOKEN_LIMIT}", "--config", "service_tier=priority",
    ]
    if bridge:
        for item in mcp_config(repo):
            command.extend(("--config", item))
    command.extend(("--dangerously-bypass-approvals-and-sandbox", "--json", "--output-last-message", str(final), "-"))
    return tuple(command)


def _worker_command(final: Path, *, bridge: bool, repo: Path) -> tuple[str, ...]:
    """Fresh external Qwen/DeepSeek worker; each JSONL owns its session."""

    return deepseek_command(repo, final, extra_config=mcp_config(repo) if bridge else ())


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _controller_lease(root: Path) -> tuple[Path, str]:
    """Atomically reserve one round for one controller process.

    A resumed controller appends to durable JSONL.  Concurrent parents would
    therefore corrupt that evidence and can relaunch the same retained DeepSeek worker.
    The lease is intentionally filesystem-local and contains no credentials.
    """

    path = root / "controller-lease.json"
    token = secrets.token_hex(16)
    payload = {"pid": os.getpid(), "token": token}
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        retained = _read_json(path, {})
        retained_pid = retained.get("pid") if isinstance(retained, Mapping) else None
        if isinstance(retained_pid, int):
            try:
                os.kill(retained_pid, 0)
            except ProcessLookupError:
                path.unlink(missing_ok=True)
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except PermissionError as error:
                raise RuntimeError("another staged controller lease is active") from error
            except OSError:
                # Windows can report a terminated/recycled pid as WinError 87
                # rather than ProcessLookupError. A failed probe is not a live
                # controller lease, so recovery may safely replace this marker.
                path.unlink(missing_ok=True)
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                raise RuntimeError("another staged controller lease is active")
        else:
            raise RuntimeError("staged controller lease is malformed; retain it for diagnosis before recovery")
    try:
        os.write(descriptor, json.dumps(payload, sort_keys=True).encode("utf-8"))
    finally:
        os.close(descriptor)
    return path, token


def _release_controller_lease(path: Path, token: str) -> None:
    """Remove only the lease acquired by this controller."""

    retained = _read_json(path, {})
    if isinstance(retained, Mapping) and retained.get("token") == token:
        path.unlink(missing_ok=True)


def _runner_snapshot(repo: Path) -> dict[str, object]:
    files = {relative: hashlib.sha256((repo / relative).read_bytes()).hexdigest() for relative in RUNNER_SOURCE_FILES}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"schema_version": 1, "files": files, "sha256": digest}


def _invocation_payload(invocation: StagedInvocation) -> dict[str, object]:
    return {
        "role": invocation.role,
        "model": invocation.model,
        "command": list(invocation.command),
        "target": str(invocation.target),
        "prompt": str(invocation.prompt),
        "stream": str(invocation.stream),
        "final": str(invocation.final),
    }


def prepare_staged_round(
    repo_root: str | Path,
    round_id: str,
    metrics_root: str | Path = "metrics",
    *,
    from_stage: str | None = None,
) -> dict[str, object]:
    """Write the immutable staged launch plan without starting a model."""

    repo = Path(repo_root).resolve()
    metrics = (repo / metrics_root).resolve() if not Path(metrics_root).is_absolute() else Path(metrics_root).resolve()
    if not round_id or any(part in round_id for part in ("/", "\\", ".")):
        raise ValueError("round_id must be one safe path component")
    root = metrics / round_id / "staged-codex"
    if root.exists():
        raise FileExistsError(f"staged round already exists: {root}")
    bridge = repo / "contextmesh" / "mcp" / "bridge.py"
    if not bridge.is_file():
        raise ValueError("missing ContextMesh MCP bridge")

    stages, ledgers = _stage_cohort(from_stage)
    cohort_metadata = {
        "schema_version": 1,
        "start_stage": stages[0].stage_id,
        "standalone_append_only_cohort": from_stage is not None,
        "lineage_mode": "fresh_independent_baseline" if from_stage is not None else "linked_from_stage_01",
    }
    root.mkdir(parents=True)
    all_arms: dict[str, object] = {}
    for arm in ARMS:
        arm_root = root / arm
        baseline = arm_root / "baseline"
        source_root = arm_root / f"{stages[0].stage_id}-source"
        materialize(source_root, stages[0].cohort, stages[0].stage_id)
        source = source_root / "workspace"
        source.rename(baseline)
        broker = arm_root / "broker"
        control_token = secrets.token_urlsafe(32) if arm != "raw" else None
        if control_token is not None:
            broker.mkdir(parents=True)
            (broker / "control-token.txt").write_text(control_token + "\n", encoding="utf-8")
        stage_payloads: list[dict[str, object]] = []
        for stage, entries in zip(stages, ledgers, strict=True):
            stage_root = arm_root / "stages" / stage.stage_id
            manifest = write_stage_manifest(stage_root, stage)
            ledger = stage_root / "ledger.json"
            ledger.write_text(json.dumps(ledger_payload(entries), indent=2, sort_keys=True) + "\n", encoding="utf-8")
            terra_root = stage_root / "terra"
            request = write_task_request(terra_root / "request.json", stage.plans)
            parent = StagedInvocation(
                role="orchestrator",
                model=TERRA_MODEL,
                command=_command(TERRA_MODEL, terra_root / "final.md", bridge=False, repo=repo),
                target=baseline,
                prompt=terra_root / "prompt.md",
                stream=terra_root / "stream.jsonl",
                final=terra_root / "final.md",
            )
            workers = tuple(
                StagedInvocation(
                    role=plan.worker_id,
                    model=WORKER_MODEL,
                    command=_command(WORKER_MODEL, stage_root / "workers" / plan.worker_id / "final.md", bridge=arm != "raw", repo=repo),
                    target=stage_root / "worktrees" / plan.worker_id,
                    prompt=stage_root / "workers" / plan.worker_id / "prompt.md",
                    stream=stage_root / "workers" / plan.worker_id / "stream.jsonl",
                    final=stage_root / "workers" / plan.worker_id / "final.md",
                )
                for plan in stage.plans
            )
            stage_payloads.append(
                {
                    "stage_id": stage.stage_id,
                    "cohort": stage.cohort,
                    "manifest": str(manifest),
                    "ledger": str(ledger),
                    "terra_request": str(request),
                    "broker": None if arm == "raw" else {
                        "source_root": str(baseline), "state_dir": str(broker / "state"),
                        "port_file": str(broker / "port.json"), "control_token_file": str(broker / "control-token.txt"),
                    },
                    "orchestrator": _invocation_payload(parent),
                    "workers": [_invocation_payload(worker) for worker in workers],
                }
            )
        all_arms[arm] = {"baseline": str(baseline), "stages": stage_payloads}
    payload: dict[str, object] = {
        "schema_version": 1,
        "round_id": round_id,
        "topology": "linked staged workflow / 1 Terra + 4 external DeepSeek workers per stage",
        "cohort": cohort_metadata,
        "models": {
            "orchestrator": TERRA_MODEL,
            "worker": WORKER_MODEL,
            "worker_provider": "ollama",
            "worker_context_window": 1_048_576,
            "orchestrator_auto_compact_token_limit": AUTO_COMPACT_TOKEN_LIMIT,
            "worker_auto_compact_token_limit": AUTO_COMPACT_TOKEN_LIMIT,
            "reasoning": "high",
            "service_tier": "priority",
            "fast_mode": True,
            "bypass": True,
        },
        "arms": all_arms,
    }
    _write_json(root / "run-plan.json", payload)
    _write_json(root / "cohort.json", cohort_metadata)
    _write_json(root / "runner-source.json", _runner_snapshot(repo))
    return payload


def _run_git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(["git", *arguments], cwd=root, text=True, capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or f"git {' '.join(arguments)} failed")
    return completed.stdout.strip()


def _ensure_git_baseline(root: Path) -> str:
    if not (root / ".git").is_dir():
        _run_git(root, "init")
        _run_git(root, "config", "user.email", "staged-harness@local.invalid")
        _run_git(root, "config", "user.name", "staged-harness")
        _run_git(root, "add", "--all", "ruleforge", "tests")
        _run_git(root, "commit", "-m", "staged RuleForge baseline")
    return _run_git(root, "rev-parse", "HEAD")


def _stage_commit(root: Path, message: str, paths: Sequence[str]) -> str:
    for path in paths:
        _run_git(root, "add", "--", path)
    _run_git(root, "commit", "--allow-empty", "-m", message)
    return _run_git(root, "rev-parse", "HEAD")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _retain_boundary_evidence(
    baseline: Path,
    stage_root: Path,
    *,
    source_commit: str,
    boundary_commit: str,
    paths: Sequence[str],
) -> dict[str, object]:
    """Persist the exact accepted boundary consumed by the next linked stage."""

    normalized = tuple(sorted(set(paths)))
    path_hashes = {
        relative: _sha256_file(baseline / relative)
        for relative in normalized
        if (baseline / relative).is_file()
    }
    diff = _run_git(
        baseline,
        "diff",
        "--no-ext-diff",
        f"{source_commit}..{boundary_commit}",
        "--",
        *normalized,
    ) if normalized else ""
    diff_path = stage_root / "boundary.diff"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(diff + ("\n" if diff and not diff.endswith("\n") else ""), encoding="utf-8")
    evidence: dict[str, object] = {
        "source_commit": source_commit,
        "boundary_commit": boundary_commit,
        "merged_paths": list(normalized),
        "path_sha256": path_hashes,
        "diff_path": str(diff_path),
        "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
    }
    _write_json(stage_root / "boundary.json", evidence)
    return evidence


def _copy_stage_cache(repo: Path, target: Path, stage: StageWorkload) -> None:
    """Replace only source-free template bindings; retain staged RRC state."""

    cache_source_root = target.parent / ".stage-cache" / stage.stage_id
    if not cache_source_root.exists():
        materialize(cache_source_root, stage.cohort, stage.stage_id)
    source_cache = cache_source_root / "workspace" / ".rrc-cache"
    destination = target / ".rrc-cache"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("template.json", "bindings.json"):
        shutil.copy2(source_cache / name, destination / name)


def _source_dependencies(entries: Sequence[OverlapLedgerEntry], source_root: Path) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for entry in sorted(entries, key=lambda item: item.canonical_path):
        body = (source_root / entry.canonical_path).read_bytes()
        facts = json.dumps(entry.required_facts, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        values.append(
            {
                "lineage_id": f"main:{entry.canonical_path}",
                "content_sha256": hashlib.sha256(body).hexdigest(),
                "brief_facts_hash": hashlib.sha256(facts).hexdigest(),
            }
        )
    return values


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _stream_thread_id(path: Path) -> str | None:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else ():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
        if event.get("type") == "system" and isinstance(event.get("session_id"), str):
            return event["session_id"]
    return None


def _stream_completed(path: Path) -> bool:
    if not path.is_file():
        return False
    completed = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return False
        if event.get("type") == "turn.completed":
            completed = True
        if (
            event.get("type") == "result"
            and event.get("subtype") == "success"
            and event.get("is_error") is not True
            and isinstance(event.get("result"), str)
            and not event["result"].lstrip().startswith("[API Error:")
        ):
            completed = True
    return completed


def _record_attempt(
    root: Path, *, role: str, command: Sequence[str], resume: bool, stream: Path
) -> Path:
    """Write an immutable launch intent before starting one Codex client."""

    attempts = root / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    path = attempts / f"{len(tuple(attempts.glob('*.json'))) + 1:03d}.json"
    _write_json(
        path,
        {
            "schema_version": 1,
            "role": role,
            "command": list(command),
            "resume": resume,
            "stream": str(stream),
            "started_unix": time.time(),
            "status": "launching",
        },
    )
    return path


def _finish_attempt(path: Path, **result: object) -> None:
    retained = _read_json(path, {})
    value = dict(retained) if isinstance(retained, Mapping) else {"schema_version": 1}
    value.update(result)
    value["finished_unix"] = time.time()
    _write_json(path, value)


def _rehydrate_progress(root: Path, progress: dict[str, Any]) -> None:
    """Treat completed stage journals as authoritative after a controller crash."""

    arms = progress.setdefault("arms", {})
    for arm in ARMS:
        arm_state = arms.setdefault(arm, {"stages": {}})
        stages = arm_state.setdefault("stages", {})
        for stage in staged_workloads():
            result = _read_json(root / arm / "stages" / stage.stage_id / "result.json", {})
            if isinstance(result, Mapping) and result.get("status") in {"complete", "degraded", "blocking_product_error"}:
                stages[stage.stage_id] = dict(result)


def _contextmesh_brief_unavailable(final: Path) -> bool:
    """Return whether a completed retained worker stopped at the broker sentinel.

    A broker transport/product repair may make this one specific completion
    recoverable.  It is deliberately narrower than a generic failed test so
    test and scaffolding outcomes never replay an otherwise completed DeepSeek worker.
    """

    if not final.is_file():
        return False
    return "CONTEXTMESH_BRIEF_UNAVAILABLE" in final.read_text(encoding="utf-8", errors="replace")


def _prefetched_context(
    endpoint: Mapping[str, object] | None,
    plans: Sequence[WorkerPlan],
    ledgers: Sequence[OverlapLedgerEntry],
) -> dict[str, dict[str, dict[str, object]]]:
    """Retrieve only already-valid broker briefs for sealed worker packets."""

    if endpoint is None:
        return {}
    host, port = endpoint.get("host"), endpoint.get("port")
    if not isinstance(host, str) or not isinstance(port, int):
        raise RuntimeError("ContextMesh endpoint is malformed")

    async def fetch(plan: WorkerPlan) -> tuple[str, dict[str, dict[str, object]]]:
        requests: list[dict[str, object]] = []
        for entry in ledgers:
            if plan.worker_id != entry.source_owner and plan.worker_id not in entry.peer_workers:
                continue
            facts = list(plan.source_facts_for(entry.canonical_path))
            # Each worker's exact profile binding is already in its Terra plan;
            # the catalog brief needs only the stable API, not the record anchor
            # that belongs to another profile's source location.
            if entry.canonical_path == "ruleforge/policy_catalog.py":
                facts = [fact for fact in facts if not fact.lstrip().startswith("'operational.")]
            if facts:
                requests.append({"brief_id": entry.brief_id, "required_facts": facts})
        if not requests:
            return plan.worker_id, {}
        client = SharedBrokerClient(host, port)
        values = await client.get_ready_worker_briefs(requests, plan.worker_id)
        return plan.worker_id, values

    async def collect() -> list[tuple[str, dict[str, dict[str, object]]]]:
        return await asyncio.gather(*(fetch(plan) for plan in plans))

    rows = asyncio.run(collect())
    return dict(rows)


def _needs_contextmesh_bridge(
    plan: WorkerPlan,
    ledgers: Sequence[OverlapLedgerEntry],
    prefetched: Mapping[str, Mapping[str, object]],
) -> bool:
    """Expose MCP only when a worker still has a broker-routed overlap.

    A sealed packet must be self-sufficient. Leaving an MCP tool visible in
    that case invites redundant model-visible reads and can erase the very
    worker saving ContextMesh is intended to measure.
    """

    related = {
        entry.brief_id
        for entry in ledgers
        if plan.worker_id == entry.source_owner or plan.worker_id in entry.peer_workers
    }
    return bool(related - set(prefetched))


def _completed_degraded_stage(stage_root: Path, plans: Sequence[WorkerPlan], state: Mapping[str, object]) -> bool:
    """Whether a degraded stage is finished evidence rather than resumable work."""

    return state.get("status") == "degraded" and all(
        _stream_completed(stage_root / "workers" / plan.worker_id / "stream.jsonl")
        and not _contextmesh_brief_unavailable(stage_root / "workers" / plan.worker_id / "final.md")
        for plan in plans
    )


def _resume_command(model: str, final: Path, thread_id: str, *, bridge: bool, repo: Path) -> tuple[str, ...]:
    if model == WORKER_MODEL:
        return _resume_worker_command(final, thread_id, bridge=bridge, repo=repo)
    command = [
        os.environ.get("STAGED_CODEX_BIN", "codex"), "exec", "resume", "--ignore-user-config", "--enable", "fast_mode", "--model", model,
        "--config", "model_reasoning_effort=high", "--config", f"model_auto_compact_token_limit={AUTO_COMPACT_TOKEN_LIMIT}", "--config", "service_tier=priority",
    ]
    if bridge:
        for item in mcp_config(repo):
            command.extend(("--config", item))
    command.extend(("--dangerously-bypass-approvals-and-sandbox", "--json", "--output-last-message", str(final), thread_id, "-"))
    return tuple(command)


def _resume_worker_command(final: Path, thread_id: str, *, bridge: bool, repo: Path) -> tuple[str, ...]:
    """Continue one retained external Qwen session without a shared pointer."""

    return deepseek_command(
        repo,
        final,
        resume_thread_id=thread_id,
        extra_config=mcp_config(repo) if bridge else (),
    )


def _model_environment(repo: Path, arm: str, worker_id: str | None = None, endpoint: Mapping[str, object] | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({"PYTHONPATH": str(repo), "REASONRENDER_ARM": arm, "CONTEXTMESH_SUMMARIZER_COMMAND": "deterministic"})
    # Worker views intentionally contain sourceless fixtures compiled by the
    # harness interpreter. Put that interpreter first so a worker's literal
    # ``python -m pytest`` acceptance command does not select an unrelated
    # machine Python with incompatible bytecode magic.
    scripts = repo / ".codex" / "dev" / ".venv" / "Scripts"
    if scripts.is_dir():
        environment["PATH"] = str(scripts) + os.pathsep + environment.get("PATH", "")
    if worker_id is not None:
        environment["CONTEXTMESH_WORKER_ID"] = worker_id
    if endpoint is not None:
        environment["CONTEXTMESH_BROKER_HOST"] = str(endpoint["host"])
        environment["CONTEXTMESH_BROKER_PORT"] = str(endpoint["port"])
    return environment


def _run_model(
    command: Sequence[str],
    *,
    cwd: Path,
    prompt: Path,
    stream: Path,
    stderr: Path,
    environment: Mapping[str, str],
    resume: bool = False,
    timeout_seconds: float = 1_200,
) -> dict[str, object]:
    """Run or continue exactly one retained model session without replaying it."""

    stream.parent.mkdir(parents=True, exist_ok=True)
    stderr.parent.mkdir(parents=True, exist_ok=True)
    if stream.exists() and stream.stat().st_size and not resume:
        return {"exit_code": None, "orchestration_error": "retained nonempty stream has no resumable thread", "resumed": False}
    mode = "a" if resume else "w"
    started = time.monotonic()
    attempt = _record_attempt(stream.parent, role="terra", command=command, resume=resume, stream=stream)
    with prompt.open("r", encoding="utf-8") as stdin, stream.open(mode, encoding="utf-8") as stdout, stderr.open(mode, encoding="utf-8") as errors:
        try:
            completed = subprocess.run(
                list(command), cwd=cwd, stdin=stdin, stdout=stdout, stderr=errors, text=True,
                env=dict(environment), timeout=max(1, timeout_seconds), check=False,
            )
            result = {"exit_code": completed.returncode, "wall_seconds": round(time.monotonic() - started, 3), "resumed": resume}
        except subprocess.TimeoutExpired:
            result = {"exit_code": None, "timed_out": True, "wall_seconds": round(time.monotonic() - started, 3), "resumed": resume}
        except OSError as error:
            result = {"exit_code": None, "orchestration_error": f"{type(error).__name__}: {error}", "wall_seconds": round(time.monotonic() - started, 3), "resumed": resume}
    _finish_attempt(attempt, status="finished", **result)
    return result


def _run_deepseek_batch(
    repo: Path,
    arm: str,
    stage_root: Path,
    plans: Sequence[WorkerPlan],
    ledgers: Sequence[OverlapLedgerEntry],
    coordinator_plans: Sequence[TerraPlan],
    baseline: Path,
    endpoint: Mapping[str, object] | None,
    deadline: float,
    allow_contextmesh_sentinel_recovery: bool = True,
) -> list[dict[str, object]]:
    """Start all four direct DeepSeek processes before waiting for any one of them."""

    overlaps = {entry.canonical_path for entry in ledgers}
    by_task = {plan.task_id: plan for plan in coordinator_plans}
    prefetched = _prefetched_context(endpoint, plans, ledgers) if arm != "raw" else {}
    started: list[tuple[WorkerPlan, subprocess.Popen[str], Any, Any, Path, Path, Path]] = []
    outcomes: list[dict[str, object]] = []
    for plan in plans:
        worker_root = stage_root / "workers" / plan.worker_id
        target = stage_root / "worktrees" / plan.worker_id
        if not target.exists():
            raw_paths = plan.initial_read_paths if arm == "raw" else tuple(path for path in plan.initial_read_paths if path not in overlaps)
            materialize_worker_workspace(
                baseline, target, raw_source_paths=raw_paths, owned_write_paths=plan.owned_write_paths, python_executable=sys.executable
            )
            (target / "AGENTS.md").write_text(WORKSPACE_INSTRUCTIONS, encoding="utf-8")
        prompt = worker_root / "prompt.md"
        packet_path = worker_root / "packet.json"
        stream = worker_root / "stream.jsonl"
        stderr = worker_root / "stderr.log"
        final = worker_root / "final.md"
        # A degraded stage can contain a mix of completed and interrupted
        # sessions.  Preserve every completed DeepSeek result and resume only the
        # retained session that did not finish.  The one exception is an
        # explicit ContextMesh-unavailable terminal message: after a genuine
        # broker product repair its same thread may continue, without replay.
        needs_broker_recovery = (
            allow_contextmesh_sentinel_recovery
            and arm == "contextmesh"
            and _contextmesh_brief_unavailable(final)
        )
        if _stream_completed(stream) and not needs_broker_recovery:
            outcomes.append({"worker_id": plan.worker_id, "mode": "retained", "stream_completed": True})
            continue
        worker_root.mkdir(parents=True, exist_ok=True)
        retained_packet = _read_json(packet_path, None)
        if retained_packet is None:
            if stream.exists() and stream.stat().st_size:
                outcomes.append({"worker_id": plan.worker_id, "orchestration_error": "retained worker stream lacks its delivered packet", "stream_completed": False})
                continue
            retained_packet = render_worker_packet(
                arm,
                plan,
                ledgers,
                by_task[plan.task_id],
                prefetched.get(plan.worker_id),
                delivery_worktree=str(target.resolve()),
            )
            _write_json(packet_path, retained_packet)
        if not isinstance(retained_packet, Mapping) or retained_packet.get("task_id") != plan.task_id or retained_packet.get("worker_id") != plan.worker_id:
            outcomes.append({"worker_id": plan.worker_id, "orchestration_error": "retained worker packet is malformed or belongs to another task", "stream_completed": False})
            continue
        prompt.write_text(worker_prompt_from_packet(arm, retained_packet), encoding="utf-8")
        thread_id = _stream_thread_id(stream)
        bridge = arm != "raw" and _needs_contextmesh_bridge(plan, ledgers, prefetched.get(plan.worker_id, {}))
        command = _resume_command(WORKER_MODEL, final, thread_id, bridge=bridge, repo=repo) if thread_id else _command(WORKER_MODEL, final, bridge=bridge, repo=repo)
        if stream.exists() and stream.stat().st_size and not thread_id:
            outcomes.append({"worker_id": plan.worker_id, "orchestration_error": "retained nonempty stream has no resumable thread", "stream_completed": False})
            continue
        mode = "a" if thread_id else "w"
        stdin = prompt.open("r", encoding="utf-8")
        stdout = stream.open(mode, encoding="utf-8")
        errors = stderr.open(mode, encoding="utf-8")
        environment = _model_environment(repo, arm, plan.worker_id, endpoint if bridge else None)
        attempt = _record_attempt(worker_root, role=plan.worker_id, command=command, resume=thread_id is not None, stream=stream)
        try:
            process = subprocess.Popen(list(command), cwd=target, stdin=stdin, stdout=stdout, stderr=errors, text=True, env=environment)
        except OSError as error:
            stdin.close(); stdout.close(); errors.close()
            outcomes.append({"worker_id": plan.worker_id, "orchestration_error": f"{type(error).__name__}: {error}", "stream_completed": False})
            _finish_attempt(attempt, status="launch_error", error=f"{type(error).__name__}: {error}")
            continue
        _finish_attempt(attempt, status="running", pid=process.pid)
        started.append((plan, process, stdin, stdout, errors, stream, stderr, attempt))

    for plan, process, stdin, stdout, errors, stream, stderr, attempt in started:
        started_at = time.monotonic()
        try:
            code = process.wait(timeout=max(1, deadline - time.monotonic()))
            outcome: dict[str, object] = {"worker_id": plan.worker_id, "exit_code": code, "wall_seconds": round(time.monotonic() - started_at, 3)}
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            outcome = {"worker_id": plan.worker_id, "exit_code": None, "timed_out": True, "wall_seconds": round(time.monotonic() - started_at, 3)}
        finally:
            stdin.close(); stdout.close(); errors.close()
        outcome["stream_completed"] = _stream_completed(stream)
        _finish_attempt(attempt, status="finished", **outcome)
        outcomes.append(outcome)
    return sorted(outcomes, key=lambda item: str(item["worker_id"]))


def _accept_and_merge(
    repo: Path, stage_root: Path, baseline: Path, plans: Sequence[WorkerPlan], *, source_policy_valid: bool = True
) -> tuple[list[dict[str, object]], list[str]]:
    """Keep only accepted disjoint worker writes at a stage boundary."""

    results: list[dict[str, object]] = []
    accepted_sources: list[tuple[str, Path]] = []
    for plan in plans:
        worker_root = stage_root / "workers" / plan.worker_id
        target = stage_root / "worktrees" / plan.worker_id
        stdout = worker_root / "acceptance.stdout.log"
        stderr = worker_root / "acceptance.stderr.log"
        if not _stream_completed(worker_root / "stream.jsonl"):
            results.append({"worker_id": plan.worker_id, "accepted": False, "reason": "incomplete_model_stream"})
            continue
        try:
            command = shlex.split(plan.acceptance_cmd)
            if command and command[0] == "python":
                command = ["uv", "run", "--project", str(repo / ".codex" / "dev"), "--locked", "python", *command[1:]]
            completed = subprocess.run(command, cwd=target, text=True, capture_output=True, timeout=120, check=False)
            stdout.write_text(completed.stdout, encoding="utf-8")
            stderr.write_text(completed.stderr, encoding="utf-8")
            accepted = completed.returncode == 0
            result: dict[str, object] = {"worker_id": plan.worker_id, "accepted": accepted, "exit_code": completed.returncode}
        except (OSError, subprocess.TimeoutExpired) as error:
            result = {"worker_id": plan.worker_id, "accepted": False, "reason": f"{type(error).__name__}: {error}"}
            accepted = False
        if accepted:
            for relative in plan.owned_write_paths:
                source = target / relative
                if source.is_file():
                    accepted_sources.append((relative, source))
        results.append(result)
    if not source_policy_valid:
        for result in results:
            if result.get("accepted") is True:
                result.update({"accepted": False, "reason": "source_policy_violation"})
    if not all(item.get("accepted") is True for item in results):
        # A partial stage remains retained diagnostic evidence, but its output
        # may not become an uncommitted hidden input to a later linked stage.
        return results, []
    copied: list[str] = []
    for relative, source in accepted_sources:
        destination = baseline / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(relative)
    return results, sorted(set(copied))


def _source_policy_violations(
    arm: str, stage_root: Path, plans: Sequence[WorkerPlan], entries: Sequence[OverlapLedgerEntry]
) -> list[dict[str, str]]:
    """Retain every direct-source bypass before it can reach a stage boundary."""

    candidates = sorted({path for plan in plans for path in plan.initial_read_paths})
    overlaps = {entry.canonical_path for entry in entries}
    rows: list[dict[str, str]] = []
    for plan in plans:
        stream = stage_root / "workers" / plan.worker_id / "stream.jsonl"
        if not stream.is_file():
            continue
        allowed = plan.initial_read_paths if arm == "raw" else tuple(path for path in plan.initial_read_paths if path not in overlaps)
        violations: tuple[SourcePolicyViolation, ...] = direct_unlisted_source_reads(stream, plan.worker_id, allowed, candidates)
        if arm != "raw":
            violations += direct_overlap_reads(stream, plan.worker_id, entries)
        for item in violations:
            rows.append({"worker_id": item.worker_id, "canonical_path": item.canonical_path, "command": item.command})
    return rows


def _packet_sha256(packet_path: Path) -> str | None:
    """Hash a retained dispatch artifact without recreating any packet content."""

    try:
        return hashlib.sha256(packet_path.read_bytes()).hexdigest()
    except OSError:
        return None


def _worker_delivery_evidence(
    arm: str,
    stage_root: Path,
    plans: Sequence[WorkerPlan],
    entries: Sequence[OverlapLedgerEntry],
) -> tuple[bool, list[dict[str, object]]]:
    """Validate packets and observed local reads that were retained at dispatch.

    This function never calls a packet renderer.  Its only inputs are the
    frozen stage contract plus artifacts written before a worker was started,
    so a later runner edit cannot manufacture promotion evidence.
    """

    overlaps = {entry.canonical_path for entry in entries}
    by_path = {entry.canonical_path: entry for entry in entries}
    rows: list[dict[str, object]] = []
    for plan in plans:
        worker_root = stage_root / "workers" / plan.worker_id
        packet_path = worker_root / "packet.json"
        read_set_path = worker_root / "local-read-set.json"
        packet = _read_json(packet_path, None)
        expected_local = list(plan.initial_read_paths) if arm == "raw" else [
            path for path in plan.initial_read_paths if path not in overlaps
        ]
        packet_valid = (
            isinstance(packet, Mapping)
            and packet.get("task_id") == plan.task_id
            and packet.get("worker_id") == plan.worker_id
            and packet.get("worktree") == str((stage_root / "worktrees" / plan.worker_id).resolve())
        )
        if packet_valid:
            if arm == "raw":
                packet_valid = (
                    packet.get("initial_read_paths") == list(plan.initial_read_paths)
                    and "local_read_paths" not in packet
                    and "contextmesh_owner_overlaps" not in packet
                    and "contextmesh_peer_overlaps" not in packet
                )
            else:
                related = {
                    entry.brief_id: entry
                    for entry in entries
                    if plan.worker_id == entry.source_owner or plan.worker_id in entry.peer_workers
                }
                prefetched = packet.get("contextmesh_prefetched_briefs", {})
                if not isinstance(prefetched, Mapping) or not all(
                    isinstance(path, str) and path in by_path and isinstance(facts, list)
                    and all(isinstance(fact, str) for fact in facts)
                    for path, facts in prefetched.items()
                ):
                    packet_valid = False
                else:
                    prefetched_ids = {
                        entry.brief_id for path, entry in by_path.items() if path in prefetched and entry.brief_id in related
                    }
                    owner_ids = {
                        item.get("brief_id")
                        for item in packet.get("contextmesh_owner_overlaps", [])
                        if isinstance(item, Mapping) and item.get("brief_id") in related and related[item["brief_id"]].source_owner == plan.worker_id
                    }
                    peer_ids = {
                        item.get("brief_id")
                        for item in packet.get("contextmesh_peer_overlaps", [])
                        if isinstance(item, Mapping) and item.get("brief_id") in related and related[item["brief_id"]].source_owner != plan.worker_id
                    }
                    packet_valid = (
                        packet.get("local_read_paths") == expected_local
                        and owner_ids.isdisjoint(peer_ids)
                        and prefetched_ids.isdisjoint(owner_ids | peer_ids)
                        and prefetched_ids | owner_ids | peer_ids == set(related)
                        and (not packet.get("contextmesh_sealed") or prefetched_ids == set(related))
                    )
        read_set = observed_local_read_set(worker_root / "stream.jsonl", plan.worker_id, expected_local)
        _write_json(read_set_path, read_set)
        read_valid = (
            read_set.get("worker_id") == plan.worker_id
            and read_set.get("allowed_local_read_paths") == sorted(expected_local)
            and all(path in expected_local for path in read_set.get("observed_local_read_paths", []))
        )
        row = {
            "worker_id": plan.worker_id,
            "packet": str(packet_path),
            "packet_sha256": _packet_sha256(packet_path),
            "packet_valid": packet_valid,
            "local_read_set": str(read_set_path),
            "local_read_set_sha256": _packet_sha256(read_set_path),
            "local_read_set_valid": read_valid,
            "observed_local_read_paths": read_set["observed_local_read_paths"],
        }
        row["valid"] = bool(packet_valid and read_valid and row["packet_sha256"] and row["local_read_set_sha256"])
        rows.append(row)
    return bool(rows) and all(row["valid"] is True for row in rows), rows


def _mcp_admission(repo: Path, root: Path) -> dict[str, object]:
    """Run exactly one no-provider health check and one source-free DeepSeek probe."""

    probe_root = root / "mcp-eligibility-probe"
    retained = _read_json(probe_root / "result.json", None)
    if isinstance(retained, dict) and "valid" in retained:
        return retained
    health = check_mcp_health(repo_root=repo)
    _write_json(probe_root / "health.json", health)
    if not health.get("valid"):
        result: dict[str, object] = {"schema_version": 1, "valid": False, "health_valid": False, "reason": "mcp_health_failed"}
    else:
        try:
            result = dict(run_mcp_model_probe(repo, probe_root))
        except (OSError, subprocess.SubprocessError) as error:
            result = {"schema_version": 1, "valid": False, "health_valid": True, "reason": f"{type(error).__name__}: {error}"}
        result["health_valid"] = True
    _write_json(probe_root / "result.json", result)
    return result


def _start_broker(
    repo: Path,
    arm_root: Path,
    baseline: Path,
    stage: StageWorkload,
    ledger: Path,
    stage_commit: str,
    parent_commit: str | None,
    round_id: str,
) -> tuple[subprocess.Popen[str], dict[str, object]]:
    broker = arm_root / "broker"
    port_file = broker / "port.json"
    if port_file.exists():
        port_file.unlink()
    token = (broker / "control-token.txt").read_text(encoding="utf-8").strip()
    command = [
        sys.executable, "-m", "contextmesh.mcp.broker_service", "--source-root", str(baseline), "--ledger", str(ledger),
        "--state-dir", str(broker / "state"), "--log", str(broker / "events.jsonl"), "--port-file", str(port_file),
        "--wait-timeout-ms", str(BROKER_WAIT_TIMEOUT_MS), "--workflow-id", f"ruleforge-staged/{round_id}/{arm_root.name}",
        "--stage-id", stage.stage_id, "--stage-commit", stage_commit, "--git-root", str(baseline), "--control-token", token,
    ]
    if parent_commit is not None:
        command.extend(("--parent-stage-commit", parent_commit))
    stdout = (broker / "service.stdout.log").open("a", encoding="utf-8")
    stderr = (broker / "service.stderr.log").open("a", encoding="utf-8")
    process = subprocess.Popen(command, cwd=repo, stdout=stdout, stderr=stderr, text=True, env=_model_environment(repo, arm_root.name))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if port_file.is_file():
            endpoint = _read_json(port_file, {})
            if isinstance(endpoint, dict) and isinstance(endpoint.get("host"), str) and isinstance(endpoint.get("port"), int):
                return process, endpoint
        if process.poll() is not None:
            break
        time.sleep(0.1)
    process.terminate()
    raise RuntimeError("ContextMesh broker did not publish a usable endpoint")


def _advance_broker(
    repo: Path,
    arm_root: Path,
    ledger: Path,
    stage: StageWorkload,
    stage_commit: str,
    parent_commit: str,
) -> None:
    broker = arm_root / "broker"
    command = [
        sys.executable, "-m", "contextmesh.mcp.advance_stage", "--endpoint", str(broker / "port.json"),
        "--control-token", (broker / "control-token.txt").read_text(encoding="utf-8").strip(), "--ledger", str(ledger), "--stage-id", stage.stage_id,
        "--stage-commit", stage_commit, "--parent-stage-commit", parent_commit,
    ]
    completed = subprocess.run(command, cwd=repo, text=True, capture_output=True, env=_model_environment(repo, arm_root.name), timeout=30, check=False)
    (broker / f"{stage.stage_id}-advance.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (broker / f"{stage.stage_id}-advance.stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "ContextMesh install_stage failed")


def _broker_parent_commit(state: Mapping[str, object], fallback: str | None = None) -> str | None:
    """Return the source revision installed for a retained broker stage."""

    return str(state.get("source_commit") or "") or fallback


def _stop_broker(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def _prepare_terra_workspace(baseline: Path, stage_root: Path) -> Path:
    target = stage_root / "terra" / "worktree"
    if not target.exists():
        materialize_workspace(baseline, target)
        (target / "AGENTS.md").write_text(WORKSPACE_INSTRUCTIONS, encoding="utf-8")
    return target


def _raw_terra_prompt(request: Path) -> str:
    return (
        "You are the single Terra coordinator for this measured RuleForge stage. Directly inspect only the declared "
        "initial source paths in the request, then write exactly four concrete JSON plans to .terra-plans. Use the request schema; "
        "include every required fact anchor verbatim, but no source bodies, imports, code, test bodies, or worker reread instructions. "
        "For each plan, use this source-free four-step structure after verifying its facts: create the owned rule module from its "
        "declared profile; construct/register its definition and expected value; preserve allowed and denied Decision behavior; add "
        "focused comparator coverage and run the named acceptance command. Do not edit RuleForge source, delegate, or launch workers. "
        "End with TERRA_RAW_PREFLIGHT.\n\n"
        f"Coordinator plan request: {request}\n"
    )


def _full_first_terra_prompt(hit: Path, stage_id: str) -> str:
    return (
        "You are the single Terra coordinator for this measured Full/RRCv2 stage. Do not inspect any RuleForge source. "
        "Use only the retained local RRC cache: prove its HIT, then render the four stage-qualified plans. Do not edit RuleForge "
        "source, delegate, or launch workers. End with TERRA_RRC_HIT_RENDER.\n\n"
        f"Run exactly: python -m harness.rrc_hit --workspace . --output {hit}\n"
        f"Then run exactly: python -m harness.terra_plans render-rrc --workspace . --output .terra-plans --stage-id {stage_id}\n"
    )


def _full_delta_terra_prompt(
    stage_id: str, workflow_id: str, parent_key: str, dependencies: Path, evidence: Path
) -> str:
    return (
        "You are the single Terra coordinator for a later measured Full/RRCv2 stage. Do not inspect RuleForge source or reconstruct "
        "a plan. Use the retained source-free stage-plan state and dependency revision file to render the four concrete delta plans. "
        "Do not edit source, delegate, or launch workers. End with TERRA_RRC_STAGE_HIT_RENDER.\n\n"
        "Run exactly: python -m harness.terra_plans render-stage-rrc "
        f"--workspace . --output .terra-plans --stage-id {stage_id} --workflow-id {workflow_id} --arm full "
        f"--parent-stage-key {parent_key} --dependencies {dependencies} --evidence-output {evidence}\n"
    )


def _require_first_rrc_hit(path: Path, plans: Sequence[WorkerPlan]) -> dict[str, object]:
    evidence = _read_json(path, {})
    expected = {plan.task_id for plan in plans}
    matches = evidence.get("lookup", {}).get("matches") if isinstance(evidence, Mapping) else None
    matched = {item.get("task_id") for item in matches if isinstance(item, Mapping)} if isinstance(matches, list) else set()
    refs = {item.get("external_ref") for item in matches if isinstance(item, Mapping)} if isinstance(matches, list) else set()
    if not (
        isinstance(evidence, Mapping)
        and evidence.get("event") == "rrc_hit"
        and isinstance(evidence.get("template_external_ref"), str)
        and evidence.get("cache_warm", {}).get("event") == "rrc_cache_warm"
        and matched == expected
        and refs == {evidence.get("template_external_ref")}
    ):
        raise RuntimeError("Full Terra lacks a valid prewarmed four-binding RRC HIT")
    return dict(evidence)


def _require_delta_rrc_hit(
    path: Path, state_path: Path, *, workflow_id: str, stage_id: str, parent_key: str, dependencies: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    hit, state = _read_json(path, {}), _read_json(state_path, {})
    if not (
        isinstance(hit, Mapping)
        and hit.get("event") == "rrc_stage_plan_hit"
        and hit.get("workflow_id") == workflow_id
        and hit.get("arm") == "full"
        and hit.get("stage_id") == stage_id
        and hit.get("parent_stage_plan_key") == parent_key
        and hit.get("reconstruction_reads") == 0
        and isinstance(hit.get("template_external_ref"), str)
        and hit.get("dependency_revisions") == list(dependencies)
        and isinstance(state, Mapping)
        and state.get("event") == "rrc_stage_plan_state"
        and state.get("workflow_id") == workflow_id
        and state.get("stage_id") == stage_id
        and state.get("parent_stage_key") == parent_key
        and state.get("stage_plan_key") == hit.get("stage_plan_key")
        and state.get("template_external_ref") == hit.get("template_external_ref")
        and state.get("dependency_revisions") == list(dependencies)
    ):
        raise RuntimeError("Full Terra lacks a valid zero-read RRC stage HIT")
    return dict(hit)


def _run_terra(
    repo: Path,
    arm: str,
    stage: StageWorkload,
    stage_root: Path,
    baseline: Path,
    round_id: str,
    entries: Sequence[OverlapLedgerEntry],
    previous_full_key: str | None,
    deadline: float,
) -> tuple[tuple[TerraPlan, ...], dict[str, object]]:
    """Produce retained plans exactly once; ContextMesh obtains Raw's files elsewhere."""

    terra_root = stage_root / "terra"
    target = _prepare_terra_workspace(baseline, stage_root)
    plan_root = target / ".terra-plans"
    stream = terra_root / "stream.jsonl"
    final = terra_root / "final.md"
    stderr = terra_root / "stderr.log"
    prompt = terra_root / "prompt.md"
    request = target / ".terra-plan-request.json"
    if arm == "raw":
        write_task_request(request, stage.plans)
        prompt.write_text(_raw_terra_prompt(request), encoding="utf-8")
    else:
        _copy_stage_cache(repo, target, stage)
        try:
            warm_rrc_cache(target, terra_root / "rrc-warm.json")
        except (OSError, RRCManifestError, subprocess.SubprocessError, ValueError) as error:
            raise RuntimeError(f"RRC local prewarm failed: {error}") from error
        if previous_full_key is None:
            prompt.write_text(_full_first_terra_prompt(terra_root / "rrc-hit.json", stage.stage_id), encoding="utf-8")
        else:
            dependencies = terra_root / "dependencies.json"
            _write_json(dependencies, {"dependency_revisions": _source_dependencies(entries, baseline)})
            prompt.write_text(
                _full_delta_terra_prompt(
                    stage.stage_id, f"ruleforge-staged/{round_id}/full", previous_full_key, dependencies,
                    terra_root / "rrc-stage-state.json",
                ),
                encoding="utf-8",
            )
    if not _stream_completed(stream):
        thread_id = _stream_thread_id(stream)
        command = _resume_command(TERRA_MODEL, final, thread_id, bridge=False, repo=repo) if thread_id else _command(TERRA_MODEL, final, bridge=False, repo=repo)
        result = _run_model(
            command, cwd=target, prompt=prompt, stream=stream, stderr=stderr,
            environment=_model_environment(repo, arm), resume=thread_id is not None,
            timeout_seconds=max(1, deadline - time.monotonic()),
        )
        _write_json(terra_root / "result.json", result)
    try:
        rendered = load_terra_plans(plan_root, stage.plans)
    except TerraPlanError as error:
        raise RuntimeError(f"{arm} Terra did not retain valid plans for {stage.stage_id}: {error}") from error
    evidence: dict[str, object] = {"delivery_plan_sha256": plan_set_sha256(rendered), "plan_root": str(plan_root)}
    if arm == "full":
        dependencies = _source_dependencies(entries, baseline)
        if previous_full_key is None:
            hit = _require_first_rrc_hit(terra_root / "rrc-hit.json", stage.plans)
            state = record_stage_plan_state(
                target, workflow_id=f"ruleforge-staged/{round_id}/full", arm="full", stage_id=stage.stage_id,
                parent_stage_key=None, task_shape={"family": "ruleforge", "workers": 4, "worker_ids": [plan.worker_id for plan in stage.plans], "plan_schema": "terra/v1"},
                template_version="ruleforge-template/v1", template_external_ref=str(hit["template_external_ref"]),
                delivery_plan_sha256=plan_set_sha256(rendered), dependency_revisions=dependencies,
                output=terra_root / "rrc-stage-state.json",
            )
        else:
            state = _read_json(terra_root / "rrc-stage-state.json", {})
            hit = _require_delta_rrc_hit(
                terra_root / "rrc-stage-hit.json", terra_root / "rrc-stage-state.json",
                workflow_id=f"ruleforge-staged/{round_id}/full", stage_id=stage.stage_id,
                parent_key=previous_full_key, dependencies=dependencies,
            )
        evidence["rrc_stage_plan_key"] = state["stage_plan_key"]
        evidence["rrc_hit"] = hit
        cache = target / ".rrc-cache"
        shutil.copytree(cache, baseline / ".rrc-cache", dirs_exist_ok=True)
    return rendered, evidence


def _usage(path: Path, output: Path) -> dict[str, object]:
    try:
        return collect_codex_stream(path, output)
    except CollectionError as error:
        return {"valid": False, "invalid_reasons": [str(error)], "totals": {}}


def _usage_total(values: Sequence[Mapping[str, object]]) -> dict[str, int]:
    fields = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "input_new_tokens", "output_tokens", "reasoning_output_tokens", "turns")
    total = {field: 0 for field in fields}
    for value in values:
        nested = value.get("totals")
        # Stream collector records use a ``totals`` envelope, while a stage
        # aggregate is already a direct totals mapping.  Supporting both is
        # what lets the arm report sum its retained stage evidence without
        # silently turning a real measured total into zero.
        if not isinstance(nested, Mapping):
            nested = value
        for field in fields:
            amount = nested.get(field, 0)
            if isinstance(amount, int):
                total[field] += amount
    return total


def _marginal(total: Mapping[str, object]) -> int:
    return sum(int(total.get(field, 0)) for field in ("input_new_tokens", "output_tokens", "reasoning_output_tokens"))


def _write_stage_usage(root: Path, arm: str, stage: StageWorkload, raw_root: Path | None = None) -> dict[str, object]:
    stage_root = root / arm / "stages" / stage.stage_id
    entries = staged_overlap_ledgers()[next(index for index, item in enumerate(staged_workloads()) if item.stage_id == stage.stage_id)]
    owners = {entry.source_owner for entry in entries}
    peers = {worker_id for entry in entries for worker_id in entry.peer_workers}
    worker_usages = [
        {
            "worker_id": plan.worker_id,
            "overlap_role": "both" if plan.worker_id in owners and plan.worker_id in peers else "owner" if plan.worker_id in owners else "peer" if plan.worker_id in peers else "unique_only",
            **_usage(stage_root / "workers" / plan.worker_id / "stream.jsonl", stage_root / "usage" / "workers" / plan.worker_id),
        }
        for plan in stage.plans
    ]
    if arm == "contextmesh" and raw_root is not None:
        terra_usage = _usage(raw_root / "raw" / "stages" / stage.stage_id / "terra" / "stream.jsonl", stage_root / "usage" / "inherited-raw-terra")
        terra_mode = "inherited_raw_plan"
    else:
        terra_usage = _usage(stage_root / "terra" / "stream.jsonl", stage_root / "usage" / "terra")
        terra_mode = "measured"
    worker_total = _usage_total(worker_usages)
    terra_total = _usage_total([terra_usage])
    by_role = {
        role: _usage_total([item for item in worker_usages if item["overlap_role"] == role])
        for role in ("owner", "peer", "both", "unique_only")
    }
    return {
        "stage_id": stage.stage_id,
        "terra_mode": terra_mode,
        "terra": terra_usage,
        "workers": worker_usages,
        "worker_totals": worker_total,
        "worker_role_totals": by_role,
        "terra_totals": terra_total,
        "worker_marginal_compute": _marginal(worker_total),
        "terra_marginal_compute": _marginal(terra_total),
        "logical_aggregate_marginal_compute": _marginal(worker_total) + _marginal(terra_total),
    }


def _arm_report(root: Path, arm: str, stages: Sequence[StageWorkload]) -> dict[str, object]:
    raw_root = root if arm == "contextmesh" else None
    stage_usages = [_write_stage_usage(root, arm, stage, raw_root) for stage in stages]
    worker_totals = _usage_total([item["worker_totals"] for item in stage_usages])
    terra_totals = _usage_total([item["terra_totals"] for item in stage_usages])
    stage_results = [_read_json(root / arm / "stages" / stage.stage_id / "result.json", {}) for stage in stages]
    contracts = [manifest_sha256(stage.plans) for stage in stages]
    return {
        "arm": arm,
        "stages": stage_usages,
        "stage_results": stage_results,
        "worker_contract_sha256": contracts,
        "worker_totals": worker_totals,
        "terra_totals": terra_totals,
        "worker_marginal_compute": _marginal(worker_totals),
        "terra_marginal_compute": _marginal(terra_totals),
        "logical_aggregate_marginal_compute": _marginal(worker_totals) + _marginal(terra_totals),
        "later_stage_totals": {
            "worker": _usage_total([item["worker_totals"] for item in stage_usages[1:]]),
            "terra": _usage_total([item["terra_totals"] for item in stage_usages[1:]]),
        },
    }


def _manifest_evidence(stage_state: Mapping[str, object]) -> tuple[str | None, object | None]:
    path_value = stage_state.get("manifest")
    if not isinstance(path_value, str):
        return None, None
    path = Path(path_value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, Mapping):
        return None, None
    reads = [
        {"worker_id": task.get("worker_id"), "initial_read_paths": task.get("initial_read_paths")}
        for task in payload.get("tasks", ())
        if isinstance(task, Mapping)
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(), reads


def _broker_evidence(root: Path, arm: str, stages: Sequence[StageWorkload]) -> tuple[bool, list[dict[str, object]]]:
    """Build a strict, retained owner/publication/service evidence matrix."""

    path = root / arm / "broker" / "events.jsonl"
    rows: list[Mapping[str, object]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, Mapping):
                rows.append(value)
    evidence: list[dict[str, object]] = []
    ledgers = staged_overlap_ledgers()
    for stage, entries in zip(stages, ledgers[-len(stages):], strict=True):
        for entry in entries:
            matching = [row for row in rows if row.get("brief_id") == entry.brief_id]
            names = {str(row.get("event")) for row in matching}
            raw_claims = [
                row for row in matching
                if row.get("event") == "source_claim_raw" and row.get("owner_id") == entry.source_owner
            ]
            diff_claims = [
                row for row in matching
                if row.get("event") == "brief_refresh_diff" and row.get("owner_id") == entry.source_owner
            ]
            invalidated_claims = [
                row for row in matching
                if row.get("event") == "brief_refresh_invalidated" and row.get("owner_id") == entry.source_owner
            ]
            reuse_events = [row for row in matching if row.get("event") == "brief_reused_unchanged"]
            primary_modes = [
                name for name, present in (
                    ("diff_refresh", bool(diff_claims)),
                    ("invalidated_raw", bool(invalidated_claims)),
                    ("unchanged_reuse", bool(reuse_events)),
                ) if present
            ]
            if primary_modes:
                mode = primary_modes[0] if len(primary_modes) == 1 else "ambiguous"
            else:
                mode = "raw" if raw_claims else "missing"
            publications = [
                row for row in matching
                if row.get("event") == "brief_published" and row.get("owner_id") == entry.source_owner
            ]
            expected_refresh = {
                "raw": "raw",
                "diff_refresh": "diff_refresh",
                "invalidated_raw": "invalidated_raw",
            }.get(mode)
            publication_valid = (
                len(publications) == 1 and publications[0].get("refresh_kind") == expected_refresh
                if expected_refresh is not None else mode == "unchanged_reuse" and not publications
            )
            services: dict[str, bool] = {}
            service_rows: list[Mapping[str, object]] = []
            for peer in entry.peer_workers:
                peer_rows = [
                    row for row in matching
                    if (
                        row.get("event") == "brief_served" and row.get("peer_id") == peer
                    ) or (
                        row.get("event") == "brief_prefetched_worker_projection" and row.get("worker_id") == peer
                    )
                ]
                services[peer] = bool(peer_rows)
                service_rows.extend(peer_rows)
            producer_rows = (
                publications if expected_refresh is not None else reuse_events
            )
            producer_ts = [row.get("ts") for row in producer_rows if isinstance(row.get("ts"), int)]
            service_ts = [row.get("ts") for row in service_rows if isinstance(row.get("ts"), int)]
            owner_before_peer = bool(producer_ts) and all(
                timestamp >= min(producer_ts) for timestamp in service_ts
            )
            owner_claim_valid = (
                len(raw_claims) == 1 if mode in {"raw", "invalidated_raw"}
                else len(diff_claims) == 1 if mode == "diff_refresh"
                else len(reuse_events) == 1 if mode == "unchanged_reuse"
                else False
            )
            peer_service_valid = bool(services) and all(services.values())
            valid = mode != "missing" and mode != "ambiguous" and owner_claim_valid and publication_valid and peer_service_valid and owner_before_peer
            evidence.append(
                {
                    "stage_id": stage.stage_id, "brief_id": entry.brief_id, "canonical_path": entry.canonical_path,
                    "source_owner": entry.source_owner, "peer_workers": list(entry.peer_workers), "mode": mode,
                    "events": sorted(names),
                    "owner_claim_valid": owner_claim_valid,
                    "publication_valid": publication_valid,
                    "peer_service": services,
                    "owner_before_peer": owner_before_peer,
                    "valid": valid,
                }
            )
    return bool(evidence) and all(row["valid"] is True for row in evidence), evidence


def _reduction(baseline: int, candidate: int) -> float | None:
    return None if baseline <= 0 else round((baseline - candidate) / baseline, 6)


def _write_report(
    root: Path,
    round_id: str,
    stages: Sequence[StageWorkload] | None = None,
    *,
    filename: str = "report.json",
) -> dict[str, object]:
    stages = tuple(stages) if stages is not None else staged_workloads()
    arms = {arm: _arm_report(root, arm, stages) for arm in ARMS}
    raw = arms["raw"]
    contextmesh = arms["contextmesh"]
    full = arms["full"]
    report: dict[str, object] = {
        "schema_version": 1,
        "round_id": round_id,
        "cohort": _read_json(root / "cohort.json", {}),
        "arms": arms,
        "reductions": {
            "contextmesh_worker_vs_raw": _reduction(int(raw["worker_marginal_compute"]), int(contextmesh["worker_marginal_compute"])),
            "rrcv2_terra_vs_raw": _reduction(int(raw["terra_marginal_compute"]), int(full["terra_marginal_compute"])),
            "full_aggregate_vs_raw": _reduction(int(raw["logical_aggregate_marginal_compute"]), int(full["logical_aggregate_marginal_compute"])),
        },
    }
    reductions = report["reductions"]
    cm_worker = reductions["contextmesh_worker_vs_raw"]
    rrc_terra = reductions["rrcv2_terra_vs_raw"]
    full_total = reductions["full_aggregate_vs_raw"]
    report["economy_gate"] = {
        "contextmesh_worker_material": isinstance(cm_worker, float) and cm_worker >= 0.15,
        "rrcv2_terra_material": isinstance(rrc_terra, float) and rrc_terra >= 0.15,
        "full_below_contextmesh": int(full["logical_aggregate_marginal_compute"]) < int(contextmesh["logical_aggregate_marginal_compute"]),
        "full_aggregate_material": isinstance(full_total, float) and full_total >= 0.15,
        "later_contextmesh_worker_below_raw": _marginal(contextmesh["later_stage_totals"]["worker"]) < _marginal(raw["later_stage_totals"]["worker"]),
        "later_rrcv2_terra_below_raw": _marginal(full["later_stage_totals"]["terra"]) < _marginal(raw["later_stage_totals"]["terra"]),
    }
    expected_workers = sum(len(stage.plans) for stage in stages)
    stage_states = {
        arm: [result for result in values["stage_results"] if isinstance(result, Mapping)]
        for arm, values in arms.items()
    }
    acceptance_counts = {
        arm: sum(
            sum(item.get("accepted") is True for item in result.get("acceptance", ()) if isinstance(item, Mapping))
            for result in results
        )
        for arm, results in stage_states.items()
    }
    source_policy_valid = all(
        all(result.get("source_policy", {}).get("valid", arm == "raw") is True for result in results)
        for arm, results in stage_states.items()
    )
    retained_worker_delivery = all(
        len(results) == len(stages)
        and all(
            isinstance(result.get("worker_delivery_evidence"), Mapping)
            and result["worker_delivery_evidence"].get("valid") is True
            and isinstance(result["worker_delivery_evidence"].get("workers"), list)
            and len(result["worker_delivery_evidence"]["workers"]) == len(stage.plans)
            and all(
                isinstance(worker, Mapping)
                and worker.get("valid") is True
                and isinstance(worker.get("packet_sha256"), str)
                and isinstance(worker.get("local_read_set_sha256"), str)
                for worker in result["worker_delivery_evidence"]["workers"]
            )
            for result, stage in zip(results, stages, strict=True)
        )
        for results in stage_states.values()
    )
    admission = _read_json(root / "mcp-eligibility-probe" / "result.json", {})
    health = _read_json(root / "mcp-eligibility-probe" / "health.json", {})
    nonraw_admission = isinstance(admission, Mapping) and admission.get("valid") is True and isinstance(health, Mapping) and health.get("valid") is True
    usage_valid = all(
        all(item.get("valid") is True for stage in values["stages"] for item in [stage["terra"], *stage["workers"]])
        for values in arms.values()
    )
    plan_pairing = all(
        raw_state.get("delivery_plan_sha256") == context_state.get("delivery_plan_sha256")
        for raw_state, context_state in zip(stage_states["raw"], stage_states["contextmesh"], strict=True)
    ) and len(stage_states["raw"]) == len(stage_states["contextmesh"]) == len(stages)
    manifests = {arm: [_manifest_evidence(result) for result in results] for arm, results in stage_states.items()}
    contract_parity = all(
        len(manifests[arm]) == len(stages)
        and all(item[0] is not None and item == baseline for item, baseline in zip(manifests[arm], manifests["raw"], strict=True))
        for arm in ("contextmesh", "full")
    ) and all(item[0] is not None for item in manifests["raw"])
    complete_stage_statuses = all(
        len(results) == len(stages) and all(result.get("status") == "complete" for result in results)
        for results in stage_states.values()
    )
    broker_matrix = {arm: _broker_evidence(root, arm, stages) for arm in ("contextmesh", "full")}
    broker_reuse_refresh = all(valid for valid, _rows in broker_matrix.values()) and all(
        {"unchanged_reuse", "diff_refresh"} <= {str(row["mode"]) for row in rows}
        for _valid, rows in broker_matrix.values()
    )
    report["evidence_gate"] = {
        "equal_complete_acceptance": all(count == expected_workers for count in acceptance_counts.values()),
        "complete_stage_statuses": complete_stage_statuses,
        "valid_collected_usage": usage_valid,
        "source_policy_valid": source_policy_valid,
        "retained_worker_packets_and_local_reads": retained_worker_delivery,
        "nonraw_mcp_admission": nonraw_admission,
        "raw_contextmesh_exact_plan_pairing": plan_pairing,
        "manifest_worker_contract_parity": contract_parity,
        "broker_reuse_and_refresh_evidence": broker_reuse_refresh,
        "full_rrc_hits": len(stage_states["full"]) == len(stages) and all(
            result.get("rrc_stage_plan_key") is not None and isinstance(result.get("rrc_hit"), Mapping)
            for result in stage_states["full"]
        ),
    }
    report["manifest_evidence"] = manifests
    report["broker_dependency_evidence"] = {arm: rows for arm, (_valid, rows) in broker_matrix.items()}
    report["acceptance_counts"] = acceptance_counts
    gate = report["economy_gate"]
    report["promotable"] = all(bool(value) for value in gate.values()) and all(
        bool(value) for value in report["evidence_gate"].values()
    )
    _write_json(root / filename, report)
    return report


def _stage_cohort(from_stage: str | None) -> tuple[tuple[StageWorkload, ...], tuple[tuple[OverlapLedgerEntry, ...], ...]]:
    """Select all stages, or a safe independent append-only cohort.

    An interrupted linked stage must resume from its retained parent.  A
    product-repaired comparison instead begins at an explicitly independent
    stage so its fresh arms do not inherit a partial prior stage.
    """

    stages = staged_workloads()
    ledgers = staged_overlap_ledgers()
    if from_stage is None:
        return stages, ledgers
    try:
        start = next(index for index, stage in enumerate(stages) if stage.stage_id == from_stage)
    except StopIteration as error:
        raise ValueError(f"unknown staged cohort start: {from_stage}") from error
    if stages[start].stage_id not in INDEPENDENT_COHORT_STARTS:
        raise ValueError("an append-only cohort must begin at an independent stage")
    return stages[start:], ledgers[start:]


def _stage_paths(
    arm_root: Path, stage: StageWorkload, entries: Sequence[OverlapLedgerEntry], *, replace_product_error_ledger: bool = False
) -> tuple[Path, Path]:
    stage_root = arm_root / "stages" / stage.stage_id
    manifest = write_stage_manifest(stage_root, stage)
    ledger = stage_root / "ledger.json"
    rendered_ledger = ledger_payload(entries)
    if replace_product_error_ledger and ledger.exists():
        retained = stage_root / "ledger.product-error-pre-repair.json"
        if not retained.exists():
            shutil.copy2(ledger, retained)
        ledger.write_text(json.dumps(rendered_ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    elif not ledger.exists():
        ledger.write_text(json.dumps(rendered_ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        try:
            retained_ledger = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"retained stage ledger is unreadable: {ledger}") from error
        if retained_ledger != rendered_ledger:
            streams = tuple((stage_root / "workers").glob("*/stream.jsonl")) if (stage_root / "workers").is_dir() else ()
            if streams:
                raise RuntimeError(
                    f"retained stage ledger drift after DeepSeek launch: {stage.stage_id}; preserve streams and append a new stage"
                )
            retained = stage_root / "ledger.prelaunch-stale.json"
            if not retained.exists():
                shutil.copy2(ledger, retained)
            ledger.write_text(json.dumps(rendered_ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return stage_root, manifest


def _raw_plan_for_contextmesh(root: Path, stage: StageWorkload) -> tuple[TerraPlan, ...]:
    source = root / "raw" / "stages" / stage.stage_id / "terra" / "worktree" / ".terra-plans"
    return load_terra_plans(source, stage.plans)


def _stage_deadline(budget_path: Path, stage_id: str) -> float:
    """Return the finite monotonic deadline for one unfinished stage.

    Each stage owns a fresh budget; a retained budget only limits resumption
    when it is tagged to that same stage, so a stale or previous-stage budget
    can never truncate the next stage's fresh window.
    """

    retained = _read_json(budget_path, {})
    if (
        isinstance(retained, Mapping)
        and retained.get("stage_id") == stage_id
        and isinstance(retained.get("deadline_unix"), (int, float))
    ):
        return time.monotonic() + max(0.0, float(retained["deadline_unix"]) - time.time())
    deadline = time.monotonic() + STAGE_TIMEOUT_SECONDS
    _write_json(
        budget_path,
        {"schema_version": 1, "stage_id": stage_id, "started_unix": time.time(), "deadline_unix": time.time() + STAGE_TIMEOUT_SECONDS},
    )
    return deadline


def _run_arm(
    repo: Path,
    root: Path,
    round_id: str,
    arm: str,
    progress: dict[str, Any],
    from_stage: str | None = None,
) -> None:
    arm_root = root / arm
    baseline = arm_root / "baseline"
    stages, ledgers = _stage_cohort(from_stage)
    arm_state = progress.setdefault("arms", {}).setdefault(arm, {"stages": {}})
    _ensure_git_baseline(baseline)
    broker_process: subprocess.Popen[str] | None = None
    endpoint: dict[str, object] | None = None
    previous_source_commit: str | None = None
    previous_full_key: str | None = None
    try:
        if arm != "raw":
            admission = _mcp_admission(repo, root)
            arm_state["mcp_admission"] = admission
            if admission.get("valid") is not True:
                arm_state.update({"status": "degraded", "diagnostic": "DeepSeek MCP eligibility evidence is invalid; retained Raw may continue"})
                _write_json(arm_root / "result.json", arm_state)
                return
        for index, (stage, entries) in enumerate(zip(stages, ledgers, strict=True)):
            # Do not let a detached controller mix a newly edited runner into
            # later paid work. Its prior code and the live imports are one
            # indivisible measurement unit.
            retained_snapshot = _read_json(root / "runner-source.json", {})
            if retained_snapshot != _runner_snapshot(repo):
                arm_state.update({"status": "degraded", "diagnostic": "runner source changed before stage launch; retain this arm"})
                _write_json(arm_root / "result.json", arm_state)
                return
            state = arm_state["stages"].setdefault(stage.stage_id, {})
            prior_stage_root = arm_root / "stages" / stage.stage_id
            repair_sentinel = arm == "contextmesh" and any(
                _contextmesh_brief_unavailable(prior_stage_root / "workers" / plan.worker_id / "final.md")
                for plan in stage.plans
            )
            stage_root, manifest = _stage_paths(
                arm_root, stage, entries, replace_product_error_ledger=repair_sentinel
            )
            ledger = stage_root / "ledger.json"
            stage_commit = _run_git(baseline, "rev-parse", "HEAD")
            mass = source_mass(baseline, stage.plans)
            require_capacity(mass)
            _write_json(stage_root / "source-mass.json", mass)
            if index:
                prior = arm_state["stages"].get(stages[index - 1].stage_id, {})
                previous_source_commit = _broker_parent_commit(prior)
                if previous_source_commit is None:
                    state["status"] = "degraded"
                    state["diagnostic"] = "prior source commit is absent; retained broker cannot advance"
                    _write_json(stage_root / "result.json", state)
                    return
            # A completed degraded stage is retained evidence (typically a
            # focused-test/scaffold outcome).  It cannot become healthier by
            # replaying its DeepSeek workers, and it must not stop later append-only work.
            if state.get("status") == "complete" or (
                _completed_degraded_stage(stage_root, stage.plans, state)
            ):
                previous_source_commit = _broker_parent_commit(state, stage_commit)
                previous_full_key = str(state.get("rrc_stage_plan_key") or "") or previous_full_key
                continue
            # Each unfinished stage gets a finite fresh budget; only a retained
            # deadline tagged to this same stage limits resumption, so a stale
            # or previous-stage budget never truncates the next stage.
            deadline = _stage_deadline(arm_root / "stage-budgets" / f"{stage.stage_id}.json", stage.stage_id)
            if time.monotonic() >= deadline:
                arm_state.update({"status": "interrupted", "diagnostic": "15-minute stage deadline reached; retain and resume the unfinished stage"})
                _write_json(arm_root / "result.json", arm_state)
                return
            if arm != "raw":
                if broker_process is None:
                    broker_process, endpoint = _start_broker(repo, arm_root, baseline, stage, ledger, stage_commit, previous_source_commit, round_id)
                else:
                    assert previous_source_commit is not None
                    _advance_broker(repo, arm_root, ledger, stage, stage_commit, previous_source_commit)
            try:
                if arm == "contextmesh":
                    rendered = _raw_plan_for_contextmesh(root, stage)
                    terra_evidence: dict[str, object] = {"delivery_plan_sha256": plan_set_sha256(rendered), "terra_source_arm": "raw"}
                else:
                    rendered, terra_evidence = _run_terra(repo, arm, stage, stage_root, baseline, round_id, entries, previous_full_key, deadline)
            except (RuntimeError, OSError, ValueError) as error:
                state.update({"status": "degraded", "diagnostic": str(error), "source_commit": stage_commit, "manifest": str(manifest)})
                _write_json(stage_root / "result.json", state)
                return
            worker_outcomes = _run_deepseek_batch(
                repo,
                arm,
                stage_root,
                stage.plans,
                entries,
                rendered,
                baseline,
                endpoint,
                deadline,
                # A completed ContextMesh sentinel made no edits and is the
                # one product-error outcome that can continue its retained
                # Codex thread after the broker/ledger repair.  Ordinary test
                # failures remain completed degraded evidence.
                allow_contextmesh_sentinel_recovery=arm == "contextmesh",
            )
            delivery_valid, delivery_evidence = _worker_delivery_evidence(
                arm, stage_root, stage.plans, entries
            )
            if arm != "raw":
                events = []
                log_path = arm_root / "broker" / "events.jsonl"
                if log_path.is_file():
                    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                if any(isinstance(event, Mapping) and event.get("event") == "brief_missing" for event in events):
                    state.update({"status": "blocking_product_error", "diagnostic": "ContextMesh brief_missing; no peer raw fallback is permitted", "source_commit": stage_commit})
                    _write_json(stage_root / "result.json", state)
                    return
            violations = _source_policy_violations(arm, stage_root, stage.plans, entries)
            _write_json(stage_root / "source-policy.json", {"valid": not violations, "violations": violations})
            acceptance, copied = _accept_and_merge(
                repo,
                stage_root,
                baseline,
                stage.plans,
                source_policy_valid=not violations and delivery_valid,
            )
            all_accepted = all(item.get("accepted") is True for item in acceptance)
            boundary_commit = (
                _stage_commit(baseline, f"{arm} {stage.stage_id} accepted changes", copied)
                if all_accepted
                else stage_commit
            )
            boundary_evidence = _retain_boundary_evidence(
                baseline,
                stage_root,
                source_commit=stage_commit,
                boundary_commit=boundary_commit,
                paths=copied,
            )
            state.update(
                {
                    "status": "complete" if all_accepted else "degraded",
                    "manifest": str(manifest), "source_commit": stage_commit, "boundary_commit": boundary_commit,
                    "delivery_plan_sha256": terra_evidence["delivery_plan_sha256"], "worker_outcomes": worker_outcomes,
                    "acceptance": acceptance, "merged_paths": copied,
                    "source_policy": {"valid": not violations, "violations": violations},
                    "worker_delivery_evidence": {"valid": delivery_valid, "workers": delivery_evidence},
                    "boundary": boundary_evidence,
                    **terra_evidence,
                }
            )
            if all_accepted:
                state.pop("diagnostic", None)
            _write_json(stage_root / "result.json", state)
            _write_json(root / "progress.json", progress)
            # The broker's live lineage is the immutable source revision that
            # workers read, not the post-merge boundary that becomes the next
            # stage's source revision.
            previous_source_commit = stage_commit
            previous_full_key = str(terra_evidence.get("rrc_stage_plan_key") or "") or previous_full_key
    finally:
        _stop_broker(broker_process)


def run_staged_round(
    repo_root: str | Path, round_id: str, metrics_root: str | Path = "metrics", *, arms: Sequence[str] = ARMS,
    from_stage: str | None = None,
) -> dict[str, object]:
    """Execute/resume the retained staged comparison exactly once per session.

    A completed JSONL stream is never replayed.  An interrupted stream with a
    retained thread id is continued with ``codex exec resume`` and appended to
    that same stream.  Test or launcher failures are retained as degraded
    evidence; only a ContextMesh broker/RRC state failure halts that arm.
    """

    repo = Path(repo_root).resolve()
    metrics = (repo / metrics_root).resolve() if not Path(metrics_root).is_absolute() else Path(metrics_root).resolve()
    root = metrics / round_id / "staged-codex"
    cohort, _ = _stage_cohort(from_stage)
    expected_cohort = {
        "schema_version": 1,
        "start_stage": cohort[0].stage_id,
        "standalone_append_only_cohort": from_stage is not None,
        "lineage_mode": "fresh_independent_baseline" if from_stage is not None else "linked_from_stage_01",
    }
    if not (root / "run-plan.json").is_file():
        prepare_staged_round(repo, round_id, metrics, from_stage=from_stage)
    retained_cohort = _read_json(root / "cohort.json", None)
    if retained_cohort != expected_cohort:
        raise RuntimeError(
            "retained staged cohort lineage does not match --from-stage; start a fresh independent round instead of mixing baselines"
        )
    lease_path, lease_token = _controller_lease(root)
    try:
        retained_snapshot = _read_json(root / "runner-source.json", {})
        current_snapshot = _runner_snapshot(repo)
        if retained_snapshot != current_snapshot:
            _write_json(root / "runner-source-recovery.json", {"prior": retained_snapshot, "current": current_snapshot})
            raise RuntimeError("runner source changed after staged round preparation; prepare a fresh round instead of mixing execution code")
        progress = _read_json(root / "progress.json", {"schema_version": 1, "round_id": round_id, "arms": {}})
        if not isinstance(progress, dict):
            raise RuntimeError("retained staged progress is malformed")
        _rehydrate_progress(root, progress)
        selected = tuple(arms)
        if not selected or any(arm not in ARMS for arm in selected):
            raise ValueError("arms must be a non-empty subset of raw, contextmesh, full")
        for arm in selected:
            _run_arm(repo, root, round_id, arm, progress, from_stage)
            _write_json(root / "progress.json", progress)
        if set(selected) == set(ARMS):
            filename = "report.json" if from_stage is None else f"report-{from_stage}-onward.json"
            return _write_report(root, round_id, cohort, filename=filename)
        return {"round_id": round_id, "selected_arms": list(selected), "progress": progress}
    finally:
        _release_controller_lease(lease_path, lease_token)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("round_id")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--metrics-root", default="metrics")
    parser.add_argument("--run", action="store_true", help="spend only by explicitly starting/resuming the retained Codex cohort")
    parser.add_argument("--from-stage", help="run a fresh independent append-only cohort beginning at this stage")
    args = parser.parse_args(argv)
    result = run_staged_round(
        args.repo_root, args.round_id, args.metrics_root, from_stage=args.from_stage,
    ) if args.run else prepare_staged_round(args.repo_root, args.round_id, args.metrics_root, from_stage=args.from_stage)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual pre-paid preparation entry point.
    raise SystemExit(main())
