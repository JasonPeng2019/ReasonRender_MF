"""Private contracts for the optional product-orchestrator experiment.

These types intentionally live outside :mod:`rrc.contract`; that module is the
frozen public seam shared by Lane A and Lane B.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Literal, Mapping

Profile = Literal["lean", "detailed"]
Complete = Callable[[str, str], tuple[str, int]]


@dataclass(frozen=True)
class OrchestratorTask:
    """One task consumed by the optional product planning orchestrator."""

    task_id: str
    family: str
    params: Mapping[str, object] = field(default_factory=dict)
    text: str = ""
    oracle_tests: str = ""
    case_shape: str | None = None
    slot_values: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.case_shape is None:
            object.__setattr__(self, "case_shape", self.text)
        if self.slot_values is None:
            object.__setattr__(self, "slot_values", self.params)

    @property
    def slot_names(self) -> tuple[str, ...]:
        values = self.slot_values if self.slot_values is not None else self.params
        return tuple(sorted(values))


@dataclass(frozen=True)
class Plan:
    """Typed, generic planning content held by a product packet."""

    steps: tuple[str, ...]
    invariants: tuple[str, ...] = ()
    edges: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("steps", "invariants", "edges", "constraints"):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))


@dataclass(frozen=True)
class PlanSpecPacket:
    """Strict generic Plan + Spec packet returned by the product planner."""

    signature: str
    slot_names: tuple[str, ...]
    plan: Plan
    specification: str
    acceptance: tuple[str, ...]
    non_goals: tuple[str, ...]
    write_paths: tuple[str, ...]
    read_first: tuple[str, ...]

    FIELDS = (
        "signature",
        "slot_names",
        "plan",
        "specification",
        "acceptance",
        "non_goals",
        "write_paths",
        "read_first",
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "slot_names", tuple(self.slot_names))
        for field_name in ("acceptance", "non_goals", "write_paths", "read_first"):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))

    def to_dict(self) -> dict[str, object]:
        return {
            "signature": self.signature,
            "slot_names": list(self.slot_names),
            "plan": {
                "steps": list(self.plan.steps),
                "invariants": list(self.plan.invariants),
                "edges": list(self.plan.edges),
                "constraints": list(self.plan.constraints),
            },
            "specification": self.specification,
            "acceptance": list(self.acceptance),
            "non_goals": list(self.non_goals),
            "write_paths": list(self.write_paths),
            "read_first": list(self.read_first),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PlanSpecPacket:
        if set(payload) != set(cls.FIELDS):
            raise ValueError("packet fields must be exactly: " + ", ".join(cls.FIELDS))

        plan_value = payload["plan"]
        if isinstance(plan_value, Mapping):
            if set(plan_value) - {"steps", "invariants", "edges", "constraints"}:
                raise ValueError("plan contains an unknown field")
            plan = Plan(
                steps=_text_tuple(plan_value.get("steps", ()), "plan.steps"),
                invariants=_text_tuple(plan_value.get("invariants", ()), "plan.invariants"),
                edges=_text_tuple(plan_value.get("edges", ()), "plan.edges"),
                constraints=_text_tuple(plan_value.get("constraints", ()), "plan.constraints"),
            )
        else:
            plan = Plan(steps=_text_tuple(plan_value, "plan"))

        signature = payload["signature"]
        specification = payload["specification"]
        if not isinstance(signature, str) or not isinstance(specification, str):
            raise TypeError("signature and specification must be strings")

        return cls(
            signature=signature,
            slot_names=_text_tuple(payload["slot_names"], "slot_names"),
            plan=plan,
            specification=specification,
            acceptance=_text_tuple(payload["acceptance"], "acceptance"),
            non_goals=_text_tuple(payload["non_goals"], "non_goals"),
            write_paths=_text_tuple(payload["write_paths"], "write_paths"),
            read_first=_text_tuple(payload["read_first"], "read_first"),
        )

    @classmethod
    def from_json(cls, payload: str) -> PlanSpecPacket:
        value = json.loads(payload)
        if not isinstance(value, Mapping):
            raise TypeError("packet JSON must contain one object")
        return cls.from_dict(value)


def _text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise TypeError(f"{field_name} must be a list of strings")
    if not all(isinstance(item, str) for item in value):
        raise TypeError(f"{field_name} must be a list of strings")
    return tuple(value)


@dataclass(frozen=True)
class PlanSpecTemplate:
    """Generic packet plus stable shape and deterministic runtime metadata."""

    external_ref: str
    case_shape: str
    packet: PlanSpecPacket
    profile: Profile
    estimated_implementation_tokens: int
    packet_token_budget: int
