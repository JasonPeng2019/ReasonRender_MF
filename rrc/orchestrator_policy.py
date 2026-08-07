"""Versioned deterministic policy for product Plan + Spec packets."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from rrc.contract import PlanSpecPacket, Profile, Task


POLICY_VERSION = "v1"
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_PLANNER_GUIDANCE = """You are the product planning orchestrator. Return one generic Plan + Spec packet for the worker.

The controller fixes the profile, estimate, and ceiling below. Do not choose or change them.
The controller supplies only the stable task shape, declared slot names, and deterministic
oracle-check count. The packet must use only named placeholders such as {slot_name}; never copy
literal slot values or include slot_values. Return exactly one JSON object with exactly these
top-level labels:
signature, slot_names, plan, specification, acceptance, non_goals, write_paths, read_first.
The plan object may contain only steps, invariants, edges, and constraints.

Every profile includes the task shape, exact signature/contract, declared slot names, focused
acceptance criteria, explicit non-goals, intended write paths, and 3-5 relevant read-first paths.
Do not add boilerplate, speculative edge matrices, or an implementation recipe.

For lean, write 1-2 direct plan steps covering normal behavior, one task-relevant edge/error,
and 1-2 focused acceptance criteria. Do not add invariants or constraints.
For detailed, write 2-4 direct plan steps, material invariants, task-relevant edges/errors,
hard constraints, and 2-4 focused acceptance criteria.

The combined whitespace-token count of the task shape and every worker-facing packet text field
must be at or below the fixed packet ceiling. Planner output is generic and reusable; only the
runtime renders current slot values after validation. Do not return extra JSON labels, a profile,
an estimate, a budget, literal dynamic values, or unrelated project guidance."""


class PacketValidationError(ValueError):
    """A planner packet does not satisfy the static product contract."""


@dataclass(frozen=True)
class PolicyDecision:
    """The complete deterministic decision supplied to the product runtime."""

    version: str
    slot_count: int
    oracle_count: int
    shape_words: int
    complexity_score: int
    profile: Profile
    estimated_implementation_tokens: int
    packet_token_budget: int


class OrchestratorPolicy:
    """Static v1 policy for choosing and bounding a product planning packet."""

    version = POLICY_VERSION

    def decide(self, task: Task) -> PolicyDecision:
        """Score declared slots, supplied checks, and stable task-shape size."""

        slot_count = len(_slot_names(task))
        oracle_count = task.oracle_tests.count("def test_")
        if oracle_count == 0 and task.oracle_tests.strip():
            oracle_count = 1
        shape_words = len(_case_shape(task).split())

        score = _slot_weight(slot_count) + _oracle_weight(oracle_count) + _shape_weight(
            shape_words
        )
        profile: Profile = "lean" if score <= 2 else "detailed"
        estimated = min(
            2400,
            max(
                600,
                600
                + 100 * slot_count
                + 150 * oracle_count
                + 2 * min(shape_words, 300),
            ),
        )
        return PolicyDecision(
            version=self.version,
            slot_count=slot_count,
            oracle_count=oracle_count,
            shape_words=shape_words,
            complexity_score=score,
            profile=profile,
            estimated_implementation_tokens=estimated,
            packet_token_budget=estimated // 4,
        )

    __call__ = decide

    def planner_guidance(self, decision: PolicyDecision) -> str:
        """Return the fixed planner instructions plus this request's policy bounds."""

        if decision.version != self.version:
            raise ValueError("planner guidance requires the current policy version")
        return (
            _PLANNER_GUIDANCE
            + f"\n\nFixed profile: {decision.profile}."
            + f" Estimated implementation tokens: {decision.estimated_implementation_tokens}."
            + f" Packet token ceiling: {decision.packet_token_budget}."
        )

    def packet_token_count(self, task: Task, packet: PlanSpecPacket) -> int:
        """Count whitespace-delimited tokens in the complete worker packet text."""

        return sum(len(text.split()) for text in _packet_texts(task, packet))

    def validate_packet(
        self,
        task: Task,
        packet: PlanSpecPacket,
        decision: PolicyDecision | None = None,
    ) -> bool:
        """Strictly validate one generic packet against the fixed policy."""

        if type(packet) is not PlanSpecPacket:
            raise PacketValidationError("packet must be a PlanSpecPacket")
        decision = self.decide(task) if decision is None else decision
        if decision.version != self.version:
            raise PacketValidationError("packet decision has the wrong policy version")

        slot_names = _slot_names(task)
        if len(packet.slot_names) != len(set(packet.slot_names)):
            raise PacketValidationError("slot_names must not contain duplicates")
        if set(packet.slot_names) != set(slot_names):
            raise PacketValidationError("slot_names must exactly match the task slot schema")

        shape = _case_shape(task)
        shape_placeholders = set(_PLACEHOLDER.findall(shape))
        if shape_placeholders != set(slot_names):
            raise PacketValidationError("case_shape placeholders must match the task slot schema")
        _reject_literals(shape, slot_names, _slot_values(task))

        if not packet.signature.strip() or not packet.specification.strip():
            raise PacketValidationError("signature and specification are required")
        if not packet.write_paths or not packet.non_goals:
            raise PacketValidationError("write_paths and non_goals are required")
        if not 3 <= len(packet.read_first) <= 5:
            raise PacketValidationError("read_first must contain 3-5 paths")

        steps = packet.plan.steps
        if not all(step.strip() for step in steps):
            raise PacketValidationError("plan steps must be non-empty")
        if decision.profile == "lean":
            if not 1 <= len(steps) <= 2 or not 1 <= len(packet.acceptance) <= 2:
                raise PacketValidationError("lean packets require 1-2 plan steps and tests")
            if len(packet.plan.edges) != 1:
                raise PacketValidationError("lean packets require one task-relevant edge/error")
            if packet.plan.invariants or packet.plan.constraints:
                raise PacketValidationError("lean packets cannot add detailed-only sections")
        else:
            if not 2 <= len(steps) <= 4 or not 2 <= len(packet.acceptance) <= 4:
                raise PacketValidationError(
                    "detailed packets require 2-4 plan steps and tests"
                )
            if not packet.plan.invariants:
                raise PacketValidationError("detailed packets require material invariants")
            if not packet.plan.edges:
                raise PacketValidationError("detailed packets require task-relevant edges")
            if not packet.plan.constraints:
                raise PacketValidationError("detailed packets require hard constraints")

        for text in _packet_texts(task, packet):
            _reject_unknown_placeholders(text, slot_names)
            _reject_literals(text, slot_names, _slot_values(task))

        token_count = self.packet_token_count(task, packet)
        if token_count > decision.packet_token_budget:
            raise PacketValidationError(
                f"packet has {token_count} tokens; ceiling is {decision.packet_token_budget}"
            )
        return True


def _case_shape(task: Task) -> str:
    return task.case_shape if task.case_shape is not None else task.text


def _slot_values(task: Task) -> Mapping[str, object]:
    return task.slot_values if task.slot_values is not None else task.params


def _slot_names(task: Task) -> tuple[str, ...]:
    values = _slot_values(task)
    if not all(isinstance(name, str) for name in values):
        raise PacketValidationError("slot names must be strings")
    return tuple(sorted(values))


def _slot_weight(count: int) -> int:
    return 0 if count <= 2 else 1 if count <= 4 else 2


def _oracle_weight(count: int) -> int:
    return 0 if count == 0 else 1 if count <= 2 else 2


def _shape_weight(words: int) -> int:
    return 0 if words <= 60 else 1 if words <= 160 else 2


def _packet_texts(task: Task, packet: PlanSpecPacket) -> tuple[str, ...]:
    """Return exactly the textual fields included in the worker token scope."""

    return (
        _case_shape(task),
        packet.signature,
        *packet.plan.steps,
        *packet.plan.invariants,
        *packet.plan.edges,
        *packet.plan.constraints,
        packet.specification,
        *packet.acceptance,
        *packet.non_goals,
        *packet.write_paths,
        *packet.read_first,
    )


def _reject_unknown_placeholders(text: str, slot_names: tuple[str, ...]) -> None:
    unknown = set(_PLACEHOLDER.findall(text)) - set(slot_names)
    if unknown:
        raise PacketValidationError(
            "generic packet contains unknown placeholders: " + ", ".join(sorted(unknown))
        )


def _reject_literals(
    text: str,
    slot_names: tuple[str, ...],
    slot_values: Mapping[str, object],
) -> None:
    scrubbed = text
    for name in slot_names:
        scrubbed = scrubbed.replace("{" + name + "}", "")
    for value in slot_values.values():
        if isinstance(value, str):
            literal = value
        else:
            literal = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        if literal and literal in scrubbed:
            raise PacketValidationError("generic packet contains a literal slot value")


def parse_packet(payload: str | Mapping[str, object]) -> PlanSpecPacket:
    """Parse a planner response into the exact typed generic packet boundary."""

    if isinstance(payload, str):
        return PlanSpecPacket.from_json(payload)
    return PlanSpecPacket.from_dict(payload)


POLICY = OrchestratorPolicy()


def packet_token_count(task: Task, packet: PlanSpecPacket) -> int:
    """Count worker-facing packet tokens using the static policy's exact scope."""

    return POLICY.packet_token_count(task, packet)


def validate_packet(
    task: Task,
    packet: PlanSpecPacket,
    decision: PolicyDecision | None = None,
) -> bool:
    """Validate a packet with the module's static policy."""

    return POLICY.validate_packet(task, packet, decision)
