"""Frozen seam shared by the ship-it-fast RRCv2 implementation lanes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class Task:
    """One coding task in the repeating evaluation workload."""

    task_id: str
    family: str
    params: dict[str, object]
    text: str
    oracle_tests: str = ""


@dataclass(frozen=True)
class Spec:
    """Reusable implementation contract and its model-authored tests."""

    signature: str
    template: str
    tests: str


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
