"""Shared helpers for policy modules."""
from __future__ import annotations

from ruleforge.domain import RuleDefinition


def definition(domain: str, name: str, source_field: str, comparator: str, error_code: str) -> RuleDefinition:
    return RuleDefinition(name=name, domain=domain, source_field=source_field, comparator=comparator, error_code=error_code)
