from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from rrc.contract import Slots, Spec, StructuralShapeV1, Task
from rrc.pipeline.template import (
    TemplateError,
    derive_bindings,
    parse_task_metadata,
    parse_template_bundle,
    render,
    resolve_template,
    retrieval_primary,
    signature_declaration_source,
    signature_symbols,
    template_bundle_bytes,
    templatize,
    tier_minus_one,
    validate_spec_for_task,
)

from tests.pipeline.helpers import make_spec, make_task


def test_task_owned_metadata_is_the_binding_authority() -> None:
    shape, values = parse_task_metadata(make_task())
    assert (shape.arity, shape.arg_types, shape.fields) == (1, ("int",), ("order_id",))
    assert values == {"entity": "Order", "field": "order_id", "function": "get_order"}


def test_spec_has_only_the_six_original_slot_fields() -> None:
    assert set(make_spec().slots.as_json()) == {
        "entity",
        "identifiers",
        "types",
        "fields",
        "constants",
        "edge_values",
    }


def test_templatize_round_trips_spec_and_independent_tests() -> None:
    task = make_task()
    spec = make_spec()
    independent = ("def test_independent():\n    assert get_order(7) == 7",)
    template = templatize(
        spec,
        independent,
        slot_values=task.slot_values,
        primary=task.primary,
    )
    rendered_spec, rendered_independent = render(template, dict(task.slot_values or ()))
    assert rendered_spec == spec
    assert rendered_independent == independent
    assert template.slot_names == ("entity", "field", "function")
    assert template.external_ref == hashlib.sha256(template_bundle_bytes(template)).hexdigest()


def test_template_bundle_strictly_reopens_and_rejects_mutation() -> None:
    task = make_task()
    template = templatize(make_spec(), slot_values=task.slot_values, primary=task.primary)
    raw = template_bundle_bytes(template)
    assert parse_template_bundle(raw) == template
    with pytest.raises(TemplateError):
        parse_template_bundle(raw + b"\n")


def test_resolve_exact_shape_uses_task_bindings() -> None:
    first = make_task()
    template = templatize(make_spec(), slot_values=first.slot_values, primary=first.primary)
    second = make_task(
        task_id="order-2",
        entity="Purchase",
        function="find_purchase",
        field="purchase_id",
    )
    rendered = resolve_template(template, second)
    assert rendered is not None
    assert rendered.signature == "def find_purchase(purchase_id: int) -> int"


def test_shape_mismatch_is_not_exact_reuse() -> None:
    task = make_task()
    template = templatize(make_spec(), slot_values=task.slot_values, primary=task.primary)
    mismatched = make_task(arg_type="str")
    assert resolve_template(template, mismatched) is None


def test_controller_binding_must_cover_every_semantic_value() -> None:
    with pytest.raises(TemplateError, match="cover"):
        derive_bindings(
            make_spec(),
            (("function", "get_order"),),
            primary="get_order",
        )


def test_derived_bindings_coalesce_exact_equal_semantic_values() -> None:
    spec = Spec(
        "Use User.",
        "def build(value: User) -> User",
        "Return User.",
        ("def test_build():\n    assert build(User())",),
        Slots(entity="User", types=("User",), identifiers=("build",)),
    )
    bindings, _ = derive_bindings(spec, None, primary="build")
    assert len([pair for pair in bindings if pair[1] == "User"]) == 1


def test_preexisting_placeholder_rejects() -> None:
    task = make_task()
    spec = replace(make_spec(), plan="Use {function}.")
    with pytest.raises(TemplateError, match="placeholder"):
        templatize(spec, slot_values=task.slot_values, primary=task.primary)


def test_validate_spec_rejects_task_shape_or_primary_drift() -> None:
    assert validate_spec_for_task(make_spec(), make_task())
    assert not validate_spec_for_task(make_spec(function="other"), make_task())


def test_fixed_shape_fields_are_not_required_to_be_controller_slots() -> None:
    task = Task(
        "select",
        "Select approved names.",
        primary="select_names",
        shape=StructuralShapeV1(("list[dict[str,str]]",), 1, ("name", "status")),
        slot_values=(("active_value", "approved"), ("function", "select_names")),
    )
    specification = Spec(
        "Select names with status approved.",
        "def select_names(records: list[dict[str, str]]) -> list[str]",
        "Return approved names.",
        (
            "def test_public():\n"
            "    assert select_names([{'name': 'a', 'status': 'approved'}]) == ['a']",
        ),
        Slots(
            identifiers=("select_names",),
            fields=("name", "status"),
            constants=("approved",),
        ),
    )

    assert validate_spec_for_task(specification, task)
    template = templatize(
        specification,
        slot_values=task.slot_values,
        primary=task.primary,
    )
    assert template.slot_names == ("active_value", "function")


def test_literal_slot_fragments_render_inside_python_strings() -> None:
    specification = Spec(
        "Split using ~.",
        "def split_value(raw: str) -> list[str]",
        "Use ~ as the delimiter.",
        ("def test_public():\n    assert split_value('a~b') == ['a', 'b']",),
        Slots(identifiers=("split_value",), constants=("~",)),
    )
    template = templatize(
        specification,
        slot_values=(("delimiter", "~"), ("function", "split_value")),
        primary="split_value",
    )
    rendered, _ = render(
        template,
        {"delimiter": ";", "function": "split_semicolon"},
    )

    assert rendered.tests == (
        "def test_public():\n    assert split_semicolon('a;b') == ['a', 'b']",
    )


def test_frozen_two_artifact_template_bundle_golden() -> None:
    specification = Spec(
        "Return 1.",
        "def get_order(x: int) -> int",
        "Returns 1.",
        ("def test_spec():\n    assert get_order(1) == 1",),
        Slots(identifiers=("get_order",), constants=("1",)),
    )
    template = templatize(
        specification,
        ("def test_independent():\n    assert get_order(1) == 1",),
        slot_values=(("constant", "1"), ("function", "get_order")),
        primary="get_order",
    )
    assert template.external_ref == (
        "90dc21dc7cf2ac6219408458ad709734c5c77cb0a917bea5e2ec7fd814d93771"
    )
    rendered, tests = render(
        template,
        {"constant": "2", "function": "fetch_order"},
    )
    assert rendered.tests == ("def test_spec():\n    assert fetch_order(1) == 2",)
    assert tests == ("def test_independent():\n    assert fetch_order(1) == 2",)


def test_header_only_signature_normalizes_to_verifier_declaration() -> None:
    assert signature_declaration_source("def f(x: int) -> int") == ("def f(x: int) -> int: ...")
    assert signature_symbols("def f(x: int) -> int") == {"f": ("int",)}


def test_general_multi_signature_accepts_null_primary_but_does_not_index() -> None:
    specification = Spec(
        "Provide both public operations.",
        "def alpha(x: int) -> int: ...\ndef beta(y: str) -> str: ...",
        "Each operation returns its argument.",
        (
            "def test_alpha():\n    assert alpha(1) == 1",
            "def test_beta():\n    assert beta('x') == 'x'",
        ),
        Slots(),
    )
    task = Task("multi", "Implement both public operations.")
    assert validate_spec_for_task(specification, task)
    assert retrieval_primary(specification, task) is None


def test_general_multi_signature_honors_explicit_top_level_or_method_primary() -> None:
    specification = Spec(
        "Provide the selected operation.",
        ("def alpha(x: int) -> int: ...\nclass Service:\n    def beta(self, y: str) -> str: ..."),
        "Return the supplied value.",
        ("def test_beta():\n    assert Service().beta('x') == 'x'",),
        Slots(),
    )
    task = Task(
        "method",
        "Implement Service.beta.",
        primary="Service.beta",
        shape=StructuralShapeV1(("str",), 1, ()),
        slot_values=(),
    )
    assert validate_spec_for_task(specification, task)
    assert retrieval_primary(specification, task) == "Service.beta"
    mismatched_hint = replace(task, primary="missing")
    assert not validate_spec_for_task(specification, mismatched_hint)
    assert validate_spec_for_task(specification, mismatched_hint, strict_primary=False)


def test_tier_minus_one_uses_real_positional_and_keyword_binding_rules() -> None:
    task = Task(
        "binding",
        "Implement f.",
        primary="f",
        shape=StructuralShapeV1(("int", "str", "bool"), 3, ()),
        slot_values=(),
    )
    base = Spec(
        "Implement f.",
        "def f(x: int, /, y: str = 'd', *, flag: bool) -> int: ...",
        "Return an integer.",
        ("def test_ok():\n    assert f(1, flag=True) == 1",),
        Slots(),
    )
    assert tier_minus_one(base, task)
    wrong = replace(base, tests=("def test_bad():\n    assert f(x=1, flag=True) == 1",))
    assert not tier_minus_one(wrong, task)
    starred = replace(base, tests=("def test_bad():\n    assert f(*(1,), flag=True) == 1",))
    assert not tier_minus_one(starred, task)
