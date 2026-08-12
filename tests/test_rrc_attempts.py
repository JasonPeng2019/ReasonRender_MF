from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from rrc.attempts import AttemptRepository, event_payload
from rrc.contextmesh import (
    AcceptedReceiptPayloadV1,
    ReceiptRecordV1,
    WorkerContextAttestationV1,
    WorkerEvidenceV1,
    WorkerInitialMessageV1,
)
from rrc.contract import (
    ArtifactRefV1,
    ReferencedTaskInputV1,
    TargetPreimageV1,
    Task,
    canonical_json_bytes,
    canonical_test_artifact_bytes,
    seal_task_input,
    task_envelope_bytes,
)
from rrc.journal import (
    AcceptedCommitV1,
    AcceptedOutcomeRecordV1,
    JournalConflict,
    SealedAttemptInputsV1,
    SQLiteRRCRepository,
    TerminalClaimV1,
)
from rrc.pipeline.solve import parse_worker_candidate
from rrc.pipeline.verify import (
    CodeArtifactV1,
    VerificationResultV1,
    VerificationTierRowV1,
    artifact_record_bytes,
    code_artifact_bytes,
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _envelope(tmp_path: Path):
    source = b"def value() -> int:\n    return 1"
    public = canonical_test_artifact_bytes(("def test_value():\n    assert value() == 1",))
    sealed = tmp_path / "assignment"
    (sealed / ".rrcv2").mkdir(parents=True)
    (sealed / "solution.py").write_bytes(source)
    (sealed / "solution.py").chmod(0o644)
    (sealed / ".rrcv2/public-tests.v1.json").write_bytes(public)
    (sealed / ".rrcv2/public-tests.v1.json").chmod(0o600)
    authority = ReferencedTaskInputV1(
        task=Task("task-1", "Implement value()."),
        sealed_root=sealed,
        source_ref=ArtifactRefV1(_sha(source), len(source), "solution.py"),
        public_test_ref=ArtifactRefV1(_sha(public), len(public), ".rrcv2/public-tests.v1.json"),
        oracle_ref=None,
        target_preimage=TargetPreimageV1.regular("solution.py", _sha(source), len(source), 0o644),
    )
    return seal_task_input(authority, input_root=(tmp_path / "owned-input").absolute())


def _prepared(tmp_path: Path):
    repo = SQLiteRRCRepository(tmp_path / "journal.sqlite3")
    attempts = AttemptRepository(repo)
    envelope = _envelope(tmp_path)
    envelope_raw = task_envelope_bytes(envelope)
    attempt = repo.begin_attempt(
        "owner",
        "cm-operation",
        SealedAttemptInputsV1(
            task_envelope_sha256=_sha(envelope_raw),
            mode="cold",
            flow_kind="spec_pipeline",
            transport="contextmesh",
            config_sha256="2" * 64,
            model_policy_sha256="3" * 64,
            verifier_policy_sha256="4" * 64,
        ),
    )
    assignment = canonical_json_bytes(
        {"kind": "rrcv2_coding_assignment", "task_id": "task-1", "v": 1}
    )
    attempts.register_prepared_input(
        attempt,
        task_envelope=envelope_raw,
        input_root=envelope.input_root,  # type: ignore[arg-type]
        assignment=assignment,
        expected_tool_use_id="tool-1",
    )
    attempt = repo.commit_prepared(
        attempt,
        canonical_json_bytes({"v": 1}),
        expected_state="preparing",
        expected_generation=0,
        expected_cursor=0,
    )
    attempt = attempts.prepare_native_worker(
        attempt,
        prompt="Implement the prepared task.",
        stage_ordinal=3,
    )
    return repo, attempts, envelope, assignment, attempt


def _stop_rows(attempt_id: str, assignment: bytes):
    transcript = b'{"type":"fixture-worker-transcript"}\n'
    final = canonical_json_bytes(
        {
            "artifact_path": "solution.py",
            "attempt_id": attempt_id,
            "source": "def value() -> int:\n    return 2",
            "v": 1,
        }
    )
    candidate = parse_worker_candidate(final, attempt_id=attempt_id, artifact_path="solution.py")
    context = WorkerContextAttestationV1(
        attempt_id=attempt_id,
        tool_use_id="tool-1",
        agent_id="agent-1",
        fork_context=False,
        assignment_sha256=_sha(assignment),
        initial_messages=(WorkerInitialMessageV1(0, "user", "a" * 64, 100),),
        expected_initial_set_sha256="b" * 64,
        root_sentinel_sha256="c" * 64,
        parent_history_sha256="d" * 64,
    )
    evidence = WorkerEvidenceV1(
        tool_use_id="tool-1",
        attempt_id=attempt_id,
        agent_id="agent-1",
        task_id="task-1",
        arm="cold",
        final_message_sha256=_sha(final),
        transcript_sha256=_sha(transcript),
        context_attestation_sha256=_sha(context.canonical_bytes()),
        requested_provider="openai",
        requested_model="gpt-5.6-luna",
        requested_reasoning="low",
        requested_service_tier="priority",
        identity_attestation="usage_only",
        effective_provider="unattested",
        effective_model="unattested",
        effective_reasoning="unattested",
        effective_service_tier="unattested",
        input_tokens=10,
        cached_input_tokens=0,
        output_tokens=5,
        reasoning_output_tokens=1,
        provider_total_tokens=15,
    )
    return (
        evidence.canonical_bytes(),
        context.canonical_bytes(),
        candidate.canonical_bytes(),
        transcript,
    )


@pytest.mark.parametrize("stop_first", (False, True))
def test_posttool_and_stop_orders_atomically_submit_one_attempt(
    tmp_path: Path, stop_first: bool
) -> None:
    repo, attempts, _envelope_value, assignment, attempt = _prepared(tmp_path)
    try:
        evidence, context, candidate, transcript = _stop_rows(attempt.attempt_id, assignment)
        spawn_payload = event_payload("post_tool_spawn", tool_use_id="tool-1", agent_id="agent-1")
        stop_payload = event_payload("subagent_stop", agent_id="agent-1")
        if stop_first:
            attempt = attempts.submit_stop(
                attempt,
                event_id="stop-1",
                worker_evidence=evidence,
                context_attestation=context,
                candidate=candidate,
                transcript=transcript,
                payload=stop_payload,
                now_ms=1_000,
            )
            assert attempt.state == "stop_pending_bind"
            attempt = attempts.bind_spawn(
                attempt,
                event_id="post-1",
                tool_use_id="tool-1",
                agent_id="agent-1",
                payload=spawn_payload,
            )
        else:
            attempt = attempts.bind_spawn(
                attempt,
                event_id="post-1",
                tool_use_id="tool-1",
                agent_id="agent-1",
                payload=spawn_payload,
            )
            assert attempt.state == "spawned"
            attempt = attempts.submit_stop(
                attempt,
                event_id="stop-1",
                worker_evidence=evidence,
                context_attestation=context,
                candidate=candidate,
                transcript=transcript,
                payload=stop_payload,
                now_ms=1_000,
            )
        assert attempt.state == "submitted"
        inventory = repo.load_call_inventory(attempt.attempt_id)
        assert len(inventory) == 1
        assert inventory[0][1] == "call_committed"
        assert inventory[0][2] is not None
        worker_cost = json.loads(inventory[0][2])
        assert worker_cost["stage"] == "implement"
        assert worker_cost["provider_total_tokens"] == 15
        claimed = attempts.claim_next(owner_scope="owner", owner_id="finisher", now_ms=2_000)
        assert claimed is not None
        assert claimed.state == "finishing"
        assert claimed.generation == 1
        submission = attempts.load_submission(claimed.attempt_id)
        assert submission.tool_use_id == "tool-1"
        assert submission.agent_id == "agent-1"
        assert submission.candidate.kind == "code"
        assert attempts.claim_next(owner_scope="owner", owner_id="other", now_ms=2_000) is None
    finally:
        repo.close()


def test_expired_dead_finisher_lease_is_recovered_once(tmp_path: Path) -> None:
    repo, attempts, _envelope_value, assignment, attempt = _prepared(tmp_path)
    try:
        evidence, context, candidate, transcript = _stop_rows(attempt.attempt_id, assignment)
        attempt = attempts.bind_spawn(
            attempt,
            event_id="post-1",
            tool_use_id="tool-1",
            agent_id="agent-1",
            payload=event_payload("post_tool_spawn", agent_id="agent-1"),
        )
        attempt = attempts.submit_stop(
            attempt,
            event_id="stop-1",
            worker_evidence=evidence,
            context_attestation=context,
            candidate=candidate,
            transcript=transcript,
            payload=event_payload("subagent_stop", agent_id="agent-1"),
            now_ms=1_000,
        )
        claimed = attempts.claim_next(
            owner_scope="owner", owner_id="finisher.dead.999", now_ms=2_000, lease_ms=1_000
        )
        assert claimed is not None and claimed.state == "finishing"
        assert attempts.reconcile_queue(
            owner_scope="owner", owner_is_alive=lambda _owner: False, now_ms=3_001
        ) == (attempt.attempt_id,)
        recovered = attempts.claim_next(
            owner_scope="owner", owner_id="finisher.live.1", now_ms=3_002
        )
        assert recovered is not None
        assert recovered.attempt_id == attempt.attempt_id
        assert recovered.generation == 2
    finally:
        repo.close()


def test_expired_live_finisher_lease_is_not_stolen(tmp_path: Path) -> None:
    repo, attempts, _envelope_value, assignment, attempt = _prepared(tmp_path)
    try:
        evidence, context, candidate, transcript = _stop_rows(attempt.attempt_id, assignment)
        attempt = attempts.bind_spawn(
            attempt,
            event_id="post-1",
            tool_use_id="tool-1",
            agent_id="agent-1",
            payload=event_payload("post_tool_spawn", agent_id="agent-1"),
        )
        attempts.submit_stop(
            attempt,
            event_id="stop-1",
            worker_evidence=evidence,
            context_attestation=context,
            candidate=candidate,
            transcript=transcript,
            payload=event_payload("subagent_stop", agent_id="agent-1"),
        )
        claimed = attempts.claim_next(
            owner_scope="owner", owner_id="finisher.live.1", now_ms=2_000, lease_ms=1_000
        )
        assert claimed is not None
        assert (
            attempts.reconcile_queue(
                owner_scope="owner", owner_is_alive=lambda _owner: True, now_ms=9_000
            )
            == ()
        )
        assert repo.load_attempt(attempt.attempt_id).state == "finishing"
    finally:
        repo.close()


def test_stop_before_bind_expiry_is_visible_to_recovery(tmp_path: Path) -> None:
    repo, attempts, _envelope_value, assignment, attempt = _prepared(tmp_path)
    try:
        evidence, context, candidate, transcript = _stop_rows(attempt.attempt_id, assignment)
        pending = attempts.submit_stop(
            attempt,
            event_id="stop-1",
            worker_evidence=evidence,
            context_attestation=context,
            candidate=candidate,
            transcript=transcript,
            payload=event_payload("subagent_stop", agent_id="agent-1"),
            now_ms=1_000,
        )
        assert pending.state == "stop_pending_bind"
        assert attempts.expired_stop_pending(owner_scope="owner", now_ms=20_999) == ()
        assert attempts.expired_stop_pending(owner_scope="owner", now_ms=21_000) == (pending,)
    finally:
        repo.close()


def test_exact_duplicate_is_noop_but_event_id_reuse_conflicts(tmp_path: Path) -> None:
    repo, attempts, _envelope_value, _assignment, attempt = _prepared(tmp_path)
    try:
        payload = event_payload("post_tool_spawn", tool_use_id="tool-1", agent_id="agent-1")
        spawned = attempts.bind_spawn(
            attempt,
            event_id="post-1",
            tool_use_id="tool-1",
            agent_id="agent-1",
            payload=payload,
        )
        duplicate = attempts.bind_spawn(
            spawned,
            event_id="post-1",
            tool_use_id="tool-1",
            agent_id="agent-1",
            payload=payload,
        )
        assert duplicate == spawned
        with pytest.raises(JournalConflict, match="reused"):
            attempts.observe_subagent_start(
                spawned,
                event_id="post-1",
                agent_id="agent-1",
                payload=event_payload("subagent_start", agent_id="agent-1"),
            )
    finally:
        repo.close()


def test_spoofed_worker_evidence_never_enqueues(tmp_path: Path) -> None:
    repo, attempts, _envelope_value, assignment, attempt = _prepared(tmp_path)
    try:
        evidence, context, candidate, transcript = _stop_rows(attempt.attempt_id, assignment)
        drift = bytearray(evidence)
        drift[evidence.index(b'"agent-1"')] = ord("z")
        with pytest.raises((JournalConflict, ValueError)):
            attempts.submit_stop(
                attempt,
                event_id="stop-1",
                worker_evidence=bytes(drift),
                context_attestation=context,
                candidate=candidate,
                transcript=transcript,
                payload=event_payload("subagent_stop", agent_id="agent-1"),
            )
        assert attempts.claim_next(owner_scope="owner", owner_id="finisher") is None
        assert repo.load_attempt(attempt.attempt_id).state == "prepared"
    finally:
        repo.close()


def test_contextmesh_acceptance_commits_and_reopens_receipt_atomically(tmp_path: Path) -> None:
    repo, attempts, _envelope_value, assignment, attempt = _prepared(tmp_path)
    try:
        evidence, context, candidate, transcript = _stop_rows(attempt.attempt_id, assignment)
        attempt = attempts.bind_spawn(
            attempt,
            event_id="post-1",
            tool_use_id="tool-1",
            agent_id="agent-1",
            payload=event_payload("post_tool_spawn", agent_id="agent-1"),
        )
        attempts.submit_stop(
            attempt,
            event_id="stop-1",
            worker_evidence=evidence,
            context_attestation=context,
            candidate=candidate,
            transcript=transcript,
            payload=event_payload("subagent_stop", agent_id="agent-1"),
        )
        claimed = attempts.claim_next(owner_scope="owner", owner_id="finisher")
        assert claimed is not None
        artifact = CodeArtifactV1(
            claimed.attempt_id, "solution.py", "def value() -> int:\n    return 2"
        )
        artifact_sha = _sha(code_artifact_bytes(artifact))
        passed = b"passed\n"
        result = VerificationResultV1(
            claimed.attempt_id,
            "rrcv2_general_v1",
            artifact_sha,
            tuple(
                VerificationTierRowV1(name, "passed", artifact_sha, _sha(passed), _sha(b""))
                for name in ("assembly", "ruff", "pyright", "pytest")
            ),
            True,
        )
        result_sha, artifact_record = repo.persist_verification(artifact, result)
        artifact_record_sha = _sha(artifact_record_bytes(artifact_record))
        payload = AcceptedReceiptPayloadV1(
            attempt_id=claimed.attempt_id,
            accept_commit_id="a" * 64,
            artifact_path="solution.py",
            source_sha256=artifact_record.source_sha256,
            source_bytes=artifact_record.source_bytes,
            verification_result_sha256=result_sha,
        )
        receipt_record = ReceiptRecordV1.bind(payload, artifact_record_sha256=artifact_record_sha)
        accepted = AcceptedCommitV1(
            accept_commit_id="a" * 64,
            mode="cold",
            transport="contextmesh",
            outcome=AcceptedOutcomeRecordV1(
                attempt_id=claimed.attempt_id,
                task_id="task-1",
                mode="cold",
                transport="contextmesh",
                branch="miss",
                escalated=False,
                index_disposition="cold_no_store",
                artifact_record_sha256=artifact_record_sha,
                verification_result_sha256=result_sha,
                cost_event_ids=(),
            ),
            bundle=None,
            case_document=None,
            everos_target=None,
            everos_dispatch=None,
            outbox_row=None,
            artifact_record=artifact_record,
            receipt_record=receipt_record.canonical_bytes(),
        )
        repo.commit_accepted(
            claimed,
            TerminalClaimV1("finisher", claimed.generation, "finishing"),
            accepted,
        )
        reopened_record, reopened_payload = repo.load_receipt(claimed.attempt_id, payload.receipt)
        assert reopened_record == receipt_record
        assert reopened_payload == payload
        assert repo.load_accepted_source(claimed.attempt_id, artifact_record_sha) == artifact.source
        attempts.close_queue(claimed.attempt_id)
    finally:
        repo.close()
