"""Durable RRCv2 attempt and provider-call journal.

This module owns the synchronous SQLite transaction kernel.  It deliberately
stores canonical bytes rather than reconstructing model or verifier results
from ambient process state after a restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeVar, cast

from rrc.contextmesh import (
    AcceptedReceiptPayloadV1,
    ReceiptRecordV1,
    parse_receipt_payload,
    parse_receipt_record,
)
from rrc.contract import canonical_json_bytes
from rrc.pipeline.verify import (
    ArtifactRecordV1,
    CodeArtifactV1,
    VerificationResultV1,
    artifact_record_bytes,
    code_artifact_bytes,
    parse_artifact_record,
    parse_code_artifact,
    parse_verification_result,
    verification_result_bytes,
)

if TYPE_CHECKING:
    from rrc.retrieval import RetrievalObservationV1

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SCOPE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_OPERATION_KEY = _SCOPE
_NAMESPACE = re.compile(r"[a-z0-9_]{1,32}\Z")
_STATEMENT_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ATTEMPT_STATES = frozenset(
    {
        "preparing",
        "prepared",
        "spawned",
        "stop_pending_bind",
        "submitted",
        "finishing",
        "accepted",
        "rejected",
    }
)
_RECOVERABLE_STATES = tuple(sorted(_ATTEMPT_STATES - {"accepted", "rejected"}))
_TERMINAL_STATES = frozenset({"accepted", "rejected"})
_MAX_CANONICAL_BYTES = 2 * 1024 * 1024
_T = TypeVar("_T")


class JournalError(RuntimeError):
    """Base failure for durable attempt authority operations."""


class JournalConflict(JournalError):
    """An immutable identity was reused with different bytes."""


class JournalStateError(JournalError):
    """A transition did not match its expected state/generation/cursor."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hex64(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _SCOPE.fullmatch(value) is None:
        raise ValueError(f"{name} does not use the frozen ASCII identifier grammar")
    return value


def _text(value: object, *, name: str, cap: int = 4096) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\r" in value:
        raise ValueError(f"{name} must be nonempty LF-only text")
    if len(value.encode("utf-8", errors="strict")) > cap:
        raise ValueError(f"{name} exceeds its byte cap")
    return value


def _canonical_blob(raw: object, *, name: str) -> bytes:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_CANONICAL_BYTES:
        raise ValueError(f"{name} must be bounded nonempty canonical bytes")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is not canonical JSON") from exc
    if canonical_json_bytes(value) != raw:
        raise ValueError(f"{name} is not canonical JSON")
    return raw


@dataclass(frozen=True)
class SealedAttemptInputsV1:
    """Pre-provider binding sealed by ``begin_attempt``."""

    task_envelope_sha256: str
    mode: Literal["baseline", "cheap_alone", "cascade", "cold", "warm"]
    flow_kind: Literal["direct", "spec_pipeline"]
    transport: Literal["sync", "contextmesh"]
    config_sha256: str
    model_policy_sha256: str
    verifier_policy_sha256: str
    v: int = 1

    def __post_init__(self) -> None:
        for name in (
            "task_envelope_sha256",
            "config_sha256",
            "model_policy_sha256",
            "verifier_policy_sha256",
        ):
            _hex64(getattr(self, name), name=name)
        if self.mode not in {"baseline", "cheap_alone", "cascade", "cold", "warm"}:
            raise ValueError("unknown attempt mode")
        expected_flow = (
            "direct" if self.mode in {"baseline", "cheap_alone", "cascade"} else "spec_pipeline"
        )
        if self.flow_kind != expected_flow:
            raise ValueError("attempt flow_kind does not match mode")
        if self.transport not in {"sync", "contextmesh"}:
            raise ValueError("unknown attempt transport")
        if self.transport == "contextmesh" and self.flow_kind != "spec_pipeline":
            raise ValueError("ContextMesh transport is allowed only for the Spec pipeline")
        if self.v != 1:
            raise ValueError("unknown sealed-attempt version")

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "config_sha256": self.config_sha256,
                "flow_kind": self.flow_kind,
                "mode": self.mode,
                "model_policy_sha256": self.model_policy_sha256,
                "task_envelope_sha256": self.task_envelope_sha256,
                "transport": self.transport,
                "v": 1,
                "verifier_policy_sha256": self.verifier_policy_sha256,
            }
        )


@dataclass(frozen=True)
class AttemptHandle:
    attempt_id: str
    authority_id: str
    database_uuid: str
    owner_scope: str
    operation_key: str
    sealed_inputs_sha256: str
    state: str
    generation: int
    cursor: int

    def __post_init__(self) -> None:
        _hex64(self.attempt_id, name="attempt_id")
        _hex64(self.database_uuid, name="database_uuid")
        _hex64(self.sealed_inputs_sha256, name="sealed_inputs_sha256")
        _identifier(self.owner_scope, name="owner_scope")
        _identifier(self.operation_key, name="operation_key")
        if self.state not in _ATTEMPT_STATES:
            raise ValueError("unknown attempt state")
        for value, name in ((self.generation, "generation"), (self.cursor, "cursor")):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"attempt {name} must be a nonnegative integer")


@dataclass(frozen=True)
class CallRecordV1:
    call_id: str
    stage: str
    stage_ordinal: int
    role: Literal["strong", "small", "native_worker", "root"]
    model: str
    settings_sha256: str
    prompt_sha256: str
    transcript_baseline_sha256: str
    v: int = 1

    def __post_init__(self) -> None:
        _identifier(self.call_id, name="call_id")
        for name in ("settings_sha256", "prompt_sha256", "transcript_baseline_sha256"):
            _hex64(getattr(self, name), name=name)
        _identifier(self.stage, name="stage")
        if (
            isinstance(self.stage_ordinal, bool)
            or not isinstance(self.stage_ordinal, int)
            or not 1 <= self.stage_ordinal <= 16
        ):
            raise ValueError("call stage_ordinal must be an integer from 1 through 16")
        if self.role not in {"strong", "small", "native_worker", "root"}:
            raise ValueError("unknown call role")
        _text(self.model, name="model", cap=256)
        if self.v != 1:
            raise ValueError("unknown call-record version")

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "call_id": self.call_id,
                "model": self.model,
                "prompt_sha256": self.prompt_sha256,
                "role": self.role,
                "settings_sha256": self.settings_sha256,
                "stage": self.stage,
                "stage_ordinal": self.stage_ordinal,
                "transcript_baseline_sha256": self.transcript_baseline_sha256,
                "v": 1,
            }
        )


@dataclass(frozen=True)
class UsageRecordV1:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int = 0
    v: int = 1

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input cannot exceed input tokens")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total tokens must equal input plus output")
        if self.v != 1:
            raise ValueError("unknown usage-record version")

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "cached_input_tokens": self.cached_input_tokens,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
                "v": 1,
            }
        )


@dataclass(frozen=True)
class TerminalClaimV1:
    """Compare-and-swap authority for one terminal transition."""

    owner_id: str
    generation: int
    expected_state: str
    v: int = 1

    def __post_init__(self) -> None:
        _identifier(self.owner_id, name="terminal owner_id")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("terminal generation must be a nonnegative integer")
        if self.expected_state not in _RECOVERABLE_STATES:
            raise ValueError("terminal expected_state must be nonterminal")
        if self.v != 1:
            raise ValueError("unknown terminal-claim version")

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "expected_state": self.expected_state,
                "generation": self.generation,
                "owner_id": self.owner_id,
                "v": 1,
            }
        )


ModeName = Literal["baseline", "cheap_alone", "cascade", "cold", "warm"]
TransportName = Literal["sync", "contextmesh"]
BranchName = Literal["direct", "miss", "reuse", "prime"]
IndexDisposition = Literal[
    "not_applicable",
    "cold_no_store",
    "indexed",
    "unindexed_primary_ambiguous",
    "unindexed_projection_unavailable",
]
RejectionPhase = Literal[
    "prepare",
    "spawn",
    "implement",
    "verify",
    "fallback",
    "deadline",
    "commit",
]
RejectionReason = Literal[
    "invalid_spec",
    "invalid_independent_tests",
    "invalid_candidate",
    "verification_failed",
    "fallback_failed",
    "deadline_exceeded",
    "ambiguous_paid_call",
    "spawn_failed",
    "evidence_invalid",
    "store_failure",
]


@dataclass(frozen=True)
class AcceptedOutcomeRecordV1:
    attempt_id: str
    task_id: str
    mode: ModeName
    transport: TransportName
    branch: BranchName
    escalated: bool
    index_disposition: IndexDisposition
    artifact_record_sha256: str
    verification_result_sha256: str
    cost_event_ids: tuple[str, ...]
    v: int = 1

    def __post_init__(self) -> None:
        _hex64(self.attempt_id, name="accepted outcome attempt_id")
        _identifier(self.task_id, name="accepted outcome task_id")
        if self.mode not in {"baseline", "cheap_alone", "cascade", "cold", "warm"}:
            raise ValueError("accepted outcome mode is invalid")
        if self.transport not in {"sync", "contextmesh"}:
            raise ValueError("accepted outcome transport is invalid")
        if self.branch not in {"direct", "miss", "reuse", "prime"}:
            raise ValueError("accepted outcome branch is invalid")
        if not isinstance(self.escalated, bool):
            raise TypeError("accepted outcome escalated must be a boolean")
        if self.index_disposition not in {
            "not_applicable",
            "cold_no_store",
            "indexed",
            "unindexed_primary_ambiguous",
            "unindexed_projection_unavailable",
        }:
            raise ValueError("accepted outcome index disposition is invalid")
        _hex64(self.artifact_record_sha256, name="artifact_record_sha256")
        _hex64(self.verification_result_sha256, name="verification_result_sha256")
        if not isinstance(self.cost_event_ids, tuple) or len(set(self.cost_event_ids)) != len(
            self.cost_event_ids
        ):
            raise ValueError("cost_event_ids must be an immutable unique tuple")
        for call_id in self.cost_event_ids:
            _identifier(call_id, name="cost_event_id")
        if self.v != 1:
            raise ValueError("unknown accepted-outcome version")

    def as_json(self) -> dict[str, object]:
        return {
            "artifact_record_sha256": self.artifact_record_sha256,
            "attempt_id": self.attempt_id,
            "branch": self.branch,
            "cost_event_ids": list(self.cost_event_ids),
            "escalated": self.escalated,
            "index_disposition": self.index_disposition,
            "mode": self.mode,
            "task_id": self.task_id,
            "transport": self.transport,
            "v": 1,
            "verification_result_sha256": self.verification_result_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())


@dataclass(frozen=True)
class AcceptedCommitV1:
    """Complete immutable acceptance intent consumed by the transactional port."""

    accept_commit_id: str
    mode: ModeName
    transport: TransportName
    outcome: AcceptedOutcomeRecordV1
    bundle: bytes | None
    case_document: bytes | None
    everos_target: bytes | None
    everos_dispatch: bytes | None
    outbox_row: bytes | None
    artifact_record: ArtifactRecordV1
    receipt_record: bytes | None
    v: int = 1

    def __post_init__(self) -> None:
        _hex64(self.accept_commit_id, name="accept_commit_id")
        if not isinstance(self.outcome, AcceptedOutcomeRecordV1):
            raise TypeError("accepted outcome must be AcceptedOutcomeRecordV1")
        if self.mode != self.outcome.mode or self.transport != self.outcome.transport:
            raise ValueError("accepted intent mode/transport differs from its outcome")
        if not isinstance(self.artifact_record, ArtifactRecordV1):
            raise TypeError("accepted artifact_record must be ArtifactRecordV1")
        for name in (
            "bundle",
            "case_document",
            "everos_target",
            "everos_dispatch",
            "outbox_row",
            "receipt_record",
        ):
            raw = getattr(self, name)
            if raw is not None:
                _canonical_blob(raw, name=name)
        if self.mode in {"baseline", "cheap_alone", "cascade"}:
            if self.transport != "sync" or any(
                value is not None
                for value in (
                    self.bundle,
                    self.case_document,
                    self.everos_target,
                    self.everos_dispatch,
                    self.outbox_row,
                    self.receipt_record,
                )
            ):
                raise ValueError("direct acceptance cannot contain retrieval or receipt rows")
            if (
                self.outcome.branch != "direct"
                or self.outcome.index_disposition != "not_applicable"
            ):
                raise ValueError("direct acceptance outcome classification is invalid")
        elif self.mode == "cold":
            if (
                any(
                    value is not None
                    for value in (
                        self.bundle,
                        self.case_document,
                        self.everos_target,
                        self.everos_dispatch,
                        self.outbox_row,
                    )
                )
                or self.outcome.index_disposition != "cold_no_store"
            ):
                raise ValueError("COLD acceptance cannot contain retrieval rows")
        elif self.mode == "warm":
            if self.bundle is None:
                raise ValueError("WARM acceptance requires a template bundle")
            if self.outcome.index_disposition == "indexed" and self.case_document is None:
                raise ValueError("indexed WARM acceptance requires a case document")
            if self.outcome.index_disposition != "indexed" and self.case_document is not None:
                raise ValueError("unindexed WARM acceptance cannot contain a case document")
            if (self.everos_target is None) != (self.everos_dispatch is None) or (
                self.everos_dispatch is None
            ) != (self.outbox_row is None):
                raise ValueError(
                    "EverOS target, dispatch, and outbox must be all-null or all-present"
                )
        else:
            raise ValueError("accepted intent mode is invalid")
        if self.transport == "sync" and self.receipt_record is not None:
            raise ValueError("sync acceptance cannot contain a receipt")
        if self.transport == "contextmesh" and self.receipt_record is None:
            raise ValueError("ContextMesh acceptance requires a receipt")
        if self.v != 1:
            raise ValueError("unknown accepted-commit version")

    def as_json(self) -> dict[str, object]:
        def value(raw: bytes | None) -> object:
            return None if raw is None else json.loads(raw)

        return {
            "accept_commit_id": self.accept_commit_id,
            "artifact_record": json.loads(artifact_record_bytes(self.artifact_record)),
            "bundle": value(self.bundle),
            "case_document": value(self.case_document),
            "everos_dispatch": value(self.everos_dispatch),
            "everos_target": value(self.everos_target),
            "mode": self.mode,
            "outbox_row": value(self.outbox_row),
            "outcome": self.outcome.as_json(),
            "receipt_record": value(self.receipt_record),
            "transport": self.transport,
            "v": 1,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())


def parse_accepted_commit(raw: bytes) -> AcceptedCommitV1:
    """Strictly reopen one canonical accepted intent."""

    raw = _canonical_blob(raw, name="accepted commit")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "accept_commit_id",
            "artifact_record",
            "bundle",
            "case_document",
            "everos_dispatch",
            "everos_target",
            "mode",
            "outbox_row",
            "outcome",
            "receipt_record",
            "transport",
            "v",
        }
        or value.get("v") != 1
    ):
        raise ValueError("accepted commit schema is invalid")
    outcome_value = value.get("outcome")
    if (
        not isinstance(outcome_value, dict)
        or set(outcome_value)
        != {
            "artifact_record_sha256",
            "attempt_id",
            "branch",
            "cost_event_ids",
            "escalated",
            "index_disposition",
            "mode",
            "task_id",
            "transport",
            "v",
            "verification_result_sha256",
        }
        or outcome_value.get("v") != 1
    ):
        raise ValueError("accepted outcome schema is invalid")
    cost_ids = outcome_value.get("cost_event_ids")
    if not isinstance(cost_ids, list) or any(not isinstance(item, str) for item in cost_ids):
        raise ValueError("accepted outcome cost-event inventory is invalid")

    def blob(name: str) -> bytes | None:
        item = value.get(name)
        return None if item is None else canonical_json_bytes(item)

    artifact_value = value.get("artifact_record")
    if not isinstance(artifact_value, dict):
        raise ValueError("accepted artifact record schema is invalid")
    outcome = AcceptedOutcomeRecordV1(
        attempt_id=cast(str, outcome_value.get("attempt_id")),
        task_id=cast(str, outcome_value.get("task_id")),
        mode=cast(ModeName, outcome_value.get("mode")),
        transport=cast(TransportName, outcome_value.get("transport")),
        branch=cast(BranchName, outcome_value.get("branch")),
        escalated=cast(bool, outcome_value.get("escalated")),
        index_disposition=cast(IndexDisposition, outcome_value.get("index_disposition")),
        artifact_record_sha256=cast(str, outcome_value.get("artifact_record_sha256")),
        verification_result_sha256=cast(str, outcome_value.get("verification_result_sha256")),
        cost_event_ids=tuple(cost_ids),
    )
    accepted = AcceptedCommitV1(
        accept_commit_id=cast(str, value.get("accept_commit_id")),
        mode=cast(ModeName, value.get("mode")),
        transport=cast(TransportName, value.get("transport")),
        outcome=outcome,
        bundle=blob("bundle"),
        case_document=blob("case_document"),
        everos_target=blob("everos_target"),
        everos_dispatch=blob("everos_dispatch"),
        outbox_row=blob("outbox_row"),
        artifact_record=parse_artifact_record(canonical_json_bytes(artifact_value)),
        receipt_record=blob("receipt_record"),
    )
    if accepted.canonical_bytes() != raw:
        raise ValueError("accepted commit is not canonical")
    return accepted


@dataclass(frozen=True)
class AcceptedCommitReceiptV1:
    attempt_id: str
    accept_commit_id: str
    outcome_sha256: str
    v: int = 1

    def __post_init__(self) -> None:
        _hex64(self.attempt_id, name="receipt attempt_id")
        _hex64(self.accept_commit_id, name="receipt accept_commit_id")
        _hex64(self.outcome_sha256, name="receipt outcome_sha256")
        if self.v != 1:
            raise ValueError("unknown accepted receipt version")


@dataclass(frozen=True)
class RejectedOutcomeRecordV1:
    """Exact terminal failure record; it never authorizes retrieval storage."""

    attempt_id: str
    task_id: str
    mode: ModeName
    transport: TransportName
    phase: RejectionPhase
    reason: RejectionReason
    source_state: str
    valid_candidate_sha256: str | None
    verification_result_sha256: str | None
    evidence_sha256: str
    cost_event_ids: tuple[str, ...]
    v: int = 1

    def __post_init__(self) -> None:
        _hex64(self.attempt_id, name="rejected outcome attempt_id")
        _identifier(self.task_id, name="rejected outcome task_id")
        if self.mode not in {"baseline", "cheap_alone", "cascade", "cold", "warm"}:
            raise ValueError("rejected outcome mode is invalid")
        if self.transport not in {"sync", "contextmesh"}:
            raise ValueError("rejected outcome transport is invalid")
        if self.phase not in {
            "prepare",
            "spawn",
            "implement",
            "verify",
            "fallback",
            "deadline",
            "commit",
        }:
            raise ValueError("rejected outcome phase is invalid")
        if self.reason not in {
            "invalid_spec",
            "invalid_independent_tests",
            "invalid_candidate",
            "verification_failed",
            "fallback_failed",
            "deadline_exceeded",
            "ambiguous_paid_call",
            "spawn_failed",
            "evidence_invalid",
            "store_failure",
        }:
            raise ValueError("rejected outcome reason is invalid")
        if self.source_state not in _RECOVERABLE_STATES:
            raise ValueError("rejected outcome source_state is invalid")
        if self.valid_candidate_sha256 is not None:
            _hex64(self.valid_candidate_sha256, name="valid_candidate_sha256")
        if self.verification_result_sha256 is not None:
            _hex64(self.verification_result_sha256, name="verification_result_sha256")
        _hex64(self.evidence_sha256, name="rejection evidence_sha256")
        if self.verification_result_sha256 is not None and self.valid_candidate_sha256 is None:
            raise ValueError("rejected verification requires its candidate")
        if self.phase in {"prepare", "spawn"} and (
            self.valid_candidate_sha256 is not None or self.verification_result_sha256 is not None
        ):
            raise ValueError("prepare/spawn rejection cannot name a candidate or verification")
        if self.phase in {"verify", "commit"} and (
            self.valid_candidate_sha256 is None or self.verification_result_sha256 is None
        ):
            raise ValueError("verify/commit rejection requires a candidate and verification")
        if self.phase == "implement" and self.verification_result_sha256 is not None:
            raise ValueError("implement rejection cannot name a verification result")
        if not isinstance(self.cost_event_ids, tuple) or len(set(self.cost_event_ids)) != len(
            self.cost_event_ids
        ):
            raise ValueError("cost_event_ids must be an immutable unique tuple")
        for call_id in self.cost_event_ids:
            _identifier(call_id, name="cost_event_id")
        if self.v != 1:
            raise ValueError("unknown rejected-outcome version")

    def as_json(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "cost_event_ids": list(self.cost_event_ids),
            "evidence_sha256": self.evidence_sha256,
            "mode": self.mode,
            "phase": self.phase,
            "reason": self.reason,
            "source_state": self.source_state,
            "task_id": self.task_id,
            "transport": self.transport,
            "v": 1,
            "valid_candidate_sha256": self.valid_candidate_sha256,
            "verification_result_sha256": self.verification_result_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())


@dataclass(frozen=True)
class RejectedCommitV1:
    reject_commit_id: str
    rejected_outcome: RejectedOutcomeRecordV1
    v: int = 1

    def __post_init__(self) -> None:
        _hex64(self.reject_commit_id, name="reject_commit_id")
        if not isinstance(self.rejected_outcome, RejectedOutcomeRecordV1):
            raise TypeError("rejected_outcome must be RejectedOutcomeRecordV1")
        if self.v != 1:
            raise ValueError("unknown rejected-commit version")

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "reject_commit_id": self.reject_commit_id,
                "rejected_outcome": self.rejected_outcome.as_json(),
                "v": 1,
            }
        )


def parse_rejected_commit(raw: bytes) -> RejectedCommitV1:
    """Strictly reopen one canonical rejected intent."""

    raw = _canonical_blob(raw, name="rejected commit")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "reject_commit_id",
            "rejected_outcome",
            "v",
        }
        or value.get("v") != 1
    ):
        raise ValueError("rejected commit schema is invalid")
    outcome_value = value.get("rejected_outcome")
    if (
        not isinstance(outcome_value, dict)
        or set(outcome_value)
        != {
            "attempt_id",
            "cost_event_ids",
            "evidence_sha256",
            "mode",
            "phase",
            "reason",
            "source_state",
            "task_id",
            "transport",
            "v",
            "valid_candidate_sha256",
            "verification_result_sha256",
        }
        or outcome_value.get("v") != 1
    ):
        raise ValueError("rejected outcome schema is invalid")
    cost_ids = outcome_value.get("cost_event_ids")
    if not isinstance(cost_ids, list) or any(not isinstance(item, str) for item in cost_ids):
        raise ValueError("rejected outcome cost-event inventory is invalid")
    rejected = RejectedCommitV1(
        reject_commit_id=cast(str, value.get("reject_commit_id")),
        rejected_outcome=RejectedOutcomeRecordV1(
            attempt_id=cast(str, outcome_value.get("attempt_id")),
            task_id=cast(str, outcome_value.get("task_id")),
            mode=cast(ModeName, outcome_value.get("mode")),
            transport=cast(TransportName, outcome_value.get("transport")),
            phase=cast(RejectionPhase, outcome_value.get("phase")),
            reason=cast(RejectionReason, outcome_value.get("reason")),
            source_state=cast(str, outcome_value.get("source_state")),
            valid_candidate_sha256=cast(str | None, outcome_value.get("valid_candidate_sha256")),
            verification_result_sha256=cast(
                str | None, outcome_value.get("verification_result_sha256")
            ),
            evidence_sha256=cast(str, outcome_value.get("evidence_sha256")),
            cost_event_ids=tuple(cost_ids),
        ),
    )
    if rejected.canonical_bytes() != raw:
        raise ValueError("rejected commit is not canonical")
    return rejected


@dataclass(frozen=True)
class RejectedCommitReceiptV1:
    attempt_id: str
    reject_commit_id: str
    outcome_sha256: str
    v: int = 1

    def __post_init__(self) -> None:
        _hex64(self.attempt_id, name="rejected receipt attempt_id")
        _hex64(self.reject_commit_id, name="rejected receipt reject_commit_id")
        _hex64(self.outcome_sha256, name="rejected receipt outcome_sha256")
        if self.v != 1:
            raise ValueError("unknown rejected receipt version")


ParticipantKind = Literal["create_table", "create_index", "select", "insert", "update", "delete"]


@dataclass(frozen=True)
class ParticipantStatementV1:
    """One immutable migration or runtime statement owned by an M4 namespace."""

    sql: str
    sha256: str
    kind: ParticipantKind
    v: int = 1

    def __post_init__(self) -> None:
        _hex64(self.sha256, name="participant SQL sha256")
        if (
            not isinstance(self.sql, str)
            or not self.sql
            or self.sql != self.sql.strip()
            or "\x00" in self.sql
            or "\r" in self.sql
            or ";" in self.sql
            or len(self.sql.encode("utf-8", errors="strict")) > 64 * 1024
        ):
            raise ValueError("participant SQL must be one bounded canonical statement")
        if _sha(self.sql.encode("utf-8", errors="strict")) != self.sha256:
            raise ValueError("participant SQL hash does not match its bytes")
        if self.kind not in {
            "create_table",
            "create_index",
            "select",
            "insert",
            "update",
            "delete",
        }:
            raise ValueError("unknown participant statement kind")
        if self.v != 1:
            raise ValueError("unknown participant-statement version")

    def as_json(self) -> dict[str, str]:
        return {"kind": self.kind, "sha256": self.sha256, "sql": self.sql}


def benchmark_operation_key(*, run_id: str, replicate_id: str, arm: str, task_id: str) -> str:
    raw = canonical_json_bytes(
        {
            "arm": _text(arm, name="arm", cap=256),
            "kind": "benchmark",
            "replicate_id": _text(replicate_id, name="replicate_id", cap=256),
            "run_id": _text(run_id, name="run_id", cap=256),
            "task_id": _text(task_id, name="task_id", cap=256),
            "v": 1,
        }
    )
    return "bench-" + _sha(raw)


def contextmesh_operation_key(
    *,
    repository_id: str,
    route_id: str,
    round_id: str,
    tool_use_id: str,
    task_id: str,
    assignment_sha256: str,
) -> str:
    _hex64(repository_id, name="repository_id")
    _hex64(assignment_sha256, name="assignment_sha256")
    raw = canonical_json_bytes(
        {
            "assignment_sha256": assignment_sha256,
            "kind": "contextmesh",
            "repository_id": repository_id,
            "round_id": _text(round_id, name="round_id", cap=256),
            "route_id": _text(route_id, name="route_id", cap=256),
            "task_id": _text(task_id, name="task_id", cap=256),
            "tool_use_id": _text(tool_use_id, name="tool_use_id", cap=256),
            "v": 1,
        }
    )
    return "cm-" + _sha(raw)


class SQLiteUnitOfWork:
    """One callback-scoped view of the repository's physical transaction."""

    def __init__(self, repository: SQLiteRRCRepository, transaction_identity: str) -> None:
        self._repository = repository
        self.transaction_identity = transaction_identity
        self._active = True

    @property
    def connection_identity(self) -> int:
        self._require_active()
        return id(self._repository._connection)

    def _require_active(self) -> None:
        if not self._active:
            raise JournalStateError("unit of work is no longer active")

    def put_bundle(self, external_ref: str, bundle: bytes) -> None:
        """Insert or byte-deduplicate one content-addressed template bundle."""

        self._require_active()
        external_ref = _hex64(external_ref, name="external_ref")
        bundle = _canonical_blob(bundle, name="template bundle")
        if _sha(bundle) != external_ref:
            raise JournalConflict("template bundle external_ref does not match its bytes")
        existing = self._repository._connection.execute(
            "SELECT bundle FROM rrcv2_bundles WHERE external_ref=?", (external_ref,)
        ).fetchone()
        if existing is not None:
            if existing[0] != bundle:
                raise JournalConflict("template bundle digest collision or stored corruption")
            return
        self._repository._connection.execute(
            "INSERT INTO rrcv2_bundles(external_ref,bundle) VALUES(?,?)",
            (external_ref, bundle),
        )

    def get_bundle(self, external_ref: str) -> bytes | None:
        self._require_active()
        external_ref = _hex64(external_ref, name="external_ref")
        row = self._repository._connection.execute(
            "SELECT bundle FROM rrcv2_bundles WHERE external_ref=?", (external_ref,)
        ).fetchone()
        if row is None:
            return None
        raw = cast(bytes, row[0])
        if _sha(raw) != external_ref:
            raise JournalConflict("stored template bundle does not match its external_ref")
        return _canonical_blob(raw, name="template bundle")

    def participant_cursor(self, namespace: str) -> TransactionParticipant:
        """Open an allowlisted namespace cursor on this same transaction."""

        self._require_active()
        return self._repository._participant_cursor(self, namespace)

    def _close(self) -> None:
        self._active = False


class TransactionParticipant:
    """Hash-registered SQL only; never exposes the physical SQLite cursor."""

    def __init__(
        self,
        uow: SQLiteUnitOfWork,
        namespace: str,
        statements: dict[str, ParticipantStatementV1],
    ) -> None:
        self._uow = uow
        self._namespace = namespace
        self._statements = statements
        self.transaction_identity = uow.transaction_identity
        self._rowcount = -1

    @property
    def rowcount(self) -> int:
        self._uow._require_active()
        return self._rowcount

    def _statement(self, statement_id: str, allowed: set[str]) -> ParticipantStatementV1:
        self._uow._require_active()
        if not isinstance(statement_id, str) or _STATEMENT_ID.fullmatch(statement_id) is None:
            raise ValueError("participant statement ID is invalid")
        try:
            statement = self._statements[statement_id]
        except KeyError as exc:
            raise JournalStateError("participant statement is not registered") from exc
        if statement.kind not in allowed:
            raise JournalStateError("participant statement kind is not valid for this operation")
        return statement

    def _run(self, statement: ParticipantStatementV1, params: tuple[object, ...]) -> sqlite3.Cursor:
        self._uow._require_active()
        if not isinstance(params, tuple):
            raise TypeError("participant SQL parameters must be a tuple")
        repository = self._uow._repository
        prefix = f"rrcv2p_{self._namespace}_"

        def authorize(
            action: int,
            first: str | None,
            second: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            forbidden = {
                sqlite3.SQLITE_ATTACH,
                sqlite3.SQLITE_DETACH,
                sqlite3.SQLITE_ALTER_TABLE,
                sqlite3.SQLITE_CREATE_INDEX,
                sqlite3.SQLITE_CREATE_TABLE,
                sqlite3.SQLITE_CREATE_TEMP_INDEX,
                sqlite3.SQLITE_CREATE_TEMP_TABLE,
                sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
                sqlite3.SQLITE_CREATE_TEMP_VIEW,
                sqlite3.SQLITE_CREATE_TRIGGER,
                sqlite3.SQLITE_CREATE_VIEW,
                sqlite3.SQLITE_DROP_INDEX,
                sqlite3.SQLITE_DROP_TABLE,
                sqlite3.SQLITE_DROP_TEMP_INDEX,
                sqlite3.SQLITE_DROP_TEMP_TABLE,
                sqlite3.SQLITE_DROP_TEMP_TRIGGER,
                sqlite3.SQLITE_DROP_TEMP_VIEW,
                sqlite3.SQLITE_DROP_TRIGGER,
                sqlite3.SQLITE_DROP_VIEW,
                sqlite3.SQLITE_PRAGMA,
                sqlite3.SQLITE_TRANSACTION,
                sqlite3.SQLITE_SAVEPOINT,
            }
            if action in forbidden:
                return sqlite3.SQLITE_DENY
            if action in {
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
            } and (not isinstance(first, str) or not first.startswith(prefix)):
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION and second == "load_extension":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        repository._connection.set_authorizer(authorize)
        try:
            cursor = repository._connection.execute(statement.sql, params)
            self._rowcount = cursor.rowcount
            return cursor
        except sqlite3.DatabaseError as exc:
            raise JournalStateError("participant statement was denied or failed") from exc
        finally:
            repository._connection.set_authorizer(None)

    def execute(self, statement_id: str, params: tuple[object, ...]) -> int:
        statement = self._statement(statement_id, {"insert", "update", "delete"})
        cursor = self._run(statement, params)
        return cursor.rowcount

    def fetch_one(self, statement_id: str, params: tuple[object, ...]) -> tuple[object, ...] | None:
        statement = self._statement(statement_id, {"select"})
        row = self._run(statement, params).fetchone()
        return None if row is None else tuple(row)

    def fetch_all(
        self, statement_id: str, params: tuple[object, ...]
    ) -> tuple[tuple[object, ...], ...]:
        statement = self._statement(statement_id, {"select"})
        return tuple(tuple(row) for row in self._run(statement, params).fetchall())


class SQLiteRRCRepository:
    """One mode-0600 SQLite authority for attempts and later acceptance rows."""

    atomic_warm = True

    def __init__(self, database: str | Path) -> None:
        self._path = Path(database)
        self._lock = threading.RLock()
        self._in_transaction = False
        self._closed = False
        self._prepare_database_path()
        self._connection = sqlite3.connect(str(self._path), timeout=30, check_same_thread=False)
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA synchronous=FULL")
        journal_mode = self._connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != "delete":
            self._connection.close()
            raise JournalError("SQLite journal mode is not the sealed DELETE setting")
        self._create_schema()
        self.database_uuid = self._load_database_uuid()
        self.authority_id = "rrcv2-sqlite-" + self.database_uuid

    def _prepare_database_path(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            observed = self._path.lstat()
        except FileNotFoundError:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(self._path, flags, 0o600)
            except FileExistsError:
                observed = self._path.lstat()
            else:
                os.close(descriptor)
                return
        if not stat.S_ISREG(observed.st_mode) or self._path.is_symlink():
            raise ValueError("journal database must be a regular non-symlink file")
        if stat.S_IMODE(observed.st_mode) != 0o600:
            raise ValueError("journal database must have exact mode 0600")

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS rrcv2_repository_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_attempts (
                attempt_id TEXT PRIMARY KEY,
                owner_scope TEXT NOT NULL,
                operation_key TEXT NOT NULL,
                sealed_inputs BLOB NOT NULL,
                sealed_inputs_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,
                generation INTEGER NOT NULL,
                cursor INTEGER NOT NULL,
                prepared BLOB,
                prepared_sha256 TEXT,
                terminal_outcome BLOB,
                terminal_owner TEXT,
                UNIQUE(owner_scope, operation_key)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_journal_events (
                attempt_id TEXT NOT NULL REFERENCES rrcv2_attempts(attempt_id),
                event_key TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                cursor_after INTEGER NOT NULL,
                PRIMARY KEY(attempt_id, event_key)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_calls (
                call_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL REFERENCES rrcv2_attempts(attempt_id),
                record BLOB NOT NULL,
                record_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,
                launch_identity BLOB,
                transcript_ref BLOB,
                usage BLOB,
                outcome BLOB,
                cost_event BLOB
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_deterministic_steps (
                attempt_id TEXT NOT NULL REFERENCES rrcv2_attempts(attempt_id),
                stage TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                output_sha256 TEXT NOT NULL,
                PRIMARY KEY(attempt_id,stage)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_bundles (
                external_ref TEXT PRIMARY KEY,
                bundle BLOB NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_code_artifacts (
                artifact_sha256 TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL REFERENCES rrcv2_attempts(attempt_id),
                artifact BLOB NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_verification_results (
                result_sha256 TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL REFERENCES rrcv2_attempts(attempt_id),
                code_artifact_sha256 TEXT NOT NULL REFERENCES rrcv2_code_artifacts(artifact_sha256),
                result BLOB NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_artifact_records (
                artifact_record_sha256 TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL UNIQUE REFERENCES rrcv2_attempts(attempt_id),
                code_artifact_sha256 TEXT NOT NULL REFERENCES rrcv2_code_artifacts(artifact_sha256),
                record BLOB NOT NULL,
                blob BLOB NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_receipts (
                attempt_id TEXT NOT NULL REFERENCES rrcv2_attempts(attempt_id),
                receipt TEXT NOT NULL,
                record BLOB NOT NULL,
                payload BLOB NOT NULL,
                artifact_record_sha256 TEXT NOT NULL
                    REFERENCES rrcv2_artifact_records(artifact_record_sha256),
                PRIMARY KEY(attempt_id,receipt)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_case_documents (
                document_sha256 TEXT PRIMARY KEY,
                document BLOB NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_projection_unavailable (
                attempt_id TEXT NOT NULL REFERENCES rrcv2_attempts(attempt_id),
                phase TEXT NOT NULL,
                record_sha256 TEXT NOT NULL UNIQUE,
                record BLOB NOT NULL,
                input BLOB NOT NULL,
                evidence BLOB NOT NULL,
                PRIMARY KEY(attempt_id,phase)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_case_index (
                row_id INTEGER PRIMARY KEY,
                case_v INTEGER NOT NULL,
                owner_scope TEXT NOT NULL,
                external_ref TEXT NOT NULL,
                document_sha256 TEXT NOT NULL,
                family TEXT,
                shape BLOB,
                slot_schema BLOB NOT NULL,
                searchable_text TEXT NOT NULL,
                UNIQUE(case_v,owner_scope,external_ref,document_sha256)
            ) STRICT;
            CREATE VIRTUAL TABLE IF NOT EXISTS rrcv2_case_fts USING fts5(
                owner_scope UNINDEXED,
                external_ref UNINDEXED,
                document_sha256 UNINDEXED,
                searchable_text,
                tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TABLE IF NOT EXISTS rrcv2_outbox (
                row_sha256 TEXT PRIMARY KEY,
                row BLOB NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_everos_targets (
                target_sha256 TEXT PRIMARY KEY,
                target BLOB NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_everos_dispatches (
                dispatch_sha256 TEXT PRIMARY KEY,
                target_sha256 TEXT NOT NULL REFERENCES rrcv2_everos_targets(target_sha256),
                dispatch BLOB NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_everos_outbox_v1 (
                case_v INTEGER NOT NULL,
                backend TEXT NOT NULL,
                operation TEXT NOT NULL,
                owner_scope TEXT NOT NULL,
                observation_id TEXT NOT NULL,
                external_ref TEXT NOT NULL,
                document_sha256 TEXT NOT NULL,
                target_sha256 TEXT NOT NULL REFERENCES rrcv2_everos_targets(target_sha256),
                dispatch_sha256 TEXT NOT NULL REFERENCES rrcv2_everos_dispatches(dispatch_sha256),
                row_sha256 TEXT NOT NULL UNIQUE,
                row BLOB NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                generation INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                lease_expires_ms INTEGER,
                last_error TEXT,
                UNIQUE(case_v,backend,operation,owner_scope,observation_id,target_sha256)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_acceptance_markers (
                accept_commit_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL UNIQUE REFERENCES rrcv2_attempts(attempt_id),
                intent_sha256 TEXT NOT NULL,
                outcome_sha256 TEXT NOT NULL,
                intent BLOB NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_rejection_evidence (
                evidence_sha256 TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL REFERENCES rrcv2_attempts(attempt_id),
                evidence BLOB NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_rejection_markers (
                reject_commit_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL UNIQUE REFERENCES rrcv2_attempts(attempt_id),
                intent_sha256 TEXT NOT NULL,
                outcome_sha256 TEXT NOT NULL,
                intent BLOB NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_oracle_scores (
                attempt_id TEXT PRIMARY KEY REFERENCES rrcv2_attempts(attempt_id),
                status TEXT NOT NULL,
                score INTEGER,
                evidence_sha256 TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_participant_migrations (
                namespace TEXT NOT NULL,
                version INTEGER NOT NULL,
                definition_sha256 TEXT NOT NULL,
                PRIMARY KEY(namespace,version)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS rrcv2_participant_statements (
                namespace TEXT NOT NULL,
                version INTEGER NOT NULL,
                statement_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                sql TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                PRIMARY KEY(namespace,version,statement_id),
                FOREIGN KEY(namespace,version)
                    REFERENCES rrcv2_participant_migrations(namespace,version)
            ) STRICT;
            """
        )
        columns = {
            cast(str, row[1])
            for row in self._connection.execute("PRAGMA table_info(rrcv2_attempts)").fetchall()
        }
        if "terminal_owner" not in columns:
            self._connection.execute("ALTER TABLE rrcv2_attempts ADD COLUMN terminal_owner TEXT")
        self._connection.commit()

    def _load_database_uuid(self) -> str:
        with self._connection:
            row = self._connection.execute(
                "SELECT value FROM rrcv2_repository_meta WHERE key='database_uuid'"
            ).fetchone()
            if row is None:
                value = secrets.token_hex(32)
                self._connection.execute(
                    "INSERT INTO rrcv2_repository_meta(key,value) VALUES('database_uuid',?)",
                    (value,),
                )
            else:
                value = row[0]
        return _hex64(value, name="database_uuid")

    def __enter__(self) -> SQLiteRRCRepository:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def run_immediate(self, callback: Callable[[SQLiteUnitOfWork], _T]) -> _T:
        """Run one callback on this repository's only connection under BEGIN IMMEDIATE."""

        with self._lock:
            if self._closed:
                raise JournalStateError("repository is closed")
            if self._in_transaction:
                raise JournalStateError("nested repository transactions are forbidden")
            self._in_transaction = True
            identity = secrets.token_hex(16)
            uow = SQLiteUnitOfWork(self, identity)
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                result = callback(uow)
                self._connection.commit()
                return result
            except BaseException:
                self._connection.rollback()
                raise
            finally:
                uow._close()
                self._in_transaction = False

    def begin_attempt(
        self,
        owner_scope: str,
        operation_key: str,
        sealed_inputs: SealedAttemptInputsV1,
    ) -> AttemptHandle:
        owner_scope = _identifier(owner_scope, name="owner_scope")
        operation_key = _identifier(operation_key, name="operation_key")
        if not isinstance(sealed_inputs, SealedAttemptInputsV1):
            raise TypeError("sealed_inputs must be SealedAttemptInputsV1")
        raw = sealed_inputs.canonical_bytes()
        digest = _sha(raw)

        def start(_uow: SQLiteUnitOfWork) -> AttemptHandle:
            row = self._connection.execute(
                """
                SELECT attempt_id,sealed_inputs,sealed_inputs_sha256,state,generation,cursor
                FROM rrcv2_attempts WHERE owner_scope=? AND operation_key=?
                """,
                (owner_scope, operation_key),
            ).fetchone()
            if row is not None:
                if row[1] != raw or row[2] != digest:
                    raise JournalConflict(
                        "operation key is already bound to different sealed inputs"
                    )
                return self._handle_from_row(owner_scope, operation_key, row)
            attempt_id = secrets.token_hex(32)
            self._connection.execute(
                """
                INSERT INTO rrcv2_attempts(
                    attempt_id,owner_scope,operation_key,sealed_inputs,sealed_inputs_sha256,
                    state,generation,cursor
                ) VALUES(?,?,?,?,?,'preparing',0,0)
                """,
                (attempt_id, owner_scope, operation_key, raw, digest),
            )
            return AttemptHandle(
                attempt_id,
                self.authority_id,
                self.database_uuid,
                owner_scope,
                operation_key,
                digest,
                "preparing",
                0,
                0,
            )

        return self.run_immediate(start)

    def _handle_from_row(
        self, owner_scope: str, operation_key: str, row: tuple[object, ...]
    ) -> AttemptHandle:
        return AttemptHandle(
            attempt_id=cast(str, row[0]),
            authority_id=self.authority_id,
            database_uuid=self.database_uuid,
            owner_scope=owner_scope,
            operation_key=operation_key,
            sealed_inputs_sha256=cast(str, row[2]),
            state=cast(str, row[3]),
            generation=cast(int, row[4]),
            cursor=cast(int, row[5]),
        )

    def _load_handle_row(self, attempt_id: str) -> AttemptHandle:
        _hex64(attempt_id, name="attempt_id")
        row = self._connection.execute(
            """
            SELECT owner_scope,operation_key,attempt_id,sealed_inputs,sealed_inputs_sha256,
                   state,generation,cursor
            FROM rrcv2_attempts WHERE attempt_id=?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise JournalStateError("attempt does not exist")
        return self._handle_from_row(cast(str, row[0]), cast(str, row[1]), row[2:])

    def load_attempt(self, attempt_id: str) -> AttemptHandle:
        with self._lock:
            return self._load_handle_row(attempt_id)

    def list_recoverable(self, owner_scope: str) -> tuple[AttemptHandle, ...]:
        owner_scope = _identifier(owner_scope, name="owner_scope")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT operation_key,attempt_id,sealed_inputs,sealed_inputs_sha256,
                       state,generation,cursor
                FROM rrcv2_attempts
                WHERE owner_scope=? AND state IN (?,?,?,?,?,?)
                ORDER BY attempt_id LIMIT 1024
                """,
                (owner_scope, *_RECOVERABLE_STATES),
            ).fetchall()
            return tuple(
                self._handle_from_row(owner_scope, cast(str, row[0]), row[1:]) for row in rows
            )

    def _require_expected(
        self,
        current: AttemptHandle,
        supplied: AttemptHandle,
        *,
        expected_state: str,
        expected_generation: int,
        expected_cursor: int,
    ) -> None:
        if (
            supplied.authority_id != self.authority_id
            or supplied.database_uuid != self.database_uuid
            or supplied.attempt_id != current.attempt_id
            or supplied.owner_scope != current.owner_scope
            or supplied.operation_key != current.operation_key
            or supplied.sealed_inputs_sha256 != current.sealed_inputs_sha256
        ):
            raise JournalConflict("attempt handle authority or binding differs")
        if expected_state not in _ATTEMPT_STATES:
            raise ValueError("unknown expected attempt state")
        if current.state != expected_state:
            raise JournalStateError("attempt state does not match expected state")
        if current.generation != expected_generation:
            raise JournalStateError("attempt generation does not match expected generation")
        if current.cursor != expected_cursor:
            raise JournalStateError("attempt cursor does not match expected cursor")

    def _transition(
        self,
        handle: AttemptHandle,
        *,
        event_key: str,
        payload: bytes,
        expected_state: str,
        expected_generation: int,
        expected_cursor: int,
        apply: Callable[[AttemptHandle], None],
    ) -> AttemptHandle:
        _text(event_key, name="event_key", cap=512)
        payload = _canonical_blob(payload, name="journal event payload")
        digest = _sha(payload)

        def transition(_uow: SQLiteUnitOfWork) -> AttemptHandle:
            current = self._load_handle_row(handle.attempt_id)
            duplicate = self._connection.execute(
                """
                SELECT payload_sha256 FROM rrcv2_journal_events
                WHERE attempt_id=? AND event_key=?
                """,
                (handle.attempt_id, event_key),
            ).fetchone()
            if duplicate is not None:
                if duplicate[0] != digest:
                    raise JournalConflict("event identity was reused with different bytes")
                return current
            self._require_expected(
                current,
                handle,
                expected_state=expected_state,
                expected_generation=expected_generation,
                expected_cursor=expected_cursor,
            )
            if current.state in _TERMINAL_STATES:
                raise JournalStateError("terminal attempts cannot transition")
            apply(current)
            cursor_after = current.cursor + 1
            self._connection.execute(
                "UPDATE rrcv2_attempts SET cursor=? WHERE attempt_id=?",
                (cursor_after, current.attempt_id),
            )
            self._connection.execute(
                """
                INSERT INTO rrcv2_journal_events(
                    attempt_id,event_key,payload_sha256,cursor_after
                ) VALUES(?,?,?,?)
                """,
                (current.attempt_id, event_key, digest, cursor_after),
            )
            return self._load_handle_row(current.attempt_id)

        return self.run_immediate(transition)

    def prepare_call(
        self,
        attempt: AttemptHandle,
        call_record: CallRecordV1,
        *,
        expected_state: str,
        expected_generation: int,
        expected_cursor: int,
    ) -> AttemptHandle:
        if not isinstance(call_record, CallRecordV1):
            raise TypeError("call_record must be CallRecordV1")
        record = call_record.canonical_bytes()

        def apply(current: AttemptHandle) -> None:
            existing = self._connection.execute(
                "SELECT attempt_id,record_sha256 FROM rrcv2_calls WHERE call_id=?",
                (call_record.call_id,),
            ).fetchone()
            if existing is not None:
                raise JournalConflict("call ID is already bound")
            self._connection.execute(
                """
                INSERT INTO rrcv2_calls(
                    call_id,attempt_id,record,record_sha256,state
                ) VALUES(?,?,?,?,'call_prepared')
                """,
                (call_record.call_id, current.attempt_id, record, _sha(record)),
            )

        return self._transition(
            attempt,
            event_key="prepare_call:" + call_record.call_id,
            payload=record,
            expected_state=expected_state,
            expected_generation=expected_generation,
            expected_cursor=expected_cursor,
            apply=apply,
        )

    def record_deterministic(
        self,
        attempt: AttemptHandle,
        stage: str,
        input_hash: str,
        output_hash: str,
        *,
        expected_state: str,
        expected_generation: int,
        expected_cursor: int,
    ) -> AttemptHandle:
        stage = _identifier(stage, name="deterministic stage")
        input_hash = _hex64(input_hash, name="deterministic input hash")
        output_hash = _hex64(output_hash, name="deterministic output hash")
        payload = canonical_json_bytes(
            {
                "input_sha256": input_hash,
                "output_sha256": output_hash,
                "stage": stage,
                "v": 1,
            }
        )

        def apply(current: AttemptHandle) -> None:
            existing = self._connection.execute(
                """
                SELECT input_sha256,output_sha256 FROM rrcv2_deterministic_steps
                WHERE attempt_id=? AND stage=?
                """,
                (current.attempt_id, stage),
            ).fetchone()
            if existing is not None:
                raise JournalConflict("deterministic stage is already bound")
            self._connection.execute(
                """
                INSERT INTO rrcv2_deterministic_steps(
                    attempt_id,stage,input_sha256,output_sha256
                ) VALUES(?,?,?,?)
                """,
                (current.attempt_id, stage, input_hash, output_hash),
            )

        return self._transition(
            attempt,
            event_key="record_deterministic:" + stage,
            payload=payload,
            expected_state=expected_state,
            expected_generation=expected_generation,
            expected_cursor=expected_cursor,
            apply=apply,
        )

    def mark_call_started(
        self,
        attempt: AttemptHandle,
        call_id: str,
        launch_identity: bytes,
        *,
        expected_state: str,
        expected_generation: int,
        expected_cursor: int,
    ) -> AttemptHandle:
        call_id = _identifier(call_id, name="call_id")
        launch_identity = _canonical_blob(launch_identity, name="launch identity")
        payload = canonical_json_bytes(
            {"call_id": call_id, "launch_identity_sha256": _sha(launch_identity), "v": 1}
        )

        def apply(current: AttemptHandle) -> None:
            result = self._connection.execute(
                """
                UPDATE rrcv2_calls SET state='call_started',launch_identity=?
                WHERE call_id=? AND attempt_id=? AND state='call_prepared'
                """,
                (launch_identity, call_id, current.attempt_id),
            )
            if result.rowcount != 1:
                raise JournalStateError("call is not in call_prepared state")

        return self._transition(
            attempt,
            event_key="mark_call_started:" + call_id,
            payload=payload,
            expected_state=expected_state,
            expected_generation=expected_generation,
            expected_cursor=expected_cursor,
            apply=apply,
        )

    def observe_call(
        self,
        attempt: AttemptHandle,
        call_id: str,
        transcript_ref: bytes,
        usage: UsageRecordV1,
        *,
        expected_state: str,
        expected_generation: int,
        expected_cursor: int,
    ) -> AttemptHandle:
        call_id = _identifier(call_id, name="call_id")
        transcript_ref = _canonical_blob(transcript_ref, name="transcript reference")
        if not isinstance(usage, UsageRecordV1):
            raise TypeError("usage must be UsageRecordV1")
        usage_raw = usage.canonical_bytes()
        payload = canonical_json_bytes(
            {
                "call_id": call_id,
                "transcript_ref_sha256": _sha(transcript_ref),
                "usage_sha256": _sha(usage_raw),
                "v": 1,
            }
        )

        def apply(current: AttemptHandle) -> None:
            result = self._connection.execute(
                """
                UPDATE rrcv2_calls SET state='call_observed',transcript_ref=?,usage=?
                WHERE call_id=? AND attempt_id=? AND state='call_started'
                """,
                (transcript_ref, usage_raw, call_id, current.attempt_id),
            )
            if result.rowcount != 1:
                raise JournalStateError("call is not in call_started state")

        return self._transition(
            attempt,
            event_key="observe_call:" + call_id,
            payload=payload,
            expected_state=expected_state,
            expected_generation=expected_generation,
            expected_cursor=expected_cursor,
            apply=apply,
        )

    def commit_call(
        self,
        attempt: AttemptHandle,
        call_id: str,
        *,
        outcome: bytes,
        cost_event: bytes,
        expected_state: str,
        expected_generation: int,
        expected_cursor: int,
    ) -> AttemptHandle:
        call_id = _identifier(call_id, name="call_id")
        outcome = _canonical_blob(outcome, name="call outcome")
        cost_event = _canonical_blob(cost_event, name="cost event")
        payload = canonical_json_bytes(
            {
                "call_id": call_id,
                "cost_event_sha256": _sha(cost_event),
                "outcome_sha256": _sha(outcome),
                "v": 1,
            }
        )

        def apply(current: AttemptHandle) -> None:
            result = self._connection.execute(
                """
                UPDATE rrcv2_calls SET state='call_committed',outcome=?,cost_event=?
                WHERE call_id=? AND attempt_id=? AND state='call_observed'
                """,
                (outcome, cost_event, call_id, current.attempt_id),
            )
            if result.rowcount != 1:
                raise JournalStateError("call is not in call_observed state")

        return self._transition(
            attempt,
            event_key="commit_call:" + call_id,
            payload=payload,
            expected_state=expected_state,
            expected_generation=expected_generation,
            expected_cursor=expected_cursor,
            apply=apply,
        )

    def commit_prepared(
        self,
        attempt: AttemptHandle,
        prepared: bytes,
        *,
        expected_state: str,
        expected_generation: int,
        expected_cursor: int,
    ) -> AttemptHandle:
        prepared = _canonical_blob(prepared, name="prepared solve")
        payload = canonical_json_bytes({"prepared_sha256": _sha(prepared), "v": 1})

        def apply(current: AttemptHandle) -> None:
            result = self._connection.execute(
                """
                UPDATE rrcv2_attempts
                SET state='prepared',prepared=?,prepared_sha256=?
                WHERE attempt_id=? AND state='preparing'
                """,
                (prepared, _sha(prepared), current.attempt_id),
            )
            if result.rowcount != 1:
                raise JournalStateError("attempt cannot commit prepared state")

        return self._transition(
            attempt,
            event_key="commit_prepared",
            payload=payload,
            expected_state=expected_state,
            expected_generation=expected_generation,
            expected_cursor=expected_cursor,
            apply=apply,
        )

    def load_prepared(self, attempt_id: str) -> bytes | None:
        _hex64(attempt_id, name="attempt_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT prepared,prepared_sha256 FROM rrcv2_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise JournalStateError("attempt does not exist")
        if row[0] is None:
            return None
        raw = cast(bytes, row[0])
        if _sha(raw) != row[1]:
            raise JournalConflict("prepared solve bytes do not match their stored digest")
        return _canonical_blob(raw, name="prepared solve")

    def call_state(self, attempt_id: str, call_id: str) -> str | None:
        _hex64(attempt_id, name="attempt_id")
        _identifier(call_id, name="call_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT state FROM rrcv2_calls WHERE attempt_id=? AND call_id=?",
                (attempt_id, call_id),
            ).fetchone()
        return None if row is None else cast(str, row[0])

    def load_committed_call(self, attempt_id: str, call_id: str) -> tuple[bytes, bytes] | None:
        """Reopen one committed provider outcome/cost pair or fail on ambiguity."""

        _hex64(attempt_id, name="attempt_id")
        _identifier(call_id, name="call_id")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT state,outcome,cost_event FROM rrcv2_calls
                WHERE attempt_id=? AND call_id=?
                """,
                (attempt_id, call_id),
            ).fetchone()
        if row is None:
            return None
        if row[0] != "call_committed" or row[1] is None or row[2] is None:
            raise JournalStateError("provider call is observed or ambiguous and cannot be replayed")
        return (
            _canonical_blob(cast(bytes, row[1]), name="committed call outcome"),
            _canonical_blob(cast(bytes, row[2]), name="committed cost event"),
        )

    def load_call_inventory(self, attempt_id: str) -> tuple[tuple[str, str, bytes | None], ...]:
        """Return every paid-call identity in durable insertion order."""

        _hex64(attempt_id, name="attempt_id")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT call_id,state,cost_event FROM rrcv2_calls
                WHERE attempt_id=? ORDER BY rowid
                """,
                (attempt_id,),
            ).fetchall()
        result: list[tuple[str, str, bytes | None]] = []
        for call_id, state, cost_event in rows:
            _identifier(call_id, name="call_id")
            if state not in {
                "call_prepared",
                "call_started",
                "call_observed",
                "call_committed",
            }:
                raise JournalConflict("stored provider call has an invalid state")
            if state == "call_committed":
                if cost_event is None:
                    raise JournalConflict("committed provider call is missing its cost event")
                validated = _canonical_blob(cast(bytes, cost_event), name="committed cost event")
            else:
                if cost_event is not None:
                    raise JournalConflict("noncommitted provider call has a cost event")
                validated = None
            result.append((cast(str, call_id), cast(str, state), validated))
        return tuple(result)

    def get_bundle(self, external_ref: str) -> bytes | None:
        """Read and revalidate one committed content-addressed bundle."""

        with self._lock:
            if self._in_transaction:
                raise JournalStateError("use the transaction-scoped unit of work inside callbacks")
            external_ref = _hex64(external_ref, name="external_ref")
            row = self._connection.execute(
                "SELECT bundle FROM rrcv2_bundles WHERE external_ref=?", (external_ref,)
            ).fetchone()
        if row is None:
            return None
        raw = cast(bytes, row[0])
        if _sha(raw) != external_ref:
            raise JournalConflict("stored template bundle does not match its external_ref")
        return _canonical_blob(raw, name="template bundle")

    def persist_projection_unavailable(
        self,
        record_raw: bytes,
        *,
        input_bytes: bytes,
        evidence: bytes,
    ) -> str:
        """Persist one exact query/store projection-failure authority."""

        from rrc.retrieval import parse_projection_unavailable

        record_raw = _canonical_blob(record_raw, name="projection-unavailable record")
        record = parse_projection_unavailable(record_raw)
        if (
            not isinstance(input_bytes, bytes)
            or len(input_bytes) > 256 * 1024
            or not isinstance(evidence, bytes)
            or len(evidence) > 4096
            or record.input_sha256 != _sha(input_bytes)
            or record.evidence_sha256 != _sha(evidence)
        ):
            raise ValueError("projection-unavailable evidence differs from its record")
        digest = _sha(record_raw)

        def persist(_uow: SQLiteUnitOfWork) -> None:
            handle = self._load_handle_row(record.attempt_id)
            if handle.state not in _RECOVERABLE_STATES:
                raise JournalStateError("terminal attempts cannot add projection evidence")
            existing = self._connection.execute(
                """
                SELECT record_sha256,record,input,evidence
                FROM rrcv2_projection_unavailable WHERE attempt_id=? AND phase=?
                """,
                (record.attempt_id, record.phase),
            ).fetchone()
            expected = (digest, record_raw, input_bytes, evidence)
            if existing is not None and existing != expected:
                raise JournalConflict("projection-unavailable authority differs")
            self._connection.execute(
                """
                INSERT OR IGNORE INTO rrcv2_projection_unavailable(
                    attempt_id,phase,record_sha256,record,input,evidence
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    record.attempt_id,
                    record.phase,
                    digest,
                    record_raw,
                    input_bytes,
                    evidence,
                ),
            )

        self.run_immediate(persist)
        return digest

    def load_projection_unavailable(self, attempt_id: str, phase: str) -> bytes | None:
        attempt_id = _hex64(attempt_id, name="attempt_id")
        if phase not in {"query", "store"}:
            raise ValueError("projection phase is invalid")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT record_sha256,record,input,evidence
                FROM rrcv2_projection_unavailable WHERE attempt_id=? AND phase=?
                """,
                (attempt_id, phase),
            ).fetchone()
        if row is None:
            return None
        from rrc.retrieval import parse_projection_unavailable

        record = parse_projection_unavailable(cast(bytes, row[1]))
        if (
            row[0] != _sha(cast(bytes, row[1]))
            or record.attempt_id != attempt_id
            or record.phase != phase
            or record.input_sha256 != _sha(cast(bytes, row[2]))
            or record.evidence_sha256 != _sha(cast(bytes, row[3]))
        ):
            raise JournalConflict("projection-unavailable authority is inconsistent")
        return cast(bytes, row[1])

    def retrieval_observations(
        self,
        owner_scope: str,
    ) -> tuple[RetrievalObservationV1, ...]:
        """Reopen owner-scoped case rows for deterministic hash ranking."""

        from rrc.contract import StructuralShapeV1
        from rrc.retrieval import RetrievalObservationV1, parse_case_document

        owner_scope = _identifier(owner_scope, name="owner_scope")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT i.external_ref,i.document_sha256,i.family,i.shape,i.slot_schema,
                       i.searchable_text,d.document
                FROM rrcv2_case_index AS i
                LEFT JOIN rrcv2_case_documents AS d
                  ON d.document_sha256=i.document_sha256
                WHERE i.case_v=1 AND i.owner_scope=?
                ORDER BY i.external_ref,i.document_sha256
                """,
                (owner_scope,),
            ).fetchall()
        result: list[RetrievalObservationV1] = []
        for external_ref, document_sha, family, shape_raw, schema_raw, searchable_text, raw in rows:
            try:
                _hex64(external_ref, name="case external_ref")
                _hex64(document_sha, name="case document_sha256")
                if raw is None:
                    continue
                document = parse_case_document(cast(bytes, raw))
                if document.document_sha256 != document_sha or document.owner_scope != owner_scope:
                    continue
                shape = None
                if shape_raw is not None:
                    shape_value = json.loads(
                        _canonical_blob(cast(bytes, shape_raw), name="case shape")
                    )
                    shape = StructuralShapeV1(
                        tuple(cast(list[str], shape_value["arg_types"])),
                        cast(int, shape_value["arity"]),
                        tuple(cast(list[str], shape_value["fields"])),
                    )
                schema_value = json.loads(
                    _canonical_blob(cast(bytes, schema_raw), name="case slot schema")
                )
                if not isinstance(schema_value, dict):
                    continue
                schema = tuple(
                    (name, tuple(cast(list[str], contexts)))
                    for name, contexts in sorted(schema_value.items())
                )
                if (
                    document.external_ref != external_ref
                    or document.family != family
                    or document.shape != shape
                    or document.slot_schema != schema
                    or document.searchable_text != searchable_text
                ):
                    continue
                result.append(
                    RetrievalObservationV1(
                        cast(str, external_ref),
                        cast(str, document_sha),
                        cast(str | None, family),
                        shape,
                        schema,
                        cast(str, searchable_text),
                    )
                )
            except (KeyError, TypeError, ValueError, JournalConflict):
                continue
        return tuple(result)

    def lexical_ranks(self, owner_scope: str, query: str) -> dict[tuple[str, str], int]:
        """Return bounded one-based FTS5 BM25 ranks for an owner-scoped query."""

        owner_scope = _identifier(owner_scope, name="owner_scope")
        if not isinstance(query, str):
            raise TypeError("retrieval query must be text")
        tokens = re.findall(r"[a-z0-9_]+", query)
        if not tokens:
            return {}
        expression = " OR ".join(f'"{token}"' for token in sorted(set(tokens)))
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT external_ref,document_sha256,bm25(rrcv2_case_fts) AS score
                FROM rrcv2_case_fts
                WHERE owner_scope=? AND rrcv2_case_fts MATCH ?
                ORDER BY score ASC,external_ref ASC,document_sha256 ASC LIMIT 22000
                """,
                (owner_scope, expression),
            ).fetchall()
        return {
            (cast(str, external_ref), cast(str, document_sha)): rank
            for rank, (external_ref, document_sha, _score) in enumerate(rows, 1)
        }

    def claim_finishing(
        self,
        attempt: AttemptHandle,
        *,
        owner_id: str,
        expected_state: str,
        expected_generation: int,
        expected_cursor: int,
    ) -> AttemptHandle:
        """Bind the synchronous terminal owner and enter ``finishing`` exactly once."""

        owner_id = _identifier(owner_id, name="terminal owner_id")
        if expected_state not in {"preparing", "prepared"}:
            raise ValueError("synchronous finishing may be claimed only from preparing or prepared")
        payload = canonical_json_bytes(
            {
                "generation": expected_generation,
                "owner_id": owner_id,
                "source_state": expected_state,
                "v": 1,
            }
        )

        def apply(current: AttemptHandle) -> None:
            changed = self._connection.execute(
                """
                UPDATE rrcv2_attempts SET state='finishing',terminal_owner=?
                WHERE attempt_id=? AND state=? AND generation=?
                """,
                (owner_id, current.attempt_id, expected_state, expected_generation),
            )
            if changed.rowcount != 1:
                raise JournalStateError("attempt could not enter finishing")

        return self._transition(
            attempt,
            event_key=f"claim_finishing:{owner_id}:{expected_generation}",
            payload=payload,
            expected_state=expected_state,
            expected_generation=expected_generation,
            expected_cursor=expected_cursor,
            apply=apply,
        )

    def persist_verification(
        self,
        artifact: CodeArtifactV1,
        result: VerificationResultV1,
    ) -> tuple[str, ArtifactRecordV1]:
        """Persist and reopen the artifact/result authority before terminal intent."""

        artifact_raw = code_artifact_bytes(artifact)
        result_raw = verification_result_bytes(result)
        artifact_sha = _sha(artifact_raw)
        result_sha = _sha(result_raw)
        reopened_artifact = parse_code_artifact(artifact_raw)
        reopened_result = parse_verification_result(result_raw)
        if (
            reopened_result.attempt_id != reopened_artifact.attempt_id
            or reopened_result.code_artifact_sha256 != artifact_sha
            or not reopened_result.public_accepted
        ):
            raise ValueError(
                "only a bound public-accepted verification may be persisted for commit"
            )
        source_raw = artifact.source.encode("utf-8", errors="strict")
        source_sha = _sha(source_raw)
        record = ArtifactRecordV1(
            attempt_id=artifact.attempt_id,
            artifact_path=artifact.artifact_path,
            source_sha256=source_sha,
            source_bytes=len(source_raw),
            blob_path=".rrcv2/artifacts/accepted-code.v1.utf8",
            blob_sha256=source_sha,
            blob_bytes=len(source_raw),
        )
        record_raw = artifact_record_bytes(record)
        parse_artifact_record(record_raw)
        record_sha = _sha(record_raw)

        def persist(_uow: SQLiteUnitOfWork) -> None:
            handle = self._load_handle_row(artifact.attempt_id)
            if handle.state not in _RECOVERABLE_STATES:
                raise JournalStateError("terminal attempts cannot add verification authority")
            rows = (
                (
                    "rrcv2_code_artifacts",
                    "artifact_sha256",
                    artifact_sha,
                    "artifact",
                    artifact_raw,
                ),
                (
                    "rrcv2_verification_results",
                    "result_sha256",
                    result_sha,
                    "result",
                    result_raw,
                ),
                (
                    "rrcv2_artifact_records",
                    "artifact_record_sha256",
                    record_sha,
                    "record",
                    record_raw,
                ),
            )
            for table, key_name, key, blob_name, blob in rows:
                existing = self._connection.execute(
                    f"SELECT {blob_name} FROM {table} WHERE {key_name}=?", (key,)
                ).fetchone()
                if existing is not None and existing[0] != blob:
                    raise JournalConflict("durable verification digest collision")
            self._connection.execute(
                """
                INSERT OR IGNORE INTO rrcv2_code_artifacts(
                    artifact_sha256,attempt_id,artifact
                ) VALUES(?,?,?)
                """,
                (artifact_sha, artifact.attempt_id, artifact_raw),
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO rrcv2_verification_results(
                    result_sha256,attempt_id,code_artifact_sha256,result
                ) VALUES(?,?,?,?)
                """,
                (result_sha, artifact.attempt_id, artifact_sha, result_raw),
            )
            existing_attempt = self._connection.execute(
                "SELECT artifact_record_sha256,record,blob FROM rrcv2_artifact_records WHERE attempt_id=?",
                (artifact.attempt_id,),
            ).fetchone()
            if existing_attempt is not None and (
                existing_attempt[0] != record_sha
                or existing_attempt[1] != record_raw
                or existing_attempt[2] != source_raw
            ):
                raise JournalConflict("attempt is already bound to a different accepted artifact")
            self._connection.execute(
                """
                INSERT OR IGNORE INTO rrcv2_artifact_records(
                    artifact_record_sha256,attempt_id,code_artifact_sha256,record,blob
                ) VALUES(?,?,?,?,?)
                """,
                (record_sha, artifact.attempt_id, artifact_sha, record_raw, source_raw),
            )

        self.run_immediate(persist)
        return result_sha, record

    def persist_rejected_verification(
        self,
        artifact: CodeArtifactV1,
        result: VerificationResultV1,
    ) -> tuple[str, str]:
        """Persist a rejected candidate/result pair without an accepted artifact record."""

        artifact_raw = code_artifact_bytes(artifact)
        result_raw = verification_result_bytes(result)
        artifact_sha = _sha(artifact_raw)
        result_sha = _sha(result_raw)
        reopened_artifact = parse_code_artifact(artifact_raw)
        reopened_result = parse_verification_result(result_raw)
        if (
            reopened_result.attempt_id != reopened_artifact.attempt_id
            or reopened_result.code_artifact_sha256 != artifact_sha
            or reopened_result.public_accepted
        ):
            raise ValueError("rejected verification must be bound and public-rejected")

        def persist(_uow: SQLiteUnitOfWork) -> None:
            handle = self._load_handle_row(artifact.attempt_id)
            if handle.state not in _RECOVERABLE_STATES:
                raise JournalStateError("terminal attempts cannot add verification authority")
            artifact_row = self._connection.execute(
                "SELECT artifact,attempt_id FROM rrcv2_code_artifacts WHERE artifact_sha256=?",
                (artifact_sha,),
            ).fetchone()
            if artifact_row is not None and (
                artifact_row[0] != artifact_raw or artifact_row[1] != artifact.attempt_id
            ):
                raise JournalConflict("rejected candidate authority differs")
            result_row = self._connection.execute(
                "SELECT result,attempt_id,code_artifact_sha256 FROM rrcv2_verification_results WHERE result_sha256=?",
                (result_sha,),
            ).fetchone()
            if result_row is not None and (
                result_row[0] != result_raw
                or result_row[1] != artifact.attempt_id
                or result_row[2] != artifact_sha
            ):
                raise JournalConflict("rejected verification authority differs")
            self._connection.execute(
                "INSERT OR IGNORE INTO rrcv2_code_artifacts(artifact_sha256,attempt_id,artifact) VALUES(?,?,?)",
                (artifact_sha, artifact.attempt_id, artifact_raw),
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO rrcv2_verification_results(
                    result_sha256,attempt_id,code_artifact_sha256,result
                ) VALUES(?,?,?,?)
                """,
                (result_sha, artifact.attempt_id, artifact_sha, result_raw),
            )

        self.run_immediate(persist)
        return artifact_sha, result_sha

    def persist_rejection_evidence(self, attempt_id: str, evidence: bytes) -> str:
        """Persist one bounded canonical rejection reason before its terminal CAS."""

        attempt_id = _hex64(attempt_id, name="attempt_id")
        evidence = _canonical_blob(evidence, name="rejection evidence")
        digest = _sha(evidence)

        def persist(_uow: SQLiteUnitOfWork) -> None:
            handle = self._load_handle_row(attempt_id)
            if handle.state not in _RECOVERABLE_STATES:
                raise JournalStateError("terminal attempts cannot add rejection evidence")
            existing = self._connection.execute(
                "SELECT attempt_id,evidence FROM rrcv2_rejection_evidence WHERE evidence_sha256=?",
                (digest,),
            ).fetchone()
            if existing is not None and (existing[0] != attempt_id or existing[1] != evidence):
                raise JournalConflict("rejection evidence authority differs")
            self._connection.execute(
                "INSERT OR IGNORE INTO rrcv2_rejection_evidence(evidence_sha256,attempt_id,evidence) VALUES(?,?,?)",
                (digest, attempt_id, evidence),
            )

        self.run_immediate(persist)
        return digest

    def _validate_rejected_authorities(self, rejected: RejectedCommitV1) -> None:
        outcome = rejected.rejected_outcome
        evidence = self._connection.execute(
            "SELECT attempt_id,evidence FROM rrcv2_rejection_evidence WHERE evidence_sha256=?",
            (outcome.evidence_sha256,),
        ).fetchone()
        if (
            evidence is None
            or evidence[0] != outcome.attempt_id
            or _sha(cast(bytes, evidence[1])) != outcome.evidence_sha256
        ):
            raise JournalConflict("rejection evidence authority is missing or inconsistent")
        _canonical_blob(cast(bytes, evidence[1]), name="rejection evidence")
        if outcome.valid_candidate_sha256 is None:
            return
        artifact_row = self._connection.execute(
            "SELECT attempt_id,artifact FROM rrcv2_code_artifacts WHERE artifact_sha256=?",
            (outcome.valid_candidate_sha256,),
        ).fetchone()
        if artifact_row is None or artifact_row[0] != outcome.attempt_id:
            raise JournalConflict("rejected candidate authority is missing")
        artifact = parse_code_artifact(cast(bytes, artifact_row[1]))
        if (
            artifact.attempt_id != outcome.attempt_id
            or _sha(cast(bytes, artifact_row[1])) != outcome.valid_candidate_sha256
        ):
            raise JournalConflict("rejected candidate authority is inconsistent")
        if outcome.verification_result_sha256 is None:
            return
        result_row = self._connection.execute(
            """
            SELECT attempt_id,code_artifact_sha256,result FROM rrcv2_verification_results
            WHERE result_sha256=?
            """,
            (outcome.verification_result_sha256,),
        ).fetchone()
        if result_row is None:
            raise JournalConflict("rejected verification authority is missing")
        result = parse_verification_result(cast(bytes, result_row[2]))
        if (
            result_row[0] != outcome.attempt_id
            or result_row[1] != outcome.valid_candidate_sha256
            or result.attempt_id != outcome.attempt_id
            or result.code_artifact_sha256 != outcome.valid_candidate_sha256
            or result.public_accepted != (outcome.reason == "store_failure")
            or _sha(cast(bytes, result_row[2])) != outcome.verification_result_sha256
        ):
            raise JournalConflict("rejected verification authority is inconsistent")

    def _validate_accepted_authorities(self, accepted: AcceptedCommitV1) -> None:
        outcome = accepted.outcome
        record_raw = artifact_record_bytes(accepted.artifact_record)
        if _sha(record_raw) != outcome.artifact_record_sha256:
            raise JournalConflict("accepted artifact record hash differs from outcome")
        artifact_row = self._connection.execute(
            """
            SELECT code_artifact_sha256,record,blob FROM rrcv2_artifact_records
            WHERE artifact_record_sha256=? AND attempt_id=?
            """,
            (outcome.artifact_record_sha256, outcome.attempt_id),
        ).fetchone()
        result_row = self._connection.execute(
            """
            SELECT code_artifact_sha256,result FROM rrcv2_verification_results
            WHERE result_sha256=? AND attempt_id=?
            """,
            (outcome.verification_result_sha256, outcome.attempt_id),
        ).fetchone()
        if artifact_row is None or result_row is None:
            raise JournalConflict("accepted verification authority is missing")
        if artifact_row[1] != record_raw:
            raise JournalConflict("accepted artifact record differs from durable authority")
        parsed_record = parse_artifact_record(cast(bytes, artifact_row[1]))
        parsed_result = parse_verification_result(cast(bytes, result_row[1]))
        source = cast(bytes, artifact_row[2])
        if (
            not parsed_result.public_accepted
            or parsed_result.attempt_id != outcome.attempt_id
            or parsed_result.code_artifact_sha256 != result_row[0]
            or result_row[0] != artifact_row[0]
            or parsed_record.source_sha256 != _sha(source)
            or parsed_record.blob_sha256 != _sha(source)
            or parsed_record.source_bytes != len(source)
            or parsed_record.blob_bytes != len(source)
        ):
            raise JournalConflict("accepted verification authority is inconsistent")
        self._receipt_authority(accepted)

    @staticmethod
    def _receipt_authority(
        accepted: AcceptedCommitV1,
    ) -> tuple[ReceiptRecordV1, bytes] | None:
        if accepted.receipt_record is None:
            return None
        try:
            record = parse_receipt_record(accepted.receipt_record)
            payload = AcceptedReceiptPayloadV1(
                attempt_id=accepted.outcome.attempt_id,
                accept_commit_id=accepted.accept_commit_id,
                artifact_path=accepted.artifact_record.artifact_path,
                source_sha256=accepted.artifact_record.source_sha256,
                source_bytes=accepted.artifact_record.source_bytes,
                verification_result_sha256=accepted.outcome.verification_result_sha256,
            ).canonical_bytes()
        except (TypeError, ValueError) as exc:
            raise JournalConflict("accepted receipt authority is malformed") from exc
        if (
            record.attempt_id != accepted.outcome.attempt_id
            or record.receipt != _sha(payload)
            or record.blob_sha256 != _sha(payload)
            or record.blob_bytes != len(payload)
            or record.artifact_record_sha256 != accepted.outcome.artifact_record_sha256
        ):
            raise JournalConflict("accepted receipt differs from its acceptance authority")
        return record, payload

    def _validate_accepted_route_rows(self, accepted: AcceptedCommitV1) -> None:
        """Reopen every route-selected row named by an accepted intent."""

        if accepted.bundle is not None:
            stored_bundle = self._connection.execute(
                "SELECT bundle FROM rrcv2_bundles WHERE external_ref=?",
                (_sha(accepted.bundle),),
            ).fetchone()
            if stored_bundle is None or stored_bundle[0] != accepted.bundle:
                raise JournalConflict("accepted bundle is missing or differs")
        receipt_authority = self._receipt_authority(accepted)
        if receipt_authority is not None:
            record, payload = receipt_authority
            stored_receipt = self._connection.execute(
                """
                SELECT record,payload,artifact_record_sha256 FROM rrcv2_receipts
                WHERE attempt_id=? AND receipt=?
                """,
                (record.attempt_id, record.receipt),
            ).fetchone()
            if stored_receipt != (
                accepted.receipt_record,
                payload,
                record.artifact_record_sha256,
            ):
                raise JournalConflict("accepted receipt row is missing or differs")
        if accepted.case_document is not None:
            from rrc.pipeline.template import parse_template_bundle
            from rrc.retrieval import parse_case_document

            if accepted.bundle is None:
                raise JournalConflict("accepted case document is missing its bundle")
            document = parse_case_document(accepted.case_document)
            bundle = parse_template_bundle(accepted.bundle)
            if (
                document.external_ref != bundle.external_ref
                or document.slot_schema != bundle.slot_contexts
            ):
                raise JournalConflict("accepted case document differs from its bundle")
            stored_document = self._connection.execute(
                "SELECT document FROM rrcv2_case_documents WHERE document_sha256=?",
                (document.document_sha256,),
            ).fetchone()
            shape_raw = (
                None if document.shape is None else canonical_json_bytes(document.shape.as_json())
            )
            schema_raw = canonical_json_bytes(
                {name: list(contexts) for name, contexts in document.slot_schema}
            )
            stored_index = self._connection.execute(
                """
                SELECT row_id,family,shape,slot_schema,searchable_text
                FROM rrcv2_case_index
                WHERE case_v=1 AND owner_scope=? AND external_ref=? AND document_sha256=?
                """,
                (
                    document.owner_scope,
                    document.external_ref,
                    document.document_sha256,
                ),
            ).fetchone()
            if (
                stored_document is None
                or stored_document[0] != accepted.case_document
                or stored_index is None
                or stored_index[1:]
                != (
                    document.family,
                    shape_raw,
                    schema_raw,
                    document.searchable_text,
                )
            ):
                raise JournalConflict("accepted case document or index row is missing or differs")
            fts_rows = self._connection.execute(
                """
                SELECT owner_scope,external_ref,document_sha256,searchable_text
                FROM rrcv2_case_fts WHERE rowid=?
                """,
                (stored_index[0],),
            ).fetchall()
            if fts_rows != [
                (
                    document.owner_scope,
                    document.external_ref,
                    document.document_sha256,
                    document.searchable_text,
                )
            ]:
                raise JournalConflict("accepted FTS row is missing or differs")
        projection_rows = self._connection.execute(
            """
            SELECT record_sha256,record,input,evidence
            FROM rrcv2_projection_unavailable
            WHERE attempt_id=? AND phase='store'
            """,
            (accepted.outcome.attempt_id,),
        ).fetchall()
        if accepted.outcome.index_disposition == "unindexed_projection_unavailable":
            from rrc.retrieval import parse_projection_unavailable

            if len(projection_rows) != 1:
                raise JournalConflict("projection-unavailable acceptance is missing its reason")
            record_sha, record_raw, input_raw, evidence_raw = projection_rows[0]
            record = parse_projection_unavailable(cast(bytes, record_raw))
            if (
                record.attempt_id != accepted.outcome.attempt_id
                or record.phase != "store"
                or record_sha != _sha(cast(bytes, record_raw))
                or record.input_sha256 != _sha(cast(bytes, input_raw))
                or record.evidence_sha256 != _sha(cast(bytes, evidence_raw))
            ):
                raise JournalConflict("projection-unavailable reason is inconsistent")
        elif projection_rows:
            raise JournalConflict("accepted outcome has an unauthorized projection-failure row")
        if accepted.outbox_row is not None:
            from rrc.everos import (
                parse_dispatch,
                parse_outbox,
                parse_target,
                validate_route_tuple,
            )

            if accepted.everos_target is None or accepted.everos_dispatch is None:
                raise JournalConflict("accepted EverOS route is incomplete")
            target = parse_target(accepted.everos_target)
            dispatch = parse_dispatch(accepted.everos_dispatch)
            outbox = parse_outbox(accepted.outbox_row)
            try:
                validate_route_tuple(target, dispatch, outbox)
            except ValueError as exc:
                raise JournalConflict("accepted EverOS route tuple is inconsistent") from exc
            stored_target = self._connection.execute(
                "SELECT target FROM rrcv2_everos_targets WHERE target_sha256=?",
                (target.sha256,),
            ).fetchone()
            stored_dispatch = self._connection.execute(
                """
                SELECT target_sha256,dispatch FROM rrcv2_everos_dispatches
                WHERE dispatch_sha256=?
                """,
                (dispatch.sha256,),
            ).fetchone()
            stored_compat = self._connection.execute(
                "SELECT row FROM rrcv2_outbox WHERE row_sha256=?",
                (_sha(accepted.outbox_row),),
            ).fetchone()
            stored_outbox = self._connection.execute(
                """
                SELECT external_ref,document_sha256,dispatch_sha256,row_sha256,row
                FROM rrcv2_everos_outbox_v1
                WHERE case_v=? AND backend=? AND operation=? AND owner_scope=?
                  AND observation_id=? AND target_sha256=?
                """,
                (
                    outbox.case_v,
                    outbox.backend,
                    outbox.operation,
                    outbox.owner_scope,
                    outbox.observation_id,
                    outbox.target_sha256,
                ),
            ).fetchone()
            if (
                stored_target != (accepted.everos_target,)
                or stored_dispatch != (target.sha256, accepted.everos_dispatch)
                or stored_compat != (accepted.outbox_row,)
                or stored_outbox
                != (
                    outbox.external_ref,
                    outbox.document_sha256,
                    outbox.dispatch_sha256,
                    _sha(accepted.outbox_row),
                    accepted.outbox_row,
                )
            ):
                raise JournalConflict("accepted EverOS route rows are missing or differ")

    def commit_accepted(
        self,
        attempt: AttemptHandle,
        claim: TerminalClaimV1,
        accepted: AcceptedCommitV1,
    ) -> AcceptedCommitReceiptV1:
        """Atomically commit terminal acceptance and all route-selected rows."""

        if not isinstance(claim, TerminalClaimV1) or not isinstance(accepted, AcceptedCommitV1):
            raise TypeError("commit_accepted requires typed claim and intent")
        if claim.expected_state != "finishing":
            raise ValueError("accepted terminal claims require expected_state=finishing")
        if accepted.outcome.attempt_id != attempt.attempt_id:
            raise JournalConflict("accepted outcome names a different attempt")
        intent = accepted.canonical_bytes()
        intent_sha = _sha(intent)
        outcome_sha = _sha(accepted.outcome.canonical_bytes())

        def commit(uow: SQLiteUnitOfWork) -> None:
            current = self._load_handle_row(attempt.attempt_id)
            self._require_expected(
                current,
                attempt,
                expected_state="finishing",
                expected_generation=claim.generation,
                expected_cursor=current.cursor,
            )
            owner = self._connection.execute(
                "SELECT terminal_owner FROM rrcv2_attempts WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone()
            if owner is None or owner[0] != claim.owner_id:
                raise JournalConflict("terminal owner differs from the claim")
            self._validate_accepted_authorities(accepted)
            if accepted.bundle is not None:
                uow.put_bundle(_sha(accepted.bundle), accepted.bundle)
            if accepted.case_document is not None:
                from rrc.pipeline.template import parse_template_bundle
                from rrc.retrieval import parse_case_document

                document = parse_case_document(accepted.case_document)
                if accepted.bundle is None:
                    raise JournalConflict("case document requires its template bundle")
                bundle = parse_template_bundle(accepted.bundle)
                if (
                    document.external_ref != bundle.external_ref
                    or document.slot_schema != bundle.slot_contexts
                ):
                    raise JournalConflict("case document differs from its template authority")
                existing_document = self._connection.execute(
                    "SELECT document FROM rrcv2_case_documents WHERE document_sha256=?",
                    (document.document_sha256,),
                ).fetchone()
                if existing_document is not None and existing_document[0] != accepted.case_document:
                    raise JournalConflict("case-document digest collision")
                self._connection.execute(
                    "INSERT OR IGNORE INTO rrcv2_case_documents(document_sha256,document) VALUES(?,?)",
                    (document.document_sha256, accepted.case_document),
                )
                shape_raw = (
                    None
                    if document.shape is None
                    else canonical_json_bytes(document.shape.as_json())
                )
                schema_raw = canonical_json_bytes(
                    {name: list(contexts) for name, contexts in document.slot_schema}
                )
                inserted = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO rrcv2_case_index(
                        case_v,owner_scope,external_ref,document_sha256,family,shape,
                        slot_schema,searchable_text
                    ) VALUES(1,?,?,?,?,?,?,?)
                    """,
                    (
                        document.owner_scope,
                        document.external_ref,
                        document.document_sha256,
                        document.family,
                        shape_raw,
                        schema_raw,
                        document.searchable_text,
                    ),
                )
                row = self._connection.execute(
                    """
                    SELECT row_id,family,shape,slot_schema,searchable_text FROM rrcv2_case_index
                    WHERE case_v=1 AND owner_scope=? AND external_ref=? AND document_sha256=?
                    """,
                    (
                        document.owner_scope,
                        document.external_ref,
                        document.document_sha256,
                    ),
                ).fetchone()
                if row is None or (
                    row[1] != document.family
                    or row[2] != shape_raw
                    or row[3] != schema_raw
                    or row[4] != document.searchable_text
                ):
                    raise JournalConflict("case-index identity differs")
                if inserted.rowcount == 1:
                    self._connection.execute(
                        """
                        INSERT INTO rrcv2_case_fts(
                            rowid,owner_scope,external_ref,document_sha256,searchable_text
                        ) VALUES(?,?,?,?,?)
                        """,
                        (
                            row[0],
                            document.owner_scope,
                            document.external_ref,
                            document.document_sha256,
                            document.searchable_text,
                        ),
                    )
            if accepted.outbox_row is not None:
                from rrc.everos import (
                    parse_dispatch,
                    parse_outbox,
                    parse_target,
                    validate_route_tuple,
                )
                from rrc.retrieval import parse_case_document

                if (
                    accepted.everos_target is None
                    or accepted.everos_dispatch is None
                    or accepted.case_document is None
                ):
                    raise JournalConflict("EverOS outbox is missing its bound authorities")
                target = parse_target(accepted.everos_target)
                dispatch = parse_dispatch(accepted.everos_dispatch)
                outbox = parse_outbox(accepted.outbox_row)
                document = parse_case_document(accepted.case_document)
                try:
                    validate_route_tuple(target, dispatch, outbox)
                except ValueError as exc:
                    raise JournalConflict("EverOS route tuple is inconsistent") from exc
                if (
                    dispatch.document_sha256 != document.document_sha256
                    or dispatch.external_ref != document.external_ref
                    or outbox.document_sha256 != document.document_sha256
                    or outbox.external_ref != document.external_ref
                ):
                    raise JournalConflict("EverOS route differs from its case document")
                self._connection.execute(
                    "INSERT OR IGNORE INTO rrcv2_outbox(row_sha256,row) VALUES(?,?)",
                    (_sha(accepted.outbox_row), accepted.outbox_row),
                )
                for table, digest, payload, hash_column, blob_column in (
                    (
                        "rrcv2_everos_targets",
                        target.sha256,
                        accepted.everos_target,
                        "target_sha256",
                        "target",
                    ),
                    (
                        "rrcv2_everos_dispatches",
                        dispatch.sha256,
                        accepted.everos_dispatch,
                        "dispatch_sha256",
                        "dispatch",
                    ),
                ):
                    existing = self._connection.execute(
                        f"SELECT {blob_column} FROM {table} WHERE {hash_column}=?", (digest,)
                    ).fetchone()
                    if existing is not None and existing[0] != payload:
                        raise JournalConflict("EverOS content digest collision")
                self._connection.execute(
                    "INSERT OR IGNORE INTO rrcv2_everos_targets(target_sha256,target) VALUES(?,?)",
                    (target.sha256, accepted.everos_target),
                )
                self._connection.execute(
                    "INSERT OR IGNORE INTO rrcv2_everos_dispatches("
                    "dispatch_sha256,target_sha256,dispatch) VALUES(?,?,?)",
                    (dispatch.sha256, target.sha256, accepted.everos_dispatch),
                )
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO rrcv2_everos_outbox_v1(
                        case_v,backend,operation,owner_scope,observation_id,external_ref,
                        document_sha256,target_sha256,dispatch_sha256,row_sha256,row
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        outbox.case_v,
                        outbox.backend,
                        outbox.operation,
                        outbox.owner_scope,
                        outbox.observation_id,
                        outbox.external_ref,
                        outbox.document_sha256,
                        outbox.target_sha256,
                        outbox.dispatch_sha256,
                        _sha(accepted.outbox_row),
                        accepted.outbox_row,
                    ),
                )
                stored_outbox = self._connection.execute(
                    """
                    SELECT external_ref,document_sha256,dispatch_sha256,row_sha256,row
                    FROM rrcv2_everos_outbox_v1
                    WHERE case_v=? AND backend=? AND operation=? AND owner_scope=?
                      AND observation_id=? AND target_sha256=?
                    """,
                    (
                        outbox.case_v,
                        outbox.backend,
                        outbox.operation,
                        outbox.owner_scope,
                        outbox.observation_id,
                        outbox.target_sha256,
                    ),
                ).fetchone()
                if stored_outbox != (
                    outbox.external_ref,
                    outbox.document_sha256,
                    outbox.dispatch_sha256,
                    _sha(accepted.outbox_row),
                    accepted.outbox_row,
                ):
                    raise JournalConflict("EverOS outbox logical identity differs")
            receipt_authority = self._receipt_authority(accepted)
            if receipt_authority is not None:
                record, payload = receipt_authority
                existing_receipt = self._connection.execute(
                    """
                    SELECT record,payload,artifact_record_sha256 FROM rrcv2_receipts
                    WHERE attempt_id=? AND receipt=?
                    """,
                    (record.attempt_id, record.receipt),
                ).fetchone()
                expected_receipt = (
                    accepted.receipt_record,
                    payload,
                    record.artifact_record_sha256,
                )
                if existing_receipt is not None and existing_receipt != expected_receipt:
                    raise JournalConflict("ContextMesh receipt logical identity differs")
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO rrcv2_receipts(
                        attempt_id,receipt,record,payload,artifact_record_sha256
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        record.attempt_id,
                        record.receipt,
                        accepted.receipt_record,
                        payload,
                        record.artifact_record_sha256,
                    ),
                )
            self._validate_accepted_route_rows(accepted)
            existing_id = self._connection.execute(
                "SELECT attempt_id,intent_sha256,intent FROM rrcv2_acceptance_markers WHERE accept_commit_id=?",
                (accepted.accept_commit_id,),
            ).fetchone()
            if existing_id is not None:
                raise JournalConflict("accept_commit_id is already bound")
            self._connection.execute(
                """
                INSERT INTO rrcv2_acceptance_markers(
                    accept_commit_id,attempt_id,intent_sha256,outcome_sha256,intent
                ) VALUES(?,?,?,?,?)
                """,
                (
                    accepted.accept_commit_id,
                    attempt.attempt_id,
                    intent_sha,
                    outcome_sha,
                    intent,
                ),
            )
            changed = self._connection.execute(
                """
                UPDATE rrcv2_attempts SET state='accepted',terminal_outcome=?
                WHERE attempt_id=? AND state='finishing' AND generation=? AND terminal_owner=?
                """,
                (intent, attempt.attempt_id, claim.generation, claim.owner_id),
            )
            if changed.rowcount != 1:
                raise JournalStateError("accepted terminal compare-and-swap failed")

        try:
            self.run_immediate(commit)
        except sqlite3.DatabaseError as exc:
            raise JournalError("acceptance transaction failed and requires reconciliation") from exc
        return AcceptedCommitReceiptV1(
            attempt.attempt_id,
            accepted.accept_commit_id,
            outcome_sha,
        )

    def reconcile_accepted(
        self,
        attempt: AttemptHandle,
        accepted: AcceptedCommitV1,
    ) -> AcceptedCommitReceiptV1:
        """Prove an exact accepted commit from a fresh authoritative read."""

        if accepted.outcome.attempt_id != attempt.attempt_id:
            raise JournalConflict("accepted outcome names a different attempt")
        intent = accepted.canonical_bytes()
        with self._lock:
            current = self._load_handle_row(attempt.attempt_id)
            marker = self._connection.execute(
                """
                SELECT intent_sha256,outcome_sha256,intent FROM rrcv2_acceptance_markers
                WHERE accept_commit_id=? AND attempt_id=?
                """,
                (accepted.accept_commit_id, attempt.attempt_id),
            ).fetchone()
            if marker is None:
                if current.state == "accepted":
                    raise JournalConflict("attempt is accepted under a different commit identity")
                raise JournalStateError("accepted commit is not durable")
            if (
                current.state != "accepted"
                or marker[0] != _sha(intent)
                or marker[1] != _sha(accepted.outcome.canonical_bytes())
                or marker[2] != intent
            ):
                raise JournalConflict("accepted commit marker or terminal state differs")
            self._validate_accepted_authorities(accepted)
            self._validate_accepted_route_rows(accepted)
        return AcceptedCommitReceiptV1(
            attempt.attempt_id,
            accepted.accept_commit_id,
            _sha(accepted.outcome.canonical_bytes()),
        )

    def commit_rejected(
        self,
        attempt: AttemptHandle,
        claim: TerminalClaimV1,
        rejected: RejectedCommitV1,
    ) -> RejectedCommitReceiptV1:
        """Atomically reject from the claim's exact legal nonterminal source state."""

        if not isinstance(claim, TerminalClaimV1) or not isinstance(rejected, RejectedCommitV1):
            raise TypeError("commit_rejected requires typed claim and intent")
        outcome = rejected.rejected_outcome
        if outcome.attempt_id != attempt.attempt_id:
            raise JournalConflict("rejected outcome names a different attempt")
        if outcome.source_state != claim.expected_state:
            raise JournalConflict("rejected outcome source state differs from claim")
        intent = rejected.canonical_bytes()
        intent_sha = _sha(intent)
        outcome_sha = _sha(outcome.canonical_bytes())

        def commit(_uow: SQLiteUnitOfWork) -> None:
            current = self._load_handle_row(attempt.attempt_id)
            self._require_expected(
                current,
                attempt,
                expected_state=claim.expected_state,
                expected_generation=claim.generation,
                expected_cursor=current.cursor,
            )
            if claim.expected_state == "finishing":
                owner = self._connection.execute(
                    "SELECT terminal_owner FROM rrcv2_attempts WHERE attempt_id=?",
                    (attempt.attempt_id,),
                ).fetchone()
                if owner is None or owner[0] != claim.owner_id:
                    raise JournalConflict("terminal owner differs from rejection claim")
            self._validate_rejected_authorities(rejected)
            existing = self._connection.execute(
                "SELECT attempt_id,intent_sha256,intent FROM rrcv2_rejection_markers WHERE reject_commit_id=?",
                (rejected.reject_commit_id,),
            ).fetchone()
            if existing is not None:
                raise JournalConflict("reject_commit_id is already bound")
            self._connection.execute(
                """
                INSERT INTO rrcv2_rejection_markers(
                    reject_commit_id,attempt_id,intent_sha256,outcome_sha256,intent
                ) VALUES(?,?,?,?,?)
                """,
                (
                    rejected.reject_commit_id,
                    attempt.attempt_id,
                    intent_sha,
                    outcome_sha,
                    intent,
                ),
            )
            changed = self._connection.execute(
                """
                UPDATE rrcv2_attempts SET state='rejected',terminal_outcome=?
                WHERE attempt_id=? AND state=? AND generation=?
                """,
                (intent, attempt.attempt_id, claim.expected_state, claim.generation),
            )
            if changed.rowcount != 1:
                raise JournalStateError("rejected terminal compare-and-swap failed")

        try:
            self.run_immediate(commit)
        except sqlite3.DatabaseError as exc:
            raise JournalError("rejection transaction failed and requires reconciliation") from exc
        return RejectedCommitReceiptV1(
            attempt.attempt_id,
            rejected.reject_commit_id,
            outcome_sha,
        )

    def reconcile_rejected(
        self,
        attempt: AttemptHandle,
        rejected: RejectedCommitV1,
    ) -> RejectedCommitReceiptV1:
        """Prove one exact rejected commit without mutating terminal state."""

        outcome = rejected.rejected_outcome
        if outcome.attempt_id != attempt.attempt_id:
            raise JournalConflict("rejected outcome names a different attempt")
        intent = rejected.canonical_bytes()
        with self._lock:
            current = self._load_handle_row(attempt.attempt_id)
            marker = self._connection.execute(
                """
                SELECT intent_sha256,outcome_sha256,intent FROM rrcv2_rejection_markers
                WHERE reject_commit_id=? AND attempt_id=?
                """,
                (rejected.reject_commit_id, attempt.attempt_id),
            ).fetchone()
            if marker is None:
                if current.state == "rejected":
                    raise JournalConflict("attempt is rejected under a different commit identity")
                raise JournalStateError("rejected commit is not durable")
            if (
                current.state != "rejected"
                or marker[0] != _sha(intent)
                or marker[1] != _sha(outcome.canonical_bytes())
                or marker[2] != intent
            ):
                raise JournalConflict("rejected commit marker or terminal state differs")
            self._validate_rejected_authorities(rejected)
        return RejectedCommitReceiptV1(
            attempt.attempt_id,
            rejected.reject_commit_id,
            _sha(outcome.canonical_bytes()),
        )

    def load_terminal_intent(self, attempt_id: str) -> tuple[str, bytes] | None:
        """Return exact terminal state/intent bytes for deterministic hydration."""

        attempt_id = _hex64(attempt_id, name="attempt_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT state,terminal_outcome FROM rrcv2_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise JournalStateError("attempt does not exist")
            if row[0] not in _TERMINAL_STATES:
                return None
            if row[1] is None:
                raise JournalConflict("terminal attempt is missing its intent")
            raw = _canonical_blob(cast(bytes, row[1]), name="terminal intent")
            marker_table = (
                "rrcv2_acceptance_markers" if row[0] == "accepted" else "rrcv2_rejection_markers"
            )
            marker = self._connection.execute(
                f"SELECT intent_sha256,outcome_sha256,intent FROM {marker_table} WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if marker is None or marker[0] != _sha(raw) or marker[2] != raw:
                raise JournalConflict("terminal intent marker is missing or inconsistent")
            try:
                value = json.loads(raw)
                outcome_value = (
                    value["outcome"] if row[0] == "accepted" else value["rejected_outcome"]
                )
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise JournalConflict("terminal intent outcome is malformed") from exc
            if marker[1] != _sha(canonical_json_bytes(outcome_value)):
                raise JournalConflict("terminal outcome hash differs from its marker")
            return cast(str, row[0]), raw

    def load_accepted_source(self, attempt_id: str, artifact_record_sha256: str) -> str:
        """Reopen the exact accepted artifact bytes named by a terminal outcome."""

        attempt_id = _hex64(attempt_id, name="attempt_id")
        artifact_record_sha256 = _hex64(
            artifact_record_sha256,
            name="artifact_record_sha256",
        )
        with self._lock:
            row = self._connection.execute(
                """
                SELECT record,blob FROM rrcv2_artifact_records
                WHERE attempt_id=? AND artifact_record_sha256=?
                """,
                (attempt_id, artifact_record_sha256),
            ).fetchone()
        if row is None:
            raise JournalConflict("accepted artifact record is missing")
        record_raw = cast(bytes, row[0])
        source = cast(bytes, row[1])
        if _sha(record_raw) != artifact_record_sha256:
            raise JournalConflict("accepted artifact record hash differs")
        record = parse_artifact_record(record_raw)
        if (
            record.attempt_id != attempt_id
            or record.source_sha256 != _sha(source)
            or record.blob_sha256 != _sha(source)
            or record.source_bytes != len(source)
            or record.blob_bytes != len(source)
        ):
            raise JournalConflict("accepted artifact bytes differ from their record")
        try:
            return source.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise JournalConflict("accepted artifact is not UTF-8") from exc

    def load_receipt(
        self, attempt_id: str, receipt: str
    ) -> tuple[ReceiptRecordV1, AcceptedReceiptPayloadV1]:
        """Reopen one exact terminal ContextMesh receipt from accepted authority."""

        attempt_id = _hex64(attempt_id, name="attempt_id")
        receipt = _hex64(receipt, name="receipt")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT r.record,r.payload,r.artifact_record_sha256,a.state,a.terminal_outcome
                FROM rrcv2_receipts AS r
                JOIN rrcv2_attempts AS a ON a.attempt_id=r.attempt_id
                WHERE r.attempt_id=? AND r.receipt=?
                """,
                (attempt_id, receipt),
            ).fetchone()
        if row is None or row[3] != "accepted" or not isinstance(row[4], bytes):
            raise JournalStateError("accepted receipt is unavailable")
        record_raw = cast(bytes, row[0])
        payload_raw = cast(bytes, row[1])
        try:
            record = parse_receipt_record(record_raw)
            payload = parse_receipt_payload(payload_raw)
            accepted = parse_accepted_commit(cast(bytes, row[4]))
        except ValueError as exc:
            raise JournalConflict("accepted receipt authority is corrupt") from exc
        if (
            record.attempt_id != attempt_id
            or record.receipt != receipt
            or record.blob_sha256 != _sha(payload_raw)
            or record.blob_bytes != len(payload_raw)
            or record.artifact_record_sha256 != row[2]
            or payload.attempt_id != attempt_id
            or payload.receipt != receipt
            or accepted.accept_commit_id != payload.accept_commit_id
            or accepted.outcome.artifact_record_sha256 != record.artifact_record_sha256
            or accepted.outcome.verification_result_sha256 != payload.verification_result_sha256
        ):
            raise JournalConflict("accepted receipt authority differs")
        self._validate_accepted_authorities(accepted)
        self._validate_accepted_route_rows(accepted)
        return record, payload

    def load_rejected_source(self, attempt_id: str, artifact_sha256: str) -> str:
        """Reopen a rejected canonical candidate by its exact digest."""

        attempt_id = _hex64(attempt_id, name="attempt_id")
        artifact_sha256 = _hex64(artifact_sha256, name="artifact_sha256")
        with self._lock:
            row = self._connection.execute(
                "SELECT attempt_id,artifact FROM rrcv2_code_artifacts WHERE artifact_sha256=?",
                (artifact_sha256,),
            ).fetchone()
        if row is None or row[0] != attempt_id:
            raise JournalConflict("rejected candidate is missing")
        raw = cast(bytes, row[1])
        if _sha(raw) != artifact_sha256:
            raise JournalConflict("rejected candidate hash differs")
        artifact = parse_code_artifact(raw)
        if artifact.attempt_id != attempt_id:
            raise JournalConflict("rejected candidate attempt differs")
        return artifact.source

    def record_oracle_score(
        self,
        attempt_id: str,
        *,
        status: Literal["not_present", "passed", "failed", "infrastructure_failure"],
        score: bool | None,
        evidence_sha256: str,
    ) -> None:
        """Persist the benchmark-only hidden-oracle disposition idempotently."""

        attempt_id = _hex64(attempt_id, name="attempt_id")
        evidence_sha256 = _hex64(evidence_sha256, name="oracle evidence_sha256")
        if status not in {"not_present", "passed", "failed", "infrastructure_failure"}:
            raise ValueError("oracle status is invalid")
        if (status == "passed" and score is not True) or (
            status == "failed" and score is not False
        ):
            raise ValueError("oracle score differs from its status")
        if status in {"not_present", "infrastructure_failure"} and score is not None:
            raise ValueError("unscored oracle status requires a null score")
        encoded_score = None if score is None else int(score)
        with self._lock, self._connection:
            state = self._connection.execute(
                "SELECT state FROM rrcv2_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if state is None or state[0] not in {"accepted", "rejected"}:
                raise JournalStateError("oracle score requires a terminal attempt")
            existing = self._connection.execute(
                "SELECT status,score,evidence_sha256 FROM rrcv2_oracle_scores WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            expected = (status, encoded_score, evidence_sha256)
            if existing is not None:
                if tuple(existing) != expected:
                    raise JournalConflict("oracle score is already bound to different evidence")
                return
            self._connection.execute(
                "INSERT INTO rrcv2_oracle_scores(attempt_id,status,score,evidence_sha256) "
                "VALUES(?,?,?,?)",
                (attempt_id, status, encoded_score, evidence_sha256),
            )

    def load_oracle_score(self, attempt_id: str) -> tuple[str, bool | None, str] | None:
        """Return one exact benchmark-only oracle disposition."""

        attempt_id = _hex64(attempt_id, name="attempt_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT status,score,evidence_sha256 FROM rrcv2_oracle_scores WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            return None
        status = cast(str, row[0])
        score_raw = row[1]
        evidence = _hex64(row[2], name="oracle evidence_sha256")
        if status not in {"not_present", "passed", "failed", "infrastructure_failure"}:
            raise JournalConflict("stored oracle status is invalid")
        if score_raw not in {None, 0, 1}:
            raise JournalConflict("stored oracle score is invalid")
        score = None if score_raw is None else bool(score_raw)
        if (
            (status == "passed" and score is not True)
            or (status == "failed" and score is not False)
            or (status in {"not_present", "infrastructure_failure"} and score is not None)
        ):
            raise JournalConflict("stored oracle score differs from its status")
        return status, score, evidence

    def pending_everos_outbox(
        self, owner_scope: str, *, limit: int = 100
    ) -> tuple[tuple[str, str], ...]:
        """List bounded pending observation/target identities for one exact owner."""

        owner_scope = _identifier(owner_scope, name="owner_scope")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("outbox list limit must be 1..100")
        with self._lock:
            now_ms = int(time.time() * 1000)
            rows = self._connection.execute(
                """
                SELECT observation_id,target_sha256 FROM rrcv2_everos_outbox_v1
                WHERE owner_scope=? AND (
                    state='pending' OR (
                        state IN ('leased','draining') AND lease_expires_ms<=?
                    )
                )
                ORDER BY observation_id,target_sha256 LIMIT ?
                """,
                (owner_scope, now_ms, limit),
            ).fetchall()
        return tuple((cast(str, row[0]), cast(str, row[1])) for row in rows)

    def claim_everos_outbox(
        self,
        owner_scope: str,
        observation_id: str,
        target_sha256: str,
    ) -> tuple[int, bytes, bytes]:
        """Claim one pending row and reopen its sealed target/dispatch payloads."""

        owner_scope = _identifier(owner_scope, name="owner_scope")
        observation_id = _hex64(observation_id, name="observation_id")
        target_sha256 = _hex64(target_sha256, name="target_sha256")

        def claim(_uow: SQLiteUnitOfWork) -> tuple[int, bytes, bytes]:
            now_ms = int(time.time() * 1000)
            row = self._connection.execute(
                """
                SELECT generation,dispatch_sha256 FROM rrcv2_everos_outbox_v1
                WHERE owner_scope=? AND observation_id=? AND target_sha256=? AND (
                    state='pending' OR (
                        state IN ('leased','draining') AND lease_expires_ms<=?
                    )
                )
                """,
                (owner_scope, observation_id, target_sha256, now_ms),
            ).fetchone()
            if row is None:
                raise JournalStateError("EverOS outbox row is not pending")
            generation = cast(int, row[0]) + 1
            changed = self._connection.execute(
                """
                UPDATE rrcv2_everos_outbox_v1
                SET state='leased',generation=?,attempts=attempts+1,
                    lease_expires_ms=?,last_error=NULL
                WHERE owner_scope=? AND observation_id=? AND target_sha256=? AND (
                    state='pending' OR (
                        state IN ('leased','draining') AND lease_expires_ms<=?
                    )
                )
                """,
                (
                    generation,
                    now_ms + 30_000,
                    owner_scope,
                    observation_id,
                    target_sha256,
                    now_ms,
                ),
            )
            if changed.rowcount != 1:
                raise JournalStateError("EverOS outbox claim raced")
            target = self._connection.execute(
                "SELECT target FROM rrcv2_everos_targets WHERE target_sha256=?",
                (target_sha256,),
            ).fetchone()
            dispatch = self._connection.execute(
                "SELECT dispatch FROM rrcv2_everos_dispatches WHERE dispatch_sha256=?",
                (row[1],),
            ).fetchone()
            if target is None or dispatch is None:
                raise JournalConflict("EverOS outbox payload authority is missing")
            return generation, cast(bytes, target[0]), cast(bytes, dispatch[0])

        return self.run_immediate(claim)

    def mark_everos_draining(
        self,
        owner_scope: str,
        observation_id: str,
        target_sha256: str,
        *,
        generation: int,
    ) -> None:
        """Advance a leased row after an unambiguous add acknowledgement."""

        owner_scope = _identifier(owner_scope, name="owner_scope")
        observation_id = _hex64(observation_id, name="observation_id")
        target_sha256 = _hex64(target_sha256, name="target_sha256")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError("outbox generation is invalid")
        with self._lock, self._connection:
            changed = self._connection.execute(
                """
                UPDATE rrcv2_everos_outbox_v1
                SET state='draining',lease_expires_ms=?
                WHERE owner_scope=? AND observation_id=? AND target_sha256=?
                  AND state='leased' AND generation=?
                """,
                (
                    int(time.time() * 1000) + 30_000,
                    owner_scope,
                    observation_id,
                    target_sha256,
                    generation,
                ),
            )
            if changed.rowcount != 1:
                raise JournalStateError("EverOS outbox draining transition is stale")

    def finish_everos_outbox(
        self,
        owner_scope: str,
        observation_id: str,
        target_sha256: str,
        *,
        generation: int,
        success: bool,
        error: str | None = None,
    ) -> None:
        """Acknowledge or visibly retry/dead-letter one at-least-once dispatch."""

        owner_scope = _identifier(owner_scope, name="owner_scope")
        observation_id = _hex64(observation_id, name="observation_id")
        target_sha256 = _hex64(target_sha256, name="target_sha256")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError("outbox generation is invalid")
        if not isinstance(success, bool):
            raise TypeError("outbox success must be a boolean")
        if success and error is not None:
            raise ValueError("successful outbox completion cannot contain an error")
        if not success:
            error = _text(error, name="outbox error", cap=4096)
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT attempts FROM rrcv2_everos_outbox_v1
                WHERE owner_scope=? AND observation_id=? AND target_sha256=?
                  AND state IN ('leased','draining') AND generation=?
                """,
                (owner_scope, observation_id, target_sha256, generation),
            ).fetchone()
            if row is None:
                raise JournalStateError("EverOS outbox completion is stale")
            state = (
                "acknowledged"
                if success
                else ("dead_letter" if cast(int, row[0]) >= 5 else "pending")
            )
            changed = self._connection.execute(
                """
                UPDATE rrcv2_everos_outbox_v1
                SET state=?,lease_expires_ms=NULL,last_error=?
                WHERE owner_scope=? AND observation_id=? AND target_sha256=?
                  AND state IN ('leased','draining') AND generation=?
                """,
                (state, error, owner_scope, observation_id, target_sha256, generation),
            )
            if changed.rowcount != 1:
                raise JournalStateError("EverOS outbox completion raced")

    def requeue_everos_outbox(
        self, owner_scope: str, observation_id: str, target_sha256: str
    ) -> None:
        """Explicitly requeue one dead-letter row with a new retry generation."""

        owner_scope = _identifier(owner_scope, name="owner_scope")
        observation_id = _hex64(observation_id, name="observation_id")
        target_sha256 = _hex64(target_sha256, name="target_sha256")
        with self._lock, self._connection:
            changed = self._connection.execute(
                """
                UPDATE rrcv2_everos_outbox_v1
                SET state='pending',generation=generation+1,attempts=0,last_error=NULL
                WHERE owner_scope=? AND observation_id=? AND target_sha256=?
                  AND state='dead_letter'
                """,
                (owner_scope, observation_id, target_sha256),
            )
            if changed.rowcount != 1:
                raise JournalStateError("EverOS dead-letter row is not requeueable")

    def everos_outbox_state(
        self, owner_scope: str, observation_id: str, target_sha256: str
    ) -> tuple[str, int, int, str | None] | None:
        owner_scope = _identifier(owner_scope, name="owner_scope")
        observation_id = _hex64(observation_id, name="observation_id")
        target_sha256 = _hex64(target_sha256, name="target_sha256")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT state,generation,attempts,last_error FROM rrcv2_everos_outbox_v1
                WHERE owner_scope=? AND observation_id=? AND target_sha256=?
                """,
                (owner_scope, observation_id, target_sha256),
            ).fetchone()
        if row is None:
            return None
        return cast(str, row[0]), cast(int, row[1]), cast(int, row[2]), cast(str | None, row[3])

    def everos_route_valid(self, owner_scope: str, target_sha256: str) -> bool:
        """Return whether acknowledged delivery was nonambiguous for a comparison.

        A second controller attempt is valid for interactive at-least-once delivery,
        but cannot be counted as a clean optional-EverOS comparison generation.
        """

        owner_scope = _identifier(owner_scope, name="owner_scope")
        target_sha256 = _hex64(target_sha256, name="target_sha256")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT state,attempts FROM rrcv2_everos_outbox_v1
                WHERE owner_scope=? AND target_sha256=?
                """,
                (owner_scope, target_sha256),
            ).fetchall()
        return all(state == "acknowledged" and attempts == 1 for state, attempts in rows)

    def apply_participant_migration(
        self,
        namespace: str,
        version: int,
        sql_sha256: str,
        statements: dict[str, ParticipantStatementV1],
    ) -> None:
        """Validate, install, and immutably register one participant schema version."""

        if not isinstance(namespace, str) or _NAMESPACE.fullmatch(namespace) is None:
            raise ValueError("participant namespace is invalid")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or not 1 <= version <= 1_000_000
        ):
            raise ValueError("participant version is invalid")
        _hex64(sql_sha256, name="participant definition sha256")
        if not isinstance(statements, dict) or not statements:
            raise ValueError("participant statements must be a nonempty canonical map")
        normalized: dict[str, ParticipantStatementV1] = {}
        for statement_id, statement in statements.items():
            if not isinstance(statement_id, str) or _STATEMENT_ID.fullmatch(statement_id) is None:
                raise ValueError("participant statement ID is invalid")
            if not isinstance(statement, ParticipantStatementV1):
                raise TypeError("participant statement has the wrong type")
            self._validate_participant_sql(namespace, statement)
            normalized[statement_id] = statement
        projection = canonical_json_bytes(
            {name: statement.as_json() for name, statement in normalized.items()}
        )
        if _sha(projection) != sql_sha256:
            raise ValueError("participant definition hash does not match its statement map")

        def migrate(_uow: SQLiteUnitOfWork) -> None:
            existing = self._connection.execute(
                """
                SELECT definition_sha256 FROM rrcv2_participant_migrations
                WHERE namespace=? AND version=?
                """,
                (namespace, version),
            ).fetchone()
            if existing is not None:
                rows = self._load_participant_statements(namespace, version)
                if existing[0] != sql_sha256 or rows != normalized:
                    raise JournalConflict("participant migration version is immutable")
                return
            latest = self._connection.execute(
                "SELECT max(version) FROM rrcv2_participant_migrations WHERE namespace=?",
                (namespace,),
            ).fetchone()[0]
            if version != (1 if latest is None else latest + 1):
                raise JournalConflict("participant migration version is not the next version")
            for statement_id in sorted(normalized):
                statement = normalized[statement_id]
                if statement.kind in {"create_table", "create_index"}:
                    self._connection.execute(statement.sql)
            self._connection.execute(
                """
                INSERT INTO rrcv2_participant_migrations(namespace,version,definition_sha256)
                VALUES(?,?,?)
                """,
                (namespace, version, sql_sha256),
            )
            for statement_id in sorted(normalized):
                statement = normalized[statement_id]
                self._connection.execute(
                    """
                    INSERT INTO rrcv2_participant_statements(
                        namespace,version,statement_id,kind,sql,sha256
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        namespace,
                        version,
                        statement_id,
                        statement.kind,
                        statement.sql,
                        statement.sha256,
                    ),
                )

        self.run_immediate(migrate)

    @staticmethod
    def _validate_participant_sql(namespace: str, statement: ParticipantStatementV1) -> None:
        sql = statement.sql
        upper = sql.upper()
        prefix = f"rrcv2p_{namespace}_"
        forbidden = re.compile(
            r"\b(?:ATTACH|DETACH|PRAGMA|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|"
            r"VACUUM|REINDEX|ALTER|DROP|TRIGGER|VIEW|VIRTUAL|WITH)\b",
            re.IGNORECASE,
        )
        if forbidden.search(sql) or "--" in sql or "/*" in sql or '"' in sql or "'" in sql:
            raise ValueError("participant SQL contains a forbidden construct")
        expected_start = {
            "create_table": "CREATE TABLE IF NOT EXISTS ",
            "create_index": "CREATE INDEX IF NOT EXISTS ",
            "select": "SELECT ",
            "insert": "INSERT INTO ",
            "update": "UPDATE ",
            "delete": "DELETE FROM ",
        }[statement.kind]
        if not upper.startswith(expected_start):
            raise ValueError("participant SQL kind does not match its statement")
        if statement.kind == "select" and re.search(r"\(\s*SELECT\b", sql, re.IGNORECASE):
            raise ValueError("participant SQL subqueries are forbidden")
        objects = re.findall(
            r"\b(?:FROM|JOIN|INTO|UPDATE|ON|TABLE\s+IF\s+NOT\s+EXISTS)\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)",
            sql,
            re.IGNORECASE,
        )
        if not objects or any(not name.startswith(prefix) for name in objects):
            raise ValueError("participant SQL references an object outside its namespace")
        if statement.kind == "create_index":
            match = re.match(
                r"CREATE INDEX IF NOT EXISTS ([A-Za-z_][A-Za-z0-9_]*) ON ",
                sql,
                re.IGNORECASE,
            )
            if match is None or not match.group(1).startswith(prefix):
                raise ValueError("participant index name is outside its namespace")
        tokens = set(re.findall(r"\brrcv2[a-zA-Z0-9_]*\b", sql))
        if any(not token.startswith(prefix) for token in tokens):
            raise ValueError("participant SQL references a nonparticipant RRCv2 object")

    def _load_participant_statements(
        self, namespace: str, version: int
    ) -> dict[str, ParticipantStatementV1]:
        rows = self._connection.execute(
            """
            SELECT statement_id,sql,sha256,kind
            FROM rrcv2_participant_statements
            WHERE namespace=? AND version=? ORDER BY statement_id
            """,
            (namespace, version),
        ).fetchall()
        return {
            cast(str, row[0]): ParticipantStatementV1(
                cast(str, row[1]), cast(str, row[2]), cast(ParticipantKind, row[3])
            )
            for row in rows
        }

    def _participant_cursor(self, uow: SQLiteUnitOfWork, namespace: str) -> TransactionParticipant:
        if not isinstance(namespace, str) or _NAMESPACE.fullmatch(namespace) is None:
            raise ValueError("participant namespace is invalid")
        latest = self._connection.execute(
            "SELECT max(version) FROM rrcv2_participant_migrations WHERE namespace=?",
            (namespace,),
        ).fetchone()[0]
        if latest is None:
            raise JournalStateError("participant namespace has no registered migration")
        statements = self._load_participant_statements(namespace, cast(int, latest))
        runtime = {
            name: statement
            for name, statement in statements.items()
            if statement.kind not in {"create_table", "create_index"}
        }
        return TransactionParticipant(uow, namespace, runtime)
