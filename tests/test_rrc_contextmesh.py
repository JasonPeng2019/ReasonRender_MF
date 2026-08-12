from __future__ import annotations

import hashlib
import json

import pytest
from rrc.contextmesh import (
    AcceptedReceiptPayloadV1,
    CodingAssignmentV1,
    NonRRCTargetV1,
    ReceiptRecordV1,
    RRCAcceptedTargetV1,
    RRCNativeFallbackTargetV1,
    RRCPendingTargetV1,
    RRCRejectedTargetV1,
    WaitEnvelopeV1,
    WaitVerifierV1,
    WorkerContextAttestationV1,
    WorkerEvidenceV1,
    WorkerInitialMessageV1,
    build_wait_envelope,
    coding_assignment_from_message,
    compression_saved,
    parse_coding_assignment,
    parse_receipt_payload,
    parse_receipt_record,
    parse_wait_envelope,
    parse_worker_context_attestation,
    parse_worker_evidence,
    validate_wait_id,
)
from rrc.contract import TargetPreimageV1, Task


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _worker() -> WorkerEvidenceV1:
    return WorkerEvidenceV1(
        tool_use_id="tool-1",
        attempt_id="1" * 64,
        agent_id="agent-1",
        task_id="task-1",
        arm="warm",
        final_message_sha256="2" * 64,
        transcript_sha256="3" * 64,
        context_attestation_sha256="4" * 64,
        requested_provider="openai",
        requested_model="gpt-5.6-luna",
        requested_reasoning="low",
        requested_service_tier="priority",
        identity_attestation="native_partial",
        effective_provider="openai",
        effective_model="gpt-5.6-luna",
        effective_reasoning="low",
        effective_service_tier="unattested",
        input_tokens=100,
        cached_input_tokens=20,
        output_tokens=30,
        reasoning_output_tokens=5,
        provider_total_tokens=130,
    )


def test_worker_evidence_matches_the_frozen_native_partial_golden() -> None:
    worker = _worker()
    assert _sha(worker.canonical_bytes()) == (
        "4b085b072ea239a3229d6afc68a1613819f74df53e7a93b530da188010cce642"
    )
    assert parse_worker_evidence(worker.canonical_bytes()) == worker


@pytest.mark.parametrize(
    "mutation",
    (
        {"provider_total_tokens": 129},
        {"cached_input_tokens": 101},
        {"requested_service_tier": "fast"},
        {"identity_attestation": "usage_only"},
    ),
)
def test_worker_evidence_rejects_usage_and_identity_drift(mutation: dict[str, object]) -> None:
    value = json.loads(_worker().canonical_bytes())
    value.update(mutation)
    with pytest.raises(ValueError):
        parse_worker_evidence(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )


def test_worker_context_attestation_is_strict_and_round_trips() -> None:
    message = WorkerInitialMessageV1(0, "user", "a" * 64, 123)
    attestation = WorkerContextAttestationV1(
        attempt_id="1" * 64,
        tool_use_id="tool-1",
        agent_id="agent-1",
        fork_context=False,
        assignment_sha256="2" * 64,
        initial_messages=(message,),
        expected_initial_set_sha256="3" * 64,
        root_sentinel_sha256="4" * 64,
        parent_history_sha256="5" * 64,
    )
    assert parse_worker_context_attestation(attestation.canonical_bytes()) == attestation
    value = json.loads(attestation.canonical_bytes())
    value["fork_context"] = True
    with pytest.raises(ValueError, match="fork_context"):
        parse_worker_context_attestation(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        )


def test_receipt_payload_and_record_bind_exact_canonical_bytes() -> None:
    payload = AcceptedReceiptPayloadV1(
        attempt_id="1" * 64,
        accept_commit_id="a" * 64,
        artifact_path="solution.py",
        source_sha256="b" * 64,
        source_bytes=12,
        verification_result_sha256="c" * 64,
    )
    record = ReceiptRecordV1.bind(payload, artifact_record_sha256="d" * 64)
    assert record.receipt == _sha(payload.canonical_bytes())
    assert record.blob_path == f".rrcv2/receipts/{record.receipt}.v1.json"
    assert parse_receipt_payload(payload.canonical_bytes()) == payload
    assert parse_receipt_record(record.canonical_bytes()) == record


def test_coding_assignment_marker_is_strict_source_referencing_authority() -> None:
    assignment = CodingAssignmentV1(
        mode="warm",
        task=Task("task-1", "Implement value()."),
        source_path="solution.py",
        public_test_path="tests/public.v1.json",
        oracle_test_path="tests/hidden.v1.json",
        owned_paths=("solution.py",),
        target_preimage=TargetPreimageV1.regular("solution.py", "a" * 64, 12, 0o644),
    )
    assert parse_coding_assignment(assignment.canonical_bytes()) == assignment
    assert coding_assignment_from_message("Do the task.\n" + assignment.marker()) == assignment
    with pytest.raises(ValueError, match="one bounded"):
        coding_assignment_from_message(assignment.marker() + "\n" + assignment.marker())


def test_coding_assignment_rejects_duplicate_source_delivery_or_target_drift() -> None:
    task = Task("task-1", "Implement value().")
    with pytest.raises(ValueError, match="own exactly"):
        CodingAssignmentV1(
            mode="cold",
            task=task,
            source_path="solution.py",
            public_test_path="tests/public.v1.json",
            oracle_test_path=None,
            owned_paths=("solution.py", "other.py"),
            target_preimage=TargetPreimageV1.regular("solution.py", "a" * 64, 12, 0o644),
        )
    with pytest.raises(ValueError, match="greenfield"):
        CodingAssignmentV1(
            mode="cold",
            task=task,
            source_path=None,
            public_test_path="tests/public.v1.json",
            oracle_test_path=None,
            owned_paths=("solution.py",),
            target_preimage=TargetPreimageV1.regular("solution.py", "a" * 64, 12, 0o644),
        )


def _targets() -> tuple[
    RRCPendingTargetV1,
    RRCAcceptedTargetV1,
    RRCRejectedTargetV1,
    NonRRCTargetV1,
]:
    return (
        RRCPendingTargetV1("a", "1" * 64, 1000),
        RRCAcceptedTargetV1(
            "b",
            "2" * 64,
            "a" * 64,
            "results/b.py",
            "b" * 64,
            12,
            WaitVerifierV1("c" * 64, "d" * 64),
            True,
        ),
        RRCRejectedTargetV1("c", "3" * 64, "verification_failed", "e" * 64),
        NonRRCTargetV1("d", "completed", _sha(b"done"), "done"),
    )


def test_mixed_wait_matches_the_frozen_golden() -> None:
    envelope = build_wait_envelope("r1", _targets())
    assert envelope.wait_id == "d7a983c6ce0864bdfd355788dc0bd669c1b00e08918b5570378522b1774ed40e"
    assert _sha(envelope.canonical_bytes()) == (
        "6e72d8c40141bf623cfbadba0d22fa2b939b7f05a0e85b03318bd91bf8c00dfb"
    )
    assert parse_wait_envelope(envelope.canonical_bytes()) == envelope
    validate_wait_id("r1", envelope)


def test_fallback_wait_row_matches_the_frozen_golden() -> None:
    target = RRCNativeFallbackTargetV1("e", "5" * 64, "finisher_unavailable", "0" * 64, "f" * 64)
    raw = json.dumps(target.as_json(), sort_keys=True, separators=(",", ":")).encode()
    assert _sha(raw) == "fcd97bff20c462a17219a53c94e2f2232eae5827799a03d2d472779058f5149e"


@pytest.mark.parametrize(
    ("native", "delivered", "expected"),
    ((100, 64, True), (100, 65, False), (100, 66, False), (0, 0, False)),
)
def test_compression_saved_uses_the_frozen_integer_boundary(
    native: int, delivered: int, expected: bool
) -> None:
    assert compression_saved(native_utf8_bytes=native, delivered_utf8_bytes=delivered) is expected


def test_wait_rejects_reordering_duplicates_and_unknown_fields() -> None:
    targets = _targets()
    with pytest.raises(ValueError, match="sorted unique"):
        WaitEnvelopeV1("0" * 64, tuple(reversed(targets)))
    raw = json.loads(build_wait_envelope("r1", targets).canonical_bytes())
    raw["extra"] = True
    with pytest.raises(ValueError):
        parse_wait_envelope(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode())
