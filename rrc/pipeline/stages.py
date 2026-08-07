"""Strict single-completion stages for Lane A."""

from __future__ import annotations

from typing import cast

from rrc.contract import (
    CostEvent,
    ModelPort,
    ModelRole,
    RunContext,
    Slots,
    Spec,
    Task,
)
from rrc.pipeline.prompts import implement_prompt, repair_prompt, spec_prompt
from rrc.pipeline.template import TemplateError, loads_unique_json, validate_spec_for_task

_SPEC_KEYS = {"plan", "signature", "contract", "tests", "slots"}
_SLOTS_KEYS = {
    "entity",
    "identifiers",
    "types",
    "fields",
    "constants",
    "edge_values",
    "values",
}
_CATEGORIES = ("identifiers", "types", "fields", "constants", "edge_values")


def _strict_strings(value: object, *, strip: bool = False) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    if any(not isinstance(item, str) or not item.strip() for item in value):
        return None
    raw_strings = cast(list[str], value)
    strings = tuple(item.strip() for item in raw_strings) if strip else tuple(raw_strings)
    if len(set(strings)) != len(strings):
        return None
    return strings


def parse_spec(raw: str, task: Task) -> Spec | None:
    """Parse the exact SPEC schema and validate it against task markers."""

    try:
        value = loads_unique_json(raw)
    except TemplateError:
        return None
    if not isinstance(value, dict) or set(value) != _SPEC_KEYS:
        return None
    obj = cast(dict[str, object], value)
    plan = obj["plan"]
    signature = obj["signature"]
    contract = obj["contract"]
    tests = _strict_strings(obj["tests"], strip=True)
    slots_value = obj["slots"]
    if (
        not isinstance(plan, str)
        or not plan.strip()
        or not isinstance(signature, str)
        or not signature.strip()
        or not isinstance(contract, str)
        or not contract.strip()
        or tests is None
        or not tests
        or not isinstance(slots_value, dict)
        or set(slots_value) != _SLOTS_KEYS
    ):
        return None

    slots_obj = cast(dict[str, object], slots_value)
    entity = slots_obj["entity"]
    if entity is not None and (not isinstance(entity, str) or not entity.strip()):
        return None
    categories: dict[str, tuple[str, ...]] = {}
    for name in _CATEGORIES:
        parsed = _strict_strings(slots_obj[name])
        if parsed is None:
            return None
        categories[name] = parsed
    values_value = slots_obj["values"]
    if not isinstance(values_value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in values_value.items()
    ):
        return None
    concrete = {key: cast(str, item) for key, item in values_value.items()}
    specification = Spec(
        plan=plan.strip(),
        signature=signature.strip(),
        contract=contract.strip(),
        tests=tests,
        slots=Slots(
            entity=cast(str | None, entity),
            identifiers=categories["identifiers"],
            types=categories["types"],
            fields=categories["fields"],
            constants=categories["constants"],
            edge_values=categories["edge_values"],
            values=dict(concrete),
        ),
    )
    return specification if validate_spec_for_task(specification, task) else None


def _complete(
    model: ModelPort,
    role: ModelRole,
    prompt: str,
    ctx: RunContext,
    stage: str,
) -> tuple[str, CostEvent]:
    completion = model.complete(role, prompt, ctx, stage)
    event = CostEvent(
        arm=ctx.arm,
        task_id=ctx.task_id,
        stage=stage,
        model=completion.model,
        usage=completion.usage,
        provider=model.provider,
    )
    return completion.text, event


def spec_stage(
    task: Task,
    model: ModelPort,
    ctx: RunContext,
    *,
    stage: str = "spec",
) -> tuple[Spec | None, CostEvent]:
    """Perform one strong SPEC completion and strict parse."""

    raw, event = _complete(model, ModelRole.STRONG, spec_prompt(task), ctx, stage)
    return parse_spec(raw, task), event


def implement_stage(
    specification: Spec,
    model: ModelPort,
    ctx: RunContext,
    *,
    stage: str = "implement",
) -> tuple[str, CostEvent]:
    """Perform one small implementation completion."""

    return _complete(model, ModelRole.SMALL, implement_prompt(specification), ctx, stage)


def repair_stage(
    specification: Spec,
    code: str,
    pytest_output: str,
    model: ModelPort,
    ctx: RunContext,
    *,
    stage: str = "repair",
) -> tuple[str, CostEvent]:
    """Perform one small repair completion."""

    return _complete(
        model,
        ModelRole.SMALL,
        repair_prompt(specification, code, pytest_output),
        ctx,
        stage,
    )
