from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.rrc_stage import RRCStageError, lookup_stage_plan, record_stage_plan_state


def _dependencies(*, catalog_hash: str = "catalog-v1", registry_hash: str = "registry-v1") -> list[dict[str, str]]:
    return [
        {
            "lineage_id": "main:ruleforge/policy_catalog.py",
            "content_sha256": catalog_hash,
            "brief_facts_hash": "catalog-facts",
        },
        {
            "lineage_id": "main:ruleforge/registry.py",
            "content_sha256": registry_hash,
            "brief_facts_hash": "registry-facts",
        },
    ]


def _shape(stage: str) -> dict[str, object]:
    return {"family": "ruleforge", "stage": stage, "workers": 4, "plan_schema": "terra/v1"}


def test_stage_plan_lookup_uses_prior_state_and_invalidates_only_changed_dependency(tmp_path: Path) -> None:
    stage_one = record_stage_plan_state(
        tmp_path,
        workflow_id="ruleforge-staged",
        arm="full",
        stage_id="stage-01",
        parent_stage_key=None,
        task_shape=_shape("stage-01"),
        template_version="plan-template/v1",
        template_external_ref="rrcv2-template",
        delivery_plan_sha256="delivery-stage-01",
        dependency_revisions=_dependencies(),
    )

    stage_two = lookup_stage_plan(
        tmp_path,
        workflow_id="ruleforge-staged",
        arm="full",
        stage_id="stage-02",
        parent_stage_key=stage_one["stage_plan_key"],
        task_shape=_shape("stage-02"),
        template_version="plan-template/v1",
        dependency_revisions=_dependencies(catalog_hash="catalog-v2"),
        output=tmp_path / "stage-two-hit.json",
    )

    assert stage_two["event"] == "rrc_stage_plan_hit"
    assert stage_two["reconstruction_reads"] == 0
    assert stage_two["changed_dependency_lineages"] == ["main:ruleforge/policy_catalog.py"]
    assert stage_two["template_external_ref"] == "rrcv2-template"
    assert "source_content" not in json.dumps(stage_two)
    assert json.loads((tmp_path / "stage-two-hit.json").read_text(encoding="utf-8")) == stage_two

    stage_two_state = record_stage_plan_state(
        tmp_path,
        workflow_id="ruleforge-staged",
        arm="full",
        stage_id="stage-02",
        parent_stage_key=stage_one["stage_plan_key"],
        task_shape=_shape("stage-02"),
        template_version="plan-template/v1",
        template_external_ref="rrcv2-template",
        delivery_plan_sha256="delivery-stage-02",
        dependency_revisions=_dependencies(catalog_hash="catalog-v2"),
    )
    stage_three = lookup_stage_plan(
        tmp_path,
        workflow_id="ruleforge-staged",
        arm="full",
        stage_id="stage-03",
        parent_stage_key=stage_two_state["stage_plan_key"],
        task_shape=_shape("stage-03"),
        template_version="plan-template/v1",
        dependency_revisions=_dependencies(catalog_hash="catalog-v2"),
    )

    assert stage_three["changed_dependency_lineages"] == []
    assert stage_three["reconstruction_reads"] == 0


def test_stage_plan_lookup_fails_closed_for_missing_or_cross_arm_parent(tmp_path: Path) -> None:
    with pytest.raises(RRCStageError, match="cache miss"):
        lookup_stage_plan(
            tmp_path,
            workflow_id="ruleforge-staged",
            arm="full",
            stage_id="stage-02",
            parent_stage_key="missing",
            task_shape=_shape("stage-02"),
            template_version="plan-template/v1",
            dependency_revisions=_dependencies(),
        )

    raw_state = record_stage_plan_state(
        tmp_path,
        workflow_id="ruleforge-staged",
        arm="raw",
        stage_id="stage-01",
        parent_stage_key=None,
        task_shape=_shape("stage-01"),
        template_version="plan-template/v1",
        template_external_ref="rrcv2-template",
        delivery_plan_sha256="delivery-stage-01",
        dependency_revisions=_dependencies(),
    )
    with pytest.raises(RRCStageError, match="another workflow or arm"):
        lookup_stage_plan(
            tmp_path,
            workflow_id="ruleforge-staged",
            arm="full",
            stage_id="stage-02",
            parent_stage_key=raw_state["stage_plan_key"],
            task_shape=_shape("stage-02"),
            template_version="plan-template/v1",
            dependency_revisions=_dependencies(),
        )
