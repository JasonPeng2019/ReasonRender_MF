from ruleforge.domain import NormalizedInput
from ruleforge.rules.security import evaluate_security


def test_matching_access_level_is_allowed() -> None:
    assert evaluate_security(NormalizedInput({"access_level": "internal"}, "subject-1", "request-1")).allowed


def test_nonmatching_access_level_is_denied() -> None:
    assert evaluate_security(NormalizedInput({"access_level": "public"}, "subject-1", "request-1")).code == "SECURITY_ACCESS_DENIED"


def test_missing_access_level_is_denied() -> None:
    assert evaluate_security(NormalizedInput({}, "subject-1", "request-1")).code == "SECURITY_ACCESS_DENIED"
