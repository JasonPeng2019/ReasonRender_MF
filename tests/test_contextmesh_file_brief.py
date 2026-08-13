from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from contextmesh.mcp.file_brief import (
    SUMMARY_FIELDS,
    BriefValidationError,
    brief_contract,
    peer_brief_size,
    summary_budget,
    target_summary_characters,
    validate_file_brief,
)
from harness.four_worker_plan import build_overlap_ledger, freeze_worker_plans, manifest_sha256


def _entry():
    plans = freeze_worker_plans()
    return next(
        item
        for item in build_overlap_ledger(plans, manifest_sha256(plans))
        if item.canonical_path == "ruleforge/evaluator.py"
    )


def _source() -> str:
    return "\n".join(f"def evaluate_{index}(request): return request" for index in range(1, 31)) + "\n"


def _brief() -> dict[str, str]:
    facts = "; ".join(_entry().required_facts)
    return {
        "purpose_and_api": "evaluate_1 through evaluate_30 return the request unchanged.",
        "data_and_dependencies": "Each function accepts request and has no imports, mutation, or collaborators.",
        "behaviour_and_failures": "Every function is a direct return; preserve that behavior and signature.",
        "plan_step_facts": facts,
        "anchors": "evaluate_1; evaluate_30",
    }


def test_valid_plan_bound_brief_is_normalized_and_detached() -> None:
    source = _source()
    payload = _brief()

    result = validate_file_brief(payload, _entry(), source)

    payload["purpose_and_api"] = "changed"
    assert result["summary"]["purpose_and_api"] != "changed"  # type: ignore[index]
    assert result["binding"]["brief_id"] == _entry().brief_id  # type: ignore[index]
    assert result["source"]["raw_size"] == len(source.encode("utf-8"))  # type: ignore[index]


def test_owner_contract_and_peer_budget_are_compact() -> None:
    source = _source()
    contract = brief_contract(_entry(), source)
    result = validate_file_brief(_brief(), _entry(), source)

    assert set(contract["required_fields"]) == set(SUMMARY_FIELDS)  # type: ignore[arg-type]
    assert contract["max_summary_bytes"] == summary_budget(_entry(), source)
    assert contract["target_summary_characters"] == target_summary_characters(_entry(), source)
    assert "authoring_rule" in contract
    assert "peer_step_requirements" not in contract
    assert "coverage_checklist" not in contract
    assert contract["required_source_facts"]
    assert isinstance(contract["peer_summary_encoding"], str)
    assert contract["peer_summary_encoding"].startswith("ordered array")
    assert "test matrices" in contract["authoring_rule"]  # type: ignore[operator]
    assert "total across all five" in contract["authoring_rule"]  # type: ignore[operator]
    assert target_summary_characters(_entry(), source) < summary_budget(_entry(), source)
    assert peer_brief_size(_entry().brief_id, result["summary"]) <= contract["max_peer_payload_bytes"]  # type: ignore[arg-type,index]


def test_budget_includes_the_brief_id_envelope_for_small_sources() -> None:
    entry = replace(_entry(), required_facts=())
    source = "\n".join(f"value_{index} = {index}" for index in range(1, 31)) + "\n"
    payload = {field: "x" for field in SUMMARY_FIELDS}

    result = validate_file_brief(payload, entry, source)

    assert peer_brief_size(entry.brief_id, result["summary"]) <= brief_contract(entry, source)["max_peer_payload_bytes"]  # type: ignore[arg-type,index]


def test_tiny_shared_source_keeps_all_five_facts_in_compact_peer_transport() -> None:
    entry = replace(_entry(), required_facts=())
    source = "x" * 359
    payload = {
        "purpose_and_api": "definition creates RuleDefinition.",
        "data_and_dependencies": "Imports RuleDefinition.",
        "behaviour_and_failures": "Returns fields unchanged.",
        "plan_step_facts": "Call with five named values.",
        "anchors": "definition.",
    }

    result = validate_file_brief(payload, entry, source)

    assert peer_brief_size(entry.brief_id, result["summary"]) <= brief_contract(entry, source)["max_peer_payload_bytes"]  # type: ignore[arg-type,index]


def test_owner_contract_keeps_peer_task_prose_out_of_tiny_source_brief() -> None:
    contract = brief_contract(_entry(), _source())
    rendered = __import__("json").dumps(contract, sort_keys=True)

    assert "minimum_daily_requests" not in rendered
    assert "test_limits_rule.py" not in rendered
    assert "approved_market" not in rendered
    assert "test_markets_rule.py" not in rendered
    assert "evaluate_definition" in rendered


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"purpose_and_api": ""}),
        lambda value: value.update({"anchors": ""}),
        lambda value: value.update({"unexpected": "field"}),
        lambda value: value.update({"plan_step_facts": "x" * 10_000}),
    ],
)
def test_incomplete_or_oversized_compact_briefs_are_rejected(mutate) -> None:
    payload = copy.deepcopy(_brief())
    mutate(payload)

    with pytest.raises(BriefValidationError, match="^brief_incomplete:"):
        validate_file_brief(payload, _entry(), _source())


def test_brief_that_omits_a_required_source_fact_is_rejected() -> None:
    payload = _brief()
    payload["plan_step_facts"] = "No source contract is present."

    with pytest.raises(BriefValidationError, match="missing required_source_fact"):
        validate_file_brief(payload, _entry(), _source())
