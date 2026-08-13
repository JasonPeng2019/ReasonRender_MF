"""Delivery policy: supported_region rule restricts delivery_region."""
from __future__ import annotations

from ruleforge.registry import RuleRegistry
from ruleforge.rules.base import definition


def register(registry: RuleRegistry) -> None:
    registry.register(
        definition(
            domain="delivery",
            name="supported_region",
            source_field="delivery_region",
            comparator="equals",
            error_code="DELIVERY_REGION_DENIED",
        ),
        expected="region-north-1",
    )
