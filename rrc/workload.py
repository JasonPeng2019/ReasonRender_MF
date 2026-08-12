"""Small deterministic workload for the Lane B reuse proof."""

from __future__ import annotations

import json

from rrc.contract import StructuralShapeV1, Task


def _fixed_return_task(task_id: str, function: str, number: int) -> Task:
    shape = json.dumps(
        {"arity": 1, "arg_types": ["int"], "fields": []},
        sort_keys=True,
        separators=(",", ":"),
    )
    values = json.dumps(
        {"function": function, "number": str(number)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return Task(
        task_id=task_id,
        text=(
            f"Implement {function}(value: int) -> int and always return {number}.\n"
            f"RRC_SHAPE: {shape}\nRRC_SLOT_VALUES: {values}"
        ),
        oracle_tests=(f"def test_oracle(): assert {function}(99) == {number}",),
        family="fixed-return",
        searchable_public=True,
        verification_profile="rrcv2_synthetic_v1",
        primary=function,
        shape=StructuralShapeV1(("int",), 1, ()),
        slot_values=(("function", function), ("number", str(number))),
    )


def two_task_workload() -> tuple[Task, Task]:
    """Return two distinct slot instances with the same structural shape."""

    return (
        _fixed_return_task("proof-first", "return_two", 2),
        _fixed_return_task("proof-second", "return_three", 3),
    )
