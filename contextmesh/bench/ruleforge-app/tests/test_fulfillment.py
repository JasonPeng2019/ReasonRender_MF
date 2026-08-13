"""Focused tests for the fulfillment allowed-destination rule."""
from __future__ import annotations

from ruleforge.domain import NormalizedInput
from ruleforge.errors import DuplicateRuleError
from ruleforge.evaluator import evaluate_definition
from ruleforge.registry import RuleRegistry
from ruleforge.rules.fulfillment import register


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
    key = "fulfillment:allowed_destination:shipment_country"
    rule, expected = r.get(key)
    assert rule.name == "allowed_destination"
    assert rule.domain == "fulfillment"
    assert rule.source_field == "shipment_country"
    assert rule.comparator == "equals"
    assert rule.error_code == "FULFILLMENT_DESTINATION_DENIED"
    assert expected == "US"


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


def test_allowed_when_shipment_country_equals_us() -> None:
    r = _registry()
    key = "fulfillment:allowed_destination:shipment_country"
    definition, expected = r.get(key)
    data = NormalizedInput({"shipment_country": "US"}, "sub-1", "req-1")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "allowed_destination"
    assert ev.field == "shipment_country"
    assert ev.comparator == "equals"


# ---------------------------------------------------------------------------
# Evaluation — rejected
# ---------------------------------------------------------------------------


def test_rejected_when_shipment_country_not_us() -> None:
    r = _registry()
    key = "fulfillment:allowed_destination:shipment_country"
    definition, expected = r.get(key)
    data = NormalizedInput({"shipment_country": "CA"}, "sub-2", "req-2")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "FULFILLMENT_DESTINATION_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "allowed_destination"
    assert ev.field == "shipment_country"
    assert ev.comparator == "equals"
    assert ev.observed == "CA"
    assert ev.detail == "comparison evaluated"


def test_rejected_when_field_missing() -> None:
    r = _registry()
    key = "fulfillment:allowed_destination:shipment_country"
    definition, expected = r.get(key)
    data = NormalizedInput({}, "sub-3", "req-3")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "FULFILLMENT_DESTINATION_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "allowed_destination"
    assert ev.field == "shipment_country"
    assert ev.comparator == "equals"
    assert ev.observed is None
    assert ev.detail == "required value is missing"
