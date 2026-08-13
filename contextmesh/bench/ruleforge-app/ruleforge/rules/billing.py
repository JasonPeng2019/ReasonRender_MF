"""Billing policy module — minimum invoice total rule."""
from __future__ import annotations

from ruleforge.registry import RuleRegistry
from ruleforge.rules.base import definition


def register(registry: RuleRegistry) -> None:
    """Register the billing minimum-invoice policy."""
    registry.register(
        definition(
            domain="billing",
            name="minimum_invoice",
            source_field="invoice_total",
            comparator="at_least",
            error_code="BILLING_MINIMUM_NOT_MET",
        ),
        expected=100.0,
    )
