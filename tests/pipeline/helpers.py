"""Shared deterministic artifacts for Lane A tests."""

from __future__ import annotations

import json

from rrc.contract import Slots, Spec, Task


def make_task(
    task_id: str = "order-1",
    *,
    entity: str = "Order",
    function: str = "get_order",
    field: str = "order_id",
    arg_type: str = "int",
    oracle_tests: str | None = None,
) -> Task:
    shape = json.dumps(
        {"arity": 1, "arg_types": [arg_type], "fields": [field]},
        separators=(",", ":"),
    )
    values = json.dumps(
        {"entity": entity, "function": function, "field": field},
        separators=(",", ":"),
    )
    return Task(
        task_id=task_id,
        text=(
            f"Implement {function} for {entity}; accept {field}: {arg_type} and return it.\n"
            f"RRC_SHAPE: {shape}\nRRC_SLOT_VALUES: {values}"
        ),
        oracle_tests=oracle_tests,
    )


def make_spec(
    *,
    entity: str = "Order",
    function: str = "get_order",
    field: str = "order_id",
    arg_type: str = "int",
) -> Spec:
    return Spec(
        plan=f"Implement {function} for {entity} with {field}.",
        signature=f"def {function}({field}: {arg_type}) -> {arg_type}",
        contract=f"Return {field} unchanged for {entity}.",
        tests=(f"def test_behavior():\n    assert {function}(3) == 3",),
        slots=Slots(
            entity=entity,
            identifiers=(function,),
            fields=(field,),
            values={"entity": entity, "function": function, "field": field},
        ),
    )


def spec_json(**overrides: object) -> str:
    specification = make_spec(**overrides)  # type: ignore[arg-type]
    slots = specification.slots
    return json.dumps(
        {
            "plan": specification.plan,
            "signature": specification.signature,
            "contract": specification.contract,
            "tests": list(specification.tests),
            "slots": {
                "entity": slots.entity,
                "identifiers": list(slots.identifiers),
                "types": list(slots.types),
                "fields": list(slots.fields),
                "constants": list(slots.constants),
                "edge_values": list(slots.edge_values),
                "values": dict(slots.values),
            },
        },
        separators=(",", ":"),
    )


def implementation(function: str = "get_order", field: str = "order_id") -> str:
    return f"def {function}({field}: int) -> int:\n    return {field}"


def broken_implementation(function: str = "get_order", field: str = "order_id") -> str:
    return f"def {function}({field}: int) -> int:\n    return -1"
