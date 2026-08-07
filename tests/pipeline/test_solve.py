from collections.abc import Iterator

from rrc.contract import Spec, Task
from rrc.pipeline.solve import solve


class RecordingMemory:
    def __init__(self, hit: Spec | None = None) -> None:
        self.hit = hit
        self.puts: list[tuple[Task, Spec]] = []

    def get(self, task: Task) -> Spec | None:
        return self.hit

    def put(self, task: Task, spec: Spec) -> None:
        self.puts.append((task, spec))
        self.hit = spec


def task(oracle_tests: str = "def test_oracle(): assert identity(4) == 4") -> Task:
    return Task(
        task_id="identity-1",
        family="identity",
        params={"function": "identity"},
        text="Implement identity(value: int) -> int.",
        oracle_tests=oracle_tests,
    )


def spec_json() -> str:
    return (
        '{"signature":"def {function}(value: int) -> int",'
        '"template":"Return value unchanged.",'
        '"tests":"def test_spec(): assert {function}(3) == 3"}'
    )


def complete_from(responses: list[tuple[str, int]]):
    remaining: Iterator[tuple[str, int]] = iter(responses)
    calls: list[tuple[str, str]] = []

    def complete(prompt: str, model: str) -> tuple[str, int]:
        calls.append((prompt, model))
        return next(remaining)

    return complete, calls


def test_warm_miss_stores_only_after_acceptance_and_reports_oracle_result() -> None:
    memory = RecordingMemory()
    complete, calls = complete_from(
        [(spec_json(), 100), ("def identity(value: int) -> int:\n    return value", 30)]
    )

    outcome = solve(
        task(), warm=True, complete=complete, memory=memory, strong="strong", cheap="cheap"
    )

    assert outcome.passed is True
    assert outcome.oracle_passed is True
    assert outcome.reused is False
    assert outcome.total == 130
    assert len(memory.puts) == 1
    assert memory.puts[0][1].signature == "def {function}(value: int) -> int"
    assert [model for _, model in calls] == ["strong", "cheap"]


def test_warm_reuse_skips_spec_and_renders_structured_params() -> None:
    memory = RecordingMemory(
        Spec(
            "def {function}(value: int) -> int",
            "Return value unchanged.",
            "def test_spec(): assert {function}(3) == 3",
        )
    )
    complete, calls = complete_from([("def identity(value: int) -> int:\n    return value", 25)])

    outcome = solve(
        task(), warm=True, complete=complete, memory=memory, strong="strong", cheap="cheap"
    )

    assert outcome.reused is True
    assert outcome.spec_tokens == 0
    assert outcome.passed is True
    assert len(calls) == 1
    assert "def identity(value: int) -> int" in calls[0][0]
    assert memory.puts == []


def test_failed_first_implementation_gets_one_repair_then_stores_after_acceptance() -> None:
    memory = RecordingMemory()
    complete, calls = complete_from(
        [
            (spec_json(), 100),
            ("def identity(value: int) -> int:\n    return 0", 30),
            ("def identity(value: int) -> int:\n    return value", 12),
        ]
    )

    outcome = solve(
        task(), warm=True, complete=complete, memory=memory, strong="strong", cheap="cheap"
    )

    assert outcome.passed is True
    assert outcome.repair_tokens == 12
    assert len(calls) == 3
    assert memory.puts


def test_final_verification_failure_does_not_poison_memory() -> None:
    memory = RecordingMemory()
    complete, _ = complete_from(
        [
            (spec_json(), 100),
            ("def identity(value: int) -> int:\n    return 0", 30),
            ("def identity(value: int) -> int:\n    return -1", 12),
        ]
    )

    outcome = solve(
        task(), warm=True, complete=complete, memory=memory, strong="strong", cheap="cheap"
    )

    assert outcome.passed is False
    assert outcome.oracle_passed is False
    assert outcome.repair_tokens == 12
    assert memory.puts == []


def test_oracle_failure_is_reported_separately_and_never_sent_to_model() -> None:
    secret = "def test_secret_oracle(): assert identity(4) == 999"
    memory = RecordingMemory()
    complete, calls = complete_from(
        [(spec_json(), 100), ("def identity(value: int) -> int:\n    return value", 30)]
    )

    outcome = solve(
        task(secret), warm=True, complete=complete, memory=memory, strong="strong", cheap="cheap"
    )

    assert outcome.passed is True
    assert outcome.oracle_passed is False
    assert all(secret not in prompt for prompt, _ in calls)
    assert len(memory.puts) == 1


def test_malformed_spec_returns_failed_outcome_without_implementing_or_storing() -> None:
    memory = RecordingMemory()
    complete, calls = complete_from([("not json", 44)])

    outcome = solve(
        task(), warm=True, complete=complete, memory=memory, strong="strong", cheap="cheap"
    )

    assert outcome.passed is False
    assert outcome.oracle_passed is None
    assert outcome.spec_tokens == 44
    assert outcome.impl_tokens == 0
    assert outcome.repair_tokens == 0
    assert len(calls) == 1
    assert memory.puts == []


def test_cold_arm_never_reads_or_writes_memory() -> None:
    class ExplodingMemory(RecordingMemory):
        def get(self, task: Task) -> Spec | None:
            raise AssertionError("cold arm read memory")

        def put(self, task: Task, spec: Spec) -> None:
            raise AssertionError("cold arm wrote memory")

    complete, _ = complete_from(
        [(spec_json(), 100), ("def identity(value: int) -> int:\n    return value", 30)]
    )

    outcome = solve(
        task(),
        warm=False,
        complete=complete,
        memory=ExplodingMemory(),
        strong="strong",
        cheap="cheap",
    )

    assert outcome.passed is True
    assert outcome.warm is False
    assert outcome.reused is False
