"""Template helpers and the ship-it-fast Lane A solve loop."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from rrc.contract import Complete, Memory, Outcome, Spec, Task
from rrc.pipeline.stages import implement, repair, spec
from rrc.pipeline.verify import run_pytest


def _normalize_param(value: object) -> object:
    """Normalize JSON-like structured values before deterministic serialization."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _normalize_param(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_param(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_normalize_param(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    raise TypeError(f"task parameters must be JSON-like, got {type(value).__name__}")


def _serialize_param(value: object) -> str:
    """Return the stable textual representation used to fill one placeholder."""

    normalized = _normalize_param(value)
    if isinstance(normalized, str):
        return normalized
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _map_spec(specification: Spec, transform: Callable[[str], str]) -> Spec:
    return Spec(
        signature=transform(specification.signature),
        template=transform(specification.template),
        tests=transform(specification.tests),
    )


def to_template(specification: Spec, params: dict[str, object]) -> Spec:
    """Replace concrete parameter values with their named placeholders."""

    replacements = [(_serialize_param(value), f"{{{key}}}") for key, value in params.items()]
    replacements.sort(key=lambda item: len(item[0]), reverse=True)

    def replace(text: str) -> str:
        for concrete, placeholder in replacements:
            if concrete:
                text = text.replace(concrete, placeholder)
        return text

    return _map_spec(specification, replace)


def render(specification: Spec, params: dict[str, object]) -> Spec:
    """Fill known named placeholders without interpreting unrelated Python braces."""

    replacements = [(f"{{{key}}}", _serialize_param(value)) for key, value in params.items()]

    def replace(text: str) -> str:
        for placeholder, concrete in replacements:
            text = text.replace(placeholder, concrete)
        return text

    return _map_spec(specification, replace)


def _failed_outcome(
    task: Task,
    *,
    warm: bool,
    reused: bool,
    spec_tokens: int,
) -> Outcome:
    return Outcome(
        task_id=task.task_id,
        warm=warm,
        passed=False,
        reused=reused,
        spec_tokens=spec_tokens,
        impl_tokens=0,
        repair_tokens=0,
        oracle_passed=None,
    )


def solve(
    task: Task,
    *,
    warm: bool,
    complete: Complete,
    memory: Memory,
    strong: str,
    cheap: str,
) -> Outcome:
    """Turn one task into verified code with at most one cheap repair call."""

    spec_tokens = 0
    hit = memory.get(task) if warm else None
    reused = hit is not None

    if hit is None:
        templated_spec, spec_tokens = spec(task, complete, strong)
        if templated_spec is None:
            return _failed_outcome(
                task,
                warm=warm,
                reused=False,
                spec_tokens=spec_tokens,
            )
    else:
        templated_spec = hit

    try:
        concrete_spec = render(templated_spec, task.params)
    except (TypeError, ValueError):
        return _failed_outcome(
            task,
            warm=warm,
            reused=reused,
            spec_tokens=spec_tokens,
        )

    code, impl_tokens = implement(concrete_spec, complete, cheap)
    passed, pytest_output = run_pytest(code, concrete_spec.tests)
    repair_tokens = 0
    if not passed:
        code, repair_tokens = repair(
            concrete_spec,
            code,
            pytest_output,
            complete,
            cheap,
        )
        passed, _ = run_pytest(code, concrete_spec.tests)

    oracle_passed = None
    if task.oracle_tests.strip():
        oracle_passed, _ = run_pytest(code, task.oracle_tests)

    outcome = Outcome(
        task_id=task.task_id,
        warm=warm,
        passed=passed,
        reused=reused,
        spec_tokens=spec_tokens,
        impl_tokens=impl_tokens,
        repair_tokens=repair_tokens,
        oracle_passed=oracle_passed,
    )

    if warm and not reused and passed:
        memory.put(task, to_template(concrete_spec, task.params))

    return outcome
