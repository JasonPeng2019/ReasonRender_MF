"""Assurance policy rule."""
from __future__ import annotations

from ruleforge.domain import Decision, NormalizedInput
from ruleforge.evaluator import evaluate_all
from ruleforge.policy_catalog import profile
from ruleforge.registry import RuleRegistry

from .base import definition


PROFILE = profile("assurance.minimum_assurance")
DEFINITION = definition(
    PROFILE.domain,
    PROFILE.name,
    PROFILE.source_field,
    PROFILE.comparator,
    PROFILE.error_code,
)


def evaluate(data: NormalizedInput) -> Decision:
    registry = RuleRegistry()
    registry.register(DEFINITION, PROFILE.expected)
    return evaluate_all(registry.all(), data)


evaluate_assurance = evaluate
