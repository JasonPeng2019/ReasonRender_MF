"""Coordinator-plan artifacts used by the measured four-worker dispatcher.

The frozen workload describes *what* each worker must do.  These helpers keep
the concrete *how* plan outside that workload: raw and ContextMesh Terras write
it after their preflight; an RRC HIT renders it into the same artifact shape.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from contextmesh.bench.rrc_long_spec_demo import CASE_SHAPE
from contextmesh.mcp.broker_service import ledger_payload
from rrc.orchestrator_contract import OrchestratorTask, PlanSpecPacket, PlanSpecTemplate
from rrc.orchestrator_runtime import render_worker_packet as render_rrc_worker_packet

from harness.four_worker_plan import (
    TerraPlan,
    WorkerPlan,
    build_overlap_ledger,
    freeze_worker_plans,
    manifest_sha256,
)
from harness.rrc_stage import lookup_stage_plan, record_stage_plan_state
from harness.worker_packets import worker_prompt


class TerraPlanError(ValueError):
    """A coordinator packet is absent, malformed, or bound to the wrong task."""


_SOLUTION_BODY_MARKERS = (
    "module pattern",
    "test pattern",
    "prepared real contract snapshot",
    "write exactly this shape",
    "from ruleforge",
    "import ",
    "def ",
    "assert ",
)
_WORKER_REREAD_MARKERS = ("re-read ", "reread ", "directly inspect ")


def plan_path(root: str | Path, worker_id: str) -> Path:
    return Path(root) / f"{worker_id}.json"


def plan_set_sha256(plans: Sequence[TerraPlan]) -> str:
    """Hash the exact four concrete plans the dispatcher will hand to Lunas."""

    return hashlib.sha256(
        json.dumps(
            [
                {
                    "task_id": item.task_id,
                    "worker_id": item.worker_id,
                    "plan_steps": item.plan_steps,
                    "source_facts": item.source_facts,
                }
                for item in plans
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def task_request(plans: Sequence[WorkerPlan]) -> dict[str, object]:
    """Return task contracts, deliberately excluding any implementation plan."""

    return {
        "schema_version": 1,
        "instructions": (
            "Write exactly one concrete coordinator plan per task. Each plan must name the task and worker, "
            "contain ordered implementation steps, and record source-derived facts required by that worker. "
            "Before writing a plan, directly inspect every initial_read_path. Each required_plan_fact_anchors entry is "
            "a source-contract verification anchor, not an implementation plan: include every anchor verbatim in "
            "that task's source_facts after confirming it against the source. The worker packet, not this plan, "
            "owns source access: do not tell a worker to re-read or directly inspect source. Never include imports, "
            "function bodies, code snippets, or prewritten test bodies. Do not invent APIs or source behavior."
        ),
        "plan_schema": {
            "task_id": "string",
            "worker_id": "string",
            "plan_steps": ["ordered non-empty string"],
            "source_facts": ["source-derived non-empty string"],
        },
        "tasks": [
            {
                "task_id": plan.task_id,
                "worker_id": plan.worker_id,
                "plan_output": f".terra-plans/{plan.worker_id}.json",
                "objective": plan.objective,
                "owned_write_paths": list(plan.owned_write_paths),
                "acceptance_cmd": plan.acceptance_cmd,
                "initial_read_paths": list(plan.initial_read_paths),
                "task_requirements": list(plan.task_requirements),
                "required_plan_fact_anchors": {
                    path: list(facts) for path, facts in plan.plan_fact_requirements
                },
            }
            for plan in plans
        ],
    }


def write_task_request(path: str | Path, plans: Sequence[WorkerPlan]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(task_request(plans), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _plan_from_payload(value: object) -> TerraPlan:
    if not isinstance(value, Mapping):
        raise TerraPlanError("coordinator plan must be a JSON object")
    try:
        return TerraPlan(
            task_id=value["task_id"],
            worker_id=value["worker_id"],
            plan_steps=tuple(value["plan_steps"]),
            source_facts=tuple(value["source_facts"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TerraPlanError(f"malformed coordinator plan: {error}") from error


def _fact_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _require_source_contract(plan: TerraPlan, worker: WorkerPlan) -> None:
    """Require Terra's retained artifact to cover every frozen source contract."""

    rendered = _fact_key(" ".join(plan.source_facts))
    missing = [
        fact
        for _, facts in worker.plan_fact_requirements
        for fact in facts
        if _fact_key(fact) not in rendered
    ]
    if missing:
        raise TerraPlanError(
            f"coordinator plan {worker.worker_id} omitted required source fact: {missing[0]}"
        )


def _bind_plan_fact_anchors(plan: TerraPlan, worker: WorkerPlan) -> TerraPlan:
    """Project an audited Terra artifact onto its frozen worker delivery facts.

    Terra may record additional source observations while planning. They remain
    in its retained JSON audit artifact, but the delivered worker packet must
    not turn those observations into a substitute for a ContextMesh brief.
    """

    return TerraPlan(
        task_id=plan.task_id,
        worker_id=plan.worker_id,
        plan_steps=plan.plan_steps,
        source_facts=tuple(fact for _, facts in worker.plan_fact_requirements for fact in facts),
    )


def _reject_solution_body(plan: TerraPlan, worker: WorkerPlan) -> None:
    """Keep RRC savings in Terra; plan steps cannot prewrite Luna output.

    Source-fact anchors intentionally contain exact import and call spelling so
    a ContextMesh brief can be executable without a worker trial import. Those
    facts are not solution bodies, so only executable delivery *steps* are
    screened here.
    """

    step_text = "\n".join(plan.plan_steps).casefold()
    marker = next((item for item in _SOLUTION_BODY_MARKERS if item in step_text), None)
    if marker:
        raise TerraPlanError(
            f"coordinator plan {worker.worker_id} contains forbidden solution body marker: {marker}"
        )
    marker = next((item for item in _WORKER_REREAD_MARKERS if item in step_text), None)
    if marker:
        raise TerraPlanError(
            f"coordinator plan {worker.worker_id} directs an arm-incompatible worker source reread"
        )


def load_terra_plans(root: str | Path, plans: Sequence[WorkerPlan]) -> tuple[TerraPlan, ...]:
    """Load exactly the four plan artifacts Terra produced for this workload."""

    source = Path(root)
    found = sorted(source.glob("*.json"))
    if not found:
        raise TerraPlanError(f"missing coordinator plan: {source}")
    by_task: dict[str, TerraPlan] = {}
    for path in found:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise TerraPlanError(f"malformed coordinator plan JSON: {path}") from error
        candidate = _plan_from_payload(value)
        if candidate.task_id in by_task:
            raise TerraPlanError(f"duplicate coordinator plan task id: {candidate.task_id}")
        by_task[candidate.task_id] = candidate
    expected_tasks = {plan.task_id for plan in plans}
    if set(by_task) != expected_tasks:
        raise TerraPlanError("coordinator plan task ids do not match the frozen task contracts")
    candidates = [by_task[worker.task_id] for worker in plans]
    result: list[TerraPlan] = []
    for worker, candidate in zip(plans, candidates, strict=True):
        _require_source_contract(candidate, worker)
        _reject_solution_body(candidate, worker)
        result.append(_bind_plan_fact_anchors(candidate, worker))
    try:
        build_overlap_ledger(plans, manifest_sha256(plans), result)
    except ValueError as error:
        raise TerraPlanError(str(error)) from error
    return tuple(result)


def render_rrc_plans(workspace: str | Path, output: str | Path, plans: Sequence[WorkerPlan]) -> tuple[TerraPlan, ...]:
    """Render the RRC cache HIT into four exact coordinator-plan artifacts."""

    workspace_root = Path(workspace)
    try:
        template = json.loads((workspace_root / ".rrc-cache" / "template.json").read_text(encoding="utf-8"))
        bindings = json.loads((workspace_root / ".rrc-cache" / "bindings.json").read_text(encoding="utf-8"))["bindings"]
    except (FileNotFoundError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise TerraPlanError(f"invalid RRC HIT cache: {error}") from error
    if not isinstance(template, Mapping) or not isinstance(bindings, Mapping):
        raise TerraPlanError("invalid RRC HIT cache shape")

    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)
    rendered: list[TerraPlan] = []
    for worker in plans:
        binding = bindings.get(worker.task_id)
        if not isinstance(binding, Mapping):
            raise TerraPlanError(f"RRC HIT lacks binding for {worker.task_id}")
        template_packet = PlanSpecPacket.from_dict(template)
        task = OrchestratorTask(
            task_id=worker.task_id,
            family="ruleforge",
            params=binding,
            case_shape=CASE_SHAPE,
            slot_values=binding,
        )
        resolved = render_rrc_worker_packet(
            CASE_SHAPE,
            task,
            PlanSpecTemplate(
                external_ref="measured-rrc-hit",
                case_shape=CASE_SHAPE,
                packet=template_packet,
                profile="detailed",
                estimated_implementation_tokens=1,
                packet_token_budget=1,
            ),
        )
        packet = resolved["packet"]
        encoded = json.dumps(packet, sort_keys=True)
        if "{" + "domain" + "}" in encoded:
            raise TerraPlanError("RRC HIT left an unresolved template field")
        plan = packet.get("plan") if isinstance(packet, Mapping) else None
        if not isinstance(plan, Mapping):
            raise TerraPlanError("RRC HIT packet lacks a plan")
        facts = [fact for _, required in worker.plan_fact_requirements for fact in required]
        value = TerraPlan(
            task_id=worker.task_id,
            worker_id=worker.worker_id,
            plan_steps=tuple(str(step) for step in plan.get("steps", ())),
            source_facts=tuple(fact for fact in facts if fact),
        )
        plan_path(destination, worker.worker_id).write_text(
            json.dumps(
                {
                    "task_id": value.task_id,
                    "worker_id": value.worker_id,
                    "plan_steps": list(value.plan_steps),
                    "source_facts": list(value.source_facts),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        rendered.append(value)
    return tuple(rendered)


def rrc_stage_task_shape(plans: Sequence[WorkerPlan]) -> dict[str, object]:
    """Return the source-free coordinator shape reused by later RRC stages."""

    return {
        "family": "ruleforge",
        "workers": len(plans),
        "worker_ids": [plan.worker_id for plan in plans],
        "plan_schema": "terra/v1",
    }


def record_rrc_stage_plan(
    workspace: str | Path,
    plans: Sequence[WorkerPlan],
    *,
    workflow_id: str,
    arm: str,
    stage_id: str,
    parent_stage_key: str | None,
    template_version: str,
    template_external_ref: str,
    dependency_revisions: Sequence[Mapping[str, object]],
    rendered: Sequence[TerraPlan],
    output: str | Path | None = None,
) -> dict[str, object]:
    """Persist a rendered stage's plan state without storing worker source text."""

    return record_stage_plan_state(
        workspace,
        workflow_id=workflow_id,
        arm=arm,
        stage_id=stage_id,
        parent_stage_key=parent_stage_key,
        task_shape=rrc_stage_task_shape(plans),
        template_version=template_version,
        template_external_ref=template_external_ref,
        delivery_plan_sha256=plan_set_sha256(rendered),
        dependency_revisions=dependency_revisions,
        output=output,
    )


def render_rrc_stage_delta_plans(
    workspace: str | Path,
    output: str | Path,
    plans: Sequence[WorkerPlan],
    *,
    workflow_id: str,
    arm: str,
    stage_id: str,
    parent_stage_key: str,
    template_version: str,
    dependency_revisions: Sequence[Mapping[str, object]],
    evidence_output: str | Path | None = None,
) -> tuple[tuple[TerraPlan, ...], dict[str, object], dict[str, object]]:
    """Use retained RRC state to render a later, source-free Terra delta plan."""

    hit = lookup_stage_plan(
        workspace,
        workflow_id=workflow_id,
        arm=arm,
        stage_id=stage_id,
        parent_stage_key=parent_stage_key,
        task_shape=rrc_stage_task_shape(plans),
        template_version=template_version,
        dependency_revisions=dependency_revisions,
        output=(Path(evidence_output).with_name("rrc-stage-hit.json") if evidence_output is not None else None),
    )
    rendered = render_rrc_plans(workspace, output, plans)
    external_ref = hit.get("template_external_ref")
    if not isinstance(external_ref, str) or not external_ref:
        raise TerraPlanError("RRC stage HIT lacks the retained template reference")
    state = record_rrc_stage_plan(
        workspace,
        plans,
        workflow_id=workflow_id,
        arm=arm,
        stage_id=stage_id,
        parent_stage_key=parent_stage_key,
        template_version=template_version,
        template_external_ref=external_ref,
        dependency_revisions=dependency_revisions,
        rendered=rendered,
        output=evidence_output,
    )
    return rendered, hit, state


def prepare_dispatch(
    arm: str, arm_root: str | Path, terra_plan_root: str | Path, cohort: str = "core"
) -> dict[str, object]:
    """Turn retained Terra plan files into ledger-bound, per-worker dispatch prompts."""

    if arm not in {"raw", "contextmesh", "full"}:
        raise TerraPlanError(f"unknown arm: {arm}")
    root = Path(arm_root)
    plans = freeze_worker_plans(cohort)
    observed_terra_plans = load_terra_plans(terra_plan_root, plans)
    by_task = {plan.task_id: plan for plan in observed_terra_plans}
    ledger = build_overlap_ledger(plans, manifest_sha256(plans), observed_terra_plans)
    if arm != "raw":
        broker_ledger = root / "broker" / "ledger.json"
        broker_ledger.parent.mkdir(parents=True, exist_ok=True)
        broker_ledger.write_text(json.dumps(ledger_payload(ledger), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for worker in plans:
        prompt = root / "workers" / worker.worker_id / "prompt.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text(worker_prompt(arm, worker, ledger, by_task[worker.task_id]), encoding="utf-8")
    delivery_digest = plan_set_sha256(observed_terra_plans)
    ready = {
        "schema_version": 1,
        "arm": arm,
        "terra_plan_sha256": delivery_digest,
        "delivery_plan_sha256": delivery_digest,
        "worker_count": len(plans),
        "overlap_count": len(ledger),
    }
    (root / "dispatch-ready.json").write_text(json.dumps(ready, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return ready


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render-rrc")
    render.add_argument("--workspace", required=True)
    render.add_argument("--output", required=True)
    render.add_argument("--cohort", default="core")
    render.add_argument("--stage-id")
    stage_render = commands.add_parser("render-stage-rrc")
    stage_render.add_argument("--workspace", required=True)
    stage_render.add_argument("--output", required=True)
    stage_render.add_argument("--stage-id", required=True)
    stage_render.add_argument("--workflow-id", required=True)
    stage_render.add_argument("--arm", required=True)
    stage_render.add_argument("--parent-stage-key", required=True)
    stage_render.add_argument("--dependencies", required=True)
    stage_render.add_argument("--evidence-output", required=True)
    dispatch = commands.add_parser("prepare-dispatch")
    dispatch.add_argument("--arm", required=True)
    dispatch.add_argument("--arm-root", required=True)
    dispatch.add_argument("--terra-plan-root", required=True)
    dispatch.add_argument("--cohort", default="core")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "render-stage-rrc":
            from harness.staged_workload import stage_workload

            value = json.loads(Path(args.dependencies).read_text(encoding="utf-8"))
            dependencies = value.get("dependency_revisions") if isinstance(value, Mapping) else None
            if not isinstance(dependencies, list):
                raise TerraPlanError("stage RRC dependency file is malformed")
            render_rrc_stage_delta_plans(
                args.workspace,
                args.output,
                stage_workload(args.stage_id).plans,
                workflow_id=args.workflow_id,
                arm=args.arm,
                stage_id=args.stage_id,
                parent_stage_key=args.parent_stage_key,
                template_version="ruleforge-template/v1",
                dependency_revisions=dependencies,
                evidence_output=args.evidence_output,
            )
        else:
            if getattr(args, "stage_id", None):
                # The staged runner owns the workflow contract; this CLI only
                # renders the already-prewarmed, source-free RRC packet into
                # its stage-qualified worker plan files.
                from harness.staged_workload import stage_workload

                plans = stage_workload(args.stage_id).plans
            else:
                plans = freeze_worker_plans(args.cohort)
            if args.command == "render-rrc":
                render_rrc_plans(args.workspace, args.output, plans)
            else:
                prepare_dispatch(args.arm, args.arm_root, args.terra_plan_root, args.cohort)
    except TerraPlanError as error:
        print(f"terra plan error: {error}")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the dispatcher.
    raise SystemExit(main())


__all__ = [
    "TerraPlanError",
    "load_terra_plans",
    "plan_set_sha256",
    "plan_path",
    "prepare_dispatch",
    "render_rrc_plans",
    "task_request",
    "write_task_request",
]
