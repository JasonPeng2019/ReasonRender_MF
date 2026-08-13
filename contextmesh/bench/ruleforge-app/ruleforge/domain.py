"""RuleForge domain contracts shared by every policy module."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class NormalizedInput:
    values: Mapping[str, object]
    subject_id: str
    request_id: str

    def value(self, field: str) -> object | None:
        return self.values.get(field)


@dataclass(frozen=True)
class Evidence:
    rule_name: str
    field: str
    observed: object | None
    comparator: str
    detail: str


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str | None
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)

    @classmethod
    def allow(cls, evidence: Evidence) -> "Decision":
        return cls(allowed=True, code=None, evidence=(evidence,))

    @classmethod
    def reject(cls, code: str, evidence: Evidence) -> "Decision":
        return cls(allowed=False, code=code, evidence=(evidence,))


@dataclass(frozen=True)
class RuleDefinition:
    name: str
    domain: str
    source_field: str
    comparator: str
    error_code: str

    def description(self) -> str:
        return f"{self.domain}:{self.name} reads {self.source_field} with {self.comparator}"


def require_text(value: object | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    text = value.strip()
    return text or None


def decision_summary(decision: Decision) -> str:
    if decision.allowed:
        return "allowed"
    return decision.code or "rejected"


def merge_evidence(*decisions: Decision) -> tuple[Evidence, ...]:
    return tuple(item for decision in decisions for item in decision.evidence)


def stable_rule_key(definition: RuleDefinition) -> str:
    return ":".join((definition.domain, definition.name, definition.source_field))


def is_terminal(decision: Decision) -> bool:
    return not decision.allowed


def empty_input(subject_id: str, request_id: str) -> NormalizedInput:
    return NormalizedInput(values={}, subject_id=subject_id, request_id=request_id)
