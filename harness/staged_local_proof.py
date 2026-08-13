"""No-provider three-stage proof bundle for the ContextMesh and RRCv2 workflow."""

from __future__ import annotations

import asyncio
import argparse
import hashlib
import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from contextmesh.bench.rrc_long_spec_demo import materialize
from contextmesh.mcp.broker_service import ledger_payload
from contextmesh.mcp.shared_broker import BrokerError, SharedBriefBroker
from harness.four_worker_plan import OverlapLedgerEntry, manifest_sha256
from harness.staged_workload import StageWorkload, staged_overlap_ledgers, staged_workloads, write_stage_manifest
from harness.terra_plans import record_rrc_stage_plan, render_rrc_plans, render_rrc_stage_delta_plans
from harness.workload_preflight import require_capacity, source_mass


class StagedProofError(RuntimeError):
    """The local proof cannot demonstrate the required staged topology."""


def _run_git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise StagedProofError(completed.stderr.strip() or f"git {' '.join(arguments)} failed")
    return completed.stdout.strip()


def _commit(root: Path, message: str, paths: Sequence[str] | None = None) -> str:
    _run_git(root, "add", "--", *(paths or ("ruleforge", "tests")))
    _run_git(root, "-c", "user.name=RuleForge staged proof", "-c", "user.email=staged-proof@example.test", "commit", "-m", message)
    return _run_git(root, "rev-parse", "HEAD")


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dependencies(entries: Sequence[OverlapLedgerEntry], source_root: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for entry in entries:
        facts_hash = hashlib.sha256(
            json.dumps(entry.required_facts, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        rows.append(
            {
                "lineage_id": f"main:{entry.canonical_path}",
                "content_sha256": _source_hash(source_root / entry.canonical_path),
                "brief_facts_hash": facts_hash,
            }
        )
    return sorted(rows, key=lambda row: row["lineage_id"])


def _brief(entry: OverlapLedgerEntry) -> dict[str, str]:
    facts = "; ".join(entry.required_facts)
    return {
        "purpose_and_api": "RuleForge source contract used by the assigned policy task.",
        "data_and_dependencies": "Declared RuleForge data and collaborators remain source-bound.",
        "behaviour_and_failures": "Use the declared evaluator and rule contract; preserve failures.",
        "plan_step_facts": facts,
        "anchors": "module contract and selected source facts",
    }


async def _complete_stage_briefs(broker: SharedBriefBroker, entries: Sequence[OverlapLedgerEntry]) -> dict[str, str]:
    """Complete owner claims in owner-first order; reused revisions need only a peer read."""

    modes: dict[str, str] = {}
    for entry in entries:
        try:
            claim = await broker.claim_source(entry.brief_id, entry.source_owner)
        except BrokerError as error:
            if error.code != "already_claimed":
                raise
            peer = entry.peer_workers[0]
            await broker.get_file_brief(entry.brief_id, peer)
            modes[entry.canonical_path] = "unchanged_reuse"
            continue
        if claim.claim_kind == "unchanged_reuse":
            # The owner receives the complete retained brief in its claim;
            # only the peer still needs a brief read in this local simulator.
            peer = entry.peer_workers[0]
            await broker.get_file_brief(entry.brief_id, peer)
            modes[entry.canonical_path] = claim.claim_kind
            continue
        if claim.claim_kind == "raw" or claim.claim_kind == "invalidated_raw":
            for index in range(1, claim.source_chunk_count):
                await broker.read_source_chunk(entry.brief_id, entry.source_owner, index)
        await broker.publish_file_brief(entry.brief_id, entry.source_owner, claim.source_hash, _brief(entry))
        peer = entry.peer_workers[0]
        await broker.get_file_brief(entry.brief_id, peer)
        modes[entry.canonical_path] = claim.claim_kind
    return modes


def _copy_stage_cache(root: Path, workspace: Path, stage: StageWorkload) -> None:
    """Refresh only RRC's local bindings; the stage repository is never rematerialized."""

    cache_source = root / "cache-source" / stage.stage_id
    materialize(cache_source, stage.cohort, stage.stage_id)
    source_cache = cache_source / "workspace" / ".rrc-cache"
    destination = workspace / ".rrc-cache"
    shutil.copytree(source_cache, destination, dirs_exist_ok=True)


def _advance_rollout(workspace: Path, stage: StageWorkload) -> str:
    if stage.next_rollout_revision is None:
        raise StagedProofError("the final stage has no successor rollout revision")
    path = workspace / "ruleforge" / "rollout.py"
    source = path.read_text(encoding="utf-8")
    expected = f'STAGE_REVISION = "{stage.required_rollout_revision}"'
    replacement = f'STAGE_REVISION = "{stage.next_rollout_revision}"'
    if expected not in source:
        raise StagedProofError(f"rollout source does not contain {expected}")
    path.write_text(source.replace(expected, replacement, 1), encoding="utf-8")
    return _commit(workspace, f"accept {stage.stage_id} owned rollout change", ("ruleforge/rollout.py",))


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_local_three_stage_proof(output: str | Path) -> dict[str, Any]:
    """Build retained, no-provider evidence for all staged state transitions."""

    root = Path(output).resolve()
    if root.exists():
        raise FileExistsError(f"local staged proof already exists: {root}")
    root.mkdir(parents=True)
    # The active paid cohort is the fresh, linked stage-35/36/37 sequence.
    # Earlier append-only work belongs to its own retained evidence and must
    # not leak a predecessor workspace, broker, or RRC state into this proof.
    stages = staged_workloads()[-3:]
    ledgers = staged_overlap_ledgers()[-3:]
    first = stages[0]
    materialize(root / "materialized", first.cohort, first.stage_id)
    workspace = root / "materialized" / "workspace"
    _run_git(workspace, "init")
    baseline_commit = _commit(workspace, "staged RuleForge baseline")
    state_dir = root / "broker-state"
    broker_log = root / "broker-events.jsonl"
    stage_reports: list[dict[str, Any]] = []
    source_masses: list[dict[str, Any]] = []
    rrc_parent_key: str | None = None
    prior_stage_source_commit: str | None = None
    broker: SharedBriefBroker | None = None

    for index, stage in enumerate(stages):
        manifests = root / "manifests"
        manifest_path = write_stage_manifest(manifests, stage)
        if index:
            _copy_stage_cache(root, workspace, stage)
        entries = ledgers[index]
        mass = source_mass(workspace, stage.plans)
        require_capacity(mass)
        source_masses.append(mass)
        _write_json(root / "source-mass" / f"{stage.stage_id}.json", mass)
        ledger_path = root / "ledgers" / f"{stage.stage_id}.json"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(json.dumps(ledger_payload(entries), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        stage_commit = _run_git(workspace, "rev-parse", "HEAD")
        if broker is None:
            broker = SharedBriefBroker(
                workspace,
                entries,
                state_dir=state_dir,
                log_path=broker_log,
                workflow_id="ruleforge-local-three-stage-proof/full",
                stage_id=stage.stage_id,
                stage_commit=stage_commit,
                git_root=workspace,
            )
        else:
            assert prior_stage_source_commit is not None
            broker.advance_stage(
                entries,
                stage_id=stage.stage_id,
                stage_commit=stage_commit,
                parent_stage_commit=prior_stage_source_commit,
            )
        modes = asyncio.run(_complete_stage_briefs(broker, entries))
        dependencies = _dependencies(entries, workspace)
        plan_root = root / "terra-plans" / stage.stage_id
        if index == 0:
            rendered = render_rrc_plans(workspace, plan_root, stage.plans)
            state = record_rrc_stage_plan(
                workspace,
                stage.plans,
                workflow_id="ruleforge-local-three-stage-proof",
                arm="full",
                stage_id=stage.stage_id,
                parent_stage_key=None,
                template_version="ruleforge-template/v1",
                template_external_ref="measured-rrc-hit",
                dependency_revisions=dependencies,
                rendered=rendered,
                output=root / "rrc" / f"{stage.stage_id}-state.json",
            )
            hit: Mapping[str, Any] | None = None
        else:
            rendered, hit, state = render_rrc_stage_delta_plans(
                workspace,
                plan_root,
                stage.plans,
                workflow_id="ruleforge-local-three-stage-proof",
                arm="full",
                stage_id=stage.stage_id,
                parent_stage_key=str(rrc_parent_key),
                template_version="ruleforge-template/v1",
                dependency_revisions=dependencies,
                evidence_output=root / "rrc" / f"{stage.stage_id}-state.json",
            )
            _write_json(root / "rrc" / f"{stage.stage_id}-hit.json", hit)
        rrc_parent_key = str(state["stage_plan_key"])
        stage_report: dict[str, Any] = {
            "stage_id": stage.stage_id,
            "manifest": str(manifest_path),
            "ledger": str(ledger_path),
            "commit": stage_commit,
            "parent_commit": prior_stage_source_commit,
            "brief_modes": modes,
            "rrc_stage_plan_key": rrc_parent_key,
            "rrc_reconstruction_reads": None if hit is None else hit["reconstruction_reads"],
            "worker_contract_sha256": manifest_sha256(stage.plans),
            "delivery_plan_count": len(rendered),
        }
        if stage.next_rollout_revision is not None:
            boundary_commit = _advance_rollout(workspace, stage)
            stage_report["boundary_commit"] = boundary_commit
            stage_report["rollout_sha256"] = _source_hash(workspace / "ruleforge" / "rollout.py")
        prior_stage_source_commit = stage_commit
        stage_reports.append(stage_report)

    assert broker is not None
    event_names = [str(row["event"]) for row in broker.events]
    report = {
        "schema_version": 1,
        "workflow_id": "ruleforge-local-three-stage-proof/full",
        "cohort": {
            "start_stage": first.stage_id,
            "stage_ids": [stage.stage_id for stage in stages],
            "lineage_mode": "fresh_independent_baseline",
        },
        "baseline_commit": baseline_commit,
        "stages": stage_reports,
        "broker_events": str(broker_log),
        "broker_event_counts": {name: event_names.count(name) for name in sorted(set(event_names))},
        "source_mass": {
            "stages": source_masses,
            "cumulative_duplicate_source_bytes": sum(int(mass["duplicate_source_bytes"]) for mass in source_masses),
        },
        "proof": {
            "later_unchanged_reuse": event_names.count("brief_reused_unchanged") > 0,
            "later_diff_refresh": event_names.count("brief_refresh_diff") > 0,
            "later_rrc_hits_zero_reconstruction": all(
                stage["rrc_reconstruction_reads"] == 0 for stage in stage_reports[1:]
            ),
        },
    }
    _write_json(root / "report.json", report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run_local_three_stage_proof(args.output), sort_keys=True))
    return 0


__all__ = ["StagedProofError", "run_local_three_stage_proof"]


if __name__ == "__main__":  # pragma: no cover - manual pre-compute gate.
    raise SystemExit(main())
