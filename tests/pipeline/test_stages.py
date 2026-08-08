import json

from rrc.contract import ModelRole, RunContext
from rrc.pipeline.stages import implement_stage, parse_spec, repair_stage, spec_stage
from rrc.pipeline.stubs import FakeModel, fake_completion

from tests.pipeline.helpers import make_spec, make_task, spec_json


def test_spec_stage_is_strict_single_shot_metered_and_hides_oracle() -> None:
    hidden_oracle = "def test_secret(): assert False"
    task = make_task(oracle_tests=hidden_oracle)
    model = FakeModel({"spec": [fake_completion(spec_json(), model="strong-fake", tokens=7)]})
    specification, event = spec_stage(task, model, RunContext("cold", task.task_id))
    assert specification == make_spec()
    assert len(model.calls) == 1
    assert model.calls[0][0] is ModelRole.STRONG
    assert hidden_oracle not in model.calls[0][1]
    assert all(
        phrase in model.calls[0][1]
        for phrase in ("Do not run commands", "edit files", "or explain")
    )
    assert "MUST be exactly one of the concrete values in RRC_SLOT_VALUES" in model.calls[0][1]
    assert (
        "Do not add parameter names, annotations, generic labels, or test literals"
        in model.calls[0][1]
    )
    assert "slots.identifiers contains only function/identifier slot values" in model.calls[0][1]
    assert "Tests must be self-contained" in model.calls[0][1]
    assert event.stage == "spec"
    assert event.provider == "fake"
    assert (event.arm, event.task_id, event.model, event.usage.total_tokens) == (
        "cold",
        task.task_id,
        "strong-fake",
        7,
    )


def test_parse_spec_rejects_duplicate_unknown_and_wrong_nested_fields() -> None:
    task = make_task()
    duplicate = spec_json().replace('"plan":', '"plan":"duplicate","plan":', 1)
    assert parse_spec(duplicate, task) is None
    unknown = json.loads(spec_json())
    unknown["unknown"] = True
    assert parse_spec(json.dumps(unknown), task) is None
    wrong = json.loads(spec_json())
    wrong["slots"]["values"]["field"] = "wrong"
    assert parse_spec(json.dumps(wrong), task) is None
    duplicate_tests = json.loads(spec_json())
    duplicate_tests["tests"] = [" test ", "test"]
    assert parse_spec(json.dumps(duplicate_tests), task) is None
    duplicate_category = json.loads(spec_json())
    duplicate_category["slots"]["fields"] = ["order_id", "order_id"]
    assert parse_spec(json.dumps(duplicate_category), task) is None
    padded_category = json.loads(spec_json())
    padded_category["slots"]["fields"] = [" order_id "]
    assert parse_spec(json.dumps(padded_category), task) is None
    literal_only = json.loads(spec_json())
    literal_only["tests"] = ['def test_behavior(): assert "get_order"']
    assert parse_spec(json.dumps(literal_only), task) is None

    cross_category = json.loads(spec_json())
    cross_category["slots"]["identifiers"].append("Order")
    assert parse_spec(json.dumps(cross_category), task) is not None


def test_implement_and_repair_each_make_one_small_model_call() -> None:
    model = FakeModel({"implement": ["bad"], "repair": ["fixed"]})
    ctx = RunContext("cold", "t")
    code, first = implement_stage(make_spec(), model, ctx)
    fixed, second = repair_stage(make_spec(), code, "failed", model, ctx)
    assert (code, fixed) == ("bad", "fixed")
    assert [call[0] for call in model.calls] == [ModelRole.SMALL, ModelRole.SMALL]
    assert [first.stage, second.stage] == ["implement", "repair"]
    assert all(
        phrase in call[1]
        for call in model.calls
        for phrase in ("Do not run commands", "edit files", "or explain")
    )
