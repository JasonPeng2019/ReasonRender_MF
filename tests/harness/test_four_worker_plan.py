from __future__ import annotations

from dataclasses import replace

import pytest
from harness.four_worker_plan import (
    WorkerPlan,
    build_overlap_ledger,
    fixture_terra_plan,
    freeze_worker_plans,
    manifest_sha256,
)


def test_freezes_four_distinct_workers_with_disjoint_write_ownership() -> None:
    plans = freeze_worker_plans()

    assert len(plans) == 4
    assert len({plan.task_id for plan in plans}) == 4
    assert len({plan.worker_id for plan in plans}) == 4
    assert len({plan.worktree for plan in plans}) == 4
    assert len({plan.branch for plan in plans}) == 4
    assert len({plan.objective for plan in plans}) == 4
    assert all(plan.task_requirements for plan in plans)
    assert all(plan.plan_fact_requirements for plan in plans)
    assert all(plan.source_fact_requirements != plan.plan_fact_requirements for plan in plans)
    assert len({path for plan in plans for path in plan.owned_write_paths}) == 8
    assert all(isinstance(plan.owned_write_paths, tuple) for plan in plans)
    with pytest.raises((AttributeError, TypeError)):
        plans[0].worker_id = "changed"  # type: ignore[misc]


def test_overlap_ledger_contains_only_the_naturally_shared_read_paths() -> None:
    plans = freeze_worker_plans()
    ledger = build_overlap_ledger(plans, manifest_sha256(plans))

    assert {entry.canonical_path for entry in ledger} == {
        "ruleforge/domain.py",
        "ruleforge/evaluator.py",
        "ruleforge/policy_catalog.py",
        "ruleforge/registry.py",
        "ruleforge/rules/base.py",
    }
    assert "ruleforge/errors.py" not in {entry.canonical_path for entry in ledger}
    assert "ruleforge/service.py" not in {entry.canonical_path for entry in ledger}


def test_overlap_owner_and_peers_are_stably_balanced_across_participants() -> None:
    plans = freeze_worker_plans()
    entries = {
        entry.canonical_path: entry
        for entry in build_overlap_ledger(plans, manifest_sha256(plans))
    }

    assert entries["ruleforge/evaluator.py"].source_owner == "worker-02"
    assert entries["ruleforge/evaluator.py"].peer_workers == ("worker-01", "worker-03")
    assert entries["ruleforge/domain.py"].source_owner == "worker-01"
    assert entries["ruleforge/domain.py"].peer_workers == ("worker-02", "worker-04")
    assert entries["ruleforge/policy_catalog.py"].source_owner == "worker-04"
    assert entries["ruleforge/policy_catalog.py"].peer_workers == ("worker-01", "worker-02")
    assert entries["ruleforge/registry.py"].source_owner == "worker-03"
    assert entries["ruleforge/registry.py"].peer_workers == ("worker-02", "worker-04")
    assert entries["ruleforge/rules/base.py"].source_owner == "worker-03"
    assert entries["ruleforge/rules/base.py"].peer_workers == ("worker-04",)
    assert "RuleRegistry.register(definition, expected) stores it under domain:name:source_field; get(key) returns (definition, expected)" in entries["ruleforge/registry.py"].required_facts
    assert "Rule registry primitives for RuleForge policy definitions." not in entries[
        "ruleforge/registry.py"
    ].required_facts


def test_manifest_and_ledger_are_deterministic() -> None:
    first = freeze_worker_plans()
    second = freeze_worker_plans()

    assert manifest_sha256(first) == manifest_sha256(second)
    assert build_overlap_ledger(first, manifest_sha256(first)) == build_overlap_ledger(
        second, manifest_sha256(second)
    )


def test_relevant_read_or_step_changes_relevant_brief() -> None:
    plans = freeze_worker_plans()
    original_hash = manifest_sha256(plans)
    original = {entry.canonical_path: entry for entry in build_overlap_ledger(plans, original_hash)}

    terra_plans = tuple(fixture_terra_plan(plan) for plan in plans)
    changed_step = replace(terra_plans[0], plan_steps=("changed security requirement",))
    changed_steps = {
        entry.canonical_path: entry
        for entry in build_overlap_ledger(plans, original_hash, (changed_step, *terra_plans[1:]))
    }
    assert changed_steps["ruleforge/evaluator.py"].brief_id != original[
        "ruleforge/evaluator.py"
    ].brief_id
    assert changed_steps["ruleforge/domain.py"].brief_id != original[
        "ruleforge/domain.py"
    ].brief_id
    assert changed_steps["ruleforge/registry.py"].brief_id == original[
        "ruleforge/registry.py"
    ].brief_id

    changed_read = replace(
        plans[0],
        initial_read_paths=("ruleforge/domain.py", "ruleforge/errors.py"),
        source_fact_requirements=(
            ("ruleforge/domain.py", ("NormalizedInput.value",)),
            ("ruleforge/errors.py", ("DuplicateRuleError",)),
        ),
        plan_fact_requirements=(
            ("ruleforge/domain.py", ("NormalizedInput.value",)),
            ("ruleforge/errors.py", ("DuplicateRuleError",)),
        ),
    )
    changed_reads = build_overlap_ledger((changed_read, *plans[1:]), original_hash)
    assert {entry.canonical_path for entry in changed_reads} == {
        "ruleforge/domain.py",
        "ruleforge/evaluator.py",
        "ruleforge/errors.py",
            "ruleforge/policy_catalog.py",
            "ruleforge/registry.py",
        "ruleforge/rules/base.py",
    }


@pytest.mark.parametrize(
    "bad_path",
    ["", "/absolute/path.py", "C:\\absolute\\path.py", "../outside.py", "a/../outside.py"],
)
def test_invalid_paths_are_rejected(bad_path: str) -> None:
    with pytest.raises(ValueError):
        WorkerPlan(
            "task",
            "worker",
            "worktree/worker",
            "branch",
            "objective",
            (bad_path,),
            "pytest tests/test_rule.py -q",
            ("ruleforge/source.py",),
            ("requirement",),
        )


def test_duplicate_worker_task_and_write_ids_are_rejected() -> None:
    plans = freeze_worker_plans()
    with pytest.raises(ValueError, match="duplicate worker_id"):
        manifest_sha256((*plans[:1], replace(plans[1], worker_id=plans[0].worker_id), *plans[2:]))
    with pytest.raises(ValueError, match="duplicate task_id"):
        manifest_sha256((*plans[:1], replace(plans[1], task_id=plans[0].task_id), *plans[2:]))
    with pytest.raises(ValueError, match="duplicate owned write path"):
        manifest_sha256((*plans[:1], replace(plans[1], owned_write_paths=plans[0].owned_write_paths), *plans[2:]))
