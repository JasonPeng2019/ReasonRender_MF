"""Focused tests for the delivery supported-region rule."""
from __future__ import annotations

from ruleforge.domain import NormalizedInput
from ruleforge.errors import DuplicateRuleError
from ruleforge.evaluator import evaluate_definition
from ruleforge.registry import RuleRegistry
from ruleforge.rules.delivery import register


def _registry() -> RuleRegistry:
    r = RuleRegistry()
    register(r)
    return r


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registers_without_error() -> None:
    r = RuleRegistry()
    register(r)
    key = "delivery:supported_region:delivery_region"
    rule, expected = r.get(key)
    assert rule.name == "supported_region"
    assert rule.domain == "delivery"
    assert rule.source_field == "delivery_region"
    assert rule.comparator == "equals"
    assert rule.error_code == "DELIVERY_REGION_DENIED"
    assert expected == "region-north-1"


def test_duplicate_registration_raises() -> None:
    r = RuleRegistry()
    register(r)
    try:
        register(r)
        assert False, "expected DuplicateRuleError"
    except DuplicateRuleError:
        pass


# ---------------------------------------------------------------------------
# Evaluation — allowed
# ---------------------------------------------------------------------------


def test_allowed_when_delivery_region_equals_expected() -> None:
    r = _registry()
    key = "delivery:supported_region:delivery_region"
    definition, expected = r.get(key)
    data = NormalizedInput({"delivery_region": "region-north-1"}, "sub-1", "req-1")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "supported_region"
    assert ev.field == "delivery_region"
    assert ev.comparator == "equals"


# ---------------------------------------------------------------------------
# Evaluation — rejected
# ---------------------------------------------------------------------------


def test_rejected_when_delivery_region_not_expected() -> None:
    r = _registry()
    key = "delivery:supported_region:delivery_region"
    definition, expected = r.get(key)
    data = NormalizedInput({"delivery_region": "region-south-2"}, "sub-2", "req-2")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "DELIVERY_REGION_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "supported_region"
    assert ev.field == "delivery_region"
    assert ev.comparator == "equals"
    assert ev.observed == "region-south-2"
    assert ev.detail == "comparison evaluated"


def test_rejected_when_field_missing() -> None:
    r = _registry()
    key = "delivery:supported_region:delivery_region"
    definition, expected = r.get(key)
    data = NormalizedInput({}, "sub-3", "req-3")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "DELIVERY_REGION_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "supported_region"
    assert ev.field == "delivery_region"
    assert ev.comparator == "equals"
    assert ev.observed is None
    assert ev.detail == "required value is missing"
