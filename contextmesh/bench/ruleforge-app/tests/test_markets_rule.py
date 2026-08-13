from ruleforge.domain import NormalizedInput
from ruleforge.rules.markets import MARKETS_CODE_DENIED, evaluate_markets


def test_approved_markets_allow() -> None:
    assert all(evaluate_markets(NormalizedInput({"market_code": value}, "subject", "request")).allowed for value in ("north", "west", "central"))


def test_denied_market_rejects() -> None:
    assert evaluate_markets(NormalizedInput({"market_code": "south"}, "subject", "request")).code == MARKETS_CODE_DENIED


def test_missing_market_code_rejects() -> None:
    assert evaluate_markets(NormalizedInput({}, "subject", "request")).code == MARKETS_CODE_DENIED
