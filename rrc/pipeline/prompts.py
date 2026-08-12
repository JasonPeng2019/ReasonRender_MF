"""Artifact-only prompts for the five frozen Lane A model stages."""

from __future__ import annotations

import json

from rrc.contract import Spec, Task

_NO_SIDE_EFFECTS = "Do not run commands, edit files, or explain."
_CODE_ONLY = (
    "Return only executable Python source. No fences, prose, or test edits. " + _NO_SIDE_EFFECTS
)


def spec_prompt(task: Task, source: str | None = None, failure: str | None = None) -> str:
    """Request the exact reusable SPEC JSON artifact without exposing oracle tests."""

    return (
        "Create a reusable implementation specification, not an implementation. "
        "Return only one strict JSON object with exactly this schema: "
        '{"plan":str,"signature":str,"contract":str,"tests":[str,...],'
        '"slots":{"entity":str|null,"identifiers":[str,...],"types":[str,...],'
        '"fields":[str,...],"constants":[str,...],"edge_values":[str,...]}}. '
        "RRC_SLOT_VALUES are controller bindings, not a Spec field. Use each relevant concrete "
        "binding in the appropriate semantic slot category and never emit a nested values key. "
        "Category mapping: slots.entity is only the entity slot or null; slots.identifiers "
        "contains only function/identifier slot values; slots.types contains only declared type "
        "slot values; slots.fields exactly equals RRC_SHAPE.fields; numeric/constant slot values "
        "and every other controller binding belong in slots.constants; and only declared edge "
        "slot values belong in edge_values. "
        "The signature annotations and slots.fields must exactly match RRC_SHAPE. Tests must be "
        "same-module pytest tests that directly call the function. Tests must be self-contained: "
        "no imports, decorators, fixtures, parametrization, plugins, or external names. "
        "Identifier/field slots must be valid Python identifiers; type slots must use names, "
        "generics, or unions, never calls or lambdas. No unknown keys or prose. "
        f"{_NO_SIDE_EFFECTS}\n"
        f"TASK:\n{task.text}"
        + ("\nSTARTER_SOURCE:\n" + source if source is not None else "")
        + ("\nPRIOR_PUBLIC_FAILURE:\n" + failure if failure is not None else "")
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


def independent_tests_prompt(task: Task, specification: Spec) -> str:
    """Request implementation-blind public acceptance tests."""

    return (
        "Write implementation-blind pytest acceptance tests for the task and frozen Spec. "
        'Return only strict JSON exactly {"v":1,"tests":[str,...]}. '
        "Do not request, infer, or inspect implementation/source/oracle bytes. "
        f"{_NO_SIDE_EFFECTS}\nTASK:\n{task.text}\nSPEC:{_spec_artifact(specification)}"
    )


def prime_prompt(task: Task, neighbours: tuple[Spec, ...]) -> str:
    """Request an assisted Spec from own-store neighbour Specs only."""

    examples = json.dumps(
        [_spec_artifact(specification) for specification in neighbours],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "Create a Spec for the NEW task using one or two own-store neighbour Specs only. "
        "Never request or read starter source. Return either the exact six-field Spec JSON or "
        '{"unfit":true}. '
        f"{_NO_SIDE_EFFECTS}\nEXAMPLES:{examples}\nNEW_TASK:\n{task.text}"
    )


def metadata_fill_prompt(task: Task) -> str:
    """Request only missing general-task retrieval metadata, never source or tests."""

    current = {
        "artifact_path": task.artifact_path,
        "family": task.family,
        "primary": task.primary,
        "shape": None if task.shape is None else task.shape.as_json(),
        "slot_values": None if task.slot_values is None else dict(task.slot_values),
        "task_text": task.text,
    }
    return (
        "Resolve metadata for retrieval only; do not implement code or tests. Return one strict "
        "JSON object exactly {v:1,authority:'small_model',primary:str,family:str|null,"
        "shape:{arg_types:[str,...],arity:int,fields:[str,...]},slot_values:{str:str,...}}. "
        "Every nonnull caller field is immutable and must be copied exactly. No tools, commands, "
        "source requests, repository reads, prose, or unknown keys.\nTASK_METADATA:"
        + json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def code_artifact_prompt(
    *,
    task: Task,
    attempt_id: str,
    source: str | None,
    stage: str,
    specification: Spec | None = None,
    current_code: str | None = None,
    failure: str | None = None,
) -> str:
    """Render one exact CodeArtifact request for direct/implement/repair stages."""

    parts = [
        "Return exactly one strict CodeArtifact JSON object with keys "
        '{"v":1,"attempt_id":str,"artifact_path":str,"source":str}.',
        "The source must be NFC, LF-only, contain no NUL, and have no terminal newline.",
        f"attempt_id and artifact_path must be exactly {attempt_id!r} and {task.artifact_path!r}.",
        _NO_SIDE_EFFECTS,
        f"STAGE:{stage}",
        f"TASK:\n{task.text}",
    ]
    if source is not None:
        parts.append("STARTER_SOURCE:\n" + source)
    if specification is not None:
        parts.append("SPEC:" + _spec_artifact(specification))
    if current_code is not None:
        parts.append("CURRENT_CODE:\n" + current_code)
    if failure is not None:
        parts.append("PUBLIC_FAILURE:\n" + failure)
    return "\n".join(parts)
