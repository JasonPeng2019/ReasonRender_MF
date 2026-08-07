from rrc.contract import Spec
from rrc.pipeline.solve import render, to_template


def test_template_round_trip_serializes_structured_params_deterministically() -> None:
    params = {
        "entity": "Order",
        "fields": ["id", "total"],
        "options": {"strict": True, "limit": 2},
    }
    concrete = Spec(
        signature="def build_Order() -> dict",
        template='Return fields ["id","total"] using {"limit":2,"strict":true}.',
        tests='def test_build(): assert build_Order() == {"id": 1, "total": 2}',
    )

    template = to_template(concrete, params)
    rendered = render(template, params)

    assert template.signature == "def build_{entity}() -> dict"
    assert template.template == "Return fields {fields} using {options}."
    assert rendered == concrete


def test_render_only_replaces_known_placeholders_and_preserves_literal_braces() -> None:
    template = Spec(
        signature="def {function}() -> dict",
        template='Return a literal mapping such as {"ok": True}.',
        tests='def test_result(): assert {function}() == {"ok": True}',
    )

    rendered = render(template, {"function": "result"})

    assert rendered.signature == "def result() -> dict"
    assert rendered.template == 'Return a literal mapping such as {"ok": True}.'
    assert rendered.tests == 'def test_result(): assert result() == {"ok": True}'
