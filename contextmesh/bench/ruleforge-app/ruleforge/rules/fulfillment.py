"""Fulfillment policy: allowed_destination rule restricts shipment_country."""
from __future__ import annotations

from ruleforge.registry import RuleRegistry
from ruleforge.rules.base import definition


def register(registry: RuleRegistry) -> None:
    registry.register(
        definition(
            domain="fulfillment",
            name="allowed_destination",
            source_field="shipment_country",
            comparator="equals",
            error_code="FULFILLMENT_DESTINATION_DENIED",
        ),
        expected="US",
    )
