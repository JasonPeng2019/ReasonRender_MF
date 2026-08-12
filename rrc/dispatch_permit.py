"""Fail-closed dispatch authority for RRCv2 provider calls.

The module performs no provider or journal mutation.  ContextMesh attempt authorization does one
read-only lookup through the trusted cell-journal authority port to prove its root call was started.
Controllers must obtain a permit first, then record ``call_started`` and launch the provider.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Mapping, Protocol

from rrc.contract import parse_task_envelope

AUTHORITY_CAP: Final = 4 * 1024 * 1024
CLI_VERSION: Final = "codex-cli 0.147.0"
PROVIDER: Final = "openai"
SERVICE_TIER: Final = "priority"
BASELINE_SEAL_SHA256: Final = "8846ca7f1c4abcc1be52664e82da7e1cda81924cca66166c0290b6505b18dec0"


class DispatchPermitError(ValueError):
    """A dispatch authority is absent, stale, malformed, or does not authorize the call."""


@dataclass(frozen=True)
class AuthorityRef:
    path: Path
    sha256: str
    bytes: int


@dataclass(frozen=True)
class DispatchPermitV1:
    kind: str
    call_id: str
    surface_id: str
    manifest_sha256: str
    plan_review_seal_sha256: str
    authority_set_sha256: str = ""
    v: int = 1


@dataclass(frozen=True)
class ProductDispatchRequestV1:
    call_id: str
    task_id: str
    run_id: str
    replicate_id: str
    arm: str
    stage: str
    stage_ordinal: int
    cell_id: str
    attempt_id: str
    transport: str
    surface_id: str
    v: int = 1


@dataclass(frozen=True)
class ProductDispatchRequestV2:
    call_id: str
    task_id: str
    run_id: str
    replicate_id: str
    arm: str
    branch: str
    stage: str
    stage_ordinal: int
    journal_cursor: int
    cell_id: str
    attempt_id: str
    transport: str
    surface_id: str
    v: int = 2


@dataclass(frozen=True)
class ProductDispatchRequestV3:
    call_id: str
    scope: str
    task_id: str
    task_envelope_sha256: str
    run_id: str
    replicate_id: str
    arm: str
    branch: str
    stage: str
    stage_ordinal: int
    journal_cursor: int
    cell_id: str
    attempt_id: str
    transport: str
    surface_id: str
    v: int = 3


@dataclass(frozen=True)
class ProductAttemptDispatchRequestV4:
    call_id: str
    scope: str
    controller: str
    task_id: str
    task_envelope_sha256: str
    run_id: str
    replicate_id: str
    arm: str
    branch: str
    stage: str
    stage_ordinal: int
    journal_cursor: int
    cell_id: str
    attempt_id: str
    transport: str
    surface_id: str
    v: int = 4


@dataclass(frozen=True)
class ProductAttemptDispatchRequestV5:
    call_id: str
    scope: str
    controller: str
    task_id: str
    task_envelope_sha256: str
    root_binding_sha256: str | None
    run_id: str
    replicate_id: str
    arm: str
    branch: str
    stage: str
    stage_ordinal: int
    journal_cursor: int
    cell_id: str
    attempt_id: str
    transport: str
    surface_id: str
    v: int = 5


@dataclass(frozen=True)
class ProductCellDispatchRequestV1:
    call_id: str
    scope: str
    controller: str
    task_id: str
    task_envelope_sha256: str
    run_id: str
    replicate_id: str
    arm: str
    branch: str
    stage: str
    stage_ordinal: int
    journal_cursor: int
    cell_id: str
    attempt_id: None
    transport: str
    surface_id: str
    v: int = 1


@dataclass(frozen=True)
class ProductJournalCursorV1:
    attempt_id: str
    cell_id: str
    call_id: str
    stage_ordinal: int
    journal_generation: int
    prior_state: str
    v: int = 1


@dataclass(frozen=True)
class ProductJournalCursorV2:
    attempt_id: str
    cell_id: str
    call_id: str
    stage_ordinal: int
    journal_generation: int
    prior_state: str
    root_binding_sha256: str | None
    root_binding_generation: int | None
    v: int = 2


@dataclass(frozen=True)
class ProductCellJournalCursorV1:
    attempt_id: None
    cell_id: str
    call_id: str
    stage_ordinal: int
    journal_generation: int
    prior_state: str
    v: int = 1


@dataclass(frozen=True)
class ProductCellAttemptBindingV1:
    cell_id: str
    root_call_id: str
    tool_use_id: str
    attempt_id: str
    task_envelope_sha256: str
    agent_id: None
    generation: int
    kind: str = "rrcv2_product_cell_attempt_binding"
    v: int = 1


@dataclass(frozen=True)
class ProductRootedAttemptAuthorityV1:
    """Journal-owned proof that one started root observed and bound one spawn attempt."""

    cell_id: str
    root_call_id: str
    root_call_state: str
    root_call_generation: int
    root_launch_identity_sha256: str
    tool_use_id: str
    tool_event_sha256: str
    tool_event_state: str
    attempt_id: str
    task_envelope_sha256: str
    binding_sha256: str
    binding_generation: int
    agent_id: None
    kind: str = "rrcv2_product_rooted_attempt_authority"
    v: int = 1


@dataclass(frozen=True)
class ProductRootedAttemptAuthorityV2:
    """Current projection, bound to reopened root-permit/start/launch/spawn rows."""

    cell_id: str
    root_call_id: str
    root_call_state: str
    root_call_generation: int
    root_permit_sha256: str
    root_started_sha256: str
    root_launch_identity_sha256: str
    tool_use_id: str
    tool_event_sha256: str
    tool_event_state: str
    attempt_id: str
    task_envelope_sha256: str
    binding_sha256: str
    binding_generation: int
    agent_id: None
    kind: str = "rrcv2_product_rooted_attempt_authority"
    v: int = 2


@dataclass(frozen=True)
class ProductRootedAttemptJournalViewV1:
    """Exact refs returned by the trusted cell journal for one rooted attempt."""

    authority_ref: AuthorityRef
    root_permit_ref: AuthorityRef
    root_started_ref: AuthorityRef
    root_launch_identity_ref: AuthorityRef
    spawn_event_ref: AuthorityRef
    v: int = 1


class ProductCellJournalAuthorityPort(Protocol):
    """Trusted journal lookup used to prove that a binding came from a started root call."""

    def load_rooted_attempt_authority(
        self, *, cell_id: str, attempt_id: str
    ) -> ProductRootedAttemptJournalViewV1 | None: ...


CALLS: Final[tuple[tuple[int, str, str], ...]] = (
    (1, "cap-01-strong-code-medium", "strong_code_medium"),
    (2, "cap-02-small-code-low", "small_code_low"),
    (3, "cap-03-small-spec-low", "small_spec_low"),
    (4, "cap-04-small-tests-low", "small_tests_low"),
    (5, "cap-05-small-metadata-low", "small_metadata_low"),
    (6, "cap-06-strong-spec-low-root", "strong_spec_low"),
    (7, "cap-07-strong-spec-low-overlap", "strong_spec_low"),
    (8, "cap-08-root-strong-medium-native", "root_strong_medium_native"),
    (9, "cap-09-worker-small-low-native", "worker_small_low_native"),
)
SURFACE_IDS: Final = tuple(sorted({row[2] for row in CALLS}))
HASH_FIELDS: Final = (
    "argv_sha256",
    "config_sha256",
    "tool_schema_sha256",
    "output_schema_sha256",
    "prompt_wrapper_sha256",
    "assembled_byte_definition_sha256",
)


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _hex64(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise DispatchPermitError(f"invalid {field}")
    return value


def read_authority(ref: AuthorityRef, *, require_mode: int | None = 0o600) -> bytes:
    """Bounded/no-follow read of one exact regular-file reference.

    Sealed authorities use the default mode-0600 requirement.  Callers may pass ``None`` only for
    immutable tracked source inputs whose digest is itself bound by a sealed authority.
    """
    _hex64(ref.sha256, "authority sha256")
    if (
        isinstance(ref.bytes, bool)
        or not isinstance(ref.bytes, int)
        or not 0 <= ref.bytes <= AUTHORITY_CAP
    ):
        raise DispatchPermitError("invalid authority byte count")
    try:
        before = ref.path.lstat()
    except OSError as exc:
        raise DispatchPermitError(f"cannot stat authority: {ref.path}") from exc
    if not stat.S_ISREG(before.st_mode) or (
        require_mode is not None and stat.S_IMODE(before.st_mode) != require_mode
    ):
        raise DispatchPermitError("authority must be a regular file with the required mode")
    if before.st_size != ref.bytes or before.st_size > AUTHORITY_CAP:
        raise DispatchPermitError("authority byte count mismatch")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(ref.path, flags)
    except OSError as exc:
        raise DispatchPermitError("cannot safely open authority") from exc
    try:
        current = os.fstat(fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or (require_mode is not None and stat.S_IMODE(current.st_mode) != require_mode)
            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise DispatchPermitError("authority changed while opening")
        chunks: list[bytes] = []
        total = 0
        while total <= AUTHORITY_CAP:
            chunk = os.read(fd, min(65_536, AUTHORITY_CAP + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        if total > AUTHORITY_CAP or (current.st_size, current.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise DispatchPermitError("authority changed or exceeded cap while reading")
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    if len(raw) != ref.bytes or hashlib.sha256(raw).hexdigest() != ref.sha256:
        raise DispatchPermitError("authority hash mismatch")
    return raw


def _canonical_object(raw: bytes, label: str, *, terminal_lf: bool = True) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DispatchPermitError(f"invalid {label}") from exc
    expected = canonical_json(value)
    if not terminal_lf:
        expected = expected[:-1]
    if not isinstance(value, dict) or expected != raw:
        raise DispatchPermitError(f"{label} is not canonical JSON")
    return value


def _toml_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = tomllib.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise DispatchPermitError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise DispatchPermitError(f"invalid {label}")
    return value


def _requested_role(surface_id: str) -> tuple[str, str]:
    if surface_id in {"strong_code_medium", "root_strong_medium_native"}:
        return ("gpt-5.5", "medium")
    if surface_id == "strong_spec_low":
        return ("gpt-5.5", "low")
    return ("gpt-5.6-luna", "low")


def capability_manifest(
    *,
    pre_capability_plan_review_seal_sha256: str,
    cli_binary_sha256: str,
    surface_hashes: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    """Construct the one exact capability manifest from reviewed constants and byte hashes."""

    _hex64(pre_capability_plan_review_seal_sha256, "plan review seal sha256")
    _hex64(cli_binary_sha256, "CLI binary sha256")
    if set(surface_hashes) != set(SURFACE_IDS):
        raise DispatchPermitError("surface hash map differs from the frozen surface set")
    surfaces: list[dict[str, object]] = []
    for surface_id in SURFACE_IDS:
        hashes = surface_hashes[surface_id]
        if set(hashes) != set(HASH_FIELDS):
            raise DispatchPermitError(f"invalid hash set for {surface_id}")
        model, reasoning = _requested_role(surface_id)
        row: dict[str, object] = {
            "surface_id": surface_id,
            "requested_provider": PROVIDER,
            "requested_model": model,
            "requested_reasoning": reasoning,
            "requested_service_tier": SERVICE_TIER,
            "cli_binary_sha256": cli_binary_sha256,
            "cli_version": CLI_VERSION,
        }
        for field in HASH_FIELDS:
            row[field] = _hex64(hashes[field], f"{surface_id}.{field}")
        surfaces.append(row)
    return {
        "v": 1,
        "kind": "rrcv2_capability_manifest",
        "pre_capability_plan_review_seal_sha256": pre_capability_plan_review_seal_sha256,
        "calls": [
            {"ordinal": ordinal, "call_id": call_id, "surface_id": surface_id}
            for ordinal, call_id, surface_id in CALLS
        ],
        "surfaces": surfaces,
    }


def _validate_plan_seal(value: dict[str, Any]) -> None:
    required = {
        "v",
        "kind",
        "verdict",
        "origin",
        "mode",
        "repo_root",
        "raw_plan_sha256",
        "record_sha256",
        "record_subject_hash",
        "transcript_sha256",
        "plan_path",
        "record_path",
        "transcript_path",
    }
    if set(value) != required:
        raise DispatchPermitError("plan review seal has unknown or missing fields")
    if (
        value["v"] != 1
        or isinstance(value["v"], bool)
        or value["kind"] != "rrcv2_raw_plan_review_seal"
        or value["verdict"] != "SHIP"
        or value["origin"] != "forked"
        or value["mode"] != "unleashed"
    ):
        raise DispatchPermitError("plan review seal is not a root-routed SHIP authority")
    for field in ("raw_plan_sha256", "record_sha256", "record_subject_hash", "transcript_sha256"):
        _hex64(value[field], f"plan seal {field}")
    root = Path(value["repo_root"])
    if not root.is_absolute() or Path(value["plan_path"]) != root / "PLAN.md":
        raise DispatchPermitError("plan review seal has a wrong repository root")


def authorize_capability(
    *,
    call_id: str,
    surface_id: str,
    manifest_ref: AuthorityRef,
    plan_review_seal_ref: AuthorityRef,
    expected_manifest: Mapping[str, object],
) -> DispatchPermitV1:
    """Return a permit only for one exact externally sealed capability call/surface pair."""

    plan = _canonical_object(read_authority(plan_review_seal_ref), "plan review seal")
    _validate_plan_seal(plan)
    manifest = _canonical_object(read_authority(manifest_ref), "capability manifest")
    if canonical_json(expected_manifest) != canonical_json(manifest):
        raise DispatchPermitError("capability manifest differs from reviewed construction")
    if set(manifest) != {
        "v",
        "kind",
        "pre_capability_plan_review_seal_sha256",
        "calls",
        "surfaces",
    }:
        raise DispatchPermitError("capability manifest has unknown or missing fields")
    if manifest.get("v") != 1 or manifest.get("kind") != "rrcv2_capability_manifest":
        raise DispatchPermitError("unsupported capability manifest")
    if manifest.get("pre_capability_plan_review_seal_sha256") != plan_review_seal_ref.sha256:
        raise DispatchPermitError("capability manifest is bound to another plan review")
    expected_pair = next(
        (
            (registered_call, registered_surface)
            for _, registered_call, registered_surface in CALLS
            if registered_call == call_id
        ),
        None,
    )
    if expected_pair != (call_id, surface_id):
        raise DispatchPermitError("call ID and surface ID are not one frozen capability pair")
    calls = manifest.get("calls")
    exact_calls = [
        {"ordinal": ordinal, "call_id": registered_call, "surface_id": registered_surface}
        for ordinal, registered_call, registered_surface in CALLS
    ]
    if calls != exact_calls:
        raise DispatchPermitError("capability call registry differs from the frozen registry")
    surface_rows = manifest.get("surfaces")
    if not isinstance(surface_rows, list) or [
        row.get("surface_id") for row in surface_rows if isinstance(row, dict)
    ] != list(SURFACE_IDS):
        raise DispatchPermitError("capability surfaces are missing, extra, or reordered")
    return DispatchPermitV1(
        kind="capability",
        call_id=call_id,
        surface_id=surface_id,
        manifest_sha256=manifest_ref.sha256,
        plan_review_seal_sha256=plan_review_seal_ref.sha256,
    )


PRODUCT_CONFIG_FIELDS: Final = {
    "v",
    "kind",
    "economic_authority_file_sha256",
    "dispatch_permit_file_sha256",
    "capability_matrix_file_sha256",
    "sandbox_probe_file_sha256",
}
OUTPUT_AUTHORITY_V2_FIELDS: Final = {
    "v",
    "kind",
    "producer",
    "producer_version",
    "immutable_preimage_manifest_sha256",
    "supersedes_sha256",
    "closed_path_grammar",
    "expected_type",
    "expected_mode",
}
OUTPUT_AUTHORITY_V2_GRAMMAR: Final = [
    ".generated/state/rrcv2-convergence/capability/apfs-evidence.v1.json",
    ".generated/state/rrcv2-convergence/capability/backend-ownership.v1.json",
    ".generated/state/rrcv2-convergence/capability/capability-manifest.v1.json",
    ".generated/state/rrcv2-convergence/capability/capability-summary.v1.json",
    ".generated/state/rrcv2-convergence/capability/calls/cap-*/**",
    ".generated/state/rrcv2-convergence/capability/docker-evidence.v1.json",
    ".generated/state/rrcv2-convergence/capability/sandbox-evidence.v1.json",
    ".generated/state/rrcv2-convergence/economic/economic-binding-overlay.v7.json",
    ".generated/state/rrcv2-convergence/economic/economic-binding-overlay.v8.json",
    ".generated/state/rrcv2-convergence/economic/economic-binding-overlay.v9.json",
    ".generated/state/rrcv2-convergence/economic/workload-core.v6.json",
    ".generated/state/rrcv2-convergence/execution.jsonl",
    ".generated/state/rrcv2-convergence/verify/**",
    "contextmesh/docker/rrcv2-verifier.lock.json",
]
OUTPUT_AUTHORITY_V3_GRAMMAR: Final = [
    *OUTPUT_AUTHORITY_V2_GRAMMAR[:-4],
    ".generated/state/rrcv2-convergence/economic/economic-binding-overlay.v10.json",
    *OUTPUT_AUTHORITY_V2_GRAMMAR[-4:],
]

PRODUCT_STAGE_MATRIX: Final[dict[tuple[str, str, str, str, int], tuple[str, int]]] = {
    ("direct", "baseline", "direct", "baseline", 1): ("strong_code_medium", 1),
    ("direct", "cheap_alone", "direct", "cheap_alone", 1): ("small_code_low", 1),
    ("direct", "cheap_alone", "direct", "cheap_alone", 2): ("small_code_low", 2),
    ("direct", "cheap_alone", "direct", "cheap_alone", 3): ("small_code_low", 3),
    ("direct", "cascade", "direct", "cascade_cheap", 1): ("small_code_low", 1),
    ("direct", "cascade", "direct", "cascade_cheap", 2): ("small_code_low", 2),
    ("direct", "cascade", "direct", "cascade_cheap", 3): ("small_code_low", 3),
    ("direct", "cascade", "direct", "cascade_strong", 4): ("strong_code_medium", 4),
    ("direct", "rrc_cold", "miss", "spec", 1): ("strong_spec_low", 1),
    ("direct", "rrc_cold", "miss", "independent_tests", 2): ("small_tests_low", 2),
    ("direct", "rrc_cold", "miss", "implement", 3): ("small_code_low", 3),
    ("direct", "rrc_cold", "miss", "repair_1", 4): ("small_code_low", 4),
    ("direct", "rrc_cold", "miss", "repair_2", 5): ("small_code_low", 5),
    ("direct", "rrc_cold", "miss", "fallback_spec", 6): ("strong_spec_low", 6),
    ("direct", "rrc_cold", "miss", "fallback_independent_tests", 7): (
        "small_tests_low",
        7,
    ),
    ("direct", "rrc_cold", "miss", "fallback_implement", 8): ("small_code_low", 8),
    ("direct", "rrc_warm", "pre_retrieval", "metadata_fill", 1): (
        "small_metadata_low",
        1,
    ),
    ("direct", "rrc_warm", "miss", "spec", 2): ("strong_spec_low", 2),
    ("direct", "rrc_warm", "miss", "independent_tests", 3): ("small_tests_low", 3),
    ("direct", "rrc_warm", "miss", "implement", 4): ("small_code_low", 4),
    ("direct", "rrc_warm", "miss", "repair_1", 5): ("small_code_low", 5),
    ("direct", "rrc_warm", "miss", "repair_2", 6): ("small_code_low", 6),
    ("direct", "rrc_warm", "miss", "fallback_spec", 7): ("strong_spec_low", 7),
    ("direct", "rrc_warm", "miss", "fallback_independent_tests", 8): (
        "small_tests_low",
        8,
    ),
    ("direct", "rrc_warm", "miss", "fallback_implement", 9): ("small_code_low", 9),
    ("direct", "rrc_warm", "reuse", "implement", 2): ("small_code_low", 2),
    ("direct", "rrc_warm", "reuse", "repair_1", 3): ("small_code_low", 3),
    ("direct", "rrc_warm", "reuse", "repair_2", 4): ("small_code_low", 4),
    ("direct", "rrc_warm", "reuse", "fallback_spec", 5): ("strong_spec_low", 5),
    ("direct", "rrc_warm", "reuse", "fallback_independent_tests", 6): (
        "small_tests_low",
        6,
    ),
    ("direct", "rrc_warm", "reuse", "fallback_implement", 7): ("small_code_low", 7),
    ("direct", "rrc_warm", "prime", "prime", 2): ("small_spec_low", 2),
    ("direct", "rrc_warm", "prime", "independent_tests", 3): ("small_tests_low", 3),
    ("direct", "rrc_warm", "prime", "implement", 4): ("small_code_low", 4),
    ("direct", "rrc_warm", "prime", "repair_1", 5): ("small_code_low", 5),
    ("direct", "rrc_warm", "prime", "repair_2", 6): ("small_code_low", 6),
    ("direct", "rrc_warm", "prime", "fallback_spec", 7): ("strong_spec_low", 7),
    ("direct", "rrc_warm", "prime", "fallback_independent_tests", 8): (
        "small_tests_low",
        8,
    ),
    ("direct", "rrc_warm", "prime", "fallback_implement", 9): ("small_code_low", 9),
    ("contextmesh", "rrc_cold", "combined", "contextmesh_root_session", 1): (
        "root_strong_medium_native",
        1,
    ),
    ("contextmesh", "rrc_warm", "combined", "contextmesh_root_session", 1): (
        "root_strong_medium_native",
        1,
    ),
}

for _key, (_surface, _cursor) in tuple(PRODUCT_STAGE_MATRIX.items()):
    _transport, _arm, _branch, _stage, _ordinal = _key
    if _transport == "direct" and _arm in {"rrc_cold", "rrc_warm"} and _stage == "implement":
        PRODUCT_STAGE_MATRIX[("contextmesh", _arm, _branch, _stage, _ordinal)] = (
            "worker_small_low_native",
            _cursor,
        )


def _controller_matrix() -> frozenset[tuple[str, str, str, str, str, str, int, str, int, str]]:
    rows: set[tuple[str, str, str, str, str, str, int, str, int, str]] = set()
    for scope in ("experiment", "interactive"):
        for controller in ("direct", "contextmesh"):
            for (transport, arm, branch, stage, ordinal), (
                surface,
                cursor,
            ) in PRODUCT_STAGE_MATRIX.items():
                rrc_arm = arm in {"rrc_cold", "rrc_warm"}
                if scope == "interactive" and not rrc_arm:
                    continue
                if controller == "direct":
                    allowed = transport == "direct"
                else:
                    allowed = rrc_arm and (
                        transport == "contextmesh" or transport == "direct" and stage != "implement"
                    )
                if allowed:
                    rows.add(
                        (
                            scope,
                            controller,
                            transport,
                            arm,
                            branch,
                            stage,
                            ordinal,
                            surface,
                            cursor,
                            "cell" if stage == "contextmesh_root_session" else "attempt",
                        )
                    )
    return frozenset(rows)


PRODUCT_CONTROLLER_MATRIX: Final = _controller_matrix()
EXPECTED_ARM_STAGE_MODELS: Final = {
    "baseline": {"whole_task": "strong_baseline"},
    "cheap_alone": {"whole_task": "small"},
    "cascade": {"first": "small", "fallback": "strong_baseline"},
    "rrc_cold": {
        "spec": "strong_spec",
        "fallback_spec": "strong_spec",
        "independent_tests": "small",
        "implement": "small",
        "repair": "small",
    },
    "rrc_warm": {
        "spec": "strong_spec",
        "fallback_spec": "strong_spec",
        "metadata_fill": "small",
        "prime": "small",
        "independent_tests": "small",
        "implement": "small",
        "repair": "small",
    },
}


def product_call_id(
    request: ProductAttemptDispatchRequestV5 | ProductCellDispatchRequestV1,
) -> str:
    """Derive the unique journal/provider ID from every dispatch identity field except itself."""

    preimage = {
        "v": request.v,
        "kind": (
            "rrcv2_product_cell_dispatch"
            if isinstance(request, ProductCellDispatchRequestV1)
            else "rrcv2_product_attempt_dispatch"
        ),
        "scope": request.scope,
        "controller": request.controller,
        "task_id": request.task_id,
        "task_envelope_sha256": request.task_envelope_sha256,
        "root_binding_sha256": (
            None
            if isinstance(request, ProductCellDispatchRequestV1)
            else request.root_binding_sha256
        ),
        "run_id": request.run_id,
        "replicate_id": request.replicate_id,
        "arm": request.arm,
        "branch": request.branch,
        "stage": request.stage,
        "stage_ordinal": request.stage_ordinal,
        "journal_cursor": request.journal_cursor,
        "cell_id": request.cell_id,
        "attempt_id": request.attempt_id,
        "transport": request.transport,
        "surface_id": request.surface_id,
    }
    return hashlib.sha256(canonical_json(preimage)).hexdigest()


def product_config_manifest(
    *,
    economic_authority_file_sha256: str,
    dispatch_permit_file_sha256: str,
    capability_matrix_file_sha256: str,
    sandbox_probe_file_sha256: str,
) -> dict[str, object]:
    """Construct the exact inactive M0 assembler/config byte authority."""

    return {
        "v": 15,
        "kind": "rrcv2_product_config_manifest",
        "economic_authority_file_sha256": _hex64(
            economic_authority_file_sha256, "economic authority file sha256"
        ),
        "dispatch_permit_file_sha256": _hex64(
            dispatch_permit_file_sha256, "dispatch permit file sha256"
        ),
        "capability_matrix_file_sha256": _hex64(
            capability_matrix_file_sha256, "capability matrix file sha256"
        ),
        "sandbox_probe_file_sha256": _hex64(sandbox_probe_file_sha256, "sandbox probe file sha256"),
    }


def generated_output_authority_v2(*, supersedes_sha256: str) -> dict[str, object]:
    """Construct the class-(g) authority used by the final product permit.

    Version 1 remains immutable historical evidence. Version 2 is created under v1's
    ``verify/**`` allowance before the final v9 overlay and explicitly supersedes v1.
    """

    return {
        "v": 2,
        "kind": "rrcv2_generated_output_authority",
        "producer": "contextmesh/scripts/rrcv2_economic_overlay.py",
        "producer_version": 2,
        "immutable_preimage_manifest_sha256": BASELINE_SEAL_SHA256,
        "supersedes_sha256": _hex64(supersedes_sha256, "superseded output authority sha256"),
        "closed_path_grammar": OUTPUT_AUTHORITY_V2_GRAMMAR,
        "expected_type": "regular",
        "expected_mode": 0o600,
    }


def validate_generated_output_authority_v2(
    *, authority_ref: AuthorityRef, superseded_ref: AuthorityRef
) -> dict[str, object]:
    """Reopen and validate the exact v2 authority and immutable v1 predecessor."""

    superseded_raw = read_authority(superseded_ref)
    value = _canonical_object(read_authority(authority_ref), "generated output authority v2")
    if set(value) != OUTPUT_AUTHORITY_V2_FIELDS:
        raise DispatchPermitError("generated output authority v2 has unknown or missing fields")
    expected = generated_output_authority_v2(
        supersedes_sha256=hashlib.sha256(superseded_raw).hexdigest()
    )
    if value != expected:
        raise DispatchPermitError("generated output authority v2 differs from the sealed contract")
    return expected


def generated_output_authority_v3(*, supersedes_sha256: str) -> dict[str, object]:
    """Construct the final authority after M0 accounting/attestation convergence."""

    value = generated_output_authority_v2(supersedes_sha256=supersedes_sha256)
    value["v"] = 3
    value["producer_version"] = 3
    value["closed_path_grammar"] = OUTPUT_AUTHORITY_V3_GRAMMAR
    return value


def validate_generated_output_authority_v3(
    *,
    authority_ref: AuthorityRef,
    superseded_v2_ref: AuthorityRef,
    superseded_v1_ref: AuthorityRef,
) -> dict[str, object]:
    """Validate v3 plus the complete immutable v2->v1 supersession chain."""

    validate_generated_output_authority_v2(
        authority_ref=superseded_v2_ref, superseded_ref=superseded_v1_ref
    )
    value = _canonical_object(read_authority(authority_ref), "generated output authority v3")
    if set(value) != OUTPUT_AUTHORITY_V2_FIELDS:
        raise DispatchPermitError("generated output authority v3 has unknown or missing fields")
    expected = generated_output_authority_v3(supersedes_sha256=superseded_v2_ref.sha256)
    if value != expected:
        raise DispatchPermitError("generated output authority v3 differs from the sealed contract")
    return expected


def _validate_review_record(
    value: Mapping[str, Any], *, kind: str, scope: str, transcript_sha256: str, repo_root: Path
) -> None:
    required = {
        "kind",
        "scope",
        "verdict",
        "subject_hash",
        "transcript_hash",
        "origin",
        "mode",
        "audience_entry_id",
        "persona",
        "recorded_at",
        "repo_root",
        "workspace_session",
        "workspace_runtime_root",
    }
    if set(value) != required:
        raise DispatchPermitError("review record has unknown or missing fields")
    if (
        value.get("kind") != kind
        or value.get("scope") != scope
        or value.get("verdict") != "SHIP"
        or value.get("origin") != "forked"
        or value.get("mode") != "unleashed"
        or value.get("workspace_session") != ""
        or value.get("workspace_runtime_root") != ""
        or value.get("repo_root") != str(repo_root)
        or value.get("transcript_hash") != transcript_sha256
    ):
        raise DispatchPermitError("review record is not the required root-routed SHIP authority")
    _hex64(value.get("subject_hash"), "review subject_hash")


def _surface_role(surface_id: str) -> tuple[str, str]:
    if surface_id not in SURFACE_IDS:
        raise DispatchPermitError("product surface is not capability-proven")
    return _requested_role(surface_id)


TASK_ENVELOPE_FIELDS: Final = {
    "v",
    "task",
    "source_ref",
    "public_test_ref",
    "oracle_ref",
    "target_preimage",
    "shape",
    "slot_values",
}
TASK_ENVELOPE_TASK_FIELDS: Final = {
    "task_id",
    "text",
    "family",
    "artifact_path",
    "searchable_public",
    "verification_profile",
    "primary",
}
TASK_ENVELOPE_REF_FIELDS: Final = {"sha256", "bytes", "path"}


def _validate_envelope_ref(value: object, name: str, *, nullable: bool) -> dict[str, object] | None:
    if value is None and nullable:
        return None
    if not isinstance(value, dict) or set(value) != TASK_ENVELOPE_REF_FIELDS:
        raise DispatchPermitError(f"{name} is not one closed task-envelope reference")
    _hex64(value.get("sha256"), f"{name} sha256")
    size = value.get("bytes")
    path = value.get("path")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or not isinstance(path, str)
        or not path
        or len(path.encode("utf-8")) > 512
        or path.startswith("/")
        or "\x00" in path
        or ".." in Path(path).parts
    ):
        raise DispatchPermitError(f"{name} has an invalid size or path")
    return value


def _validate_task_envelope(
    task_envelope_ref: AuthorityRef,
    request: ProductAttemptDispatchRequestV5 | ProductCellDispatchRequestV1,
) -> dict[str, object]:
    if request.task_envelope_sha256 != task_envelope_ref.sha256:
        raise DispatchPermitError("product request does not bind the task envelope")
    # TaskEnvelopeV1 is owned by rrc.contract.canonical_json_bytes and therefore
    # has no file-format terminal LF.  Other sealed dispatch authorities retain
    # this module's canonical-JSON-line convention.
    raw = read_authority(task_envelope_ref)
    try:
        parse_task_envelope(raw)
    except (TypeError, ValueError) as exc:
        raise DispatchPermitError("task envelope is not canonical JSON") from exc
    value = _canonical_object(raw, "task envelope", terminal_lf=False)
    if set(value) != TASK_ENVELOPE_FIELDS or value.get("v") != 1:
        raise DispatchPermitError("task envelope has unknown or missing fields")
    task = value.get("task")
    if not isinstance(task, dict) or set(task) != TASK_ENVELOPE_TASK_FIELDS:
        raise DispatchPermitError("task envelope task has unknown or missing fields")
    if task.get("task_id") != request.task_id:
        raise DispatchPermitError("task envelope and dispatch task IDs differ")
    for field in ("task_id", "text", "artifact_path", "verification_profile"):
        field_value = task.get(field)
        if (
            not isinstance(field_value, str)
            or not field_value
            or "\x00" in field_value
            or len(field_value.encode("utf-8")) > 256 * 1024
        ):
            raise DispatchPermitError(f"task envelope has invalid {field}")
    if task.get("verification_profile") not in {"rrcv2_general_v1", "rrcv2_synthetic_v1"}:
        raise DispatchPermitError("task envelope verification profile is not frozen")
    if not isinstance(task.get("searchable_public"), bool):
        raise DispatchPermitError("task envelope searchable_public must be boolean")
    for field in ("family", "primary"):
        if task.get(field) is not None and not isinstance(task.get(field), str):
            raise DispatchPermitError(f"task envelope has invalid {field}")
    _validate_envelope_ref(value.get("source_ref"), "source_ref", nullable=True)
    _validate_envelope_ref(value.get("public_test_ref"), "public_test_ref", nullable=False)
    _validate_envelope_ref(value.get("oracle_ref"), "oracle_ref", nullable=True)
    target = value.get("target_preimage")
    if not isinstance(target, dict) or target.get("v") != 1:
        raise DispatchPermitError("task envelope target preimage is invalid")
    target_kind = target.get("kind")
    if not isinstance(target_kind, str):
        raise DispatchPermitError("task envelope target preimage kind is invalid")
    expected_target_fields = {
        "none": {"v", "kind"},
        "absent": {"v", "kind", "path", "mode"},
        "regular": {"v", "kind", "path", "sha256", "bytes", "mode"},
    }.get(target_kind)
    if expected_target_fields is None or set(target) != expected_target_fields:
        raise DispatchPermitError("task envelope target preimage is not one closed variant")
    return value


CELL_ATTEMPT_BINDING_FIELDS: Final = {
    "v",
    "kind",
    "cell_id",
    "root_call_id",
    "tool_use_id",
    "attempt_id",
    "task_envelope_sha256",
    "agent_id",
    "generation",
}
ROOTED_ATTEMPT_AUTHORITY_FIELDS: Final = {
    "v",
    "kind",
    "cell_id",
    "root_call_id",
    "root_call_state",
    "root_call_generation",
    "root_permit_sha256",
    "root_started_sha256",
    "root_launch_identity_sha256",
    "tool_use_id",
    "tool_event_sha256",
    "tool_event_state",
    "attempt_id",
    "task_envelope_sha256",
    "binding_sha256",
    "binding_generation",
    "agent_id",
}
ROOT_PERMIT_FIELDS: Final = {
    "v",
    "kind",
    "call_id",
    "surface_id",
    "manifest_sha256",
    "plan_review_seal_sha256",
    "authority_set_sha256",
}
ROOT_STARTED_FIELDS: Final = {
    "v",
    "kind",
    "cell_id",
    "root_call_id",
    "state",
    "generation",
    "root_permit_sha256",
    "root_launch_identity_sha256",
}
ROOT_LAUNCH_IDENTITY_FIELDS: Final = {
    "v",
    "kind",
    "cell_id",
    "root_call_id",
    "session_id",
    "transcript_baseline_sha256",
}
SPAWN_EVENT_FIELDS: Final = {
    "v",
    "kind",
    "cell_id",
    "root_call_id",
    "root_call_generation",
    "tool_use_id",
    "attempt_id",
    "task_envelope_sha256",
    "binding_generation",
    "state",
}


def cell_attempt_binding_value(binding: ProductCellAttemptBindingV1) -> dict[str, object]:
    """Serialize the immutable pre-spawn cell-to-attempt binding."""

    return {
        "v": binding.v,
        "kind": binding.kind,
        "cell_id": binding.cell_id,
        "root_call_id": binding.root_call_id,
        "tool_use_id": binding.tool_use_id,
        "attempt_id": binding.attempt_id,
        "task_envelope_sha256": binding.task_envelope_sha256,
        "agent_id": binding.agent_id,
        "generation": binding.generation,
    }


def rooted_attempt_authority_value(
    authority: ProductRootedAttemptAuthorityV1 | ProductRootedAttemptAuthorityV2,
) -> dict[str, object]:
    """Serialize the journal-owned root-start/spawn binding authority."""

    value: dict[str, object] = {
        "v": authority.v,
        "kind": authority.kind,
        "cell_id": authority.cell_id,
        "root_call_id": authority.root_call_id,
        "root_call_state": authority.root_call_state,
        "root_call_generation": authority.root_call_generation,
        "root_launch_identity_sha256": authority.root_launch_identity_sha256,
        "tool_use_id": authority.tool_use_id,
        "tool_event_sha256": authority.tool_event_sha256,
        "tool_event_state": authority.tool_event_state,
        "attempt_id": authority.attempt_id,
        "task_envelope_sha256": authority.task_envelope_sha256,
        "binding_sha256": authority.binding_sha256,
        "binding_generation": authority.binding_generation,
        "agent_id": authority.agent_id,
    }
    if isinstance(authority, ProductRootedAttemptAuthorityV2):
        value["root_permit_sha256"] = authority.root_permit_sha256
        value["root_started_sha256"] = authority.root_started_sha256
    return value


def dispatch_permit_value(permit: DispatchPermitV1) -> dict[str, object]:
    """Serialize one provider permit for durable journal correlation."""

    return {
        "v": permit.v,
        "kind": permit.kind,
        "call_id": permit.call_id,
        "surface_id": permit.surface_id,
        "manifest_sha256": permit.manifest_sha256,
        "plan_review_seal_sha256": permit.plan_review_seal_sha256,
        "authority_set_sha256": permit.authority_set_sha256,
    }


def _validate_cell_attempt_binding(
    binding_ref: AuthorityRef,
    request: ProductAttemptDispatchRequestV5,
    cell_journal_authority: ProductCellJournalAuthorityPort,
) -> tuple[int, AuthorityRef]:
    if request.root_binding_sha256 != binding_ref.sha256:
        raise DispatchPermitError("ContextMesh request does not bind its cell-attempt authority")
    value = _canonical_object(read_authority(binding_ref), "cell-attempt binding")
    if set(value) != CELL_ATTEMPT_BINDING_FIELDS:
        raise DispatchPermitError("cell-attempt binding has unknown or missing fields")
    root = ProductCellDispatchRequestV1(
        call_id="",
        scope=request.scope,
        controller="contextmesh",
        task_id=request.task_id,
        task_envelope_sha256=request.task_envelope_sha256,
        run_id=request.run_id,
        replicate_id=request.replicate_id,
        arm=request.arm,
        branch="combined",
        stage="contextmesh_root_session",
        stage_ordinal=1,
        journal_cursor=1,
        cell_id=request.cell_id,
        attempt_id=None,
        transport="contextmesh",
        surface_id="root_strong_medium_native",
    )
    root = replace(root, call_id=product_call_id(root))
    generation = value.get("generation")
    tool_use_id = value.get("tool_use_id")
    if (
        value.get("v") != 1
        or value.get("kind") != "rrcv2_product_cell_attempt_binding"
        or value.get("cell_id") != request.cell_id
        or value.get("root_call_id") != root.call_id
        or not isinstance(tool_use_id, str)
        or not tool_use_id
        or len(tool_use_id.encode("utf-8")) > 256
        or "\x00" in tool_use_id
        or value.get("attempt_id") != request.attempt_id
        or value.get("task_envelope_sha256") != request.task_envelope_sha256
        or value.get("agent_id") is not None
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
    ):
        raise DispatchPermitError("cell-attempt binding differs from the rooted spawn authority")
    journal_view = cell_journal_authority.load_rooted_attempt_authority(
        cell_id=request.cell_id, attempt_id=request.attempt_id
    )
    if journal_view is None:
        raise DispatchPermitError("ContextMesh attempt has no journal-owned root-start authority")
    if journal_view.v != 1:
        raise DispatchPermitError("unsupported rooted-attempt journal view")
    rooted = _canonical_object(
        read_authority(journal_view.authority_ref), "rooted-attempt authority"
    )
    root_permit = _canonical_object(
        read_authority(journal_view.root_permit_ref), "root permit receipt"
    )
    root_started = _canonical_object(
        read_authority(journal_view.root_started_ref), "root-started row"
    )
    launch_identity = _canonical_object(
        read_authority(journal_view.root_launch_identity_ref), "root launch identity"
    )
    spawn_event = _canonical_object(
        read_authority(journal_view.spawn_event_ref), "spawn-observed event"
    )
    if set(rooted) != ROOTED_ATTEMPT_AUTHORITY_FIELDS:
        raise DispatchPermitError("rooted-attempt authority has unknown or missing fields")
    if (
        set(root_permit) != ROOT_PERMIT_FIELDS
        or set(root_started) != ROOT_STARTED_FIELDS
        or set(launch_identity) != ROOT_LAUNCH_IDENTITY_FIELDS
        or set(spawn_event) != SPAWN_EVENT_FIELDS
    ):
        raise DispatchPermitError("rooted-attempt journal rows have unknown or missing fields")
    root_generation = rooted.get("root_call_generation")
    binding_authority_generation = rooted.get("binding_generation")
    if (
        rooted.get("v") != 2
        or rooted.get("kind") != "rrcv2_product_rooted_attempt_authority"
        or rooted.get("cell_id") != request.cell_id
        or rooted.get("root_call_id") != root.call_id
        or rooted.get("root_call_state") != "started"
        or isinstance(root_generation, bool)
        or not isinstance(root_generation, int)
        or root_generation != generation + 1
        or rooted.get("root_permit_sha256") != journal_view.root_permit_ref.sha256
        or rooted.get("root_started_sha256") != journal_view.root_started_ref.sha256
        or rooted.get("root_launch_identity_sha256") != journal_view.root_launch_identity_ref.sha256
        or rooted.get("tool_event_sha256") != journal_view.spawn_event_ref.sha256
        or rooted.get("tool_use_id") != tool_use_id
        or rooted.get("tool_event_state") != "spawn_observed"
        or rooted.get("attempt_id") != request.attempt_id
        or rooted.get("task_envelope_sha256") != request.task_envelope_sha256
        or rooted.get("binding_sha256") != binding_ref.sha256
        or binding_authority_generation != generation
        or rooted.get("agent_id") is not None
        or root_permit.get("v") != 1
        or root_permit.get("kind") != "product"
        or root_permit.get("call_id") != root.call_id
        or root_permit.get("surface_id") != "root_strong_medium_native"
        or root_started
        != {
            "v": 1,
            "kind": "rrcv2_product_root_call_started",
            "cell_id": request.cell_id,
            "root_call_id": root.call_id,
            "state": "started",
            "generation": root_generation,
            "root_permit_sha256": journal_view.root_permit_ref.sha256,
            "root_launch_identity_sha256": journal_view.root_launch_identity_ref.sha256,
        }
        or launch_identity.get("v") != 1
        or launch_identity.get("kind") != "rrcv2_product_root_launch_identity"
        or launch_identity.get("cell_id") != request.cell_id
        or launch_identity.get("root_call_id") != root.call_id
        or not isinstance(launch_identity.get("session_id"), str)
        or not launch_identity.get("session_id")
        or spawn_event
        != {
            "v": 1,
            "kind": "rrcv2_contextmesh_spawn_observed",
            "cell_id": request.cell_id,
            "root_call_id": root.call_id,
            "root_call_generation": root_generation,
            "tool_use_id": tool_use_id,
            "attempt_id": request.attempt_id,
            "task_envelope_sha256": request.task_envelope_sha256,
            "binding_generation": generation,
            "state": "spawn_observed",
        }
    ):
        raise DispatchPermitError("journal authority does not prove the rooted spawn binding")
    for field in ("manifest_sha256", "plan_review_seal_sha256", "authority_set_sha256"):
        _hex64(root_permit[field], f"root permit {field}")
    _hex64(launch_identity["transcript_baseline_sha256"], "root transcript baseline sha256")
    return generation, journal_view.authority_ref


def authorize_product(
    *,
    request: ProductAttemptDispatchRequestV5 | ProductCellDispatchRequestV1,
    journal_cursor: ProductJournalCursorV2 | ProductCellJournalCursorV1,
    task_envelope_ref: AuthorityRef,
    cell_attempt_binding_ref: AuthorityRef | None,
    cell_journal_authority: ProductCellJournalAuthorityPort | None,
    repo_root: Path,
    workload_core_ref: AuthorityRef,
    overlay_ref: AuthorityRef,
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
    plan_review_seal_ref: AuthorityRef,
    plan_ref: AuthorityRef,
    plan_transcript_ref: AuthorityRef,
    plan_record_ref: AuthorityRef,
    claim_transcript_ref: AuthorityRef,
    claim_record_ref: AuthorityRef,
    config_manifest_ref: AuthorityRef,
    economic_authority_file_ref: AuthorityRef,
    dispatch_permit_file_ref: AuthorityRef,
    capability_matrix_file_ref: AuthorityRef,
    sandbox_probe_file_ref: AuthorityRef,
) -> DispatchPermitV1:
    """Authorize one functional-only product call from the complete post-capability authority set."""

    # Local imports avoid making the capability-only path depend on economic protocol code.
    from rrc.economic_authority import EconomicAuthorityError, validate_economic_binding_overlay_v10

    repo_root = repo_root.absolute()
    plan_seal = _canonical_object(read_authority(plan_review_seal_ref), "plan review seal")
    _validate_plan_seal(plan_seal)
    if Path(str(plan_seal.get("repo_root", ""))).absolute() != repo_root:
        raise DispatchPermitError("plan review belongs to another repository")
    plan_raw = read_authority(plan_ref, require_mode=None)
    plan_transcript_raw = read_authority(plan_transcript_ref)
    plan_record_raw = read_authority(plan_record_ref, require_mode=None)
    if (
        plan_seal.get("raw_plan_sha256") != hashlib.sha256(plan_raw).hexdigest()
        or plan_seal.get("transcript_sha256") != hashlib.sha256(plan_transcript_raw).hexdigest()
        or plan_seal.get("record_sha256") != hashlib.sha256(plan_record_raw).hexdigest()
    ):
        raise DispatchPermitError("plan review seal is stale")
    plan_record = _toml_object(plan_record_raw, "plan review record")
    _validate_review_record(
        plan_record,
        kind="plan",
        scope="plan",
        transcript_sha256=plan_transcript_ref.sha256,
        repo_root=repo_root,
    )
    if plan_record.get("subject_hash") != plan_seal.get("record_subject_hash"):
        raise DispatchPermitError("plan seal and record subject disagree")

    claim_transcript_raw = read_authority(claim_transcript_ref)
    try:
        claim_text = claim_transcript_raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DispatchPermitError("claim transcript is not UTF-8") from exc
    if not claim_text.startswith("CLAIM: SUPPORTED\n"):
        raise DispatchPermitError("claim transcript is not SUPPORTED")
    claim_record = _toml_object(
        read_authority(claim_record_ref, require_mode=None), "claim review record"
    )
    _validate_review_record(
        claim_record,
        kind="claim",
        scope="worktree",
        transcript_sha256=claim_transcript_ref.sha256,
        repo_root=repo_root,
    )

    core_raw = read_authority(workload_core_ref)
    _canonical_object(core_raw, "workload core")
    overlay_raw = read_authority(overlay_ref)
    overlay = _canonical_object(overlay_raw, "economic overlay")
    try:
        validate_economic_binding_overlay_v10(
            overlay,
            workload_core_ref=workload_core_ref,
            source_workload_ref=source_workload_ref,
            source_oracles_ref=source_oracles_ref,
            capability_manifest_ref=capability_manifest_ref,
            capability_summary_ref=capability_summary_ref,
            capability_evidence_inventory_ref=capability_evidence_inventory_ref,
            sandbox_evidence_v2_ref=sandbox_evidence_v2_ref,
            setup_accounting_ref=setup_accounting_ref,
            setup_accounting_v2_ref=setup_accounting_v2_ref,
            worker_context_attestation_ref=worker_context_attestation_ref,
            root_rollout_ref=root_rollout_ref,
            worker_rollout_ref=worker_rollout_ref,
            root_environment_ref=root_environment_ref,
            worker_environment_ref=worker_environment_ref,
            generated_output_authority_v3_ref=generated_output_authority_v3_ref,
            generated_output_authority_v2_ref=generated_output_authority_v2_ref,
            generated_output_authority_v1_ref=generated_output_authority_v1_ref,
            abandoned_manifest_ref=abandoned_manifest_ref,
            redesign_evidence_ref=redesign_evidence_ref,
            analyzer_ref=analyzer_ref,
        )
    except EconomicAuthorityError as exc:
        raise DispatchPermitError("product economic authority is invalid") from exc
    if overlay.get("economic_claim_eligible") is not False:
        raise DispatchPermitError("product permit cannot authorize an economic claim")
    validate_generated_output_authority_v3(
        authority_ref=generated_output_authority_v3_ref,
        superseded_v2_ref=generated_output_authority_v2_ref,
        superseded_v1_ref=generated_output_authority_v1_ref,
    )
    workload = _canonical_object(
        read_authority(source_workload_ref, require_mode=None), "source workload"
    )
    protocol = workload.get("protocol")
    if (
        not isinstance(protocol, dict)
        or protocol.get("arm_stage_models") != EXPECTED_ARM_STAGE_MODELS
    ):
        raise DispatchPermitError("workload stage/model map differs from the dispatch contract")
    tasks = workload.get("tasks")
    task_rows = (
        [row for row in tasks if isinstance(row, dict) and row.get("task_id") == request.task_id]
        if isinstance(tasks, list)
        else []
    )
    if (
        (
            isinstance(request, ProductCellDispatchRequestV1)
            and request.v != 1
            or isinstance(request, ProductAttemptDispatchRequestV5)
            and request.v != 5
        )
        or request.scope not in {"experiment", "interactive"}
        or request.controller not in {"direct", "contextmesh"}
    ):
        raise DispatchPermitError("product request scope/version is not frozen")
    task_envelope = _validate_task_envelope(task_envelope_ref, request)
    envelope_task = task_envelope["task"]
    assert isinstance(envelope_task, dict)
    for value, field in (
        (request.run_id, "run_id"),
        (request.cell_id, "cell_id"),
        (request.task_id, "task_id"),
        (request.branch, "branch"),
        (request.stage, "stage"),
    ):
        if not value or len(value.encode("utf-8")) > 256 or "\x00" in value:
            raise DispatchPermitError(f"invalid product {field}")
    if isinstance(request, ProductAttemptDispatchRequestV5):
        _hex64(request.attempt_id, "product attempt_id")
    elif request.attempt_id is not None:
        raise DispatchPermitError("cell-root dispatch must have a null attempt ID")
    if (
        isinstance(request.stage_ordinal, bool)
        or not isinstance(request.stage_ordinal, int)
        or not 1 <= request.stage_ordinal <= 16
        or isinstance(request.journal_cursor, bool)
        or not isinstance(request.journal_cursor, int)
        or not 1 <= request.journal_cursor <= 16
    ):
        raise DispatchPermitError("invalid product stage ordinal")
    orders = workload.get("orders")
    arms = protocol.get("arms")
    if request.scope == "experiment":
        if (
            len(task_rows) != 1
            or not isinstance(orders, dict)
            or request.replicate_id not in orders
            or not isinstance(arms, list)
            or request.arm not in arms
        ):
            raise DispatchPermitError("experiment request is not one frozen workload cell")
        workload_task = task_rows[0]
        source_ref = task_envelope.get("source_ref")
        public_ref = task_envelope.get("public_test_ref")
        oracle_ref = task_envelope.get("oracle_ref")
        if (
            envelope_task
            != {
                "task_id": workload_task.get("task_id"),
                "text": workload_task.get("task_text"),
                "family": workload_task.get("family"),
                "artifact_path": workload_task.get("artifact_path"),
                "searchable_public": False,
                "verification_profile": workload_task.get("profile"),
                "primary": workload_task.get("primary"),
            }
            or not isinstance(source_ref, dict)
            or source_ref.get("sha256") != workload_task.get("source_sha256")
            or source_ref.get("path") != workload_task.get("artifact_path")
            or not isinstance(public_ref, dict)
            or public_ref.get("sha256") != workload_task.get("public_tests_sha256")
            or not isinstance(oracle_ref, dict)
            or oracle_ref.get("sha256") != workload_task.get("oracle_sha256")
            or task_envelope.get("shape") != workload_task.get("shape")
            or task_envelope.get("slot_values") != workload_task.get("slot_values")
        ):
            raise DispatchPermitError("experiment task envelope differs from the frozen task")
    elif (
        request.replicate_id != "interactive"
        or request.arm not in {"rrc_cold", "rrc_warm"}
        or envelope_task.get("verification_profile") != "rrcv2_general_v1"
    ):
        raise DispatchPermitError("interactive request is not one non-economic general task")
    source_ref = task_envelope.get("source_ref")
    target_preimage = task_envelope.get("target_preimage")
    artifact_path = envelope_task.get("artifact_path")
    if request.controller == "direct":
        if target_preimage != {"v": 1, "kind": "none"}:
            raise DispatchPermitError("direct controller requires an inline none target")
    elif isinstance(source_ref, dict):
        if target_preimage != {
            "v": 1,
            "kind": "regular",
            "path": artifact_path,
            "sha256": source_ref.get("sha256"),
            "bytes": source_ref.get("bytes"),
            "mode": 0o644,
        }:
            raise DispatchPermitError("ContextMesh source requires one exact regular target")
    elif target_preimage != {
        "v": 1,
        "kind": "absent",
        "path": artifact_path,
        "mode": 0o644,
    }:
        raise DispatchPermitError("ContextMesh greenfield task requires one exact absent target")
    matrix_row = PRODUCT_STAGE_MATRIX.get(
        (request.transport, request.arm, request.branch, request.stage, request.stage_ordinal)
    )
    if (
        matrix_row is None
        or request.surface_id != matrix_row[0]
        or request.journal_cursor != matrix_row[1]
    ):
        raise DispatchPermitError(
            "product transport/arm/branch/stage/ordinal/cursor tuple is not frozen"
        )
    cursor_kind = "cell" if isinstance(request, ProductCellDispatchRequestV1) else "attempt"
    controller_row = (
        request.scope,
        request.controller,
        request.transport,
        request.arm,
        request.branch,
        request.stage,
        request.stage_ordinal,
        request.surface_id,
        request.journal_cursor,
        cursor_kind,
    )
    if controller_row not in PRODUCT_CONTROLLER_MATRIX:
        raise DispatchPermitError("product controller/call kind does not match the frozen row")
    expected_model, expected_reasoning = _surface_role(request.surface_id)
    capability_manifest = _canonical_object(
        read_authority(capability_manifest_ref), "capability manifest"
    )
    surfaces = capability_manifest.get("surfaces")
    surface_rows = (
        [
            row
            for row in surfaces
            if isinstance(row, dict) and row.get("surface_id") == request.surface_id
        ]
        if isinstance(surfaces, list)
        else []
    )
    if (
        len(surface_rows) != 1
        or surface_rows[0].get("requested_provider") != PROVIDER
        or surface_rows[0].get("requested_model") != expected_model
        or surface_rows[0].get("requested_reasoning") != expected_reasoning
        or surface_rows[0].get("requested_service_tier") != SERVICE_TIER
    ):
        raise DispatchPermitError("product surface role differs from capability evidence")
    if request.call_id != product_call_id(request):
        raise DispatchPermitError("product call ID does not bind the dispatch tuple")
    binding_generation: int | None = None
    rooted_attempt_authority_ref: AuthorityRef | None = None
    if isinstance(request, ProductAttemptDispatchRequestV5):
        if request.controller == "contextmesh":
            if cell_attempt_binding_ref is None:
                raise DispatchPermitError("ContextMesh attempt lacks a rooted cell binding")
            if cell_journal_authority is None:
                raise DispatchPermitError("ContextMesh attempt lacks its cell journal authority")
            binding_generation, rooted_attempt_authority_ref = _validate_cell_attempt_binding(
                cell_attempt_binding_ref, request, cell_journal_authority
            )
        elif request.root_binding_sha256 is not None or cell_attempt_binding_ref is not None:
            raise DispatchPermitError("direct attempt cannot carry a cell-attempt binding")
        elif cell_journal_authority is not None:
            raise DispatchPermitError("direct attempt cannot carry a cell journal authority")
    elif cell_attempt_binding_ref is not None or cell_journal_authority is not None:
        raise DispatchPermitError("cell-root request cannot carry a spawned-attempt binding")
    request_binding_sha256 = (
        request.root_binding_sha256
        if isinstance(request, ProductAttemptDispatchRequestV5)
        else None
    )
    cursor_kind_matches = (
        isinstance(request, ProductCellDispatchRequestV1)
        and isinstance(journal_cursor, ProductCellJournalCursorV1)
        or isinstance(request, ProductAttemptDispatchRequestV5)
        and isinstance(journal_cursor, ProductJournalCursorV2)
    )
    if (
        not cursor_kind_matches
        or isinstance(journal_cursor, ProductCellJournalCursorV1)
        and journal_cursor.v != 1
        or isinstance(journal_cursor, ProductJournalCursorV2)
        and journal_cursor.v != 2
        or journal_cursor.attempt_id != request.attempt_id
        or journal_cursor.cell_id != request.cell_id
        or journal_cursor.call_id != request.call_id
        or journal_cursor.stage_ordinal != request.stage_ordinal
        or isinstance(journal_cursor.journal_generation, bool)
        or not isinstance(journal_cursor.journal_generation, int)
        or journal_cursor.journal_generation < 0
        or journal_cursor.prior_state != "absent"
        or isinstance(journal_cursor, ProductJournalCursorV2)
        and (
            journal_cursor.root_binding_sha256 != request_binding_sha256
            or journal_cursor.root_binding_generation != binding_generation
        )
    ):
        raise DispatchPermitError("product journal cursor does not authorize a new provider launch")

    manifest = _canonical_object(read_authority(config_manifest_ref), "product config manifest")
    if set(manifest) != PRODUCT_CONFIG_FIELDS:
        raise DispatchPermitError("product config manifest has unknown or missing fields")
    expected_config = product_config_manifest(
        economic_authority_file_sha256=hashlib.sha256(
            read_authority(economic_authority_file_ref, require_mode=None)
        ).hexdigest(),
        dispatch_permit_file_sha256=hashlib.sha256(
            read_authority(dispatch_permit_file_ref, require_mode=None)
        ).hexdigest(),
        capability_matrix_file_sha256=hashlib.sha256(
            read_authority(capability_matrix_file_ref, require_mode=None)
        ).hexdigest(),
        sandbox_probe_file_sha256=hashlib.sha256(
            read_authority(sandbox_probe_file_ref, require_mode=None)
        ).hexdigest(),
    )
    if manifest != expected_config:
        raise DispatchPermitError("product config manifest is stale")
    authority_hashes = {
        "task_envelope": task_envelope_ref.sha256,
        "workload_core": workload_core_ref.sha256,
        "overlay": overlay_ref.sha256,
        "capability_manifest": capability_manifest_ref.sha256,
        "capability_summary": capability_summary_ref.sha256,
        "capability_inventory": capability_evidence_inventory_ref.sha256,
        "sandbox_v2": sandbox_evidence_v2_ref.sha256,
        "setup_accounting": setup_accounting_ref.sha256,
        "setup_accounting_v2": setup_accounting_v2_ref.sha256,
        "worker_context_attestation": worker_context_attestation_ref.sha256,
        "generated_output_authority_v3": generated_output_authority_v3_ref.sha256,
        "generated_output_authority_v2": generated_output_authority_v2_ref.sha256,
        "plan": plan_review_seal_ref.sha256,
        "claim_transcript": claim_transcript_ref.sha256,
        "claim_record": claim_record_ref.sha256,
        "config": config_manifest_ref.sha256,
    }
    if cell_attempt_binding_ref is not None:
        authority_hashes["cell_attempt_binding"] = cell_attempt_binding_ref.sha256
    if rooted_attempt_authority_ref is not None:
        authority_hashes["rooted_attempt_authority"] = rooted_attempt_authority_ref.sha256
    return DispatchPermitV1(
        kind="product",
        call_id=request.call_id,
        surface_id=request.surface_id,
        manifest_sha256=overlay_ref.sha256,
        plan_review_seal_sha256=plan_review_seal_ref.sha256,
        authority_set_sha256=hashlib.sha256(canonical_json(authority_hashes)).hexdigest(),
    )
