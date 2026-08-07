"""Single-shot model stages for the ship-it-fast Lane A pipeline."""

from __future__ import annotations

import json

from rrc.contract import Complete, Spec, Task

_JSON_ONLY = "Output only the JSON object. Do not run commands, do not edit files, do not explain."
_CODE_ONLY = "Output only the Python code. Do not run commands, do not edit files, do not explain."


def spec_prompt(task: Task) -> str:
    """Build the expensive, single-shot specification prompt."""

    placeholders = ", ".join(f"{{{name}}}" for name in task.params)
    params = json.dumps(task.params, ensure_ascii=False, sort_keys=True)
    return (
        "You are the SPEC stage. Write a concise reusable contract for the coding task; "
        "do not implement it. Return one JSON object with exactly three string fields: "
        '"signature", "template", and "tests". "signature" is the exact Python signature. '
        '"template" describes behavior, edges, and errors. "tests" contains same-module pytest '
        "test functions that call the implementation directly without importing it. "
        f"Use these named placeholders everywhere their task-specific values occur: {placeholders}. "
        "Leave ordinary Python braces unchanged.\n"
        f"Family: {task.family}\n"
        f"Parameters: {params}\n"
        f"Task: {task.text}\n"
        f"{_JSON_ONLY}"
    )


def parse_spec(raw: str) -> Spec | None:
    """Parse one strict SPEC JSON response, returning ``None`` on any schema error."""

    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    if set(value) != {"signature", "template", "tests"}:
        return None
    signature = value.get("signature")
    template = value.get("template")
    tests = value.get("tests")
    if (
        not isinstance(signature, str)
        or not isinstance(template, str)
        or not isinstance(tests, str)
    ):
        return None
    return Spec(signature=signature, template=template, tests=tests)


def spec(task: Task, complete: Complete, model: str) -> tuple[Spec | None, int]:
    """Call the expensive model once and parse its reusable specification."""

    raw, tokens = complete(spec_prompt(task), model)
    return parse_spec(raw), tokens


def impl_prompt(specification: Spec) -> str:
    """Build the cheap implementation prompt."""

    artifact = json.dumps(
        {
            "signature": specification.signature,
            "template": specification.template,
            "tests": specification.tests,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return (
        "You are the IMPLEMENT stage. Implement the whole task described by the spec. "
        "Match the signature exactly, satisfy the general contract, and pass the tests. "
        "Do not change or include the tests.\n"
        f"Spec: {artifact}\n"
        f"{_CODE_ONLY}"
    )


def implement(specification: Spec, complete: Complete, model: str) -> tuple[str, int]:
    """Call the cheap model once for an implementation."""

    return complete(impl_prompt(specification), model)


def repair_prompt(specification: Spec, code: str, pytest_output: str) -> str:
    """Build the one permitted repair prompt."""

    artifact = json.dumps(
        {
            "signature": specification.signature,
            "template": specification.template,
            "tests": specification.tests,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return (
        "You are the REPAIR stage. Fix the Python implementation so it matches the signature, "
        "satisfies the spec, and passes the unchanged tests.\n"
        f"Spec: {artifact}\n"
        f"Current code:\n{code}\n"
        f"Pytest output:\n{pytest_output}\n"
        f"{_CODE_ONLY}"
    )


def repair(
    specification: Spec,
    code: str,
    pytest_output: str,
    complete: Complete,
    model: str,
) -> tuple[str, int]:
    """Call the cheap model once to repair a failed implementation."""

    return complete(repair_prompt(specification, code, pytest_output), model)
