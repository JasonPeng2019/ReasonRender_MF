from __future__ import annotations

import json

from contextmesh.bench.rrc_long_spec_demo import materialize
from harness.four_worker_plan import build_overlap_ledger, manifest_sha256
from harness.staged_workload import STAGE_IDS, stage_workload, staged_overlap_ledgers, staged_workloads, write_stage_manifest


def test_append_only_stages_have_distinct_tasks_and_a_natural_rollout_overlap(tmp_path) -> None:
    stages = staged_workloads()

    assert [stage.stage_id for stage in stages] == list(STAGE_IDS)
    assert len({plan.task_id for stage in stages for plan in stage.plans}) == 148
    assert all(len(stage.plans) == 4 for stage in stages)
    assert [len({path for plan in stage.plans for path in plan.owned_write_paths}) for stage in stages] == [9, 9, 8, 9, 9, 8, 9, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 9, 9, 8]

    first, second, third, fourth, fifth, sixth, seventh, eighth, ninth, tenth, eleventh, twelfth, thirteenth, fourteenth, fifteenth, sixteenth, seventeenth, eighteenth, nineteenth, twentieth, twenty_first, twenty_second, twenty_third, twenty_fourth, twenty_fifth, twenty_sixth, twenty_seventh, twenty_eighth, twenty_ninth, thirtieth, thirty_first, thirty_second, thirty_third, thirty_fourth, thirty_fifth, thirty_sixth, thirty_seventh = stages
    assert first.required_rollout_revision == "stage-00" and first.next_rollout_revision == "stage-01"
    assert second.required_rollout_revision == "stage-01" and second.next_rollout_revision == "stage-02"
    assert third.required_rollout_revision == "stage-02" and third.next_rollout_revision is None
    assert fourth.required_rollout_revision == "stage-02" and fourth.next_rollout_revision == "stage-04"
    assert fifth.required_rollout_revision == "stage-04" and fifth.next_rollout_revision == "stage-05"
    assert sixth.required_rollout_revision == "stage-05" and sixth.next_rollout_revision is None
    assert seventh.required_rollout_revision == "stage-05" and seventh.next_rollout_revision == "stage-07"
    assert eighth.required_rollout_revision == "stage-07" and eighth.next_rollout_revision is None
    assert ninth.required_rollout_revision == "independent" and ninth.next_rollout_revision is None
    assert tenth.required_rollout_revision == "independent" and tenth.next_rollout_revision is None
    assert eleventh.required_rollout_revision == "independent" and eleventh.next_rollout_revision is None
    assert twelfth.required_rollout_revision == "independent" and twelfth.next_rollout_revision is None
    assert thirteenth.required_rollout_revision == "independent" and thirteenth.next_rollout_revision is None
    assert fourteenth.required_rollout_revision == "independent" and fourteenth.next_rollout_revision is None
    assert fifteenth.required_rollout_revision == "independent" and fifteenth.next_rollout_revision is None
    assert sixteenth.required_rollout_revision == "independent" and sixteenth.next_rollout_revision is None
    assert seventeenth.required_rollout_revision == "independent" and seventeenth.next_rollout_revision is None
    assert eighteenth.required_rollout_revision == "independent" and eighteenth.next_rollout_revision is None
    assert nineteenth.required_rollout_revision == "independent" and nineteenth.next_rollout_revision is None
    assert twentieth.required_rollout_revision == "independent" and twentieth.next_rollout_revision is None
    assert twenty_first.required_rollout_revision == "independent" and twenty_first.next_rollout_revision is None
    assert twenty_second.required_rollout_revision == "independent" and twenty_second.next_rollout_revision is None
    assert twenty_third.required_rollout_revision == "independent" and twenty_third.next_rollout_revision is None
    assert twenty_fourth.required_rollout_revision == "independent" and twenty_fourth.next_rollout_revision is None
    assert twenty_fifth.required_rollout_revision == "independent" and twenty_fifth.next_rollout_revision is None
    assert twenty_sixth.required_rollout_revision == "independent" and twenty_sixth.next_rollout_revision is None
    assert twenty_seventh.required_rollout_revision == "independent" and twenty_seventh.next_rollout_revision is None
    assert twenty_eighth.required_rollout_revision == "independent" and twenty_eighth.next_rollout_revision is None
    assert twenty_ninth.required_rollout_revision == "independent" and twenty_ninth.next_rollout_revision is None
    assert thirtieth.required_rollout_revision == "independent" and thirtieth.next_rollout_revision is None
    assert thirty_first.required_rollout_revision == "independent" and thirty_first.next_rollout_revision is None
    assert thirty_second.required_rollout_revision == "independent" and thirty_second.next_rollout_revision is None
    assert thirty_third.required_rollout_revision == "independent" and thirty_third.next_rollout_revision is None
    assert thirty_fourth.required_rollout_revision == "independent" and thirty_fourth.next_rollout_revision is None
    assert thirty_fifth.required_rollout_revision == "stage-00" and thirty_fifth.next_rollout_revision == "stage-35"
    assert thirty_sixth.required_rollout_revision == "stage-35" and thirty_sixth.next_rollout_revision == "stage-36"
    assert thirty_seventh.required_rollout_revision == "stage-36" and thirty_seventh.next_rollout_revision is None
    for stage in stages:
        rollout_readers = [plan.worker_id for plan in stage.plans if "ruleforge/rollout.py" in plan.initial_read_paths]
        if stage.stage_id in {"stage-09", "stage-10", "stage-11", "stage-12", "stage-13", "stage-14", "stage-15", "stage-16", "stage-17", "stage-18", "stage-19", "stage-20", "stage-21", "stage-22", "stage-23", "stage-24", "stage-25", "stage-26", "stage-27", "stage-28", "stage-29", "stage-30", "stage-31", "stage-32", "stage-33", "stage-34"}:
            assert rollout_readers == []
            continue
        assert rollout_readers == ["worker-01", "worker-02"]
        entries = build_overlap_ledger(stage.plans, manifest_sha256(stage.plans))
        rollout = next(entry for entry in entries if entry.canonical_path == "ruleforge/rollout.py")
        assert rollout.source_owner in rollout_readers
        assert set(rollout.peer_workers) == set(rollout_readers) - {rollout.source_owner}
        manifest = write_stage_manifest(tmp_path, stage)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        assert [task["task_id"] for task in payload["tasks"]] == [plan.task_id for plan in stage.plans]


def test_stage_materializer_binds_stage_qualified_task_ids(tmp_path) -> None:
    stage = stage_workload("stage-04")
    manifest = materialize(tmp_path / "ruleforge", stage.cohort, stage.stage_id)

    assert [task["task_id"] for task in manifest["tasks"]] == [plan.task_id for plan in stage.plans]
    assert (tmp_path / "ruleforge" / "workspace" / "ruleforge" / "rollout.py").is_file()


def test_stable_overlaps_carry_a_compact_two_stage_catalog_window_without_task_plans() -> None:
    ledgers = staged_overlap_ledgers()
    catalogs = [next(entry for entry in entries if entry.canonical_path == "ruleforge/policy_catalog.py") for entries in ledgers]
    rollout = [
        next(entry for entry in entries if entry.canonical_path == "ruleforge/rollout.py")
        for entries in ledgers[:8]
    ]

    assert catalogs[-2].required_facts == catalogs[-1].required_facts
    assert len(catalogs[-1].required_facts) == 7
    assert all(4 <= len(catalog.required_facts) <= 9 for catalog in catalogs)
    assert catalogs[-2].requirements_hash == catalogs[-1].requirements_hash
    assert catalogs[-2].brief_id != catalogs[-1].brief_id
    latest_ledger = ledgers[-1]
    assert all(
        "PolicyProfile(" not in fact
        for entry in latest_ledger
        if entry.canonical_path != "ruleforge/policy_catalog.py"
        for fact in entry.required_facts
    )
    assert [entry.required_facts for entry in rollout] == [
        ('STAGE_REVISION = "stage-00"',),
        ('STAGE_REVISION = "stage-01"',),
        ('STAGE_REVISION = "stage-02"',),
        ('STAGE_REVISION = "stage-02"',),
        ('STAGE_REVISION = "stage-04"',),
        ('STAGE_REVISION = "stage-05"',),
        ('STAGE_REVISION = "stage-05"',),
        ('STAGE_REVISION = "stage-07"',),
    ]
    operational = stage_workload("stage-17").plans
    assert all(
        any("operational.control_" in fact for _, facts in plan.plan_fact_requirements for fact in facts)
        for plan in operational
    )
    assert all(
        any("OPERATIONAL_CONTROL_" in fact for _, facts in plan.plan_fact_requirements for fact in facts)
        for plan in operational
    )
    catalog_readers = [plan for plan in operational if "ruleforge/policy_catalog.py" in plan.initial_read_paths]
    assert len(catalog_readers) == 3
    assert all("Import profile from ruleforge.policy_catalog" in plan.source_facts_for("ruleforge/policy_catalog.py")[0] for plan in catalog_readers)
