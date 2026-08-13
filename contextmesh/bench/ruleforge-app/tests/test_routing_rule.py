"""Focused tests for the preferred-tier routing rule."""
from __future__ import annotations

from ruleforge.domain import NormalizedInput
from ruleforge.evaluator import evaluate_definition
from ruleforge.registry import RuleRegistry
from ruleforge.rules.routing import register


def _registry() -> RuleRegistry:
    reg = RuleRegistry()
    register(reg)
    return reg


class TestPreferredTierRegistration:
    def test_register_success(self) -> None:
        registry = _registry()
        definition, expected = registry.get("routing:preferred_tier:route_tier")
        assert definition.name == "preferred_tier"
        assert definition.domain == "routing"
        assert definition.source_field == "route_tier"
        assert definition.comparator == "equals"
        assert definition.error_code == "ROUTING_TIER_DENIED"
        assert expected == "priority"


class TestPreferredTierEvaluation:
    def test_allowed_when_tier_is_priority(self) -> None:
        registry = _registry()
        definition, expected = registry.get("routing:preferred_tier:route_tier")
        data = NormalizedInput(values={"route_tier": "priority"}, subject_id="s1", request_id="r1")
        decision = evaluate_definition(definition, expected, data)
        assert decision.allowed is True

    def test_rejected_when_tier_not_priority(self) -> None:
        registry = _registry()
        definition, expected = registry.get("routing:preferred_tier:route_tier")
        data = NormalizedInput(values={"route_tier": "standard"}, subject_id="s1", request_id="r1")
        decision = evaluate_definition(definition, expected, data)
        assert decision.allowed is False
        assert decision.code == "ROUTING_TIER_DENIED"

    def test_rejected_when_tier_missing(self) -> None:
        registry = _registry()
        definition, expected = registry.get("routing:preferred_tier:route_tier")
        data = NormalizedInput(values={}, subject_id="s1", request_id="r1")
        decision = evaluate_definition(definition, expected, data)
        assert decision.allowed is False
        assert decision.code == "ROUTING_TIER_DENIED"

    def test_rejected_when_tier_malformed(self) -> None:
        registry = _registry()
        definition, expected = registry.get("routing:preferred_tier:route_tier")
        data = NormalizedInput(values={"route_tier": 42}, subject_id="s1", request_id="r1")
        decision = evaluate_definition(definition, expected, data)
        assert decision.allowed is False
        assert decision.code == "ROUTING_TIER_DENIED"
