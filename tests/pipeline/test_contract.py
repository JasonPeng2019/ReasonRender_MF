from rrc.contract import NoMemory, Outcome, Spec, Task


def test_outcome_total_counts_every_model_stage() -> None:
    outcome = Outcome(
        task_id="0001",
        warm=True,
        passed=True,
        reused=False,
        spec_tokens=100,
        impl_tokens=40,
        repair_tokens=10,
        oracle_passed=True,
    )

    assert outcome.total == 150


def test_no_memory_is_a_no_op() -> None:
    memory = NoMemory()
    task = Task(
        task_id="0001",
        family="identity",
        params={"function": "identity"},
        text="Implement an identity function.",
    )

    assert memory.get(task) is None
    assert memory.put(task, Spec("def identity(): ...", "Identity.", "tests")) is None
