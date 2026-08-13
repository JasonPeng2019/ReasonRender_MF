"""Retention policy module — minimum account age rule."""
from __future__ import annotations

from ruleforge.registry import RuleRegistry
from ruleforge.rules.base import definition


def register(registry: RuleRegistry) -> None:
    """Register the retention minimum-account-age policy."""
    registry.register(
        definition(
            domain="retention",
            name="minimum_retention_age",
            source_field="account_age_days",
            comparator="at_least",
            error_code="RETENTION_AGE_DENIED",
        ),
        expected=30.0,
    )
