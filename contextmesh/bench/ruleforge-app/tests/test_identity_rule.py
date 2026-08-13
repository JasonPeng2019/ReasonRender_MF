"""Focused tests for the identity approved-domain rule."""
from __future__ import annotations

from ruleforge.domain import NormalizedInput
from ruleforge.errors import DuplicateRuleError
from ruleforge.evaluator import evaluate_definition
from ruleforge.registry import RuleRegistry
from ruleforge.rules.identity import register


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
    key = "identity:approved_domain:email_domain"
    rule, expected = r.get(key)
    assert rule.name == "approved_domain"
    assert rule.domain == "identity"
    assert rule.source_field == "email_domain"
    assert rule.comparator == "one_of"
    assert rule.error_code == "IDENTITY_DOMAIN_DENIED"
    assert expected == ["example.com", "acmecorp.com"]


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


def test_allowed_when_email_domain_is_approved() -> None:
    r = _registry()
    key = "identity:approved_domain:email_domain"
    definition, expected = r.get(key)
    data = NormalizedInput({"email_domain": "example.com"}, "sub-1", "req-1")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "approved_domain"
    assert ev.field == "email_domain"
    assert ev.comparator == "one_of"


def test_allowed_when_email_domain_is_approved_case_insensitive() -> None:
    r = _registry()
    key = "identity:approved_domain:email_domain"
    definition, expected = r.get(key)
    data = NormalizedInput({"email_domain": "ACMECORP.COM"}, "sub-1", "req-1")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "approved_domain"


def test_allowed_when_second_approved_domain() -> None:
    r = _registry()
    key = "identity:approved_domain:email_domain"
    definition, expected = r.get(key)
    data = NormalizedInput({"email_domain": "acmecorp.com"}, "sub-3", "req-3")
    decision = evaluate_definition(definition, expected, data)
    assert decision.allowed
    assert decision.code is None


# ---------------------------------------------------------------------------
# Evaluation — rejected
# ---------------------------------------------------------------------------


def test_rejected_when_email_domain_not_approved() -> None:
    r = _registry()
    key = "identity:approved_domain:email_domain"
    definition, expected = r.get(key)
    data = NormalizedInput({"email_domain": "evil.com"}, "sub-4", "req-4")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "IDENTITY_DOMAIN_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "approved_domain"
    assert ev.field == "email_domain"
    assert ev.comparator == "one_of"
    assert ev.observed == "evil.com"
    assert ev.detail == "comparison evaluated"


def test_rejected_when_field_missing() -> None:
    r = _registry()
    key = "identity:approved_domain:email_domain"
    definition, expected = r.get(key)
    data = NormalizedInput({}, "sub-5", "req-5")
    decision = evaluate_definition(definition, expected, data)
    assert not decision.allowed
    assert decision.code == "IDENTITY_DOMAIN_DENIED"
    assert len(decision.evidence) == 1
    ev = decision.evidence[0]
    assert ev.rule_name == "approved_domain"
    assert ev.field == "email_domain"
    assert ev.comparator == "one_of"
    assert ev.observed is None
    assert ev.detail == "required value is missing"
