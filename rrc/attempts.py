"""Durable split-phase ContextMesh attempt adapter.

This adapter shares the physical SQLite transaction owned by
``SQLiteRRCRepository``.  It adds only ContextMesh lifecycle/evidence rows and
updates the canonical attempt state in the same ``BEGIN IMMEDIATE`` unit of
work, so a submitted queue row can never diverge from its attempt state.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from rrc.contextmesh import (
    WorkerContextAttestationV1,
    WorkerEvidenceV1,
    parse_worker_context_attestation,
    parse_worker_evidence,
)
from rrc.contract import CostEventV1, TaskEnvelopeV1, canonical_json_bytes, parse_task_envelope
from rrc.journal import (
    AttemptHandle,
    CallRecordV1,
    JournalConflict,
    JournalStateError,
    SQLiteRRCRepository,
    UsageRecordV1,
)
from rrc.pipeline.solve import WorkerCandidateV1, parse_worker_candidate_record

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[!-~]{1,256}\Z")
_OWNER = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_MAX_BLOB = 2 * 1024 * 1024
_MAX_TRANSCRIPT = 64 * 1024 * 1024


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hex64(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _identifier(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or _IDENTIFIER.fullmatch(value) is None
        or any(character in value for character in ("/", "\\", '"', "'"))
    ):
        raise ValueError(f"{name} does not use the frozen printable identifier grammar")
    return value


def _owner(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _OWNER.fullmatch(value) is None:
        raise ValueError(f"{name} does not use the frozen owner grammar")
    return value


def _canonical(raw: bytes, *, name: str) -> bytes:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_BLOB:
        raise ValueError(f"{name} is missing or exceeds its byte cap")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is not JSON") from exc
    if canonical_json_bytes(value) != raw:
        raise ValueError(f"{name} is not canonical JSON")
    return raw


@dataclass(frozen=True)
class ContextMeshSubmissionV1:
    attempt: AttemptHandle
    task_envelope: TaskEnvelopeV1
    input_root: Path
    assignment_sha256: str
    assignment: bytes
    tool_use_id: str
    agent_id: str
    worker_evidence: WorkerEvidenceV1
    context_attestation: WorkerContextAttestationV1
    candidate: WorkerCandidateV1
    final_message_sha256: str
    transcript_sha256: str
    transcript_raw: bytes


@dataclass(frozen=True)
class RegisteredContextMeshInputV1:
    attempt: AttemptHandle
    task_envelope: TaskEnvelopeV1
    input_root: Path
    target_root: Path
    assignment_sha256: str
    assignment: bytes
    expected_tool_use_id: str


class AttemptRepository:
    """ContextMesh lifecycle participant over one canonical RRC repository."""

    def __init__(self, repository: SQLiteRRCRepository) -> None:
        if not isinstance(repository, SQLiteRRCRepository):
            raise TypeError("AttemptRepository requires SQLiteRRCRepository")
        self.repository = repository
        # The core repository intentionally hides its raw cursor from normal
        # participants.  This trusted adapter is the one M4 implementation that
        # must atomically update both the ContextMesh row and core attempt row.
        self._connection = cast(Any, repository)._connection
        self._install_schema()

    @property
    def authority_id(self) -> str:
        return self.repository.authority_id

    @property
    def database_uuid(self) -> str:
        return self.repository.database_uuid

    def _install_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS rrcv2p_attempts_bindings (
                attempt_id TEXT PRIMARY KEY REFERENCES rrcv2_attempts(attempt_id),
                task_envelope BLOB NOT NULL,
                task_envelope_sha256 TEXT NOT NULL,
                input_root TEXT NOT NULL,
                target_root TEXT NOT NULL,
                assignment BLOB NOT NULL,
                assignment_sha256 TEXT NOT NULL,
                expected_tool_use_id TEXT NOT NULL,
                tool_use_id TEXT,
                agent_id TEXT,
                observed_agent_id TEXT,
                worker_evidence BLOB,
                context_attestation BLOB,
                candidate BLOB,
                final_message_sha256 TEXT,
                transcript_sha256 TEXT,
                transcript BLOB,
                stop_expires_ms INTEGER,
                native_call_id TEXT
            ) STRICT
            """,
            """
            CREATE TABLE IF NOT EXISTS rrcv2p_attempts_events (
                event_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL REFERENCES rrcv2_attempts(attempt_id),
                event_type TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                payload BLOB NOT NULL
            ) STRICT
            """,
            """
            CREATE TABLE IF NOT EXISTS rrcv2p_attempts_queue (
                attempt_id TEXT PRIMARY KEY REFERENCES rrcv2_attempts(attempt_id),
                state TEXT NOT NULL,
                owner_id TEXT,
                generation INTEGER NOT NULL DEFAULT 0,
                lease_expires_ms INTEGER,
                enqueued_ms INTEGER NOT NULL
            ) STRICT
            """,
            """
            CREATE INDEX IF NOT EXISTS rrcv2p_attempts_queue_ready
            ON rrcv2p_attempts_queue(state,enqueued_ms,attempt_id)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS rrcv2p_attempts_expected_tool
            ON rrcv2p_attempts_bindings(expected_tool_use_id)
            """,
        )

        def install(_uow: object) -> None:
            for statement in statements:
                self._connection.execute(statement)
            columns = {
                cast(str, row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(rrcv2p_attempts_bindings)"
                ).fetchall()
            }
            if "native_call_id" not in columns:
                self._connection.execute(
                    "ALTER TABLE rrcv2p_attempts_bindings ADD COLUMN native_call_id TEXT"
                )

        self.repository.run_immediate(install)

    @staticmethod
    def _exact_attempt(current: AttemptHandle, expected: AttemptHandle) -> None:
        if (
            current.attempt_id != expected.attempt_id
            or current.authority_id != expected.authority_id
            or current.database_uuid != expected.database_uuid
            or current.owner_scope != expected.owner_scope
            or current.operation_key != expected.operation_key
            or current.sealed_inputs_sha256 != expected.sealed_inputs_sha256
            or current.state != expected.state
            or current.generation != expected.generation
            or current.cursor != expected.cursor
        ):
            raise JournalStateError("ContextMesh attempt cursor differs")

    def _record_event(
        self,
        *,
        attempt_id: str,
        event_id: str,
        event_type: str,
        payload: bytes,
    ) -> bool:
        _hex64(attempt_id, name="attempt_id")
        _identifier(event_id, name="event_id")
        _owner(event_type, name="event_type")
        payload = _canonical(payload, name="ContextMesh event payload")
        digest = _sha(payload)
        row = self._connection.execute(
            """
            SELECT attempt_id,event_type,payload_sha256,payload
            FROM rrcv2p_attempts_events WHERE event_id=?
            """,
            (event_id,),
        ).fetchone()
        if row is not None:
            if row != (attempt_id, event_type, digest, payload):
                raise JournalConflict("ContextMesh event identity was reused with different bytes")
            return False
        self._connection.execute(
            """
            INSERT INTO rrcv2p_attempts_events(
                event_id,attempt_id,event_type,payload_sha256,payload
            ) VALUES(?,?,?,?,?)
            """,
            (event_id, attempt_id, event_type, digest, payload),
        )
        return True

    def register_prepared_input(
        self,
        attempt: AttemptHandle,
        *,
        task_envelope: bytes,
        input_root: Path,
        target_root: Path | None = None,
        assignment: bytes,
        expected_tool_use_id: str,
    ) -> AttemptHandle:
        """Persist all input authorities needed after the root process exits."""

        task_envelope = _canonical(task_envelope, name="task envelope")
        parse_task_envelope(task_envelope)
        assignment = _canonical(assignment, name="coding assignment")
        _identifier(expected_tool_use_id, name="expected_tool_use_id")
        if not isinstance(input_root, Path) or not input_root.is_absolute():
            raise ValueError("ContextMesh input_root must be absolute")
        root_text = str(input_root)
        target = input_root if target_root is None else target_root
        if not isinstance(target, Path) or not target.is_absolute():
            raise ValueError("ContextMesh target_root must be absolute")
        target_text = str(target)
        if (
            len(root_text.encode("utf-8")) > 4096
            or "\x00" in root_text
            or len(target_text.encode("utf-8")) > 4096
            or "\x00" in target_text
        ):
            raise ValueError("ContextMesh input_root is invalid")
        expected = (
            task_envelope,
            _sha(task_envelope),
            root_text,
            target_text,
            assignment,
            _sha(assignment),
            expected_tool_use_id,
        )

        def register(_uow: object) -> None:
            current = self.repository._load_handle_row(attempt.attempt_id)
            self._exact_attempt(current, attempt)
            if current.state not in {"preparing", "prepared"}:
                raise JournalStateError("ContextMesh input may be registered only before spawn")
            row = self._connection.execute(
                """
                SELECT task_envelope,task_envelope_sha256,input_root,target_root,assignment,
                       assignment_sha256,expected_tool_use_id
                FROM rrcv2p_attempts_bindings WHERE attempt_id=?
                """,
                (attempt.attempt_id,),
            ).fetchone()
            if row is not None:
                if row != expected:
                    raise JournalConflict("ContextMesh attempt input authority differs")
                return
            self._connection.execute(
                """
                INSERT INTO rrcv2p_attempts_bindings(
                    attempt_id,task_envelope,task_envelope_sha256,input_root,target_root,
                    assignment,assignment_sha256,expected_tool_use_id
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (attempt.attempt_id, *expected),
            )

        self.repository.run_immediate(register)
        return self.repository.load_attempt(attempt.attempt_id)

    @staticmethod
    def _native_worker_call_id(attempt_id: str) -> str:
        return "call-" + _sha(
            canonical_json_bytes(
                {
                    "attempt_id": attempt_id,
                    "role": "native_worker",
                    "stage": "implement",
                    "v": 1,
                }
            )
        )

    def prepare_native_worker(
        self,
        attempt: AttemptHandle,
        *,
        prompt: str,
        stage_ordinal: int,
        model: str = "gpt-5.6-luna",
        call_id: str | None = None,
    ) -> AttemptHandle:
        """Journal the native worker before the root is allowed to spawn it."""

        if not isinstance(prompt, str) or not prompt:
            raise ValueError("native worker prompt must be nonempty")
        call_id = call_id or self._native_worker_call_id(attempt.attempt_id)
        _identifier(call_id, name="native worker call_id")
        binding = self._connection.execute(
            "SELECT native_call_id FROM rrcv2p_attempts_bindings WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone()
        if binding is None:
            raise JournalConflict("native worker input binding is missing")
        if binding[0] is not None and binding[0] != call_id:
            raise JournalConflict("native worker call identity changed")

        def bind_call_identity(_uow: object) -> None:
            current = self._connection.execute(
                "SELECT native_call_id FROM rrcv2p_attempts_bindings WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone()
            if current is None or current[0] not in {None, call_id}:
                raise JournalConflict("native worker call identity changed")
            self._connection.execute(
                """
                UPDATE rrcv2p_attempts_bindings SET native_call_id=?
                WHERE attempt_id=? AND (native_call_id IS NULL OR native_call_id=?)
                """,
                (call_id, attempt.attempt_id, call_id),
            )

        self.repository.run_immediate(bind_call_identity)
        existing = self.repository.call_state(attempt.attempt_id, call_id)
        if existing is not None:
            if existing not in {
                "call_prepared",
                "call_started",
                "call_observed",
                "call_committed",
            }:
                raise JournalConflict("native worker call has an invalid state")
            return self.repository.load_attempt(attempt.attempt_id)
        settings = canonical_json_bytes(
            {
                "model": model,
                "reasoning": "low",
                "service_tier": "priority",
                "v": 1,
            }
        )
        return self.repository.prepare_call(
            attempt,
            CallRecordV1(
                call_id=call_id,
                stage="implement",
                stage_ordinal=stage_ordinal,
                role="native_worker",
                model=model,
                settings_sha256=_sha(settings),
                prompt_sha256=_sha(prompt.encode("utf-8", errors="strict")),
                transcript_baseline_sha256=_sha(b""),
            ),
            expected_state=attempt.state,
            expected_generation=attempt.generation,
            expected_cursor=attempt.cursor,
        )

    def start_native_worker(self, attempt: AttemptHandle, *, agent_id: str) -> AttemptHandle:
        """Advance the prepared worker call immediately after native spawn binding."""

        _identifier(agent_id, name="agent_id")
        binding = self._connection.execute(
            "SELECT native_call_id FROM rrcv2p_attempts_bindings WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone()
        if binding is None or not isinstance(binding[0], str):
            raise JournalConflict("native worker call identity is missing")
        call_id = cast(str, binding[0])
        state = self.repository.call_state(attempt.attempt_id, call_id)
        if state in {"call_started", "call_observed", "call_committed"}:
            return self.repository.load_attempt(attempt.attempt_id)
        if state != "call_prepared":
            raise JournalConflict("native worker call was not prepared before spawn")
        launch = canonical_json_bytes(
            {"agent_id": agent_id, "attempt_id": attempt.attempt_id, "v": 1}
        )
        return self.repository.mark_call_started(
            attempt,
            call_id,
            launch,
            expected_state=attempt.state,
            expected_generation=attempt.generation,
            expected_cursor=attempt.cursor,
        )

    def authorize_native_worker_launch(
        self, attempt: AttemptHandle, *, tool_use_id: str
    ) -> AttemptHandle:
        """Commit ``call_started`` immediately before allowing the native spawn tool."""

        _identifier(tool_use_id, name="tool_use_id")
        binding = self._connection.execute(
            "SELECT native_call_id FROM rrcv2p_attempts_bindings WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone()
        if binding is None or not isinstance(binding[0], str):
            raise JournalConflict("native worker call identity is missing")
        call_id = cast(str, binding[0])
        state = self.repository.call_state(attempt.attempt_id, call_id)
        if state in {"call_started", "call_observed", "call_committed"}:
            return self.repository.load_attempt(attempt.attempt_id)
        if state != "call_prepared":
            raise JournalConflict("native worker call was not prepared before authorization")
        launch = canonical_json_bytes(
            {
                "attempt_id": attempt.attempt_id,
                "tool_use_id": tool_use_id,
                "v": 1,
            }
        )
        return self.repository.mark_call_started(
            attempt,
            call_id,
            launch,
            expected_state=attempt.state,
            expected_generation=attempt.generation,
            expected_cursor=attempt.cursor,
        )

    def observe_subagent_start(
        self,
        attempt: AttemptHandle,
        *,
        event_id: str,
        agent_id: str,
        payload: bytes,
    ) -> AttemptHandle:
        _identifier(agent_id, name="agent_id")

        def observe(_uow: object) -> None:
            current = self.repository._load_handle_row(attempt.attempt_id)
            self._exact_attempt(current, attempt)
            if not self._record_event(
                attempt_id=attempt.attempt_id,
                event_id=event_id,
                event_type="subagent_start",
                payload=payload,
            ):
                return
            if current.state not in {"prepared", "spawned"}:
                raise JournalStateError("SubagentStart is not legal in this attempt state")
            row = self._connection.execute(
                "SELECT observed_agent_id FROM rrcv2p_attempts_bindings WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone()
            if row is None:
                raise JournalConflict("ContextMesh input binding is missing")
            if row[0] is not None and row[0] != agent_id:
                raise JournalConflict("SubagentStart agent differs")
            self._connection.execute(
                "UPDATE rrcv2p_attempts_bindings SET observed_agent_id=? WHERE attempt_id=?",
                (agent_id, attempt.attempt_id),
            )

        self.repository.run_immediate(observe)
        return self.repository.load_attempt(attempt.attempt_id)

    def bind_spawn(
        self,
        attempt: AttemptHandle,
        *,
        event_id: str,
        tool_use_id: str,
        agent_id: str,
        payload: bytes,
    ) -> AttemptHandle:
        _identifier(tool_use_id, name="tool_use_id")
        _identifier(agent_id, name="agent_id")

        def bind(_uow: object) -> None:
            current = self.repository._load_handle_row(attempt.attempt_id)
            self._exact_attempt(current, attempt)
            if not self._record_event(
                attempt_id=attempt.attempt_id,
                event_id=event_id,
                event_type="post_tool_spawn",
                payload=payload,
            ):
                return
            row = self._connection.execute(
                """
                SELECT tool_use_id,agent_id,observed_agent_id,worker_evidence
                FROM rrcv2p_attempts_bindings WHERE attempt_id=?
                """,
                (attempt.attempt_id,),
            ).fetchone()
            if row is None:
                raise JournalConflict("ContextMesh input binding is missing")
            if row[0] is not None or row[1] is not None:
                raise JournalConflict("ContextMesh spawn was already bound")
            expected_tool = self._connection.execute(
                "SELECT expected_tool_use_id FROM rrcv2p_attempts_bindings WHERE attempt_id=?",
                (attempt.attempt_id,),
            ).fetchone()
            if expected_tool != (tool_use_id,):
                raise JournalConflict("PostToolUse tool differs from PreToolUse authority")
            if row[2] is not None and row[2] != agent_id:
                raise JournalConflict("PostToolUse agent differs from SubagentStart")
            if current.state not in {"prepared", "stop_pending_bind"}:
                raise JournalStateError("PostToolUse spawn is not legal in this attempt state")
            next_state = "spawned" if current.state == "prepared" else "submitted"
            self._connection.execute(
                """
                UPDATE rrcv2p_attempts_bindings SET tool_use_id=?,agent_id=?,stop_expires_ms=NULL
                WHERE attempt_id=?
                """,
                (tool_use_id, agent_id, attempt.attempt_id),
            )
            if current.state == "stop_pending_bind" and row[3] is None:
                raise JournalConflict("stop-pending attempt has no worker evidence")
            changed = self._connection.execute(
                """
                UPDATE rrcv2_attempts SET state=?,cursor=cursor+1
                WHERE attempt_id=? AND state=? AND generation=? AND cursor=?
                """,
                (
                    next_state,
                    attempt.attempt_id,
                    current.state,
                    current.generation,
                    current.cursor,
                ),
            )
            if changed.rowcount != 1:
                raise JournalStateError("ContextMesh spawn compare-and-swap failed")
            if next_state == "submitted":
                self._enqueue(attempt.attempt_id)

        self.repository.run_immediate(bind)
        current = self.repository.load_attempt(attempt.attempt_id)
        return self.start_native_worker(current, agent_id=agent_id)

    def find_by_tool_use(self, *, owner_scope: str, tool_use_id: str) -> AttemptHandle | None:
        """Find the unique durable pre-tool assignment for a native tool call."""

        _owner(owner_scope, name="owner_scope")
        _identifier(tool_use_id, name="tool_use_id")
        row = self._connection.execute(
            """
            SELECT a.attempt_id FROM rrcv2p_attempts_bindings AS b
            JOIN rrcv2_attempts AS a ON a.attempt_id=b.attempt_id
            WHERE a.owner_scope=? AND b.expected_tool_use_id=?
            """,
            (owner_scope, tool_use_id),
        ).fetchone()
        return None if row is None else self.repository.load_attempt(cast(str, row[0]))

    def find_by_agent(self, *, owner_scope: str, agent_id: str) -> AttemptHandle | None:
        """Find the unique attempt bound to or observed for one native agent."""

        _owner(owner_scope, name="owner_scope")
        _identifier(agent_id, name="agent_id")
        rows = self._connection.execute(
            """
            SELECT a.attempt_id FROM rrcv2p_attempts_bindings AS b
            JOIN rrcv2_attempts AS a ON a.attempt_id=b.attempt_id
            WHERE a.owner_scope=? AND (b.agent_id=? OR b.observed_agent_id=?)
            ORDER BY a.attempt_id
            """,
            (owner_scope, agent_id, agent_id),
        ).fetchall()
        if len(rows) > 1:
            raise JournalConflict("native agent is bound to multiple ContextMesh attempts")
        return None if not rows else self.repository.load_attempt(cast(str, rows[0][0]))

    def load_registered_input(self, attempt_id: str) -> RegisteredContextMeshInputV1:
        """Reopen the pre-provider ContextMesh input authority."""

        _hex64(attempt_id, name="attempt_id")
        attempt = self.repository.load_attempt(attempt_id)
        row = self._connection.execute(
            """
            SELECT task_envelope,task_envelope_sha256,input_root,target_root,assignment,
                   assignment_sha256,expected_tool_use_id
            FROM rrcv2p_attempts_bindings WHERE attempt_id=?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise JournalStateError("ContextMesh registered input is unavailable")
        task_raw = cast(bytes, row[0])
        assignment = cast(bytes, row[4])
        if _sha(task_raw) != row[1] or _sha(assignment) != row[5]:
            raise JournalConflict("ContextMesh registered input hash differs")
        envelope = parse_task_envelope(task_raw)
        root = Path(cast(str, row[2]))
        object.__setattr__(envelope, "_input_root", root)
        return RegisteredContextMeshInputV1(
            attempt=attempt,
            task_envelope=envelope,
            input_root=root,
            target_root=Path(cast(str, row[3])),
            assignment_sha256=cast(str, row[5]),
            assignment=assignment,
            expected_tool_use_id=cast(str, row[6]),
        )

    def submit_stop(
        self,
        attempt: AttemptHandle,
        *,
        event_id: str,
        worker_evidence: bytes,
        context_attestation: bytes,
        candidate: bytes,
        transcript: bytes,
        payload: bytes,
        cell_id: str | None = None,
        now_ms: int | None = None,
    ) -> AttemptHandle:
        evidence = parse_worker_evidence(worker_evidence)
        context = parse_worker_context_attestation(context_attestation)
        parsed_candidate = parse_worker_candidate_record(candidate)
        if not isinstance(transcript, bytes) or not transcript or len(transcript) > _MAX_TRANSCRIPT:
            raise ValueError("worker transcript is missing or exceeds its byte cap")
        if _sha(transcript) != evidence.transcript_sha256:
            raise JournalConflict("worker transcript differs from WorkerEvidenceV1")
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now_ms must be a nonnegative integer")
        if cell_id is None:
            cell_id = "contextmesh-" + attempt.attempt_id[:32]
        _owner(cell_id, name="cell_id")

        def stop(_uow: object) -> None:
            current = self.repository._load_handle_row(attempt.attempt_id)
            self._exact_attempt(current, attempt)
            if not self._record_event(
                attempt_id=attempt.attempt_id,
                event_id=event_id,
                event_type="subagent_stop",
                payload=payload,
            ):
                return
            row = self._connection.execute(
                """
                SELECT task_envelope,assignment_sha256,tool_use_id,agent_id,observed_agent_id,
                       worker_evidence,native_call_id
                FROM rrcv2p_attempts_bindings WHERE attempt_id=?
                """,
                (attempt.attempt_id,),
            ).fetchone()
            if row is None:
                raise JournalConflict("ContextMesh input binding is missing")
            envelope = parse_task_envelope(cast(bytes, row[0]))
            if (
                evidence.attempt_id != attempt.attempt_id
                or context.attempt_id != attempt.attempt_id
                or evidence.task_id != envelope.task.task_id
                or evidence.agent_id != context.agent_id
                or evidence.tool_use_id != context.tool_use_id
                or evidence.context_attestation_sha256 != _sha(context_attestation)
                or evidence.final_message_sha256 != parsed_candidate.final_message_sha256
                or context.assignment_sha256 != row[1]
                or (row[2] is not None and evidence.tool_use_id != row[2])
                or (row[3] is not None and evidence.agent_id != row[3])
                or (row[4] is not None and evidence.agent_id != row[4])
            ):
                raise JournalConflict("worker evidence/candidate/context correlation differs")
            if row[5] is not None:
                raise JournalConflict("worker stop evidence was already stored")
            if current.state not in {"prepared", "spawned"}:
                raise JournalStateError("SubagentStop is not legal in this attempt state")
            if not isinstance(row[6], str):
                raise JournalConflict("native worker call identity is missing")
            call_id = cast(str, row[6])
            call = self._connection.execute(
                """
                SELECT state,record FROM rrcv2_calls
                WHERE attempt_id=? AND call_id=?
                """,
                (attempt.attempt_id, call_id),
            ).fetchone()
            if (
                call is None
                or call[0] not in {"call_prepared", "call_started"}
                or not isinstance(call[1], bytes)
            ):
                raise JournalStateError("native worker call is not available for completion")
            try:
                call_record = json.loads(cast(bytes, call[1]))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise JournalConflict("native worker call record is malformed") from exc
            if (
                not isinstance(call_record, dict)
                or call_record.get("stage") != "implement"
                or call_record.get("role") != "native_worker"
                or call_record.get("model") != evidence.requested_model
            ):
                raise JournalConflict("native worker call record differs from worker evidence")
            cost = CostEventV1(
                cost_event_id=call_id,
                cell_id=cell_id,
                attempt_id=attempt.attempt_id,
                task_id=evidence.task_id,
                arm=evidence.arm,
                stage="implement",
                stage_ordinal=cast(int, call_record["stage_ordinal"]),
                prompt_sha256=cast(str, call_record["prompt_sha256"]),
                final_message_sha256=evidence.final_message_sha256,
                transcript_sha256=evidence.transcript_sha256,
                requested_provider=evidence.requested_provider,
                requested_model=evidence.requested_model,
                requested_reasoning=evidence.requested_reasoning,
                requested_service_tier=evidence.requested_service_tier,
                identity_attestation=evidence.identity_attestation,
                effective_provider=evidence.effective_provider,
                effective_model=evidence.effective_model,
                effective_reasoning=evidence.effective_reasoning,
                effective_service_tier=evidence.effective_service_tier,
                input_tokens=evidence.input_tokens,
                cached_input_tokens=evidence.cached_input_tokens,
                output_tokens=evidence.output_tokens,
                reasoning_output_tokens=evidence.reasoning_output_tokens,
                provider_total_tokens=evidence.provider_total_tokens,
            )
            usage = UsageRecordV1(
                evidence.input_tokens,
                evidence.output_tokens,
                evidence.provider_total_tokens,
                evidence.cached_input_tokens,
            )
            transcript_ref = canonical_json_bytes(
                {"bytes": len(transcript), "sha256": evidence.transcript_sha256, "v": 1}
            )
            call_changed = self._connection.execute(
                """
                UPDATE rrcv2_calls
                SET state='call_committed',transcript_ref=?,usage=?,outcome=?,cost_event=?
                WHERE attempt_id=? AND call_id=? AND state IN ('call_prepared','call_started')
                """,
                (
                    transcript_ref,
                    usage.canonical_bytes(),
                    parsed_candidate.canonical_bytes(),
                    cost.canonical_bytes(),
                    attempt.attempt_id,
                    call_id,
                ),
            )
            if call_changed.rowcount != 1:
                raise JournalStateError("native worker call commit compare-and-swap failed")
            next_state = "stop_pending_bind" if current.state == "prepared" else "submitted"
            self._connection.execute(
                """
                UPDATE rrcv2p_attempts_bindings
                SET worker_evidence=?,context_attestation=?,candidate=?,
                    final_message_sha256=?,transcript_sha256=?,transcript=?,stop_expires_ms=?
                WHERE attempt_id=?
                """,
                (
                    worker_evidence,
                    context_attestation,
                    candidate,
                    evidence.final_message_sha256,
                    evidence.transcript_sha256,
                    transcript,
                    now + 20_000 if next_state == "stop_pending_bind" else None,
                    attempt.attempt_id,
                ),
            )
            changed = self._connection.execute(
                """
                UPDATE rrcv2_attempts SET state=?,cursor=cursor+1
                WHERE attempt_id=? AND state=? AND generation=? AND cursor=?
                """,
                (
                    next_state,
                    attempt.attempt_id,
                    current.state,
                    current.generation,
                    current.cursor,
                ),
            )
            if changed.rowcount != 1:
                raise JournalStateError("ContextMesh stop compare-and-swap failed")
            if next_state == "submitted":
                self._enqueue(attempt.attempt_id)

        self.repository.run_immediate(stop)
        return self.repository.load_attempt(attempt.attempt_id)

    def _enqueue(self, attempt_id: str) -> None:
        row = self._connection.execute(
            "SELECT state FROM rrcv2p_attempts_queue WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is not None:
            if row[0] != "pending":
                raise JournalConflict("ContextMesh enqueue state differs")
            return
        self._connection.execute(
            """
            INSERT INTO rrcv2p_attempts_queue(attempt_id,state,enqueued_ms)
            VALUES(?,'pending',?)
            """,
            (attempt_id, int(time.time() * 1000)),
        )

    def reconcile_queue(
        self,
        *,
        owner_scope: str,
        owner_is_alive: Callable[[str], bool],
        now_ms: int | None = None,
    ) -> tuple[str, ...]:
        """Close terminal rows and release only expired leases with a proven-dead owner."""

        _owner(owner_scope, name="owner_scope")
        if not callable(owner_is_alive):
            raise TypeError("owner_is_alive must be callable")
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now_ms must be a nonnegative integer")
        recovered: list[str] = []

        def reconcile(_uow: object) -> None:
            self._connection.execute(
                """
                UPDATE rrcv2p_attempts_queue SET state='closed',lease_expires_ms=NULL
                WHERE attempt_id IN (
                    SELECT a.attempt_id FROM rrcv2_attempts AS a
                    WHERE a.owner_scope=? AND a.state IN ('accepted','rejected')
                ) AND state!='closed'
                """,
                (owner_scope,),
            )
            rows = self._connection.execute(
                """
                SELECT q.attempt_id,q.owner_id,q.generation,a.cursor
                FROM rrcv2p_attempts_queue AS q
                JOIN rrcv2_attempts AS a ON a.attempt_id=q.attempt_id
                WHERE a.owner_scope=? AND a.state='finishing' AND q.state='claimed'
                  AND q.lease_expires_ms IS NOT NULL AND q.lease_expires_ms<=?
                ORDER BY q.attempt_id
                """,
                (owner_scope, now),
            ).fetchall()
            for attempt_id, previous_owner, generation, cursor in rows:
                if not isinstance(previous_owner, str) or owner_is_alive(previous_owner):
                    continue
                queue = self._connection.execute(
                    """
                    UPDATE rrcv2p_attempts_queue
                    SET state='pending',owner_id=NULL,lease_expires_ms=NULL
                    WHERE attempt_id=? AND state='claimed' AND owner_id=? AND generation=?
                    """,
                    (attempt_id, previous_owner, generation),
                )
                core = self._connection.execute(
                    """
                    UPDATE rrcv2_attempts
                    SET state='submitted',terminal_owner=NULL,cursor=cursor+1
                    WHERE attempt_id=? AND state='finishing' AND terminal_owner=?
                      AND generation=? AND cursor=?
                    """,
                    (attempt_id, previous_owner, generation, cursor),
                )
                if queue.rowcount != 1 or core.rowcount != 1:
                    raise JournalStateError("expired finisher lease recovery CAS failed")
                recovered.append(cast(str, attempt_id))

        self.repository.run_immediate(reconcile)
        return tuple(recovered)

    def expired_stop_pending(
        self, *, owner_scope: str, now_ms: int | None = None
    ) -> tuple[AttemptHandle, ...]:
        """Return stop-before-bind attempts whose bounded correlation window expired."""

        _owner(owner_scope, name="owner_scope")
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now_ms must be a nonnegative integer")
        rows = self._connection.execute(
            """
            SELECT a.attempt_id FROM rrcv2_attempts AS a
            JOIN rrcv2p_attempts_bindings AS b ON b.attempt_id=a.attempt_id
            WHERE a.owner_scope=? AND a.state='stop_pending_bind'
              AND b.stop_expires_ms IS NOT NULL AND b.stop_expires_ms<=?
            ORDER BY a.attempt_id
            """,
            (owner_scope, now),
        ).fetchall()
        return tuple(self.repository.load_attempt(cast(str, row[0])) for row in rows)

    def claim_next(
        self,
        *,
        owner_scope: str,
        owner_id: str,
        now_ms: int | None = None,
        lease_ms: int = 30_000,
    ) -> AttemptHandle | None:
        _owner(owner_scope, name="owner_scope")
        _owner(owner_id, name="owner_id")
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if (
            isinstance(now, bool)
            or not isinstance(now, int)
            or now < 0
            or isinstance(lease_ms, bool)
            or not isinstance(lease_ms, int)
            or not 1_000 <= lease_ms <= 120_000
        ):
            raise ValueError("ContextMesh lease time is invalid")
        selected: list[str] = []

        def claim(_uow: object) -> None:
            row = self._connection.execute(
                """
                SELECT q.attempt_id,q.generation,a.generation,a.cursor
                FROM rrcv2p_attempts_queue AS q
                JOIN rrcv2_attempts AS a ON a.attempt_id=q.attempt_id
                WHERE q.state='pending' AND a.state='submitted' AND a.owner_scope=?
                ORDER BY q.enqueued_ms,q.attempt_id LIMIT 1
                """,
                (owner_scope,),
            ).fetchone()
            if row is None:
                return
            attempt_id = cast(str, row[0])
            generation = cast(int, row[2]) + 1
            queue = self._connection.execute(
                """
                UPDATE rrcv2p_attempts_queue
                SET state='claimed',owner_id=?,generation=?,lease_expires_ms=?
                WHERE attempt_id=? AND state='pending' AND generation=?
                """,
                (owner_id, generation, now + lease_ms, attempt_id, row[1]),
            )
            core = self._connection.execute(
                """
                UPDATE rrcv2_attempts
                SET state='finishing',terminal_owner=?,generation=?,cursor=cursor+1
                WHERE attempt_id=? AND state='submitted' AND generation=? AND cursor=?
                """,
                (owner_id, generation, attempt_id, row[2], row[3]),
            )
            if queue.rowcount != 1 or core.rowcount != 1:
                raise JournalStateError("ContextMesh finisher claim compare-and-swap failed")
            selected.append(attempt_id)

        self.repository.run_immediate(claim)
        return None if not selected else self.repository.load_attempt(selected[0])

    def renew(
        self,
        attempt: AttemptHandle,
        *,
        owner_id: str,
        now_ms: int | None = None,
        lease_ms: int = 30_000,
    ) -> AttemptHandle:
        _owner(owner_id, name="owner_id")
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now_ms is invalid")

        def heartbeat(_uow: object) -> None:
            current = self.repository._load_handle_row(attempt.attempt_id)
            self._exact_attempt(current, attempt)
            if current.state != "finishing":
                raise JournalStateError("only a finishing ContextMesh attempt may renew")
            changed = self._connection.execute(
                """
                UPDATE rrcv2p_attempts_queue SET lease_expires_ms=?
                WHERE attempt_id=? AND state='claimed' AND owner_id=? AND generation=?
                """,
                (now + lease_ms, attempt.attempt_id, owner_id, attempt.generation),
            )
            if changed.rowcount != 1:
                raise JournalStateError("ContextMesh lease renewal compare-and-swap failed")

        self.repository.run_immediate(heartbeat)
        return self.repository.load_attempt(attempt.attempt_id)

    def load_submission(self, attempt_id: str) -> ContextMeshSubmissionV1:
        _hex64(attempt_id, name="attempt_id")
        attempt = self.repository.load_attempt(attempt_id)
        row = self._connection.execute(
            """
            SELECT task_envelope,task_envelope_sha256,input_root,target_root,assignment,
                   assignment_sha256,tool_use_id,agent_id,worker_evidence,
                   context_attestation,candidate,final_message_sha256,transcript_sha256
                   ,transcript
            FROM rrcv2p_attempts_bindings WHERE attempt_id=?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None or any(item is None for item in row[6:]):
            raise JournalStateError("ContextMesh submission is incomplete")
        task_raw = cast(bytes, row[0])
        assignment = cast(bytes, row[4])
        evidence_raw = cast(bytes, row[8])
        context_raw = cast(bytes, row[9])
        candidate_raw = cast(bytes, row[10])
        transcript = cast(bytes, row[13])
        if _sha(task_raw) != row[1] or _sha(assignment) != row[5]:
            raise JournalConflict("ContextMesh submission input hash differs")
        envelope = parse_task_envelope(task_raw)
        input_root = Path(cast(str, row[2]))
        object.__setattr__(envelope, "_input_root", input_root)
        evidence = parse_worker_evidence(evidence_raw)
        context = parse_worker_context_attestation(context_raw)
        candidate = parse_worker_candidate_record(candidate_raw)
        if (
            evidence.attempt_id != attempt_id
            or context.attempt_id != attempt_id
            or evidence.tool_use_id != row[6]
            or evidence.agent_id != row[7]
            or evidence.final_message_sha256 != row[11]
            or evidence.transcript_sha256 != row[12]
            or _sha(transcript) != row[12]
            or evidence.context_attestation_sha256 != _sha(context_raw)
            or candidate.final_message_sha256 != row[11]
        ):
            raise JournalConflict("ContextMesh submission evidence differs")
        return ContextMeshSubmissionV1(
            attempt=attempt,
            task_envelope=envelope,
            input_root=input_root,
            assignment_sha256=cast(str, row[5]),
            assignment=assignment,
            tool_use_id=cast(str, row[6]),
            agent_id=cast(str, row[7]),
            worker_evidence=evidence,
            context_attestation=context,
            candidate=candidate,
            final_message_sha256=cast(str, row[11]),
            transcript_sha256=cast(str, row[12]),
            transcript_raw=transcript,
        )

    def close_queue(self, attempt_id: str) -> None:
        """Mark a claimed queue row terminal after canonical attempt commit."""

        _hex64(attempt_id, name="attempt_id")

        def close(_uow: object) -> None:
            attempt = self.repository._load_handle_row(attempt_id)
            if attempt.state not in {"accepted", "rejected"}:
                raise JournalStateError("ContextMesh queue closes only after terminal commit")
            changed = self._connection.execute(
                """
                UPDATE rrcv2p_attempts_queue SET state='closed',lease_expires_ms=NULL
                WHERE attempt_id=? AND state='claimed' AND generation=?
                """,
                (attempt_id, attempt.generation),
            )
            if changed.rowcount != 1:
                raise JournalStateError("ContextMesh queue close compare-and-swap failed")

        self.repository.run_immediate(close)


def event_payload(event_type: str, **values: object) -> bytes:
    """Create one exact bounded event payload used as retry identity."""

    _owner(event_type, name="event_type")
    return canonical_json_bytes({"event_type": event_type, "v": 1, **values})
