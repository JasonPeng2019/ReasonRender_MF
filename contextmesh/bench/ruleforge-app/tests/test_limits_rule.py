from ruleforge.domain import NormalizedInput
from ruleforge.rules.limits import evaluate


def _input(**values: object) -> NormalizedInput:
    return NormalizedInput(values=values, subject_id="subject-1", request_id="request-1")


def test_at_threshold_allows() -> None:
    assert evaluate(_input(daily_requests=100)).allowed


def test_below_threshold_rejects_with_limits_code() -> None:
    assert evaluate(_input(daily_requests=99)).code == "LIMITS_REQUEST_DENIED"


def test_missing_daily_requests_rejects() -> None:
    assert evaluate(_input()).code == "LIMITS_REQUEST_DENIED"
