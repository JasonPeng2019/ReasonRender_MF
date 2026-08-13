"""Reputation policy module — minimum trust rule."""
from __future__ import annotations

from ruleforge.registry import RuleRegistry
from ruleforge.rules.base import definition


def register(registry: RuleRegistry) -> None:
    """Register the reputation minimum-trust policy."""
    registry.register(
        definition(
            domain="reputation",
            name="minimum_trust",
            source_field="trust_score",
            comparator="greater_than",
            error_code="REPUTATION_TRUST_DENIED",
        ),
        expected=70.0,
    )
