from ruleforge.domain import NormalizedInput
from ruleforge.rules.assurance import evaluate


def _input(score: object | None = None) -> NormalizedInput:
    values = {} if score is None else {"assurance_score": score}
    return NormalizedInput(values=values, subject_id="subject-1", request_id="request-1")


def test_score_above_95_allows() -> None:
    assert evaluate(_input(95.1)).allowed


def test_score_at_or_below_95_is_denied() -> None:
    assert all(evaluate(_input(score)).code == "ASSURANCE_SCORE_DENIED" for score in (95.0, 94.9))


def test_missing_score_is_denied() -> None:
    assert evaluate(_input()).code == "ASSURANCE_SCORE_DENIED"
