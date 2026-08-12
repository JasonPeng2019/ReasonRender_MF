"""Strict task-marker parsing and deterministic RRCv2 template mechanics."""

from __future__ import annotations

import ast
import json
import keyword
import re
import tokenize
from collections.abc import Callable
from dataclasses import dataclass
from io import StringIO
from typing import cast

from rrc.contract import Slots, Spec, Task, Template, canonical_json_bytes

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


def _validate_slot_values(values: dict[str, str], *, allow_empty: bool = True) -> None:
    if not values and not allow_empty:
        raise TemplateError("slot values must not be empty")
    for key, value in values.items():
        if not _valid_identifier(key) or not value.strip():
            raise TemplateError("slot names and values must be non-empty identifiers/strings")
    concrete = list(values.values())
    if len(set(concrete)) != len(concrete):
        raise TemplateError("concrete slot values must be unique")


def parse_task_metadata(task: Task) -> tuple[TaskShape, dict[str, str]]:
    """Parse the two required final task markers with an exact schema."""

    if task.shape is not None and task.slot_values is not None:
        values = dict(task.slot_values)
        _validate_slot_values(
            values,
            allow_empty=task.verification_profile == "rrcv2_general_v1",
        )
        return (
            TaskShape(
                task.shape.arity,
                tuple(_canonical_type(item) for item in task.shape.arg_types),
                task.shape.fields,
            ),
            values,
        )

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
    _validate_slot_values(values, allow_empty=False)
    if any(
        key in _NAME_VALUE_SLOTS and not _valid_identifier(value) for key, value in values.items()
    ):
        raise TemplateError("name-valued slots must contain valid identifiers")
    return TaskShape(arity, canonical_types, tuple(sorted(fields))), dict(values)


def signature_declaration_source(signature: str) -> str:
    """Convert legacy header-only signatures to the verifier's declaration module."""

    source = signature.strip()
    if not source:
        raise TemplateError("signature must not be empty")
    try:
        ast.parse(source)
        return source
    except SyntaxError:
        pass
    converted: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("def ", "async def ")):
            if stripped.endswith(":"):
                line += " ..."
            else:
                line += ": ..."
        converted.append(line)
    result = "\n".join(converted)
    try:
        ast.parse(result)
    except SyntaxError as error:
        raise TemplateError("signature is not parseable") from error
    return result


def _function_arguments(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    method: bool,
) -> tuple[str, ...]:
    positional = (*function.args.posonlyargs, *function.args.args)
    static = any(
        isinstance(item, ast.Name) and item.id == "staticmethod" for item in function.decorator_list
    )
    classmethod = any(
        isinstance(item, ast.Name) and item.id == "classmethod" for item in function.decorator_list
    )
    receiver = 1 if method and not static else 0
    if receiver and (not positional or positional[0].arg != ("cls" if classmethod else "self")):
        raise TemplateError("method signature receiver is invalid")
    if function.args.vararg is not None or function.args.kwarg is not None:
        raise TemplateError("variadic signatures are not reusable")
    arguments = (*positional[receiver:], *function.args.kwonlyargs)
    annotations: list[str] = []
    for argument in arguments:
        if argument.annotation is None:
            raise TemplateError("all non-receiver arguments require annotations")
        annotations.append(_canonical_type(ast.unparse(argument.annotation)))
    return tuple(annotations)


def signature_symbols(signature: str) -> dict[str, tuple[str, ...]]:
    """Return all top-level/direct-method symbols and receiver-excluded argument types."""

    source = signature_declaration_source(signature)
    try:
        module = ast.parse(source)
    except SyntaxError as error:  # pragma: no cover - guarded by conversion
        raise TemplateError("signature is not parseable") from error
    symbols: dict[str, tuple[str, ...]] = {}
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in symbols:
                raise TemplateError("signature contains a duplicate symbol")
            symbols[node.name] = _function_arguments(node, method=False)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                name = f"{node.name}.{item.name}"
                if name in symbols:
                    raise TemplateError("signature contains a duplicate symbol")
                symbols[name] = _function_arguments(item, method=True)
    if not symbols:
        raise TemplateError("signature must declare at least one callable")
    return symbols


def _signature_nodes(
    signature: str,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    module = ast.parse(signature_declaration_source(signature))
    nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nodes[f"{node.name}.{item.name}"] = item
    return nodes


def _call_binds(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    call: ast.Call,
    *,
    method: bool,
) -> bool:
    if any(isinstance(argument, ast.Starred) for argument in call.args) or any(
        keyword.arg is None for keyword in call.keywords
    ):
        return False
    positional = [*function.args.posonlyargs, *function.args.args]
    static = any(
        isinstance(item, ast.Name) and item.id == "staticmethod" for item in function.decorator_list
    )
    receiver = 1 if method and not static else 0
    positional = positional[receiver:]
    defaults_start = len(positional) - len(function.args.defaults)
    positional_only = {argument.arg for argument in function.args.posonlyargs[receiver:]}
    named = {argument.arg for argument in positional}
    keyword_only = {argument.arg for argument in function.args.kwonlyargs}
    keyword_names = [cast(str, keyword.arg) for keyword in call.keywords]
    if len(call.args) > len(positional) or len(set(keyword_names)) != len(keyword_names):
        return False
    if any(name in positional_only or name not in named | keyword_only for name in keyword_names):
        return False
    already_positional = {argument.arg for argument in positional[: len(call.args)]}
    if already_positional & set(keyword_names):
        return False
    supplied = already_positional | set(keyword_names)
    required_positional = {argument.arg for argument in positional[:defaults_start]}
    required_keyword = {
        argument.arg
        for argument, default in zip(
            function.args.kwonlyargs,
            function.args.kw_defaults,
            strict=True,
        )
        if default is None
    }
    return required_positional | required_keyword <= supplied


def tier_minus_one(
    specification: Spec,
    task: Task,
    *,
    independent_tests: tuple[str, ...] = (),
) -> bool:
    """Apply only the frozen structural/render/test-reference predicates for a hit."""

    try:
        selected = retrieval_primary(specification, task)
        if selected is None:
            return False
        symbols = signature_symbols(specification.signature)
        shape, _values = parse_task_metadata(task)
        if symbols[selected] != shape.arg_types:
            return False
        node = _signature_nodes(specification.signature)[selected]
        terminal = selected.rsplit(".", 1)[-1]
        method = "." in selected
        for source in (*specification.tests, *independent_tests):
            tree = ast.parse(source)
            for call in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
                matches = (isinstance(call.func, ast.Name) and call.func.id == terminal) or (
                    isinstance(call.func, ast.Attribute) and call.func.attr == terminal
                )
                if matches and _call_binds(node, call, method=method):
                    return True
    except (KeyError, SyntaxError, TemplateError, TypeError, ValueError):
        return False
    return False


def retrieval_primary(specification: Spec, task: Task) -> str | None:
    """Resolve the unique retrieval-primary without rejecting a valid multi-callable Spec."""

    symbols = signature_symbols(specification.signature)
    explicit = task.primary
    if explicit is None and task.slot_values is not None:
        explicit = dict(task.slot_values).get("function")
    if explicit is not None:
        return explicit if explicit in symbols else None
    identifiers = tuple(specification.slots.identifiers)
    matches = [
        symbol
        for symbol in symbols
        if symbol in identifiers or symbol.rsplit(".", 1)[-1] in identifiers
    ]
    if len(matches) == 1:
        return matches[0]
    if len(symbols) == 1 and not identifiers:
        return next(iter(symbols))
    return None


def loose_sanity_check(specification: Spec, *, primary: str | None = None) -> bool:
    """Apply deliberately small non-semantic checks to a rendered specification."""

    try:
        symbols = signature_symbols(specification.signature)
    except TemplateError:
        return False
    selected = set(symbols) if primary is None else {primary} if primary in symbols else set()
    terminals = {name.rsplit(".", 1)[-1] for name in selected}
    for test in specification.tests:
        try:
            tree = ast.parse(test)
        except SyntaxError:
            continue
        if any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id in terminals)
                or (isinstance(node.func, ast.Attribute) and node.func.attr in terminals)
            )
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


def validate_spec_for_task(
    specification: Spec,
    task: Task,
    *,
    strict_primary: bool = True,
) -> bool:
    """Validate exact task/slot/shape agreement without executing model-authored code."""

    try:
        symbols = signature_symbols(specification.signature)
        selected = retrieval_primary(specification, task)
        explicit = task.primary
        if explicit is None and task.slot_values is not None:
            explicit = dict(task.slot_values).get("function")
        if strict_primary and explicit is not None and selected != explicit:
            return False
        if not loose_sanity_check(specification, primary=selected):
            return False
        if any(
            not _valid_identifier(value)
            for value in (*specification.slots.identifiers, *specification.slots.fields)
        ):
            return False
        for value in specification.slots.types:
            _canonical_type(value)
        category_values = _slot_category_values(specification.slots)
        task_values: dict[str, str] | None = None
        if task.shape is not None or task.slot_values is not None:
            if task.shape is None or task.slot_values is None:
                return False
            shape, task_values = parse_task_metadata(task)
            if selected is not None:
                annotations = symbols[selected]
                if len(annotations) != shape.arity or annotations != shape.arg_types:
                    return False
            if tuple(sorted(specification.slots.fields)) != shape.fields:
                return False
            # Shape fields are structural names, not necessarily varying slot
            # values (the frozen select-status workload keeps ``name`` and
            # ``status`` fixed across every family member).  All other model-
            # classified slot values must still come from controller bindings.
            non_field_values = tuple(
                (
                    *(
                        (specification.slots.entity,)
                        if specification.slots.entity is not None
                        else ()
                    ),
                    *specification.slots.identifiers,
                    *specification.slots.types,
                    *specification.slots.constants,
                    *specification.slots.edge_values,
                )
            )
            if any(value not in task_values.values() for value in non_field_values):
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
            if (
                strict_primary
                and task_values.get("function") is not None
                and task_values["function"] != selected
            ):
                return False
        templatize(
            specification,
            slot_values=None if task_values is None else tuple(sorted(task_values.items())),
            primary=task.primary or (None if task_values is None else task_values.get("function")),
        )
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


def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _ast_index(source: str, offsets: list[int], line: int, utf8_column: int) -> int:
    """Translate CPython's UTF-8 AST column into a Python character index."""

    lines = source.splitlines(keepends=True)
    try:
        prefix = lines[line - 1].encode("utf-8")[:utf8_column].decode("utf-8", errors="strict")
    except (IndexError, UnicodeDecodeError) as exc:
        raise TemplateError("AST position is not on a UTF-8 character boundary") from exc
    return offsets[line - 1] + len(prefix)


def _map_python(
    source: str,
    replacements: tuple[tuple[str, str], ...],
    categories: dict[str, set[str]],
) -> str:
    """Replace code occurrences while retaining coincidental call-argument literals."""

    parse_source = source
    suffix = ""
    if source.lstrip().startswith(("def ", "async def ")) and "\n" not in source:
        parse_source = source.rstrip(":") + ":\n    pass\n"
        suffix = parse_source[len(source.rstrip(":")) :]
    blocked: dict[str, set[tuple[int, int]]] = {}
    try:
        tree = ast.parse(parse_source)
        offsets = _line_offsets(parse_source)
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            for argument in (*call.args, *(keyword.value for keyword in call.keywords)):
                if (
                    not isinstance(argument, ast.Constant)
                    or argument.end_lineno is None
                    or argument.end_col_offset is None
                ):
                    continue
                segment = ast.get_source_segment(parse_source, argument)
                if segment is None:
                    continue
                start = _ast_index(parse_source, offsets, argument.lineno, argument.col_offset)
                end = _ast_index(
                    parse_source,
                    offsets,
                    argument.end_lineno,
                    argument.end_col_offset,
                )
                blocked.setdefault(segment, set()).add((start, end))
    except (SyntaxError, ValueError):
        tree = None
    matches: list[tuple[int, int, str]] = []
    for concrete, placeholder in replacements:
        start = 0
        while True:
            if concrete.isidentifier():
                match = re.search(
                    rf"(?<!\w){re.escape(concrete)}(?!\w)",
                    parse_source[start:],
                )
                index = -1 if match is None else start + match.start()
            else:
                index = parse_source.find(concrete, start)
            if index < 0:
                break
            end = index + len(concrete)
            category = categories.get(concrete, set())
            if not (
                "constant" in category
                and "edge_value" not in category
                and (index, end) in blocked.get(concrete, set())
            ):
                matches.append((index, end, placeholder))
            start = index + max(1, len(concrete))
    matches.sort(key=lambda row: (row[0], -(row[1] - row[0]), row[2]))
    for left, right in zip(matches, matches[1:]):
        if right[0] < left[1]:
            raise TemplateError("ambiguous overlapping slot occurrences")
    result: list[str] = []
    cursor = 0
    for start, end, placeholder in matches:
        result.append(parse_source[cursor:start])
        result.append(placeholder)
        cursor = end
    result.append(parse_source[cursor:])
    rendered = "".join(result)
    if suffix:
        rendered = rendered[: -len(suffix)].rstrip(":")
        if source.rstrip().endswith(":"):
            rendered += ":"
    return rendered


def _map_slots(
    slots: Slots,
    transform: Callable[[str], str],
) -> Slots:
    return Slots(
        entity=None if slots.entity is None else transform(slots.entity),
        identifiers=tuple(transform(value) for value in slots.identifiers),
        types=tuple(transform(value) for value in slots.types),
        fields=tuple(transform(value) for value in slots.fields),
        constants=tuple(transform(value) for value in slots.constants),
        edge_values=tuple(transform(value) for value in slots.edge_values),
    )


def _map_spec(
    specification: Spec,
    transform: Callable[[str], str],
) -> Spec:
    return Spec(
        plan=transform(specification.plan),
        signature=transform(specification.signature),
        contract=transform(specification.contract),
        tests=tuple(transform(test) for test in specification.tests),
        slots=_map_slots(specification.slots, transform),
    )


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
    )


def _semantic_values(specification: Spec) -> tuple[tuple[str, str], ...]:
    slots = specification.slots
    rows: list[tuple[str, str]] = []
    if slots.entity is not None:
        rows.append(("entity", slots.entity))
    for category, values in (
        ("identifier", slots.identifiers),
        ("type", slots.types),
        ("field", slots.fields),
        ("constant", slots.constants),
        ("edge_value", slots.edge_values),
    ):
        rows.extend((category, value) for value in values)
    return tuple(rows)


def _next_name(prefix: str, used: set[str]) -> str:
    if prefix in {"entity", "function"} and prefix not in used:
        return prefix
    for index in range(1000):
        candidate = f"{prefix}_{index:03d}"
        if candidate not in used:
            return candidate
    raise TemplateError("slot-name allocation exhausted")


def derive_bindings(
    specification: Spec,
    supplied: tuple[tuple[str, str], ...] | None,
    *,
    primary: str | None,
    independent_tests: tuple[str, ...] = (),
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, tuple[str, ...]], ...]]:
    """Bind every exact semantic value to one stable controller-owned slot name."""

    supplied_map = {} if supplied is None else dict(supplied)
    _validate_slot_values(supplied_map) if supplied_map else None
    if len(set(supplied_map.values())) != len(supplied_map):
        raise TemplateError("caller slot values must be distinct")
    # Structural field names can be fixed family metadata rather than varying
    # controller slots.  When the caller supplies an authoritative slot map,
    # template only the field names that are actually present in that map.
    semantic = tuple(
        (category, value)
        for category, value in _semantic_values(specification)
        if category != "field" or supplied is None or value in supplied_map.values()
    )
    distinct = tuple(dict.fromkeys(value for _, value in semantic))
    if any(value not in distinct for value in supplied_map.values()):
        raise TemplateError("caller supplied an extra slot value")
    by_value = {value: name for name, value in supplied_map.items()}
    used = set(supplied_map)
    primary_terminal = None if primary is None else primary.rsplit(".", 1)[-1]
    for category, value in semantic:
        if value in by_value:
            continue
        if supplied is not None:
            raise TemplateError("caller bindings do not cover every semantic value")
        if category == "entity":
            prefix = "entity"
        elif category == "identifier" and value == primary_terminal:
            prefix = "function"
        else:
            prefix = category
        name = _next_name(prefix, used)
        used.add(name)
        by_value[value] = name
    bindings = tuple(sorted(((name, value) for value, name in by_value.items())))
    contexts: dict[str, set[str]] = {name: set() for name, _ in bindings}
    for category, value in semantic:
        if category == "entity":
            contexts[by_value[value]].add("text")
        elif category in {"identifier", "field"}:
            contexts[by_value[value]].add("identifier")
        elif category == "type":
            contexts[by_value[value]].add("type")
        else:
            contexts[by_value[value]].add("python_literal")
    for text in (specification.plan, specification.contract):
        for value, name in by_value.items():
            if value in text:
                contexts[name].add("text")
    code_sources = (
        specification.signature,
        *specification.tests,
        *independent_tests,
    )
    for source in code_sources:
        parse_source = source
        if source is specification.signature:
            parse_source = source.rstrip(":") + ":\n    pass\n"
        try:
            tokens = tuple(tokenize.generate_tokens(StringIO(parse_source).readline))
            tree = ast.parse(parse_source)
        except (SyntaxError, tokenize.TokenError):
            continue
        annotation_texts = {
            ast.get_source_segment(parse_source, node)
            for node in ast.walk(tree)
            if isinstance(node, (ast.arg, ast.FunctionDef, ast.AsyncFunctionDef))
            for annotation in ((node.annotation if isinstance(node, ast.arg) else node.returns),)
            if annotation is not None
            for node in (annotation,)
        }
        exception_names = {
            child.id
            for node in ast.walk(tree)
            for child in ast.walk(node)
            if isinstance(node, (ast.Raise, ast.ExceptHandler)) and isinstance(child, ast.Name)
        }
        for value, name in by_value.items():
            if any(text is not None and value in text for text in annotation_texts):
                contexts[name].add("type")
            if value in exception_names:
                contexts[name].add("exception_symbol")
            for token in tokens:
                if token.type == tokenize.NAME and token.string == value:
                    contexts[name].add("identifier")
                elif token.type == tokenize.NUMBER and token.string == value:
                    contexts[name].add("python_literal")
                elif token.type == tokenize.STRING:
                    try:
                        literal = ast.literal_eval(token.string)
                    except (SyntaxError, ValueError):
                        continue
                    if isinstance(literal, str) and value in literal:
                        contexts[name].add("string_content")
    if any(not observed for observed in contexts.values()):
        raise TemplateError("slot value has no valid occurrence context")
    return bindings, tuple((name, tuple(sorted(contexts[name]))) for name, _ in bindings)


def templatize(
    specification: Spec,
    independent_tests: tuple[str, ...] = (),
    *,
    slot_values: tuple[tuple[str, str], ...] | None = None,
    primary: str | None = None,
) -> Template:
    """Genericize the Spec and blind tests from controller-owned bindings."""

    bindings, contexts = derive_bindings(
        specification,
        slot_values,
        primary=primary,
        independent_tests=independent_tests,
    )
    concrete_values = dict(bindings)
    slot_names = tuple(sorted(concrete_values))
    all_concrete = (*_spec_strings(specification), *independent_tests)
    if any(_PLACEHOLDER.search(text) for text in all_concrete):
        raise TemplateError("pre-existing slot placeholder")
    replacements = tuple(
        sorted(
            ((value, f"{{{name}}}") for name, value in concrete_values.items()),
            key=lambda item: len(item[0]),
            reverse=True,
        )
    )
    categories: dict[str, set[str]] = {}
    for category, value in _semantic_values(specification):
        categories.setdefault(value, set()).add(category)
    transform = lambda text: _map_text(text, replacements)  # noqa: E731
    skeleton = Spec(
        plan=transform(specification.plan),
        signature=_map_python(specification.signature, replacements, categories),
        contract=transform(specification.contract),
        tests=tuple(_map_python(test, replacements, categories) for test in specification.tests),
        slots=_map_slots(specification.slots, transform),
    )
    generic_tests = tuple(_map_python(test, replacements, categories) for test in independent_tests)
    noncode = (
        skeleton.plan,
        skeleton.contract,
        *((skeleton.slots.entity,) if skeleton.slots.entity is not None else ()),
        *skeleton.slots.identifiers,
        *skeleton.slots.types,
        *skeleton.slots.fields,
        *skeleton.slots.constants,
        *skeleton.slots.edge_values,
    )
    if any(
        concrete in _PLACEHOLDER.sub("", text)
        for concrete in concrete_values.values()
        for text in noncode
    ):
        raise TemplateError("concrete slot value leaked into generic template")
    template = Template(skeleton, generic_tests, slot_names, contexts)
    rendered_spec, rendered_tests = render(template, concrete_values)
    if rendered_spec != specification or rendered_tests != independent_tests:
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
    return isinstance(template.independent_tests, tuple) and isinstance(
        template.slot_contexts, tuple
    )


def _integrity_ok(template: Template) -> bool:
    if not _runtime_template_shape_ok(template):
        return False
    names = template.slot_names
    if names != tuple(sorted(set(names))):
        return False
    payload = template.canonical_bytes().decode("utf-8")
    return set(_PLACEHOLDER.findall(payload)) == set(names)


def render(template: Template, values: dict[str, str]) -> tuple[Spec, tuple[str, ...]]:
    """Render a defensive concrete Spec and independent tests."""

    if not _integrity_ok(template) or set(values) != set(template.slot_names):
        raise TemplateError("template integrity or slot-key mismatch")
    _validate_slot_values(values)
    for name, contexts in template.slot_contexts:
        concrete = values[name]
        if "identifier" in contexts and not _valid_identifier(concrete):
            raise TemplateError("identifier-context slot value is invalid")
        if "type" in contexts:
            _canonical_type(concrete)
        if "exception_symbol" in contexts and not all(
            _valid_identifier(part) for part in concrete.split(".")
        ):
            raise TemplateError("exception-context slot value is invalid")
        if "python_literal" in contexts:
            try:
                ast.literal_eval(concrete)
            except (SyntaxError, ValueError) as exc:
                # Controller slot values represent semantic string contents,
                # not always complete Python literal spellings.  Permit the
                # bounded quote/backslash/control-free fragment vocabulary used
                # by string constants in the frozen workload; replacement then
                # remains safe inside its already validated quoted context.
                if (
                    not concrete
                    or any(char in concrete for char in "'\"\\\r\n\x00")
                    or any(ord(char) < 0x20 for char in concrete)
                ):
                    raise TemplateError("literal-context slot value is invalid") from exc
    replacements = tuple((f"{{{name}}}", values[name]) for name in template.slot_names)

    def transform(text: str) -> str:
        result = text
        for placeholder, concrete in replacements:
            result = result.replace(placeholder, concrete)
        return result

    return _map_spec(template.spec_skeleton, transform), tuple(
        transform(test) for test in template.independent_tests
    )


def resolve_template(template: Template, task: Task) -> Spec | None:
    """Return a rendered exact structural match; treat every defect as a MISS."""

    try:
        shape, values = parse_task_metadata(task)
        rendered, _ = render(template, values)
        selected = retrieval_primary(rendered, task)
        if selected is None:
            return None
        annotations = signature_symbols(rendered.signature)[selected]
        if len(annotations) != shape.arity or annotations != shape.arg_types:
            return None
        if tuple(sorted(rendered.slots.fields)) != shape.fields:
            return None
        if not validate_spec_for_task(rendered, task) or not tier_minus_one(
            rendered,
            task,
            independent_tests=template.independent_tests,
        ):
            return None
        return rendered
    except (AttributeError, TemplateError, KeyError, TypeError, ValueError):
        return None


def template_bundle_bytes(template: Template) -> bytes:
    """Return the content-addressed canonical TemplateBundle bytes."""

    if not _integrity_ok(template):
        raise TemplateError("template integrity is invalid")
    return template.canonical_bytes()


def parse_template_bundle(raw: bytes) -> Template:
    """Strictly reopen one canonical TemplateBundle without trusting its store."""

    try:
        value = loads_unique_json(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, TemplateError) as exc:
        raise TemplateError("template bundle is not strict JSON") from exc
    if canonical_json_bytes(value) != raw or not isinstance(value, dict):
        raise TemplateError("template bundle is not canonical JSON")
    if set(value) != {"independent_tests", "slot_contexts", "slot_names", "spec_template", "v"}:
        raise TemplateError("template bundle schema is invalid")
    if value.get("v") != 1:
        raise TemplateError("template bundle version is invalid")
    spec_value = value.get("spec_template")
    tests_value = value.get("independent_tests")
    names_value = value.get("slot_names")
    contexts_value = value.get("slot_contexts")
    if (
        not isinstance(spec_value, dict)
        or set(spec_value) != {"contract", "plan", "signature", "slots", "tests"}
        or not isinstance(tests_value, dict)
        or set(tests_value) != {"tests", "v"}
        or tests_value.get("v") != 1
        or not isinstance(names_value, list)
        or any(not isinstance(item, str) for item in names_value)
        or not isinstance(contexts_value, dict)
    ):
        raise TemplateError("template bundle nested schema is invalid")
    slots_value = spec_value.get("slots")
    spec_tests = spec_value.get("tests")
    independent_tests = tests_value.get("tests")
    if (
        not isinstance(slots_value, dict)
        or set(slots_value)
        != {"constants", "edge_values", "entity", "fields", "identifiers", "types"}
        or not isinstance(spec_tests, list)
        or not isinstance(independent_tests, list)
        or any(not isinstance(item, str) for item in (*spec_tests, *independent_tests))
    ):
        raise TemplateError("template bundle Spec schema is invalid")
    categories: dict[str, tuple[str, ...]] = {}
    for name in ("identifiers", "types", "fields", "constants", "edge_values"):
        item = slots_value.get(name)
        if not isinstance(item, list) or any(not isinstance(value, str) for value in item):
            raise TemplateError("template bundle slot category is invalid")
        categories[name] = tuple(item)
    entity = slots_value.get("entity")
    if entity is not None and not isinstance(entity, str):
        raise TemplateError("template bundle entity is invalid")
    if any(not isinstance(spec_value.get(name), str) for name in ("plan", "signature", "contract")):
        raise TemplateError("template bundle Spec text is invalid")
    try:
        spec = Spec(
            plan=cast(str, spec_value["plan"]),
            signature=cast(str, spec_value["signature"]),
            contract=cast(str, spec_value["contract"]),
            tests=tuple(cast(list[str], spec_tests)),
            slots=Slots(
                entity=cast(str | None, entity),
                identifiers=categories["identifiers"],
                types=categories["types"],
                fields=categories["fields"],
                constants=categories["constants"],
                edge_values=categories["edge_values"],
            ),
        )
        contexts = tuple(
            (
                cast(str, name),
                tuple(cast(list[str], item)),
            )
            for name, item in sorted(contexts_value.items())
            if isinstance(name, str)
            and isinstance(item, list)
            and all(isinstance(context, str) for context in item)
        )
        if len(contexts) != len(contexts_value):
            raise TemplateError("template bundle slot contexts are invalid")
        template = Template(
            spec,
            tuple(cast(list[str], independent_tests)),
            tuple(cast(list[str], names_value)),
            contexts,
        )
    except (TypeError, ValueError) as exc:
        raise TemplateError("template bundle content is invalid") from exc
    if not _integrity_ok(template) or template.canonical_bytes() != raw:
        raise TemplateError("template bundle failed its integrity round trip")
    return template
