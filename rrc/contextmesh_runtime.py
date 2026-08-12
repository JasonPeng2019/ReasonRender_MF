"""Controller-side ContextMesh preparation for the canonical RRCv2 engine."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, cast

from rrc.attempts import AttemptRepository
from rrc.contextmesh import (
    CodingAssignmentV1,
    WorkerContextAttestationV1,
    WorkerEvidenceV1,
    WorkerInitialMessageV1,
    parse_coding_assignment,
)
from rrc.contract import (
    ArmMode,
    ArtifactRefV1,
    Config,
    ModelPort,
    ReferencedTaskInputV1,
    RetrievalPort,
    RunContext,
    TaskEnvelopeV1,
    canonical_json_bytes,
    reopen_task_inputs,
    seal_task_input,
    task_envelope_bytes,
)
from rrc.journal import AttemptHandle, JournalConflict, JournalStateError, contextmesh_operation_key
from rrc.pipeline.solve import (
    PreparedSolve,
    WorkerCandidateV1,
    begin_contextmesh_attempt,
    hydrate_prepared,
    parse_worker_candidate,
    prepare_contextmesh,
)

_MAX_SOURCE_BYTES = 1024 * 1024
_MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
_SUBAGENT_CONTEXT = (
    "ReasonRenderCoding already sealed the source and tests. Use only the source-blind "
    "IMPLEMENT assignment. Do not call tools or try to read repository files."
)


def _native_implement_ordinal(*, mode: str, branch: str) -> int:
    if mode == "cold" and branch == "miss":
        return 3
    if mode == "warm" and branch == "reuse":
        return 2
    if mode == "warm" and branch in {"miss", "prime"}:
        return 4
    raise ValueError("native IMPLEMENT has no legal stage ordinal")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_confined(root: Path, relative: str, *, cap: int) -> tuple[bytes, int]:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("ContextMesh target root must be an absolute real directory")
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("ContextMesh reference path is not canonical")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    descriptors: list[int] = []
    try:
        current = os.open(root, directory_flags)
        descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise ValueError("ContextMesh reference parent is not a directory")
        descriptor = os.open(parts[-1], flags | getattr(os, "O_NONBLOCK", 0), dir_fd=current)
        descriptors.append(descriptor)
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_size > cap:
            raise ValueError("ContextMesh reference is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = cap + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > cap or len(raw) != observed.st_size:
            raise ValueError("ContextMesh reference changed or exceeds its cap")
        return raw, stat.S_IMODE(observed.st_mode)
    except OSError as exc:
        raise ValueError("ContextMesh reference is unavailable") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _prove_absent(root: Path, relative: str) -> None:
    parts = PurePosixPath(relative).parts
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    descriptors: list[int] = []
    try:
        current = os.open(root, flags)
        descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(part, flags, dir_fd=current)
            descriptors.append(current)
        try:
            os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise ValueError("greenfield ContextMesh target already exists")
    except OSError as exc:
        raise ValueError("greenfield ContextMesh parent is unavailable") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _write(path: Path, raw: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_memory_runtime(
    repository: object,
    *,
    owner_scope: str,
    backend: str,
    everos_target_path: Path | None = None,
) -> tuple[Config, RetrievalPort]:
    """Build the exact local or optional-EverOS retrieval route from sealed bytes."""

    from rrc.everos import EverOSHybridRetrieval
    from rrc.retrieval import SQLiteHybridRetrieval

    if backend == "sqlite":
        if everos_target_path is not None:
            raise ValueError("SQLite memory cannot name an EverOS target")
        config = Config(owner_scope=owner_scope, memory_backend="sqlite")
        return config, SQLiteHybridRetrieval(cast(Any, repository))
    if backend != "everos" or everos_target_path is None:
        raise ValueError("EverOS memory requires a sealed target descriptor")
    target_path = everos_target_path.absolute()
    raw, mode = _read_confined(target_path.parent, target_path.name, cap=16 * 1024)
    if mode != 0o600:
        raise ValueError("EverOS target descriptor must be mode 0600")
    config = Config(owner_scope=owner_scope, memory_backend="everos", everos_target=raw)
    return config, EverOSHybridRetrieval(cast(Any, repository))


@dataclass(frozen=True)
class PreparedContextMeshAssignmentV1:
    prepared: PreparedSolve
    assignment: CodingAssignmentV1
    assignment_sha256: str
    worker_prompt: str


@dataclass(frozen=True)
class NativeWorkerResultV1:
    """Strict controller-owned interpretation of one completed native worker."""

    candidate: WorkerCandidateV1
    context_attestation: WorkerContextAttestationV1
    worker_evidence: WorkerEvidenceV1
    transcript_raw: bytes


def subagent_start_context() -> str:
    """Return the exact source-blind SubagentStart message attested at Stop."""

    return _SUBAGENT_CONTEXT


def _strict_rollout(raw: bytes) -> list[dict[str, Any]]:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_TRANSCRIPT_BYTES:
        raise ValueError("worker transcript is missing or exceeds its byte cap")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("worker transcript is not UTF-8") from exc
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("worker transcript contains malformed JSONL") from exc
        if not isinstance(row, dict):
            raise ValueError("worker transcript contains a non-object row")
        rows.append(row)
    if not rows:
        raise ValueError("worker transcript contains no rows")
    return rows


def _message(payload: dict[str, Any]) -> tuple[str, str, bytes]:
    role = payload.get("role")
    content = payload.get("content")
    if (
        role not in {"system", "developer", "user", "assistant"}
        or not isinstance(content, list)
        or not content
        or any(
            not isinstance(block, dict)
            or block.get("type") != "input_text"
            or not isinstance(block.get("text"), str)
            for block in content
        )
    ):
        raise ValueError("worker transcript message is not exact input_text content")
    text = "\n".join(cast(str, block["text"]) for block in content)
    return cast(str, role), text, canonical_json_bytes(content)


def derive_native_worker_result(
    *,
    registered_assignment: PreparedContextMeshAssignmentV1,
    tool_use_id: str,
    agent_id: str,
    final_message: str,
    transcript_raw: bytes,
    root_sentinel: str,
    parent_history_sentinel: str,
) -> NativeWorkerResultV1:
    """Attest one native rollout and parse its final through WorkerCandidateV1.

    The transcript, rather than hook payload metadata, owns effective identity,
    usage, the full ordered initial-message set, and the sole final assistant
    message.  This keeps callback spoofing from reaching the finishing queue.
    """

    if not all(
        isinstance(value, str) and value
        for value in (tool_use_id, agent_id, final_message, root_sentinel, parent_history_sentinel)
    ):
        raise ValueError("native worker correlation strings must be nonempty")
    if unicodedata.normalize("NFC", final_message) != final_message:
        # Candidate parsing remains typed and repairable, but the hook payload
        # still has to match the transcript byte-for-byte below.
        pass
    rows = _strict_rollout(transcript_raw)
    meta = rows[0].get("payload")
    if rows[0].get("type") != "session_meta" or not isinstance(meta, dict):
        raise ValueError("worker transcript lacks its session metadata")
    if (
        meta.get("id") != agent_id
        or meta.get("agent_role") != "worker"
        or meta.get("thread_source") != "subagent"
        or not isinstance(meta.get("session_id"), str)
        or not isinstance(meta.get("parent_thread_id"), str)
        or meta.get("session_id") != meta.get("parent_thread_id")
    ):
        raise ValueError("worker native agent/session identity differs")

    initial_payloads: list[dict[str, Any]] = []
    response_finals: list[str] = []
    event_finals: list[str] = []
    usage_rows: list[tuple[int, dict[str, int]]] = []
    completion_rows: list[int] = []
    turn_contexts: list[dict[str, Any]] = []
    output_started = False
    usage_fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    for index, row in enumerate(rows[1:], 1):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")
        if row.get("type") == "turn_context":
            turn_contexts.append(payload)
        if (
            not output_started
            and row.get("type") == "response_item"
            and payload_type == "message"
            and payload.get("role") in {"developer", "user", "system"}
        ):
            initial_payloads.append(payload)
            continue
        if row.get("type") == "event_msg" and payload_type == "agent_message":
            message = payload.get("message")
            if not isinstance(message, str):
                raise ValueError("worker event final is malformed")
            output_started = True
            event_finals.append(message)
        if (
            row.get("type") == "response_item"
            and payload_type == "message"
            and payload.get("role") == "assistant"
        ):
            _role, message, _raw = _message(payload)
            output_started = True
            response_finals.append(message)
        if row.get("type") == "event_msg" and payload_type == "token_count":
            info = payload.get("info")
            total = info.get("total_token_usage") if isinstance(info, dict) else None
            if not isinstance(total, dict):
                raise ValueError("worker transcript usage is malformed")
            counts: dict[str, int] = {}
            for field in usage_fields:
                value = total.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"worker transcript {field} is invalid")
                counts[field] = value
            if (
                counts["cached_input_tokens"] > counts["input_tokens"]
                or counts["reasoning_output_tokens"] > counts["output_tokens"]
                or counts["total_tokens"] != counts["input_tokens"] + counts["output_tokens"]
            ):
                raise ValueError("worker transcript usage arithmetic differs")
            usage_rows.append((index, counts))
        if row.get("type") == "event_msg" and payload_type == "task_complete":
            completion_rows.append(index)
        visible_types = {str(row.get("type", "")).lower(), str(payload_type or "").lower()}
        if any(
            marker in value
            for value in visible_types
            for marker in ("error", "failed", "aborted", "cancelled")
        ):
            raise ValueError("worker transcript contains a visible failure")

    if len(initial_payloads) != 4:
        raise ValueError("worker initial-message cardinality differs")
    initial: list[WorkerInitialMessageV1] = []
    initial_texts: list[str] = []
    for index, payload in enumerate(initial_payloads):
        role, text, raw = _message(payload)
        initial_texts.append(text)
        initial.append(WorkerInitialMessageV1(index, cast(Any, role), _sha(raw), len(raw)))
    expected_roles = ("developer", "user", "developer", "user")
    if tuple(row.role for row in initial) != expected_roles:
        raise ValueError("worker initial-message roles/order differ")
    if initial_texts[2] != _SUBAGENT_CONTEXT:
        raise ValueError("worker SubagentStart context differs")
    if initial_texts[3] != registered_assignment.worker_prompt:
        raise ValueError("worker source-blind assignment differs")
    for forbidden in (root_sentinel, parent_history_sentinel):
        if any(forbidden in text for text in initial_texts) or forbidden.encode() in transcript_raw:
            raise ValueError("worker inherited forbidden root context")

    if (
        len(event_finals) != 1
        or len(response_finals) != 1
        or event_finals[0] != final_message
        or response_finals[0] != final_message
    ):
        raise ValueError("worker final message differs from its native transcript")
    if not usage_rows or len(completion_rows) != 1:
        raise ValueError("worker transcript lacks one final usage/completion")
    for (_previous_index, previous), (_index, current) in zip(usage_rows, usage_rows[1:]):
        if any(current[field] < previous[field] for field in usage_fields):
            raise ValueError("worker cumulative usage regressed")
    final_usage_index, usage = usage_rows[-1]
    if completion_rows[0] <= final_usage_index:
        raise ValueError("worker final usage is not before task completion")
    if len(turn_contexts) != 1:
        raise ValueError("worker effective turn context cardinality differs")
    turn = turn_contexts[0]
    effective_model = turn.get("model")
    effective_reasoning = turn.get("effort")
    if effective_model != "gpt-5.6-luna" or effective_reasoning != "low":
        raise ValueError("worker effective model/reasoning differs")
    if meta.get("model_provider") != "openai":
        raise ValueError("worker effective provider differs")

    assignment = registered_assignment.assignment
    prepared = registered_assignment.prepared
    assignment_sha = registered_assignment.assignment_sha256
    message_rows = tuple(initial)
    context = WorkerContextAttestationV1(
        attempt_id=prepared.attempt.attempt_id,
        tool_use_id=tool_use_id,
        agent_id=agent_id,
        fork_context=False,
        assignment_sha256=assignment_sha,
        initial_messages=message_rows,
        expected_initial_set_sha256=_sha(
            canonical_json_bytes([row.as_json() for row in message_rows])
        ),
        root_sentinel_sha256=_sha(root_sentinel.encode("utf-8")),
        parent_history_sha256=_sha(parent_history_sentinel.encode("utf-8")),
    )
    final_raw = final_message.encode("utf-8", errors="strict")
    candidate = parse_worker_candidate(
        final_raw,
        attempt_id=prepared.attempt.attempt_id,
        artifact_path=prepared.task.artifact_path,
    )
    evidence = WorkerEvidenceV1(
        tool_use_id=tool_use_id,
        attempt_id=prepared.attempt.attempt_id,
        agent_id=agent_id,
        task_id=prepared.task.task_id,
        arm=cast(Any, assignment.mode),
        final_message_sha256=_sha(final_raw),
        transcript_sha256=_sha(transcript_raw),
        context_attestation_sha256=_sha(context.canonical_bytes()),
        requested_provider="openai",
        requested_model="gpt-5.6-luna",
        requested_reasoning="low",
        requested_service_tier="priority",
        identity_attestation="native_partial",
        effective_provider="openai",
        effective_model="gpt-5.6-luna",
        effective_reasoning="low",
        effective_service_tier="unattested",
        input_tokens=usage["input_tokens"],
        cached_input_tokens=usage["cached_input_tokens"],
        output_tokens=usage["output_tokens"],
        reasoning_output_tokens=usage["reasoning_output_tokens"],
        provider_total_tokens=usage["total_tokens"],
    )
    return NativeWorkerResultV1(candidate, context, evidence, transcript_raw)


def render_worker_prompt(prepared: PreparedSolve, *, assignment_sha256: str) -> str:
    """Render the source-blind initial IMPLEMENT prompt for one native worker."""

    if prepared.specification is None:
        raise ValueError("ContextMesh worker prompt requires a concrete specification")
    payload = canonical_json_bytes(
        {
            "artifact_path": prepared.task.artifact_path,
            "assignment_sha256": assignment_sha256,
            "attempt_id": prepared.attempt.attempt_id,
            "branch": prepared.branch.value,
            "independent_tests_sha256": _sha(
                canonical_json_bytes({"tests": list(prepared.independent_tests), "v": 1})
            ),
            "specification": prepared.specification.as_json(),
            "task_id": prepared.task.task_id,
            "v": 1,
        }
    ).decode("utf-8")
    prompt = (
        "[ReasonRenderCoding ContextMesh IMPLEMENT v1]\n"
        "Implement only the concrete specification in the canonical payload below. "
        "The controller already consumed and sealed all task sources. Do not read files, "
        "run shell commands, call tools, or request more context. Return exactly one compact "
        "JSON object with keys artifact_path, attempt_id, source, v; no Markdown or prose.\n"
        + payload
    )
    raw = prompt.encode("utf-8", errors="strict")
    if len(raw) > 196_608:
        raise ValueError("ContextMesh worker prompt exceeds the dispatch framing bound")
    return prompt


class ContextMeshController:
    """Seal one strict assignment, journal prepare, and emit a source-blind worker prompt."""

    def __init__(
        self,
        *,
        attempts: AttemptRepository,
        model: ModelPort,
        retrieval: RetrievalPort,
        config: Config,
        target_root: Path,
        attempts_root: Path,
        route_id: str,
        round_id: str,
    ) -> None:
        if attempts.authority_id != getattr(retrieval, "authority_id", None):
            raise ValueError("ContextMesh retrieval and journal authority differ")
        if attempts.database_uuid != getattr(retrieval, "database_uuid", None):
            raise ValueError("ContextMesh retrieval and journal database differ")
        self.attempts = attempts
        self.model = model
        self.retrieval = retrieval
        self.config = config
        self.target_root = target_root.resolve(strict=True)
        self.attempts_root = attempts_root.absolute()
        self.route_id = route_id
        self.round_id = round_id
        self.attempts_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.attempts_root, 0o700)

    def reopen(self, attempt_id: str) -> PreparedContextMeshAssignmentV1:
        """Reopen a prepared split-phase assignment without any provider call."""

        registered = self.attempts.load_registered_input(attempt_id)
        try:
            assignment = parse_coding_assignment(registered.assignment)
        except (TypeError, ValueError) as exc:
            raise JournalConflict("registered coding assignment is invalid") from exc
        prepared_raw = self.attempts.repository.load_prepared(attempt_id)
        if prepared_raw is None:
            raise JournalStateError("ContextMesh attempt has no prepared authority")
        prepared = hydrate_prepared(
            prepared_raw,
            attempt=registered.attempt,
            input=registered.task_envelope,
            journal=self.attempts.repository,
        )
        assignment_sha = _sha(registered.assignment)
        prompt = render_worker_prompt(prepared, assignment_sha256=assignment_sha)
        ordinal = _native_implement_ordinal(mode=assignment.mode, branch=prepared.branch.value)
        current = self.attempts.prepare_native_worker(
            prepared.attempt,
            prompt=prompt,
            stage_ordinal=ordinal,
        )
        prepared = replace(prepared, attempt=current)
        return PreparedContextMeshAssignmentV1(
            prepared=prepared,
            assignment=assignment,
            assignment_sha256=assignment_sha,
            worker_prompt=prompt,
        )

    def prepare(
        self,
        assignment: CodingAssignmentV1,
        *,
        tool_use_id: str,
        model_factory: Callable[[AttemptHandle, TaskEnvelopeV1], ModelPort] | None = None,
    ) -> PreparedContextMeshAssignmentV1:
        if not isinstance(assignment, CodingAssignmentV1):
            raise TypeError("assignment must be CodingAssignmentV1")
        assignment_raw = assignment.canonical_bytes()
        operation_key = contextmesh_operation_key(
            repository_id=self.attempts.database_uuid,
            route_id=self.route_id,
            round_id=self.round_id,
            tool_use_id=tool_use_id,
            task_id=assignment.task.task_id,
            assignment_sha256=_sha(assignment_raw),
        )
        existing = self.attempts.find_by_tool_use(
            owner_scope=self.config.owner_scope, tool_use_id=tool_use_id
        )
        if existing is not None:
            registered = self.attempts.load_registered_input(existing.attempt_id)
            if registered.assignment != assignment_raw:
                raise JournalConflict("tool_use_id is bound to a different coding assignment")
            envelope = registered.task_envelope
            attempt = registered.attempt
        else:
            operation_root = self.attempts_root / _sha(operation_key.encode("utf-8"))
            incoming = operation_root / "incoming"
            input_root = operation_root / "input"
            try:
                operation_root.mkdir(mode=0o700)
                incoming.mkdir(mode=0o700)
                source_ref: ArtifactRefV1 | None = None
                if assignment.source_path is not None:
                    source, source_mode = _read_confined(
                        self.target_root, assignment.source_path, cap=_MAX_SOURCE_BYTES
                    )
                    target = assignment.target_preimage
                    if (
                        target.sha256 != _sha(source)
                        or target.bytes != len(source)
                        or target.mode != source_mode
                    ):
                        raise ValueError("coding assignment source differs from target preimage")
                    _write(incoming / assignment.task.artifact_path, source, mode=source_mode)
                    source_ref = ArtifactRefV1(
                        _sha(source), len(source), assignment.task.artifact_path
                    )
                else:
                    _prove_absent(self.target_root, assignment.task.artifact_path)
                public, _public_mode = _read_confined(
                    self.target_root, assignment.public_test_path, cap=_MAX_SOURCE_BYTES
                )
                public_path = ".rrcv2/public-tests.v1.json"
                _write(incoming / public_path, public, mode=0o600)
                public_ref = ArtifactRefV1(_sha(public), len(public), public_path)
                oracle_ref: ArtifactRefV1 | None = None
                if assignment.oracle_test_path is not None:
                    oracle, _oracle_mode = _read_confined(
                        self.target_root, assignment.oracle_test_path, cap=_MAX_SOURCE_BYTES
                    )
                    oracle_path = ".rrcv2/oracle-tests.v1.json"
                    _write(incoming / oracle_path, oracle, mode=0o600)
                    oracle_ref = ArtifactRefV1(_sha(oracle), len(oracle), oracle_path)
                envelope = seal_task_input(
                    ReferencedTaskInputV1(
                        task=assignment.task,
                        sealed_root=incoming,
                        source_ref=source_ref,
                        public_test_ref=public_ref,
                        oracle_ref=oracle_ref,
                        target_preimage=assignment.target_preimage,
                    ),
                    input_root=input_root.absolute(),
                )
                attempt = begin_contextmesh_attempt(
                    envelope,
                    mode=ArmMode(assignment.mode),
                    cfg=self.config,
                    journal=self.attempts.repository,
                    operation_key=operation_key,
                )
                self.attempts.register_prepared_input(
                    attempt,
                    task_envelope=task_envelope_bytes(envelope),
                    input_root=input_root.absolute(),
                    target_root=self.target_root,
                    assignment=assignment_raw,
                    expected_tool_use_id=tool_use_id,
                )
            except BaseException:
                if (
                    self.attempts.find_by_tool_use(
                        owner_scope=self.config.owner_scope, tool_use_id=tool_use_id
                    )
                    is None
                ):
                    shutil.rmtree(operation_root, ignore_errors=True)
                raise
        reopen_task_inputs(envelope)
        provider_model = self.model if model_factory is None else model_factory(attempt, envelope)
        if attempt.state == "prepared":
            prepared_raw = self.attempts.repository.load_prepared(attempt.attempt_id)
            if prepared_raw is None:
                raise JournalStateError("prepared ContextMesh attempt has no prepared bytes")
            prepared = hydrate_prepared(
                prepared_raw,
                attempt=attempt,
                input=envelope,
                journal=self.attempts.repository,
            )
        elif attempt.state == "preparing":
            prepared = prepare_contextmesh(
                envelope,
                mode=ArmMode(assignment.mode),
                model=provider_model,
                retrieval=self.retrieval,
                cfg=self.config,
                journal=self.attempts.repository,
                operation_key=operation_key,
                attempt=attempt,
            )
        else:
            raise JournalStateError("ContextMesh assignment is no longer preparable")
        assignment_sha = _sha(assignment_raw)
        prompt = render_worker_prompt(prepared, assignment_sha256=assignment_sha)
        ordinal = _native_implement_ordinal(mode=assignment.mode, branch=prepared.branch.value)
        product_call_id_builder = getattr(provider_model, "product_call_id", None)
        product_authorizer = getattr(provider_model, "authorize_product_call", None)
        native_call_id: str | None = None
        if callable(product_call_id_builder) != callable(product_authorizer):
            raise TypeError("product model must provide both call-ID and authorization methods")
        if callable(product_call_id_builder):
            call_id_builder = cast(Callable[..., str], product_call_id_builder)
            authorize_call = cast(Callable[..., None], product_authorizer)
            ctx = RunContext(
                assignment.mode,
                prepared.task.task_id,
                self.config.owner_scope,
                cast(str | None, getattr(provider_model, "product_cell_id", None)),
            )
            native_call_id = call_id_builder(
                attempt=prepared.attempt,
                ctx=ctx,
                branch=prepared.branch.value,
                stage="implement",
                stage_ordinal=ordinal,
            )
            authorize_call(
                attempt=prepared.attempt,
                ctx=ctx,
                branch=prepared.branch.value,
                stage="implement",
                stage_ordinal=ordinal,
                call_id=native_call_id,
            )
        current = self.attempts.prepare_native_worker(
            prepared.attempt,
            prompt=prompt,
            stage_ordinal=ordinal,
            call_id=native_call_id,
        )
        prepared = replace(prepared, attempt=current)
        return PreparedContextMeshAssignmentV1(
            prepared=prepared,
            assignment=assignment,
            assignment_sha256=assignment_sha,
            worker_prompt=prompt,
        )
