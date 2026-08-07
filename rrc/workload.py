"""Small deterministic workload for the Lane B reuse proof."""

from __future__ import annotations

import json

from rrc.contract import Task


def _fixed_return_task(task_id: str, function: str, number: int) -> Task:
    shape = json.dumps(
        {"arity": 1, "arg_types": ["int"], "fields": []},
        separators=(",", ":"),
    )
    values = json.dumps(
        {"function": function, "number": str(number)},
        separators=(",", ":"),
    )
    return Task(
        task_id=task_id,
        text=(
            f"Implement {function}(value: int) -> int and always return {number}.\n"
            f"RRC_SHAPE: {shape}\nRRC_SLOT_VALUES: {values}"
        ),
        oracle_tests=f"def test_oracle(): assert {function}(99) == {number}",
    )


def two_task_workload() -> tuple[Task, Task]:
    """Return two distinct slot instances with the same structural shape."""

    return (
        _fixed_return_task("proof-first", "return_two", 2),
        _fixed_return_task("proof-second", "return_three", 3),
    )
