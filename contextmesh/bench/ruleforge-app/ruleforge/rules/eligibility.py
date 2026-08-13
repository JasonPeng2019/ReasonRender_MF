"""Eligibility policy module — minimum age rule."""
from __future__ import annotations

from ruleforge.registry import RuleRegistry
from ruleforge.rules.base import definition


def register(registry: RuleRegistry) -> None:
    """Register the eligibility minimum-age policy."""
    registry.register(
        definition(
            domain="eligibility",
            name="minimum_age",
            source_field="age_years",
            comparator="at_least",
            error_code="ELIGIBILITY_AGE_DENIED",
        ),
        expected=18.0,
    )
