"""Frozen seam shared by the ship-it-fast RRCv2 implementation lanes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Literal, Mapping, Protocol


Profile = Literal["lean", "detailed"]


@dataclass(frozen=True)
class Task:
    """One coding task in the repeating evaluation workload."""

    task_id: str
    family: str
    params: dict[str, object] = field(default_factory=dict)
    text: str = ""
    oracle_tests: str = ""
    case_shape: str | None = None
    slot_values: dict[str, object] | None = None

    def __post_init__(self) -> None:
        """Fill the new explicit boundary from legacy task fields when omitted."""

        if self.case_shape is None:
            object.__setattr__(self, "case_shape", self.text)
        if self.slot_values is None:
            object.__setattr__(self, "slot_values", self.params)

    @property
    def slot_names(self) -> tuple[str, ...]:
        """Return the declared dynamic slot names in deterministic order."""

        values = self.slot_values if self.slot_values is not None else self.params
        return tuple(sorted(values))


@dataclass(frozen=True)
class Spec:
    """Reusable implementation contract and its model-authored tests."""

    signature: str
    template: str
    tests: str


@dataclass(frozen=True)
class Plan:
    """Typed, generic planning content held by a product packet."""

    steps: tuple[str, ...]
    invariants: tuple[str, ...] = ()
    edges: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Keep all repeated plan sections immutable for packet storage."""

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
        """Normalize JSON-list-shaped fields to immutable tuples."""

        object.__setattr__(self, "slot_names", tuple(self.slot_names))
        for field_name in ("acceptance", "non_goals", "write_paths", "read_first"):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))

    def to_dict(self) -> dict[str, object]:
        """Return the exact labelled generic packet shape for JSON transport."""

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
    def from_dict(cls, payload: Mapping[str, object]) -> "PlanSpecPacket":
        """Parse one strict packet and reject missing or extra top-level fields."""

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
    def from_json(cls, payload: str) -> "PlanSpecPacket":
        """Parse a JSON packet using the same strict boundary as ``from_dict``."""

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


@dataclass(frozen=True)
class Template:
    """Exact generic spec skeleton stored by RRC for later rendering."""

    external_ref: str
    spec: Spec
    slot_names: tuple[str, ...]

    def __post_init__(self) -> None:
        """Keep the ordered slot schema immutable even for list-like input."""

        object.__setattr__(self, "slot_names", tuple(self.slot_names))


@dataclass(frozen=True)
class Outcome:
    """Token and verification result returned by the Lane A solve loop."""

    task_id: str
    warm: bool
    passed: bool
    reused: bool
    spec_tokens: int
    impl_tokens: int
    repair_tokens: int
    oracle_passed: bool | None = None

    @property
    def total(self) -> int:
        """Return all tokens spent by the task's model calls."""

        return self.spec_tokens + self.impl_tokens + self.repair_tokens


# A backend performs exactly one completion and reports its total token count.
Complete = Callable[[str, str], tuple[str, int]]


class Memory(Protocol):
    """Semantic memory supplied by Lane B."""

    def get(self, task: Task) -> Spec | None:
        """Return the closest reusable spec, if one is available."""

        ...

    def put(self, task: Task, spec: Spec) -> None:
        """Store an accepted templated spec for future retrieval."""

        ...


class NoMemory:
    """No-op memory used by the cold arm."""

    def get(self, task: Task) -> Spec | None:
        """Always miss."""

        return None

    def put(self, task: Task, spec: Spec) -> None:
        """Discard the spec."""

        return None
