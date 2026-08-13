"""Market policy definitions."""
from __future__ import annotations

from ruleforge.domain import Decision, NormalizedInput
from ruleforge.evaluator import evaluate_all
from ruleforge.registry import RuleRegistry
from ruleforge.rules.base import definition


MARKETS_CODE_DENIED = "MARKETS_CODE_DENIED"
APPROVED_MARKETS = ("north", "west", "central")
approved_market = definition(
    domain="markets",
    name="approved_market",
    source_field="market_code",
    comparator="one_of",
    error_code=MARKETS_CODE_DENIED,
)
registry = RuleRegistry()
registry.register(approved_market, APPROVED_MARKETS)


def evaluate_markets(data: NormalizedInput) -> Decision:
    return evaluate_all(registry.all(), data)


evaluate = evaluate_markets
