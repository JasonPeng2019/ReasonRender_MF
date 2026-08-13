from __future__ import annotations

import json
from typing import Any, cast

from harness.four_worker_plan import build_overlap_ledger, freeze_worker_plans, manifest_sha256
from harness.worker_packets import render_worker_packet, worker_prompt, worker_prompt_from_packet


def _plans_and_ledger():
    plans = freeze_worker_plans()
    return plans, build_overlap_ledger(plans, manifest_sha256(plans))


def test_workers_receive_distinct_packets_with_only_their_own_overlaps() -> None:
    plans, ledger = _plans_and_ledger()
    packets: list[dict[str, Any]] = [
        cast(dict[str, Any], render_worker_packet("contextmesh", plan, ledger)) for plan in plans
    ]

    assert len({packet["task_id"] for packet in packets}) == 4
    assert len({tuple(packet["owned_write_paths"]) for packet in packets}) == 4
    assert [entry["canonical_path"] for entry in packets[0]["contextmesh_owner_overlaps"]] == ["ruleforge/domain.py"]
    assert [entry["canonical_path"] for entry in packets[0]["contextmesh_peer_overlaps"]] == ["ruleforge/evaluator.py", "ruleforge/policy_catalog.py"]
    assert [entry["canonical_path"] for entry in packets[1]["contextmesh_owner_overlaps"]] == ["ruleforge/evaluator.py"]
    assert [entry["canonical_path"] for entry in packets[1]["contextmesh_peer_overlaps"]] == ["ruleforge/domain.py", "ruleforge/policy_catalog.py", "ruleforge/registry.py"]
    assert [entry["canonical_path"] for entry in packets[2]["contextmesh_owner_overlaps"]] == ["ruleforge/registry.py", "ruleforge/rules/base.py"]
    assert [entry["canonical_path"] for entry in packets[2]["contextmesh_peer_overlaps"]] == ["ruleforge/evaluator.py"]
    assert [entry["canonical_path"] for entry in packets[3]["contextmesh_owner_overlaps"]] == ["ruleforge/policy_catalog.py"]
    assert [entry["canonical_path"] for entry in packets[3]["contextmesh_peer_overlaps"]] == [
        "ruleforge/domain.py",
        "ruleforge/registry.py",
        "ruleforge/rules/base.py",
    ]
    assert all(
        "plan_steps" not in entry
        for packet in packets
        for entry in (*packet["contextmesh_owner_overlaps"], *packet["contextmesh_peer_overlaps"])
    )


def test_raw_packet_has_no_broker_material_and_full_packet_is_resolved() -> None:
    plans, ledger = _plans_and_ledger()
    raw = cast(dict[str, Any], render_worker_packet("raw", plans[0], ledger))
    full = cast(dict[str, Any], render_worker_packet("full", plans[0], ledger))

    assert "contextmesh_owner_overlaps" not in raw
    assert "contextmesh_peer_overlaps" not in raw
    assert raw["worktree"] == plans[0].worktree
    assert raw["initial_read_paths"] == ["ruleforge/domain.py", "ruleforge/evaluator.py", "ruleforge/policy_catalog.py"]
    assert full["coordinator_plan"]["plan_steps"]
    assert "slot_values" not in full
    assert "initial_read_paths" not in full
    assert full["local_read_paths"] == []
    prompt = worker_prompt("full", plans[0], ledger)
    assert "contextmesh/claim_source" in prompt
    assert "contextmesh/publish_file_brief" in prompt
    assert "before claiming the next id" in prompt
    assert "required-fact" in prompt
    assert "peer_step_requirements" not in prompt
    assert "contextmesh_owner_overlaps" in prompt
    assert "contextmesh_peer_overlaps" in prompt
    assert "CONTEXTMESH_BRIEF_UNAVAILABLE" in prompt
    assert "Default: packet + broker facts are sufficient" in prompt
    assert "exact profile binding is in coordinator_plan" in prompt
    assert "same-claim repair" in prompt
    worker_two_prompt = worker_prompt("full", plans[1], ledger)
    assert "approved_market" not in worker_two_prompt
    assert "test_markets_rule.py" not in worker_two_prompt


def test_owner_prompt_requires_verbatim_facts_within_the_first_publish_budget() -> None:
    plans, ledger = _plans_and_ledger()
    prompt = worker_prompt("contextmesh", plans[0], ledger)

    assert "include every required_source_fact verbatim" in prompt
    assert "keep the total under max_summary_bytes on the first publish" in prompt
    assert "brief_template" in prompt
    assert "one same-claim repair" in prompt


def test_non_raw_prompts_require_source_needed_justification_before_extra_inspection() -> None:
    plans, ledger = _plans_and_ledger()

    for arm in ("contextmesh", "full"):
        prompt = worker_prompt(arm, plans[0], ledger)

        assert "SOURCE_NEEDED" in prompt
        assert "exact minimum target" in prompt
        assert "specific fact/decision" in prompt
        assert "why packet/broker cannot answer" in prompt
        assert "local_read_paths" in prompt
        assert "read_file, list_directory, glob, grep" in prompt
        assert "never raw-read or re-fetch overlap paths" in prompt
        assert "Implement the task now, then run the named acceptance command and report the result" in prompt


def test_non_raw_prompts_forbid_source_needed_from_authorizing_overlap_reads() -> None:
    plans, ledger = _plans_and_ledger()

    for arm in ("contextmesh", "full"):
        prompt = worker_prompt(arm, plans[0], ledger)

        assert "SOURCE_NEEDED cannot authorize reading any brokered/overlap path" in prompt
        assert "including via baseline/relative path" in prompt
        assert "the packet or prefetched brief is final for those overlaps even if a fact appears missing" in prompt
        assert "such a gap must not be filled by raw read" in prompt


def test_non_raw_prompts_default_to_zero_extra_source_inspection() -> None:
    plans, ledger = _plans_and_ledger()

    for arm in ("contextmesh", "full"):
        prompt = worker_prompt(arm, plans[0], ledger)

        assert "Default: packet + broker facts are sufficient" in prompt
        assert "Implement only owned_write_paths" in prompt
        assert "do not browse/list/search/read for orientation, conventions, layout, reassurance, generated files, or tests" in prompt


def test_non_raw_prompts_require_one_justification_per_lookup_and_prompt_return() -> None:
    plans, ledger = _plans_and_ledger()

    for arm in ("contextmesh", "full"):
        prompt = worker_prompt(arm, plans[0], ledger)

        assert "standalone concise SOURCE_NEEDED" in prompt
        assert "including any path in local_read_paths" in prompt
        assert "It authorizes one smallest inspection only" in prompt


def test_non_raw_prompts_keep_discovery_tools_available_without_runtime_enforcement() -> None:
    plans, ledger = _plans_and_ledger()

    for arm in ("contextmesh", "full"):
        prompt = worker_prompt(arm, plans[0], ledger)

        assert "Prompt guidance only, no runtime enforcement" in prompt
        assert "list_directory, glob, grep, and all discovery tools stay present, enabled, unblocked, and un-intercepted" in prompt
        assert "nothing removes, deactivates, intercepts, or blocks a tool call" not in prompt
        assert "hard-block" not in prompt
        assert "disable" not in prompt


def test_non_raw_prompts_do_not_auto_inspect_local_read_paths() -> None:
    plans, ledger = _plans_and_ledger()

    for arm in ("contextmesh", "full"):
        prompt = worker_prompt(arm, plans[0], ledger)

        assert "Inspect local_read_paths once" not in prompt
        assert "inspect local_read_paths once" not in prompt
        assert "inspect each packet.initial_read_paths once" not in prompt
        assert "Before any repository inspection" in prompt
        assert "a shell command that reads/searches/lists files" in prompt
        assert "emit exactly one standalone concise SOURCE_NEEDED line" in prompt


def test_non_raw_prompts_exempt_broker_tool_calls_from_source_needed() -> None:
    plans, ledger = _plans_and_ledger()

    for arm in ("contextmesh", "full"):
        prompt = worker_prompt(arm, plans[0], ledger)

        assert "Broker calls are not repository reads" in prompt
        assert "need no SOURCE_NEEDED" in prompt


def test_non_raw_prompts_require_peer_get_file_briefs_even_when_other_paths_are_prefetched() -> None:
    plans, ledger = _plans_and_ledger()

    for arm in ("contextmesh", "full"):
        prompt = worker_prompt(arm, plans[0], ledger)

        assert "if contextmesh_peer_overlaps is nonempty" in prompt
        assert "exactly one contextmesh/get_file_briefs for those peer ids" in prompt
        assert "is mandatory, even when other paths are prefetched" in prompt
        assert "prefetched facts never replace it" in prompt


def test_non_raw_prompts_prescribe_one_source_justification_line_template() -> None:
    plans, ledger = _plans_and_ledger()

    for arm in ("contextmesh", "full"):
        prompt = worker_prompt(arm, plans[0], ledger)

        assert "SOURCE_NEEDED: <exact minimum target>; missing: <specific fact/decision>; why packet/broker cannot answer: <reason>" in prompt
        assert "emit exactly one standalone concise SOURCE_NEEDED line" in prompt
        assert "It authorizes one smallest inspection only" in prompt


def test_non_raw_prompts_forbid_narrating_or_restating_policy() -> None:
    plans, ledger = _plans_and_ledger()

    for arm in ("contextmesh", "full"):
        prompt = worker_prompt(arm, plans[0], ledger)

        assert "Do not narrate, deliberate, or restate this policy" in prompt
        assert "Run the named acceptance command once after edits" in prompt
        assert "act and run acceptance after obligations" not in prompt


def test_raw_prompt_has_no_source_needed_policy() -> None:
    plans, ledger = _plans_and_ledger()

    prompt = worker_prompt("raw", plans[0], ledger)

    assert "SOURCE_NEEDED" not in prompt
    assert "Default: packet + broker facts are sufficient" not in prompt
    assert "zero extra source inspection" not in prompt


def test_raw_prompt_preserves_direct_inspection_behavior() -> None:
    plans, ledger = _plans_and_ledger()

    prompt = worker_prompt("raw", plans[0], ledger)

    assert "inspect each packet.initial_read_paths once" in prompt
    assert "No other source path is available or permitted" in prompt
    assert "no ContextMesh broker is configured" in prompt
    assert "contextmesh/claim_source" not in prompt
    assert "contextmesh_prefetched_briefs" not in prompt


def test_delivered_packet_uses_the_exact_runtime_worktree() -> None:
    plans, ledger = _plans_and_ledger()

    packet = render_worker_packet(
        "raw",
        plans[0],
        ledger,
        delivery_worktree=r"C:\retained\stage-35\worktrees\worker-01",
    )

    assert packet["worktree"] == r"C:\retained\stage-35\worktrees\worker-01"


def test_prefetched_contextmesh_brief_removes_that_overlap_from_model_tool_routing() -> None:
    plans, ledger = _plans_and_ledger()
    entry = next(item for item in ledger if item.canonical_path == "ruleforge/domain.py")
    prefetched = {entry.brief_id: {"brief_id": entry.brief_id, "facts": ["api", "data"]}}

    packet = cast(dict[str, Any], render_worker_packet("contextmesh", plans[0], ledger, prefetched_briefs=prefetched))

    assert packet["contextmesh_prefetched_briefs"] == {entry.canonical_path: ["api", "data"]}
    assert all(item["brief_id"] != entry.brief_id for item in packet["contextmesh_owner_overlaps"])
    assert entry.canonical_path not in packet["local_read_paths"]


def test_fully_prefetched_packet_forbids_a_redundant_broker_call() -> None:
    plans, ledger = _plans_and_ledger()
    prefetched = {
        entry.brief_id: {"brief_id": entry.brief_id, "facts": ["api", "data"]}
        for entry in ledger
        if plans[0].worker_id == entry.source_owner or plans[0].worker_id in entry.peer_workers
    }

    prompt = worker_prompt("contextmesh", plans[0], ledger, prefetched_briefs=prefetched)

    assert "All overlaps are sealed. No ContextMesh MCP tool is configured." in prompt
    assert "prepare_task_context once" not in prompt
    assert "contextmesh_sealed" in prompt
    assert "contextmesh_owner_overlaps" not in prompt


def test_retained_packet_can_be_rendered_without_recreating_it() -> None:
    plans, ledger = _plans_and_ledger()
    packet = render_worker_packet("raw", plans[0], ledger)

    prompt = worker_prompt_from_packet("raw", packet)

    assert json.dumps(packet, indent=2, sort_keys=True) in prompt
