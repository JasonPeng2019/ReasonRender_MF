"""Focused tests for the eligibility minimum-age rule."""
from __future__ import annotations

from ruleforge.domain import NormalizedInput
from ruleforge.errors import DuplicateRuleError
from ruleforge.evaluator import evaluate_definition
from ruleforge.registry import RuleRegistry
from ruleforge.rules.eligibility import register


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
    key = "eligibility:minimum_age:age_years"
    rule, expected = r.get(key)
    assert rule.name == "minimum_age"
    assert rule.domain == "eligibility"
    assert rule.source_field == "age_years"
    assert rule.comparator == "at_least"
    assert rule.error_code == "ELIGIBILITY_AGE_DENIED"
    assert expected == 18.0


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


def test_allowed_when_age_meets_minimum() -> None:
    r = _registry()
    key = "eligibility:minimum_age:age_years"
    definition, expected = r.get(key)
    data = NormalizedInput({"age_years": 25.0}, "sub-1", "req-1")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_age"
    assert ev.field == "age_years"
    assert ev.comparator == "at_least"


def test_allowed_when_age_exactly_minimum() -> None:
    r = _registry()
    key = "eligibility:minimum_age:age_years"
    definition, expected = r.get(key)
    data = NormalizedInput({"age_years": 18.0}, "sub-2", "req-2")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_age"
    assert ev.field == "age_years"
    assert ev.comparator == "at_least"


# ---------------------------------------------------------------------------
# Evaluation — rejected
# ---------------------------------------------------------------------------


def test_rejected_when_age_below_minimum() -> None:
    r = _registry()
    key = "eligibility:minimum_age:age_years"
    definition, expected = r.get(key)
    data = NormalizedInput({"age_years": 16.0}, "sub-3", "req-3")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "ELIGIBILITY_AGE_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_age"
    assert ev.field == "age_years"
    assert ev.comparator == "at_least"
    assert ev.observed == 16.0
    assert ev.detail == "comparison evaluated"


def test_rejected_when_field_missing() -> None:
    r = _registry()
    key = "eligibility:minimum_age:age_years"
    definition, expected = r.get(key)
    data = NormalizedInput({}, "sub-4", "req-4")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "ELIGIBILITY_AGE_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_age"
    assert ev.field == "age_years"
    assert ev.comparator == "at_least"
    assert ev.observed is None
    assert ev.detail == "required value is missing"


def test_rejected_when_age_malformed() -> None:
    r = _registry()
    key = "eligibility:minimum_age:age_years"
    definition, expected = r.get(key)
    data = NormalizedInput({"age_years": "not-a-number"}, "sub-5", "req-5")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "ELIGIBILITY_AGE_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_age"
    assert ev.field == "age_years"
    assert ev.comparator == "at_least"
    assert ev.observed == "not-a-number"
    assert ev.detail == "comparison evaluated"
