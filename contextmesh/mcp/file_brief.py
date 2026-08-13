"""Compact, plan-bound ContextMesh source briefs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, NoReturn, cast

from harness.four_worker_plan import OverlapLedgerEntry

JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
ValidatedFileBrief = dict[str, JSONValue]
SUMMARY_FIELDS = (
    "purpose_and_api",
    "data_and_dependencies",
    "behaviour_and_failures",
    "plan_step_facts",
    "anchors",
)
# A peer needs the source facts selected by its plan, not a percentage of an
# arbitrarily large file.  Keep the default small while leaving headroom for
# the literal facts that the publisher must preserve.
MIN_SUMMARY_BYTES = 1_024
FACT_HEADROOM_BYTES = 512


class BriefValidationError(ValueError):
    """Raised when a peer-safe file brief is incomplete, oversized, or misbound."""


def _fail(message: str) -> NoReturn:
    raise BriefValidationError(f"brief_incomplete: {message}")


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        _fail(f"{field} must be an object")
    return cast(Mapping[str, object], value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        _fail(f"{field} must be a non-empty string")
    text = value.strip()
    if not text:
        _fail(f"{field} must be a non-empty string")
    return text


def _plan_scope(entry: OverlapLedgerEntry) -> list[dict[str, JSONValue]]:
    return [
        {"worker_id": worker_id, "step_indexes": list(range(len(steps)))}
        for worker_id, steps in entry.plan_steps
    ]


def _compact_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def peer_brief_payload(brief_id: str, summary: Mapping[str, object]) -> dict[str, object]:
    """Return the exact compact five-value object exposed to a peer.

    The owner publishes named fields for a clear authoring contract.  The peer
    already knows the fixed order from its packet, so repeating five long JSON
    keys in every broker response would consume a material fraction of a tiny
    source's budget without conveying another source fact.
    """

    return {"brief_id": brief_id, "summary": [summary[field] for field in SUMMARY_FIELDS]}


def peer_brief_size(brief_id: str, summary: Mapping[str, object]) -> int:
    """Measure the compact peer payload, including its ``brief_id`` envelope."""

    return len(_compact_json(peer_brief_payload(brief_id, summary)))


def summary_budget(entry: OverlapLedgerEntry, source_content: str) -> int:
    """Budget the brief from the plan facts, never from raw-file size."""

    # Keep ``source_content`` in the signature because validation binds the
    # brief to a real source body.  Its size must not turn a 130 KB generated
    # catalog into permission to send a 110 KB "summary" to every peer.
    if not source_content:
        raise ValueError("source_content must be non-empty")
    empty_summary = {field: "" for field in SUMMARY_FIELDS}
    envelope = peer_brief_size(entry.brief_id, empty_summary)
    fact_bytes = sum(len(fact.encode("utf-8")) for fact in entry.required_facts)
    peer_cap = max(MIN_SUMMARY_BYTES, fact_bytes * 2 + FACT_HEADROOM_BYTES)
    return max(1, peer_cap - envelope)


def target_summary_characters(entry: OverlapLedgerEntry, source_content: str) -> int:
    """Give the owner a safe, simple target below the exact byte limit."""

    return max(32, summary_budget(entry, source_content) - 160)


def brief_contract(entry: OverlapLedgerEntry, source_content: str) -> dict[str, JSONValue]:
    """Return the tiny authoring contract sent only to the raw source owner."""

    empty_summary = {field: "" for field in SUMMARY_FIELDS}
    max_summary = summary_budget(entry, source_content)
    return {
        "schema_version": "file-brief/v1",
        "max_summary_bytes": max_summary,
        "target_summary_characters": target_summary_characters(entry, source_content),
        "max_peer_payload_bytes": peer_brief_size(entry.brief_id, empty_summary) + max_summary,
        "peer_summary_encoding": "ordered array: purpose_and_api, data_and_dependencies, behaviour_and_failures, plan_step_facts, anchors",
        "authoring_rule": (
            "Extract source-bound facts only. Include every required_source_fact verbatim after confirming it against the source. "
            "The peer already has its own task plan: do not restate objectives, write paths, test matrices, "
            "commands, or unrelated framework facts. target_summary_characters is the total across all five values, "
            "not a per-field allowance. For a source smaller than 512 bytes, use one source-bound phrase per field "
            "of at most 35 characters."
        ),
        "required_source_facts": list(entry.required_facts),
        "required_fields": {
            "purpose_and_api": "Semicolon-separated relevant symbol signatures, behavior, mutation, and side effects.",
            "data_and_dependencies": "Semicolon-separated imports, collaborators, data shapes, literals, and direction.",
            "behaviour_and_failures": "Semicolon-separated ordered behavior, failures, invariants, and surprising edges.",
            "plan_step_facts": "Semicolon-separated source-derived consumption facts: exact constructor/call shape, public result fields, collection shape, and source errors where present.",
            "anchors": "Semicolon-separated source symbol or fact locators; numeric line ranges are optional.",
        },
    }


def _summary(payload: object, entry: OverlapLedgerEntry, source_content: str) -> dict[str, str]:
    value = _mapping(payload, "brief")
    if set(value) != set(SUMMARY_FIELDS):
        _fail("brief must contain exactly the compact summary fields")
    summary: dict[str, str] = {field: _text(value.get(field), field) for field in SUMMARY_FIELDS}
    empty_summary = {field: "" for field in SUMMARY_FIELDS}
    peer_cap = peer_brief_size(entry.brief_id, empty_summary) + summary_budget(entry, source_content)
    if peer_brief_size(entry.brief_id, summary) > peer_cap:
        _fail(f"summary exceeds {summary_budget(entry, source_content)} byte content budget")
    return summary


def _fact_key(value: str) -> str:
    """Normalize a fact for an exact, punctuation-insensitive coverage check."""

    return "".join(character for character in value.casefold() if character.isalnum())


def _require_coverage(summary: Mapping[str, str], entry: OverlapLedgerEntry) -> None:
    """Reject a terse-looking brief that omitted a consumer's required fact."""

    rendered = _fact_key(" ".join(summary.values()))
    missing = [fact for fact in entry.required_facts if _fact_key(fact) not in rendered]
    if missing:
        _fail("missing required_source_fact: " + "; ".join(missing))


def validate_file_brief(
    payload: object, entry: OverlapLedgerEntry, source_content: str
) -> ValidatedFileBrief:
    """Validate an owner-authored compact summary and bind it to immutable metadata."""

    if not isinstance(entry, OverlapLedgerEntry):
        _fail("entry must be an OverlapLedgerEntry")
    if not isinstance(source_content, str) or not source_content:
        _fail("source_content must be non-empty text")
    summary = _summary(payload, entry, source_content)
    _require_coverage(summary, entry)
    source_hash = hashlib.sha256(source_content.encode("utf-8")).hexdigest()
    value: dict[str, Any] = {
        "schema_version": "file-brief/v1",
        "source": {
            "canonical_path": entry.canonical_path,
            "language": "python" if entry.canonical_path.endswith(".py") else "text",
            "content_sha256": source_hash,
            "line_count": len(source_content.splitlines()),
            "raw_size": len(source_content.encode("utf-8")),
        },
        "binding": {
            "manifest_hash": entry.manifest_hash,
            "brief_id": entry.brief_id,
            "requirements_hash": entry.requirements_hash,
            "source_owner": entry.source_owner,
            "peer_workers": list(entry.peer_workers),
            "plan_scope": _plan_scope(entry),
        },
        "summary": summary,
    }
    try:
        return cast(ValidatedFileBrief, json.loads(json.dumps(value, sort_keys=True, allow_nan=False)))
    except (TypeError, ValueError) as error:
        _fail(f"brief is not JSON-compatible: {error}")
    raise AssertionError("unreachable")


__all__ = [
    "BriefValidationError",
    "SUMMARY_FIELDS",
    "ValidatedFileBrief",
    "brief_contract",
    "peer_brief_payload",
    "peer_brief_size",
    "summary_budget",
    "target_summary_characters",
    "validate_file_brief",
]
