"""Focused tests for the billing minimum-invoice rule."""
from __future__ import annotations

from ruleforge.domain import NormalizedInput
from ruleforge.errors import DuplicateRuleError
from ruleforge.evaluator import evaluate_definition
from ruleforge.registry import RuleRegistry
from ruleforge.rules.billing import register


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
    key = "billing:minimum_invoice:invoice_total"
    rule, expected = r.get(key)
    assert rule.name == "minimum_invoice"
    assert rule.domain == "billing"
    assert rule.source_field == "invoice_total"
    assert rule.comparator == "at_least"
    assert rule.error_code == "BILLING_MINIMUM_NOT_MET"
    assert expected == 100.0


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


def test_allowed_when_invoice_total_meets_minimum() -> None:
    r = _registry()
    key = "billing:minimum_invoice:invoice_total"
    definition, expected = r.get(key)
    data = NormalizedInput({"invoice_total": 150.0}, "sub-1", "req-1")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_invoice"
    assert ev.field == "invoice_total"
    assert ev.comparator == "at_least"


# ---------------------------------------------------------------------------
# Evaluation — rejected
# ---------------------------------------------------------------------------


def test_rejected_when_invoice_total_below_minimum() -> None:
    r = _registry()
    key = "billing:minimum_invoice:invoice_total"
    definition, expected = r.get(key)
    data = NormalizedInput({"invoice_total": 50.0}, "sub-2", "req-2")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "BILLING_MINIMUM_NOT_MET"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_invoice"
    assert ev.field == "invoice_total"
    assert ev.comparator == "at_least"
    assert ev.observed == 50.0
    assert ev.detail == "comparison evaluated"


def test_rejected_when_field_missing() -> None:
    r = _registry()
    key = "billing:minimum_invoice:invoice_total"
    definition, expected = r.get(key)
    data = NormalizedInput({}, "sub-3", "req-3")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "BILLING_MINIMUM_NOT_MET"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_invoice"
    assert ev.field == "invoice_total"
    assert ev.comparator == "at_least"
    assert ev.observed is None
    assert ev.detail == "required value is missing"
