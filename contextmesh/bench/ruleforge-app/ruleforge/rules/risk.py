"""Risk policy module — manual review threshold rule."""
from __future__ import annotations

from ruleforge.registry import RuleRegistry
from ruleforge.rules.base import definition


def register(registry: RuleRegistry) -> None:
    """Register the risk manual-review-threshold policy."""
    registry.register(
        definition(
            domain="risk",
            name="manual_review_threshold",
            source_field="transaction_score",
            comparator="greater_than",
            error_code="RISK_MANUAL_REVIEW_REQUIRED",
        ),
        expected=75.0,
    )
