"""Strict task-marker parsing and deterministic RRCv2 template mechanics."""

from __future__ import annotations

import ast
import hashlib
import json
import keyword
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from rrc.contract import Slots, Spec, Task, Template

_SHAPE_PREFIX = "RRC_SHAPE:"
_VALUES_PREFIX = "RRC_SLOT_VALUES:"
_SHAPE_KEYS = {"arity", "arg_types", "fields"}
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_NAME_VALUE_SLOTS = frozenset({"field", "function", "identifier"})


class TemplateError(ValueError):
    """Raised when task metadata or a reusable template is not safe to use."""


@dataclass(frozen=True)
class TaskShape:
    """Canonical structural shape asserted by a task."""

    arity: int
    arg_types: tuple[str, ...]
    fields: tuple[str, ...]


class _DuplicateKey(TemplateError):
    pass


def loads_unique_json(raw: str) -> object:
    """Load JSON while rejecting duplicate object keys."""

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateKey(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, TypeError, _DuplicateKey) as error:
        raise TemplateError("invalid or duplicate-key JSON") from error


def _valid_identifier(value: str) -> bool:
    return bool(_NAME.fullmatch(value)) and not keyword.iskeyword(value)


def _qualified_type_name(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return _valid_identifier(node.id)
    return (
        isinstance(node, ast.Attribute)
        and _valid_identifier(node.attr)
        and _qualified_type_name(node.value)
    )


def _supported_type_argument(node: ast.expr) -> bool:
    if isinstance(node, ast.Tuple):
        return bool(node.elts) and all(_supported_type_argument(item) for item in node.elts)
    if isinstance(node, ast.List):
        return all(_supported_type_expression(item) for item in node.elts)
    if isinstance(node, ast.Constant) and node.value is Ellipsis:
        return True
    return _supported_type_expression(node)


def _supported_type_expression(node: ast.expr) -> bool:
    if _qualified_type_name(node):
        return True
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.Subscript):
        return _qualified_type_name(node.value) and _supported_type_argument(node.slice)
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.BitOr)
        and _supported_type_expression(node.left)
        and _supported_type_expression(node.right)
    )


def _canonical_type(annotation: str) -> str:
    if not annotation.strip():
        raise TemplateError("argument types must be non-empty")
    try:
        expression = ast.parse(annotation, mode="eval").body
    except (SyntaxError, ValueError) as error:
        raise TemplateError("argument type is not a Python annotation") from error
    if not _supported_type_expression(expression):
        raise TemplateError("argument type uses unsupported annotation syntax")
    return ast.unparse(expression)


def _string_list(value: object, label: str, *, unique: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise TemplateError(f"{label} must be a list of non-empty strings")
    strings = cast(list[str], value)
    if unique and len(set(strings)) != len(strings):
        raise TemplateError(f"{label} contains duplicates")
    return tuple(strings)


def _validate_slot_values(values: dict[str, str]) -> None:
    if not values:
        raise TemplateError("slot values must not be empty")
    for key, value in values.items():
        if not _valid_identifier(key) or not value.strip():
            raise TemplateError("slot names and values must be non-empty identifiers/strings")
    concrete = list(values.values())
    if len(set(concrete)) != len(concrete):
        raise TemplateError("concrete slot values must be unique")
    for index, left in enumerate(concrete):
        for right in concrete[index + 1 :]:
            if left in right or right in left:
                raise TemplateError("concrete slot values must not contain one another")


def parse_task_metadata(task: Task) -> tuple[TaskShape, dict[str, str]]:
    """Parse the two required final task markers with an exact schema."""

    nonblank = [line.strip() for line in task.text.splitlines() if line.strip()]
    shape_lines = [line for line in nonblank if line.startswith(_SHAPE_PREFIX)]
    value_lines = [line for line in nonblank if line.startswith(_VALUES_PREFIX)]
    if len(shape_lines) != 1 or len(value_lines) != 1 or len(nonblank) < 2:
        raise TemplateError("task must contain exactly one shape and slot-values marker")
    if nonblank[-2] != shape_lines[0] or nonblank[-1] != value_lines[0]:
        raise TemplateError("task markers must be the final two non-empty lines")

    shape_raw = loads_unique_json(shape_lines[0][len(_SHAPE_PREFIX) :].strip())
    values_raw = loads_unique_json(value_lines[0][len(_VALUES_PREFIX) :].strip())
    if not isinstance(shape_raw, dict) or set(shape_raw) != _SHAPE_KEYS:
        raise TemplateError("RRC_SHAPE has the wrong fields")
    if not isinstance(values_raw, dict):
        raise TemplateError("RRC_SLOT_VALUES must be an object")

    shape_obj = cast(dict[str, object], shape_raw)
    arity = shape_obj["arity"]
    if isinstance(arity, bool) or not isinstance(arity, int) or arity < 0:
        raise TemplateError("shape arity must be a non-negative integer")
    arg_types = _string_list(shape_obj["arg_types"], "arg_types")
    fields = _string_list(shape_obj["fields"], "fields")
    if len(arg_types) != arity:
        raise TemplateError("shape arity and arg_types disagree")
    canonical_types = tuple(_canonical_type(item) for item in arg_types)
    if any(not _valid_identifier(field) for field in fields):
        raise TemplateError("shape fields must be valid identifiers")

    values_obj = cast(dict[str, object], values_raw)
    if any(not isinstance(value, str) for value in values_obj.values()):
        raise TemplateError("slot values must be strings")
    values = {key: cast(str, value) for key, value in values_obj.items()}
    _validate_slot_values(values)
    if any(
        key in _NAME_VALUE_SLOTS and not _valid_identifier(value) for key, value in values.items()
    ):
        raise TemplateError("name-valued slots must contain valid identifiers")
    return TaskShape(arity, canonical_types, tuple(sorted(fields))), dict(values)


def _parse_signature(signature: str) -> tuple[str, tuple[str, ...]]:
    source = signature.strip()
    if not source:
        raise TemplateError("signature must not be empty")
    if not source.endswith(":"):
        source += ":"
    try:
        module = ast.parse(f"{source}\n    pass\n")
    except SyntaxError as error:
        raise TemplateError("signature is not parseable") from error
    if len(module.body) != 1 or not isinstance(module.body[0], ast.FunctionDef):
        raise TemplateError("signature must contain exactly one function")
    function = module.body[0]
    arguments = (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
    if function.args.vararg is not None or function.args.kwarg is not None:
        raise TemplateError("variadic signatures are not reusable")
    annotations: list[str] = []
    for argument in arguments:
        if argument.annotation is None:
            raise TemplateError("all arguments require annotations")
        annotations.append(ast.unparse(argument.annotation))
    return function.name, tuple(annotations)


def loose_sanity_check(specification: Spec) -> bool:
    """Apply deliberately small non-semantic checks to a rendered specification."""

    try:
        function_name, _ = _parse_signature(specification.signature)
    except TemplateError:
        return False
    for test in specification.tests:
        try:
            tree = ast.parse(test)
        except SyntaxError:
            continue
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == function_name
            for node in ast.walk(tree)
        ):
            return True
    return False


def _slot_category_values(slots: Slots) -> tuple[str, ...]:
    return (
        *((slots.entity,) if slots.entity is not None else ()),
        *slots.identifiers,
        *slots.types,
        *slots.fields,
        *slots.constants,
        *slots.edge_values,
    )


def validate_spec_for_task(specification: Spec, task: Task) -> bool:
    """Validate exact task/slot/shape agreement without executing model-authored code."""

    try:
        shape, task_values = parse_task_metadata(task)
        _validate_slot_values(specification.slots.values)
        if specification.slots.values != task_values:
            return False
        function_name, annotations = _parse_signature(specification.signature)
        if len(annotations) != shape.arity or annotations != shape.arg_types:
            return False
        if tuple(sorted(specification.slots.fields)) != shape.fields:
            return False
        if not loose_sanity_check(specification):
            return False
        if any(
            not _valid_identifier(value)
            for value in (*specification.slots.identifiers, *specification.slots.fields)
        ):
            return False
        for value in specification.slots.types:
            _canonical_type(value)
        category_values = _slot_category_values(specification.slots)
        if any(value not in task_values.values() for value in category_values):
            return False
        searchable = (
            specification.plan,
            specification.signature,
            specification.contract,
            *specification.tests,
            *category_values,
        )
        if any(not any(value in text for text in searchable) for value in task_values.values()):
            return False
        if task_values.get("function") is not None and task_values["function"] != function_name:
            return False
        templatize(specification)
    except TemplateError:
        return False
    return True


def _map_text(text: str, replacements: tuple[tuple[str, str], ...]) -> str:
    result = text
    for concrete, placeholder in replacements:
        if concrete.isidentifier():
            result = re.sub(rf"(?<!\w){re.escape(concrete)}(?!\w)", placeholder, result)
        else:
            result = result.replace(concrete, placeholder)
    return result


def _map_slots(
    slots: Slots,
    transform: Callable[[str], str],
    values: dict[str, str],
) -> Slots:
    return Slots(
        entity=None if slots.entity is None else transform(slots.entity),
        identifiers=tuple(transform(value) for value in slots.identifiers),
        types=tuple(transform(value) for value in slots.types),
        fields=tuple(transform(value) for value in slots.fields),
        constants=tuple(transform(value) for value in slots.constants),
        edge_values=tuple(transform(value) for value in slots.edge_values),
        values=dict(values),
    )


def _map_spec(
    specification: Spec,
    transform: Callable[[str], str],
    values: dict[str, str],
) -> Spec:
    return Spec(
        plan=transform(specification.plan),
        signature=transform(specification.signature),
        contract=transform(specification.contract),
        tests=tuple(transform(test) for test in specification.tests),
        slots=_map_slots(specification.slots, transform, values),
    )


def _template_payload(specification: Spec, slot_names: tuple[str, ...]) -> dict[str, object]:
    slots = specification.slots
    return {
        "slot_names": list(slot_names),
        "spec_skeleton": {
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
    }


def _spec_strings(specification: Spec) -> tuple[str, ...]:
    slots = specification.slots
    return (
        specification.plan,
        specification.signature,
        specification.contract,
        *specification.tests,
        *((slots.entity,) if slots.entity is not None else ()),
        *slots.identifiers,
        *slots.types,
        *slots.fields,
        *slots.constants,
        *slots.edge_values,
        *slots.values.values(),
    )


def fingerprint(specification: Spec, slot_names: tuple[str, ...]) -> str:
    """Return the canonical SHA-256 identity for one generic skeleton."""

    payload = json.dumps(
        _template_payload(specification, slot_names),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def templatize(specification: Spec) -> Template:
    """Genericize every Spec field, reject leaks, and prove a concrete round trip."""

    concrete_values = dict(specification.slots.values)
    _validate_slot_values(concrete_values)
    slot_names = tuple(sorted(concrete_values))
    if any(_PLACEHOLDER.search(text) for text in _spec_strings(specification)):
        raise TemplateError("pre-existing slot placeholder")
    replacements = tuple(
        sorted(
            ((value, f"{{{name}}}") for name, value in concrete_values.items()),
            key=lambda item: len(item[0]),
            reverse=True,
        )
    )
    transform = lambda text: _map_text(text, replacements)  # noqa: E731
    skeleton = _map_spec(
        specification,
        transform,
        {name: f"{{{name}}}" for name in slot_names},
    )
    if any(
        concrete in _PLACEHOLDER.sub("", text)
        for concrete in concrete_values.values()
        for text in _spec_strings(skeleton)
    ):
        raise TemplateError("concrete slot value leaked into generic template")
    template = Template(fingerprint(skeleton, slot_names), skeleton, slot_names)
    if render(template, concrete_values) != specification:
        raise TemplateError("generic template does not round-trip")
    return template


def _runtime_template_shape_ok(template: object) -> bool:
    if not isinstance(template, Template):
        return False
    specification = template.spec_skeleton
    if not isinstance(template.external_ref, str) or not isinstance(template.slot_names, tuple):
        return False
    if any(not isinstance(name, str) for name in template.slot_names):
        return False
    if not isinstance(specification, Spec):
        return False
    if any(
        not isinstance(value, str)
        for value in (specification.plan, specification.signature, specification.contract)
    ):
        return False
    if not isinstance(specification.tests, tuple) or any(
        not isinstance(test, str) for test in specification.tests
    ):
        return False
    slots = specification.slots
    if not isinstance(slots, Slots) or (
        slots.entity is not None and not isinstance(slots.entity, str)
    ):
        return False
    categories = (
        slots.identifiers,
        slots.types,
        slots.fields,
        slots.constants,
        slots.edge_values,
    )
    if any(
        not isinstance(category, tuple) or any(not isinstance(value, str) for value in category)
        for category in categories
    ):
        return False
    return isinstance(slots.values, dict) and all(
        isinstance(key, str) and isinstance(value, str) for key, value in slots.values.items()
    )


def _integrity_ok(template: Template) -> bool:
    if not _runtime_template_shape_ok(template):
        return False
    names = template.slot_names
    if names != tuple(sorted(set(names))) or not names:
        return False
    if set(template.spec_skeleton.slots.values) != set(names):
        return False
    if any(template.spec_skeleton.slots.values[name] != f"{{{name}}}" for name in names):
        return False
    if template.external_ref != fingerprint(template.spec_skeleton, names):
        return False
    payload = json.dumps(_template_payload(template.spec_skeleton, names), ensure_ascii=False)
    return set(_PLACEHOLDER.findall(payload)) == set(names)


def render(template: Template, values: dict[str, str]) -> Spec:
    """Render a defensive concrete Spec from a fingerprint-checked skeleton."""

    if not _integrity_ok(template) or set(values) != set(template.slot_names):
        raise TemplateError("template integrity or slot-key mismatch")
    _validate_slot_values(values)
    replacements = tuple((f"{{{name}}}", values[name]) for name in template.slot_names)

    def transform(text: str) -> str:
        result = text
        for placeholder, concrete in replacements:
            result = result.replace(placeholder, concrete)
        return result

    return _map_spec(template.spec_skeleton, transform, dict(values))


def resolve_template(template: Template, task: Task) -> Spec | None:
    """Return a rendered exact structural match; treat every defect as a MISS."""

    try:
        shape, values = parse_task_metadata(task)
        rendered = render(template, values)
        _, annotations = _parse_signature(rendered.signature)
        if len(annotations) != shape.arity or annotations != shape.arg_types:
            return None
        if tuple(sorted(rendered.slots.fields)) != shape.fields:
            return None
        if not validate_spec_for_task(rendered, task):
            return None
        return rendered
    except (AttributeError, TemplateError, KeyError, TypeError, ValueError):
        return None
