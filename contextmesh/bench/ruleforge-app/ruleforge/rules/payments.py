"""Payments policy: approved_currency rule restricts currency."""
from __future__ import annotations

from ruleforge.registry import RuleRegistry
from ruleforge.rules.base import definition


def register(registry: RuleRegistry) -> None:
    registry.register(
        definition(
            domain="payments",
            name="approved_currency",
            source_field="currency",
            comparator="one_of",
            error_code="PAYMENTS_CURRENCY_DENIED",
        ),
        expected=["usd", "cad", "eur"],
    )
