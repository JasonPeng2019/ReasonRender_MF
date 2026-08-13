"""Routing policy module — preferred tier evaluation."""
from __future__ import annotations

from ruleforge.registry import RuleRegistry
from ruleforge.rules.base import definition


def register(registry: RuleRegistry) -> None:
    """Register the preferred-tier routing policy."""
    registry.register(
        definition(
            domain='routing',
            name='preferred_tier',
            source_field='route_tier',
            comparator='equals',
            error_code='ROUTING_TIER_DENIED',
        ),
        expected='priority',
    )
