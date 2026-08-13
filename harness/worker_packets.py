"""Render the four frozen worker packets without copying a common task prompt."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from harness.four_worker_plan import OverlapLedgerEntry, TerraPlan, WorkerPlan, fixture_terra_plan


def _ledger_entry(entry: OverlapLedgerEntry) -> dict[str, object]:
    """Expose only broker routing identity; the complete ledger is audit-only."""

    return {
        "brief_id": entry.brief_id,
        "canonical_path": entry.canonical_path,
        "requirements_hash": entry.requirements_hash,
    }


def render_worker_packet(
    arm: str,
    plan: WorkerPlan,
    ledger: Iterable[OverlapLedgerEntry],
    coordinator_plan: TerraPlan | None = None,
    prefetched_briefs: dict[str, dict[str, object]] | None = None,
    *,
    delivery_worktree: str | None = None,
) -> dict[str, object]:
    """Return one Terra-produced packet plus only its naturally relevant overlaps."""

    if arm not in {"raw", "contextmesh", "full"}:
        raise ValueError(f"unknown arm {arm!r}")
    coordinator_plan = coordinator_plan or fixture_terra_plan(plan)
    if coordinator_plan.task_id != plan.task_id or coordinator_plan.worker_id != plan.worker_id:
        raise ValueError("coordinator plan does not belong to this worker contract")
    prefetched_briefs = prefetched_briefs or {}
    all_related = [
        (entry, _ledger_entry(entry))
        for entry in ledger
        if plan.worker_id == entry.source_owner or plan.worker_id in entry.peer_workers
    ]
    related = [(entry, packet_entry) for entry, packet_entry in all_related if entry.brief_id not in prefetched_briefs]
    overlap_paths = {entry.canonical_path for entry, _ in all_related}
    packet: dict[str, object] = {
        "task_id": plan.task_id,
        "worker_id": plan.worker_id,
        "worktree": delivery_worktree or plan.worktree,
        "branch": plan.branch,
        "objective": plan.objective,
        "owned_write_paths": list(plan.owned_write_paths),
        "acceptance_cmd": plan.acceptance_cmd,
        "task_requirements": list(plan.task_requirements),
        "coordinator_plan": {
            "plan_steps": list(coordinator_plan.plan_steps),
            "source_facts": list(coordinator_plan.source_facts),
        },
    }
    if arm == "raw":
        packet["initial_read_paths"] = list(plan.initial_read_paths)
    else:
        # The broker retains and validates the complete five-field owner
        # brief. It grants a sealed worker only the exact fact projection that
        # this worker's source contract needs. That keeps the packet complete
        # for implementation without repeating unrelated brief prose or audit
        # anchors to every DeepSeek worker.
        worker_briefs: dict[str, list[str]] = {}
        for entry, _packet_entry in all_related:
            value = prefetched_briefs.get(entry.brief_id)
            facts = value.get("facts") if isinstance(value, dict) else None
            if isinstance(facts, list) and all(isinstance(item, str) for item in facts) and facts:
                worker_briefs[entry.canonical_path] = facts
        if worker_briefs:
            packet["contextmesh_prefetched_briefs"] = worker_briefs
        owner_overlaps = [packet_entry for entry, packet_entry in related if entry.source_owner == plan.worker_id]
        peer_overlaps = [packet_entry for entry, packet_entry in related if entry.source_owner != plan.worker_id]
        if owner_overlaps or peer_overlaps:
            packet["contextmesh_owner_overlaps"] = owner_overlaps
            packet["contextmesh_peer_overlaps"] = peer_overlaps
        else:
            packet["contextmesh_sealed"] = True
        packet["local_read_paths"] = [path for path in plan.initial_read_paths if path not in overlap_paths]
    return packet


def worker_prompt(
    arm: str,
    plan: WorkerPlan,
    ledger: Iterable[OverlapLedgerEntry],
    coordinator_plan: TerraPlan | None = None,
    prefetched_briefs: dict[str, dict[str, object]] | None = None,
) -> str:
    """Explain one concrete task's direct-source or ContextMesh policy."""

    packet = render_worker_packet(arm, plan, ledger, coordinator_plan, prefetched_briefs)
    return worker_prompt_from_packet(arm, packet)


def worker_prompt_from_packet(arm: str, packet: Mapping[str, object]) -> str:
    """Render a prompt from the exact packet retained for the worker.

    The staged controller writes this payload before starting a worker and uses
    this helper rather than rendering a second packet.  That makes the packet
    beside a completed stream the delivered artifact, not a later proof
    reconstruction.
    """

    if arm not in {"raw", "contextmesh", "full"}:
        raise ValueError(f"unknown arm {arm!r}")
    if not isinstance(packet.get("task_id"), str) or not isinstance(packet.get("worker_id"), str):
        raise ValueError("worker packet lacks task_id or worker_id")
    common = (
        "Work only on this task in the current isolated worktree. The coordinator_plan below is your actual "
        "retained plan: follow it; do not recreate the repository plan or explore for alternative patterns. "
        "Use apply_patch for edits and run only the named acceptance command. Do not delegate, read parent "
        "directories, inspect unrelated source, or read/disassemble .pyc runtime files. The worker view intentionally "
        "has no skills or optional guidance directory: do not try to read one; begin the task now.\n\n"
    )
    if arm == "raw":
        policy = (
            "Before editing, directly inspect each packet.initial_read_paths once, then use those files and the coordinator plan. "
            "No other source path is available or permitted; no ContextMesh broker is configured.\n\n"
        )
    else:
        sealed = bool(packet.get("contextmesh_sealed"))
        policy = (
            "Default: packet + broker facts are sufficient. Implement only owned_write_paths; do not browse/list/search/read for orientation, conventions, layout, reassurance, generated files, or tests. "
            "Before any repository inspection—read_file, list_directory, glob, grep, or a shell command that reads/searches/lists files—emit exactly one standalone concise SOURCE_NEEDED line: "
            "SOURCE_NEEDED: <exact minimum target>; missing: <specific fact/decision>; why packet/broker cannot answer: <reason>. "
            "It authorizes one smallest inspection only, including any path in local_read_paths. SOURCE_NEEDED cannot authorize reading any brokered/overlap path, including via baseline/relative path; "
            "the packet or prefetched brief is final for those overlaps even if a fact appears missing, and such a gap must not be filled by raw read. "
            "Broker calls are not repository reads and need no SOURCE_NEEDED; never raw-read or re-fetch overlap paths. "
            "Prompt guidance only, no runtime enforcement: list_directory, glob, grep, and all discovery tools stay present, enabled, unblocked, and un-intercepted. "
            "Run the named acceptance command once after edits; rerun only after an owned-file edit fixes its failure. Do not narrate, deliberate, or restate this policy."
        )
        if sealed:
            policy += "All overlaps are sealed. No ContextMesh MCP tool is configured.\n\n"
        else:
            policy += (
                "Process every owner id in packet order, one at a time: contextmesh/claim_source for one id; read every returned chunk; for a raw/diff "
                "claim, author and publish its five terse required-fact fields via contextmesh/publish_file_brief before claiming the next id. Author "
                "from that claim's brief_template: include every required_source_fact verbatim and keep the total under max_summary_bytes on the first "
                "publish; an unchanged reuse needs no publication. After every owner id is published or reused, if contextmesh_peer_overlaps is "
                "nonempty, exactly one contextmesh/get_file_briefs for those peer ids is mandatory, even when other paths are prefetched; prefetched "
                "facts never replace it. For policy_catalog, the exact profile binding is in coordinator_plan: brief only catalog API plus required "
                "anchors, never expanded profile bodies. Never direct-read an overlap or use fallback. A rejected owner brief gets one same-claim "
                "repair; otherwise return CONTEXTMESH_BRIEF_UNAVAILABLE without edits.\n\n"
            )
        policy += "Implement the task now, then run the named acceptance command and report the result.\n\n"
    return common + policy + json.dumps(dict(packet), indent=2, sort_keys=True) + "\n"


__all__ = ["render_worker_packet", "worker_prompt", "worker_prompt_from_packet"]
