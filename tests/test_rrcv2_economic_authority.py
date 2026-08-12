from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from rrc.dispatch_permit import AuthorityRef, canonical_json
from rrc.economic_authority import (
    EconomicAuthorityError,
    assemble_functional_result,
    economic_binding_overlay_v10,
    validate_economic_binding_overlay_v10,
    validate_workload_core_v6,
    workload_core_v6,
)

ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = ROOT / "contextmesh/bench/rrcv2_workload.json"
ORACLES = ROOT / "contextmesh/bench/rrcv2_oracles.json"
CAPABILITY_MANIFEST = (
    ROOT / ".generated/state/rrcv2-convergence/capability/capability-manifest.v1.json"
)
CAPABILITY_SUMMARY = (
    ROOT / ".generated/state/rrcv2-convergence/capability/capability-summary.v1.json"
)
REDESIGN = (
    ROOT / ".generated/state/rrcv2-convergence/capability/explore-strong/redesign-evidence.v2.json"
)
ANALYZER = ROOT / "contextmesh/bench/rrcv2_analyzer.py"
INVENTORY = ROOT / ".generated/state/rrcv2-convergence/verify/capability-evidence-inventory.v1.json"
SANDBOX_V2 = ROOT / ".generated/state/rrcv2-convergence/verify/sandbox-evidence.v2.json"
SETUP = ROOT / ".generated/state/rrcv2-convergence/verify/setup-accounting.v1.json"
ABANDONED = (
    ROOT
    / ".generated/state/rrcv2-convergence/verify/aborted-capability-attempt-1/archive-manifest.json"
)
OUTPUT_AUTHORITY_V1 = ROOT / ".generated/state/rrcv2-convergence/generated-output-authority.v1.json"
OUTPUT_AUTHORITY_V2 = (
    ROOT / ".generated/state/rrcv2-convergence/verify/generated-output-authority.v2.json"
)
OUTPUT_AUTHORITY_V3 = (
    ROOT / ".generated/state/rrcv2-convergence/verify/generated-output-authority.v3.json"
)
SETUP_V2 = ROOT / ".generated/state/rrcv2-convergence/verify/setup-accounting.v2.json"
WORKER_ATTESTATION = (
    ROOT / ".generated/state/rrcv2-convergence/verify/native-worker-context-attestation.v1.json"
)
ROOT_CALL = (
    ROOT / ".generated/state/rrcv2-convergence/capability/calls/cap-08-root-strong-medium-native"
)
WORKER_CALL = (
    ROOT / ".generated/state/rrcv2-convergence/capability/calls/cap-09-worker-small-low-native"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ref(path: Path) -> AuthorityRef:
    raw = path.read_bytes()
    return AuthorityRef(path=path, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))


def _core() -> dict[str, object]:
    return workload_core_v6(
        source_workload_path="contextmesh/bench/rrcv2_workload.json",
        source_workload_raw=WORKLOAD.read_bytes(),
        source_oracles_path="contextmesh/bench/rrcv2_oracles.json",
        source_oracles_raw=ORACLES.read_bytes(),
    )


def _overlay(core_sha256: str) -> dict[str, object]:
    workload = json.loads(WORKLOAD.read_text())
    return economic_binding_overlay_v10(
        workload_core_sha256=core_sha256,
        source_workload_sha256=_sha(WORKLOAD),
        source_oracles_sha256=_sha(ORACLES),
        capability_manifest_sha256=_sha(CAPABILITY_MANIFEST),
        capability_summary_sha256=_sha(CAPABILITY_SUMMARY),
        capability_evidence_inventory_sha256=_sha(INVENTORY),
        sandbox_evidence_v2_sha256=_sha(SANDBOX_V2),
        setup_accounting_sha256=_sha(SETUP),
        generated_output_authority_v2_sha256=_sha(OUTPUT_AUTHORITY_V2),
        redesign_evidence_sha256=_sha(REDESIGN),
        rate_payload_sha256=workload["economic_evidence"]["rate_payload_sha256"],
        analyzer_file_sha256=_sha(ANALYZER),
        analysis_sha256=workload["protocol"]["analysis_sha256"],
        setup_accounting_v2_sha256=_sha(SETUP_V2),
        worker_context_attestation_sha256=_sha(WORKER_ATTESTATION),
        generated_output_authority_v3_sha256=_sha(OUTPUT_AUTHORITY_V3),
    )


def test_workload_core_v6_deletes_only_superseded_economic_authority() -> None:
    core = _core()
    assert set(core) == {
        "v",
        "kind",
        "source_workload_path",
        "source_workload_sha256",
        "source_oracles_path",
        "source_oracles_sha256",
        "projection_sha256",
    }
    assert core["v"] == 6
    assert core["kind"] == "rrcv2_workload_core"
    assert (
        validate_workload_core_v6(
            core,
            source_workload_raw=WORKLOAD.read_bytes(),
            source_oracles_raw=ORACLES.read_bytes(),
        )
        == core
    )

    source = json.loads(WORKLOAD.read_text())
    for field in sorted(set(source) - {"economic_evidence"}):
        poisoned = copy.deepcopy(source)
        poisoned[field] = {"poisoned": field}
        with pytest.raises(EconomicAuthorityError):
            validate_workload_core_v6(
                core,
                source_workload_raw=canonical_json(poisoned),
                source_oracles_raw=ORACLES.read_bytes(),
            )

    permitted = copy.deepcopy(source)
    permitted["economic_evidence"] = {"superseded": True}
    permitted["protocol"]["workload_authority_sha256"] = "0" * 64
    projected = workload_core_v6(
        source_workload_path="contextmesh/bench/rrcv2_workload.json",
        source_workload_raw=canonical_json(permitted),
        source_oracles_path="contextmesh/bench/rrcv2_oracles.json",
        source_oracles_raw=ORACLES.read_bytes(),
    )
    assert projected["projection_sha256"] == core["projection_sha256"]


def test_workload_core_v6_rejects_oracle_and_schema_drift() -> None:
    core = _core()
    poisoned_oracles = json.loads(ORACLES.read_text())
    poisoned_oracles["tasks"][0]["task_id"] = "poisoned"
    with pytest.raises(EconomicAuthorityError):
        validate_workload_core_v6(
            core,
            source_workload_raw=WORKLOAD.read_bytes(),
            source_oracles_raw=canonical_json(poisoned_oracles),
        )
    for field in tuple(core):
        poisoned = dict(core)
        poisoned[field] = "poisoned"
        with pytest.raises(EconomicAuthorityError):
            validate_workload_core_v6(
                poisoned,
                source_workload_raw=WORKLOAD.read_bytes(),
                source_oracles_raw=ORACLES.read_bytes(),
            )
    poisoned = dict(core)
    poisoned["unknown"] = True
    with pytest.raises(EconomicAuthorityError):
        validate_workload_core_v6(
            poisoned,
            source_workload_raw=WORKLOAD.read_bytes(),
            source_oracles_raw=ORACLES.read_bytes(),
        )


def test_overlay_v10_is_functional_only_and_bound_to_measured_capability(tmp_path: Path) -> None:
    core = _core()
    core_path = tmp_path / "core.json"
    core_path.write_bytes(canonical_json(core))
    core_path.chmod(0o600)
    core_ref = _ref(core_path)
    overlay = _overlay(core_ref.sha256)
    assert overlay["capability_overhead_tokens_max"] == 65_536
    assert overlay["conservative_input_token_upper_bound"] == 196_608
    assert overlay["economic_claim_eligible"] is False
    assert overlay["request_priced_estimate_only"] is True
    assert (
        validate_economic_binding_overlay_v10(
            overlay,
            workload_core_ref=core_ref,
            source_workload_ref=_ref(WORKLOAD),
            source_oracles_ref=_ref(ORACLES),
            capability_manifest_ref=_ref(CAPABILITY_MANIFEST),
            capability_summary_ref=_ref(CAPABILITY_SUMMARY),
            capability_evidence_inventory_ref=_ref(INVENTORY),
            sandbox_evidence_v2_ref=_ref(SANDBOX_V2),
            setup_accounting_ref=_ref(SETUP),
            setup_accounting_v2_ref=_ref(SETUP_V2),
            worker_context_attestation_ref=_ref(WORKER_ATTESTATION),
            root_rollout_ref=_ref(ROOT_CALL / "rollout.jsonl"),
            worker_rollout_ref=_ref(WORKER_CALL / "rollout.jsonl"),
            root_environment_ref=_ref(ROOT_CALL / "environment.json"),
            worker_environment_ref=_ref(WORKER_CALL / "environment.json"),
            generated_output_authority_v3_ref=_ref(OUTPUT_AUTHORITY_V3),
            generated_output_authority_v2_ref=_ref(OUTPUT_AUTHORITY_V2),
            generated_output_authority_v1_ref=_ref(OUTPUT_AUTHORITY_V1),
            abandoned_manifest_ref=_ref(ABANDONED),
            redesign_evidence_ref=_ref(REDESIGN),
            analyzer_ref=_ref(ANALYZER),
        )
        == overlay
    )

    for field in tuple(overlay):
        poisoned = dict(overlay)
        value = poisoned[field]
        poisoned[field] = not value if isinstance(value, bool) else "poisoned"
        with pytest.raises(EconomicAuthorityError):
            validate_economic_binding_overlay_v10(
                poisoned,
                workload_core_ref=core_ref,
                source_workload_ref=_ref(WORKLOAD),
                source_oracles_ref=_ref(ORACLES),
                capability_manifest_ref=_ref(CAPABILITY_MANIFEST),
                capability_summary_ref=_ref(CAPABILITY_SUMMARY),
                capability_evidence_inventory_ref=_ref(INVENTORY),
                sandbox_evidence_v2_ref=_ref(SANDBOX_V2),
                setup_accounting_ref=_ref(SETUP),
                setup_accounting_v2_ref=_ref(SETUP_V2),
                worker_context_attestation_ref=_ref(WORKER_ATTESTATION),
                root_rollout_ref=_ref(ROOT_CALL / "rollout.jsonl"),
                worker_rollout_ref=_ref(WORKER_CALL / "rollout.jsonl"),
                root_environment_ref=_ref(ROOT_CALL / "environment.json"),
                worker_environment_ref=_ref(WORKER_CALL / "environment.json"),
                generated_output_authority_v3_ref=_ref(OUTPUT_AUTHORITY_V3),
                generated_output_authority_v2_ref=_ref(OUTPUT_AUTHORITY_V2),
                generated_output_authority_v1_ref=_ref(OUTPUT_AUTHORITY_V1),
                abandoned_manifest_ref=_ref(ABANDONED),
                redesign_evidence_ref=_ref(REDESIGN),
                analyzer_ref=_ref(ANALYZER),
            )


def test_functional_assembler_preserves_tokens_and_forbids_savings() -> None:
    overlay = _overlay("a" * 64)
    tokens = {
        "input_tokens": 100,
        "cached_input_tokens": 40,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,
        "provider_total_tokens": 120,
    }
    result = assemble_functional_result(
        overlay=overlay,
        token_metrics=tokens,
        public_accepted=True,
        hidden_oracle_passed=True,
    )
    assert result["token_metrics"] == tokens
    assert result["quality"] == {
        "hidden_oracle_passed": True,
        "public_accepted": True,
    }
    assert result["economic_claim_eligible"] is False
    assert result["savings_claimed"] is False

    with pytest.raises(EconomicAuthorityError):
        assemble_functional_result(
            overlay={**overlay, "economic_claim_eligible": True},
            token_metrics=tokens,
            public_accepted=True,
            hidden_oracle_passed=True,
        )
    with pytest.raises(EconomicAuthorityError):
        assemble_functional_result(
            overlay=overlay,
            token_metrics={**tokens, "provider_total_tokens": 119},
            public_accepted=True,
            hidden_oracle_passed=True,
        )


def test_final_setup_and_worker_context_authorities_are_exact() -> None:
    setup = json.loads(SETUP_V2.read_text())
    assert setup["abandoned_logical_result_rows"] == 4
    assert setup["redesign_logical_result_rows"] == 1
    assert setup["canonical_logical_result_rows"] == 9
    assert setup["total_known_setup_logical_result_rows"] == 14
    assert setup["total_known_setup_provider_visible_tokens"] == 183_932
    assert setup["provider_request_cardinality"] == "unattested"
    assert setup["product_arm_attribution"] == "none"

    attestation = json.loads(WORKER_ATTESTATION.read_text())
    assert attestation["fork_context"] is False
    assert attestation["worker_environment_source"] == "inherited_root_process"
    assert attestation["worker_session_id"] == attestation["root_session_id"]
    assert attestation["worker_agent_id"] != attestation["root_session_id"]
    assert attestation["worker_parent_thread_id"] == attestation["root_session_id"]
    assert [row["source"] for row in attestation["initial_messages"]] == [
        "base_developer",
        "environment",
        "subagent_start",
        "assignment",
    ]
    assert attestation["root_private_message_absent"] is True
