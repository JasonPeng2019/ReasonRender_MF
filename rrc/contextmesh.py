"""Strict ContextMesh transport contracts for the canonical RRCv2 pipeline.

The types in this module contain transport evidence only.  They do not make a
verification or acceptance decision: those decisions remain owned by the
pipeline verifier and the transactional acceptance repository.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, TypeAlias, cast

from rrc.contract import StructuralShapeV1, TargetPreimageV1, Task, canonical_json_bytes

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_TASK_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_PRINTABLE_ID = re.compile(r"[!-~]{1,256}\Z")
_MODELS = frozenset({"gpt-5.5", "gpt-5.6-luna"})
_REASONING = frozenset({"low", "medium"})
_ROLES = frozenset({"system", "developer", "user", "assistant"})
_MAX_RECEIPT_BYTES = 2_000
_MAX_WAIT_BYTES = 40 * 1024
_MAX_RESULT_BYTES = 2_000
_MAX_SOURCE_BYTES = 1024 * 1024
_MAX_ASSIGNMENT_BYTES = 2 * 1024 * 1024
_ASSIGNMENT_PREFIX = "RRCV2_CODING_ASSIGNMENT_V1:"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hex64(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _identifier(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or _PRINTABLE_ID.fullmatch(value) is None
        or any(character in value for character in ("/", "\\", '"', "'"))
    ):
        raise ValueError(f"{name} must use the frozen printable identifier grammar")
    return value


def _task_id(value: object) -> str:
    if not isinstance(value, str) or _TASK_ID.fullmatch(value) is None:
        raise ValueError("task_id does not use the frozen identifier grammar")
    return value


def _nonnegative(value: object, *, name: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative non-bool integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} exceeds its frozen bound")
    return value


def _path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise ValueError("artifact path must be bounded nonempty UTF-8")
    if value.startswith("/") or "\\" in value or "\x00" in value or "\r" in value:
        raise ValueError("artifact path must be relative canonical POSIX")
    parts = PurePosixPath(value).parts
    if not parts or parts[0] == ".rrcv2" or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("artifact path must be relative canonical POSIX")
    if PurePosixPath(value).as_posix() != value:
        raise ValueError("artifact path must be relative canonical POSIX")
    return value


def _reference_path(value: object, *, name: str) -> str:
    try:
        return _path(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not a canonical repository path") from exc


def _canonical_object(
    raw: bytes, *, fields: set[str], name: str, limit: int = _MAX_WAIT_BYTES
) -> dict[str, object]:
    if not isinstance(raw, bytes) or not raw or len(raw) > limit:
        raise ValueError(f"{name} exceeds its canonical byte bound")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{name} is not strict JSON") from exc
    if not isinstance(value, dict) or set(value) != fields or canonical_json_bytes(value) != raw:
        raise ValueError(f"{name} schema or canonical encoding is invalid")
    return cast(dict[str, object], value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _target_from_json(value: object) -> TargetPreimageV1:
    if not isinstance(value, dict):
        raise ValueError("coding assignment target_preimage is invalid")
    kind = value.get("kind")
    if kind == "absent" and set(value) == {"kind", "mode", "path", "v"}:
        if value["v"] != 1:
            raise ValueError("target_preimage version is invalid")
        return TargetPreimageV1.absent(cast(str, value["path"]), cast(int, value["mode"]))
    if kind == "regular" and set(value) == {
        "bytes",
        "kind",
        "mode",
        "path",
        "sha256",
        "v",
    }:
        if value["v"] != 1:
            raise ValueError("target_preimage version is invalid")
        return TargetPreimageV1.regular(
            cast(str, value["path"]),
            cast(str, value["sha256"]),
            cast(int, value["bytes"]),
            cast(int, value["mode"]),
        )
    raise ValueError("ContextMesh target_preimage must be absent or regular")


def _shape_from_json(value: object) -> StructuralShapeV1 | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"arg_types", "arity", "fields"}:
        raise ValueError("coding assignment shape is invalid")
    arg_types = value["arg_types"]
    fields = value["fields"]
    if not isinstance(arg_types, list) or not isinstance(fields, list):
        raise ValueError("coding assignment shape is invalid")
    return StructuralShapeV1(
        tuple(cast(list[str], arg_types)),
        cast(int, value["arity"]),
        tuple(cast(list[str], fields)),
    )


@dataclass(frozen=True)
class CodingAssignmentV1:
    """Root-authored, source-referencing ContextMesh coding assignment."""

    mode: Literal["cold", "warm"]
    task: Task
    source_path: str | None
    public_test_path: str
    oracle_test_path: str | None
    owned_paths: tuple[str, ...]
    target_preimage: TargetPreimageV1
    kind: Literal["rrcv2_coding_assignment"] = "rrcv2_coding_assignment"
    v: int = 1

    def __post_init__(self) -> None:
        if self.mode not in {"cold", "warm"}:
            raise ValueError("coding assignment mode is invalid")
        if not isinstance(self.task, Task) or self.task.public_tests or self.task.oracle_tests:
            raise ValueError("coding assignment Task must use reference-owned tests")
        if self.source_path is not None:
            _reference_path(self.source_path, name="source_path")
        _reference_path(self.public_test_path, name="public_test_path")
        if self.oracle_test_path is not None:
            _reference_path(self.oracle_test_path, name="oracle_test_path")
        if not isinstance(self.owned_paths, tuple) or self.owned_paths != (
            self.task.artifact_path,
        ):
            raise ValueError("coding assignment must own exactly its artifact path")
        if not isinstance(self.target_preimage, TargetPreimageV1):
            raise TypeError("coding assignment target_preimage is invalid")
        if self.source_path is None:
            if (
                self.target_preimage.kind != "absent"
                or self.target_preimage.path != self.task.artifact_path
            ):
                raise ValueError("greenfield coding assignment requires matching absent target")
        elif (
            self.source_path != self.task.artifact_path
            or self.target_preimage.kind != "regular"
            or self.target_preimage.path != self.source_path
        ):
            raise ValueError("coding assignment source and regular target must match")
        if self.kind != "rrcv2_coding_assignment" or self.v != 1:
            raise ValueError("coding assignment kind/version is invalid")

    def as_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "mode": self.mode,
            "oracle_test_path": self.oracle_test_path,
            "owned_paths": list(self.owned_paths),
            "public_test_path": self.public_test_path,
            "source_path": self.source_path,
            "target_preimage": self.target_preimage.as_json(),
            "task": {
                "artifact_path": self.task.artifact_path,
                "family": self.task.family,
                "primary": self.task.primary,
                "searchable_public": self.task.searchable_public,
                "shape": None if self.task.shape is None else self.task.shape.as_json(),
                "slot_values": (
                    None if self.task.slot_values is None else dict(self.task.slot_values)
                ),
                "task_id": self.task.task_id,
                "text": self.task.text,
                "verification_profile": self.task.verification_profile,
            },
            "v": 1,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())

    def marker(self) -> str:
        return _ASSIGNMENT_PREFIX + " " + self.canonical_bytes().decode("utf-8")


def parse_coding_assignment(raw: bytes) -> CodingAssignmentV1:
    value = _canonical_object(
        raw,
        fields={
            "kind",
            "mode",
            "oracle_test_path",
            "owned_paths",
            "public_test_path",
            "source_path",
            "target_preimage",
            "task",
            "v",
        },
        name="coding assignment",
        limit=_MAX_ASSIGNMENT_BYTES,
    )
    task_value = value["task"]
    if not isinstance(task_value, dict) or set(task_value) != {
        "artifact_path",
        "family",
        "primary",
        "searchable_public",
        "shape",
        "slot_values",
        "task_id",
        "text",
        "verification_profile",
    }:
        raise ValueError("coding assignment Task schema is invalid")
    slot_value = task_value["slot_values"]
    if slot_value is not None and (
        not isinstance(slot_value, dict)
        or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in slot_value.items()
        )
    ):
        raise ValueError("coding assignment slot_values are invalid")
    owned = value["owned_paths"]
    if not isinstance(owned, list) or any(not isinstance(item, str) for item in owned):
        raise ValueError("coding assignment owned_paths are invalid")
    task = Task(
        task_id=cast(str, task_value["task_id"]),
        text=cast(str, task_value["text"]),
        family=cast(str | None, task_value["family"]),
        artifact_path=cast(str, task_value["artifact_path"]),
        searchable_public=cast(bool, task_value["searchable_public"]),
        verification_profile=cast(str, task_value["verification_profile"]),
        primary=cast(str | None, task_value["primary"]),
        shape=_shape_from_json(task_value["shape"]),
        slot_values=(
            None if slot_value is None else tuple(sorted(cast(dict[str, str], slot_value).items()))
        ),
    )
    result = CodingAssignmentV1(
        mode=cast(Literal["cold", "warm"], value["mode"]),
        task=task,
        source_path=cast(str | None, value["source_path"]),
        public_test_path=cast(str, value["public_test_path"]),
        oracle_test_path=cast(str | None, value["oracle_test_path"]),
        owned_paths=tuple(cast(list[str], owned)),
        target_preimage=_target_from_json(value["target_preimage"]),
        kind=cast(Literal["rrcv2_coding_assignment"], value["kind"]),
        v=cast(int, value["v"]),
    )
    if result.canonical_bytes() != raw:
        raise ValueError("coding assignment does not round-trip")
    return result


def coding_assignment_from_message(message: str) -> CodingAssignmentV1 | None:
    if not isinstance(message, str):
        raise TypeError("assignment message must be text")
    matches = [
        line[len(_ASSIGNMENT_PREFIX) :].lstrip(" ")
        for line in message.splitlines()
        if line.startswith(_ASSIGNMENT_PREFIX)
    ]
    if not matches:
        return None
    if (
        len(matches) != 1
        or not matches[0]
        or len(matches[0].encode("utf-8")) > _MAX_ASSIGNMENT_BYTES
    ):
        raise ValueError("assignment message must contain one bounded canonical marker")
    return parse_coding_assignment(matches[0].encode("utf-8", errors="strict"))


@dataclass(frozen=True)
class WorkerInitialMessageV1:
    index: int
    role: Literal["system", "developer", "user", "assistant"]
    sha256: str
    bytes: int

    def __post_init__(self) -> None:
        _nonnegative(self.index, name="initial message index", maximum=63)
        if self.role not in _ROLES:
            raise ValueError("initial message role is invalid")
        _hex64(self.sha256, name="initial message sha256")
        _nonnegative(self.bytes, name="initial message bytes", maximum=2 * 1024 * 1024)

    def as_json(self) -> dict[str, object]:
        return {
            "bytes": self.bytes,
            "index": self.index,
            "role": self.role,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class WorkerContextAttestationV1:
    attempt_id: str
    tool_use_id: str
    agent_id: str
    fork_context: Literal[False]
    assignment_sha256: str
    initial_messages: tuple[WorkerInitialMessageV1, ...]
    expected_initial_set_sha256: str
    root_sentinel_sha256: str
    parent_history_sha256: str
    validation: Literal["passed"] = "passed"
    v: int = 1

    def __post_init__(self) -> None:
        _hex64(self.attempt_id, name="attempt_id")
        _identifier(self.tool_use_id, name="tool_use_id")
        _identifier(self.agent_id, name="agent_id")
        if self.fork_context is not False:
            raise ValueError("worker context must use fork_context=false")
        for value, name in (
            (self.assignment_sha256, "assignment_sha256"),
            (self.expected_initial_set_sha256, "expected_initial_set_sha256"),
            (self.root_sentinel_sha256, "root_sentinel_sha256"),
            (self.parent_history_sha256, "parent_history_sha256"),
        ):
            _hex64(value, name=name)
        if (
            not isinstance(self.initial_messages, tuple)
            or not self.initial_messages
            or len(self.initial_messages) > 64
            or any(not isinstance(row, WorkerInitialMessageV1) for row in self.initial_messages)
        ):
            raise ValueError("initial_messages must be a bounded nonempty tuple")
        if tuple(row.index for row in self.initial_messages) != tuple(
            range(len(self.initial_messages))
        ):
            raise ValueError("initial message indexes must be contiguous from zero")
        if self.validation != "passed" or self.v != 1:
            raise ValueError("worker context validation/version is invalid")

    def as_json(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "assignment_sha256": self.assignment_sha256,
            "attempt_id": self.attempt_id,
            "expected_initial_set_sha256": self.expected_initial_set_sha256,
            "fork_context": False,
            "initial_messages": [row.as_json() for row in self.initial_messages],
            "parent_history_sha256": self.parent_history_sha256,
            "root_sentinel_sha256": self.root_sentinel_sha256,
            "tool_use_id": self.tool_use_id,
            "v": 1,
            "validation": "passed",
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())


def parse_worker_context_attestation(raw: bytes) -> WorkerContextAttestationV1:
    fields = {
        "agent_id",
        "assignment_sha256",
        "attempt_id",
        "expected_initial_set_sha256",
        "fork_context",
        "initial_messages",
        "parent_history_sha256",
        "root_sentinel_sha256",
        "tool_use_id",
        "v",
        "validation",
    }
    value = _canonical_object(raw, fields=fields, name="worker context attestation")
    messages = value["initial_messages"]
    if not isinstance(messages, list):
        raise ValueError("worker context initial_messages is invalid")
    parsed: list[WorkerInitialMessageV1] = []
    for item in messages:
        if not isinstance(item, dict) or set(item) != {"bytes", "index", "role", "sha256"}:
            raise ValueError("worker context message schema is invalid")
        parsed.append(
            WorkerInitialMessageV1(
                index=cast(int, item["index"]),
                role=cast(Literal["system", "developer", "user", "assistant"], item["role"]),
                sha256=cast(str, item["sha256"]),
                bytes=cast(int, item["bytes"]),
            )
        )
    result = WorkerContextAttestationV1(
        attempt_id=cast(str, value["attempt_id"]),
        tool_use_id=cast(str, value["tool_use_id"]),
        agent_id=cast(str, value["agent_id"]),
        fork_context=cast(Literal[False], value["fork_context"]),
        assignment_sha256=cast(str, value["assignment_sha256"]),
        initial_messages=tuple(parsed),
        expected_initial_set_sha256=cast(str, value["expected_initial_set_sha256"]),
        root_sentinel_sha256=cast(str, value["root_sentinel_sha256"]),
        parent_history_sha256=cast(str, value["parent_history_sha256"]),
        validation=cast(Literal["passed"], value["validation"]),
        v=cast(int, value["v"]),
    )
    if result.canonical_bytes() != raw:
        raise ValueError("worker context attestation does not round-trip")
    return result


@dataclass(frozen=True)
class WorkerEvidenceV1:
    tool_use_id: str
    attempt_id: str
    agent_id: str
    task_id: str
    arm: Literal["cold", "warm"]
    final_message_sha256: str
    transcript_sha256: str
    context_attestation_sha256: str
    requested_provider: Literal["openai"]
    requested_model: Literal["gpt-5.6-luna"]
    requested_reasoning: Literal["low"]
    requested_service_tier: Literal["priority"]
    identity_attestation: Literal["native_complete", "native_partial", "usage_only"]
    effective_provider: str
    effective_model: str
    effective_reasoning: str
    effective_service_tier: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    provider_total_tokens: int
    v: int = 1

    def __post_init__(self) -> None:
        _identifier(self.tool_use_id, name="tool_use_id")
        _hex64(self.attempt_id, name="attempt_id")
        _identifier(self.agent_id, name="agent_id")
        _task_id(self.task_id)
        if self.arm not in {"cold", "warm"}:
            raise ValueError("worker evidence arm is invalid")
        for value, name in (
            (self.final_message_sha256, "final_message_sha256"),
            (self.transcript_sha256, "transcript_sha256"),
            (self.context_attestation_sha256, "context_attestation_sha256"),
        ):
            _hex64(value, name=name)
        if (
            self.requested_provider != "openai"
            or self.requested_model != "gpt-5.6-luna"
            or self.requested_reasoning != "low"
            or self.requested_service_tier != "priority"
        ):
            raise ValueError("worker requested identity differs from the frozen surface")
        if self.identity_attestation not in {"native_complete", "native_partial", "usage_only"}:
            raise ValueError("worker identity_attestation is invalid")
        effective = (
            (self.effective_provider, {"openai", "unattested"}),
            (self.effective_model, _MODELS | {"unattested"}),
            (self.effective_reasoning, _REASONING | {"unattested"}),
            (self.effective_service_tier, {"priority", "unattested"}),
        )
        if any(value not in allowed for value, allowed in effective):
            raise ValueError("worker effective identity field is invalid")
        if self.identity_attestation == "usage_only" and any(
            value != "unattested" for value, _allowed in effective
        ):
            raise ValueError("usage-only worker evidence cannot claim effective identity")
        if self.identity_attestation == "native_complete" and "unattested" in {
            self.effective_provider,
            self.effective_model,
            self.effective_reasoning,
            self.effective_service_tier,
        }:
            raise ValueError("native-complete worker evidence requires every effective field")
        for value, name in (
            (self.input_tokens, "input_tokens"),
            (self.cached_input_tokens, "cached_input_tokens"),
            (self.output_tokens, "output_tokens"),
            (self.reasoning_output_tokens, "reasoning_output_tokens"),
            (self.provider_total_tokens, "provider_total_tokens"),
        ):
            _nonnegative(value, name=name)
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached worker input exceeds total input")
        if self.reasoning_output_tokens > self.output_tokens:
            raise ValueError("worker reasoning output exceeds output")
        if self.provider_total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("worker provider total must equal input plus output")
        if self.v != 1:
            raise ValueError("worker evidence version is invalid")

    def as_json(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "arm": self.arm,
            "attempt_id": self.attempt_id,
            "cached_input_tokens": self.cached_input_tokens,
            "context_attestation_sha256": self.context_attestation_sha256,
            "effective_model": self.effective_model,
            "effective_provider": self.effective_provider,
            "effective_reasoning": self.effective_reasoning,
            "effective_service_tier": self.effective_service_tier,
            "final_message_sha256": self.final_message_sha256,
            "identity_attestation": self.identity_attestation,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "provider_total_tokens": self.provider_total_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "requested_model": self.requested_model,
            "requested_provider": self.requested_provider,
            "requested_reasoning": self.requested_reasoning,
            "requested_service_tier": self.requested_service_tier,
            "task_id": self.task_id,
            "tool_use_id": self.tool_use_id,
            "transcript_sha256": self.transcript_sha256,
            "v": 1,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())


def parse_worker_evidence(raw: bytes) -> WorkerEvidenceV1:
    fields = {
        "agent_id",
        "arm",
        "attempt_id",
        "cached_input_tokens",
        "context_attestation_sha256",
        "effective_model",
        "effective_provider",
        "effective_reasoning",
        "effective_service_tier",
        "final_message_sha256",
        "identity_attestation",
        "input_tokens",
        "output_tokens",
        "provider_total_tokens",
        "reasoning_output_tokens",
        "requested_model",
        "requested_provider",
        "requested_reasoning",
        "requested_service_tier",
        "task_id",
        "tool_use_id",
        "transcript_sha256",
        "v",
    }
    value = _canonical_object(raw, fields=fields, name="worker evidence")
    result = WorkerEvidenceV1(**cast(dict[str, object], value))  # type: ignore[arg-type]
    if result.canonical_bytes() != raw:
        raise ValueError("worker evidence does not round-trip")
    return result


@dataclass(frozen=True)
class AcceptedReceiptPayloadV1:
    attempt_id: str
    accept_commit_id: str
    artifact_path: str
    source_sha256: str
    source_bytes: int
    verification_result_sha256: str
    v: int = 1

    def __post_init__(self) -> None:
        _hex64(self.attempt_id, name="attempt_id")
        _hex64(self.accept_commit_id, name="accept_commit_id")
        _path(self.artifact_path)
        _hex64(self.source_sha256, name="source_sha256")
        _nonnegative(self.source_bytes, name="source_bytes", maximum=_MAX_SOURCE_BYTES)
        _hex64(self.verification_result_sha256, name="verification_result_sha256")
        if self.v != 1:
            raise ValueError("receipt payload version is invalid")
        if len(self.canonical_bytes()) > _MAX_RECEIPT_BYTES:
            raise ValueError("receipt payload exceeds 2,000 bytes")

    def as_json(self) -> dict[str, object]:
        return {
            "accept_commit_id": self.accept_commit_id,
            "artifact_path": self.artifact_path,
            "attempt_id": self.attempt_id,
            "source_bytes": self.source_bytes,
            "source_sha256": self.source_sha256,
            "v": 1,
            "verification_result_sha256": self.verification_result_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())

    @property
    def receipt(self) -> str:
        return _sha(self.canonical_bytes())


@dataclass(frozen=True)
class ReceiptRecordV1:
    attempt_id: str
    receipt: str
    blob_path: str
    blob_sha256: str
    blob_bytes: int
    blob_mode: int
    artifact_record_sha256: str
    v: int = 1

    def __post_init__(self) -> None:
        _hex64(self.attempt_id, name="attempt_id")
        _hex64(self.receipt, name="receipt")
        if self.blob_path != f".rrcv2/receipts/{self.receipt}.v1.json":
            raise ValueError("receipt blob path is not canonical")
        if _hex64(self.blob_sha256, name="blob_sha256") != self.receipt:
            raise ValueError("receipt must equal blob_sha256")
        _nonnegative(self.blob_bytes, name="blob_bytes", maximum=_MAX_RECEIPT_BYTES)
        if self.blob_mode != 0o600:
            raise ValueError("receipt blob mode must be 0600")
        _hex64(self.artifact_record_sha256, name="artifact_record_sha256")
        if self.v != 1:
            raise ValueError("receipt record version is invalid")

    def as_json(self) -> dict[str, object]:
        return {
            "artifact_record_sha256": self.artifact_record_sha256,
            "attempt_id": self.attempt_id,
            "blob_bytes": self.blob_bytes,
            "blob_mode": self.blob_mode,
            "blob_path": self.blob_path,
            "blob_sha256": self.blob_sha256,
            "receipt": self.receipt,
            "v": 1,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())

    @classmethod
    def bind(
        cls, payload: AcceptedReceiptPayloadV1, *, artifact_record_sha256: str
    ) -> ReceiptRecordV1:
        if not isinstance(payload, AcceptedReceiptPayloadV1):
            raise TypeError("receipt payload must be AcceptedReceiptPayloadV1")
        raw = payload.canonical_bytes()
        receipt = _sha(raw)
        return cls(
            attempt_id=payload.attempt_id,
            receipt=receipt,
            blob_path=f".rrcv2/receipts/{receipt}.v1.json",
            blob_sha256=receipt,
            blob_bytes=len(raw),
            blob_mode=0o600,
            artifact_record_sha256=artifact_record_sha256,
        )


def parse_receipt_payload(raw: bytes) -> AcceptedReceiptPayloadV1:
    value = _canonical_object(
        raw,
        fields={
            "accept_commit_id",
            "artifact_path",
            "attempt_id",
            "source_bytes",
            "source_sha256",
            "v",
            "verification_result_sha256",
        },
        name="accepted receipt payload",
    )
    result = AcceptedReceiptPayloadV1(**cast(dict[str, object], value))  # type: ignore[arg-type]
    if result.canonical_bytes() != raw:
        raise ValueError("accepted receipt payload does not round-trip")
    return result


def parse_receipt_record(raw: bytes) -> ReceiptRecordV1:
    value = _canonical_object(
        raw,
        fields={
            "artifact_record_sha256",
            "attempt_id",
            "blob_bytes",
            "blob_mode",
            "blob_path",
            "blob_sha256",
            "receipt",
            "v",
        },
        name="receipt record",
    )
    result = ReceiptRecordV1(**cast(dict[str, object], value))  # type: ignore[arg-type]
    if result.canonical_bytes() != raw:
        raise ValueError("receipt record does not round-trip")
    return result


@dataclass(frozen=True)
class WaitVerifierV1:
    profile_sha256: str
    summary_sha256: str

    def __post_init__(self) -> None:
        _hex64(self.profile_sha256, name="profile_sha256")
        _hex64(self.summary_sha256, name="summary_sha256")

    def as_json(self) -> dict[str, str]:
        return {"profile_sha256": self.profile_sha256, "summary_sha256": self.summary_sha256}


@dataclass(frozen=True)
class RRCPendingTargetV1:
    agent_id: str
    attempt_id: str
    poll_after_ms: int
    kind: Literal["rrc_pending"] = "rrc_pending"

    def __post_init__(self) -> None:
        _identifier(self.agent_id, name="agent_id")
        _hex64(self.attempt_id, name="attempt_id")
        value = _nonnegative(self.poll_after_ms, name="poll_after_ms", maximum=15_000)
        if value < 1:
            raise ValueError("poll_after_ms must be at least one")

    def as_json(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "attempt_id": self.attempt_id,
            "kind": self.kind,
            "poll_after_ms": self.poll_after_ms,
        }


@dataclass(frozen=True)
class RRCAcceptedTargetV1:
    agent_id: str
    attempt_id: str
    receipt: str
    path: str
    sha256: str
    bytes: int
    verifier: WaitVerifierV1
    compression_saved: bool
    kind: Literal["rrc_accepted"] = "rrc_accepted"

    def __post_init__(self) -> None:
        _identifier(self.agent_id, name="agent_id")
        _hex64(self.attempt_id, name="attempt_id")
        _hex64(self.receipt, name="receipt")
        _path(self.path)
        _hex64(self.sha256, name="sha256")
        _nonnegative(self.bytes, name="bytes", maximum=_MAX_SOURCE_BYTES)
        if not isinstance(self.verifier, WaitVerifierV1):
            raise TypeError("accepted wait verifier is invalid")
        if not isinstance(self.compression_saved, bool):
            raise TypeError("compression_saved must be boolean")

    def as_json(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "attempt_id": self.attempt_id,
            "bytes": self.bytes,
            "compression_saved": self.compression_saved,
            "kind": self.kind,
            "path": self.path,
            "receipt": self.receipt,
            "sha256": self.sha256,
            "verifier": self.verifier.as_json(),
        }


@dataclass(frozen=True)
class RRCRejectedTargetV1:
    agent_id: str
    attempt_id: str
    reason_code: str
    reason_sha256: str
    kind: Literal["rrc_rejected"] = "rrc_rejected"

    def __post_init__(self) -> None:
        _identifier(self.agent_id, name="agent_id")
        _hex64(self.attempt_id, name="attempt_id")
        if _REASON.fullmatch(self.reason_code) is None:
            raise ValueError("wait rejection reason_code is invalid")
        _hex64(self.reason_sha256, name="reason_sha256")

    def as_json(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "attempt_id": self.attempt_id,
            "kind": self.kind,
            "reason_code": self.reason_code,
            "reason_sha256": self.reason_sha256,
        }


@dataclass(frozen=True)
class RRCNativeFallbackTargetV1:
    agent_id: str
    attempt_id: str
    reason_code: str
    reason_sha256: str
    fallback_event_sha256: str
    experiment_valid: Literal[False] = False
    kind: Literal["rrc_native_fallback"] = "rrc_native_fallback"

    def __post_init__(self) -> None:
        _identifier(self.agent_id, name="agent_id")
        _hex64(self.attempt_id, name="attempt_id")
        if _REASON.fullmatch(self.reason_code) is None:
            raise ValueError("fallback reason_code is invalid")
        _hex64(self.reason_sha256, name="reason_sha256")
        _hex64(self.fallback_event_sha256, name="fallback_event_sha256")
        if self.experiment_valid is not False:
            raise ValueError("native fallback is always experiment-invalid")

    def as_json(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "attempt_id": self.attempt_id,
            "experiment_valid": False,
            "fallback_event_sha256": self.fallback_event_sha256,
            "kind": self.kind,
            "reason_code": self.reason_code,
            "reason_sha256": self.reason_sha256,
        }


@dataclass(frozen=True)
class NonRRCTargetV1:
    agent_id: str
    native_status: Literal["pending", "completed", "failed", "timed_out"]
    native_payload_sha256: str
    native_payload: str
    kind: Literal["non_rrc"] = "non_rrc"

    def __post_init__(self) -> None:
        _identifier(self.agent_id, name="agent_id")
        if self.native_status not in {"pending", "completed", "failed", "timed_out"}:
            raise ValueError("native_status is invalid")
        raw = self.native_payload.encode("utf-8", errors="strict")
        if len(raw) > 1024 or "\x00" in self.native_payload or "\r" in self.native_payload:
            raise ValueError("native_payload is invalid")
        if _hex64(self.native_payload_sha256, name="native_payload_sha256") != _sha(raw):
            raise ValueError("native payload hash differs from its bytes")

    def as_json(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "kind": self.kind,
            "native_payload": self.native_payload,
            "native_payload_sha256": self.native_payload_sha256,
            "native_status": self.native_status,
        }


WaitTargetV1: TypeAlias = (
    RRCPendingTargetV1
    | RRCAcceptedTargetV1
    | RRCRejectedTargetV1
    | RRCNativeFallbackTargetV1
    | NonRRCTargetV1
)


def wait_id(round_id: str, targets: tuple[WaitTargetV1, ...]) -> str:
    if not isinstance(round_id, str) or _TASK_ID.fullmatch(round_id) is None:
        raise ValueError("round_id is invalid")
    rows: list[dict[str, object]] = []
    for target in sorted(targets, key=lambda item: item.agent_id):
        if isinstance(target, NonRRCTargetV1):
            rows.append({"agent_id": target.agent_id, "attempt_id": None, "kind": "non_rrc"})
        else:
            rows.append(
                {"agent_id": target.agent_id, "attempt_id": target.attempt_id, "kind": "rrc"}
            )
    return _sha(canonical_json_bytes({"round_id": round_id, "targets": rows, "v": 1}))


@dataclass(frozen=True)
class WaitEnvelopeV1:
    wait_id: str
    targets: tuple[WaitTargetV1, ...]
    v: int = 1

    def __post_init__(self) -> None:
        _hex64(self.wait_id, name="wait_id")
        if not isinstance(self.targets, tuple) or not 1 <= len(self.targets) <= 16:
            raise ValueError("wait targets must contain 1 through 16 rows")
        if any(
            not isinstance(
                target,
                (
                    RRCPendingTargetV1,
                    RRCAcceptedTargetV1,
                    RRCRejectedTargetV1,
                    RRCNativeFallbackTargetV1,
                    NonRRCTargetV1,
                ),
            )
            for target in self.targets
        ):
            raise TypeError("wait target has an unknown type")
        ids = tuple(target.agent_id for target in self.targets)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("wait target agent IDs must be sorted unique")
        if any(
            len(canonical_json_bytes(target.as_json())) > _MAX_RESULT_BYTES
            for target in self.targets
        ):
            raise ValueError("wait target exceeds 2,000 bytes")
        if self.v != 1 or len(self.canonical_bytes()) > _MAX_WAIT_BYTES:
            raise ValueError("wait envelope version or byte bound is invalid")

    def as_json(self) -> dict[str, object]:
        return {
            "targets": [target.as_json() for target in self.targets],
            "v": 1,
            "wait_id": self.wait_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())


def compression_saved(*, native_utf8_bytes: int, delivered_utf8_bytes: int) -> bool:
    native = _nonnegative(native_utf8_bytes, name="native_utf8_bytes")
    delivered = _nonnegative(delivered_utf8_bytes, name="delivered_utf8_bytes")
    return native > 0 and 100 * delivered < 65 * native


def build_wait_envelope(round_id: str, targets: tuple[WaitTargetV1, ...]) -> WaitEnvelopeV1:
    ordered = tuple(sorted(targets, key=lambda item: item.agent_id))
    return WaitEnvelopeV1(wait_id(round_id, ordered), ordered)


def validate_wait_id(round_id: str, envelope: WaitEnvelopeV1) -> None:
    if not isinstance(envelope, WaitEnvelopeV1):
        raise TypeError("envelope must be WaitEnvelopeV1")
    if wait_id(round_id, envelope.targets) != envelope.wait_id:
        raise ValueError("wait_id differs from its round/target discriminator preimage")


def parse_wait_envelope(raw: bytes) -> WaitEnvelopeV1:
    value = _canonical_object(
        raw,
        fields={"targets", "v", "wait_id"},
        name="wait envelope",
    )
    if value["v"] != 1 or not isinstance(value["targets"], list):
        raise ValueError("wait envelope version or targets are invalid")
    parsed: list[WaitTargetV1] = []
    for item in value["targets"]:
        if not isinstance(item, dict):
            raise ValueError("wait target must be an object")
        kind = item.get("kind")
        if kind == "rrc_pending" and set(item) == {
            "agent_id",
            "attempt_id",
            "kind",
            "poll_after_ms",
        }:
            parsed.append(
                RRCPendingTargetV1(
                    cast(str, item["agent_id"]),
                    cast(str, item["attempt_id"]),
                    cast(int, item["poll_after_ms"]),
                )
            )
        elif kind == "rrc_accepted" and set(item) == {
            "agent_id",
            "attempt_id",
            "bytes",
            "compression_saved",
            "kind",
            "path",
            "receipt",
            "sha256",
            "verifier",
        }:
            verifier = item["verifier"]
            if not isinstance(verifier, dict) or set(verifier) != {
                "profile_sha256",
                "summary_sha256",
            }:
                raise ValueError("accepted wait verifier schema is invalid")
            parsed.append(
                RRCAcceptedTargetV1(
                    cast(str, item["agent_id"]),
                    cast(str, item["attempt_id"]),
                    cast(str, item["receipt"]),
                    cast(str, item["path"]),
                    cast(str, item["sha256"]),
                    cast(int, item["bytes"]),
                    WaitVerifierV1(
                        cast(str, verifier["profile_sha256"]),
                        cast(str, verifier["summary_sha256"]),
                    ),
                    cast(bool, item["compression_saved"]),
                )
            )
        elif kind == "rrc_rejected" and set(item) == {
            "agent_id",
            "attempt_id",
            "kind",
            "reason_code",
            "reason_sha256",
        }:
            parsed.append(
                RRCRejectedTargetV1(
                    cast(str, item["agent_id"]),
                    cast(str, item["attempt_id"]),
                    cast(str, item["reason_code"]),
                    cast(str, item["reason_sha256"]),
                )
            )
        elif kind == "rrc_native_fallback" and set(item) == {
            "agent_id",
            "attempt_id",
            "experiment_valid",
            "fallback_event_sha256",
            "kind",
            "reason_code",
            "reason_sha256",
        }:
            parsed.append(
                RRCNativeFallbackTargetV1(
                    cast(str, item["agent_id"]),
                    cast(str, item["attempt_id"]),
                    cast(str, item["reason_code"]),
                    cast(str, item["reason_sha256"]),
                    cast(str, item["fallback_event_sha256"]),
                    cast(Literal[False], item["experiment_valid"]),
                )
            )
        elif kind == "non_rrc" and set(item) == {
            "agent_id",
            "kind",
            "native_payload",
            "native_payload_sha256",
            "native_status",
        }:
            parsed.append(
                NonRRCTargetV1(
                    cast(str, item["agent_id"]),
                    cast(
                        Literal["pending", "completed", "failed", "timed_out"],
                        item["native_status"],
                    ),
                    cast(str, item["native_payload_sha256"]),
                    cast(str, item["native_payload"]),
                )
            )
        else:
            raise ValueError("wait target schema is invalid")
    result = WaitEnvelopeV1(cast(str, value["wait_id"]), tuple(parsed), cast(int, value["v"]))
    if result.canonical_bytes() != raw:
        raise ValueError("wait envelope does not round-trip")
    return result
