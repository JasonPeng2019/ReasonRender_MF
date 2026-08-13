"""Identity policy: approved_domain rule restricts email_domain."""
from __future__ import annotations

from ruleforge.registry import RuleRegistry
from ruleforge.rules.base import definition


def register(registry: RuleRegistry) -> None:
    registry.register(
        definition(
            domain="identity",
            name="approved_domain",
            source_field="email_domain",
            comparator="one_of",
            error_code="IDENTITY_DOMAIN_DENIED",
        ),
        expected=["example.com", "acmecorp.com"],
    )
