"""Security policy rule evaluation."""
from __future__ import annotations

from ..domain import Decision, NormalizedInput, RuleDefinition
from ..evaluator import evaluate_all
from ..policy_catalog import profile
from ..registry import RuleRegistry


SECURITY_PROFILE = profile("security.required_access_level")
SECURITY_RULE = RuleDefinition(
    name=SECURITY_PROFILE.name,
    domain=SECURITY_PROFILE.domain,
    source_field=SECURITY_PROFILE.source_field,
    comparator=SECURITY_PROFILE.comparator,
    error_code=SECURITY_PROFILE.error_code,
)
SECURITY_REGISTRY = RuleRegistry()
SECURITY_REGISTRY.register(SECURITY_RULE, SECURITY_PROFILE.expected)


def evaluate_security(data: NormalizedInput) -> Decision:
    """Evaluate the registered security access-level rule."""
    return evaluate_all(SECURITY_REGISTRY.all(), data)
