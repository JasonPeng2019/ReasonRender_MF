"""Focused tests for the reputation minimum-trust rule."""
from __future__ import annotations

from ruleforge.domain import NormalizedInput
from ruleforge.errors import DuplicateRuleError
from ruleforge.evaluator import evaluate_definition
from ruleforge.registry import RuleRegistry
from ruleforge.rules.reputation import register


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
    key = "reputation:minimum_trust:trust_score"
    rule, expected = r.get(key)
    assert rule.name == "minimum_trust"
    assert rule.domain == "reputation"
    assert rule.source_field == "trust_score"
    assert rule.comparator == "greater_than"
    assert rule.error_code == "REPUTATION_TRUST_DENIED"
    assert expected == 70.0


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


def test_allowed_when_trust_score_exceeds_minimum() -> None:
    r = _registry()
    key = "reputation:minimum_trust:trust_score"
    definition, expected = r.get(key)
    data = NormalizedInput({"trust_score": 85.0}, "sub-1", "req-1")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_trust"
    assert ev.field == "trust_score"
    assert ev.comparator == "greater_than"
    assert ev.observed == 85.0
    assert ev.detail == "comparison evaluated"


# ---------------------------------------------------------------------------
# Evaluation — rejected
# ---------------------------------------------------------------------------


def test_rejected_when_trust_score_below_minimum() -> None:
    r = _registry()
    key = "reputation:minimum_trust:trust_score"
    definition, expected = r.get(key)
    data = NormalizedInput({"trust_score": 60.0}, "sub-2", "req-2")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "REPUTATION_TRUST_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_trust"
    assert ev.field == "trust_score"
    assert ev.comparator == "greater_than"
    assert ev.observed == 60.0
    assert ev.detail == "comparison evaluated"


def test_rejected_when_field_missing() -> None:
    r = _registry()
    key = "reputation:minimum_trust:trust_score"
    definition, expected = r.get(key)
    data = NormalizedInput({}, "sub-3", "req-3")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "REPUTATION_TRUST_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_trust"
    assert ev.field == "trust_score"
    assert ev.comparator == "greater_than"
    assert ev.observed is None
    assert ev.detail == "required value is missing"


def test_rejected_when_trust_score_malformed() -> None:
    r = _registry()
    key = "reputation:minimum_trust:trust_score"
    definition, expected = r.get(key)
    data = NormalizedInput({"trust_score": "not-a-number"}, "sub-4", "req-4")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "REPUTATION_TRUST_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_trust"
    assert ev.field == "trust_score"
    assert ev.comparator == "greater_than"
    assert ev.observed == "not-a-number"
    assert ev.detail == "comparison evaluated"
