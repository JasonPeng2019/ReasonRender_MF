"""Source-free RRCv2 state for later workflow-stage Terra delta plans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rrc.store import SQLiteTemplateStore


class RRCStageError(ValueError):
    """A stage-plan record is missing, cross-workflow, or malformed."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RRCStageError(f"{field} must be a non-empty string")
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _dependency_revisions(value: Sequence[Mapping[str, object]]) -> tuple[dict[str, str], ...]:
    revisions: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise RRCStageError("dependency revisions must be objects")
        revisions.append(
            {
                "lineage_id": _text(item.get("lineage_id"), "dependency lineage_id"),
                "content_sha256": _text(item.get("content_sha256"), "dependency content_sha256"),
                "brief_facts_hash": _text(item.get("brief_facts_hash"), "dependency brief_facts_hash"),
            }
        )
    if len({item["lineage_id"] for item in revisions}) != len(revisions):
        raise RRCStageError("dependency lineage ids must not repeat")
    return tuple(sorted(revisions, key=lambda item: item["lineage_id"]))


def stage_plan_key(
    *,
    workflow_id: str,
    arm: str,
    stage_id: str,
    parent_stage_key: str | None,
    task_shape: Mapping[str, object],
    template_version: str,
    dependency_revisions: Sequence[Mapping[str, object]],
) -> str:
    """Hash only source-free stage-plan identity and ContextMesh revisions."""

    identity = {
        "workflow_id": _text(workflow_id, "workflow_id"),
        "arm": _text(arm, "arm"),
        "stage_id": _text(stage_id, "stage_id"),
        "parent_stage_key": parent_stage_key,
        "task_shape": dict(task_shape),
        "template_version": _text(template_version, "template_version"),
        "dependency_revisions": _dependency_revisions(dependency_revisions),
    }
    return "rrcv2-stage-" + _digest(identity)


def _stage_store(root: Path) -> SQLiteTemplateStore:
    cache = root / ".rrc-cache"
    cache.mkdir(parents=True, exist_ok=True)
    return SQLiteTemplateStore(cache / "plan-spec.sqlite")


def record_stage_plan_state(
    workspace: str | Path,
    *,
    workflow_id: str,
    arm: str,
    stage_id: str,
    parent_stage_key: str | None,
    task_shape: Mapping[str, object],
    template_version: str,
    template_external_ref: str,
    delivery_plan_sha256: str,
    dependency_revisions: Sequence[Mapping[str, object]],
    output: str | Path | None = None,
) -> dict[str, object]:
    """Persist the completed Terra stage without source or worker-solution text."""

    root = Path(workspace).resolve()
    revisions = _dependency_revisions(dependency_revisions)
    key = stage_plan_key(
        workflow_id=workflow_id,
        arm=arm,
        stage_id=stage_id,
        parent_stage_key=parent_stage_key,
        task_shape=task_shape,
        template_version=template_version,
        dependency_revisions=revisions,
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "event": "rrc_stage_plan_state",
        "stage_plan_key": key,
        "workflow_id": _text(workflow_id, "workflow_id"),
        "arm": _text(arm, "arm"),
        "stage_id": _text(stage_id, "stage_id"),
        "parent_stage_key": parent_stage_key,
        "task_shape": dict(task_shape),
        "task_shape_sha256": _digest(dict(task_shape)),
        "template_version": _text(template_version, "template_version"),
        "template_external_ref": _text(template_external_ref, "template_external_ref"),
        "delivery_plan_sha256": _text(delivery_plan_sha256, "delivery_plan_sha256"),
        "dependency_revisions": list(revisions),
        "dependency_revisions_sha256": _digest(revisions),
    }
    store = _stage_store(root)
    try:
        if parent_stage_key is not None and store.get_stage_plan_state(parent_stage_key) is None:
            raise RRCStageError("parent stage-plan state is not retained")
        store.put_stage_plan_state(key, payload)
    finally:
        store.close()
    if output is not None:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def lookup_stage_plan(
    workspace: str | Path,
    *,
    workflow_id: str,
    arm: str,
    stage_id: str,
    parent_stage_key: str,
    task_shape: Mapping[str, object],
    template_version: str,
    dependency_revisions: Sequence[Mapping[str, object]],
    output: str | Path | None = None,
) -> dict[str, object]:
    """Retrieve prior plan structure and compute a source-free delta-plan key.

    A changed ContextMesh revision does not force Terra to reconstruct source.
    It is carried as an affected dependency in the new key so only plans that
    cite that lineage become a new stage record.
    """

    root = Path(workspace).resolve()
    revisions = _dependency_revisions(dependency_revisions)
    store = _stage_store(root)
    try:
        parent = store.get_stage_plan_state(_text(parent_stage_key, "parent_stage_key"))
    finally:
        store.close()
    if parent is None:
        raise RRCStageError("stage-plan cache miss")
    if parent.get("workflow_id") != workflow_id or parent.get("arm") != arm:
        raise RRCStageError("stage-plan state belongs to another workflow or arm")
    if parent.get("template_version") != template_version:
        raise RRCStageError("stage-plan template version changed")
    prior_revisions = parent.get("dependency_revisions")
    if not isinstance(prior_revisions, list):
        raise RRCStageError("stage-plan state lacks dependency revisions")
    prior_by_lineage = {
        item.get("lineage_id"): item
        for item in prior_revisions
        if isinstance(item, Mapping) and isinstance(item.get("lineage_id"), str)
    }
    changed = [
        revision["lineage_id"]
        for revision in revisions
        if prior_by_lineage.get(revision["lineage_id"])
        != {
            "lineage_id": revision["lineage_id"],
            "content_sha256": revision["content_sha256"],
            "brief_facts_hash": revision["brief_facts_hash"],
        }
    ]
    key = stage_plan_key(
        workflow_id=workflow_id,
        arm=arm,
        stage_id=stage_id,
        parent_stage_key=parent_stage_key,
        task_shape=task_shape,
        template_version=template_version,
        dependency_revisions=revisions,
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "event": "rrc_stage_plan_hit",
        "stage_plan_key": key,
        "parent_stage_plan_key": parent_stage_key,
        "workflow_id": workflow_id,
        "arm": arm,
        "stage_id": stage_id,
        "task_shape": dict(task_shape),
        "task_shape_sha256": _digest(dict(task_shape)),
        "template_version": template_version,
        "template_external_ref": parent.get("template_external_ref"),
        "prior_delivery_plan_sha256": parent.get("delivery_plan_sha256"),
        "dependency_revisions": list(revisions),
        "changed_dependency_lineages": sorted(changed),
        "reconstruction_reads": 0,
    }
    if output is not None:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


__all__ = ["RRCStageError", "lookup_stage_plan", "record_stage_plan_state", "stage_plan_key"]
