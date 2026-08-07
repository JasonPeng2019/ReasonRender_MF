import importlib
from dataclasses import replace

import pytest
from rrc.contract import (
    ArmMode,
    BranchDecision,
    Candidate,
    Config,
    ModelRole,
    StoreFailure,
    Task,
    Template,
)
from rrc.pipeline import solve
from rrc.pipeline.stubs import FakeModel, InMemoryRetrieval
from rrc.pipeline.template import templatize

from tests.pipeline.helpers import (
    broken_implementation,
    implementation,
    make_spec,
    make_task,
    spec_json,
)


class ExplodingRetrieval(InMemoryRetrieval):
    def retrieve(self, task: Task, cfg: Config):  # type: ignore[no-untyped-def]
        raise AssertionError("cold arm retrieved")

    def get_template(self, external_ref: str):  # type: ignore[no-untyped-def]
        raise AssertionError("cold arm resolved")

    def store(self, task: Task, template, outcome):  # type: ignore[no-untyped-def]
        raise AssertionError("cold arm stored")


def test_cold_direct_success_never_touches_retrieval_and_returns_template() -> None:
    task = make_task(oracle_tests="def test_oracle(): assert get_order(4) == 4")
    model = FakeModel({"spec": [spec_json()], "implement": [implementation()]})
    outcome = solve(
        task,
        mode=ArmMode.COLD,
        model=model,
        retrieval=ExplodingRetrieval(),
        cfg=Config(),
    )
    assert outcome.passed is True
    assert outcome.pass_at_1 is True
    assert outcome.arm == "cold"
    assert outcome.branch is BranchDecision.MISS
    assert outcome.repairs == 0
    assert outcome.escalated is False
    assert outcome.code == implementation()
    assert outcome.template is not None
    assert [event.stage for event in outcome.cost_events] == ["spec", "implement"]
    assert [call[0] for call in model.calls] == [ModelRole.STRONG, ModelRole.SMALL]


@pytest.mark.parametrize(
    ("cap", "stages", "passed"),
    [(9, ["spec", "implement", "repair"], True), (0, ["spec", "implement"], False)],
)
def test_fresh_miss_honors_zero_or_one_repair(cap: int, stages: list[str], passed: bool) -> None:
    responses: dict[str, list[str]] = {
        "spec": [spec_json()],
        "implement": [broken_implementation()],
        "repair": [implementation()],
    }
    retrieval = InMemoryRetrieval()
    outcome = solve(
        make_task(),
        mode=ArmMode.WARM,
        model=FakeModel(responses),
        retrieval=retrieval,
        cfg=Config(repair_cap_N=cap),
    )
    assert outcome.passed is passed
    assert [event.stage for event in outcome.cost_events] == stages
    assert outcome.repairs == int(cap > 0)
    assert outcome.template is not None if passed else outcome.template is None
    assert retrieval.store_calls == int(passed)
    assert outcome.branch is BranchDecision.MISS
    assert outcome.escalated is False
    assert retrieval.retrieve_calls == 1
    assert retrieval.get_calls == 0


@pytest.mark.parametrize(
    ("cap", "stages"),
    [(1, ["spec", "implement", "repair"]), (0, ["spec", "implement"])],
)
def test_cold_final_failures_honor_cap_and_never_touch_retrieval(
    cap: int, stages: list[str]
) -> None:
    responses = {
        "spec": [spec_json()],
        "implement": [broken_implementation()],
        "repair": [broken_implementation().replace("-1", "-2")],
    }
    outcome = solve(
        make_task(oracle_tests="def test_oracle(): assert get_order(1) == 1"),
        mode=ArmMode.COLD,
        model=FakeModel(responses),
        retrieval=ExplodingRetrieval(),
        cfg=Config(repair_cap_N=cap),
    )
    assert outcome.passed is False
    assert outcome.pass_at_1 is False
    assert outcome.repairs == cap
    assert outcome.template is None
    assert [event.stage for event in outcome.cost_events] == stages


def test_cold_repair_success_returns_repaired_final_code() -> None:
    oracle = "def test_oracle(): assert get_order(5) == 5"
    model = FakeModel(
        {
            "spec": [spec_json()],
            "implement": [broken_implementation()],
            "repair": [implementation()],
        }
    )
    outcome = solve(
        make_task(oracle_tests=oracle),
        mode=ArmMode.COLD,
        model=model,
        retrieval=ExplodingRetrieval(),
        cfg=Config(),
    )
    assert outcome.code == implementation()
    assert outcome.passed is True
    assert outcome.pass_at_1 is True
    assert outcome.repairs == 1
    assert outcome.escalated is False
    assert outcome.template is not None
    assert [event.stage for event in outcome.cost_events] == ["spec", "implement", "repair"]
    assert all(oracle not in call[1] for call in model.calls)


def test_cold_verifier_timeout_becomes_failed_final_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    solve_module = importlib.import_module("rrc.pipeline.solve")

    def timeout_result(
        code: str,
        tests: str | tuple[str, ...],
        timeout: float = 15,
    ) -> tuple[bool, str]:
        return False, f"pytest timed out after {timeout:g} seconds"

    monkeypatch.setattr(solve_module, "run_pytest", timeout_result)
    model = FakeModel({"spec": [spec_json()], "implement": [implementation()]})
    outcome = solve(
        make_task(),
        mode=ArmMode.COLD,
        model=model,
        retrieval=ExplodingRetrieval(),
        cfg=Config(repair_cap_N=0),
    )
    assert outcome.code == implementation()
    assert outcome.passed is False
    assert outcome.pass_at_1 is None
    assert outcome.template is None
    assert [event.stage for event in outcome.cost_events] == ["spec", "implement"]


def test_invalid_modes_and_negative_caps_fail_before_any_port_call() -> None:
    model = FakeModel({})
    retrieval = InMemoryRetrieval()
    with pytest.raises(ValueError):
        solve(
            make_task(),
            mode=ArmMode.BASELINE,
            model=model,
            retrieval=retrieval,
            cfg=Config(),
        )
    with pytest.raises(ValueError):
        solve(
            make_task(),
            mode=ArmMode.WARM,
            model=model,
            retrieval=retrieval,
            cfg=Config(repair_cap_N=-1),
        )
    assert model.calls == []
    assert retrieval.retrieve_calls == retrieval.get_calls == retrieval.store_calls == 0


def test_malformed_initial_spec_returns_empty_final_code_and_only_spec_cost() -> None:
    outcome = solve(
        make_task(oracle_tests="def test_oracle(): assert False"),
        mode=ArmMode.COLD,
        model=FakeModel({"spec": ["not json"]}),
        retrieval=ExplodingRetrieval(),
        cfg=Config(),
    )
    assert outcome.code == ""
    assert outcome.passed is False
    assert outcome.pass_at_1 is False
    assert outcome.template is None
    assert [event.stage for event in outcome.cost_events] == ["spec"]


def test_warm_exact_reuse_has_no_spec_cost_and_stores_once() -> None:
    template = templatize(make_spec())
    retrieval = InMemoryRetrieval({template.external_ref: template})
    model = FakeModel({"implement": [implementation()]})
    outcome = solve(make_task(), mode=ArmMode.WARM, model=model, retrieval=retrieval, cfg=Config())
    assert outcome.branch is BranchDecision.REUSE
    assert outcome.passed is True
    assert outcome.repairs == 0
    assert outcome.escalated is False
    assert outcome.template is template
    assert [event.stage for event in outcome.cost_events] == ["implement"]
    assert (retrieval.retrieve_calls, retrieval.get_calls, retrieval.store_calls) == (1, 1, 1)
    assert len(model.calls) == 1


def test_reuse_repair_success_does_not_escalate() -> None:
    template = templatize(make_spec())
    retrieval = InMemoryRetrieval({template.external_ref: template})
    model = FakeModel({"implement": [broken_implementation()], "repair": [implementation()]})
    outcome = solve(make_task(), mode=ArmMode.WARM, model=model, retrieval=retrieval, cfg=Config())
    assert outcome.passed is True
    assert outcome.repairs == 1
    assert outcome.escalated is False
    assert [event.stage for event in outcome.cost_events] == ["implement", "repair"]
    assert retrieval.store_calls == 1


@pytest.mark.parametrize(
    ("cap", "expected"),
    [
        (1, ["implement", "repair", "fallback_spec", "fallback_implement"]),
        (0, ["implement", "fallback_spec", "fallback_implement"]),
    ],
)
def test_failed_reuse_repairs_if_allowed_then_falls_back_once(
    cap: int, expected: list[str]
) -> None:
    template = templatize(make_spec())
    retrieval = InMemoryRetrieval({template.external_ref: template})
    responses = {
        "implement": [broken_implementation()],
        "repair": [broken_implementation()],
        "fallback_spec": [spec_json()],
        "fallback_implement": [implementation()],
    }
    oracle = "def test_oracle(): assert get_order(7) == 7"
    task = make_task(oracle_tests=oracle)
    model = FakeModel(responses)
    outcome = solve(
        task,
        mode=ArmMode.WARM,
        model=model,
        retrieval=retrieval,
        cfg=Config(repair_cap_N=cap),
    )
    assert outcome.passed is True
    assert outcome.branch is BranchDecision.REUSE
    assert outcome.escalated is True
    assert outcome.repairs == int(cap > 0)
    assert outcome.pass_at_1 is True
    assert [event.stage for event in outcome.cost_events] == expected
    expected_roles = [ModelRole.SMALL]
    if cap:
        expected_roles.append(ModelRole.SMALL)
    expected_roles.extend((ModelRole.STRONG, ModelRole.SMALL))
    assert [call[0] for call in model.calls] == expected_roles
    assert all(oracle not in call[1] for call in model.calls)
    assert all(
        (event.arm, event.task_id, event.provider, event.model, event.usage.total_tokens)
        == ("warm", task.task_id, "fake", "fake", 1)
        for event in outcome.cost_events
    )
    assert retrieval.store_calls == 1


def test_cap_zero_malformed_fallback_retains_initial_code() -> None:
    template = templatize(make_spec())
    retrieval = InMemoryRetrieval({template.external_ref: template})
    initial = broken_implementation()
    outcome = solve(
        make_task(),
        mode=ArmMode.WARM,
        model=FakeModel({"implement": [initial], "fallback_spec": ["not json"]}),
        retrieval=retrieval,
        cfg=Config(repair_cap_N=0),
    )
    assert outcome.code == initial
    assert outcome.repairs == 0
    assert outcome.escalated is True
    assert outcome.template is None
    assert [event.stage for event in outcome.cost_events] == ["implement", "fallback_spec"]
    assert retrieval.store_calls == 0


def test_valid_fallback_implementation_failure_is_final_and_not_stored() -> None:
    template = templatize(make_spec())
    retrieval = InMemoryRetrieval({template.external_ref: template})
    fallback_code = broken_implementation().replace("-1", "-3")
    model = FakeModel(
        {
            "implement": [broken_implementation()],
            "repair": [broken_implementation().replace("-1", "-2")],
            "fallback_spec": [spec_json()],
            "fallback_implement": [fallback_code],
        }
    )
    outcome = solve(make_task(), mode=ArmMode.WARM, model=model, retrieval=retrieval, cfg=Config())
    assert outcome.code == fallback_code
    assert outcome.passed is False
    assert outcome.template is None
    assert [event.stage for event in outcome.cost_events] == [
        "implement",
        "repair",
        "fallback_spec",
        "fallback_implement",
    ]
    assert retrieval.store_calls == 0


def test_malformed_fallback_retains_last_reuse_code_and_does_not_store() -> None:
    template = templatize(make_spec())
    retrieval = InMemoryRetrieval({template.external_ref: template})
    repaired = broken_implementation().replace("-1", "-2")
    model = FakeModel(
        {
            "implement": [broken_implementation()],
            "repair": [repaired],
            "fallback_spec": ["not json"],
        }
    )
    outcome = solve(make_task(), mode=ArmMode.WARM, model=model, retrieval=retrieval, cfg=Config())
    assert outcome.code == repaired
    assert outcome.passed is False
    assert outcome.template is None
    assert retrieval.store_calls == 0
    assert [event.stage for event in outcome.cost_events] == [
        "implement",
        "repair",
        "fallback_spec",
    ]


def test_stale_or_structurally_wrong_candidate_becomes_fresh_miss() -> None:
    template = templatize(make_spec())
    corrupt = replace(template, external_ref="0" * 64)
    retrieval = InMemoryRetrieval({corrupt.external_ref: corrupt})
    model = FakeModel({"spec": [spec_json()], "implement": [implementation()]})
    outcome = solve(make_task(), mode=ArmMode.WARM, model=model, retrieval=retrieval, cfg=Config())
    assert outcome.branch is BranchDecision.MISS
    assert [event.stage for event in outcome.cost_events] == ["spec", "implement"]


def test_candidate_with_missing_own_store_row_becomes_fresh_miss() -> None:
    class StaleRetrieval(InMemoryRetrieval):
        def retrieve(self, task: Task, cfg: Config) -> list[Candidate]:
            self.retrieve_calls += 1
            return [Candidate("missing", 1.0)]

    retrieval = StaleRetrieval()
    model = FakeModel({"spec": [spec_json()], "implement": [implementation()]})
    outcome = solve(make_task(), mode=ArmMode.WARM, model=model, retrieval=retrieval, cfg=Config())
    assert outcome.branch is BranchDecision.MISS
    assert outcome.passed is True
    assert (retrieval.retrieve_calls, retrieval.get_calls, retrieval.store_calls) == (1, 1, 1)


def test_malformed_nested_candidate_becomes_fresh_miss_without_escaping() -> None:
    malformed = Template("bad", None, ("entity",))  # type: ignore[arg-type]
    retrieval = InMemoryRetrieval({"bad": malformed})
    model = FakeModel({"spec": [spec_json()], "implement": [implementation()]})
    outcome = solve(make_task(), mode=ArmMode.WARM, model=model, retrieval=retrieval, cfg=Config())
    assert outcome.branch is BranchDecision.MISS
    assert outcome.passed is True


def test_store_error_wraps_the_exact_completed_outcome_and_cause() -> None:
    cause = OSError("disk full")
    retrieval = InMemoryRetrieval(store_error=cause)
    model = FakeModel({"spec": [spec_json()], "implement": [implementation()]})
    with pytest.raises(StoreFailure) as caught:
        solve(make_task(), mode=ArmMode.WARM, model=model, retrieval=retrieval, cfg=Config())
    assert caught.value.__cause__ is cause
    assert caught.value.outcome.passed is True
    assert [event.stage for event in caught.value.outcome.cost_events] == ["spec", "implement"]


def test_store_error_after_direct_reuse_preserves_reuse_outcome() -> None:
    template = templatize(make_spec())
    cause = OSError("index unavailable")
    retrieval = InMemoryRetrieval(
        templates={template.external_ref: template},
        store_error=cause,
    )
    with pytest.raises(StoreFailure) as caught:
        solve(
            make_task(),
            mode=ArmMode.WARM,
            model=FakeModel({"implement": [implementation()]}),
            retrieval=retrieval,
            cfg=Config(),
        )
    outcome = caught.value.outcome
    assert caught.value.__cause__ is cause
    assert outcome.branch is BranchDecision.REUSE
    assert outcome.passed is True
    assert outcome.escalated is False
    assert outcome.template is template
    assert [event.stage for event in outcome.cost_events] == ["implement"]
    assert (retrieval.retrieve_calls, retrieval.get_calls, retrieval.store_calls) == (1, 1, 1)


def test_store_error_after_fallback_preserves_all_cost_events() -> None:
    template = templatize(make_spec())
    cause = OSError("index unavailable")
    retrieval = InMemoryRetrieval(
        templates={template.external_ref: template},
        store_error=cause,
    )
    model = FakeModel(
        {
            "implement": [broken_implementation()],
            "repair": [broken_implementation()],
            "fallback_spec": [spec_json()],
            "fallback_implement": [implementation()],
        }
    )
    with pytest.raises(StoreFailure) as caught:
        solve(
            make_task(),
            mode=ArmMode.WARM,
            model=model,
            retrieval=retrieval,
            cfg=Config(),
        )
    outcome = caught.value.outcome
    assert caught.value.__cause__ is cause
    assert outcome.branch is BranchDecision.REUSE
    assert outcome.passed is True
    assert outcome.repairs == 1
    assert outcome.escalated is True
    assert outcome.template is not None
    assert [event.stage for event in outcome.cost_events] == [
        "implement",
        "repair",
        "fallback_spec",
        "fallback_implement",
    ]
    assert (retrieval.retrieve_calls, retrieval.get_calls, retrieval.store_calls) == (1, 1, 1)


def test_two_task_stream_misses_then_reuses_same_template_without_spec() -> None:
    first = make_task(oracle_tests="def test_oracle(): assert get_order(8) == 8")
    second = make_task(
        task_id="purchase-2",
        entity="Purchase",
        function="fetch_purchase",
        field="key",
        oracle_tests="def test_oracle(): assert fetch_purchase(8) == 8",
    )
    retrieval = InMemoryRetrieval()
    model = FakeModel(
        {
            "spec": [spec_json()],
            "implement": [implementation(), implementation("fetch_purchase", "key")],
        }
    )
    first_outcome = solve(first, mode=ArmMode.WARM, model=model, retrieval=retrieval, cfg=Config())
    second_outcome = solve(
        second, mode=ArmMode.WARM, model=model, retrieval=retrieval, cfg=Config()
    )
    assert first_outcome.branch is BranchDecision.MISS
    assert second_outcome.branch is BranchDecision.REUSE
    assert first_outcome.template == second_outcome.template
    assert "spec" not in [event.stage for event in second_outcome.cost_events]
    assert first_outcome.passed and second_outcome.passed
    assert first_outcome.pass_at_1 and second_outcome.pass_at_1
    assert retrieval.store_calls == 2
