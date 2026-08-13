"""Focused tests for the payments approved-currency rule."""
from __future__ import annotations

from ruleforge.domain import NormalizedInput
from ruleforge.errors import DuplicateRuleError
from ruleforge.evaluator import evaluate_definition
from ruleforge.registry import RuleRegistry
from ruleforge.rules.payments import register


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
    key = "payments:approved_currency:currency"
    rule, expected = r.get(key)
    assert rule.name == "approved_currency"
    assert rule.domain == "payments"
    assert rule.source_field == "currency"
    assert rule.comparator == "one_of"
    assert rule.error_code == "PAYMENTS_CURRENCY_DENIED"
    assert expected == ["usd", "cad", "eur"]


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


def test_allowed_when_currency_is_approved() -> None:
    r = _registry()
    key = "payments:approved_currency:currency"
    definition, expected = r.get(key)
    data = NormalizedInput({"currency": "usd"}, "sub-1", "req-1")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "approved_currency"
    assert ev.field == "currency"
    assert ev.comparator == "one_of"


def test_allowed_when_currency_is_approved_case_insensitive() -> None:
    r = _registry()
    key = "payments:approved_currency:currency"
    definition, expected = r.get(key)
    data = NormalizedInput({"currency": "EUR"}, "sub-2", "req-2")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "approved_currency"


def test_allowed_when_second_approved_currency() -> None:
    r = _registry()
    key = "payments:approved_currency:currency"
    definition, expected = r.get(key)
    data = NormalizedInput({"currency": "cad"}, "sub-3", "req-3")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None


# ---------------------------------------------------------------------------
# Evaluation — rejected
# ---------------------------------------------------------------------------


def test_rejected_when_currency_not_approved() -> None:
    r = _registry()
    key = "payments:approved_currency:currency"
    definition, expected = r.get(key)
    data = NormalizedInput({"currency": "jpy"}, "sub-4", "req-4")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "PAYMENTS_CURRENCY_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "approved_currency"
    assert ev.field == "currency"
    assert ev.comparator == "one_of"
    assert ev.observed == "jpy"
    assert ev.detail == "comparison evaluated"


def test_rejected_when_field_missing() -> None:
    r = _registry()
    key = "payments:approved_currency:currency"
    definition, expected = r.get(key)
    data = NormalizedInput({}, "sub-5", "req-5")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "PAYMENTS_CURRENCY_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "approved_currency"
    assert ev.field == "currency"
    assert ev.comparator == "one_of"
    assert ev.observed is None
    assert ev.detail == "required value is missing"
