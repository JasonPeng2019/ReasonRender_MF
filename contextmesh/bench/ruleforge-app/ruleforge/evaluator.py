"""Evaluation orchestration for registered RuleForge policies."""
from __future__ import annotations

from collections.abc import Iterable

from .domain import Decision, Evidence, NormalizedInput, RuleDefinition, merge_evidence
from .normalizer import lower_text, number


def compare(observed: object | None, comparator: str, expected: object) -> bool:
    if comparator == "one_of":
        return lower_text(observed) in {str(item).lower() for item in expected}
    if comparator == "equals":
        return observed == expected
    if comparator == "at_least":
        value = number(observed)
        return value is not None and value >= float(expected)
    if comparator == "greater_than":
        value = number(observed)
        return value is not None and value > float(expected)
    raise ValueError(f"unsupported comparator: {comparator}")


def evaluate_definition(definition: RuleDefinition, expected: object, data: NormalizedInput) -> Decision:
    observed = data.value(definition.source_field)
    evidence = Evidence(definition.name, definition.source_field, observed, definition.comparator, "comparison evaluated")
    if observed is None:
        return Decision.reject(definition.error_code, Evidence(definition.name, definition.source_field, None, definition.comparator, "required value is missing"))
    if compare(observed, definition.comparator, expected):
        return Decision.allow(evidence)
    return Decision.reject(definition.error_code, evidence)


def evaluate_all(rules: Iterable[tuple[RuleDefinition, object]], data: NormalizedInput) -> Decision:
    decisions = tuple(evaluate_definition(definition, expected, data) for definition, expected in rules)
    rejected = next((decision for decision in decisions if not decision.allowed), None)
    if rejected is not None:
        return Decision(False, rejected.code, merge_evidence(*decisions))
    return Decision(True, None, merge_evidence(*decisions))


def explain(decision: Decision) -> list[str]:
    return [f"{item.rule_name}:{item.field}:{item.detail}" for item in decision.evidence]


def has_error(decision: Decision, code: str) -> bool:
    return decision.code == code


def is_allowed(decision: Decision) -> bool:
    return decision.allowed


def rule_count(rules: Iterable[tuple[RuleDefinition, object]]) -> int:
    return sum(1 for _ in rules)


def rejected_fields(decision: Decision) -> tuple[str, ...]:
    return tuple(item.field for item in decision.evidence if decision.code)


def requires_manual_review(decision: Decision) -> bool:
    return decision.code == "RISK_MANUAL_REVIEW_REQUIRED"
