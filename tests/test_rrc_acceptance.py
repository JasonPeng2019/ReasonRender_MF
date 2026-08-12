from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from rrc.contract import Config, Slots, Spec, StructuralShapeV1, Task, canonical_json_bytes
from rrc.journal import (
    AcceptedCommitReceiptV1,
    AcceptedCommitV1,
    AcceptedOutcomeRecordV1,
    JournalConflict,
    RejectedCommitReceiptV1,
    RejectedCommitV1,
    RejectedOutcomeRecordV1,
    SealedAttemptInputsV1,
    SQLiteRRCRepository,
    TerminalClaimV1,
)
from rrc.pipeline.template import template_bundle_bytes, templatize
from rrc.pipeline.verify import (
    ArtifactRecordV1,
    CodeArtifactV1,
    VerificationResultV1,
    VerificationTierRowV1,
)
from rrc.retrieval import SQLiteHybridRetrieval, case_document


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _attempt(repo: SQLiteRRCRepository, *, mode: str = "baseline"):
    flow = "direct" if mode in {"baseline", "cheap_alone", "cascade"} else "spec_pipeline"
    attempt = repo.begin_attempt(
        "owner",
        f"op-{mode}",
        SealedAttemptInputsV1(
            task_envelope_sha256="1" * 64,
            mode=mode,  # type: ignore[arg-type]
            flow_kind=flow,  # type: ignore[arg-type]
            transport="sync",
            config_sha256="2" * 64,
            model_policy_sha256="3" * 64,
            verifier_policy_sha256="4" * 64,
        ),
    )
    return repo.claim_finishing(
        attempt,
        owner_id="sync-owner",
        expected_state="preparing",
        expected_generation=0,
        expected_cursor=0,
    )


def _verification(repo: SQLiteRRCRepository, attempt_id: str) -> tuple[str, ArtifactRecordV1]:
    source = "def f(x: int) -> int:\n    return x"
    artifact = CodeArtifactV1(attempt_id, "solution.py", source)
    artifact_raw = canonical_json_bytes(
        {
            "artifact_path": artifact.artifact_path,
            "attempt_id": artifact.attempt_id,
            "source": artifact.source,
            "v": 1,
        }
    )
    passed = b"passed\n"
    result = VerificationResultV1(
        attempt_id,
        "rrcv2_general_v1",
        _sha(artifact_raw),
        tuple(
            VerificationTierRowV1(name, "passed", _sha(artifact_raw), _sha(passed), _sha(b""))
            for name in ("assembly", "ruff", "pyright", "pytest")
        ),
        True,
    )
    return repo.persist_verification(artifact, result)


def _accepted(
    attempt_id: str,
    result_sha: str,
    artifact_record: ArtifactRecordV1,
    *,
    mode: str,
    bundle: bytes | None = None,
    case: bytes | None = None,
) -> AcceptedCommitV1:
    artifact_record_raw = canonical_json_bytes(
        {
            "artifact_path": artifact_record.artifact_path,
            "attempt_id": artifact_record.attempt_id,
            "blob_bytes": artifact_record.blob_bytes,
            "blob_mode": artifact_record.blob_mode,
            "blob_path": artifact_record.blob_path,
            "blob_sha256": artifact_record.blob_sha256,
            "source_bytes": artifact_record.source_bytes,
            "source_sha256": artifact_record.source_sha256,
            "v": 1,
        }
    )
    return AcceptedCommitV1(
        accept_commit_id="a" * 64,
        mode=mode,  # type: ignore[arg-type]
        transport="sync",
        outcome=AcceptedOutcomeRecordV1(
            attempt_id=attempt_id,
            task_id="task-1",
            mode=mode,  # type: ignore[arg-type]
            transport="sync",
            branch="direct" if mode == "baseline" else "miss",
            escalated=False,
            index_disposition=(
                "not_applicable"
                if mode == "baseline"
                else ("indexed" if case is not None else "unindexed_primary_ambiguous")
            ),
            artifact_record_sha256=_sha(artifact_record_raw),
            verification_result_sha256=result_sha,
            cost_event_ids=("call-1",),
        ),
        bundle=bundle,
        case_document=case,
        everos_target=None,
        everos_dispatch=None,
        outbox_row=None,
        artifact_record=artifact_record,
        receipt_record=None,
    )


def test_direct_acceptance_is_idempotent_and_writes_no_bundle(tmp_path: Path) -> None:
    with SQLiteRRCRepository(tmp_path / "journal.sqlite3") as repo:
        attempt = _attempt(repo)
        result_sha, artifact_record = _verification(repo, attempt.attempt_id)
        accepted = _accepted(
            attempt.attempt_id,
            result_sha,
            artifact_record,
            mode="baseline",
        )
        claim = TerminalClaimV1("sync-owner", 0, "finishing")
        first = repo.commit_accepted(attempt, claim, accepted)
        second = repo.reconcile_accepted(attempt, accepted)
        assert (
            first
            == second
            == AcceptedCommitReceiptV1(
                attempt.attempt_id,
                "a" * 64,
                _sha(accepted.outcome.canonical_bytes()),
            )
        )
        assert repo.load_attempt(attempt.attempt_id).state == "accepted"
        assert repo.get_bundle("b" * 64) is None


def test_warm_acceptance_stores_bundle_atomically_and_rejects_drift(tmp_path: Path) -> None:
    with SQLiteRRCRepository(tmp_path / "journal.sqlite3") as repo:
        attempt = _attempt(repo, mode="warm")
        result_sha, artifact_record = _verification(repo, attempt.attempt_id)
        template = templatize(
            Spec(
                "Return 1.",
                "def get_order(x: int) -> int",
                "Returns 1.",
                ("def test_spec():\n    assert get_order(1) == 1",),
                Slots(identifiers=("get_order",), constants=("1",)),
            ),
            ("def test_independent():\n    assert get_order(1) == 1",),
            slot_values=(("constant", "1"), ("function", "get_order")),
            primary="get_order",
        )
        bundle = template_bundle_bytes(template)
        task = Task(
            "task-1",
            "lookup one integer and return constant 1.",
            family="lookup",
            searchable_public=True,
            primary="get_order",
            shape=StructuralShapeV1(("int",), 1, ()),
            slot_values=(("constant", "1"), ("function", "get_order")),
        )
        case = case_document(task, Config("owner"), template).canonical_bytes()
        accepted = _accepted(
            attempt.attempt_id,
            result_sha,
            artifact_record,
            mode="warm",
            bundle=bundle,
            case=case,
        )
        receipt = repo.commit_accepted(
            attempt,
            TerminalClaimV1("sync-owner", 0, "finishing"),
            accepted,
        )
        assert receipt.outcome_sha256 == _sha(accepted.outcome.canonical_bytes())
        assert repo.get_bundle(_sha(bundle)) == bundle
        retrieval = SQLiteHybridRetrieval(repo)
        query = Task(
            "task-2",
            "lookup one integer and return constant 2.",
            family="lookup",
            searchable_public=True,
            primary="fetch_item",
            shape=StructuralShapeV1(("int",), 1, ()),
            slot_values=(("constant", "2"), ("function", "fetch_item")),
        )
        hits = retrieval.retrieve(query, Config("owner"))
        assert [hit.external_ref for hit in hits] == [template.external_ref]
        assert retrieval.classify(query, template.external_ref) == "exact"
        assert retrieval.get_template(template.external_ref) == template
        near = Task(
            "task-3",
            "lookup one integer and return constant 3.",
            family="lookup",
            searchable_public=True,
            primary="fetch_pair",
            shape=StructuralShapeV1(("int", "int"), 2, ()),
            slot_values=(("constant", "3"), ("function", "fetch_pair")),
        )
        assert [hit.external_ref for hit in retrieval.retrieve(near, Config("owner"))] == [
            template.external_ref
        ]
        assert retrieval.classify(near, template.external_ref) == "near"
        assert SQLiteHybridRetrieval(repo).retrieve(query, Config("other-owner")) == []
        with pytest.raises(JournalConflict):
            repo.reconcile_accepted(
                attempt,
                replace(accepted, accept_commit_id="b" * 64),
            )


def test_prepare_rejection_is_idempotent_and_has_zero_acceptance_store(tmp_path: Path) -> None:
    with SQLiteRRCRepository(tmp_path / "journal.sqlite3") as repo:
        attempt = repo.begin_attempt(
            "owner",
            "op-prepare-reject",
            SealedAttemptInputsV1(
                task_envelope_sha256="1" * 64,
                mode="cold",
                flow_kind="spec_pipeline",
                transport="sync",
                config_sha256="2" * 64,
                model_policy_sha256="3" * 64,
                verifier_policy_sha256="4" * 64,
            ),
        )
        evidence = canonical_json_bytes({"kind": "rejection", "reason": "invalid_spec", "v": 1})
        evidence_sha = repo.persist_rejection_evidence(attempt.attempt_id, evidence)
        outcome = RejectedOutcomeRecordV1(
            attempt_id=attempt.attempt_id,
            task_id="task-1",
            mode="cold",
            transport="sync",
            phase="prepare",
            reason="invalid_spec",
            source_state="preparing",
            valid_candidate_sha256=None,
            verification_result_sha256=None,
            evidence_sha256=evidence_sha,
            cost_event_ids=("call-1",),
        )
        rejected = RejectedCommitV1("b" * 64, outcome)
        first = repo.commit_rejected(
            attempt,
            TerminalClaimV1("sync-owner", 0, "preparing"),
            rejected,
        )
        second = repo.reconcile_rejected(attempt, rejected)
        assert (
            first
            == second
            == RejectedCommitReceiptV1(
                attempt.attempt_id,
                "b" * 64,
                _sha(outcome.canonical_bytes()),
            )
        )
        assert repo.load_attempt(attempt.attempt_id).state == "rejected"
        assert repo.get_bundle("b" * 64) is None


def test_verified_rejection_reopens_exact_candidate_result_and_evidence(tmp_path: Path) -> None:
    with SQLiteRRCRepository(tmp_path / "journal.sqlite3") as repo:
        attempt = _attempt(repo, mode="cold")
        source = "def f(x: int) -> int:\n    return 0"
        artifact = CodeArtifactV1(attempt.attempt_id, "solution.py", source)
        artifact_raw = canonical_json_bytes(
            {
                "artifact_path": artifact.artifact_path,
                "attempt_id": artifact.attempt_id,
                "source": artifact.source,
                "v": 1,
            }
        )
        failed = b"failed\n"
        diagnostic = canonical_json_bytes({"kind": "verification_failure", "tier": "ruff", "v": 1})
        result = VerificationResultV1(
            attempt.attempt_id,
            "rrcv2_general_v1",
            _sha(artifact_raw),
            (
                VerificationTierRowV1(
                    "assembly", "passed", _sha(artifact_raw), _sha(b"passed\n"), _sha(b"")
                ),
                VerificationTierRowV1(
                    "ruff", "failed", _sha(artifact_raw), _sha(failed), _sha(diagnostic)
                ),
            ),
            False,
        )
        artifact_sha, result_sha = repo.persist_rejected_verification(artifact, result)
        evidence_sha = repo.persist_rejection_evidence(
            attempt.attempt_id,
            canonical_json_bytes({"kind": "verification_failure", "tier": "ruff", "v": 1}),
        )
        outcome = RejectedOutcomeRecordV1(
            attempt_id=attempt.attempt_id,
            task_id="task-1",
            mode="cold",
            transport="sync",
            phase="verify",
            reason="verification_failed",
            source_state="finishing",
            valid_candidate_sha256=artifact_sha,
            verification_result_sha256=result_sha,
            evidence_sha256=evidence_sha,
            cost_event_ids=("call-1",),
        )
        rejected = RejectedCommitV1("c" * 64, outcome)
        repo.commit_rejected(
            attempt,
            TerminalClaimV1("sync-owner", 0, "finishing"),
            rejected,
        )
        assert repo.reconcile_rejected(attempt, rejected).reject_commit_id == "c" * 64
        with pytest.raises(JournalConflict):
            repo.reconcile_rejected(
                attempt,
                replace(
                    rejected,
                    rejected_outcome=replace(outcome, evidence_sha256="d" * 64),
                ),
            )
