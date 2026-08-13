from __future__ import annotations

import json

import pytest
from contextmesh.bench.rrc_long_spec_demo import materialize
from harness.four_worker_plan import fixture_terra_plan, freeze_worker_plans
from harness.staged_workload import stage_workload
from harness.terra_plans import (
    TerraPlanError,
    load_terra_plans,
    prepare_dispatch,
    record_rrc_stage_plan,
    render_rrc_stage_delta_plans,
    render_rrc_plans,
    task_request,
)


def test_frozen_task_request_contains_no_precomputed_coordinator_plan() -> None:
    request = task_request(freeze_worker_plans())
    encoded = json.dumps(request, sort_keys=True)
    assert isinstance(request["tasks"], list)
    assert isinstance(request["instructions"], str)
    tasks = request["tasks"]

    assert len(tasks) == 4
    assert "coordinator_plan" not in encoded
    assert all(isinstance(task, dict) and "plan_steps" not in task for task in tasks)
    assert all(
        isinstance(task, dict)
        and isinstance(task.get("required_plan_fact_anchors"), dict)
        and task["required_plan_fact_anchors"]
        for task in tasks
    )
    assert "generic_packet" not in encoded
    assert "do not tell a worker to re-read or directly inspect source" in request["instructions"]


def test_rrc_hit_renders_concrete_plan_artifacts_then_dispatches_them(tmp_path) -> None:
    materialize(tmp_path / "workload")
    workspace = tmp_path / "workload" / "workspace"
    arm_root = tmp_path / "full"
    plans = freeze_worker_plans()

    rendered = render_rrc_plans(workspace, workspace / ".terra-plans", plans)
    ready = prepare_dispatch("full", arm_root, workspace / ".terra-plans")
    prompt = (arm_root / "workers" / "worker-01" / "prompt.md").read_text(encoding="utf-8")

    assert len(rendered) == 4
    assert ready["worker_count"] == 4 and ready["overlap_count"] == 5
    assert ready["delivery_plan_sha256"]
    assert '"coordinator_plan"' in prompt
    assert "generic_packet" not in prompt and "slot_values" not in prompt
    assert (arm_root / "broker" / "ledger.json").is_file()
    for plan, worker in zip(rendered, plans, strict=True):
        assert plan.source_facts == tuple(
            fact for _, facts in worker.plan_fact_requirements for fact in facts
        )
        encoded = "\n".join(plan.plan_steps).casefold()
        assert "from ruleforge" not in encoded
        assert "def " not in encoded


def test_exact_import_source_facts_are_not_mistaken_for_prewritten_worker_code(tmp_path) -> None:
    plans = stage_workload("stage-19").plans
    for worker in plans:
        observed = fixture_terra_plan(worker)
        (tmp_path / f"{worker.worker_id}.json").write_text(
            json.dumps(
                {
                    "task_id": observed.task_id,
                    "worker_id": observed.worker_id,
                    "plan_steps": list(observed.plan_steps),
                    "source_facts": list(observed.source_facts),
                }
            ),
            encoding="utf-8",
        )

    rendered = load_terra_plans(tmp_path, plans)

    assert any("Import profile from ruleforge.policy_catalog" in fact for plan in rendered for fact in plan.source_facts)


def test_later_rrc_stage_renders_delta_plan_without_reconstruction_reads(tmp_path) -> None:
    materialize(tmp_path / "workload")
    workspace = tmp_path / "workload" / "workspace"
    plans = freeze_worker_plans()
    stage_one = render_rrc_plans(workspace, workspace / ".terra-plans-stage-1", plans)
    dependencies_one = [
        {
            "lineage_id": "ruleforge/policy_catalog.py",
            "content_sha256": "catalog-v1",
            "brief_facts_hash": "catalog-facts-v1",
        }
    ]
    state_one = record_rrc_stage_plan(
        workspace,
        plans,
        workflow_id="ruleforge-staged-local",
        arm="full",
        stage_id="stage-1",
        parent_stage_key=None,
        template_version="ruleforge-template/v1",
        template_external_ref="measured-rrc-hit",
        dependency_revisions=dependencies_one,
        rendered=stage_one,
    )

    rendered, hit, state_two = render_rrc_stage_delta_plans(
        workspace,
        workspace / ".terra-plans-stage-2",
        plans,
        workflow_id="ruleforge-staged-local",
        arm="full",
        stage_id="stage-2",
        parent_stage_key=str(state_one["stage_plan_key"]),
        template_version="ruleforge-template/v1",
        dependency_revisions=[
            {
                "lineage_id": "ruleforge/policy_catalog.py",
                "content_sha256": "catalog-v2",
                "brief_facts_hash": "catalog-facts-v2",
            }
        ],
    )

    assert len(rendered) == 4
    assert hit["reconstruction_reads"] == 0
    assert hit["changed_dependency_lineages"] == ["ruleforge/policy_catalog.py"]
    assert state_two["parent_stage_key"] == state_one["stage_plan_key"]

    _stage_three, hit_three, state_three = render_rrc_stage_delta_plans(
        workspace,
        workspace / ".terra-plans-stage-3",
        plans,
        workflow_id="ruleforge-staged-local",
        arm="full",
        stage_id="stage-3",
        parent_stage_key=str(state_two["stage_plan_key"]),
        template_version="ruleforge-template/v1",
        dependency_revisions=[
            {
                "lineage_id": "ruleforge/policy_catalog.py",
                "content_sha256": "catalog-v2",
                "brief_facts_hash": "catalog-facts-v2",
            }
        ],
    )

    assert hit_three["reconstruction_reads"] == 0
    assert hit_three["changed_dependency_lineages"] == []
    assert state_three["parent_stage_key"] == state_two["stage_plan_key"]


def test_plan_anchors_do_not_preempt_complete_contextmesh_brief_facts(tmp_path) -> None:
    materialize(tmp_path / "workload")
    workspace = tmp_path / "workload" / "workspace"
    arm_root = tmp_path / "full"
    plans = freeze_worker_plans()

    render_rrc_plans(workspace, workspace / ".terra-plans", plans)
    prepare_dispatch("full", arm_root, workspace / ".terra-plans")
    prompt = (arm_root / "workers" / "worker-01" / "prompt.md").read_text(encoding="utf-8")
    ledger = json.loads((arm_root / "broker" / "ledger.json").read_text(encoding="utf-8"))
    catalog = next(entry for entry in ledger if entry["canonical_path"] == "ruleforge/policy_catalog.py")

    assert "Versioned operational policy profiles used by RuleForge rule modules." in prompt
    assert "security.required_access_level -> security, required_access_level" not in prompt
    assert any(
        "security.required_access_level -> security, required_access_level" in fact
        for fact in catalog["required_facts"]
    )


def test_dispatch_refuses_missing_or_misbound_terra_plan(tmp_path) -> None:
    plans = freeze_worker_plans()
    with pytest.raises(TerraPlanError, match="missing coordinator plan"):
        load_terra_plans(tmp_path, plans)

    (tmp_path / "worker-01.json").write_text(
        json.dumps({"task_id": "wrong", "worker_id": "worker-01", "plan_steps": ["x"], "source_facts": ["y"]}),
        encoding="utf-8",
    )
    with pytest.raises(TerraPlanError):
        load_terra_plans(tmp_path, plans)


def test_dispatch_refuses_a_terra_plan_that_omits_a_required_source_contract(tmp_path) -> None:
    plans = freeze_worker_plans()
    for worker in plans:
        observed = fixture_terra_plan(worker)
        facts = list(observed.source_facts)
        if worker.worker_id == "worker-04":
            facts.remove(
                "Service facade for evaluating RuleForge policy registries."
            )
        (tmp_path / f"{worker.worker_id}.json").write_text(
            json.dumps(
                {
                    "task_id": observed.task_id,
                    "worker_id": observed.worker_id,
                    "plan_steps": list(observed.plan_steps),
                    "source_facts": facts,
                }
            ),
            encoding="utf-8",
        )

    with pytest.raises(TerraPlanError, match="worker-04 omitted required source fact"):
        load_terra_plans(tmp_path, plans)


def test_dispatch_delivers_only_frozen_plan_anchors_after_terra_observes_extra_facts(tmp_path) -> None:
    plans = freeze_worker_plans()
    for worker in plans:
        observed = fixture_terra_plan(worker)
        facts = [*observed.source_facts, "Extra planner observation must not reach a worker packet."]
        (tmp_path / f"{worker.worker_id}.json").write_text(
            json.dumps(
                {
                    "task_id": observed.task_id,
                    "worker_id": observed.worker_id,
                    "plan_steps": list(observed.plan_steps),
                    "source_facts": facts,
                }
            ),
            encoding="utf-8",
        )

    delivered = load_terra_plans(tmp_path, plans)

    assert all("Extra planner observation" not in plan.source_facts for plan in delivered)
    assert delivered == tuple(fixture_terra_plan(plan) for plan in plans)


def test_dispatch_delivers_the_retained_terra_artifact_without_solution_body(tmp_path) -> None:
    materialize(tmp_path / "workload")
    workspace = tmp_path / "workload" / "workspace"
    plans = freeze_worker_plans()
    raw_plan_root = tmp_path / "raw-plans"
    raw_plan_root.mkdir()
    for plan in plans:
        observed = fixture_terra_plan(plan)
        (raw_plan_root / f"{plan.worker_id}.json").write_text(
            json.dumps(
                {
                    "task_id": observed.task_id,
                    "worker_id": observed.worker_id,
                    "plan_steps": list(observed.plan_steps),
                    "source_facts": list(observed.source_facts),
                }
            ),
            encoding="utf-8",
        )
    render_rrc_plans(workspace, workspace / ".terra-plans", plans)
    raw_root, full_root = tmp_path / "raw", tmp_path / "full"

    raw_ready = prepare_dispatch("raw", raw_root, raw_plan_root)
    full_ready = prepare_dispatch("full", full_root, workspace / ".terra-plans")
    raw_prompt = (raw_root / "workers" / "worker-02" / "prompt.md").read_text(encoding="utf-8")
    full_prompt = (full_root / "workers" / "worker-02" / "prompt.md").read_text(encoding="utf-8")
    raw_packet = json.loads(raw_prompt[raw_prompt.index("{") :])
    full_packet = json.loads(full_prompt[full_prompt.index("{") :])

    assert raw_ready["delivery_plan_sha256"] != full_ready["delivery_plan_sha256"]
    assert raw_packet["coordinator_plan"]["plan_steps"] == list(fixture_terra_plan(plans[1]).plan_steps)
    assert full_packet["coordinator_plan"]["plan_steps"] != raw_packet["coordinator_plan"]["plan_steps"]
    assert full_packet["coordinator_plan"]["source_facts"] == list(fixture_terra_plan(plans[1]).source_facts)


@pytest.mark.parametrize(
    "bad_step",
    (
        "Re-read ruleforge/domain.py before editing.",
        "Write exactly this shape: from ruleforge.registry import RuleRegistry",
    ),
)
def test_dispatch_rejects_source_rereads_and_prewritten_solution_bodies(tmp_path, bad_step: str) -> None:
    plans = freeze_worker_plans()
    for worker in plans:
        observed = fixture_terra_plan(worker)
        steps = list(observed.plan_steps)
        if worker.worker_id == "worker-01":
            steps[0] = bad_step
        (tmp_path / f"{worker.worker_id}.json").write_text(
            json.dumps(
                {
                    "task_id": observed.task_id,
                    "worker_id": observed.worker_id,
                    "plan_steps": steps,
                    "source_facts": list(observed.source_facts),
                }
            ),
            encoding="utf-8",
        )

    with pytest.raises(TerraPlanError):
        load_terra_plans(tmp_path, plans)
