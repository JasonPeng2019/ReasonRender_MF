"""The fixed two-task proof workload."""

from __future__ import annotations

from rrc.contract import Task


def two_task_workload() -> tuple[Task, Task]:
    """Return two same-shape tasks with distinct function and numeric slots."""

    shape = (
        "Implement a Python function named {function}(value: int) -> int "
        "that returns the fixed numeric value {number}."
    )
    return (
        Task(
            task_id="proof-first",
            family="fixed-return",
            params={"function": "return_two", "number": 2},
            text=shape,
            oracle_tests="def test_oracle(): assert return_two(99) == 2",
        ),
        Task(
            task_id="proof-second",
            family="fixed-return",
            params={"function": "return_three", "number": 3},
            text=shape,
            oracle_tests="def test_oracle(): assert return_three(-7) == 3",
        ),
    )
