"""Canonical journaled RRCv2 dispatcher and synchronous prepare/finish front end."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from rrc.contextmesh import AcceptedReceiptPayloadV1, ReceiptRecordV1
from rrc.contract import (
    ArmMode,
    BranchDecision,
    Candidate,
    Completion,
    Config,
    CostEventV1,
    ModelPort,
    ModelRole,
    ResolvedTaskMetadataV1,
    RetrievalPort,
    RunContext,
    SealedTaskMaterialsV1,
    SolveOutcome,
    Spec,
    StoreFailure,
    Task,
    TaskEnvelopeV1,
    Template,
    Usage,
    canonical_json_bytes,
    reopen_task_inputs,
    task_envelope_bytes,
)
from rrc.everos import build_dispatch, parse_target
from rrc.journal import (
    AcceptedCommitV1,
    AcceptedOutcomeRecordV1,
    AttemptHandle,
    CallRecordV1,
    JournalStateError,
    RejectedCommitV1,
    RejectedOutcomeRecordV1,
    SealedAttemptInputsV1,
    SQLiteRRCRepository,
    TerminalClaimV1,
    UsageRecordV1,
    parse_accepted_commit,
    parse_rejected_commit,
)
from rrc.pipeline.prompts import (
    code_artifact_prompt,
    independent_tests_prompt,
    metadata_fill_prompt,
    prime_prompt,
    spec_prompt,
)
from rrc.pipeline.sandbox import SealedDockerSandbox
from rrc.pipeline.stages import parse_spec
from rrc.pipeline.template import (
    TemplateError,
    derive_bindings,
    parse_template_bundle,
    render,
    resolve_template,
    retrieval_primary,
    signature_declaration_source,
    signature_symbols,
    template_bundle_bytes,
    templatize,
    tier_minus_one,
)
from rrc.pipeline.verify import (
    CodeArtifactV1,
    VerificationRunV1,
    VerificationTestsV1,
    code_artifact_bytes,
    score_oracle,
    verification_result_bytes,
    verify_candidate,
)
from rrc.retrieval import ProjectionError, case_document, projection_unavailable

_HEX64 = frozenset("0123456789abcdef")
_MODEL_POLICY = canonical_json_bytes(
    {
        "provider": "openai",
        "requested_service_tier": "priority",
        "small": {"model": "gpt-5.6-luna", "reasoning": "low"},
        "strong": {"model": "gpt-5.5", "reasoning": "medium"},
        "v": 1,
    }
)
_VERIFIER_POLICY = canonical_json_bytes({"profile_source": "rrcv2-verifier-image-lock.v2", "v": 1})


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_object(raw: bytes, *, name: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is not JSON") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise ValueError(f"{name} is not canonical JSON")
    return cast(dict[str, object], value)


@dataclass(frozen=True)
class WorkerCandidateV1:
    """Controller-authored bounded interpretation of one native/model final."""

    kind: Literal["code", "invalid_candidate"]
    final_message_sha256: str
    artifact: CodeArtifactV1 | None = None
    reason: str | None = None
    observed_utf8_bytes: int | None = None
    v: int = 1

    def __post_init__(self) -> None:
        if len(self.final_message_sha256) != 64 or not set(self.final_message_sha256) <= _HEX64:
            raise ValueError("candidate final-message hash must be lowercase SHA-256")
        if self.kind == "code":
            if (
                self.artifact is None
                or self.reason is not None
                or self.observed_utf8_bytes is not None
            ):
                raise ValueError("valid worker candidate fields are inconsistent")
            code_artifact_bytes(self.artifact)
        elif self.kind == "invalid_candidate":
            if self.artifact is not None or self.reason not in {
                "malformed_json",
                "schema",
                "non_nfc",
                "oversize",
                "artifact_path_mismatch",
                "source_invalid",
            }:
                raise ValueError("invalid worker candidate fields are inconsistent")
            if self.observed_utf8_bytes is not None and (
                isinstance(self.observed_utf8_bytes, bool)
                or not isinstance(self.observed_utf8_bytes, int)
                or self.observed_utf8_bytes < 0
            ):
                raise ValueError("candidate observed byte count is invalid")
        else:
            raise ValueError("unknown worker candidate kind")
        if self.v != 1:
            raise ValueError("unknown WorkerCandidate version")

    def as_json(self) -> dict[str, object]:
        return {
            "artifact": (
                None
                if self.artifact is None
                else {
                    "artifact_path": self.artifact.artifact_path,
                    "attempt_id": self.artifact.attempt_id,
                    "source": self.artifact.source,
                    "v": 1,
                }
            ),
            "final_message_sha256": self.final_message_sha256,
            "kind": self.kind,
            "observed_utf8_bytes": self.observed_utf8_bytes,
            "reason": self.reason,
            "v": 1,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())


def parse_worker_candidate_record(raw: bytes) -> WorkerCandidateV1:
    """Strictly reopen a controller-authored WorkerCandidateV1 record."""

    value = _json_object(raw, name="worker candidate")
    if (
        set(value)
        != {
            "artifact",
            "final_message_sha256",
            "kind",
            "observed_utf8_bytes",
            "reason",
            "v",
        }
        or value.get("v") != 1
    ):
        raise ValueError("worker candidate record schema is invalid")
    artifact_value = value.get("artifact")
    artifact: CodeArtifactV1 | None = None
    if artifact_value is not None:
        if not isinstance(artifact_value, dict) or set(artifact_value) != {
            "artifact_path",
            "attempt_id",
            "source",
            "v",
        }:
            raise ValueError("worker candidate artifact schema is invalid")
        artifact = CodeArtifactV1(
            attempt_id=cast(str, artifact_value.get("attempt_id")),
            artifact_path=cast(str, artifact_value.get("artifact_path")),
            source=cast(str, artifact_value.get("source")),
            v=cast(int, artifact_value.get("v")),
        )
    result = WorkerCandidateV1(
        kind=cast(Literal["code", "invalid_candidate"], value.get("kind")),
        final_message_sha256=cast(str, value.get("final_message_sha256")),
        artifact=artifact,
        reason=cast(str | None, value.get("reason")),
        observed_utf8_bytes=cast(int | None, value.get("observed_utf8_bytes")),
        v=cast(int, value.get("v")),
    )
    if result.canonical_bytes() != raw:
        raise ValueError("worker candidate record does not round-trip")
    return result


def parse_worker_candidate(
    final_message: str | bytes,
    *,
    attempt_id: str,
    artifact_path: str,
) -> WorkerCandidateV1:
    """Parse a bounded exact CodeArtifact without retaining an invalid body."""

    if isinstance(final_message, str):
        try:
            raw = final_message.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return WorkerCandidateV1("invalid_candidate", _sha(b""), reason="non_nfc")
    elif isinstance(final_message, bytes):
        raw = final_message
    else:
        raise TypeError("worker final_message must be str or bytes")
    digest = _sha(raw)
    observed = len(raw)
    if observed > 2 * 1024 * 1024:
        return WorkerCandidateV1(
            "invalid_candidate", digest, reason="oversize", observed_utf8_bytes=observed
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return WorkerCandidateV1("invalid_candidate", digest, reason="non_nfc")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return WorkerCandidateV1(
            "invalid_candidate", digest, reason="malformed_json", observed_utf8_bytes=observed
        )
    if not isinstance(value, dict) or set(value) != {"artifact_path", "attempt_id", "source", "v"}:
        return WorkerCandidateV1(
            "invalid_candidate", digest, reason="schema", observed_utf8_bytes=observed
        )
    if value.get("artifact_path") != artifact_path or value.get("attempt_id") != attempt_id:
        return WorkerCandidateV1(
            "invalid_candidate",
            digest,
            reason="artifact_path_mismatch",
            observed_utf8_bytes=observed,
        )
    try:
        artifact = CodeArtifactV1(
            attempt_id=cast(str, value["attempt_id"]),
            artifact_path=cast(str, value["artifact_path"]),
            source=cast(str, value["source"]),
            v=cast(int, value["v"]),
        )
        code_artifact_bytes(artifact)
    except (KeyError, TypeError, ValueError):
        return WorkerCandidateV1(
            "invalid_candidate", digest, reason="source_invalid", observed_utf8_bytes=observed
        )
    return WorkerCandidateV1("code", digest, artifact=artifact)


def parse_resolved_metadata(raw: str, task: Task) -> ResolvedTaskMetadataV1 | None:
    """Strictly parse one measured WARM general metadata hint."""

    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "authority",
            "family",
            "primary",
            "shape",
            "slot_values",
            "v",
        }
        or value.get("v") != 1
        or value.get("authority") != "small_model"
    ):
        return None
    shape_value = value.get("shape")
    slots_value = value.get("slot_values")
    if (
        not isinstance(shape_value, dict)
        or set(shape_value) != {"arg_types", "arity", "fields"}
        or not isinstance(shape_value.get("arg_types"), list)
        or not isinstance(shape_value.get("fields"), list)
        or not isinstance(slots_value, dict)
        or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in slots_value.items()
        )
    ):
        return None
    try:
        from rrc.contract import StructuralShapeV1

        resolved = ResolvedTaskMetadataV1(
            primary=cast(str, value["primary"]),
            family=cast(str | None, value["family"]),
            shape=StructuralShapeV1(
                tuple(cast(list[str], shape_value["arg_types"])),
                cast(int, shape_value["arity"]),
                tuple(cast(list[str], shape_value["fields"])),
            ),
            slot_values=tuple(sorted(cast(dict[str, str], slots_value).items())),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if (
        (task.primary is not None and resolved.primary != task.primary)
        or (task.family is not None and resolved.family != task.family)
        or (task.shape is not None and resolved.shape != task.shape)
        or (task.slot_values is not None and resolved.slot_values != task.slot_values)
    ):
        return None
    return resolved


@dataclass(frozen=True)
class PreparedSolve:
    """Trusted runtime value created only after a journaled prepare transition."""

    attempt: AttemptHandle
    task: Task
    materials: SealedTaskMaterialsV1
    branch: BranchDecision
    specification: Spec | None
    independent_tests: tuple[str, ...]
    template: Template | None
    cost_events: tuple[CostEventV1, ...]
    call_ids: tuple[str, ...]
    resolved_metadata: ResolvedTaskMetadataV1 | None = None
    v: int = 1

    def canonical_bytes(self) -> bytes:
        if self.specification is None:
            raise ValueError("direct attempts do not have PreparedSolve bytes")
        return canonical_json_bytes(
            {
                "attempt_id": self.attempt.attempt_id,
                "branch": self.branch.value,
                "call_ids": list(self.call_ids),
                "independent_tests": {"tests": list(self.independent_tests), "v": 1},
                "resolved_metadata": (
                    None if self.resolved_metadata is None else self.resolved_metadata.as_json()
                ),
                "specification": self.specification.as_json(),
                "task_id": self.task.task_id,
                "template_bundle": (
                    None
                    if self.template is None
                    else json.loads(template_bundle_bytes(self.template))
                ),
                "v": 1,
            }
        )


def hydrate_prepared(
    raw: bytes,
    *,
    attempt: AttemptHandle,
    input: TaskEnvelopeV1,
    journal: SQLiteRRCRepository,
) -> PreparedSolve:
    """Reopen one committed prepared record without repeating retrieval or model work."""

    if attempt.state not in {
        "prepared",
        "spawned",
        "stop_pending_bind",
        "submitted",
        "finishing",
    }:
        raise ValueError("prepared hydration requires a live split-phase state")
    value = _json_object(raw, name="prepared solve")
    if (
        set(value)
        != {
            "attempt_id",
            "branch",
            "call_ids",
            "independent_tests",
            "resolved_metadata",
            "specification",
            "task_id",
            "template_bundle",
            "v",
        }
        or value.get("v") != 1
    ):
        raise ValueError("prepared solve schema is invalid")
    if value.get("attempt_id") != attempt.attempt_id or value.get("task_id") != input.task.task_id:
        raise ValueError("prepared solve identity differs")
    try:
        branch = BranchDecision(cast(str, value["branch"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("prepared solve branch is invalid") from exc
    if branch not in {BranchDecision.MISS, BranchDecision.REUSE, BranchDecision.PRIME}:
        raise ValueError("prepared solve branch is invalid for Spec flow")
    resolved_value = value.get("resolved_metadata")
    resolved = None
    task = input.task
    if resolved_value is not None:
        if not isinstance(resolved_value, dict):
            raise ValueError("prepared resolved metadata is invalid")
        resolved = parse_resolved_metadata(
            canonical_json_bytes(resolved_value).decode("utf-8"),
            input.task,
        )
        if resolved is None:
            raise ValueError("prepared resolved metadata failed strict validation")
        task = replace(
            input.task,
            primary=resolved.primary,
            family=resolved.family,
            shape=resolved.shape,
            slot_values=resolved.slot_values,
        )
    specification_value = value.get("specification")
    if not isinstance(specification_value, dict):
        raise ValueError("prepared solve specification is invalid")
    specification = parse_spec(
        canonical_json_bytes(specification_value).decode("utf-8"),
        task,
        strict_primary=resolved is None,
    )
    if specification is None:
        raise ValueError("prepared solve specification failed strict validation")
    tests_value = value.get("independent_tests")
    if not isinstance(tests_value, dict):
        raise ValueError("prepared solve independent tests are invalid")
    independent = _parse_independent_tests(canonical_json_bytes(tests_value).decode("utf-8"))
    if independent is None:
        raise ValueError("prepared solve independent tests failed strict validation")
    call_ids_value = value.get("call_ids")
    if not isinstance(call_ids_value, list) or any(
        not isinstance(item, str) for item in call_ids_value
    ):
        raise ValueError("prepared solve call inventory is invalid")
    inventory = journal.load_call_inventory(attempt.attempt_id)
    expected_count = len(call_ids_value)
    expected_rows = inventory[:expected_count]
    extra_rows = inventory[expected_count:]
    expected_events: list[CostEventV1] = []
    expected_ids: list[str] = []
    for call_id, state, cost_raw in expected_rows:
        if state != "call_committed" or cost_raw is None:
            raise ValueError("prepared solve call inventory contains an ambiguous provider call")
        event, observed_id = _cost_from_bytes(cost_raw)
        if observed_id != call_id:
            raise ValueError("prepared solve cost event differs from its call identity")
        expected_events.append(event)
        expected_ids.append(call_id)
    worker_call_id = "call-" + _sha(
        canonical_json_bytes(
            {
                "attempt_id": attempt.attempt_id,
                "role": "native_worker",
                "stage": "implement",
                "v": 1,
            }
        )
    )
    if (
        tuple(call_ids_value) != tuple(expected_ids)
        or len(extra_rows) > 1
        or (extra_rows and extra_rows[0][0] != worker_call_id)
    ):
        raise ValueError("prepared solve call inventory differs from committed journal")
    template_value = value.get("template_bundle")
    template: Template | None = None
    if template_value is not None:
        if not isinstance(template_value, dict):
            raise ValueError("prepared template bundle is invalid")
        template = parse_template_bundle(canonical_json_bytes(template_value))
    materials = reopen_task_inputs(input)
    hydrated = PreparedSolve(
        attempt=attempt,
        task=task,
        materials=materials,
        branch=branch,
        specification=specification,
        independent_tests=independent,
        template=template,
        cost_events=tuple(expected_events),
        call_ids=tuple(expected_ids),
        resolved_metadata=resolved,
    )
    if hydrated.canonical_bytes() != raw:
        raise ValueError("prepared solve failed canonical roundtrip")
    return hydrated


@dataclass(frozen=True)
class _CallCompletion:
    completion: Completion
    cost_event: CostEventV1
    call_id: str
    attempt: AttemptHandle


@dataclass(frozen=True)
class _CacheRenderRejection:
    """Bounded deterministic evidence for one structurally exact cache rejection."""

    external_ref: str
    input_bytes: bytes
    output_bytes: bytes
    code: Literal["identifier_context_slot_invalid", "cache_render_invalid"]


def _completion_bytes(completion: Completion) -> bytes:
    return canonical_json_bytes(
        {
            "effective_provider": completion.effective_provider,
            "effective_reasoning": completion.effective_reasoning,
            "effective_service_tier": completion.effective_service_tier,
            "identity_attestation": completion.identity_attestation,
            "model": completion.model,
            "text": completion.text,
            "transcript_sha256": completion.transcript_sha256,
            "usage": {
                "cached_input_tokens": completion.usage.cached_input_tokens,
                "completion_tokens": completion.usage.completion_tokens,
                "prompt_tokens": completion.usage.prompt_tokens,
                "reasoning_output_tokens": completion.usage.reasoning_output_tokens,
                "total_tokens": completion.usage.total_tokens,
            },
            "v": 1,
        }
    )


def _completion_from_bytes(raw: bytes) -> Completion:
    value = _json_object(raw, name="committed call outcome")
    if (
        set(value)
        != {
            "effective_provider",
            "effective_reasoning",
            "effective_service_tier",
            "identity_attestation",
            "model",
            "text",
            "transcript_sha256",
            "usage",
            "v",
        }
        or value.get("v") != 1
    ):
        raise ValueError("committed call outcome schema is invalid")
    usage = value.get("usage")
    if not isinstance(usage, dict) or set(usage) != {
        "cached_input_tokens",
        "completion_tokens",
        "prompt_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    }:
        raise ValueError("committed call usage schema is invalid")
    return Completion(
        cast(str, value["text"]),
        Usage(
            cast(int, usage["prompt_tokens"]),
            cast(int, usage["completion_tokens"]),
            cast(int, usage["total_tokens"]),
            cast(int, usage["cached_input_tokens"]),
            cast(int, usage["reasoning_output_tokens"]),
        ),
        cast(str, value["model"]),
        cast(str | None, value["transcript_sha256"]),
        cast(str, value["identity_attestation"]),
        cast(str, value["effective_provider"]),
        cast(str, value["effective_reasoning"]),
        cast(str, value["effective_service_tier"]),
    )


def _cost_bytes(event: CostEventV1) -> bytes:
    return event.canonical_bytes()


def _cost_from_bytes(raw: bytes) -> tuple[CostEventV1, str]:
    value = _json_object(raw, name="committed cost event")
    expected = {
        "arm",
        "attempt_id",
        "cached_input_tokens",
        "cell_id",
        "cost_event_id",
        "effective_model",
        "effective_provider",
        "effective_reasoning",
        "effective_service_tier",
        "final_message_sha256",
        "identity_attestation",
        "input_tokens",
        "output_tokens",
        "prompt_sha256",
        "provider_total_tokens",
        "reasoning_output_tokens",
        "requested_model",
        "requested_provider",
        "requested_reasoning",
        "requested_service_tier",
        "stage",
        "stage_ordinal",
        "task_id",
        "transcript_sha256",
        "v",
    }
    if set(value) != expected or value.get("v") != 1:
        raise ValueError("committed cost event schema is invalid")
    event = CostEventV1(
        cost_event_id=cast(str, value["cost_event_id"]),
        cell_id=cast(str, value["cell_id"]),
        attempt_id=cast(str, value["attempt_id"]),
        arm=cast(str, value["arm"]),
        task_id=cast(str, value["task_id"]),
        stage=cast(str, value["stage"]),
        stage_ordinal=cast(int, value["stage_ordinal"]),
        prompt_sha256=cast(str, value["prompt_sha256"]),
        final_message_sha256=cast(str, value["final_message_sha256"]),
        transcript_sha256=cast(str, value["transcript_sha256"]),
        requested_provider=cast(str, value["requested_provider"]),
        requested_model=cast(str, value["requested_model"]),
        requested_reasoning=cast(str, value["requested_reasoning"]),
        requested_service_tier=cast(str, value["requested_service_tier"]),
        identity_attestation=cast(str, value["identity_attestation"]),
        effective_provider=cast(str, value["effective_provider"]),
        effective_model=cast(str, value["effective_model"]),
        effective_reasoning=cast(str, value["effective_reasoning"]),
        effective_service_tier=cast(str, value["effective_service_tier"]),
        input_tokens=cast(int, value["input_tokens"]),
        cached_input_tokens=cast(int, value["cached_input_tokens"]),
        output_tokens=cast(int, value["output_tokens"]),
        reasoning_output_tokens=cast(int, value["reasoning_output_tokens"]),
        provider_total_tokens=cast(int, value["provider_total_tokens"]),
    )
    if event.canonical_bytes() != raw:
        raise ValueError("committed cost event failed canonical roundtrip")
    return event, event.cost_event_id


_DIRECT_ORDINALS: dict[str, dict[str, int]] = {
    "baseline": {"baseline": 1},
    "cheap_alone": {"cheap_alone": 1},
    "cascade": {
        "cascade_cheap": 1,
        "cascade_strong": 4,
    },
}
_SPEC_ORDINALS: dict[tuple[str, str], dict[str, int]] = {
    ("cold", "miss"): {
        "spec": 1,
        "independent_tests": 2,
        "implement": 3,
        "repair_1": 4,
        "repair_2": 5,
        "fallback_spec": 6,
        "fallback_independent_tests": 7,
        "fallback_implement": 8,
    },
    ("warm", "miss"): {
        "metadata_fill": 1,
        "spec": 2,
        "independent_tests": 3,
        "implement": 4,
        "repair_1": 5,
        "repair_2": 6,
        "fallback_spec": 7,
        "fallback_independent_tests": 8,
        "fallback_implement": 9,
    },
    ("warm", "reuse"): {
        "metadata_fill": 1,
        "implement": 2,
        "repair_1": 3,
        "repair_2": 4,
        "fallback_spec": 5,
        "fallback_independent_tests": 6,
        "fallback_implement": 7,
    },
    ("warm", "prime"): {
        "metadata_fill": 1,
        "prime": 2,
        "independent_tests": 3,
        "implement": 4,
        "repair_1": 5,
        "repair_2": 6,
        "fallback_spec": 7,
        "fallback_independent_tests": 8,
        "fallback_implement": 9,
    },
}


def _stage_ordinal(*, arm: str, branch: BranchDecision, stage: str) -> int:
    """Return the frozen logical provider slot; aliases and unknown rows fail closed."""

    if branch is BranchDecision.DIRECT:
        table = _DIRECT_ORDINALS.get(arm)
    else:
        table = _SPEC_ORDINALS.get((arm, branch.value))
    if table is None or stage not in table:
        raise ValueError(f"stage {stage!r} is not legal for {arm}/{branch.value}")
    return table[stage]


def _journal_completion(
    *,
    model: ModelPort,
    role: ModelRole,
    prompt: str,
    ctx: RunContext,
    branch: str,
    stage: str,
    stage_ordinal: int,
    attempt: AttemptHandle,
    journal: SQLiteRRCRepository,
) -> _CallCompletion:
    call_id_builder = getattr(model, "product_call_id", None)
    call_authorizer = getattr(model, "authorize_product_call", None)
    if callable(call_id_builder) != callable(call_authorizer):
        raise TypeError("product model must provide both call-ID and authorization methods")
    if callable(call_id_builder):
        call_id = call_id_builder(
            attempt=attempt,
            ctx=ctx,
            branch=branch,
            stage=stage,
            stage_ordinal=stage_ordinal,
        )
        if not isinstance(call_id, str) or len(call_id) != 64 or not set(call_id) <= _HEX64:
            raise ValueError("product dispatch authorizer returned an invalid call ID")
    else:
        call_id = "call-" + _sha(
            canonical_json_bytes(
                {
                    "attempt_id": attempt.attempt_id,
                    "ordinal": stage_ordinal,
                    "stage": stage,
                    "v": 1,
                }
            )
        )
    committed = journal.load_committed_call(attempt.attempt_id, call_id)
    if committed is not None:
        completion = _completion_from_bytes(committed[0])
        event, observed_call_id = _cost_from_bytes(committed[1])
        if observed_call_id != call_id:
            raise ValueError("committed cost event call ID differs")
        return _CallCompletion(completion, event, call_id, journal.load_attempt(attempt.attempt_id))
    if callable(call_authorizer):
        call_authorizer(
            attempt=attempt,
            ctx=ctx,
            branch=branch,
            stage=stage,
            stage_ordinal=stage_ordinal,
            call_id=call_id,
        )
    prompt_raw = prompt.encode("utf-8", errors="strict")
    prompt_sha = _sha(prompt_raw)
    expected_model = "gpt-5.5" if role is ModelRole.STRONG else "gpt-5.6-luna"
    record = CallRecordV1(
        call_id=call_id,
        stage=stage,
        stage_ordinal=stage_ordinal,
        role="strong" if role is ModelRole.STRONG else "small",
        model=expected_model,
        settings_sha256=_sha(_MODEL_POLICY),
        prompt_sha256=prompt_sha,
        transcript_baseline_sha256=_sha(b""),
    )
    current = journal.prepare_call(
        attempt,
        record,
        expected_state=attempt.state,
        expected_generation=attempt.generation,
        expected_cursor=attempt.cursor,
    )
    launch = canonical_json_bytes(
        {"provider": model.provider, "role": role.value, "stage": stage, "v": 1}
    )
    current = journal.mark_call_started(
        current,
        call_id,
        launch,
        expected_state=current.state,
        expected_generation=current.generation,
        expected_cursor=current.cursor,
    )
    try:
        completion = model.complete(role, prompt, ctx, stage)
    except Exception as exc:
        raise JournalStateError(
            "provider call failed after launch and is ambiguous; replay is forbidden"
        ) from exc
    observed_transcript = canonical_json_bytes(
        {
            "final_message_sha256": _sha(completion.text.encode("utf-8", errors="strict")),
            "model": completion.model,
            "v": 1,
        }
    )
    transcript_sha = completion.transcript_sha256 or _sha(observed_transcript)
    current = journal.observe_call(
        current,
        call_id,
        observed_transcript,
        UsageRecordV1(
            completion.usage.prompt_tokens,
            completion.usage.completion_tokens,
            completion.usage.total_tokens,
            completion.usage.cached_input_tokens,
        ),
        expected_state=current.state,
        expected_generation=current.generation,
        expected_cursor=current.cursor,
    )
    requested_reasoning = (
        "medium" if role is ModelRole.STRONG and stage in {"baseline", "cascade_strong"} else "low"
    )
    effective_identity_attested = completion.identity_attestation == "native_complete"
    event = CostEventV1(
        cost_event_id=call_id,
        cell_id=ctx.cell_id or ("sync-" + attempt.attempt_id[:32]),
        attempt_id=attempt.attempt_id,
        arm=ctx.arm,
        task_id=ctx.task_id,
        stage=stage,
        stage_ordinal=stage_ordinal,
        prompt_sha256=prompt_sha,
        final_message_sha256=_sha(completion.text.encode("utf-8", errors="strict")),
        transcript_sha256=transcript_sha,
        requested_provider="openai",
        requested_model=expected_model,
        requested_reasoning=requested_reasoning,
        requested_service_tier="priority",
        identity_attestation=completion.identity_attestation,
        effective_provider=(
            completion.effective_provider if effective_identity_attested else "unattested"
        ),
        effective_model=completion.model if effective_identity_attested else "unattested",
        effective_reasoning=(
            completion.effective_reasoning if effective_identity_attested else "unattested"
        ),
        effective_service_tier=(
            completion.effective_service_tier if effective_identity_attested else "unattested"
        ),
        input_tokens=completion.usage.prompt_tokens,
        cached_input_tokens=completion.usage.cached_input_tokens,
        output_tokens=completion.usage.completion_tokens,
        reasoning_output_tokens=completion.usage.reasoning_output_tokens,
        provider_total_tokens=completion.usage.total_tokens,
    )
    current = journal.commit_call(
        current,
        call_id,
        outcome=_completion_bytes(completion),
        cost_event=_cost_bytes(event),
        expected_state=current.state,
        expected_generation=current.generation,
        expected_cursor=current.cursor,
    )
    return _CallCompletion(completion, event, call_id, current)


def _parse_independent_tests(raw: str) -> tuple[str, ...] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or set(value) != {"tests", "v"} or value.get("v") != 1:
        return None
    tests = value.get("tests")
    if (
        not isinstance(tests, list)
        or not tests
        or len(tests) > 64
        or any(not isinstance(test, str) or not test for test in tests)
        or len(set(tests)) != len(tests)
    ):
        return None
    return tuple(cast(list[str], tests))


def _load_bundle(retrieval: RetrievalPort, candidate: Candidate) -> Template | None:
    try:
        value = retrieval.get_template(candidate.external_ref)
        if isinstance(value, Template):
            bundle = value
        elif isinstance(value, bytes):
            bundle = parse_template_bundle(value)
        else:
            return None
        return bundle if bundle.external_ref == candidate.external_ref else None
    except (AttributeError, TemplateError, TypeError, ValueError):
        return None


def _select_warm_branch(
    task: Task,
    retrieval: RetrievalPort,
    cfg: Config,
) -> tuple[
    BranchDecision,
    Spec | None,
    tuple[str, ...],
    Template | None,
    tuple[Spec, ...],
    _CacheRenderRejection | None,
]:
    neighbours: list[Spec] = []
    for candidate in retrieval.retrieve(task, cfg)[: cfg.top_k]:
        if not isinstance(candidate, Candidate) or candidate.score < cfg.tau_floor:
            continue
        bundle = _load_bundle(retrieval, candidate)
        if bundle is None:
            continue
        classifier = getattr(retrieval, "classify", None)
        classification = classifier(task, candidate.external_ref) if callable(classifier) else None
        if classification == "miss":
            continue
        if classification == "near":
            neighbours.append(bundle.spec_template)
            continue
        render_input = canonical_json_bytes(
            {
                "slot_values": dict(task.slot_values or ()),
                "task_id": task.task_id,
                "template_external_ref": bundle.external_ref,
                "v": 1,
            }
        )
        try:
            _rendered_candidate, independent = render(bundle, dict(task.slot_values or ()))
        except (TemplateError, TypeError, ValueError) as error:
            if classification == "exact":
                message = str(error)
                code: Literal["identifier_context_slot_invalid", "cache_render_invalid"] = (
                    "identifier_context_slot_invalid"
                    if message == "identifier-context slot value is invalid"
                    else "cache_render_invalid"
                )
                render_output = canonical_json_bytes(
                    {
                        "code": code,
                        "error_sha256": _sha(message.encode("utf-8", errors="strict")),
                        "passed": False,
                        "v": 1,
                    }
                )
                return (
                    BranchDecision.MISS,
                    None,
                    (),
                    None,
                    (),
                    _CacheRenderRejection(
                        bundle.external_ref,
                        render_input,
                        render_output,
                        code,
                    ),
                )
            continue
        rendered = resolve_template(bundle, task)
        if rendered is not None:
            return BranchDecision.REUSE, rendered, independent, bundle, (), None
        if classification == "exact":
            render_output = canonical_json_bytes(
                {
                    "code": "cache_render_invalid",
                    "error_sha256": _sha(b"rendered exact template failed task validation"),
                    "passed": False,
                    "v": 1,
                }
            )
            return (
                BranchDecision.MISS,
                None,
                (),
                None,
                (),
                _CacheRenderRejection(
                    bundle.external_ref,
                    render_input,
                    render_output,
                    "cache_render_invalid",
                ),
            )
        if classification in {None, "near"}:
            neighbours.append(bundle.spec_template)
    if neighbours and task.family is not None and cfg.prefer_prime_on_shape_diff:
        return BranchDecision.PRIME, None, (), None, tuple(neighbours[:2]), None
    return BranchDecision.MISS, None, (), None, (), None


def _run_verifier(
    *,
    attempt_id: str,
    task: Task,
    source: str,
    tests: VerificationTestsV1,
    specification: Spec | None,
) -> VerificationRunV1:
    sandbox = SealedDockerSandbox(Path(__file__).resolve().parents[2])
    synthetic_target = None
    if task.verification_profile == "rrcv2_synthetic_v1" and task.slot_values is not None:
        synthetic_target = dict(task.slot_values).get("function")
    return verify_candidate(
        attempt_id=attempt_id,
        verification_profile=task.verification_profile,  # type: ignore[arg-type]
        artifact_path=task.artifact_path,
        source=source,
        test_suite=tests,
        sandbox=sandbox,
        signature_source=(
            None if specification is None else signature_declaration_source(specification.signature)
        ),
        spec_driven=specification is not None,
        synthetic_target=synthetic_target,
    )


def _score_hidden_oracle(
    *,
    artifact: CodeArtifactV1,
    task: Task,
    oracle_tests: tuple[str, ...],
    specification: Spec | None,
) -> bool | None:
    if not oracle_tests:
        return None
    sandbox = SealedDockerSandbox(Path(__file__).resolve().parents[2])
    target = None if task.slot_values is None else dict(task.slot_values).get("function")
    return score_oracle(
        artifact=artifact,
        verification_profile=task.verification_profile,  # type: ignore[arg-type]
        oracle_tests=oracle_tests,
        sandbox=sandbox,
        synthetic_target=target,
        signature_source=(
            None if specification is None else signature_declaration_source(specification.signature)
        ),
    ).passed


def _begin(
    input: TaskEnvelopeV1,
    *,
    mode: ArmMode,
    cfg: Config,
    journal: SQLiteRRCRepository,
    operation_key: str,
    transport: Literal["sync", "contextmesh"] = "sync",
) -> AttemptHandle:
    envelope_sha = _sha(task_envelope_bytes(input))
    sealed = SealedAttemptInputsV1(
        task_envelope_sha256=envelope_sha,
        mode=mode.value,  # type: ignore[arg-type]
        flow_kind=(
            "direct"
            if mode in {ArmMode.BASELINE, ArmMode.CHEAP_ALONE, ArmMode.CASCADE}
            else "spec_pipeline"
        ),  # type: ignore[arg-type]
        transport=transport,
        config_sha256=_sha(cfg.canonical_bytes()),
        model_policy_sha256=_sha(_MODEL_POLICY),
        verifier_policy_sha256=_sha(_VERIFIER_POLICY),
    )
    return journal.begin_attempt(cfg.owner_scope, operation_key, sealed)


def prepare_contextmesh(
    input: TaskEnvelopeV1,
    *,
    mode: ArmMode,
    model: ModelPort,
    retrieval: RetrievalPort,
    cfg: Config,
    journal: SQLiteRRCRepository,
    operation_key: str,
    attempt: AttemptHandle | None = None,
) -> PreparedSolve:
    """Begin or reopen one split-phase ContextMesh COLD/WARM preparation."""

    if mode not in {ArmMode.COLD, ArmMode.WARM}:
        raise ValueError("ContextMesh preparation is available only for COLD/WARM")
    reopen_task_inputs(input)
    if attempt is None:
        attempt = begin_contextmesh_attempt(
            input,
            mode=mode,
            cfg=cfg,
            journal=journal,
            operation_key=operation_key,
        )
    if attempt.state == "prepared":
        raw = journal.load_prepared(attempt.attempt_id)
        if raw is None:
            raise JournalStateError("prepared ContextMesh attempt has no prepared authority")
        return hydrate_prepared(raw, attempt=attempt, input=input, journal=journal)
    if attempt.state != "preparing":
        raise JournalStateError("ContextMesh attempt is not available for preparation")
    return prepare(
        input,
        mode=mode,
        model=model,
        retrieval=retrieval,
        cfg=cfg,
        attempt=attempt,
        journal=journal,
    )


def begin_contextmesh_attempt(
    input: TaskEnvelopeV1,
    *,
    mode: ArmMode,
    cfg: Config,
    journal: SQLiteRRCRepository,
    operation_key: str,
) -> AttemptHandle:
    """Seal the split-phase attempt identity before preparation does any work."""

    if mode not in {ArmMode.COLD, ArmMode.WARM}:
        raise ValueError("ContextMesh attempts are available only for COLD/WARM")
    reopen_task_inputs(input)
    return _begin(
        input,
        mode=mode,
        cfg=cfg,
        journal=journal,
        operation_key=operation_key,
        transport="contextmesh",
    )


def _require_ports(
    *,
    mode: ArmMode,
    retrieval: RetrievalPort,
    journal: SQLiteRRCRepository,
    acceptance: SQLiteRRCRepository,
) -> None:
    if (
        journal.authority_id != acceptance.authority_id
        or journal.database_uuid != acceptance.database_uuid
    ):
        raise ValueError("journal and acceptance must be the same repository authority")
    if mode is ArmMode.WARM:
        if (
            getattr(retrieval, "authority_id", None) != journal.authority_id
            or getattr(retrieval, "database_uuid", None) != journal.database_uuid
            or not getattr(acceptance, "atomic_warm", False)
        ):
            raise ValueError("WARM retrieval/journal/acceptance authority mismatch")


def prepare(
    input: TaskEnvelopeV1,
    *,
    mode: ArmMode,
    model: ModelPort,
    retrieval: RetrievalPort,
    cfg: Config,
    attempt: AttemptHandle,
    journal: SQLiteRRCRepository,
) -> PreparedSolve:
    """Journal retrieval/SPEC/PRIME/test generation and seal one prepared solve."""

    if mode not in {ArmMode.COLD, ArmMode.WARM}:
        raise ValueError("prepare is available only for COLD/WARM")
    materials = reopen_task_inputs(input)
    task = input.task
    model_cell_id = getattr(model, "product_cell_id", None)
    ctx = RunContext(
        mode.value,
        task.task_id,
        cfg.owner_scope,
        model_cell_id if isinstance(model_cell_id, str) else None,
    )
    events: list[CostEventV1] = []
    call_ids: list[str] = []
    branch = BranchDecision.MISS
    specification: Spec | None = None
    independent: tuple[str, ...] = ()
    template: Template | None = None
    neighbours: tuple[Spec, ...] = ()
    cache_rejection: _CacheRenderRejection | None = None
    current = attempt
    resolved_metadata: ResolvedTaskMetadataV1 | None = None
    metadata_failed = False
    if (
        mode is ArmMode.WARM
        and task.verification_profile == "rrcv2_general_v1"
        and (task.shape is None or task.slot_values is None)
    ):
        called = _journal_completion(
            model=model,
            role=ModelRole.SMALL,
            prompt=metadata_fill_prompt(task),
            ctx=ctx,
            branch="pre_retrieval",
            stage="metadata_fill",
            stage_ordinal=_stage_ordinal(
                arm=mode.value, branch=BranchDecision.MISS, stage="metadata_fill"
            ),
            attempt=current,
            journal=journal,
        )
        current = called.attempt
        events.append(called.cost_event)
        call_ids.append(called.call_id)
        resolved_metadata = parse_resolved_metadata(called.completion.text, task)
        if resolved_metadata is None:
            metadata_failed = True
        else:
            task = replace(
                task,
                primary=resolved_metadata.primary,
                family=resolved_metadata.family,
                shape=resolved_metadata.shape,
                slot_values=resolved_metadata.slot_values,
            )
    if mode is ArmMode.WARM:
        if not metadata_failed:
            (
                branch,
                specification,
                independent,
                template,
                neighbours,
                cache_rejection,
            ) = _select_warm_branch(task, retrieval, cfg)
            projection_failure = getattr(retrieval, "projection_failure", None)
            projection_code = projection_failure(task) if callable(projection_failure) else None
            if projection_code is not None:
                projection_input = task.text.encode("utf-8", errors="strict")
                projection, projection_evidence = projection_unavailable(
                    attempt_id=current.attempt_id,
                    phase="query",
                    code=cast(str, projection_code),
                    input_bytes=projection_input,
                )
                journal.persist_projection_unavailable(
                    projection.canonical_bytes(),
                    input_bytes=projection_input,
                    evidence=projection_evidence,
                )
        if metadata_failed:
            current = journal.record_deterministic(
                current,
                "metadata_fill_validation",
                call_ids[-1].removeprefix("call-"),
                _sha(canonical_json_bytes({"passed": False, "v": 1})),
                expected_state=current.state,
                expected_generation=current.generation,
                expected_cursor=current.cursor,
            )
        else:
            retrieval_input = canonical_json_bytes(
                {
                    "config_sha256": _sha(cfg.canonical_bytes()),
                    "task_envelope_sha256": _sha(task_envelope_bytes(input)),
                    "v": 1,
                }
            )
            retrieval_output = canonical_json_bytes(
                {
                    "branch": branch.value,
                    "cache_render_rejection": (
                        None
                        if cache_rejection is None
                        else {
                            "code": cache_rejection.code,
                            "output_sha256": _sha(cache_rejection.output_bytes),
                            "template_external_ref": cache_rejection.external_ref,
                        }
                    ),
                    "neighbours": [item.as_json() for item in neighbours],
                    "template_external_ref": (None if template is None else template.external_ref),
                    "v": 1,
                }
            )
            current = journal.record_deterministic(
                current,
                "retrieval",
                _sha(retrieval_input),
                _sha(retrieval_output),
                expected_state=current.state,
                expected_generation=current.generation,
                expected_cursor=current.cursor,
            )
            if cache_rejection is not None:
                current = journal.record_deterministic(
                    current,
                    "cache_render_rejection",
                    _sha(cache_rejection.input_bytes),
                    _sha(cache_rejection.output_bytes),
                    expected_state=current.state,
                    expected_generation=current.generation,
                    expected_cursor=current.cursor,
                )
                tier_input = canonical_json_bytes(
                    {
                        "cache_render_rejection_sha256": _sha(cache_rejection.output_bytes),
                        "specification": None,
                        "task_id": task.task_id,
                        "v": 1,
                    }
                )
                tier_output = canonical_json_bytes(
                    {
                        "passed": False,
                        "rejection_code": cache_rejection.code,
                        "v": 1,
                    }
                )
                current = journal.record_deterministic(
                    current,
                    "tier_minus_one",
                    _sha(tier_input),
                    _sha(tier_output),
                    expected_state=current.state,
                    expected_generation=current.generation,
                    expected_cursor=current.cursor,
                )
        if branch is BranchDecision.REUSE:
            tier_input = canonical_json_bytes(
                {
                    "specification": None if specification is None else specification.as_json(),
                    "task_id": task.task_id,
                    "v": 1,
                }
            )
            tier_output = canonical_json_bytes({"passed": specification is not None, "v": 1})
            current = journal.record_deterministic(
                current,
                "tier_minus_one",
                _sha(tier_input),
                _sha(tier_output),
                expected_state=current.state,
                expected_generation=current.generation,
                expected_cursor=current.cursor,
            )
    if branch is BranchDecision.PRIME:
        called = _journal_completion(
            model=model,
            role=ModelRole.SMALL,
            prompt=prime_prompt(task, neighbours),
            ctx=ctx,
            branch=BranchDecision.PRIME.value,
            stage="prime",
            stage_ordinal=_stage_ordinal(
                arm=mode.value, branch=BranchDecision.PRIME, stage="prime"
            ),
            attempt=current,
            journal=journal,
        )
        current = called.attempt
        events.append(called.cost_event)
        call_ids.append(called.call_id)
        specification = parse_spec(
            called.completion.text,
            task,
            strict_primary=resolved_metadata is None,
        )
        tier_passed = specification is not None and tier_minus_one(specification, task)
        tier_input = canonical_json_bytes(
            {
                "specification": None if specification is None else specification.as_json(),
                "task_id": task.task_id,
                "v": 1,
            }
        )
        current = journal.record_deterministic(
            current,
            "tier_minus_one",
            _sha(tier_input),
            _sha(canonical_json_bytes({"passed": tier_passed, "v": 1})),
            expected_state=current.state,
            expected_generation=current.generation,
            expected_cursor=current.cursor,
        )
        if not tier_passed:
            branch = BranchDecision.MISS
    if branch is BranchDecision.MISS:
        called = _journal_completion(
            model=model,
            role=ModelRole.STRONG,
            prompt=spec_prompt(task, materials.source),
            ctx=ctx,
            branch=branch.value,
            stage="spec",
            stage_ordinal=_stage_ordinal(arm=mode.value, branch=branch, stage="spec"),
            attempt=current,
            journal=journal,
        )
        current = called.attempt
        events.append(called.cost_event)
        call_ids.append(called.call_id)
        specification = parse_spec(
            called.completion.text,
            task,
            strict_primary=resolved_metadata is None,
        )
    if specification is None:
        raise ValueError("SPEC/PRIME output failed strict validation")
    if branch is not BranchDecision.REUSE:
        called = _journal_completion(
            model=model,
            role=ModelRole.SMALL,
            prompt=independent_tests_prompt(task, specification),
            ctx=ctx,
            branch=branch.value,
            stage="independent_tests",
            stage_ordinal=_stage_ordinal(arm=mode.value, branch=branch, stage="independent_tests"),
            attempt=current,
            journal=journal,
        )
        current = called.attempt
        events.append(called.cost_event)
        call_ids.append(called.call_id)
        parsed = _parse_independent_tests(called.completion.text)
        if parsed is None:
            raise ValueError("independent test output failed strict validation")
        independent = parsed
    prepared = PreparedSolve(
        attempt=current,
        task=task,
        materials=materials,
        branch=branch,
        specification=specification,
        independent_tests=independent,
        template=template,
        cost_events=tuple(events),
        call_ids=tuple(call_ids),
        resolved_metadata=resolved_metadata,
    )
    committed = journal.commit_prepared(
        current,
        prepared.canonical_bytes(),
        expected_state="preparing",
        expected_generation=current.generation,
        expected_cursor=current.cursor,
    )
    return replace(prepared, attempt=committed)


def _failure_excerpt(run: VerificationRunV1 | None, candidate: WorkerCandidateV1) -> str:
    if run is not None and run.repair_evidence is not None:
        return run.repair_evidence.excerpt
    if candidate.kind == "invalid_candidate":
        return "candidate_parse:" + cast(str, candidate.reason)
    return "public verification failed"


def _candidate_call(
    *,
    prepared: PreparedSolve,
    model: ModelPort,
    journal: SQLiteRRCRepository,
    ctx: RunContext,
    stage: str,
    stage_ordinal: int | None = None,
    role: ModelRole,
    source: str | None,
    current_code: str | None = None,
    failure: str | None = None,
    specification: Spec | None = None,
) -> tuple[WorkerCandidateV1, _CallCompletion]:
    called = _journal_completion(
        model=model,
        role=role,
        prompt=code_artifact_prompt(
            task=prepared.task,
            attempt_id=prepared.attempt.attempt_id,
            source=source,
            stage=stage,
            specification=specification,
            current_code=current_code,
            failure=failure,
        ),
        ctx=ctx,
        branch=prepared.branch.value,
        stage=stage,
        stage_ordinal=(
            _stage_ordinal(arm=ctx.arm, branch=prepared.branch, stage=stage)
            if stage_ordinal is None
            else stage_ordinal
        ),
        attempt=prepared.attempt,
        journal=journal,
    )
    return (
        parse_worker_candidate(
            called.completion.text,
            attempt_id=prepared.attempt.attempt_id,
            artifact_path=prepared.task.artifact_path,
        ),
        called,
    )


def _case_document(
    task: Task,
    cfg: Config,
    bundle: Template,
    specification: Spec,
) -> tuple[bytes | None, str, ProjectionError | None]:
    primary = retrieval_primary(specification, task)
    if primary is None:
        return None, "unindexed_primary_ambiguous", None
    index_task = task
    if index_task.shape is None:
        annotations = signature_symbols(specification.signature)[primary]
        from rrc.contract import StructuralShapeV1

        index_task = replace(
            index_task,
            shape=StructuralShapeV1(
                annotations,
                len(annotations),
                tuple(sorted(specification.slots.fields)),
            ),
        )
    if index_task.slot_values is None:
        try:
            bindings, _contexts = derive_bindings(
                specification,
                None,
                primary=primary,
                independent_tests=bundle.independent_tests,
            )
        except TemplateError:
            return None, "unindexed_projection_unavailable", ProjectionError("value_leak")
        index_task = replace(index_task, slot_values=bindings)
    index_task = replace(index_task, primary=primary)
    try:
        document = case_document(index_task, cfg, bundle)
    except ProjectionError as exc:
        return None, "unindexed_projection_unavailable", exc
    return document.canonical_bytes(), "indexed", None


def _rejected_outcome(
    *,
    prepared: PreparedSolve,
    mode: ArmMode,
    candidate: WorkerCandidateV1 | None,
    run: VerificationRunV1 | None,
    events: list[CostEventV1],
    call_ids: list[str],
    repairs: int,
    escalated: bool,
    phase: Literal["prepare", "spawn", "implement", "verify", "fallback", "deadline", "commit"],
    reason: Literal[
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
    ],
    journal: SQLiteRRCRepository,
    transport: Literal["sync", "contextmesh"] = "sync",
    terminal_owner: str = "sync-owner",
    specification: Spec | None = None,
) -> SolveOutcome:
    """Persist one exact rejection and return its public failed outcome."""

    current = journal.load_attempt(prepared.attempt.attempt_id)
    artifact_sha: str | None = None
    result_sha: str | None = None
    if run is not None:
        if run.artifact is None or run.public_accepted:
            raise ValueError("rejected verification evidence must be public-rejected")
        artifact_sha, result_sha = journal.persist_rejected_verification(
            run.artifact,
            run.result,
        )
    elif candidate is not None and candidate.kind == "code":
        # A valid candidate reaches verification before terminal rejection.  This
        # branch therefore indicates controller/verifier evidence corruption.
        raise ValueError("valid unverified candidate cannot become a terminal rejection")
    if phase in {"verify", "commit"} and run is None:
        phase = "implement"
        reason = "invalid_candidate"
    if current.state != "finishing" and phase in {"verify", "fallback", "commit"}:
        current = journal.claim_finishing(
            current,
            owner_id=terminal_owner,
            expected_state=current.state,
            expected_generation=current.generation,
            expected_cursor=current.cursor,
        )
    evidence = canonical_json_bytes(
        {
            "attempt_id": current.attempt_id,
            "candidate_final_message_sha256": (
                None if candidate is None else candidate.final_message_sha256
            ),
            "kind": "rejection_evidence",
            "phase": phase,
            "reason": reason,
            "verification_result_sha256": (
                None if run is None else _sha(verification_result_bytes(run.result))
            ),
            "v": 1,
        }
    )
    evidence_sha = journal.persist_rejection_evidence(current.attempt_id, evidence)
    durable_inventory = journal.load_call_inventory(current.attempt_id)
    durable_call_ids = tuple(row[0] for row in durable_inventory)
    if tuple(call_ids) and tuple(call_ids) != durable_call_ids:
        raise ValueError("rejection call inventory differs from durable journal")
    rejected = RejectedCommitV1(
        secrets.token_hex(32),
        RejectedOutcomeRecordV1(
            attempt_id=current.attempt_id,
            task_id=prepared.task.task_id,
            mode=mode.value,  # type: ignore[arg-type]
            transport=transport,
            phase=phase,
            reason=reason,
            source_state=current.state,
            valid_candidate_sha256=artifact_sha,
            verification_result_sha256=result_sha,
            evidence_sha256=evidence_sha,
            cost_event_ids=durable_call_ids,
        ),
    )
    try:
        journal.commit_rejected(
            current,
            TerminalClaimV1(terminal_owner, current.generation, current.state),
            rejected,
        )
    except Exception as error:
        # A COMMIT acknowledgement is not rollback evidence.  The exact rejection
        # intent is already known, so only a fresh read that reopens that marker
        # may turn this ambiguous exception into terminal success.  In particular,
        # never create a second rejection identity or replay paid work here.
        try:
            journal.reconcile_rejected(current, rejected)
        except Exception as reconcile_error:
            raise JournalStateError(
                "rejected commit is recovery-pending; replay is forbidden"
            ) from reconcile_error
        del error
    oracle: bool | None = None
    oracle_status: Literal["not_present", "passed", "failed", "infrastructure_failure"] = (
        "not_present"
    )
    if run is not None and run.artifact is not None and prepared.materials.oracle_tests:
        try:
            oracle = _score_hidden_oracle(
                artifact=run.artifact,
                task=prepared.task,
                oracle_tests=prepared.materials.oracle_tests,
                specification=specification,
            )
            oracle_status = "passed" if oracle is True else "failed"
        except Exception:
            oracle = None
            oracle_status = "infrastructure_failure"
        oracle_evidence = canonical_json_bytes(
            {
                "attempt_id": prepared.attempt.attempt_id,
                "score": oracle,
                "status": oracle_status,
                "v": 1,
            }
        )
        try:
            journal.record_oracle_score(
                prepared.attempt.attempt_id,
                status=oracle_status,
                score=oracle,
                evidence_sha256=_sha(oracle_evidence),
            )
        except Exception:
            oracle = None
            oracle_status = "infrastructure_failure"
    return SolveOutcome(
        task_id=prepared.task.task_id,
        arm=mode.value,
        code=(
            run.artifact.source
            if run is not None and run.artifact is not None
            else (
                candidate.artifact.source
                if candidate is not None and candidate.artifact is not None
                else ""
            )
        ),
        passed=False,
        pass_at_1=oracle,
        branch=prepared.branch,
        repairs=repairs,
        escalated=escalated,
        template=None,
        cost_events=tuple(events),
        oracle_status=oracle_status,
    )


def reject_contextmesh_attempt(
    prepared: PreparedSolve,
    *,
    mode: ArmMode,
    phase: Literal["prepare", "spawn", "implement", "deadline"],
    reason: Literal[
        "invalid_spec",
        "invalid_independent_tests",
        "invalid_candidate",
        "deadline_exceeded",
        "ambiguous_paid_call",
        "spawn_failed",
        "evidence_invalid",
        "store_failure",
    ],
    journal: SQLiteRRCRepository,
    terminal_owner: str,
) -> SolveOutcome:
    """Terminalize a split-phase attempt before the durable finisher can run."""

    if mode not in {ArmMode.COLD, ArmMode.WARM}:
        raise ValueError("ContextMesh rejection requires COLD or WARM mode")
    return _rejected_outcome(
        prepared=prepared,
        mode=mode,
        candidate=None,
        run=None,
        events=[],
        call_ids=[],
        repairs=0,
        escalated=False,
        phase=phase,
        reason=reason,
        journal=journal,
        transport="contextmesh",
        terminal_owner=terminal_owner,
    )


def _durable_events(
    journal: SQLiteRRCRepository,
    attempt_id: str,
) -> tuple[list[CostEventV1], list[str], bool]:
    """Reopen exact committed costs and report whether a paid call is ambiguous."""

    events: list[CostEventV1] = []
    call_ids: list[str] = []
    ambiguous = False
    for call_id, state, raw in journal.load_call_inventory(attempt_id):
        call_ids.append(call_id)
        if state != "call_committed" or raw is None:
            ambiguous = True
            continue
        event, observed = _cost_from_bytes(raw)
        if observed != call_id:
            raise ValueError("durable cost event differs from its call identity")
        events.append(event)
    return events, call_ids, ambiguous


def _repair_count(events: list[CostEventV1], mode: str) -> int:
    """Count repairs under the frozen direct-arm repeated-stage convention."""

    if mode == "cheap_alone":
        return max(0, sum(event.stage == "cheap_alone" for event in events) - 1)
    if mode == "cascade":
        return max(0, sum(event.stage == "cascade_cheap" for event in events) - 1)
    return sum(event.stage.startswith("repair_") for event in events)


def _hydrate_terminal(
    *,
    attempt: AttemptHandle,
    journal: SQLiteRRCRepository,
) -> SolveOutcome:
    """Reconstruct the public result from immutable terminal authority only."""

    terminal = journal.load_terminal_intent(attempt.attempt_id)
    if terminal is None:
        raise ValueError("attempt is not terminal")
    state, raw = terminal
    value = _json_object(raw, name="terminal intent")
    events, durable_ids, _ambiguous = _durable_events(journal, attempt.attempt_id)
    if state == "accepted":
        accepted_intent = parse_accepted_commit(raw)
        journal.reconcile_accepted(attempt, accepted_intent)
        if (
            set(value)
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
            raise ValueError("accepted terminal intent schema is invalid")
        outcome = value.get("outcome")
        if not isinstance(outcome, dict) or set(outcome) != {
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
        }:
            raise ValueError("accepted terminal outcome schema is invalid")
        code = journal.load_accepted_source(
            attempt.attempt_id,
            cast(str, outcome["artifact_record_sha256"]),
        )
        bundle_value = value.get("bundle")
        bundle = (
            None
            if bundle_value is None
            else parse_template_bundle(canonical_json_bytes(bundle_value))
        )
        passed = True
        oracle_row = journal.load_oracle_score(attempt.attempt_id)
    elif state == "rejected":
        rejected_intent = parse_rejected_commit(raw)
        journal.reconcile_rejected(attempt, rejected_intent)
        if set(value) != {"reject_commit_id", "rejected_outcome", "v"} or value.get("v") != 1:
            raise ValueError("rejected terminal intent schema is invalid")
        outcome = value.get("rejected_outcome")
        if not isinstance(outcome, dict) or set(outcome) != {
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
        }:
            raise ValueError("rejected terminal outcome schema is invalid")
        artifact_sha = outcome.get("valid_candidate_sha256")
        code = (
            ""
            if artifact_sha is None
            else journal.load_rejected_source(attempt.attempt_id, cast(str, artifact_sha))
        )
        bundle = None
        passed = False
        oracle_row = journal.load_oracle_score(attempt.attempt_id)
    else:
        raise ValueError("unknown terminal state")
    if outcome.get("attempt_id") != attempt.attempt_id:
        raise ValueError("terminal outcome attempt differs")
    ids = outcome.get("cost_event_ids")
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise ValueError("terminal outcome cost-event inventory is invalid")
    if tuple(ids) != tuple(durable_ids):
        raise ValueError("terminal outcome cost-event inventory differs from journal")
    branch = (
        BranchDecision(cast(str, outcome["branch"]))
        if state == "accepted"
        else (
            BranchDecision.DIRECT
            if outcome.get("mode") in {"baseline", "cheap_alone", "cascade"}
            else BranchDecision.MISS
        )
    )
    repairs = _repair_count(events, cast(str, outcome["mode"]))
    oracle_status = "not_present" if oracle_row is None else oracle_row[0]
    oracle_score = None if oracle_row is None else oracle_row[1]
    return SolveOutcome(
        task_id=cast(str, outcome["task_id"]),
        arm=cast(str, outcome["mode"]),
        code=code,
        passed=passed,
        pass_at_1=oracle_score,
        branch=branch,
        repairs=repairs,
        escalated=bool(outcome.get("escalated", False)),
        template=bundle,
        cost_events=tuple(events),
        oracle_status=cast(
            Literal["not_present", "passed", "failed", "infrastructure_failure"],
            oracle_status,
        ),
    )


def _accepted_outcome(
    *,
    prepared: PreparedSolve,
    mode: ArmMode,
    code: str,
    run: VerificationRunV1,
    events: list[CostEventV1],
    call_ids: list[str],
    repairs: int,
    escalated: bool,
    specification: Spec | None,
    bundle: Template | None,
    cfg: Config,
    acceptance: SQLiteRRCRepository,
    transport: Literal["sync", "contextmesh"] = "sync",
    terminal_owner: str = "sync-owner",
) -> SolveOutcome:
    if run.artifact is None or not run.public_accepted:
        raise ValueError("accepted outcome requires a public-accepted artifact")
    result_sha, artifact_record = acceptance.persist_verification(run.artifact, run.result)
    claimed = prepared.attempt
    if claimed.state != "finishing":
        claimed = acceptance.claim_finishing(
            claimed,
            owner_id=terminal_owner,
            expected_state=claimed.state,
            expected_generation=claimed.generation,
            expected_cursor=claimed.cursor,
        )
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
    if mode in {ArmMode.BASELINE, ArmMode.CHEAP_ALONE, ArmMode.CASCADE}:
        branch = "direct"
        disposition = "not_applicable"
        bundle_raw = None
        case = None
    elif mode is ArmMode.COLD:
        branch = prepared.branch.value
        disposition = "cold_no_store"
        bundle_raw = None
        case = None
    else:
        if bundle is None:
            raise ValueError("WARM acceptance requires a template bundle")
        branch = prepared.branch.value
        bundle_raw = template_bundle_bytes(bundle)
        if specification is None:
            raise ValueError("WARM acceptance requires its concrete specification")
        case, disposition, projection_error = _case_document(
            prepared.task, cfg, bundle, specification
        )
        if projection_error is not None:
            projection_input = prepared.task.text.encode("utf-8", errors="strict")
            projection, projection_evidence = projection_unavailable(
                attempt_id=prepared.attempt.attempt_id,
                phase="store",
                code=projection_error.code,
                input_bytes=projection_input,
            )
            acceptance.persist_projection_unavailable(
                projection.canonical_bytes(),
                input_bytes=projection_input,
                evidence=projection_evidence,
            )
    accept_commit_id = secrets.token_hex(32)
    everos_target: bytes | None = None
    everos_dispatch: bytes | None = None
    outbox_row: bytes | None = None
    if (
        mode is ArmMode.WARM
        and case is not None
        and cfg.memory_backend == "everos"
        and prepared.task.searchable_public
    ):
        if bundle is None:
            raise ValueError("EverOS WARM acceptance requires its template bundle")
        if cfg.everos_target is None:
            raise ValueError("EverOS WARM acceptance is missing its sealed target")
        target = parse_target(cfg.everos_target)
        from rrc.retrieval import parse_case_document

        document = parse_case_document(case)
        dispatch, outbox = build_dispatch(
            target=target,
            task=prepared.task,
            external_ref=bundle.external_ref,
            document_sha256=document.document_sha256,
            accept_commit_id=accept_commit_id,
            owner_scope=cfg.owner_scope,
            timestamp_ms=int(time.time() * 1000),
        )
        everos_target = target.canonical_bytes()
        everos_dispatch = dispatch.canonical_bytes()
        outbox_row = outbox.canonical_bytes()
    outcome_record = AcceptedOutcomeRecordV1(
        attempt_id=prepared.attempt.attempt_id,
        task_id=prepared.task.task_id,
        mode=mode.value,  # type: ignore[arg-type]
        transport=transport,
        branch=branch,  # type: ignore[arg-type]
        escalated=escalated,
        index_disposition=disposition,  # type: ignore[arg-type]
        artifact_record_sha256=_sha(artifact_record_raw),
        verification_result_sha256=result_sha,
        cost_event_ids=tuple(call_ids),
    )
    receipt_record: bytes | None = None
    if transport == "contextmesh":
        payload = AcceptedReceiptPayloadV1(
            attempt_id=prepared.attempt.attempt_id,
            accept_commit_id=accept_commit_id,
            artifact_path=artifact_record.artifact_path,
            source_sha256=artifact_record.source_sha256,
            source_bytes=artifact_record.source_bytes,
            verification_result_sha256=result_sha,
        )
        receipt_record = ReceiptRecordV1.bind(
            payload,
            artifact_record_sha256=_sha(artifact_record_raw),
        ).canonical_bytes()
    accepted = AcceptedCommitV1(
        accept_commit_id=accept_commit_id,
        mode=mode.value,  # type: ignore[arg-type]
        transport=transport,
        outcome=outcome_record,
        bundle=bundle_raw,
        case_document=case,
        everos_target=everos_target,
        everos_dispatch=everos_dispatch,
        outbox_row=outbox_row,
        artifact_record=artifact_record,
        receipt_record=receipt_record,
    )
    try:
        acceptance.commit_accepted(
            claimed,
            TerminalClaimV1(terminal_owner, claimed.generation, "finishing"),
            accepted,
        )
    except Exception as error:
        provisional = SolveOutcome(
            task_id=prepared.task.task_id,
            arm=mode.value,
            code=code,
            passed=True,
            pass_at_1=None,
            branch=prepared.branch,
            repairs=repairs,
            escalated=escalated,
            template=bundle,
            cost_events=tuple(events),
        )
        try:
            acceptance.reconcile_accepted(claimed, accepted)
        except JournalStateError:
            current = acceptance.load_attempt(claimed.attempt_id)
            if current.state == "finishing":
                evidence = canonical_json_bytes(
                    {
                        "attempt_id": current.attempt_id,
                        "candidate_final_message_sha256": None,
                        "kind": "rejection_evidence",
                        "phase": "commit",
                        "reason": "store_failure",
                        "verification_result_sha256": result_sha,
                        "v": 1,
                    }
                )
                try:
                    evidence_sha = acceptance.persist_rejection_evidence(
                        current.attempt_id, evidence
                    )
                    rejected = RejectedCommitV1(
                        secrets.token_hex(32),
                        RejectedOutcomeRecordV1(
                            attempt_id=current.attempt_id,
                            task_id=prepared.task.task_id,
                            mode=mode.value,  # type: ignore[arg-type]
                            transport=transport,
                            phase="commit",
                            reason="store_failure",
                            source_state="finishing",
                            valid_candidate_sha256=_sha(code_artifact_bytes(run.artifact)),
                            verification_result_sha256=result_sha,
                            evidence_sha256=evidence_sha,
                            cost_event_ids=tuple(call_ids),
                        ),
                    )
                    acceptance.commit_rejected(
                        current,
                        TerminalClaimV1(terminal_owner, current.generation, "finishing"),
                        rejected,
                    )
                except Exception:
                    # The completed computation remains attached to StoreFailure;
                    # an unavailable/corrupt repository is externally recovery-pending.
                    pass
            raise StoreFailure(provisional) from error
        except Exception:
            raise StoreFailure(provisional) from error
    oracle_status: Literal["not_present", "passed", "failed", "infrastructure_failure"] = (
        "not_present"
    )
    try:
        oracle = _score_hidden_oracle(
            artifact=run.artifact,
            task=prepared.task,
            oracle_tests=prepared.materials.oracle_tests,
            specification=specification,
        )
        if oracle is True:
            oracle_status = "passed"
        elif oracle is False:
            oracle_status = "failed"
    except Exception:
        oracle = None
        oracle_status = "infrastructure_failure"
    oracle_evidence = canonical_json_bytes(
        {
            "attempt_id": prepared.attempt.attempt_id,
            "score": oracle,
            "status": oracle_status,
            "v": 1,
        }
    )
    try:
        acceptance.record_oracle_score(
            prepared.attempt.attempt_id,
            status=oracle_status,
            score=oracle,
            evidence_sha256=_sha(oracle_evidence),
        )
    except Exception:
        # Acceptance is immutable and must not roll back because scoring evidence
        # failed.  The caller receives an explicitly invalid benchmark status.
        oracle = None
        oracle_status = "infrastructure_failure"
    return SolveOutcome(
        task_id=prepared.task.task_id,
        arm=mode.value,
        code=code,
        passed=True,
        pass_at_1=oracle,
        branch=prepared.branch,
        repairs=repairs,
        escalated=escalated,
        template=bundle,
        cost_events=tuple(events),
        oracle_status=oracle_status,
    )


def finish(
    prepared: PreparedSolve,
    candidate: WorkerCandidateV1,
    *,
    mode: ArmMode,
    model: ModelPort,
    cfg: Config,
    journal: SQLiteRRCRepository,
    acceptance: SQLiteRRCRepository,
    transport: Literal["sync", "contextmesh"] = "sync",
    terminal_owner: str = "sync-owner",
) -> SolveOutcome:
    """Verify/repair/fallback one exact prepared candidate and commit terminal state."""

    if mode not in {ArmMode.COLD, ArmMode.WARM} or prepared.attempt.state not in {
        "prepared",
        "finishing",
    }:
        raise ValueError("finish requires one prepared/claimed COLD/WARM attempt")
    if prepared.specification is None:
        raise ValueError("prepared Spec pipeline is missing its specification")
    specification = prepared.specification
    if prepared.attempt.state == "prepared":
        if transport != "sync":
            raise ValueError("ContextMesh finish requires a durable submitted/finishing claim")
        claimed = journal.claim_finishing(
            prepared.attempt,
            owner_id=terminal_owner,
            expected_state="prepared",
            expected_generation=prepared.attempt.generation,
            expected_cursor=prepared.attempt.cursor,
        )
    else:
        if transport != "contextmesh":
            raise ValueError("synchronous finish cannot consume a ContextMesh finishing claim")
        claimed = prepared.attempt
    prepared = replace(prepared, attempt=claimed)
    durable_events, durable_call_ids, ambiguous = _durable_events(
        journal, prepared.attempt.attempt_id
    )
    if ambiguous:
        raise JournalStateError("finish cannot continue with an ambiguous provider call")
    prepared_count = len(prepared.call_ids)
    if (
        tuple(durable_call_ids[:prepared_count]) != prepared.call_ids
        or tuple(durable_events[:prepared_count]) != prepared.cost_events
    ):
        raise ValueError("prepared cost inventory differs from the durable journal")
    model_cell_id = getattr(model, "product_cell_id", None)
    ctx = RunContext(
        mode.value,
        prepared.task.task_id,
        cfg.owner_scope,
        model_cell_id if isinstance(model_cell_id, str) else None,
    )
    # ContextMesh commits the native worker call after the immutable prepared
    # authority is sealed.  Reopen the complete durable inventory here so the
    # terminal outcome accounts for that worker exactly once.  Synchronous
    # callers already carry the same inventory, so this is an identity check for
    # them rather than an additional cost.
    events = durable_events
    call_ids = durable_call_ids
    current = candidate
    run: VerificationRunV1 | None = None
    repairs = 0
    for repair_index in range(cfg.repair_cap_N + 1):
        if current.kind == "code" and current.artifact is not None:
            run = _run_verifier(
                attempt_id=prepared.attempt.attempt_id,
                task=prepared.task,
                source=current.artifact.source,
                tests=VerificationTestsV1(
                    spec=specification.tests,
                    independent=prepared.independent_tests,
                    public=prepared.materials.public_tests,
                ),
                specification=specification,
            )
            if run.public_accepted:
                bundle = templatize(
                    specification,
                    prepared.independent_tests,
                    slot_values=prepared.task.slot_values,
                    primary=prepared.task.primary,
                )
                return _accepted_outcome(
                    prepared=prepared,
                    mode=mode,
                    code=run.normalized_source,
                    run=run,
                    events=events,
                    call_ids=call_ids,
                    repairs=repairs,
                    escalated=False,
                    specification=specification,
                    bundle=bundle,
                    cfg=cfg,
                    acceptance=acceptance,
                    transport=transport,
                    terminal_owner=terminal_owner,
                )
        if repair_index == cfg.repair_cap_N:
            break
        stage = f"repair_{repair_index + 1}"
        called = _journal_completion(
            model=model,
            role=ModelRole.SMALL,
            prompt=code_artifact_prompt(
                task=prepared.task,
                attempt_id=prepared.attempt.attempt_id,
                source=None,
                stage=stage,
                specification=specification,
                current_code=(
                    current.artifact.source
                    if current.kind == "code" and current.artifact is not None
                    else None
                ),
                failure=_failure_excerpt(run, current),
            ),
            ctx=ctx,
            branch=prepared.branch.value,
            stage=stage,
            stage_ordinal=_stage_ordinal(arm=mode.value, branch=prepared.branch, stage=stage),
            attempt=prepared.attempt,
            journal=journal,
        )
        prepared = replace(prepared, attempt=called.attempt)
        events.append(called.cost_event)
        call_ids.append(called.call_id)
        repairs += 1
        current = parse_worker_candidate(
            called.completion.text,
            attempt_id=prepared.attempt.attempt_id,
            artifact_path=prepared.task.artifact_path,
        )
        run = None
    fallback_attempted = True
    if mode in {ArmMode.COLD, ArmMode.WARM}:
        failure = _failure_excerpt(run, current)
        called = _journal_completion(
            model=model,
            role=ModelRole.STRONG,
            prompt=spec_prompt(prepared.task, prepared.materials.source, failure),
            ctx=ctx,
            branch=prepared.branch.value,
            stage="fallback_spec",
            stage_ordinal=_stage_ordinal(
                arm=mode.value, branch=prepared.branch, stage="fallback_spec"
            ),
            attempt=prepared.attempt,
            journal=journal,
        )
        prepared = replace(prepared, attempt=called.attempt)
        events.append(called.cost_event)
        call_ids.append(called.call_id)
        fallback_spec = parse_spec(
            called.completion.text,
            prepared.task,
            strict_primary=prepared.resolved_metadata is None,
        )
        if fallback_spec is not None:
            specification = fallback_spec
            called_tests = _journal_completion(
                model=model,
                role=ModelRole.SMALL,
                prompt=independent_tests_prompt(prepared.task, fallback_spec),
                ctx=ctx,
                branch=prepared.branch.value,
                stage="fallback_independent_tests",
                stage_ordinal=_stage_ordinal(
                    arm=mode.value,
                    branch=prepared.branch,
                    stage="fallback_independent_tests",
                ),
                attempt=prepared.attempt,
                journal=journal,
            )
            prepared = replace(prepared, attempt=called_tests.attempt)
            events.append(called_tests.cost_event)
            call_ids.append(called_tests.call_id)
            fallback_tests = _parse_independent_tests(called_tests.completion.text)
            if fallback_tests is not None:
                fallback_candidate, called_impl = _candidate_call(
                    prepared=prepared,
                    model=model,
                    journal=journal,
                    ctx=ctx,
                    stage="fallback_implement",
                    role=ModelRole.SMALL,
                    source=None,
                    specification=fallback_spec,
                )
                prepared = replace(prepared, attempt=called_impl.attempt)
                events.append(called_impl.cost_event)
                call_ids.append(called_impl.call_id)
                # The fallback candidate supersedes the exhausted candidate for
                # terminal evidence even when it is malformed or fails public
                # verification.  Otherwise a fallback_failed marker can point at
                # the pre-fallback bytes/result while accounting the fallback call.
                current = fallback_candidate
                run = None
                if fallback_candidate.kind == "code" and fallback_candidate.artifact is not None:
                    fallback_run = _run_verifier(
                        attempt_id=prepared.attempt.attempt_id,
                        task=prepared.task,
                        source=fallback_candidate.artifact.source,
                        tests=VerificationTestsV1(
                            spec=fallback_spec.tests,
                            independent=fallback_tests,
                            public=prepared.materials.public_tests,
                        ),
                        specification=fallback_spec,
                    )
                    run = fallback_run
                    if fallback_run.public_accepted:
                        bundle = templatize(
                            fallback_spec,
                            fallback_tests,
                            slot_values=prepared.task.slot_values,
                            primary=prepared.task.primary,
                        )
                        return _accepted_outcome(
                            prepared=prepared,
                            mode=mode,
                            code=fallback_run.normalized_source,
                            run=fallback_run,
                            events=events,
                            call_ids=call_ids,
                            repairs=repairs,
                            escalated=True,
                            specification=fallback_spec,
                            bundle=bundle,
                            cfg=cfg,
                            acceptance=acceptance,
                            transport=transport,
                            terminal_owner=terminal_owner,
                        )
    escalated = fallback_attempted
    return _rejected_outcome(
        prepared=prepared,
        mode=mode,
        candidate=current,
        run=run,
        events=events,
        call_ids=call_ids,
        repairs=repairs,
        escalated=escalated,
        phase="fallback" if escalated else "verify",
        reason=(
            "fallback_failed"
            if escalated
            else ("verification_failed" if run is not None else "invalid_candidate")
        ),
        journal=journal,
        transport=transport,
        terminal_owner=terminal_owner,
        specification=specification,
    )


def _direct(
    input: TaskEnvelopeV1,
    *,
    mode: ArmMode,
    model: ModelPort,
    cfg: Config,
    attempt: AttemptHandle,
    journal: SQLiteRRCRepository,
    acceptance: SQLiteRRCRepository,
) -> SolveOutcome:
    materials = reopen_task_inputs(input)
    prepared = PreparedSolve(
        attempt=attempt,
        task=input.task,
        materials=materials,
        branch=BranchDecision.DIRECT,
        specification=None,
        independent_tests=(),
        template=None,
        cost_events=(),
        call_ids=(),
    )
    ctx = RunContext(mode.value, input.task.task_id, cfg.owner_scope)
    if mode is ArmMode.BASELINE:
        initial_stage, role = "baseline", ModelRole.STRONG
    elif mode is ArmMode.CHEAP_ALONE:
        initial_stage, role = "cheap_alone", ModelRole.SMALL
    else:
        initial_stage, role = "cascade_cheap", ModelRole.SMALL
    current, called = _candidate_call(
        prepared=prepared,
        model=model,
        journal=journal,
        ctx=ctx,
        stage=initial_stage,
        stage_ordinal=1,
        role=role,
        source=materials.source,
    )
    prepared = replace(
        prepared,
        attempt=called.attempt,
        cost_events=(called.cost_event,),
        call_ids=(called.call_id,),
    )
    events = list(prepared.cost_events)
    call_ids = list(prepared.call_ids)
    run: VerificationRunV1 | None = None
    repairs = 0
    max_repairs = 0 if mode is ArmMode.BASELINE else cfg.repair_cap_N
    for index in range(max_repairs + 1):
        if current.kind == "code" and current.artifact is not None:
            run = _run_verifier(
                attempt_id=attempt.attempt_id,
                task=input.task,
                source=current.artifact.source,
                tests=VerificationTestsV1(public=materials.public_tests),
                specification=None,
            )
            if run.public_accepted:
                return _accepted_outcome(
                    prepared=prepared,
                    mode=mode,
                    code=run.normalized_source,
                    run=run,
                    events=events,
                    call_ids=call_ids,
                    repairs=repairs,
                    escalated=False,
                    specification=None,
                    bundle=None,
                    cfg=cfg,
                    acceptance=acceptance,
                )
        if index == max_repairs:
            break
        current, called = _candidate_call(
            prepared=prepared,
            model=model,
            journal=journal,
            ctx=ctx,
            stage=initial_stage,
            stage_ordinal=index + 2,
            role=ModelRole.SMALL,
            source=None,
            current_code=current.artifact.source if current.artifact is not None else None,
            failure=_failure_excerpt(run, current),
        )
        prepared = replace(prepared, attempt=called.attempt)
        events.append(called.cost_event)
        call_ids.append(called.call_id)
        repairs += 1
        run = None
    if mode is ArmMode.CASCADE:
        current, called = _candidate_call(
            prepared=prepared,
            model=model,
            journal=journal,
            ctx=ctx,
            stage="cascade_strong",
            role=ModelRole.STRONG,
            source=materials.source,
            failure=_failure_excerpt(run, current),
        )
        prepared = replace(prepared, attempt=called.attempt)
        events.append(called.cost_event)
        call_ids.append(called.call_id)
        run = None
        if current.kind == "code" and current.artifact is not None:
            run = _run_verifier(
                attempt_id=attempt.attempt_id,
                task=input.task,
                source=current.artifact.source,
                tests=VerificationTestsV1(public=materials.public_tests),
                specification=None,
            )
            if run.public_accepted:
                return _accepted_outcome(
                    prepared=prepared,
                    mode=mode,
                    code=run.normalized_source,
                    run=run,
                    events=events,
                    call_ids=call_ids,
                    repairs=repairs,
                    escalated=True,
                    specification=None,
                    bundle=None,
                    cfg=cfg,
                    acceptance=acceptance,
                )
    return _rejected_outcome(
        prepared=prepared,
        mode=mode,
        candidate=current,
        run=run,
        events=events,
        call_ids=call_ids,
        repairs=repairs,
        escalated=mode is ArmMode.CASCADE,
        phase="verify",
        reason="verification_failed" if run is not None else "invalid_candidate",
        journal=journal,
    )


def solve(
    input: TaskEnvelopeV1,
    *,
    mode: ArmMode,
    model: ModelPort,
    retrieval: RetrievalPort,
    cfg: Config,
    journal: SQLiteRRCRepository,
    acceptance: SQLiteRRCRepository,
    operation_key: str,
) -> SolveOutcome:
    """Solve one sealed task through the frozen five-arm canonical dispatcher."""

    if not isinstance(input, TaskEnvelopeV1):
        raise TypeError("solve input must be TaskEnvelopeV1")
    if not isinstance(mode, ArmMode):
        raise TypeError("solve mode must be ArmMode")
    if not isinstance(cfg, Config):
        raise TypeError("solve cfg must be Config")
    _require_ports(
        mode=mode,
        retrieval=retrieval,
        journal=journal,
        acceptance=acceptance,
    )
    # Reopen before begin_attempt: malformed/swapped input is a zero-port-call rejection.
    reopen_task_inputs(input)
    attempt = _begin(
        input,
        mode=mode,
        cfg=cfg,
        journal=journal,
        operation_key=operation_key,
    )
    if attempt.state in {"accepted", "rejected"}:
        return _hydrate_terminal(attempt=attempt, journal=journal)
    if mode in {ArmMode.BASELINE, ArmMode.CHEAP_ALONE, ArmMode.CASCADE}:
        try:
            return _direct(
                input,
                mode=mode,
                model=model,
                cfg=cfg,
                attempt=attempt,
                journal=journal,
                acceptance=acceptance,
            )
        except StoreFailure:
            raise
        except (JournalStateError, RuntimeError):
            current = journal.load_attempt(attempt.attempt_id)
            events, call_ids, _ambiguous = _durable_events(journal, attempt.attempt_id)
            failed = PreparedSolve(
                attempt=current,
                task=input.task,
                materials=reopen_task_inputs(input),
                branch=BranchDecision.DIRECT,
                specification=None,
                independent_tests=(),
                template=None,
                cost_events=tuple(events),
                call_ids=tuple(call_ids),
            )
            return _rejected_outcome(
                prepared=failed,
                mode=mode,
                candidate=None,
                run=None,
                events=events,
                call_ids=call_ids,
                repairs=_repair_count(events, mode.value),
                escalated=False,
                phase="implement",
                reason="ambiguous_paid_call",
                journal=journal,
            )
    try:
        if attempt.state == "prepared":
            prepared_raw = journal.load_prepared(attempt.attempt_id)
            if prepared_raw is None:
                raise ValueError("prepared attempt is missing its authority")
            prepared = hydrate_prepared(
                prepared_raw,
                attempt=attempt,
                input=input,
                journal=journal,
            )
        else:
            prepared = prepare(
                input,
                mode=mode,
                model=model,
                retrieval=retrieval,
                cfg=cfg,
                attempt=attempt,
                journal=journal,
            )
    except (ValueError, JournalStateError, RuntimeError) as error:
        current = journal.load_attempt(attempt.attempt_id)
        events, call_ids, ambiguous = _durable_events(journal, attempt.attempt_id)
        reason = (
            "ambiguous_paid_call"
            if ambiguous
            else (
                "invalid_independent_tests" if "independent test" in str(error) else "invalid_spec"
            )
        )
        failed = PreparedSolve(
            attempt=current,
            task=input.task,
            materials=reopen_task_inputs(input),
            branch=BranchDecision.MISS,
            specification=None,
            independent_tests=(),
            template=None,
            cost_events=tuple(events),
            call_ids=tuple(call_ids),
        )
        return _rejected_outcome(
            prepared=failed,
            mode=mode,
            candidate=None,
            run=None,
            events=events,
            call_ids=call_ids,
            repairs=0,
            escalated=False,
            phase="prepare",
            reason=reason,
            journal=journal,
        )
    try:
        candidate, called = _candidate_call(
            prepared=prepared,
            model=model,
            journal=journal,
            ctx=RunContext(mode.value, input.task.task_id, cfg.owner_scope),
            stage="implement",
            role=ModelRole.SMALL,
            source=None,
            specification=prepared.specification,
        )
    except (JournalStateError, RuntimeError):
        current = journal.load_attempt(attempt.attempt_id)
        events, call_ids, _ambiguous = _durable_events(journal, attempt.attempt_id)
        failed = replace(
            prepared,
            attempt=current,
            cost_events=tuple(events),
            call_ids=tuple(call_ids),
        )
        return _rejected_outcome(
            prepared=failed,
            mode=mode,
            candidate=None,
            run=None,
            events=events,
            call_ids=call_ids,
            repairs=0,
            escalated=False,
            phase="implement",
            reason="ambiguous_paid_call",
            journal=journal,
        )
    prepared = replace(
        prepared,
        attempt=called.attempt,
        cost_events=(*prepared.cost_events, called.cost_event),
        call_ids=(*prepared.call_ids, called.call_id),
    )
    try:
        return finish(
            prepared,
            candidate,
            mode=mode,
            model=model,
            cfg=cfg,
            journal=journal,
            acceptance=acceptance,
        )
    except StoreFailure:
        raise
    except (JournalStateError, RuntimeError):
        current = journal.load_attempt(attempt.attempt_id)
        events, call_ids, _ambiguous = _durable_events(journal, attempt.attempt_id)
        failed = replace(
            prepared,
            attempt=current,
            cost_events=tuple(events),
            call_ids=tuple(call_ids),
        )
        return _rejected_outcome(
            prepared=failed,
            mode=mode,
            candidate=None,
            run=None,
            events=events,
            call_ids=call_ids,
            repairs=_repair_count(events, mode.value),
            escalated=mode is ArmMode.WARM
            and prepared.branch in {BranchDecision.REUSE, BranchDecision.PRIME},
            phase="fallback",
            reason="ambiguous_paid_call",
            journal=journal,
        )


__all__ = [
    "PreparedSolve",
    "WorkerCandidateV1",
    "finish",
    "parse_worker_candidate",
    "prepare",
    "solve",
]
