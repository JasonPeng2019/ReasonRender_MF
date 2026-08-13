"""Quality policy module — minimum quality score evaluation."""
from __future__ import annotations

from ruleforge.registry import RuleRegistry
from ruleforge.rules.base import definition


def register(registry: RuleRegistry) -> None:
    """Register the minimum-quality policy."""
    registry.register(
        definition(
            domain='quality',
            name='minimum_quality',
            source_field='quality_score',
            comparator='greater_than',
            error_code='QUALITY_SCORE_DENIED',
        ),
        expected=90.0,
    )
