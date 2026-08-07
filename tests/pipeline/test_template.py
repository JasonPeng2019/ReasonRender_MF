from dataclasses import replace

import pytest
from rrc.contract import Slots, Spec, Task, Template
from rrc.pipeline.template import (
    TemplateError,
    fingerprint,
    parse_task_metadata,
    render,
    resolve_template,
    templatize,
    validate_spec_for_task,
)

from tests.pipeline.helpers import make_spec, make_task


def test_markers_parse_to_canonical_ordered_types_and_sorted_fields() -> None:
    shape, values = parse_task_metadata(make_task())
    assert (shape.arity, shape.arg_types, shape.fields) == (1, ("int",), ("order_id",))
    assert values == {"entity": "Order", "function": "get_order", "field": "order_id"}


@pytest.mark.parametrize(
    "text",
    [
        "no markers",
        'RRC_SHAPE: {"arity":1,"arg_types":["int"],"fields":[],"extra":1}\n'
        'RRC_SLOT_VALUES: {"function":"f"}',
        'RRC_SHAPE: {"arity":1,"arity":1,"arg_types":["int"],"fields":[]}\n'
        'RRC_SLOT_VALUES: {"function":"f"}',
        'RRC_SHAPE: {"arity":2,"arg_types":["int"],"fields":[]}\nRRC_SLOT_VALUES: {"function":"f"}',
        'RRC_SHAPE: {"arity":1,"arg_types":["int"],"fields":["id","id"]}\n'
        'RRC_SLOT_VALUES: {"function":"f"}',
        'RRC_SHAPE: {"arity":1,"arg_types":["int"],"fields":[]}\n'
        'RRC_SLOT_VALUES: {"left":"value","right":"value"}',
        'RRC_SHAPE: {"arity":1,"arg_types":["int"],"fields":[]}\n'
        'RRC_SLOT_VALUES: {"left":"Order","right":"Orders"}',
        'RRC_SHAPE: {"arity":1,"arg_types":["int"],"fields":["not valid"]}\n'
        'RRC_SLOT_VALUES: {"function":"valid_function"}',
        'RRC_SHAPE: {"arity":1,"arg_types":["int"],"fields":[]}\n'
        'RRC_SLOT_VALUES: {"function":"bad-name"}',
        'RRC_SHAPE: {"arity":1,"arg_types":["int"],"fields":[]}\n'
        'RRC_SLOT_VALUES: {"function":"9bad"}',
        'RRC_SHAPE: {"arity":1,"arg_types":["factory()"],"fields":[]}\n'
        'RRC_SLOT_VALUES: {"function":"valid_function"}',
        'RRC_SHAPE: {"arity":1,"arg_types":["lambda: int"],"fields":[]}\n'
        'RRC_SLOT_VALUES: {"function":"valid_function"}',
    ],
)
def test_marker_schema_rejects_malformed_colliding_or_aliased_values(text: str) -> None:
    with pytest.raises(TemplateError):
        parse_task_metadata(replace(make_task(), text=text))


@pytest.mark.parametrize(
    "annotation",
    [
        "int",
        "list[str]",
        "typing.Optional[int]",
        "dict[str, list[int | None]]",
        "tuple[int, ...]",
    ],
)
def test_marker_schema_accepts_supported_type_grammar(annotation: str) -> None:
    task = Task(
        "valid-type",
        "Build valid_function.\n"
        f'RRC_SHAPE: {{"arity":1,"arg_types":["{annotation}"],'
        '"fields":["valid_field"]}\n'
        'RRC_SLOT_VALUES: {"function":"valid_function","field":"valid_field"}',
    )
    shape, _ = parse_task_metadata(task)
    assert shape.arg_types


def test_spec_rejects_invalid_identifier_and_type_label_values() -> None:
    base_task = make_task()
    identifier_task = replace(
        base_task,
        text=base_task.text.replace(
            '"field":"order_id"}', '"field":"order_id","alias":"bad-name"}'
        ),
    )
    identifier_spec = replace(
        make_spec(),
        plan=make_spec().plan + " bad-name",
        slots=replace(
            make_spec().slots,
            identifiers=("get_order", "bad-name"),
            values={**make_spec().slots.values, "alias": "bad-name"},
        ),
    )
    assert validate_spec_for_task(identifier_spec, identifier_task) is False

    type_task = replace(
        base_task,
        text=base_task.text.replace(
            '"field":"order_id"}', '"field":"order_id","type":"factory()"}'
        ),
    )
    type_spec = replace(
        make_spec(),
        plan=make_spec().plan + " factory()",
        slots=replace(
            make_spec().slots,
            types=("factory()",),
            values={**make_spec().slots.values, "type": "factory()"},
        ),
    )
    assert validate_spec_for_task(type_spec, type_task) is False


def test_full_spec_templatizes_nested_fields_and_round_trips_defensively() -> None:
    concrete = make_spec()
    template = templatize(concrete)
    skeleton = template.spec_skeleton
    assert skeleton.signature == "def {function}({field}: int) -> int"
    assert skeleton.slots.entity == "{entity}"
    assert skeleton.slots.identifiers == ("{function}",)
    assert skeleton.slots.fields == ("{field}",)
    assert skeleton.slots.values == {
        "entity": "{entity}",
        "field": "{field}",
        "function": "{function}",
    }
    rendered = render(template, dict(concrete.slots.values))
    assert rendered == concrete
    rendered.slots.values["field"] = "mutated"
    assert template.spec_skeleton.slots.values["field"] == "{field}"


def test_every_nested_slot_category_is_templatized() -> None:
    values = {
        "entity": "Order",
        "identifier": "get_order",
        "type": "Money",
        "field": "order_id",
        "constant": "ZERO",
        "edge": "EMPTY",
    }
    specification = Spec(
        plan="Order get_order Money order_id ZERO EMPTY",
        signature="def get_order(order_id: Money) -> Money",
        contract="Order Money ZERO EMPTY",
        tests=("def test_behavior(): assert get_order(ZERO) == EMPTY",),
        slots=Slots(
            entity="Order",
            identifiers=("get_order",),
            types=("Money",),
            fields=("order_id",),
            constants=("ZERO",),
            edge_values=("EMPTY",),
            values=values,
        ),
    )
    template = templatize(specification)
    slots = template.spec_skeleton.slots
    assert slots.entity == "{entity}"
    assert slots.identifiers == ("{identifier}",)
    assert slots.types == ("{type}",)
    assert slots.fields == ("{field}",)
    assert slots.constants == ("{constant}",)
    assert slots.edge_values == ("{edge}",)
    assert render(template, values) == specification


def test_external_ref_is_canonical_sha256_and_checked_on_load() -> None:
    template = templatize(make_spec())
    assert (
        template.external_ref == "193ee2b510f3c456b5b7a0454505857a5aab4e12d764dbd9b01d9ca1bdbcd224"
    )
    assert template.external_ref == fingerprint(template.spec_skeleton, template.slot_names)
    reversed_values = replace(
        make_spec().slots,
        values={"function": "get_order", "field": "order_id", "entity": "Order"},
    )
    assert (
        templatize(replace(make_spec(), slots=reversed_values)).external_ref
        == template.external_ref
    )
    corrupt = Template("0" * 64, template.spec_skeleton, template.slot_names)
    assert resolve_template(corrupt, make_task()) is None
    malformed = Template("x", None, ("entity",))  # type: ignore[arg-type]
    assert resolve_template(malformed, make_task()) is None


def test_templatize_rejects_preexisting_placeholders_and_concrete_leakage() -> None:
    concrete = make_spec()
    with pytest.raises(TemplateError):
        templatize(replace(concrete, plan=concrete.plan + " {entity}"))
    with pytest.raises(TemplateError):
        templatize(replace(concrete, contract="Orderly behavior returns order_id."))


def test_slot_name_may_equal_its_concrete_value_without_false_leakage() -> None:
    specification = Spec(
        plan="Use id.",
        signature="def fetch(id: int) -> int",
        contract="Return id.",
        tests=("def test_behavior(): assert fetch(1) == 1",),
        slots=Slots(fields=("id",), values={"id": "id"}),
    )
    template = templatize(specification)
    assert template.spec_skeleton.slots.values == {"id": "{id}"}
    assert render(template, {"id": "id"}) == specification


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (make_task(task_id="new", entity="Purchase", function="fetch_purchase", field="key"), True),
        (make_task(arg_type="str"), False),
        (
            replace(
                make_task(),
                text=make_task().text.replace(
                    '"arity":1,"arg_types":["int"]',
                    '"arity":2,"arg_types":["int","int"]',
                ),
            ),
            False,
        ),
        (
            replace(
                make_task(field="key"),
                text=make_task(field="key").text.replace(
                    '"fields":["key"]', '"fields":["order_id"]'
                ),
            ),
            False,
        ),
    ],
)
def test_resolution_requires_exact_key_arity_type_and_field_shape(
    task: Task, expected: bool
) -> None:
    resolved = resolve_template(templatize(make_spec()), task)
    assert (resolved is not None) is expected


def test_resolution_rejects_missing_extra_or_empty_slot_sets_and_bad_sanity() -> None:
    template = templatize(make_spec())
    base = make_task()
    extra = replace(
        base,
        text=base.text.replace('"field":"order_id"}', '"field":"order_id","extra":"EXTRA"}'),
    )
    empty = replace(base, text=base.text.replace('"field":"order_id"', '"field":""'))
    missing = replace(base, text=base.text.replace(',"field":"order_id"', ""))
    assert resolve_template(template, extra) is None
    assert resolve_template(template, empty) is None
    assert resolve_template(template, missing) is None

    no_function_test = replace(make_spec(), tests=('def test_behavior(): assert "get_order"',))
    assert resolve_template(templatize(no_function_test), base) is None


def test_field_set_matching_ignores_order_but_rejects_duplicates_at_parse_time() -> None:
    task = Task(
        "two-fields",
        "Build lookup for Record.\n"
        'RRC_SHAPE: {"arity":1,"arg_types":["int"],"fields":["beta","alpha"]}\n'
        'RRC_SLOT_VALUES: {"entity":"Record","function":"lookup",'
        '"first":"alpha","second":"beta"}',
    )
    specification = Spec(
        plan="Record lookup alpha beta",
        signature="def lookup(alpha: int) -> int",
        contract="Return alpha; beta is the secondary Record field.",
        tests=("def test_behavior(): assert lookup(1) == 1",),
        slots=Slots(
            entity="Record",
            identifiers=("lookup",),
            fields=("alpha", "beta"),
            values={
                "entity": "Record",
                "function": "lookup",
                "first": "alpha",
                "second": "beta",
            },
        ),
    )
    assert resolve_template(templatize(specification), task) is not None


def test_templatize_rejects_concrete_collisions_inside_spec() -> None:
    specification = Spec(
        plan="Use Order and Orders.",
        signature="def build(value: int) -> int",
        contract="Return value.",
        tests=("def test_behavior(): assert build(1) == 1",),
        slots=Slots(values={"one": "Order", "many": "Orders"}),
    )
    with pytest.raises(TemplateError):
        templatize(specification)
