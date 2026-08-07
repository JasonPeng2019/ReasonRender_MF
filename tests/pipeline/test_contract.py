from rrc.contract import (
    ArmMode,
    BranchDecision,
    Config,
    NullRetrieval,
    SolveOutcome,
    Solver,
    StoreFailure,
)
from rrc.pipeline import solve

from tests.pipeline.helpers import make_task


def test_public_solver_has_the_frozen_callable_seam() -> None:
    solver: Solver = solve
    assert callable(solver)
    assert Config().repair_cap_N == 1


def test_store_failure_retains_completed_outcome() -> None:
    outcome = SolveOutcome(
        task_id="t",
        arm="warm",
        code="pass",
        passed=True,
        pass_at_1=True,
        branch=BranchDecision.MISS,
        repairs=0,
        escalated=False,
        template=None,
        cost_events=(),
    )
    failure = StoreFailure(outcome)
    assert failure.outcome is outcome


def test_null_retrieval_is_a_typed_no_op() -> None:
    retrieval = NullRetrieval()
    assert retrieval.retrieve(make_task(), Config()) == []
    assert retrieval.get_template("missing") is None


def test_only_cold_and_warm_are_lane_a_modes() -> None:
    assert ArmMode.COLD.value == "cold"
    assert ArmMode.WARM.value == "warm"
