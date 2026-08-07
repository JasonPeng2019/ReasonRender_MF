"""Artifact-only prompts for the five frozen Lane A model stages."""

from __future__ import annotations

import json

from rrc.contract import Spec, Task

_NO_SIDE_EFFECTS = "Do not run commands, edit files, or explain."
_CODE_ONLY = (
    "Return only executable Python source. No fences, prose, or test edits. " + _NO_SIDE_EFFECTS
)


def spec_prompt(task: Task) -> str:
    """Request the exact reusable SPEC JSON artifact without exposing oracle tests."""

    return (
        "Create a reusable implementation specification, not an implementation. "
        "Return only one strict JSON object with exactly this schema: "
        '{"plan":str,"signature":str,"contract":str,"tests":[str,...],'
        '"slots":{"entity":str|null,"identifiers":[str,...],"types":[str,...],'
        '"fields":[str,...],"constants":[str,...],"edge_values":[str,...],'
        '"values":{"slot_name":"concrete value"}}}. '
        "Use each concrete RRC_SLOT_VALUES value in the relevant textual or slot-category field. "
        "The signature annotations and slots.fields must exactly match RRC_SHAPE. Tests must be "
        "same-module pytest tests that directly call the function. Identifier/field slots must be "
        "valid Python identifiers; type slots must use names, generics, or unions, never calls or "
        "lambdas. No unknown keys or prose. "
        f"{_NO_SIDE_EFFECTS}\n"
        f"TASK:\n{task.text}"
    )


def _spec_artifact(specification: Spec) -> str:
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
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def implement_prompt(specification: Spec) -> str:
    """Request only an implementation for a concrete Spec."""

    return (
        "Implement the complete concrete specification exactly. Do not include or alter tests.\n"
        f"SPEC:{_spec_artifact(specification)}\n{_CODE_ONLY}"
    )


def repair_prompt(specification: Spec, code: str, pytest_output: str) -> str:
    """Request only corrected source after one failed verification."""

    return (
        "Repair the implementation against the unchanged concrete specification and failures.\n"
        f"SPEC:{_spec_artifact(specification)}\n"
        f"CURRENT_CODE:\n{code}\nPYTEST_OUTPUT:\n{pytest_output}\n{_CODE_ONLY}"
    )
