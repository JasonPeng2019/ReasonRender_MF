"""Focused tests for the retention minimum-account-age rule."""
from __future__ import annotations

from ruleforge.domain import NormalizedInput
from ruleforge.errors import DuplicateRuleError
from ruleforge.evaluator import evaluate_definition
from ruleforge.registry import RuleRegistry
from ruleforge.rules.retention import register


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
    key = "retention:minimum_retention_age:account_age_days"
    rule, expected = r.get(key)
    assert rule.name == "minimum_retention_age"
    assert rule.domain == "retention"
    assert rule.source_field == "account_age_days"
    assert rule.comparator == "at_least"
    assert rule.error_code == "RETENTION_AGE_DENIED"
    assert expected == 30.0


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


def test_allowed_when_account_age_at_least_threshold() -> None:
    r = _registry()
    key = "retention:minimum_retention_age:account_age_days"
    definition, expected = r.get(key)
    data = NormalizedInput({"account_age_days": 30.0}, "sub-1", "req-1")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_retention_age"
    assert ev.field == "account_age_days"
    assert ev.comparator == "at_least"
    assert ev.observed == 30.0
    assert ev.detail == "comparison evaluated"


def test_allowed_when_account_age_exceeds_threshold() -> None:
    r = _registry()
    key = "retention:minimum_retention_age:account_age_days"
    definition, expected = r.get(key)
    data = NormalizedInput({"account_age_days": 45.0}, "sub-1", "req-1")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None


# ---------------------------------------------------------------------------
# Evaluation — rejected
# ---------------------------------------------------------------------------


def test_rejected_when_account_age_below_threshold() -> None:
    r = _registry()
    key = "retention:minimum_retention_age:account_age_days"
    definition, expected = r.get(key)
    data = NormalizedInput({"account_age_days": 15.0}, "sub-2", "req-2")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "RETENTION_AGE_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_retention_age"
    assert ev.field == "account_age_days"
    assert ev.comparator == "at_least"
    assert ev.observed == 15.0
    assert ev.detail == "comparison evaluated"


def test_rejected_when_field_missing() -> None:
    r = _registry()
    key = "retention:minimum_retention_age:account_age_days"
    definition, expected = r.get(key)
    data = NormalizedInput({}, "sub-3", "req-3")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "RETENTION_AGE_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_retention_age"
    assert ev.field == "account_age_days"
    assert ev.comparator == "at_least"
    assert ev.observed is None
    assert ev.detail == "required value is missing"


def test_rejected_when_account_age_malformed() -> None:
    r = _registry()
    key = "retention:minimum_retention_age:account_age_days"
    definition, expected = r.get(key)
    data = NormalizedInput({"account_age_days": "not-a-number"}, "sub-4", "req-4")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "RETENTION_AGE_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_retention_age"
    assert ev.field == "account_age_days"
    assert ev.comparator == "at_least"
    assert ev.observed == "not-a-number"
    assert ev.detail == "comparison evaluated"
