"""Limits policy rule."""
from __future__ import annotations

from ..domain import Decision, NormalizedInput, RuleDefinition
from ..evaluator import evaluate_all
from ..policy_catalog import profile
from ..registry import RuleRegistry


def evaluate(data: NormalizedInput) -> Decision:
    policy = profile("limits.minimum_daily_requests")
    definition = RuleDefinition(
        name=policy.name,
        domain=policy.domain,
        source_field=policy.source_field,
        comparator=policy.comparator,
        error_code=policy.error_code,
    )
    registry = RuleRegistry()
    registry.register(definition, policy.expected)
    return evaluate_all(registry.all(), data)


evaluate_limits = evaluate
