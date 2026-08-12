"""Functional-only economic authority for the post-capability RRCv2 protocol.

Version 6 remains the immutable task/oracle/order core.  Version 7 deliberately disables live
economic and savings claims because the installed Codex capability surface cannot attest effective
service tier for every call.  This module is pure: it validates already sealed bytes and never
launches a provider, touches a journal, or imports the legacy analyzer.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Final, Mapping

from rrc.dispatch_permit import (
    AuthorityRef,
    DispatchPermitError,
    canonical_json,
    read_authority,
    validate_generated_output_authority_v2,
    validate_generated_output_authority_v3,
)


class EconomicAuthorityError(ValueError):
    """The frozen core, functional overlay, or result row is malformed or stale."""


CORE_FIELDS: Final = {
    "v",
    "kind",
    "source_workload_path",
    "source_workload_sha256",
    "source_oracles_path",
    "source_oracles_sha256",
    "projection_sha256",
}
OVERLAY_FIELDS: Final = {
    "v",
    "kind",
    "workload_core_sha256",
    "source_workload_sha256",
    "source_oracles_sha256",
    "capability_manifest_sha256",
    "capability_summary_sha256",
    "redesign_evidence_sha256",
    "rate_payload_sha256",
    "assembled_prompt_utf8_bytes_max",
    "capability_overhead_tokens_max",
    "conservative_input_token_upper_bound",
    "long_context_threshold_exclusive",
    "identity_policy",
    "economic_claim_eligible",
    "ineligibility_reason",
    "request_priced_estimate_only",
    "analyzer_file_sha256",
    "analysis_sha256",
}
OVERLAY_V8_FIELDS: Final = OVERLAY_FIELDS | {
    "capability_evidence_inventory_sha256",
    "sandbox_evidence_v2_sha256",
    "setup_accounting_sha256",
}
OVERLAY_V9_FIELDS: Final = OVERLAY_V8_FIELDS | {"generated_output_authority_v2_sha256"}
OVERLAY_V10_FIELDS: Final = OVERLAY_V9_FIELDS | {
    "setup_accounting_v2_sha256",
    "worker_context_attestation_sha256",
    "generated_output_authority_v3_sha256",
}
WORKER_CONTEXT_FIELDS: Final = {
    "v",
    "kind",
    "root_call_id",
    "worker_call_id",
    "root_rollout_sha256",
    "worker_rollout_sha256",
    "root_environment_sha256",
    "worker_environment_sha256",
    "worker_environment_source",
    "root_session_id",
    "worker_agent_id",
    "worker_session_id",
    "worker_parent_thread_id",
    "fork_context",
    "spawn_assignment_sha256",
    "initial_messages",
    "expected_initial_set_sha256",
    "root_private_message_sha256",
    "root_private_message_absent",
    "validation",
}
WORKLOAD_PATH: Final = "contextmesh/bench/rrcv2_workload.json"
ORACLES_PATH: Final = "contextmesh/bench/rrcv2_oracles.json"
RATE_PAYLOAD_SHA256: Final = "41479a47731582016733886d7162c1ad15446c1ba503578982989ca188c9dc28"
ANALYSIS_SHA256: Final = "443b39155eb0166cbf4d569103d370b779981861a183f2d62fafee2d2a8e8689"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hex64(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise EconomicAuthorityError(f"invalid {field}")
    return value


def _canonical_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EconomicAuthorityError(f"invalid {label}") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise EconomicAuthorityError(f"{label} must be canonical JSON")
    return value


def _safe_repo_path(value: object, expected: str, field: str) -> str:
    if not isinstance(value, str) or value != expected:
        raise EconomicAuthorityError(f"invalid {field}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise EconomicAuthorityError(f"unsafe {field}")
    return value


def _projection(workload: Mapping[str, Any]) -> dict[str, Any]:
    if "economic_evidence" not in workload:
        raise EconomicAuthorityError("v6 workload lacks superseded economic_evidence")
    projected = dict(workload)
    del projected["economic_evidence"]
    protocol = projected.get("protocol")
    if not isinstance(protocol, dict) or "workload_authority_sha256" not in protocol:
        raise EconomicAuthorityError("v6 workload lacks workload authority")
    protocol = dict(protocol)
    del protocol["workload_authority_sha256"]
    projected["protocol"] = protocol
    return projected


def workload_core_v6(
    *,
    source_workload_path: str,
    source_workload_raw: bytes,
    source_oracles_path: str,
    source_oracles_raw: bytes,
) -> dict[str, object]:
    """Build the exact v6 task/oracle/order projection authority."""

    _safe_repo_path(source_workload_path, WORKLOAD_PATH, "source_workload_path")
    _safe_repo_path(source_oracles_path, ORACLES_PATH, "source_oracles_path")
    workload = _canonical_object(source_workload_raw, "source workload")
    oracles = _canonical_object(source_oracles_raw, "source oracles")
    if workload.get("v") != 6 or oracles.get("v") != 6:
        raise EconomicAuthorityError("source workload and oracles must both be version 6")
    return {
        "v": 6,
        "kind": "rrcv2_workload_core",
        "source_workload_path": source_workload_path,
        "source_workload_sha256": _sha(source_workload_raw),
        "source_oracles_path": source_oracles_path,
        "source_oracles_sha256": _sha(source_oracles_raw),
        "projection_sha256": _sha(canonical_json(_projection(workload))),
    }


def validate_workload_core_v6(
    value: Mapping[str, object], *, source_workload_raw: bytes, source_oracles_raw: bytes
) -> dict[str, object]:
    """Recompute and validate the exact core, rejecting every retained-byte mutation."""

    if set(value) != CORE_FIELDS:
        raise EconomicAuthorityError("workload core has unknown or missing fields")
    expected = workload_core_v6(
        source_workload_path=str(value.get("source_workload_path", "")),
        source_workload_raw=source_workload_raw,
        source_oracles_path=str(value.get("source_oracles_path", "")),
        source_oracles_raw=source_oracles_raw,
    )
    if dict(value) != expected:
        raise EconomicAuthorityError("workload core differs from source projection")
    return expected


def economic_binding_overlay_v7(
    *,
    workload_core_sha256: str,
    source_workload_sha256: str,
    source_oracles_sha256: str,
    capability_manifest_sha256: str,
    capability_summary_sha256: str,
    redesign_evidence_sha256: str,
    rate_payload_sha256: str,
    analyzer_file_sha256: str,
    analysis_sha256: str,
) -> dict[str, object]:
    """Build the exact functional-only v7 binding over measured capability evidence."""

    hashes = {
        "workload_core_sha256": workload_core_sha256,
        "source_workload_sha256": source_workload_sha256,
        "source_oracles_sha256": source_oracles_sha256,
        "capability_manifest_sha256": capability_manifest_sha256,
        "capability_summary_sha256": capability_summary_sha256,
        "redesign_evidence_sha256": redesign_evidence_sha256,
        "rate_payload_sha256": rate_payload_sha256,
        "analyzer_file_sha256": analyzer_file_sha256,
        "analysis_sha256": analysis_sha256,
    }
    for field, digest in hashes.items():
        _hex64(digest, field)
    if rate_payload_sha256 != RATE_PAYLOAD_SHA256:
        raise EconomicAuthorityError("overlay rate payload is not the frozen request estimate")
    if analysis_sha256 != ANALYSIS_SHA256:
        raise EconomicAuthorityError("overlay analysis predicate differs from frozen v6")
    return {
        "v": 7,
        "kind": "rrcv2_economic_binding_overlay",
        **hashes,
        "assembled_prompt_utf8_bytes_max": 131_072,
        "capability_overhead_tokens_max": 65_536,
        "conservative_input_token_upper_bound": 196_608,
        "long_context_threshold_exclusive": 272_000,
        "identity_policy": "native_observed_or_unattested",
        "economic_claim_eligible": False,
        "ineligibility_reason": "effective_identity_or_tier_unattested",
        "request_priced_estimate_only": True,
    }


def economic_binding_overlay_v8(
    *,
    workload_core_sha256: str,
    source_workload_sha256: str,
    source_oracles_sha256: str,
    capability_manifest_sha256: str,
    capability_summary_sha256: str,
    capability_evidence_inventory_sha256: str,
    sandbox_evidence_v2_sha256: str,
    setup_accounting_sha256: str,
    redesign_evidence_sha256: str,
    rate_payload_sha256: str,
    analyzer_file_sha256: str,
    analysis_sha256: str,
) -> dict[str, object]:
    """Build v8, which adds transitive call, sandbox, and abandoned-cost authority."""

    value = economic_binding_overlay_v7(
        workload_core_sha256=workload_core_sha256,
        source_workload_sha256=source_workload_sha256,
        source_oracles_sha256=source_oracles_sha256,
        capability_manifest_sha256=capability_manifest_sha256,
        capability_summary_sha256=capability_summary_sha256,
        redesign_evidence_sha256=redesign_evidence_sha256,
        rate_payload_sha256=rate_payload_sha256,
        analyzer_file_sha256=analyzer_file_sha256,
        analysis_sha256=analysis_sha256,
    )
    value["v"] = 8
    value["kind"] = "rrcv2_economic_binding_overlay"
    value["capability_evidence_inventory_sha256"] = _hex64(
        capability_evidence_inventory_sha256, "capability evidence inventory sha256"
    )
    value["sandbox_evidence_v2_sha256"] = _hex64(
        sandbox_evidence_v2_sha256, "sandbox evidence v2 sha256"
    )
    value["setup_accounting_sha256"] = _hex64(setup_accounting_sha256, "setup accounting sha256")
    return value


def economic_binding_overlay_v9(
    *,
    workload_core_sha256: str,
    source_workload_sha256: str,
    source_oracles_sha256: str,
    capability_manifest_sha256: str,
    capability_summary_sha256: str,
    capability_evidence_inventory_sha256: str,
    sandbox_evidence_v2_sha256: str,
    setup_accounting_sha256: str,
    generated_output_authority_v2_sha256: str,
    redesign_evidence_sha256: str,
    rate_payload_sha256: str,
    analyzer_file_sha256: str,
    analysis_sha256: str,
) -> dict[str, object]:
    """Build the final overlay bound to the superseding generated-output authority."""

    value = economic_binding_overlay_v8(
        workload_core_sha256=workload_core_sha256,
        source_workload_sha256=source_workload_sha256,
        source_oracles_sha256=source_oracles_sha256,
        capability_manifest_sha256=capability_manifest_sha256,
        capability_summary_sha256=capability_summary_sha256,
        capability_evidence_inventory_sha256=capability_evidence_inventory_sha256,
        sandbox_evidence_v2_sha256=sandbox_evidence_v2_sha256,
        setup_accounting_sha256=setup_accounting_sha256,
        redesign_evidence_sha256=redesign_evidence_sha256,
        rate_payload_sha256=rate_payload_sha256,
        analyzer_file_sha256=analyzer_file_sha256,
        analysis_sha256=analysis_sha256,
    )
    value["v"] = 9
    value["generated_output_authority_v2_sha256"] = _hex64(
        generated_output_authority_v2_sha256, "generated output authority v2 sha256"
    )
    return value


def economic_binding_overlay_v10(
    *,
    setup_accounting_v2_sha256: str,
    worker_context_attestation_sha256: str,
    generated_output_authority_v3_sha256: str,
    **v9_arguments: str,
) -> dict[str, object]:
    """Build the final overlay after total setup and native-context convergence."""

    value = economic_binding_overlay_v9(**v9_arguments)
    value["v"] = 10
    value["setup_accounting_v2_sha256"] = _hex64(
        setup_accounting_v2_sha256, "setup accounting v2 sha256"
    )
    value["worker_context_attestation_sha256"] = _hex64(
        worker_context_attestation_sha256, "worker context attestation sha256"
    )
    value["generated_output_authority_v3_sha256"] = _hex64(
        generated_output_authority_v3_sha256, "generated output authority v3 sha256"
    )
    return value


def _authority_raw(ref: AuthorityRef, label: str, *, tracked_source: bool = False) -> bytes:
    try:
        return read_authority(ref, require_mode=None if tracked_source else 0o600)
    except DispatchPermitError as exc:
        raise EconomicAuthorityError(f"invalid {label} authority") from exc


def validate_economic_binding_overlay_v7(
    value: Mapping[str, object],
    *,
    workload_core_ref: AuthorityRef,
    source_workload_ref: AuthorityRef,
    source_oracles_ref: AuthorityRef,
    capability_manifest_ref: AuthorityRef,
    capability_summary_ref: AuthorityRef,
    redesign_evidence_ref: AuthorityRef,
    analyzer_ref: AuthorityRef,
) -> dict[str, object]:
    """Reopen every referenced authority and prove the v7 overlay is exact and ineligible."""

    if set(value) != OVERLAY_FIELDS:
        raise EconomicAuthorityError("economic overlay has unknown or missing fields")
    core_raw = _authority_raw(workload_core_ref, "workload core")
    workload_raw = _authority_raw(source_workload_ref, "source workload", tracked_source=True)
    oracles_raw = _authority_raw(source_oracles_ref, "source oracles", tracked_source=True)
    manifest_raw = _authority_raw(capability_manifest_ref, "capability manifest")
    summary_raw = _authority_raw(capability_summary_ref, "capability summary")
    redesign_raw = _authority_raw(redesign_evidence_ref, "redesign evidence")
    analyzer_raw = _authority_raw(analyzer_ref, "analyzer", tracked_source=True)
    core = _canonical_object(core_raw, "workload core")
    validate_workload_core_v6(
        core, source_workload_raw=workload_raw, source_oracles_raw=oracles_raw
    )
    workload = _canonical_object(workload_raw, "source workload")
    economic = workload.get("economic_evidence")
    protocol = workload.get("protocol")
    if not isinstance(economic, dict) or not isinstance(protocol, dict):
        raise EconomicAuthorityError("source workload lacks v6 authority fields")
    expected = economic_binding_overlay_v7(
        workload_core_sha256=_sha(core_raw),
        source_workload_sha256=_sha(workload_raw),
        source_oracles_sha256=_sha(oracles_raw),
        capability_manifest_sha256=_sha(manifest_raw),
        capability_summary_sha256=_sha(summary_raw),
        redesign_evidence_sha256=_sha(redesign_raw),
        rate_payload_sha256=str(economic.get("rate_payload_sha256", "")),
        analyzer_file_sha256=_sha(analyzer_raw),
        analysis_sha256=str(protocol.get("analysis_sha256", "")),
    )
    if dict(value) != expected:
        raise EconomicAuthorityError("economic overlay differs from sealed authorities")
    summary = _canonical_object(summary_raw, "capability summary")
    if summary.get("capability_manifest_sha256") != capability_manifest_ref.sha256:
        raise EconomicAuthorityError("capability summary is bound to another manifest")
    maxima = summary.get("surface_input_tokens_max")
    if not isinstance(maxima, dict) or not maxima:
        raise EconomicAuthorityError("capability summary lacks surface maxima")
    if any(
        isinstance(tokens, bool) or not isinstance(tokens, int) or not 0 <= tokens <= 65_536
        for tokens in maxima.values()
    ):
        raise EconomicAuthorityError("capability framing exceeds the v7 bound")
    if summary.get("exact_call_count") != 9:
        raise EconomicAuthorityError("capability summary does not contain exactly nine calls")
    return expected


def setup_accounting_v1(
    *,
    abandoned_manifest_sha256: str,
    abandoned_usage: list[Mapping[str, object]],
    abandoned_rejections_without_usage: int,
    capability_summary_sha256: str,
    canonical_usage: list[Mapping[str, object]],
) -> dict[str, object]:
    """Bind setup consumption outside every product arm without imputing rejected-call usage."""

    _hex64(abandoned_manifest_sha256, "abandoned manifest sha256")
    _hex64(capability_summary_sha256, "capability summary sha256")

    def total(rows: list[Mapping[str, object]], expected: int, label: str) -> int:
        if len(rows) != expected:
            raise EconomicAuthorityError(f"{label} usage cardinality differs")
        result = 0
        for row in rows:
            value = row.get("provider_total_tokens")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise EconomicAuthorityError(f"invalid {label} provider usage")
            result += value
        return result

    abandoned_tokens = total(abandoned_usage, 4, "abandoned")
    canonical_tokens = total(canonical_usage, 9, "canonical")
    if abandoned_rejections_without_usage != 1:
        raise EconomicAuthorityError("abandoned rejection count differs")
    return {
        "v": 1,
        "kind": "rrcv2_setup_accounting",
        "abandoned_capability_attempt_manifest_sha256": abandoned_manifest_sha256,
        "abandoned_provider_calls": 4,
        "abandoned_provider_visible_tokens": abandoned_tokens,
        "abandoned_rejections_without_usage": 1,
        "canonical_capability_summary_sha256": capability_summary_sha256,
        "canonical_provider_calls": 9,
        "canonical_provider_visible_tokens": canonical_tokens,
        "total_setup_provider_visible_tokens": abandoned_tokens + canonical_tokens,
        "product_arm_attribution": "none",
    }


def setup_accounting_v2(
    *,
    abandoned_manifest_sha256: str,
    abandoned_usage: list[Mapping[str, object]],
    abandoned_rejections_without_usage: int,
    redesign_evidence_sha256: str,
    redesign_usage: Mapping[str, object],
    capability_summary_sha256: str,
    canonical_usage: list[Mapping[str, object]],
) -> dict[str, object]:
    """Account every known paid M0 result row without claiming HTTP request cardinality."""

    v1 = setup_accounting_v1(
        abandoned_manifest_sha256=abandoned_manifest_sha256,
        abandoned_usage=abandoned_usage,
        abandoned_rejections_without_usage=abandoned_rejections_without_usage,
        capability_summary_sha256=capability_summary_sha256,
        canonical_usage=canonical_usage,
    )
    _hex64(redesign_evidence_sha256, "redesign evidence sha256")
    input_tokens = redesign_usage.get("input_tokens")
    output_tokens = redesign_usage.get("output_tokens")
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 0
        or isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens < 0
    ):
        raise EconomicAuthorityError("invalid redesign provider usage")
    redesign_total = input_tokens + output_tokens
    abandoned_total = v1["abandoned_provider_visible_tokens"]
    canonical_total = v1["canonical_provider_visible_tokens"]
    setup_subtotal = v1["total_setup_provider_visible_tokens"]
    if (
        isinstance(abandoned_total, bool)
        or not isinstance(abandoned_total, int)
        or isinstance(canonical_total, bool)
        or not isinstance(canonical_total, int)
        or isinstance(setup_subtotal, bool)
        or not isinstance(setup_subtotal, int)
    ):
        raise EconomicAuthorityError("setup v1 totals are malformed")
    return {
        "v": 2,
        "kind": "rrcv2_setup_accounting",
        "abandoned_capability_attempt_manifest_sha256": abandoned_manifest_sha256,
        "abandoned_logical_result_rows": 4,
        "abandoned_provider_visible_tokens": abandoned_total,
        "abandoned_rejections_without_usage": 1,
        "redesign_evidence_sha256": redesign_evidence_sha256,
        "redesign_logical_result_rows": 1,
        "redesign_provider_visible_tokens": redesign_total,
        "canonical_capability_summary_sha256": capability_summary_sha256,
        "canonical_logical_result_rows": 9,
        "canonical_provider_visible_tokens": canonical_total,
        "total_known_setup_logical_result_rows": 14,
        "total_known_setup_provider_visible_tokens": setup_subtotal + redesign_total,
        "provider_request_cardinality": "unattested",
        "product_arm_attribution": "none",
    }


def _rollout_rows(raw: bytes, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise EconomicAuthorityError(f"invalid {label} rollout encoding") from exc
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EconomicAuthorityError(f"invalid {label} rollout JSONL") from exc
        if not isinstance(row, dict):
            raise EconomicAuthorityError(f"invalid {label} rollout row")
        rows.append(row)
    if not rows:
        raise EconomicAuthorityError(f"empty {label} rollout")
    return rows


def _message_text(payload: Mapping[str, Any]) -> tuple[str, str, bytes]:
    role = payload.get("role")
    content = payload.get("content")
    if (
        role not in {"developer", "user"}
        or not isinstance(content, list)
        or not content
        or any(
            not isinstance(block, dict)
            or block.get("type") != "input_text"
            or not isinstance(block.get("text"), str)
            for block in content
        )
    ):
        raise EconomicAuthorityError("worker initial message is not exact input_text content")
    texts = [block["text"] for block in content]
    return role, "\n".join(texts), canonical_json(content)


def worker_context_attestation_v1(
    *,
    root_rollout_raw: bytes,
    worker_rollout_raw: bytes,
    root_environment_raw: bytes,
    worker_environment_raw: bytes,
) -> dict[str, object]:
    """Derive a complete observed initial-message inventory from the retained native rollouts."""

    root_rows = _rollout_rows(root_rollout_raw, "root")
    worker_rows = _rollout_rows(worker_rollout_raw, "worker")
    root_meta = root_rows[0].get("payload")
    worker_meta = worker_rows[0].get("payload")
    if not isinstance(root_meta, dict) or not isinstance(worker_meta, dict):
        raise EconomicAuthorityError("native rollout lacks session metadata")
    root_session = root_meta.get("session_id")
    worker_agent = worker_meta.get("id")
    worker_session = worker_meta.get("session_id")
    worker_parent = worker_meta.get("parent_thread_id")
    if (
        not all(isinstance(value, str) and value for value in (root_session, worker_agent))
        or worker_session != root_session
        or worker_parent != root_session
        or worker_agent == root_session
    ):
        raise EconomicAuthorityError("native worker session/parent/agent identity differs")

    spawn_rows: list[dict[str, Any]] = []
    root_private_messages: list[str] = []
    for row in root_rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
            spawn_rows.append(payload)
        if payload.get("type") == "user_message" and isinstance(payload.get("message"), str):
            root_private_messages.append(payload["message"])
    if len(spawn_rows) != 1 or len(root_private_messages) != 1:
        raise EconomicAuthorityError("native root spawn/private-message cardinality differs")
    try:
        spawn_arguments = json.loads(spawn_rows[0]["arguments"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise EconomicAuthorityError("native spawn arguments are malformed") from exc
    expected_assignment = (
        "RRCV2_WORKER_CAPABILITY. Return exactly CAPABILITY_WORKER_OK. Do not call any tool."
    )
    if spawn_arguments != {
        "agent_type": "worker",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "low",
        "service_tier": "priority",
        "fork_context": False,
        "message": expected_assignment,
    }:
        raise EconomicAuthorityError("native spawn arguments differ from the capability contract")

    initial_payloads: list[dict[str, Any]] = []
    for row in worker_rows[1:]:
        payload = row.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "agent_message":
            break
        if (
            row.get("type") == "response_item"
            and isinstance(payload, dict)
            and payload.get("type") == "message"
            and payload.get("role") in {"developer", "user"}
        ):
            initial_payloads.append(payload)
    if len(initial_payloads) != 4:
        raise EconomicAuthorityError("worker initial-message cardinality differs")
    sources = ("base_developer", "environment", "subagent_start", "assignment")
    expected_roles = ("developer", "user", "developer", "user")
    messages: list[dict[str, object]] = []
    texts: list[str] = []
    for index, (payload, source, expected_role) in enumerate(
        zip(initial_payloads, sources, expected_roles, strict=True)
    ):
        role, message, message_raw = _message_text(payload)
        if role != expected_role:
            raise EconomicAuthorityError("worker initial-message role/order differs")
        texts.append(message)
        messages.append(
            {
                "index": index,
                "role": role,
                "source": source,
                "sha256": _sha(message_raw),
                "bytes": len(message_raw),
            }
        )
    if (
        texts[2] != "CAPABILITY_SUBAGENT_START_CONTEXT"
        or texts[3] != expected_assignment + "\nCAPABILITY_PRETOOL_REWRITE"
    ):
        raise EconomicAuthorityError("worker hook/assignment messages differ")
    root_private = root_private_messages[0].encode("utf-8")
    if any(root_private_messages[0] in text for text in texts):
        raise EconomicAuthorityError("root private message inherited into worker initial context")
    if root_environment_raw != worker_environment_raw:
        raise EconomicAuthorityError(
            "worker environment is not the observed inherited root process"
        )
    return {
        "v": 1,
        "kind": "rrcv2_native_worker_context_attestation",
        "root_call_id": "cap-08-root-strong-medium-native",
        "worker_call_id": "cap-09-worker-small-low-native",
        "root_rollout_sha256": _sha(root_rollout_raw),
        "worker_rollout_sha256": _sha(worker_rollout_raw),
        "root_environment_sha256": _sha(root_environment_raw),
        "worker_environment_sha256": _sha(worker_environment_raw),
        "worker_environment_source": "inherited_root_process",
        "root_session_id": root_session,
        "worker_agent_id": worker_agent,
        "worker_session_id": worker_session,
        "worker_parent_thread_id": worker_parent,
        "fork_context": False,
        "spawn_assignment_sha256": _sha(expected_assignment.encode("utf-8")),
        "initial_messages": messages,
        "expected_initial_set_sha256": _sha(canonical_json(messages)),
        "root_private_message_sha256": _sha(root_private),
        "root_private_message_absent": True,
        "validation": "passed",
    }


def validate_worker_context_attestation_v1(
    value: Mapping[str, object],
    *,
    root_rollout_ref: AuthorityRef,
    worker_rollout_ref: AuthorityRef,
    root_environment_ref: AuthorityRef,
    worker_environment_ref: AuthorityRef,
) -> dict[str, object]:
    """Reopen the four native artifacts and reproduce the complete observed attestation."""

    if set(value) != WORKER_CONTEXT_FIELDS:
        raise EconomicAuthorityError("worker context attestation has unknown or missing fields")
    expected = worker_context_attestation_v1(
        root_rollout_raw=_authority_raw(root_rollout_ref, "root rollout"),
        worker_rollout_raw=_authority_raw(worker_rollout_ref, "worker rollout"),
        root_environment_raw=_authority_raw(root_environment_ref, "root environment"),
        worker_environment_raw=_authority_raw(worker_environment_ref, "worker environment"),
    )
    if dict(value) != expected:
        raise EconomicAuthorityError("worker context attestation differs from retained rollouts")
    return expected


def validate_economic_binding_overlay_v8(
    value: Mapping[str, object],
    *,
    workload_core_ref: AuthorityRef,
    source_workload_ref: AuthorityRef,
    source_oracles_ref: AuthorityRef,
    capability_manifest_ref: AuthorityRef,
    capability_summary_ref: AuthorityRef,
    capability_evidence_inventory_ref: AuthorityRef,
    sandbox_evidence_v2_ref: AuthorityRef,
    setup_accounting_ref: AuthorityRef,
    abandoned_manifest_ref: AuthorityRef,
    redesign_evidence_ref: AuthorityRef,
    analyzer_ref: AuthorityRef,
) -> dict[str, object]:
    """Validate the final functional-only overlay and every transitive capability authority."""

    if set(value) != OVERLAY_V8_FIELDS:
        raise EconomicAuthorityError("economic overlay v8 has unknown or missing fields")
    core_raw = _authority_raw(workload_core_ref, "workload core")
    workload_raw = _authority_raw(source_workload_ref, "source workload", tracked_source=True)
    oracles_raw = _authority_raw(source_oracles_ref, "source oracles", tracked_source=True)
    manifest_raw = _authority_raw(capability_manifest_ref, "capability manifest")
    summary_raw = _authority_raw(capability_summary_ref, "capability summary")
    inventory_raw = _authority_raw(
        capability_evidence_inventory_ref, "capability evidence inventory"
    )
    sandbox_raw = _authority_raw(sandbox_evidence_v2_ref, "sandbox evidence v2")
    setup_raw = _authority_raw(setup_accounting_ref, "setup accounting")
    abandoned_raw = _authority_raw(abandoned_manifest_ref, "abandoned capability manifest")
    redesign_raw = _authority_raw(redesign_evidence_ref, "redesign evidence")
    analyzer_raw = _authority_raw(analyzer_ref, "analyzer", tracked_source=True)
    core = _canonical_object(core_raw, "workload core")
    validate_workload_core_v6(
        core, source_workload_raw=workload_raw, source_oracles_raw=oracles_raw
    )
    workload = _canonical_object(workload_raw, "source workload")
    economic = workload.get("economic_evidence")
    protocol = workload.get("protocol")
    if not isinstance(economic, dict) or not isinstance(protocol, dict):
        raise EconomicAuthorityError("source workload lacks v6 authority fields")
    expected = economic_binding_overlay_v8(
        workload_core_sha256=_sha(core_raw),
        source_workload_sha256=_sha(workload_raw),
        source_oracles_sha256=_sha(oracles_raw),
        capability_manifest_sha256=_sha(manifest_raw),
        capability_summary_sha256=_sha(summary_raw),
        capability_evidence_inventory_sha256=_sha(inventory_raw),
        sandbox_evidence_v2_sha256=_sha(sandbox_raw),
        setup_accounting_sha256=_sha(setup_raw),
        redesign_evidence_sha256=_sha(redesign_raw),
        rate_payload_sha256=str(economic.get("rate_payload_sha256", "")),
        analyzer_file_sha256=_sha(analyzer_raw),
        analysis_sha256=str(protocol.get("analysis_sha256", "")),
    )
    if dict(value) != expected:
        raise EconomicAuthorityError("economic overlay v8 differs from sealed authorities")
    summary = _canonical_object(summary_raw, "capability summary")
    inventory = _canonical_object(inventory_raw, "capability evidence inventory")
    sandbox = _canonical_object(sandbox_raw, "sandbox evidence v2")
    setup = _canonical_object(setup_raw, "setup accounting")
    abandoned = _canonical_object(abandoned_raw, "abandoned capability manifest")
    if (
        inventory.get("capability_manifest_sha256") != capability_manifest_ref.sha256
        or inventory.get("capability_summary_sha256") != capability_summary_ref.sha256
        or not isinstance(inventory.get("calls"), list)
        or len(inventory["calls"]) != 9
    ):
        raise EconomicAuthorityError("capability inventory binding/cardinality differs")
    results = summary.get("results")
    if not isinstance(results, list) or len(results) != 9:
        raise EconomicAuthorityError("capability summary cardinality differs")
    probes = sandbox.get("probes")
    if (
        sandbox.get("v") != 2
        or sandbox.get("provider_launch_total") != 0
        or not isinstance(probes, list)
        or len(probes) != 20
        or any(not isinstance(row, dict) or row.get("status") != "passed" for row in probes)
    ):
        raise EconomicAuthorityError("sandbox v2 does not contain twenty passed probes")
    abandoned_usage = abandoned.get("usage")
    abandoned_rejections = abandoned.get("provider_schema_rejections_without_usage")
    if not isinstance(abandoned_usage, list) or not isinstance(abandoned_rejections, int):
        raise EconomicAuthorityError("abandoned capability accounting is malformed")
    expected_setup = setup_accounting_v1(
        abandoned_manifest_sha256=_sha(abandoned_raw),
        abandoned_usage=abandoned_usage,
        abandoned_rejections_without_usage=abandoned_rejections,
        capability_summary_sha256=capability_summary_ref.sha256,
        canonical_usage=results,
    )
    if setup != expected_setup:
        raise EconomicAuthorityError("setup accounting totals do not reconcile")
    return expected


def validate_economic_binding_overlay_v9(
    value: Mapping[str, object],
    *,
    workload_core_ref: AuthorityRef,
    source_workload_ref: AuthorityRef,
    source_oracles_ref: AuthorityRef,
    capability_manifest_ref: AuthorityRef,
    capability_summary_ref: AuthorityRef,
    capability_evidence_inventory_ref: AuthorityRef,
    sandbox_evidence_v2_ref: AuthorityRef,
    setup_accounting_ref: AuthorityRef,
    generated_output_authority_v2_ref: AuthorityRef,
    generated_output_authority_v1_ref: AuthorityRef,
    abandoned_manifest_ref: AuthorityRef,
    redesign_evidence_ref: AuthorityRef,
    analyzer_ref: AuthorityRef,
) -> dict[str, object]:
    """Validate v9 and the exact v2 generated-output authority it adds to v8."""

    if set(value) != OVERLAY_V9_FIELDS:
        raise EconomicAuthorityError("economic overlay v9 has unknown or missing fields")
    v8 = dict(value)
    v8.pop("generated_output_authority_v2_sha256")
    v8["v"] = 8
    validate_economic_binding_overlay_v8(
        v8,
        workload_core_ref=workload_core_ref,
        source_workload_ref=source_workload_ref,
        source_oracles_ref=source_oracles_ref,
        capability_manifest_ref=capability_manifest_ref,
        capability_summary_ref=capability_summary_ref,
        capability_evidence_inventory_ref=capability_evidence_inventory_ref,
        sandbox_evidence_v2_ref=sandbox_evidence_v2_ref,
        setup_accounting_ref=setup_accounting_ref,
        abandoned_manifest_ref=abandoned_manifest_ref,
        redesign_evidence_ref=redesign_evidence_ref,
        analyzer_ref=analyzer_ref,
    )
    try:
        validate_generated_output_authority_v2(
            authority_ref=generated_output_authority_v2_ref,
            superseded_ref=generated_output_authority_v1_ref,
        )
    except DispatchPermitError as exc:
        raise EconomicAuthorityError("generated output authority v2 is invalid") from exc
    workload_raw = _authority_raw(source_workload_ref, "source workload", tracked_source=True)
    workload = _canonical_object(workload_raw, "source workload")
    economic = workload.get("economic_evidence")
    protocol = workload.get("protocol")
    if not isinstance(economic, dict) or not isinstance(protocol, dict):
        raise EconomicAuthorityError("source workload lacks v6 authority fields")
    expected = economic_binding_overlay_v9(
        workload_core_sha256=workload_core_ref.sha256,
        source_workload_sha256=source_workload_ref.sha256,
        source_oracles_sha256=source_oracles_ref.sha256,
        capability_manifest_sha256=capability_manifest_ref.sha256,
        capability_summary_sha256=capability_summary_ref.sha256,
        capability_evidence_inventory_sha256=capability_evidence_inventory_ref.sha256,
        sandbox_evidence_v2_sha256=sandbox_evidence_v2_ref.sha256,
        setup_accounting_sha256=setup_accounting_ref.sha256,
        generated_output_authority_v2_sha256=generated_output_authority_v2_ref.sha256,
        redesign_evidence_sha256=redesign_evidence_ref.sha256,
        rate_payload_sha256=str(economic.get("rate_payload_sha256", "")),
        analyzer_file_sha256=analyzer_ref.sha256,
        analysis_sha256=str(protocol.get("analysis_sha256", "")),
    )
    if dict(value) != expected:
        raise EconomicAuthorityError("economic overlay v9 differs from sealed authorities")
    return expected


def validate_economic_binding_overlay_v10(
    value: Mapping[str, object],
    *,
    workload_core_ref: AuthorityRef,
    source_workload_ref: AuthorityRef,
    source_oracles_ref: AuthorityRef,
    capability_manifest_ref: AuthorityRef,
    capability_summary_ref: AuthorityRef,
    capability_evidence_inventory_ref: AuthorityRef,
    sandbox_evidence_v2_ref: AuthorityRef,
    setup_accounting_ref: AuthorityRef,
    setup_accounting_v2_ref: AuthorityRef,
    worker_context_attestation_ref: AuthorityRef,
    root_rollout_ref: AuthorityRef,
    worker_rollout_ref: AuthorityRef,
    root_environment_ref: AuthorityRef,
    worker_environment_ref: AuthorityRef,
    generated_output_authority_v3_ref: AuthorityRef,
    generated_output_authority_v2_ref: AuthorityRef,
    generated_output_authority_v1_ref: AuthorityRef,
    abandoned_manifest_ref: AuthorityRef,
    redesign_evidence_ref: AuthorityRef,
    analyzer_ref: AuthorityRef,
) -> dict[str, object]:
    """Validate final M0 economic/capability authority including setup and native context."""

    if set(value) != OVERLAY_V10_FIELDS:
        raise EconomicAuthorityError("economic overlay v10 has unknown or missing fields")
    v9 = dict(value)
    v9.pop("setup_accounting_v2_sha256")
    v9.pop("worker_context_attestation_sha256")
    v9.pop("generated_output_authority_v3_sha256")
    v9["v"] = 9
    validate_economic_binding_overlay_v9(
        v9,
        workload_core_ref=workload_core_ref,
        source_workload_ref=source_workload_ref,
        source_oracles_ref=source_oracles_ref,
        capability_manifest_ref=capability_manifest_ref,
        capability_summary_ref=capability_summary_ref,
        capability_evidence_inventory_ref=capability_evidence_inventory_ref,
        sandbox_evidence_v2_ref=sandbox_evidence_v2_ref,
        setup_accounting_ref=setup_accounting_ref,
        generated_output_authority_v2_ref=generated_output_authority_v2_ref,
        generated_output_authority_v1_ref=generated_output_authority_v1_ref,
        abandoned_manifest_ref=abandoned_manifest_ref,
        redesign_evidence_ref=redesign_evidence_ref,
        analyzer_ref=analyzer_ref,
    )
    try:
        validate_generated_output_authority_v3(
            authority_ref=generated_output_authority_v3_ref,
            superseded_v2_ref=generated_output_authority_v2_ref,
            superseded_v1_ref=generated_output_authority_v1_ref,
        )
    except DispatchPermitError as exc:
        raise EconomicAuthorityError("generated output authority v3 is invalid") from exc
    attestation = _canonical_object(
        _authority_raw(worker_context_attestation_ref, "worker context attestation"),
        "worker context attestation",
    )
    validate_worker_context_attestation_v1(
        attestation,
        root_rollout_ref=root_rollout_ref,
        worker_rollout_ref=worker_rollout_ref,
        root_environment_ref=root_environment_ref,
        worker_environment_ref=worker_environment_ref,
    )
    abandoned = _canonical_object(
        _authority_raw(abandoned_manifest_ref, "abandoned capability manifest"),
        "abandoned capability manifest",
    )
    redesign = _canonical_object(
        _authority_raw(redesign_evidence_ref, "redesign evidence"), "redesign evidence"
    )
    summary = _canonical_object(
        _authority_raw(capability_summary_ref, "capability summary"), "capability summary"
    )
    abandoned_usage = abandoned.get("usage")
    redesign_usage = redesign.get("provider_usage")
    canonical_usage = summary.get("results")
    rejections = abandoned.get("provider_schema_rejections_without_usage")
    if (
        not isinstance(abandoned_usage, list)
        or not isinstance(redesign_usage, dict)
        or not isinstance(canonical_usage, list)
        or not isinstance(rejections, int)
    ):
        raise EconomicAuthorityError("setup v2 inputs are malformed")
    setup_v2 = _canonical_object(
        _authority_raw(setup_accounting_v2_ref, "setup accounting v2"), "setup accounting v2"
    )
    expected_setup_v2 = setup_accounting_v2(
        abandoned_manifest_sha256=abandoned_manifest_ref.sha256,
        abandoned_usage=abandoned_usage,
        abandoned_rejections_without_usage=rejections,
        redesign_evidence_sha256=redesign_evidence_ref.sha256,
        redesign_usage=redesign_usage,
        capability_summary_sha256=capability_summary_ref.sha256,
        canonical_usage=canonical_usage,
    )
    if setup_v2 != expected_setup_v2:
        raise EconomicAuthorityError("setup accounting v2 totals do not reconcile")
    workload = _canonical_object(
        _authority_raw(source_workload_ref, "source workload", tracked_source=True),
        "source workload",
    )
    economic = workload.get("economic_evidence")
    protocol = workload.get("protocol")
    if not isinstance(economic, dict) or not isinstance(protocol, dict):
        raise EconomicAuthorityError("source workload lacks v6 authority fields")
    expected = economic_binding_overlay_v10(
        workload_core_sha256=workload_core_ref.sha256,
        source_workload_sha256=source_workload_ref.sha256,
        source_oracles_sha256=source_oracles_ref.sha256,
        capability_manifest_sha256=capability_manifest_ref.sha256,
        capability_summary_sha256=capability_summary_ref.sha256,
        capability_evidence_inventory_sha256=capability_evidence_inventory_ref.sha256,
        sandbox_evidence_v2_sha256=sandbox_evidence_v2_ref.sha256,
        setup_accounting_sha256=setup_accounting_ref.sha256,
        generated_output_authority_v2_sha256=generated_output_authority_v2_ref.sha256,
        redesign_evidence_sha256=redesign_evidence_ref.sha256,
        rate_payload_sha256=str(economic.get("rate_payload_sha256", "")),
        analyzer_file_sha256=analyzer_ref.sha256,
        analysis_sha256=str(protocol.get("analysis_sha256", "")),
        setup_accounting_v2_sha256=setup_accounting_v2_ref.sha256,
        worker_context_attestation_sha256=worker_context_attestation_ref.sha256,
        generated_output_authority_v3_sha256=generated_output_authority_v3_ref.sha256,
    )
    if dict(value) != expected:
        raise EconomicAuthorityError("economic overlay v10 differs from sealed authorities")
    return expected


def assemble_functional_result(
    *,
    overlay: Mapping[str, object],
    token_metrics: Mapping[str, object],
    public_accepted: bool,
    hidden_oracle_passed: bool,
) -> dict[str, object]:
    """Assemble a live functional result without routing it to the v6 savings analyzer."""

    if (
        set(overlay) != OVERLAY_V10_FIELDS
        or overlay.get("v") != 10
        or overlay.get("economic_claim_eligible") is not False
        or overlay.get("request_priced_estimate_only") is not True
    ):
        raise EconomicAuthorityError("functional result requires the exact ineligible v10 overlay")
    required = {
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "provider_total_tokens",
    }
    if set(token_metrics) != required:
        raise EconomicAuthorityError("token metrics have unknown or missing fields")
    metrics: dict[str, int] = {}
    for field, value in token_metrics.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EconomicAuthorityError("token metrics must be nonnegative integers")
        metrics[field] = value
    input_tokens = metrics["input_tokens"]
    cached = metrics["cached_input_tokens"]
    output = metrics["output_tokens"]
    reasoning = metrics["reasoning_output_tokens"]
    if (
        cached > input_tokens
        or reasoning > output
        or metrics["provider_total_tokens"] != input_tokens + output
    ):
        raise EconomicAuthorityError("token metrics do not reconcile")
    if type(public_accepted) is not bool or type(hidden_oracle_passed) is not bool:
        raise EconomicAuthorityError("quality metrics must be booleans")
    return {
        "v": 10,
        "kind": "rrcv2_functional_result",
        "overlay_sha256": _sha(canonical_json(dict(overlay))),
        "token_metrics": metrics,
        "quality": {
            "public_accepted": public_accepted,
            "hidden_oracle_passed": hidden_oracle_passed,
        },
        "economic_claim_eligible": False,
        "savings_claimed": False,
    }
