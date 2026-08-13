"""Compliance policy module — approved country evaluation."""
from __future__ import annotations

from ruleforge.registry import RuleRegistry
from ruleforge.rules.base import definition


def register(registry: RuleRegistry) -> None:
    """Register the approved-country compliance policy."""
    registry.register(
        definition(
            domain='compliance',
            name='approved_country',
            source_field='country',
            comparator='one_of',
            error_code='COMPLIANCE_COUNTRY_DENIED',
        ),
        expected=['us', 'ca', 'gb'],
    )
