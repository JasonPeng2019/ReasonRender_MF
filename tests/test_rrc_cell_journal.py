from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from rrc.cell_journal import RootToolEventV1, SQLiteCellJournal
from rrc.contextmesh import RRCRejectedTargetV1, build_wait_envelope
from rrc.contract import canonical_json_bytes
from rrc.dispatch_permit import (
    DispatchPermitV1,
    ProductCellDispatchRequestV1,
    product_call_id,
    read_authority,
)
from rrc.journal import (
    JournalConflict,
    RejectedCommitV1,
    RejectedOutcomeRecordV1,
    SealedAttemptInputsV1,
    SQLiteRRCRepository,
    TerminalClaimV1,
)


def _root(task_envelope_sha256: str) -> ProductCellDispatchRequestV1:
    request = ProductCellDispatchRequestV1(
        call_id="",
        scope="interactive",
        controller="contextmesh",
        task_id="demo-task",
        task_envelope_sha256=task_envelope_sha256,
        run_id="demo-run",
        replicate_id="interactive",
        arm="rrc_cold",
        branch="combined",
        stage="contextmesh_root_session",
        stage_ordinal=1,
        journal_cursor=1,
        cell_id="demo-cell",
        attempt_id=None,
        transport="contextmesh",
        surface_id="root_strong_medium_native",
    )
    return replace(request, call_id=product_call_id(request))


def _inputs(task_envelope_sha256: str) -> SealedAttemptInputsV1:
    return SealedAttemptInputsV1(
        task_envelope_sha256=task_envelope_sha256,
        mode="cold",
        flow_kind="spec_pipeline",
        transport="contextmesh",
        config_sha256="2" * 64,
        model_policy_sha256="3" * 64,
        verifier_policy_sha256="4" * 64,
    )


def _permit(call_id: str) -> DispatchPermitV1:
    return DispatchPermitV1(
        kind="product",
        call_id=call_id,
        surface_id="root_strong_medium_native",
        manifest_sha256="a" * 64,
        plan_review_seal_sha256="b" * 64,
        authority_set_sha256="c" * 64,
    )


def test_real_cell_journal_binds_only_a_started_matching_attempt_and_reopens(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rrcv2.sqlite3"
    envelope_sha = "1" * 64
    with SQLiteRRCRepository(database) as repository:
        attempt = repository.begin_attempt("owner", "operation", _inputs(envelope_sha))
        cells = SQLiteCellJournal(repository, authority_root=tmp_path / "authority")
        created = cells.begin_cell(_root(envelope_sha))
        cursor = cells.prepare_root_call(created)
        assert cursor.prior_state == "absent"
        assert cursor.journal_generation == 0
        started = cells.mark_root_started(
            cells.load_cell("demo-cell"),
            permit=_permit(created.root_call_id),
            session_id="launch-demo-cell",
            transcript_baseline_sha256=hashlib.sha256(b"").hexdigest(),
        )
        bound = cells.bind_attempt(
            started,
            tool_use_id="tool-1",
            attempt_id=attempt.attempt_id,
            task_envelope_sha256=envelope_sha,
            expected_generation=1,
        )
        assert read_authority(bound.binding_ref)
        assert read_authority(bound.journal_view.authority_ref)
        assert (
            cells.load_rooted_attempt_authority(cell_id="demo-cell", attempt_id=attempt.attempt_id)
            == bound.journal_view
        )
        cells.complete_bound_attempt(
            cell_id="demo-cell",
            attempt_id=attempt.attempt_id,
            tool_use_id="tool-1",
            agent_id="agent-1",
            expected_generation=1,
        )

    with SQLiteRRCRepository(database) as repository:
        reopened = SQLiteCellJournal(repository, authority_root=tmp_path / "authority")
        view = reopened.load_rooted_attempt_authority(
            cell_id="demo-cell", attempt_id=attempt.attempt_id
        )
        assert view is not None
        assert read_authority(view.root_permit_ref)
        reopened.complete_bound_attempt(
            cell_id="demo-cell",
            attempt_id=attempt.attempt_id,
            tool_use_id="tool-1",
            agent_id="agent-1",
            expected_generation=1,
        )


def test_real_cell_journal_rejects_cross_task_and_conflicting_spawn(tmp_path: Path) -> None:
    with SQLiteRRCRepository(tmp_path / "rrcv2.sqlite3") as repository:
        attempt = repository.begin_attempt("owner", "operation", _inputs("1" * 64))
        cells = SQLiteCellJournal(repository, authority_root=tmp_path / "authority")
        created = cells.begin_cell(_root("1" * 64))
        cells.prepare_root_call(created)
        started = cells.mark_root_started(
            cells.load_cell("demo-cell"),
            permit=_permit(created.root_call_id),
            session_id="launch-demo-cell",
            transcript_baseline_sha256="0" * 64,
        )
        with pytest.raises(JournalConflict, match="task envelopes differ"):
            cells.bind_attempt(
                started,
                tool_use_id="tool-1",
                attempt_id=attempt.attempt_id,
                task_envelope_sha256="9" * 64,
                expected_generation=1,
            )
        cells.bind_attempt(
            started,
            tool_use_id="tool-1",
            attempt_id=attempt.attempt_id,
            task_envelope_sha256="1" * 64,
            expected_generation=1,
        )
        cells.complete_bound_attempt(
            cell_id="demo-cell",
            attempt_id=attempt.attempt_id,
            tool_use_id="tool-1",
            agent_id="agent-1",
            expected_generation=1,
        )
        with pytest.raises(JournalConflict):
            cells.complete_bound_attempt(
                cell_id="demo-cell",
                attempt_id=attempt.attempt_id,
                tool_use_id="tool-1",
                agent_id="another-agent",
                expected_generation=1,
            )


def test_cell_root_usage_and_combined_union_are_restart_safe_and_fail_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rrcv2.sqlite3"
    terminal_before: bytes
    with SQLiteRRCRepository(database) as repository:
        attempt = repository.begin_attempt("owner", "operation", _inputs("1" * 64))
        cells = SQLiteCellJournal(repository, authority_root=tmp_path / "authority")
        created = cells.begin_cell(_root("1" * 64))
        cells.prepare_root_call(created)
        started = cells.mark_root_started(
            cells.load_cell("demo-cell"),
            permit=_permit(created.root_call_id),
            session_id="launch-demo-cell",
            transcript_baseline_sha256="0" * 64,
        )
        cells.bind_attempt(
            started,
            tool_use_id="tool-1",
            attempt_id=attempt.attempt_id,
            task_envelope_sha256="1" * 64,
            expected_generation=1,
        )
        cells.complete_bound_attempt(
            cell_id="demo-cell",
            attempt_id=attempt.attempt_id,
            tool_use_id="tool-1",
            agent_id="agent-1",
            expected_generation=1,
        )
        evidence = canonical_json_bytes({"kind": "spawn_failed", "v": 1})
        evidence_sha = repository.persist_rejection_evidence(attempt.attempt_id, evidence)
        outcome = RejectedOutcomeRecordV1(
            attempt_id=attempt.attempt_id,
            task_id="demo-task",
            mode="cold",
            transport="contextmesh",
            phase="spawn",
            reason="spawn_failed",
            source_state="preparing",
            valid_candidate_sha256=None,
            verification_result_sha256=None,
            evidence_sha256=evidence_sha,
            cost_event_ids=(),
        )
        rejected = RejectedCommitV1("9" * 64, outcome)
        repository.commit_rejected(
            attempt,
            TerminalClaimV1("cell-test", attempt.generation, attempt.state),
            rejected,
        )
        terminal_before = repository.load_terminal_intent(attempt.attempt_id)[1]  # type: ignore[index]
        wait = build_wait_envelope(
            "demo-run",
            (
                RRCRejectedTargetV1(
                    "agent-1",
                    attempt.attempt_id,
                    "spawn_failed",
                    hashlib.sha256(evidence).hexdigest(),
                ),
            ),
        )
        wait_raw = wait.canonical_bytes()
        cells.record_root_tool_event(
            cell_id="demo-cell",
            event=RootToolEventV1(
                "wait",
                "wait-1",
                hashlib.sha256(b"wait-input").hexdigest(),
                hashlib.sha256(wait_raw).hexdigest(),
                wait_raw,
            ),
            expected_generation=1,
        )
        observed = cells.observe_root_call(
            cell_id="demo-cell",
            expected_generation=1,
            root_session_id="root-session-1",
            prompt_sha256="1" * 64,
            final_message_sha256="2" * 64,
            transcript_sha256="3" * 64,
            transcript_bytes=100,
            requested_provider="openai",
            requested_model="gpt-5.5",
            requested_reasoning="medium",
            requested_service_tier="priority",
            identity_attestation="usage_only",
            effective_provider="unattested",
            effective_model="unattested",
            effective_reasoning="unattested",
            effective_service_tier="unattested",
            usage={
                "input_tokens": 10,
                "cached_input_tokens": 2,
                "output_tokens": 4,
                "reasoning_output_tokens": 1,
                "provider_total_tokens": 14,
            },
        )
        cost = cells.commit_root_cost_event(
            cell_id="demo-cell", expected_generation=observed.generation
        )
        assert cost.attempt_id is None
        combined = cells.build_combined_session(cell_id="demo-cell", round_id="demo-run")
        committed = cells.commit_combined_session(
            combined,
            expected_generation=cells.load_cell("demo-cell").generation,
        )
        assert committed.all_cost_event_ids == (created.root_call_id,)
        assert repository.load_terminal_intent(attempt.attempt_id)[1] == terminal_before  # type: ignore[index]

    with SQLiteRRCRepository(database) as repository:
        reopened = SQLiteCellJournal(repository, authority_root=tmp_path / "authority")
        combined = reopened.load_combined_session("demo-cell")
        assert combined is not None
        assert combined.root_session_id == "root-session-1"
        assert combined.attempts[0].terminal_kind == "rejected"
        assert repository.load_terminal_intent(attempt.attempt_id)[1] == terminal_before  # type: ignore[index]
