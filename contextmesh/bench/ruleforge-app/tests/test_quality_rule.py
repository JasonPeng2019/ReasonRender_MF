"""Focused tests for the quality minimum-score rule."""
from __future__ import annotations

from ruleforge.domain import NormalizedInput
from ruleforge.errors import DuplicateRuleError
from ruleforge.evaluator import evaluate_definition
from ruleforge.registry import RuleRegistry
from ruleforge.rules.quality import register


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
    key = "quality:minimum_quality:quality_score"
    rule, expected = r.get(key)
    assert rule.name == "minimum_quality"
    assert rule.domain == "quality"
    assert rule.source_field == "quality_score"
    assert rule.comparator == "greater_than"
    assert rule.error_code == "QUALITY_SCORE_DENIED"
    assert expected == 90.0


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


def test_allowed_when_quality_score_exceeds_minimum() -> None:
    r = _registry()
    key = "quality:minimum_quality:quality_score"
    definition, expected = r.get(key)
    data = NormalizedInput({"quality_score": 95.0}, "sub-1", "req-1")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_quality"
    assert ev.field == "quality_score"
    assert ev.comparator == "greater_than"
    assert ev.observed == 95.0
    assert ev.detail == "comparison evaluated"


# ---------------------------------------------------------------------------
# Evaluation — rejected
# ---------------------------------------------------------------------------


def test_rejected_when_quality_score_at_threshold() -> None:
    r = _registry()
    key = "quality:minimum_quality:quality_score"
    definition, expected = r.get(key)
    data = NormalizedInput({"quality_score": 90.0}, "sub-2", "req-2")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "QUALITY_SCORE_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_quality"
    assert ev.field == "quality_score"
    assert ev.comparator == "greater_than"
    assert ev.observed == 90.0
    assert ev.detail == "comparison evaluated"


def test_rejected_when_quality_score_below_minimum() -> None:
    r = _registry()
    key = "quality:minimum_quality:quality_score"
    definition, expected = r.get(key)
    data = NormalizedInput({"quality_score": 85.0}, "sub-3", "req-3")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "QUALITY_SCORE_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_quality"
    assert ev.field == "quality_score"
    assert ev.comparator == "greater_than"
    assert ev.observed == 85.0
    assert ev.detail == "comparison evaluated"


def test_rejected_when_field_missing() -> None:
    r = _registry()
    key = "quality:minimum_quality:quality_score"
    definition, expected = r.get(key)
    data = NormalizedInput({}, "sub-4", "req-4")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "QUALITY_SCORE_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_quality"
    assert ev.field == "quality_score"
    assert ev.comparator == "greater_than"
    assert ev.observed is None
    assert ev.detail == "required value is missing"


def test_rejected_when_quality_score_malformed() -> None:
    r = _registry()
    key = "quality:minimum_quality:quality_score"
    definition, expected = r.get(key)
    data = NormalizedInput({"quality_score": "not-a-number"}, "sub-5", "req-5")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "QUALITY_SCORE_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "minimum_quality"
    assert ev.field == "quality_score"
    assert ev.comparator == "greater_than"
    assert ev.observed == "not-a-number"
    assert ev.detail == "comparison evaluated"
