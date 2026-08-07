from rrc.contract import Spec, Task
from rrc.pipeline.stages import implement, parse_spec, repair, spec, spec_prompt


def make_task() -> Task:
    return Task(
        task_id="identity-1",
        family="identity",
        params={"function": "identity"},
        text="Implement identity(value: int) -> int.",
        oracle_tests="SECRET ORACLE CONTENT",
    )


def test_spec_stage_parses_strict_json_and_hides_oracle_tests() -> None:
    calls: list[tuple[str, str]] = []

    def complete(prompt: str, model: str) -> tuple[str, int]:
        calls.append((prompt, model))
        return (
            '{"signature":"def {function}(value: int) -> int",'
            '"template":"Return value unchanged.",'
            '"tests":"def test_identity(): assert {function}(3) == 3"}',
            91,
        )

    result, tokens = spec(make_task(), complete, "strong-model")

    assert result == Spec(
        signature="def {function}(value: int) -> int",
        template="Return value unchanged.",
        tests="def test_identity(): assert {function}(3) == 3",
    )
    assert tokens == 91
    assert calls[0][1] == "strong-model"
    assert make_task().text in calls[0][0]
    assert make_task().oracle_tests not in calls[0][0]
    assert calls[0][0].endswith(
        "Output only the JSON object. Do not run commands, do not edit files, do not explain."
    )


def test_parse_spec_returns_none_for_malformed_or_wrongly_typed_json() -> None:
    assert parse_spec("not json") is None
    assert parse_spec('{"signature": 1, "template": "x", "tests": "y"}') is None


def test_implement_and_repair_are_single_shot_and_count_tokens() -> None:
    prompts: list[str] = []
    responses = iter([("def identity(value: int) -> int: return 0", 20), ("fixed", 7)])

    def complete(prompt: str, model: str) -> tuple[str, int]:
        assert model == "cheap-model"
        prompts.append(prompt)
        return next(responses)

    task_spec = Spec("def identity(value: int) -> int", "Return value.", "tests")

    code, implement_tokens = implement(task_spec, complete, "cheap-model")
    fixed, repair_tokens = repair(task_spec, code, "assertion failed", complete, "cheap-model")

    assert (code, implement_tokens) == ("def identity(value: int) -> int: return 0", 20)
    assert (fixed, repair_tokens) == ("fixed", 7)
    assert prompts[0].endswith(
        "Output only the Python code. Do not run commands, do not edit files, do not explain."
    )
    assert "assertion failed" in prompts[1]
    assert prompts[1].endswith(
        "Output only the Python code. Do not run commands, do not edit files, do not explain."
    )


def test_spec_prompt_requires_named_placeholders_for_every_parameter() -> None:
    task = Task(
        task_id="crud-1",
        family="crud",
        params={"entity": "Order", "fields": ["id", "total"]},
        text="Build an Order record with id and total fields.",
    )

    prompt = spec_prompt(task)

    assert "{entity}" in prompt
    assert "{fields}" in prompt
    assert "same-module pytest test functions" in prompt
