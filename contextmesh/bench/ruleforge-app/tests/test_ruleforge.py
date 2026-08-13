from ruleforge.domain import RuleDefinition
from ruleforge.registry import RuleRegistry
from ruleforge.service import PolicyService


def test_registry_policy_service_rejects_missing_field() -> None:
    registry = RuleRegistry()
    registry.register(RuleDefinition("minimum_invoice", "billing", "invoice_total", "at_least", "BILLING_MINIMUM_NOT_MET"), 100)
    decision = PolicyService(registry).evaluate({}, "subject", "request")
    assert decision.allowed is False
    assert decision.code == "BILLING_MINIMUM_NOT_MET"
