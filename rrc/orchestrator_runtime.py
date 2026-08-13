"""Deterministic product planning and worker runtime for generic packets."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from rrc.everos import EverOSClient
from rrc.orchestrator_contract import (
    Complete,
    OrchestratorTask,
    PlanSpecPacket,
    PlanSpecTemplate,
    Profile,
)
from rrc.orchestrator_policy import POLICY, OrchestratorPolicy, PolicyDecision, parse_packet
from rrc.store import SQLiteTemplateStore


@dataclass(frozen=True)
class OrchestratorResult:
    """Result of one product planner/worker execution."""

    hit: bool
    external_ref: str
    score: float | None
    packet: PlanSpecPacket
    profile: Profile
    estimated_implementation_tokens: int
    packet_token_budget: int
    planner_tokens: int
    worker_output: str
    worker_tokens: int


class OrchestratorRuntime:
    """Run one planner MISS or reusable-template HIT through one worker."""

    def __init__(
        self,
        planner: Complete,
        planner_model: str,
        worker: Complete,
        worker_model: str,
        store: SQLiteTemplateStore,
        everos: EverOSClient,
        policy: OrchestratorPolicy = POLICY,
    ) -> None:
        self._planner = planner
        self._planner_model = planner_model
        self._worker = worker
        self._worker_model = worker_model
        self._store = store
        self._everos = everos
        self._policy = policy

    def run(self, task: OrchestratorTask) -> OrchestratorResult:
        """Execute a task, bypassing the planner when a valid template is found."""

        case_shape = _required_case_shape(task)
        missing_ref: str | None = None
        for external_ref, score in self._everos.search(case_shape):
            try:
                template = self._store.get_plan_spec(external_ref)
            except (KeyError, TypeError, ValueError):
                template = None
            if template is None and missing_ref is None:
                # EverOS is persistent while a demo/runtime store may be new.
                # Rebind the first same-shape opaque ref after validating the
                # new packet, so stale search rows cannot starve the cache.
                missing_ref = external_ref
            if template is None or not self._matches(task, template, external_ref):
                continue

            worker_output, worker_tokens = self._worker(
                _worker_prompt(case_shape, task, template), self._worker_model
            )
            return _result(
                hit=True,
                score=score,
                template=template,
                planner_tokens=0,
                worker_output=worker_output,
                worker_tokens=worker_tokens,
            )

        decision = self._policy.decide(task)
        planner_output, planner_tokens = self._planner(
            _planner_prompt(task, case_shape, decision, self._policy), self._planner_model
        )
        packet = parse_packet(planner_output)
        self._policy.validate_packet(task, packet, decision)

        template = PlanSpecTemplate(
            external_ref=missing_ref or str(uuid.uuid4()),
            case_shape=case_shape,
            packet=packet,
            profile=decision.profile,
            estimated_implementation_tokens=decision.estimated_implementation_tokens,
            packet_token_budget=decision.packet_token_budget,
        )
        worker_output, worker_tokens = self._worker(
            _worker_prompt(case_shape, task, template), self._worker_model
        )
        self._store.put_plan_spec(template)
        self._everos.index(case_shape, template.external_ref)
        return _result(
            hit=False,
            score=None,
            template=template,
            planner_tokens=planner_tokens,
            worker_output=worker_output,
            worker_tokens=worker_tokens,
        )

    def lookup_cached_template(self, task: OrchestratorTask) -> tuple[PlanSpecTemplate, float] | None:
        """Return one validated cached template without invoking planner or worker."""

        case_shape = _required_case_shape(task)
        for external_ref, score in self._everos.search(case_shape):
            try:
                template = self._store.get_plan_spec(external_ref)
            except (KeyError, TypeError, ValueError):
                template = None
            if template is not None and self._matches(task, template, external_ref):
                return template, score
        return None

    def _matches(
        self,
        task: OrchestratorTask,
        template: PlanSpecTemplate,
        external_ref: str,
    ) -> bool:
        """Reject stale refs and malformed generic rows before treating them as HITs."""

        if template.external_ref != external_ref:
            return False
        if template.case_shape != _required_case_shape(task):
            return False
        decision = self._policy.decide(task)
        if template.profile != decision.profile:
            return False
        if template.estimated_implementation_tokens != decision.estimated_implementation_tokens:
            return False
        if template.packet_token_budget != decision.packet_token_budget:
            return False
        try:
            return self._policy.validate_packet(task, template.packet, decision)
        except (AttributeError, TypeError, ValueError):
            return False


def _required_case_shape(task: OrchestratorTask) -> str:
    if task.case_shape is None:
        raise ValueError("task case_shape is required")
    return task.case_shape


def _planner_prompt(
    task: OrchestratorTask,
    case_shape: str,
    decision: PolicyDecision,
    policy: OrchestratorPolicy,
) -> str:
    return "\n\n".join(
        (
            policy.planner_guidance(decision),
            "Stable case_shape:\n" + case_shape,
            "Declared slot names:\n"
            + json.dumps(list(task.slot_names), ensure_ascii=False, separators=(",", ":")),
            "Oracle-check count:\n" + str(decision.oracle_count),
            "Return the generic packet as JSON only.",
        )
    )


def _serialize_slot(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _render_text(text: str, slot_names: tuple[str, ...], slot_values: Mapping[str, object]) -> str:
    for name in slot_names:
        text = text.replace("{" + name + "}", _serialize_slot(slot_values[name]))
    return text


def _render_value(
    value: object,
    slot_names: tuple[str, ...],
    slot_values: Mapping[str, object],
) -> object:
    if isinstance(value, str):
        return _render_text(value, slot_names, slot_values)
    if isinstance(value, list):
        return [_render_value(item, slot_names, slot_values) for item in value]
    if isinstance(value, dict):
        return {key: _render_value(item, slot_names, slot_values) for key, item in value.items()}
    return value


def _worker_prompt(case_shape: str, task: OrchestratorTask, template: PlanSpecTemplate) -> str:
    return "Product worker input:\n" + json.dumps(
        render_worker_packet(case_shape, task, template),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def render_worker_packet(
    case_shape: str, task: OrchestratorTask, template: PlanSpecTemplate
) -> dict[str, object]:
    """Render one exact worker packet from a cached template and concrete task.

    This is the full-arm renderer: callers receive resolved task and packet
    values, never a generic packet accompanied by detached ``slot_values``.
    """

    slot_values = task.slot_values if task.slot_values is not None else task.params
    packet = _render_value(template.packet.to_dict(), task.slot_names, slot_values)
    return {
        "task": _render_text(case_shape, task.slot_names, slot_values),
        "packet": packet,
        "profile": template.profile,
        "estimated_implementation_tokens": template.estimated_implementation_tokens,
        "packet_token_budget": template.packet_token_budget,
    }


def _result(
    *,
    hit: bool,
    score: float | None,
    template: PlanSpecTemplate,
    planner_tokens: int,
    worker_output: str,
    worker_tokens: int,
) -> OrchestratorResult:
    return OrchestratorResult(
        hit=hit,
        external_ref=template.external_ref,
        score=score,
        packet=template.packet,
        profile=template.profile,
        estimated_implementation_tokens=template.estimated_implementation_tokens,
        packet_token_budget=template.packet_token_budget,
        planner_tokens=planner_tokens,
        worker_output=worker_output,
        worker_tokens=worker_tokens,
    )
