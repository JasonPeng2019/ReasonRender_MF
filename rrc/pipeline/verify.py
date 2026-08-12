"""Structured, evidence-bearing verification for generated Python code."""

from __future__ import annotations

import ast
import hashlib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rrc.pipeline.sandbox import (
    CAPABILITY_SHA256,
    IMAGE_CONFIG_DIGEST,
    RUNTIME_LOCK_SHA256,
    Profile,
    SandboxExecutionError,
    SandboxInvocationEvidenceV1,
    SandboxLimitsV1,
    SandboxResultV1,
    SandboxTestFileV1,
    SandboxTierExecutionV1,
    SandboxUnavailable,
    SealedDockerSandbox,
    VerifierSandboxPort,
    validate_sandbox_result,
)
from rrc.policy import PolicyKind, SyntheticPolicyError, validate_synthetic_source

MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_TEST_BYTES = 16 * 1024
MAX_SPEC_FIELD_BYTES = 64 * 1024
MAX_TESTS = 128
MAX_AST_NODES = 100_000
MAX_FILE_AST_NODES = 20_000
PROFILES = {"rrcv2_general_v1", "rrcv2_synthetic_v1"}
TierName = Literal["assembly", "ruff", "signature_conformance", "pyright", "pytest"]
_SYNTHETIC_BUILTIN_TYPES = frozenset(
    {"int", "str", "bool", "float", "list", "dict", "tuple", "set"}
)
_SYNTHETIC_RESERVED_ROOTS = frozenset(
    {
        "TYPE_CHECKING",
        "math",
        "re",
        "json",
        "int",
        "str",
        "bool",
        "float",
        "list",
        "dict",
        "tuple",
        "set",
        "abs",
        "all",
        "any",
        "enumerate",
        "len",
        "max",
        "min",
        "range",
        "reversed",
        "round",
        "sorted",
        "sum",
        "zip",
        "AssertionError",
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "ZeroDivisionError",
        "OverflowError",
    }
)


@dataclass(frozen=True)
class CodeArtifactV1:
    attempt_id: str
    artifact_path: str
    source: str
    v: int = 1


@dataclass(frozen=True)
class ArtifactRecordV1:
    attempt_id: str
    artifact_path: str
    source_sha256: str
    source_bytes: int
    blob_path: str
    blob_sha256: str
    blob_bytes: int
    blob_mode: int = 0o600
    v: int = 1


@dataclass(frozen=True)
class VerificationTierRowV1:
    name: TierName
    status: Literal["passed", "failed"]
    input_sha256: str
    output_sha256: str
    diagnostics_sha256: str
    v: int = 1


@dataclass(frozen=True)
class VerificationResultV1:
    attempt_id: str
    verification_profile: str
    code_artifact_sha256: str
    tiers: tuple[VerificationTierRowV1, ...]
    public_accepted: bool
    v: int = 1


@dataclass(frozen=True)
class VerificationRunV1:
    """Runtime envelope carrying every canonical durable verifier authority."""

    result: VerificationResultV1
    artifact: CodeArtifactV1 | None
    evidence_blobs: tuple[bytes, ...]
    execution_evidence: PytestExecutionEvidenceV1 | None = None
    repair_evidence: RepairEvidenceV1 | None = None
    tier_execution_evidence: tuple[TierExecutionEvidenceV1, ...] = ()

    @property
    def public_accepted(self) -> bool:
        return self.result.public_accepted

    @property
    def tiers(self) -> tuple[VerificationTierRowV1, ...]:
        return self.result.tiers

    @property
    def code_artifact_sha256(self) -> str:
        return self.result.code_artifact_sha256

    @property
    def normalized_source(self) -> str:
        return self.artifact.source if self.artifact is not None else ""

    @property
    def collection_evidence(self) -> PytestExecutionEvidenceV1 | None:
        """Compatibility name for the canonical execution evidence authority."""

        return self.execution_evidence


@dataclass(frozen=True)
class VerificationTestsV1:
    spec: tuple[str, ...] = ()
    independent: tuple[str, ...] = ()
    public: tuple[str, ...] = ()

    @property
    def all(self) -> tuple[str, ...]:
        return (*self.spec, *self.independent, *self.public)


@dataclass(frozen=True)
class PytestExecutionEvidenceV1:
    attempt_id: str
    verification_profile: str
    verification_result_sha256: str
    test_sources: tuple[tuple[str, str], ...]
    collected_node_ids: tuple[str, ...]
    completed_node_ids: tuple[str, ...]
    v: int = 1


PytestCollectionEvidenceV1 = PytestExecutionEvidenceV1


@dataclass(frozen=True)
class RepairEvidenceV1:
    attempt_id: str
    verification_result_sha256: str
    failed_tier: TierName
    excerpt_sha256: str
    excerpt: str
    v: int = 1


@dataclass(frozen=True)
class TierExecutionEvidenceV1:
    attempt_id: str
    verification_profile: str
    verification_result_sha256: str
    code_artifact_sha256: str
    verification_tier: TierName
    ordinal: int
    sandbox: SandboxTierExecutionV1
    v: int = 1


@dataclass(frozen=True)
class OracleResultV1:
    attempt_id: str
    code_artifact_sha256: str
    passed: bool
    collected_node_ids: tuple[str, ...]
    completed_node_ids: tuple[str, ...]
    diagnostics_sha256: str
    v: int = 1


def _canonical(value: object) -> bytes:
    def normalize(item: object) -> object:
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, int):
            return item
        if isinstance(item, str):
            return unicodedata.normalize("NFC", item)
        if isinstance(item, (list, tuple)):
            return [normalize(value) for value in item]
        if isinstance(item, dict):
            normalized: dict[str, object] = {}
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise TypeError("canonical JSON object keys must be strings")
                normalized_key = unicodedata.normalize("NFC", key)
                if normalized_key in normalized:
                    raise ValueError("canonical JSON object keys collide after NFC normalization")
                normalized[normalized_key] = normalize(nested)
            return normalized
        raise TypeError("canonical JSON permits only null, bool, integer, string, array, or object")

    return json.dumps(
        normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _artifact_bytes(artifact: CodeArtifactV1) -> bytes:
    return _canonical(
        {
            "artifact_path": artifact.artifact_path,
            "attempt_id": artifact.attempt_id,
            "source": artifact.source,
            "v": artifact.v,
        }
    )


def code_artifact_bytes(artifact: CodeArtifactV1) -> bytes:
    """Return the exact compact durable CodeArtifact bytes."""

    _validate_code_artifact(artifact.attempt_id, artifact.artifact_path, artifact.source)
    return _artifact_bytes(artifact)


def artifact_record_bytes(record: ArtifactRecordV1) -> bytes:
    """Return the exact compact durable accepted-artifact record bytes."""

    return _canonical(
        {
            "artifact_path": record.artifact_path,
            "attempt_id": record.attempt_id,
            "blob_bytes": record.blob_bytes,
            "blob_mode": record.blob_mode,
            "blob_path": record.blob_path,
            "blob_sha256": record.blob_sha256,
            "source_bytes": record.source_bytes,
            "source_sha256": record.source_sha256,
            "v": record.v,
        }
    )


def verification_result_bytes(result: VerificationResultV1) -> bytes:
    """Return the exact compact durable VerificationResult bytes."""

    return _canonical(
        {
            "attempt_id": result.attempt_id,
            "code_artifact_sha256": result.code_artifact_sha256,
            "public_accepted": result.public_accepted,
            "tiers": [
                {
                    "diagnostics_sha256": row.diagnostics_sha256,
                    "input_sha256": row.input_sha256,
                    "name": row.name,
                    "output_sha256": row.output_sha256,
                    "status": row.status,
                    "v": row.v,
                }
                for row in result.tiers
            ],
            "v": result.v,
            "verification_profile": result.verification_profile,
        }
    )


def parse_verification_result(raw: bytes) -> VerificationResultV1:
    """Reopen and validate exact canonical VerificationResult bytes."""

    value = _closed_evidence_object(raw)
    if set(value) != {
        "attempt_id",
        "code_artifact_sha256",
        "public_accepted",
        "tiers",
        "v",
        "verification_profile",
    }:
        raise ValueError("verification result has unknown or missing fields")
    profile = value.get("verification_profile")
    accepted = value.get("public_accepted")
    rows = value.get("tiers")
    if (
        value.get("v") != 1
        or not _hex64(value.get("attempt_id"))
        or not _hex64(value.get("code_artifact_sha256"))
        or profile not in PROFILES
        or not isinstance(accepted, bool)
        or not isinstance(rows, list)
        or not rows
    ):
        raise ValueError("verification result identity is invalid")
    tiers: list[VerificationTierRowV1] = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "diagnostics_sha256",
                "input_sha256",
                "name",
                "output_sha256",
                "status",
                "v",
            }
            or row.get("v") != 1
            or row.get("name")
            not in {"assembly", "ruff", "signature_conformance", "pyright", "pytest"}
            or row.get("status") not in {"passed", "failed"}
            or not _hex64(row.get("input_sha256"))
            or not _hex64(row.get("output_sha256"))
            or not _hex64(row.get("diagnostics_sha256"))
        ):
            raise ValueError("verification result tier row is invalid")
        tiers.append(
            VerificationTierRowV1(
                name=row["name"],  # type: ignore[arg-type]
                status=row["status"],  # type: ignore[arg-type]
                input_sha256=str(row["input_sha256"]),
                output_sha256=str(row["output_sha256"]),
                diagnostics_sha256=str(row["diagnostics_sha256"]),
            )
        )
    names = tuple(row.name for row in tiers)
    spec_sequence = ("assembly", "ruff", "signature_conformance", "pyright", "pytest")
    direct_sequence = ("assembly", "ruff", "pyright", "pytest")
    sequences = (
        (spec_sequence,)
        if profile == "rrcv2_synthetic_v1"
        else (
            direct_sequence,
            spec_sequence,
        )
    )
    if not any(names == sequence[: len(names)] for sequence in sequences):
        raise ValueError("verification result tier order is invalid")
    if any(row.status != "passed" for row in tiers[:-1]):
        raise ValueError("verification result has a nonterminal failed tier")
    passed_output_sha = _sha(b"passed\n")
    failed_output_sha = _sha(b"failed\n")
    empty_diagnostics_sha = _sha(b"")
    for row in tiers:
        expected_output_sha = passed_output_sha if row.status == "passed" else failed_output_sha
        expected_diagnostics_sha = (
            empty_diagnostics_sha
            if row.status == "passed"
            else _sha(_diagnostic_excerpt(b"", tier=row.name))
        )
        if (
            row.input_sha256 != value["code_artifact_sha256"]
            or row.output_sha256 != expected_output_sha
            or row.diagnostics_sha256 != expected_diagnostics_sha
        ):
            raise ValueError("verification result tier evidence relation is invalid")
    if accepted:
        if not any(names == sequence for sequence in sequences) or tiers[-1].status != "passed":
            raise ValueError("accepted verification result is incomplete")
    elif tiers[-1].status != "failed":
        raise ValueError("rejected verification result lacks one terminal failure")
    return VerificationResultV1(
        attempt_id=str(value["attempt_id"]),
        verification_profile=str(profile),
        code_artifact_sha256=str(value["code_artifact_sha256"]),
        tiers=tuple(tiers),
        public_accepted=accepted,
    )


def execution_evidence_bytes(evidence: PytestExecutionEvidenceV1) -> bytes:
    """Return the exact durable pytest collection/completion authority."""

    return _canonical(
        {
            "attempt_id": evidence.attempt_id,
            "collected_node_ids": list(evidence.collected_node_ids),
            "completed_node_ids": list(evidence.completed_node_ids),
            "test_sources": [
                {"filename": filename, "sha256": sha256}
                for filename, sha256 in evidence.test_sources
            ],
            "v": evidence.v,
            "verification_profile": evidence.verification_profile,
            "verification_result_sha256": evidence.verification_result_sha256,
        }
    )


collection_evidence_bytes = execution_evidence_bytes


def repair_evidence_bytes(evidence: RepairEvidenceV1) -> bytes:
    """Return the exact durable repair-observation authority."""

    return _canonical(
        {
            "attempt_id": evidence.attempt_id,
            "excerpt": evidence.excerpt,
            "excerpt_sha256": evidence.excerpt_sha256,
            "failed_tier": evidence.failed_tier,
            "v": evidence.v,
            "verification_result_sha256": evidence.verification_result_sha256,
        }
    )


def tier_execution_evidence_bytes(evidence: TierExecutionEvidenceV1) -> bytes:
    """Return one exact durable verifier-tier execution sidecar."""

    sandbox = evidence.sandbox
    return _canonical(
        {
            "attempt_id": evidence.attempt_id,
            "code_artifact_sha256": evidence.code_artifact_sha256,
            "ordinal": evidence.ordinal,
            "sandbox": {
                "backend_after_sha256": sandbox.backend_after_sha256,
                "backend_before_sha256": sandbox.backend_before_sha256,
                "capability_sha256": sandbox.capability_sha256,
                "image_config_digest": sandbox.image_config_digest,
                "invocations": [
                    {
                        "argv_sha256": row.argv_sha256,
                        "exit_code": row.exit_code,
                        "stderr_sha256": row.stderr_sha256,
                        "stdout_sha256": row.stdout_sha256,
                        "v": row.v,
                    }
                    for row in sandbox.invocations
                ],
                "normalized_source_sha256": sandbox.normalized_source_sha256,
                "runtime_lock_sha256": sandbox.runtime_lock_sha256,
                "tier": sandbox.tier,
                "v": sandbox.v,
            },
            "v": evidence.v,
            "verification_profile": evidence.verification_profile,
            "verification_result_sha256": evidence.verification_result_sha256,
            "verification_tier": evidence.verification_tier,
        }
    )


def _hex64(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _closed_evidence_object(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("durable verifier evidence violates its size bound")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("durable verifier evidence is not UTF-8 JSON") from exc
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise ValueError("durable verifier evidence is not canonical")
    return value


def parse_code_artifact(raw: bytes) -> CodeArtifactV1:
    """Reopen and validate exact canonical CodeArtifact bytes."""

    value = _closed_evidence_object(raw)
    if set(value) != {"artifact_path", "attempt_id", "source", "v"}:
        raise ValueError("code artifact has unknown or missing fields")
    artifact_path = value.get("artifact_path")
    attempt_id = value.get("attempt_id")
    source = value.get("source")
    if (
        value.get("v") != 1
        or not isinstance(attempt_id, str)
        or not isinstance(artifact_path, str)
        or not isinstance(source, str)
    ):
        raise ValueError("code artifact identity is invalid")
    _validate_code_artifact(attempt_id, artifact_path, source)
    artifact = CodeArtifactV1(attempt_id, artifact_path, source)
    if code_artifact_bytes(artifact) != raw:
        raise ValueError("code artifact bytes are not exact canonical authority")
    return artifact


def parse_artifact_record(raw: bytes) -> ArtifactRecordV1:
    """Reopen one exact accepted-artifact row selected by a trusted outcome."""

    value = _closed_evidence_object(raw)
    if set(value) != {
        "artifact_path",
        "attempt_id",
        "blob_bytes",
        "blob_mode",
        "blob_path",
        "blob_sha256",
        "source_bytes",
        "source_sha256",
        "v",
    }:
        raise ValueError("artifact record has unknown or missing fields")
    source_bytes = value.get("source_bytes")
    blob_bytes = value.get("blob_bytes")
    blob_mode = value.get("blob_mode")
    if (
        value.get("v") != 1
        or not _hex64(value.get("attempt_id"))
        or not isinstance(value.get("artifact_path"), str)
        or not _hex64(value.get("source_sha256"))
        or not _hex64(value.get("blob_sha256"))
        or value.get("source_sha256") != value.get("blob_sha256")
        or isinstance(source_bytes, bool)
        or not isinstance(source_bytes, int)
        or source_bytes <= 0
        or source_bytes > MAX_ARTIFACT_BYTES
        or isinstance(blob_bytes, bool)
        or not isinstance(blob_bytes, int)
        or blob_bytes != source_bytes
        or value.get("blob_path") != ".rrcv2/artifacts/accepted-code.v1.utf8"
        or isinstance(blob_mode, bool)
        or not isinstance(blob_mode, int)
        or blob_mode != 0o600
    ):
        raise ValueError("artifact record identity is invalid")
    return ArtifactRecordV1(
        attempt_id=str(value["attempt_id"]),
        artifact_path=str(value["artifact_path"]),
        source_sha256=str(value["source_sha256"]),
        source_bytes=source_bytes,
        blob_path=str(value["blob_path"]),
        blob_sha256=str(value["blob_sha256"]),
        blob_bytes=blob_bytes,
        blob_mode=blob_mode,
    )


def parse_execution_evidence(raw: bytes) -> PytestExecutionEvidenceV1:
    """Reopen and validate an exact durable collection/completion authority."""

    value = _closed_evidence_object(raw)
    if set(value) != {
        "attempt_id",
        "collected_node_ids",
        "completed_node_ids",
        "test_sources",
        "v",
        "verification_profile",
        "verification_result_sha256",
    }:
        raise ValueError("pytest execution evidence has unknown or missing fields")
    collected = value.get("collected_node_ids")
    completed = value.get("completed_node_ids")
    sources = value.get("test_sources")
    profile = value.get("verification_profile")
    if (
        value.get("v") != 1
        or not _hex64(value.get("attempt_id"))
        or not _hex64(value.get("verification_result_sha256"))
        or profile not in PROFILES
        or not isinstance(collected, list)
        or not isinstance(completed, list)
        or not isinstance(sources, list)
        or any(not isinstance(item, str) or not item for item in collected)
        or any(not isinstance(item, str) or not item for item in completed)
        or len(set(collected)) != len(collected)
        or len(set(completed)) != len(completed)
    ):
        raise ValueError("pytest execution evidence identity is invalid")
    source_rows: list[tuple[str, str]] = []
    for row in sources:
        if (
            not isinstance(row, dict)
            or set(row) != {"filename", "sha256"}
            or not isinstance(row.get("filename"), str)
            or not row.get("filename")
            or not _hex64(row.get("sha256"))
        ):
            raise ValueError("pytest execution evidence source row is invalid")
        source_rows.append((str(row["filename"]), str(row["sha256"])))
    if not source_rows or len({name for name, _ in source_rows}) != len(source_rows):
        raise ValueError("pytest execution evidence source inventory is invalid")
    return PytestExecutionEvidenceV1(
        attempt_id=str(value["attempt_id"]),
        verification_profile=str(profile),
        verification_result_sha256=str(value["verification_result_sha256"]),
        test_sources=tuple(source_rows),
        collected_node_ids=tuple(str(item) for item in collected),
        completed_node_ids=tuple(str(item) for item in completed),
    )


parse_collection_evidence = parse_execution_evidence


def parse_repair_evidence(raw: bytes) -> RepairEvidenceV1:
    """Reopen and validate an exact durable repair-observation authority."""

    value = _closed_evidence_object(raw)
    if set(value) != {
        "attempt_id",
        "excerpt",
        "excerpt_sha256",
        "failed_tier",
        "v",
        "verification_result_sha256",
    }:
        raise ValueError("repair evidence has unknown or missing fields")
    excerpt = value.get("excerpt")
    failed_tier = value.get("failed_tier")
    if (
        value.get("v") != 1
        or not _hex64(value.get("attempt_id"))
        or not _hex64(value.get("verification_result_sha256"))
        or not _hex64(value.get("excerpt_sha256"))
        or failed_tier not in {"assembly", "ruff", "signature_conformance", "pyright", "pytest"}
        or not isinstance(excerpt, str)
        or "\r" in excerpt
        or excerpt != unicodedata.normalize("NFC", excerpt)
        or len(excerpt.encode("utf-8")) > 32 * 1024
        or _sha(excerpt.encode("utf-8")) != value.get("excerpt_sha256")
    ):
        raise ValueError("repair evidence identity is invalid")
    return RepairEvidenceV1(
        attempt_id=str(value["attempt_id"]),
        verification_result_sha256=str(value["verification_result_sha256"]),
        failed_tier=failed_tier,  # type: ignore[arg-type]
        excerpt_sha256=str(value["excerpt_sha256"]),
        excerpt=excerpt,
    )


def parse_tier_execution_evidence(raw: bytes) -> TierExecutionEvidenceV1:
    """Reopen one closed durable verifier-tier execution sidecar."""

    value = _closed_evidence_object(raw)
    if set(value) != {
        "attempt_id",
        "code_artifact_sha256",
        "ordinal",
        "sandbox",
        "v",
        "verification_profile",
        "verification_result_sha256",
        "verification_tier",
    }:
        raise ValueError("tier execution evidence has unknown or missing fields")
    sandbox = value.get("sandbox")
    ordinal = value.get("ordinal")
    if (
        value.get("v") != 1
        or not _hex64(value.get("attempt_id"))
        or not _hex64(value.get("code_artifact_sha256"))
        or not _hex64(value.get("verification_result_sha256"))
        or value.get("verification_profile") not in PROFILES
        or value.get("verification_tier")
        not in {"ruff", "signature_conformance", "pyright", "pytest"}
        or isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or ordinal < 0
        or not isinstance(sandbox, dict)
        or set(sandbox)
        != {
            "backend_after_sha256",
            "backend_before_sha256",
            "capability_sha256",
            "image_config_digest",
            "invocations",
            "normalized_source_sha256",
            "runtime_lock_sha256",
            "tier",
            "v",
        }
    ):
        raise ValueError("tier execution evidence identity is invalid")
    normalized_sha = sandbox.get("normalized_source_sha256")
    invocations = sandbox.get("invocations")
    image = sandbox.get("image_config_digest")
    if (
        sandbox.get("v") != 1
        or sandbox.get("tier") not in {"ruff", "pyright", "pytest_collect", "pytest"}
        or not _hex64(sandbox.get("backend_before_sha256"))
        or sandbox.get("backend_after_sha256") != sandbox.get("backend_before_sha256")
        or sandbox.get("capability_sha256") != CAPABILITY_SHA256
        or sandbox.get("runtime_lock_sha256") != RUNTIME_LOCK_SHA256
        or image != IMAGE_CONFIG_DIGEST
        or (normalized_sha is not None and not _hex64(normalized_sha))
        or not isinstance(invocations, list)
        or not invocations
    ):
        raise ValueError("tier sandbox execution authority is invalid")
    parsed_invocations: list[SandboxInvocationEvidenceV1] = []
    for row in invocations:
        exit_code = row.get("exit_code") if isinstance(row, dict) else None
        if (
            not isinstance(row, dict)
            or set(row) != {"argv_sha256", "exit_code", "stderr_sha256", "stdout_sha256", "v"}
            or row.get("v") != 1
            or not _hex64(row.get("argv_sha256"))
            or not _hex64(row.get("stdout_sha256"))
            or not _hex64(row.get("stderr_sha256"))
            or isinstance(exit_code, bool)
            or not isinstance(exit_code, int)
        ):
            raise ValueError("tier invocation execution authority is invalid")
        parsed_invocations.append(
            SandboxInvocationEvidenceV1(
                argv_sha256=str(row["argv_sha256"]),
                exit_code=exit_code,
                stdout_sha256=str(row["stdout_sha256"]),
                stderr_sha256=str(row["stderr_sha256"]),
            )
        )
    sandbox_tier = sandbox["tier"]
    return TierExecutionEvidenceV1(
        attempt_id=str(value["attempt_id"]),
        verification_profile=str(value["verification_profile"]),
        verification_result_sha256=str(value["verification_result_sha256"]),
        code_artifact_sha256=str(value["code_artifact_sha256"]),
        verification_tier=value["verification_tier"],  # type: ignore[arg-type]
        ordinal=ordinal,
        sandbox=SandboxTierExecutionV1(
            tier=sandbox_tier,  # type: ignore[arg-type]
            backend_before_sha256=str(sandbox["backend_before_sha256"]),
            backend_after_sha256=str(sandbox["backend_after_sha256"]),
            capability_sha256=str(sandbox["capability_sha256"]),
            runtime_lock_sha256=str(sandbox["runtime_lock_sha256"]),
            image_config_digest=str(image),
            invocations=tuple(parsed_invocations),
            normalized_source_sha256=(str(normalized_sha) if normalized_sha is not None else None),
        ),
    )


def reopen_verification_evidence(
    *,
    expected_result_sha256: str,
    result_raw: bytes,
    artifact_raw: bytes | None,
    expected_artifact_record_sha256: str | None,
    artifact_record_raw: bytes | None,
    artifact_blob_raw: bytes | None,
    expected_tier_execution_sha256s: tuple[str, ...],
    tier_execution_raws: tuple[bytes, ...],
    expected_execution_sha256: str | None,
    execution_raw: bytes | None,
    expected_repair_sha256: str | None,
    repair_raw: bytes | None,
) -> tuple[
    VerificationResultV1,
    PytestExecutionEvidenceV1 | None,
    RepairEvidenceV1 | None,
]:
    """Strictly reopen one result and every semantically bound durable sidecar."""

    if not _hex64(expected_result_sha256) or _sha(result_raw) != expected_result_sha256:
        raise ValueError("verification result differs from its trusted authority hash")
    if (expected_execution_sha256 is None) != (execution_raw is None) or (
        execution_raw is not None
        and (
            not _hex64(expected_execution_sha256)
            or _sha(execution_raw) != expected_execution_sha256
        )
    ):
        raise ValueError("pytest execution evidence differs from its trusted authority hash")
    if (expected_repair_sha256 is None) != (repair_raw is None) or (
        repair_raw is not None
        and (not _hex64(expected_repair_sha256) or _sha(repair_raw) != expected_repair_sha256)
    ):
        raise ValueError("repair evidence differs from its trusted authority hash")
    result = parse_verification_result(result_raw)
    result_sha256 = _sha(result_raw)
    artifact = parse_code_artifact(artifact_raw) if artifact_raw is not None else None
    if (
        len(expected_tier_execution_sha256s) != len(tier_execution_raws)
        or any(not _hex64(value) for value in expected_tier_execution_sha256s)
        or any(
            _sha(raw) != expected
            for raw, expected in zip(
                tier_execution_raws, expected_tier_execution_sha256s, strict=True
            )
        )
    ):
        raise ValueError("tier execution evidence differs from its trusted inventory")
    tier_execution = tuple(parse_tier_execution_evidence(raw) for raw in tier_execution_raws)
    execution = parse_execution_evidence(execution_raw) if execution_raw is not None else None
    repair = parse_repair_evidence(repair_raw) if repair_raw is not None else None
    assembly_failed = (
        len(result.tiers) == 1
        and result.tiers[0].name == "assembly"
        and result.tiers[0].status == "failed"
    )
    if assembly_failed:
        if artifact is not None:
            raise ValueError("assembly failure must not expose a CodeArtifact authority")
    elif (
        artifact is None
        or artifact.attempt_id != result.attempt_id
        or _sha(artifact_raw or b"") != result.code_artifact_sha256
    ):
        raise ValueError("verification result is not bound to its CodeArtifact authority")
    if result.public_accepted:
        if (
            artifact is None
            or expected_artifact_record_sha256 is None
            or not _hex64(expected_artifact_record_sha256)
            or artifact_record_raw is None
            or artifact_blob_raw is None
            or _sha(artifact_record_raw) != expected_artifact_record_sha256
        ):
            raise ValueError("accepted result lacks its independently selected ArtifactRecord")
        artifact_record = parse_artifact_record(artifact_record_raw)
        source_raw = artifact.source.encode("utf-8")
        if (
            artifact_record.attempt_id != artifact.attempt_id
            or artifact_record.artifact_path != artifact.artifact_path
            or artifact_record.source_sha256 != _sha(source_raw)
            or artifact_record.source_bytes != len(source_raw)
            or artifact_record.blob_sha256 != _sha(artifact_blob_raw)
            or artifact_record.blob_bytes != len(artifact_blob_raw)
            or artifact_blob_raw != source_raw
        ):
            raise ValueError("ArtifactRecord does not bind the accepted CodeArtifact")
    elif any(
        value is not None
        for value in (
            expected_artifact_record_sha256,
            artifact_record_raw,
            artifact_blob_raw,
        )
    ):
        raise ValueError("rejected verification must not carry an accepted ArtifactRecord")
    if tuple(row.ordinal for row in tier_execution) != tuple(range(len(tier_execution))):
        raise ValueError("tier execution evidence ordinals are incomplete or reordered")
    result_tier_names = tuple(row.name for row in result.tiers)
    for row in tier_execution:
        if (
            row.attempt_id != result.attempt_id
            or row.verification_profile != result.verification_profile
            or row.verification_result_sha256 != result_sha256
            or row.code_artifact_sha256 != result.code_artifact_sha256
            or row.verification_tier not in result_tier_names
        ):
            raise ValueError("tier execution evidence is not bound to its result/artifact")
    expected_tool_tiers = tuple(
        name for name in result_tier_names if name in {"ruff", "pyright", "pytest"}
    )
    observed_tool_tiers: list[TierName] = []
    for row in tier_execution:
        if not observed_tool_tiers or observed_tool_tiers[-1] != row.verification_tier:
            observed_tool_tiers.append(row.verification_tier)
        if (
            (row.verification_tier == "ruff" and row.sandbox.tier != "ruff")
            or (row.verification_tier == "pyright" and row.sandbox.tier != "pyright")
            or (
                row.verification_tier == "pytest"
                and row.sandbox.tier not in {"pytest_collect", "pytest"}
            )
        ):
            raise ValueError("tier execution evidence names the wrong sandbox tier")
    if tuple(observed_tool_tiers) != expected_tool_tiers:
        raise ValueError("tier execution evidence inventory differs from the executed cursor")
    pytest_rows = [row for row in tier_execution if row.verification_tier == "pytest"]
    if pytest_rows and (
        pytest_rows[0].sandbox.tier != "pytest_collect"
        or any(row.sandbox.tier != "pytest" for row in pytest_rows[1:])
        or (result.public_accepted and len(pytest_rows) < 2)
    ):
        raise ValueError("pytest execution evidence inventory is incomplete or reordered")
    if execution is not None and (
        execution.attempt_id != result.attempt_id
        or execution.verification_profile != result.verification_profile
        or execution.verification_result_sha256 != result_sha256
    ):
        raise ValueError("pytest execution evidence is not bound to its result")
    pytest_terminal = result.tiers[-1].name == "pytest"
    if pytest_terminal != (execution is not None):
        raise ValueError("pytest execution evidence presence differs from the tier cursor")
    if result.public_accepted:
        if (
            execution is None
            or not execution.collected_node_ids
            or execution.completed_node_ids != execution.collected_node_ids
            or repair is not None
        ):
            raise ValueError("accepted verification evidence is incomplete or contradictory")
    else:
        if (
            repair is None
            or repair.attempt_id != result.attempt_id
            or repair.verification_result_sha256 != result_sha256
            or repair.failed_tier != result.tiers[-1].name
        ):
            raise ValueError("repair evidence is not bound to the terminal failed tier")
    return result, execution, repair


def _repair_excerpt(raw: bytes) -> str:
    text = unicodedata.normalize("NFC", raw.decode("utf-8", errors="replace"))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    bounded = text.encode("utf-8")[: 32 * 1024]
    return bounded.decode("utf-8", errors="ignore")


def _tier(
    name: TierName, passed: bool, input_raw: bytes, output: bytes, diagnostics: bytes
) -> VerificationTierRowV1:
    return VerificationTierRowV1(
        name=name,
        status="passed" if passed else "failed",
        input_sha256=_sha(input_raw),
        output_sha256=_sha(output),
        diagnostics_sha256=_sha(diagnostics),
    )


def _finish(
    *,
    attempt_id: str,
    verification_profile: Profile,
    artifact_path: str,
    code_artifact_sha256: str,
    tiers: list[VerificationTierRowV1],
    accepted: bool,
    normalized_source: str | None,
    evidence_blobs: list[bytes],
    tier_sandbox_evidence: list[tuple[TierName, SandboxTierExecutionV1]],
    collection_node_ids: tuple[str, ...] = (),
    completed_node_ids: tuple[str, ...] = (),
    collection_tests: tuple[SandboxTestFileV1, ...] = (),
    repair_failure: tuple[TierName, bytes] | None = None,
) -> VerificationRunV1:
    artifact = (
        CodeArtifactV1(attempt_id, artifact_path, normalized_source)
        if normalized_source is not None
        else None
    )
    result = VerificationResultV1(
        attempt_id=attempt_id,
        verification_profile=verification_profile,
        code_artifact_sha256=code_artifact_sha256,
        tiers=tuple(tiers),
        public_accepted=accepted,
    )
    result_sha256 = _sha(verification_result_bytes(result))
    tier_execution_evidence = tuple(
        TierExecutionEvidenceV1(
            attempt_id=attempt_id,
            verification_profile=verification_profile,
            verification_result_sha256=result_sha256,
            code_artifact_sha256=code_artifact_sha256,
            verification_tier=verification_tier,
            ordinal=ordinal,
            sandbox=sandbox_evidence,
        )
        for ordinal, (verification_tier, sandbox_evidence) in enumerate(tier_sandbox_evidence)
    )
    evidence_blobs.extend(
        tier_execution_evidence_bytes(evidence) for evidence in tier_execution_evidence
    )
    collection_evidence = None
    if collection_node_ids or collection_tests:
        collection_evidence = PytestExecutionEvidenceV1(
            attempt_id=attempt_id,
            verification_profile=verification_profile,
            verification_result_sha256=result_sha256,
            test_sources=tuple((item.filename, _sha(item.source)) for item in collection_tests),
            collected_node_ids=collection_node_ids,
            completed_node_ids=completed_node_ids,
        )
        evidence_blobs.append(collection_evidence_bytes(collection_evidence))
    repair_evidence = None
    if not accepted and repair_failure is not None:
        failed_tier, raw_excerpt = repair_failure
        excerpt = _repair_excerpt(raw_excerpt)
        repair_evidence = RepairEvidenceV1(
            attempt_id=attempt_id,
            verification_result_sha256=result_sha256,
            failed_tier=failed_tier,
            excerpt_sha256=_sha(excerpt.encode("utf-8")),
            excerpt=excerpt,
        )
        evidence_blobs.append(repair_evidence_bytes(repair_evidence))
    return VerificationRunV1(
        result,
        artifact,
        tuple(evidence_blobs),
        collection_evidence,
        repair_evidence,
        tier_execution_evidence,
    )


def _validate_artifact(attempt_id: str, artifact_path: str, source: str) -> bytes:
    if len(attempt_id) != 64 or any(char not in "0123456789abcdef" for char in attempt_id):
        raise ValueError("attempt_id must be 64 lowercase hex characters")
    lexical_parts = artifact_path.split("/")
    if (
        not artifact_path
        or any(part in {"", ".", ".."} for part in lexical_parts)
        or artifact_path.startswith("/")
        or "\\" in artifact_path
        or "\x00" in artifact_path
        or unicodedata.normalize("NFC", artifact_path) != artifact_path
        or lexical_parts[0] == ".rrcv2"
    ):
        raise ValueError("artifact path is not task-owned")
    raw = source.encode("utf-8", errors="strict")
    if (
        not raw
        or len(raw) > MAX_ARTIFACT_BYTES
        or unicodedata.normalize("NFC", source) != source
        or "\r" in source
        or "\x00" in source
    ):
        raise ValueError("candidate source violates encoding or size policy")
    tree = ast.parse(source, filename=artifact_path)
    if sum(1 for _ in ast.walk(tree)) > MAX_FILE_AST_NODES:
        raise ValueError("candidate AST exceeds the node cap")
    if len(_artifact_bytes(CodeArtifactV1(attempt_id, artifact_path, source))) > MAX_ARTIFACT_BYTES:
        raise ValueError("canonical CodeArtifact exceeds the byte cap")
    return raw


def _validate_code_artifact(attempt_id: str, artifact_path: str, source: str) -> bytes:
    raw = _validate_artifact(attempt_id, artifact_path, source)
    if source.endswith("\n"):
        raise ValueError("canonical CodeArtifact source must not have a terminal LF")
    return raw


def _function_header(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    header = (
        "async" if isinstance(node, ast.AsyncFunctionDef) else "sync",
        node.name,
        ast.dump(node.args, include_attributes=False),
        tuple(ast.dump(item, include_attributes=False) for item in node.decorator_list),
        ast.dump(node.returns, include_attributes=False) if node.returns else None,
        node.type_comment,
        tuple(
            ast.dump(item, include_attributes=False) for item in getattr(node, "type_params", ())
        ),
    )
    return repr(header)


def _class_header(node: ast.ClassDef) -> str:
    return repr(
        (
            node.name,
            tuple(ast.dump(item, include_attributes=False) for item in node.decorator_list),
            tuple(ast.dump(item, include_attributes=False) for item in node.bases),
            tuple(
                (keyword.arg, ast.dump(keyword.value, include_attributes=False))
                for keyword in node.keywords
            ),
            tuple(
                ast.dump(item, include_attributes=False)
                for item in getattr(node, "type_params", ())
            ),
        )
    )


def _headers(source: str) -> dict[str, tuple[str | None, str]]:
    tree = ast.parse(source, type_comments=True)
    if tree.type_ignores:
        raise ValueError("candidate source contains a type-ignore directive")
    result: dict[str, tuple[str | None, str]] = {}
    top_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in top_names:
                raise ValueError("duplicate required symbol")
            top_names.add(node.name)
            result[node.name] = (None, _function_header(node))
        elif isinstance(node, ast.ClassDef):
            if node.name in top_names:
                raise ValueError("duplicate required class")
            top_names.add(node.name)
            class_header = _class_header(node)
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                name = f"{node.name}.{item.name}"
                if name in result:
                    raise ValueError("duplicate required symbol")
                result[name] = (class_header, _function_header(item))
    return result


def _public_chain(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        result = (node.id,)
    elif isinstance(node, ast.Attribute):
        result = _attribute_path(node)
    else:
        raise ValueError("signature name is not a public chain")
    if len(result) > 8 or not all(_public_segment(item) for item in result):
        raise ValueError("signature name chain is invalid")
    return result


def _validate_general_default(node: ast.AST, *, depth: int = 0) -> None:
    if depth > 4:
        raise ValueError("signature default exceeds its depth cap")
    if isinstance(node, ast.Constant):
        value = node.value
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1:
            return
        if (
            isinstance(value, str)
            and unicodedata.normalize("NFC", value) == value
            and len(value.encode("utf-8")) <= 4096
        ):
            return
        raise ValueError("signature default literal is invalid")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        if not isinstance(node.operand, ast.Constant) or isinstance(node.operand.value, bool):
            raise ValueError("signature signed default is invalid")
        value = node.operand.value
        if not isinstance(value, int):
            raise ValueError("signature signed default is invalid")
        if isinstance(node.op, ast.UAdd) and 0 <= value <= 2**63 - 1:
            return
        if isinstance(node.op, ast.USub) and 1 <= value <= 2**63:
            return
        raise ValueError("signature signed default is out of range")
    if isinstance(node, ast.Tuple) and len(node.elts) <= 16:
        for item in node.elts:
            _validate_general_default(item, depth=depth + 1)
        return
    raise ValueError("signature default is outside the frozen grammar")


def _validate_general_type(node: ast.AST, *, depth: int = 0, class_base: bool = False) -> None:
    if depth > 8 or len(ast.unparse(node).encode("utf-8")) > 512:
        raise ValueError("signature type exceeds its depth cap")
    if isinstance(node, (ast.Name, ast.Attribute)):
        _public_chain(node)
        return
    if isinstance(node, ast.Constant) and node.value is None and not class_base:
        return
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) and not class_base:
        _validate_general_type(node.left, depth=depth + 1)
        _validate_general_type(node.right, depth=depth + 1)
        return
    if isinstance(node, ast.Subscript):
        _public_chain(node.value)
        items = node.slice.elts if isinstance(node.slice, ast.Tuple) else (node.slice,)
        if not items or len(items) > 16:
            raise ValueError("signature type argument count is invalid")
        for item in items:
            _validate_general_type(item, depth=depth + 1, class_base=class_base)
        return
    raise ValueError("signature type is outside the frozen grammar")


def _validate_general_decorator(node: ast.AST) -> None:
    if isinstance(node, (ast.Name, ast.Attribute)):
        chain = _public_chain(node)
        if chain[-1] in {"staticmethod", "classmethod"} and len(chain) != 1:
            raise ValueError("signature method discriminator must be bare")
        return
    if not isinstance(node, ast.Call):
        raise ValueError("signature decorator is outside the frozen grammar")
    chain = _public_chain(node.func)
    if chain[-1] in {"staticmethod", "classmethod"}:
        raise ValueError("signature method discriminator cannot be called")
    if len(node.args) + len(node.keywords) > 16 or any(
        isinstance(item, ast.Starred) for item in node.args
    ):
        raise ValueError("signature decorator arguments are invalid")
    names: set[str] = set()
    for item in node.args:
        _validate_general_default(item)
    for keyword in node.keywords:
        if keyword.arg is None or not _public_segment(keyword.arg) or keyword.arg in names:
            raise ValueError("signature decorator keyword is invalid")
        names.add(keyword.arg)
        _validate_general_default(keyword.value)


def _validate_signature_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef, *, method: bool
) -> None:
    if (
        not _public_segment(node.name)
        or node.type_comment is not None
        or getattr(node, "type_params", ())
    ):
        raise ValueError("signature callable metadata is invalid")
    if len(node.decorator_list) > 8:
        raise ValueError("signature callable has too many decorators")
    for decorator in node.decorator_list:
        _validate_general_decorator(decorator)
    if (
        len(node.body) != 1
        or not isinstance(node.body[0], ast.Expr)
        or not isinstance(node.body[0].value, ast.Constant)
        or node.body[0].value.value is not Ellipsis
        or node.args.vararg is not None
        or node.args.kwarg is not None
        or node.returns is None
    ):
        raise ValueError("signature callable is not one declaration stub")
    _validate_general_type(node.returns)
    positional = [*node.args.posonlyargs, *node.args.args]
    static = sum(
        isinstance(item, ast.Name) and item.id == "staticmethod" for item in node.decorator_list
    )
    classmethod = sum(
        isinstance(item, ast.Name) and item.id == "classmethod" for item in node.decorator_list
    )
    if static > 1 or classmethod > 1 or (static and classmethod):
        raise ValueError("signature method discriminator is invalid")
    receiver_count = 0
    if method and not static:
        expected = "cls" if classmethod else "self"
        if not positional or positional[0].arg != expected:
            raise ValueError("signature method receiver is invalid")
        receiver_count = 1
    defaults_start = len(positional) - len(node.args.defaults)
    for index, argument in enumerate(positional):
        if not _public_segment(argument.arg):
            raise ValueError("signature parameter name is invalid")
        is_receiver = index < receiver_count
        if not is_receiver and argument.annotation is None:
            raise ValueError("signature parameter annotation is required")
        if argument.annotation is not None:
            _validate_general_type(argument.annotation)
        if is_receiver and index >= defaults_start:
            raise ValueError("signature receiver cannot have a default")
    for default in node.args.defaults:
        _validate_general_default(default)
    for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
        if not _public_segment(argument.arg) or argument.annotation is None:
            raise ValueError("signature keyword-only parameter is invalid")
        _validate_general_type(argument.annotation)
        if default is not None:
            _validate_general_default(default)


def _general_signature_headers(source: str) -> dict[str, tuple[str | None, str]]:
    if (
        not source
        or len(source.encode("utf-8", errors="strict")) > MAX_SPEC_FIELD_BYTES
        or source != unicodedata.normalize("NFC", source)
        or "\r" in source
        or "\x00" in source
    ):
        raise ValueError("general signature violates the text or size policy")
    tree = ast.parse(source, type_comments=True)
    if sum(1 for _ in ast.walk(tree)) > MAX_FILE_AST_NODES or not tree.body or tree.type_ignores:
        raise ValueError("general signature exceeds the AST cap or is empty")
    annotation_nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            annotation_nodes.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            annotation_nodes.append(node.returns)
    if any(
        (segment := ast.get_source_segment(source, node)) is None
        or len(segment.encode("utf-8")) > 512
        for node in annotation_nodes
    ):
        raise ValueError("general signature annotation exceeds the byte cap")
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in names:
                raise ValueError("duplicate required symbol")
            names.add(node.name)
            _validate_signature_function(node, method=False)
            continue
        if not isinstance(node, ast.ClassDef) or not _public_segment(node.name):
            raise ValueError("general signature has a forbidden top-level declaration")
        if node.name in names or getattr(node, "type_params", ()):
            raise ValueError("duplicate or parameterized signature class")
        names.add(node.name)
        if len(node.decorator_list) > 8 or len(node.bases) > 8 or len(node.keywords) > 8:
            raise ValueError("signature class metadata exceeds its cap")
        for decorator in node.decorator_list:
            _validate_general_decorator(decorator)
        for base in node.bases:
            _validate_general_type(base, class_base=True)
        keyword_names: set[str] = set()
        for keyword in node.keywords:
            if (
                keyword.arg is None
                or not _public_segment(keyword.arg)
                or keyword.arg in keyword_names
            ):
                raise ValueError("signature class keyword is invalid")
            keyword_names.add(keyword.arg)
            if keyword.arg == "metaclass":
                _public_chain(keyword.value)
            elif isinstance(keyword.value, (ast.Name, ast.Attribute)):
                _public_chain(keyword.value)
            else:
                _validate_general_default(keyword.value)
        method_names: set[str] = set()
        if not node.body:
            raise ValueError("signature class must declare at least one method")
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                raise ValueError("signature class contains a forbidden member")
            if item.name in method_names:
                raise ValueError("duplicate required method")
            method_names.add(item.name)
            _validate_signature_function(item, method=True)
    return _headers(source)


def _signature_matches(
    source: str,
    signature_source: str,
    *,
    required: dict[str, tuple[str | None, str]] | None = None,
) -> bool:
    try:
        required_headers = required if required is not None else _headers(signature_source)
        actual = _headers(source)
    except (SyntaxError, ValueError):
        return False
    return bool(required_headers) and all(
        actual.get(name) == header for name, header in required_headers.items()
    )


def _public_segment(value: str) -> bool:
    return value.isascii() and value.isidentifier() and not value.startswith("_")


def _attribute_path(node: ast.Attribute) -> tuple[str, ...]:
    segments: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        segments.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        raise ValueError("synthetic annotation has a computed root")
    result = (current.id, *reversed(segments))
    if not all(_public_segment(item) for item in result):
        raise ValueError("synthetic annotation has a private or invalid segment")
    return result


def _synthetic_annotation_paths(node: ast.AST, *, depth: int = 0) -> set[tuple[str, ...]]:
    if depth > 8 or len(ast.unparse(node).encode("utf-8")) > 512:
        raise ValueError("synthetic annotation exceeds its bound")
    if isinstance(node, ast.Name):
        if not _public_segment(node.id):
            raise ValueError("synthetic annotation name is invalid")
        return set() if node.id in _SYNTHETIC_BUILTIN_TYPES else {(node.id,)}
    if isinstance(node, ast.Constant) and node.value is None:
        return set()
    if isinstance(node, ast.Attribute):
        return {_attribute_path(node)}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _synthetic_annotation_paths(
            node.left, depth=depth + 1
        ) | _synthetic_annotation_paths(node.right, depth=depth + 1)
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id in {"list", "dict", "tuple", "set"}
    ):
        items = node.slice.elts if isinstance(node.slice, ast.Tuple) else (node.slice,)
        if (node.value.id in {"list", "set"} and len(items) != 1) or (
            node.value.id == "dict" and len(items) != 2
        ):
            raise ValueError("synthetic container annotation has invalid arity")
        result: set[tuple[str, ...]] = set()
        for item in items:
            result.update(_synthetic_annotation_paths(item, depth=depth + 1))
        return result
    raise ValueError("synthetic annotation is outside the positive type grammar")


def _synthetic_type_prelude(signature_source: str, *, target: str, body_source: str) -> str:
    if (
        not signature_source
        or len(signature_source.encode("utf-8", errors="strict")) > MAX_SPEC_FIELD_BYTES
        or signature_source != unicodedata.normalize("NFC", signature_source)
        or "\r" in signature_source
        or "\x00" in signature_source
    ):
        raise ValueError("synthetic signature violates the text or size policy")
    try:
        tree = ast.parse(signature_source)
        body = ast.parse(body_source)
    except SyntaxError as exc:
        raise ValueError("synthetic signature/body is invalid Python") from exc
    paths: set[tuple[str, ...]] = set()
    required_functions = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if (
        len(tree.body) != 1
        or len(required_functions) != 1
        or required_functions[0].name != target
        or sum(1 for _ in ast.walk(tree)) > MAX_FILE_AST_NODES
    ):
        raise ValueError("synthetic signature must declare exactly the target")
    function = required_functions[0]
    arguments = (
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    )
    for argument in arguments:
        if argument.annotation is None:
            raise ValueError("synthetic signature arguments require annotations")
        paths.update(_synthetic_annotation_paths(argument.annotation))
    if function.returns is None:
        raise ValueError("synthetic signature requires a return annotation")
    paths.update(_synthetic_annotation_paths(function.returns))
    roots = {path[0] for path in paths}
    if roots & (_SYNTHETIC_RESERVED_ROOTS | {target}):
        raise ValueError("synthetic domain type root collides with a reserved controller root")
    body_bindings: set[str] = set()

    def bound_names(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, (ast.Tuple, ast.List)):
            return {name for item in node.elts for name in bound_names(item)}
        return set()

    for node in body.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body_bindings.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            body_bindings.update(name for item in targets for name in bound_names(item))
    if body_bindings & (_SYNTHETIC_RESERVED_ROOTS | roots):
        raise ValueError("synthetic body binding collides with its trusted prelude")
    annotation_node_ids: set[int] = set()
    for node in ast.walk(body):
        annotations: tuple[ast.AST, ...] = ()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations = tuple(
                annotation
                for annotation in (
                    *(argument.annotation for argument in node.args.posonlyargs),
                    *(argument.annotation for argument in node.args.args),
                    *(argument.annotation for argument in node.args.kwonlyargs),
                    node.args.vararg.annotation if node.args.vararg is not None else None,
                    node.args.kwarg.annotation if node.args.kwarg is not None else None,
                    node.returns,
                )
                if annotation is not None
            )
        elif isinstance(node, ast.AnnAssign):
            annotations = (node.annotation,)
        for annotation in annotations:
            annotation_node_ids.update(id(item) for item in ast.walk(annotation))
    if any(
        isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in roots
        and id(node) not in annotation_node_ids
        for node in ast.walk(body)
    ):
        raise ValueError("synthetic domain type root is used outside an annotation")
    trie: dict[str, dict] = {}
    for path in sorted(paths):
        current = trie
        for segment in path:
            current = current.setdefault(segment, {})

    lines: list[str] = []

    def emit(nodes: dict[str, dict], depth: int) -> None:
        if not nodes:
            lines.append("    " * depth + "pass")
            return
        for name in sorted(nodes):
            lines.append("    " * depth + f"class {name}:")
            emit(nodes[name], depth + 1)

    emit(trie, 1)
    return (
        "from __future__ import annotations\n\n"
        "import json as json\n"
        "import math as math\n"
        "import re as re\n\n"
        "TYPE_CHECKING = False\n"
        "if TYPE_CHECKING:\n" + "\n".join(lines) + "\n\n"
    )


def _synthetic_runtime_source(
    signature_source: str, *, target: str, body_source: str
) -> tuple[str, bytes]:
    prelude = _synthetic_type_prelude(
        signature_source,
        target=target,
        body_source=body_source,
    )
    body_tree = ast.parse(body_source)
    uses_facade = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"json", "math", "re"}
        for node in ast.walk(body_tree)
    )
    if "    class " not in prelude and not uses_facade:
        prelude = ""
    assembled = (prelude + body_source).encode("utf-8", errors="strict")
    if len(assembled) > MAX_ARTIFACT_BYTES:
        raise ValueError("synthetic assembled source exceeds the byte cap")
    if sum(1 for _ in ast.walk(ast.parse(assembled))) > MAX_FILE_AST_NODES:
        raise ValueError("synthetic assembled source exceeds the AST cap")
    return prelude, assembled


def _without_terminal_lf(source: str) -> str:
    return source[:-1] if source.endswith("\n") else source


def _normalized_type_prelude(prelude: str) -> str:
    """Regenerate the exact pinned-Ruff representation of a nonempty trusted prelude."""

    if not prelude:
        return ""
    marker = "if TYPE_CHECKING:\n"
    if prelude.count(marker) != 1:
        raise ValueError("trusted synthetic prelude marker is invalid")
    header = prelude[: prelude.index(marker) + len(marker)]
    tree = ast.parse(prelude)
    conditionals = [node for node in tree.body if isinstance(node, ast.If)]
    if len(conditionals) != 1:
        raise ValueError("trusted synthetic prelude conditional is invalid")

    def emit(nodes: list[ast.stmt], depth: int) -> list[str]:
        if len(nodes) == 1 and isinstance(nodes[0], ast.Pass):
            return ["    " * depth + "pass"]
        lines: list[str] = []
        for index, node in enumerate(nodes):
            if not isinstance(node, ast.ClassDef):
                raise ValueError("trusted synthetic prelude tree is invalid")
            if index:
                lines.append("")
            lines.append("    " * depth + f"class {node.name}:")
            lines.extend(emit(node.body, depth + 1))
        return lines

    body = conditionals[0].body
    leading = "\n" if body and isinstance(body[0], ast.ClassDef) else ""
    return header + leading + "\n".join(emit(body, 1)) + "\n\n\n"


def _normalized_synthetic_source(
    normalized_runtime: str,
    *,
    expected_prelude: str,
    signature_source: str,
    target: str,
) -> tuple[str, str]:
    """Return (durable full source, normalized model body) after trusted-prefix validation."""

    if not expected_prelude:
        body = _without_terminal_lf(normalized_runtime)
        regenerated, _ = _synthetic_runtime_source(
            signature_source,
            target=target,
            body_source=body,
        )
        if regenerated:
            raise ValueError("Ruff-normalized body unexpectedly requires a synthetic prelude")
        return body, body
    expected_prefix = _normalized_type_prelude(expected_prelude)
    if not normalized_runtime.startswith(expected_prefix) or len(normalized_runtime) <= len(
        expected_prefix
    ):
        raise ValueError("Ruff changed or removed the trusted synthetic prelude bytes")
    body = _without_terminal_lf(normalized_runtime[len(expected_prefix) :])
    regenerated, _ = _synthetic_runtime_source(
        signature_source,
        target=target,
        body_source=body,
    )
    if not regenerated or _normalized_type_prelude(regenerated) != expected_prefix:
        raise ValueError("normalized synthetic body no longer binds the trusted prelude")
    return _without_terminal_lf(normalized_runtime), body


def _synthetic_artifact_body(
    source: str,
    *,
    signature_source: str,
    target: str,
) -> str:
    """Reopen one durable synthetic artifact and return its separately validated model body."""

    template = _synthetic_type_prelude(
        signature_source,
        target=target,
        body_source="1",
    )
    expected_prefix = _normalized_type_prelude(template)
    has_prefix = source.startswith(expected_prefix) and len(source) > len(expected_prefix)
    if has_prefix:
        body = source[len(expected_prefix) :]
    else:
        body = source
    body = _without_terminal_lf(body)
    expected_prelude, _ = _synthetic_runtime_source(
        signature_source,
        target=target,
        body_source=body,
    )
    if bool(expected_prelude) != has_prefix:
        raise ValueError("synthetic artifact prelude presence differs from its signature/body")
    if has_prefix:
        if _normalized_type_prelude(expected_prelude) != expected_prefix:
            raise ValueError("synthetic artifact trusted prefix differs from its body authority")
        canonical, validated_body = _normalized_synthetic_source(
            source,
            expected_prelude=expected_prelude,
            signature_source=signature_source,
            target=target,
        )
        if canonical != source or validated_body != body:
            raise ValueError("synthetic artifact is not in canonical normalized form")
    return body


def _diagnostic_excerpt(raw: bytes, *, tier: TierName) -> bytes:
    del raw
    return _canonical({"kind": "verification_failure", "tier": tier, "v": 1})


def _bounded_evidence_append(
    parts: list[bytes], raw: bytes, *, retained: int, cap: int, label: str
) -> int:
    if len(raw) > cap - retained:
        raise ValueError(f"aggregate {label} evidence exceeds the output cap")
    parts.append(raw)
    return retained + len(raw)


def _validate_tests(tests: VerificationTestsV1, source_nodes: int) -> tuple[SandboxTestFileV1, ...]:
    if (
        len(tests.spec) > 32
        or len(tests.independent) > 32
        or len(tests.public) > 64
        or not tests.all
        or len(tests.all) > MAX_TESTS
        or any(
            len(set(category_tests)) != len(category_tests)
            for category_tests in (tests.spec, tests.independent, tests.public)
        )
    ):
        raise ValueError("verification requires bounded unique categorized tests")
    results: list[SandboxTestFileV1] = []
    total_nodes = source_nodes
    total_bytes = 0
    for category, category_tests in (
        ("spec", tests.spec),
        ("independent", tests.independent),
        ("public", tests.public),
    ):
        for index, test in enumerate(category_tests):
            if test != unicodedata.normalize("NFC", test) or "\r" in test or "\x00" in test:
                raise ValueError("test artifact violates the text policy")
            raw = test.encode("utf-8", errors="strict")
            if not raw or len(raw) > MAX_TEST_BYTES:
                raise ValueError("test artifact violates the size policy")
            try:
                nodes = sum(1 for _ in ast.walk(ast.parse(test)))
            except SyntaxError as exc:
                raise ValueError("test artifact is not valid Python") from exc
            if nodes > MAX_FILE_AST_NODES:
                raise ValueError("test artifact exceeds the per-file AST cap")
            total_nodes += nodes
            total_bytes += len(raw)
            results.append(SandboxTestFileV1(category, index, raw))  # type: ignore[arg-type]
    if total_nodes > MAX_AST_NODES or total_bytes > MAX_ARTIFACT_BYTES:
        raise ValueError("attempt test artifacts exceed aggregate caps")
    return tuple(results)


def verify_candidate(
    *,
    attempt_id: str,
    verification_profile: Profile,
    artifact_path: str,
    source: str,
    tests: tuple[str, ...] | None = None,
    test_suite: VerificationTestsV1 | None = None,
    sandbox: VerifierSandboxPort,
    signature_source: str | None = None,
    spec_driven: bool | None = None,
    synthetic_target: str | None = None,
    limits: SandboxLimitsV1 = SandboxLimitsV1(),
) -> VerificationRunV1:
    """Run the ordered public-acceptance tiers and fail closed on incomplete evidence."""

    if verification_profile not in PROFILES:
        raise ValueError("unsupported verification profile")
    if (tests is None) == (test_suite is None):
        raise ValueError("provide exactly one categorized or legacy test authority")
    suite = test_suite or VerificationTestsV1(public=tests or ())
    is_spec_driven = signature_source is not None if spec_driven is None else spec_driven
    if (
        (is_spec_driven and signature_source is None)
        or (not is_spec_driven and signature_source is not None)
        or (verification_profile == "rrcv2_synthetic_v1" and not is_spec_driven)
    ):
        raise ValueError("verification profile/flow/signature combination is impossible")
    tiers: list[VerificationTierRowV1] = []
    evidence_blobs: list[bytes] = []
    tier_sandbox_evidence: list[tuple[TierName, SandboxTierExecutionV1]] = []
    expected_synthetic_nodes: tuple[str, ...] | None = None
    synthetic_prelude: str | None = None
    resolved_synthetic_target: str | None = None
    required_signature_headers: dict[str, tuple[str | None, str]] | None = None
    runtime_source_raw: bytes
    failure_observation: tuple[TierName, bytes] | None = None

    def record(
        name: TierName,
        passed: bool,
        input_raw: bytes,
        output: bytes,
        diagnostics: bytes,
    ) -> VerificationTierRowV1:
        nonlocal failure_observation
        canonical_diagnostics = b"" if passed else _diagnostic_excerpt(diagnostics, tier=name)
        evidence_blobs.extend((output, canonical_diagnostics))
        if not passed:
            failure_observation = (name, diagnostics)
        return _tier(name, passed, input_raw, output, canonical_diagnostics)

    def capture(
        result: object,
        *,
        sandbox_tier: Literal["ruff", "pyright", "pytest_collect", "pytest"],
        verification_tier: TierName,
    ) -> None:
        if not isinstance(result, SandboxResultV1):
            raise ValueError("sandbox result has the wrong type")
        validate_sandbox_result(result, expected_tier=sandbox_tier, limits=limits)
        if result.execution_evidence is None:
            raise ValueError("sandbox result lacks durable tier execution evidence")
        tier_sandbox_evidence.append((verification_tier, result.execution_evidence))
        for invocation in result.invocations:
            evidence_blobs.extend((invocation.stdout, invocation.stderr))

    try:
        source_raw = _validate_artifact(attempt_id, artifact_path, source)
        if signature_source is not None and verification_profile == "rrcv2_general_v1":
            required_signature_headers = _general_signature_headers(signature_source)
        if verification_profile == "rrcv2_synthetic_v1":
            validate_synthetic_source(source, kind=PolicyKind.CANDIDATE)
            target = synthetic_target
            if target is None and signature_source is not None:
                if (
                    not signature_source
                    or len(signature_source.encode("utf-8", errors="strict")) > MAX_SPEC_FIELD_BYTES
                    or signature_source != unicodedata.normalize("NFC", signature_source)
                    or "\r" in signature_source
                    or "\x00" in signature_source
                ):
                    raise ValueError("synthetic signature violates the text or size policy")
                names = tuple(_headers(signature_source))
                target = names[0] if len(names) == 1 and "." not in names[0] else None
            if target is None:
                raise ValueError("synthetic verification requires one declared target")
            resolved_synthetic_target = target
            assert signature_source is not None
            synthetic_prelude, runtime_source_raw = _synthetic_runtime_source(
                signature_source,
                target=target,
                body_source=source,
            )
            required_signature_headers = _headers(signature_source)
        else:
            runtime_source_raw = source_raw
        source_nodes = sum(1 for _ in ast.walk(ast.parse(runtime_source_raw)))
        test_files = _validate_tests(suite, source_nodes)
        if (
            len(runtime_source_raw) + sum(len(item.source) for item in test_files)
            > MAX_ARTIFACT_BYTES
        ):
            raise ValueError("attempt source and tests exceed the aggregate byte cap")
        if verification_profile == "rrcv2_synthetic_v1":
            expected: list[str] = []
            for file, test in zip(test_files, suite.all, strict=True):
                policy = validate_synthetic_source(test, kind=PolicyKind.TEST, target=target)
                if len(policy.declared_functions) != 1:
                    raise ValueError("synthetic test must declare exactly one node")
                expected.append(f"{file.filename}::{policy.declared_functions[0]}")
            expected_synthetic_nodes = tuple(expected)
    except (SyntaxError, SyntheticPolicyError, UnicodeError, ValueError) as exc:
        raw = source.encode("utf-8", errors="replace")[:MAX_ARTIFACT_BYTES]
        tiers.append(record("assembly", False, raw, b"failed\n", str(exc).encode()))
        return _finish(
            attempt_id=attempt_id,
            verification_profile=verification_profile,
            artifact_path=artifact_path,
            code_artifact_sha256=_sha(raw),
            tiers=tiers,
            accepted=False,
            normalized_source=None,
            evidence_blobs=evidence_blobs,
            tier_sandbox_evidence=tier_sandbox_evidence,
            repair_failure=failure_observation,
        )

    artifact = CodeArtifactV1(attempt_id, artifact_path, _without_terminal_lf(source))
    artifact_raw = _artifact_bytes(artifact)
    tiers.append(record("assembly", True, artifact_raw, b"passed\n", b""))

    ruff = sandbox.run(
        tier="ruff",
        verification_profile=verification_profile,
        artifact_path=artifact_path,
        source=runtime_source_raw,
        tests=(),
        selected_node_ids=(),
        limits=limits,
    )
    capture(ruff, sandbox_tier="ruff", verification_tier="ruff")
    normalized = ruff.normalized_source
    ruff_ok = ruff.exit_code == 0 and normalized is not None
    ruff_diagnostics = ruff.stdout + ruff.stderr
    tiers.append(
        record(
            "ruff",
            ruff_ok,
            artifact_raw,
            b"passed\n" if ruff_ok else b"failed\n",
            ruff_diagnostics,
        )
    )
    if not ruff_ok:
        return _finish(
            attempt_id=attempt_id,
            verification_profile=verification_profile,
            artifact_path=artifact_path,
            code_artifact_sha256=_sha(artifact_raw),
            tiers=tiers,
            accepted=False,
            normalized_source=artifact.source,
            evidence_blobs=evidence_blobs,
            tier_sandbox_evidence=tier_sandbox_evidence,
            repair_failure=failure_observation,
        )
    assert normalized is not None
    try:
        normalized_runtime_text = normalized.decode("utf-8", errors="strict")
        if verification_profile == "rrcv2_synthetic_v1":
            assert (
                synthetic_prelude is not None
                and signature_source is not None
                and resolved_synthetic_target is not None
            )
            normalized_text, normalized_body = _normalized_synthetic_source(
                normalized_runtime_text,
                expected_prelude=synthetic_prelude,
                signature_source=signature_source,
                target=resolved_synthetic_target,
            )
        else:
            normalized_text = _without_terminal_lf(normalized_runtime_text)
            normalized_body = normalized_text
        normalized_raw = _validate_artifact(attempt_id, artifact_path, normalized_text)
        normalized_nodes = sum(1 for _ in ast.walk(ast.parse(normalized_raw)))
        revalidated_tests = _validate_tests(suite, normalized_nodes)
        if revalidated_tests != test_files:
            raise ValueError("normalized attempt test authority drifted")
        if (
            len(normalized_raw) + sum(len(item.source) for item in revalidated_tests)
            > MAX_ARTIFACT_BYTES
        ):
            raise ValueError("normalized attempt source and tests exceed the aggregate byte cap")
        if verification_profile == "rrcv2_synthetic_v1":
            validate_synthetic_source(normalized_body, kind=PolicyKind.CANDIDATE)
    except (SyntaxError, SyntheticPolicyError, UnicodeError, ValueError) as exc:
        tiers[-1] = record("ruff", False, artifact_raw, b"failed\n", str(exc).encode())
        return _finish(
            attempt_id=attempt_id,
            verification_profile=verification_profile,
            artifact_path=artifact_path,
            code_artifact_sha256=_sha(artifact_raw),
            tiers=tiers,
            accepted=False,
            normalized_source=artifact.source,
            evidence_blobs=evidence_blobs,
            tier_sandbox_evidence=tier_sandbox_evidence,
            repair_failure=failure_observation,
        )
    artifact = CodeArtifactV1(attempt_id, artifact_path, normalized_text)
    artifact_raw = _artifact_bytes(artifact)
    tiers[0] = _tier("assembly", True, artifact_raw, b"passed\n", b"")
    tiers[1] = _tier("ruff", True, artifact_raw, b"passed\n", b"")

    if signature_source is not None:
        signature_ok = _signature_matches(
            normalized_body,
            signature_source,
            required=required_signature_headers,
        )
        tiers.append(
            record(
                "signature_conformance",
                signature_ok,
                artifact_raw,
                b"passed\n" if signature_ok else b"failed\n",
                b"" if signature_ok else b"required signature differs",
            )
        )
        if not signature_ok:
            return _finish(
                attempt_id=attempt_id,
                verification_profile=verification_profile,
                artifact_path=artifact_path,
                code_artifact_sha256=_sha(artifact_raw),
                tiers=tiers,
                accepted=False,
                normalized_source=normalized_text,
                evidence_blobs=evidence_blobs,
                tier_sandbox_evidence=tier_sandbox_evidence,
                repair_failure=failure_observation,
            )

    typing = sandbox.run(
        tier="pyright",
        verification_profile=verification_profile,
        artifact_path=artifact_path,
        source=normalized_text.encode(),
        tests=(),
        selected_node_ids=(),
        limits=limits,
    )
    capture(typing, sandbox_tier="pyright", verification_tier="pyright")
    typing_ok = typing.exit_code == 0
    tiers.append(
        record(
            "pyright",
            typing_ok,
            artifact_raw,
            b"passed\n" if typing_ok else b"failed\n",
            typing.stdout + typing.stderr,
        )
    )
    if not typing_ok:
        return _finish(
            attempt_id=attempt_id,
            verification_profile=verification_profile,
            artifact_path=artifact_path,
            code_artifact_sha256=_sha(artifact_raw),
            tiers=tiers,
            accepted=False,
            normalized_source=normalized_text,
            evidence_blobs=evidence_blobs,
            tier_sandbox_evidence=tier_sandbox_evidence,
            repair_failure=failure_observation,
        )

    collected = sandbox.run(
        tier="pytest_collect",
        verification_profile=verification_profile,
        artifact_path=artifact_path,
        source=normalized_text.encode(),
        tests=test_files,
        selected_node_ids=(),
        limits=limits,
    )
    capture(collected, sandbox_tier="pytest_collect", verification_tier="pytest")
    node_ids = collected.collected_node_ids
    if (
        collected.exit_code != 0
        or not node_ids
        or (expected_synthetic_nodes is not None and node_ids != expected_synthetic_nodes)
    ):
        tiers.append(
            record(
                "pytest",
                False,
                artifact_raw,
                b"failed\n",
                collected.stdout + collected.stderr,
            )
        )
        return _finish(
            attempt_id=attempt_id,
            verification_profile=verification_profile,
            artifact_path=artifact_path,
            code_artifact_sha256=_sha(artifact_raw),
            tiers=tiers,
            accepted=False,
            normalized_source=normalized_text,
            evidence_blobs=evidence_blobs,
            tier_sandbox_evidence=tier_sandbox_evidence,
            collection_node_ids=node_ids,
            completed_node_ids=(),
            collection_tests=test_files,
            repair_failure=failure_observation,
        )
    if verification_profile == "rrcv2_synthetic_v1":
        assert expected_synthetic_nodes is not None
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        stdout_retained = 0
        stderr_retained = 0
        completed_nodes: list[str] = []
        execution_ok = True
        for node_id in expected_synthetic_nodes:
            execution = sandbox.run(
                tier="pytest",
                verification_profile=verification_profile,
                artifact_path=artifact_path,
                source=normalized_text.encode(),
                tests=test_files,
                selected_node_ids=(node_id,),
                limits=limits,
            )
            capture(execution, sandbox_tier="pytest", verification_tier="pytest")
            stdout_retained = _bounded_evidence_append(
                stdout_parts,
                execution.stdout,
                retained=stdout_retained,
                cap=limits.stdout_bytes,
                label="synthetic pytest",
            )
            stderr_retained = _bounded_evidence_append(
                stderr_parts,
                execution.stderr,
                retained=stderr_retained,
                cap=limits.stderr_bytes,
                label="synthetic pytest",
            )
            completed_nodes.extend(execution.completed_node_ids)
            if (
                execution.exit_code != 0
                or execution.collected_node_ids != (node_id,)
                or execution.completed_node_ids != (node_id,)
            ):
                execution_ok = False
                break
        executed_stdout = b"".join(stdout_parts)
        executed_stderr = b"".join(stderr_parts)
        pytest_ok = execution_ok and tuple(completed_nodes) == expected_synthetic_nodes
    else:
        executed = sandbox.run(
            tier="pytest",
            verification_profile=verification_profile,
            artifact_path=artifact_path,
            source=normalized_text.encode(),
            tests=test_files,
            selected_node_ids=(),
            limits=limits,
        )
        capture(executed, sandbox_tier="pytest", verification_tier="pytest")
        executed_stdout = executed.stdout
        executed_stderr = executed.stderr
        pytest_ok = (
            executed.exit_code == 0
            and executed.collected_node_ids == node_ids
            and executed.completed_node_ids == node_ids
        )
    tiers.append(
        record(
            "pytest",
            pytest_ok,
            artifact_raw,
            b"passed\n" if pytest_ok else b"failed\n",
            executed_stdout + executed_stderr,
        )
    )
    return _finish(
        attempt_id=attempt_id,
        verification_profile=verification_profile,
        artifact_path=artifact_path,
        code_artifact_sha256=_sha(artifact_raw),
        tiers=tiers,
        accepted=pytest_ok,
        normalized_source=normalized_text,
        evidence_blobs=evidence_blobs,
        tier_sandbox_evidence=tier_sandbox_evidence,
        collection_node_ids=node_ids,
        completed_node_ids=(
            tuple(completed_nodes)
            if verification_profile == "rrcv2_synthetic_v1"
            else executed.completed_node_ids
        ),
        collection_tests=test_files,
        repair_failure=failure_observation,
    )


def score_oracle(
    *,
    artifact: CodeArtifactV1,
    verification_profile: Profile,
    oracle_tests: tuple[str, ...],
    sandbox: VerifierSandboxPort,
    synthetic_target: str | None = None,
    signature_source: str | None = None,
    limits: SandboxLimitsV1 = SandboxLimitsV1(),
) -> OracleResultV1:
    """Score hidden tests in fresh sandboxes without creating an acceptance tier."""

    if verification_profile not in PROFILES:
        raise ValueError("unsupported verification profile")
    source_raw = _validate_code_artifact(
        artifact.attempt_id, artifact.artifact_path, artifact.source
    )
    if not oracle_tests or len(oracle_tests) > 64 or len(set(oracle_tests)) != len(oracle_tests):
        raise ValueError("oracle tests must be a bounded unique nonempty tuple")
    files: list[SandboxTestFileV1] = []
    test_nodes = 0
    for index, test in enumerate(oracle_tests):
        if test != unicodedata.normalize("NFC", test) or "\r" in test or "\x00" in test:
            raise ValueError("oracle test violates the text policy")
        raw = test.encode("utf-8", errors="strict")
        if not raw or len(raw) > MAX_TEST_BYTES:
            raise ValueError("oracle test violates the size policy")
        nodes = sum(1 for _ in ast.walk(ast.parse(test)))
        if nodes > MAX_FILE_AST_NODES:
            raise ValueError("oracle test exceeds the per-file AST cap")
        test_nodes += nodes
        files.append(SandboxTestFileV1("oracle", index, raw))
    if verification_profile == "rrcv2_synthetic_v1":
        if synthetic_target is None or signature_source is None:
            raise ValueError("synthetic oracle scoring requires the signature and declared target")
        body_source = _synthetic_artifact_body(
            artifact.source,
            signature_source=signature_source,
            target=synthetic_target,
        )
        validate_synthetic_source(body_source, kind=PolicyKind.CANDIDATE)
        if not _signature_matches(body_source, signature_source):
            raise ValueError("synthetic oracle artifact signature differs")
        runtime_source_raw = source_raw
        expected_oracle_nodes = tuple(
            f"{file.filename}::{validate_synthetic_source(test, kind=PolicyKind.TEST, target=synthetic_target).declared_functions[0]}"
            for file, test in zip(files, oracle_tests, strict=True)
        )
    else:
        if synthetic_target is not None or signature_source is not None:
            raise ValueError("general oracle scoring cannot accept synthetic authority")
        expected_oracle_nodes = None
        runtime_source_raw = source_raw
    if (
        sum(1 for _ in ast.walk(ast.parse(runtime_source_raw))) + test_nodes > MAX_AST_NODES
        or len(runtime_source_raw) + sum(len(file.source) for file in files) > MAX_ARTIFACT_BYTES
    ):
        raise ValueError("oracle attempt exceeds aggregate caps")
    test_files = tuple(files)
    collected = sandbox.run(
        tier="pytest_collect",
        verification_profile=verification_profile,
        artifact_path=artifact.artifact_path,
        source=runtime_source_raw,
        tests=test_files,
        selected_node_ids=(),
        limits=limits,
    )
    validate_sandbox_result(collected, expected_tier="pytest_collect", limits=limits)
    node_ids = collected.collected_node_ids
    diagnostic_parts: list[bytes] = []
    diagnostics_retained = 0
    diagnostics_cap = limits.stdout_bytes + limits.stderr_bytes
    diagnostics_retained = _bounded_evidence_append(
        diagnostic_parts,
        collected.stdout + collected.stderr,
        retained=diagnostics_retained,
        cap=diagnostics_cap,
        label="oracle diagnostics",
    )
    completed: list[str] = []
    passed = (
        collected.exit_code == 0
        and bool(node_ids)
        and (expected_oracle_nodes is None or node_ids == expected_oracle_nodes)
    )
    if passed and verification_profile == "rrcv2_synthetic_v1":
        assert expected_oracle_nodes is not None
        for node_id in expected_oracle_nodes:
            execution = sandbox.run(
                tier="pytest",
                verification_profile=verification_profile,
                artifact_path=artifact.artifact_path,
                source=runtime_source_raw,
                tests=test_files,
                selected_node_ids=(node_id,),
                limits=limits,
            )
            validate_sandbox_result(execution, expected_tier="pytest", limits=limits)
            diagnostics_retained = _bounded_evidence_append(
                diagnostic_parts,
                execution.stdout + execution.stderr,
                retained=diagnostics_retained,
                cap=diagnostics_cap,
                label="oracle diagnostics",
            )
            completed.extend(execution.completed_node_ids)
            if (
                execution.exit_code != 0
                or execution.collected_node_ids != (node_id,)
                or execution.completed_node_ids != (node_id,)
            ):
                passed = False
                break
    elif passed:
        execution = sandbox.run(
            tier="pytest",
            verification_profile=verification_profile,
            artifact_path=artifact.artifact_path,
            source=runtime_source_raw,
            tests=test_files,
            selected_node_ids=(),
            limits=limits,
        )
        validate_sandbox_result(execution, expected_tier="pytest", limits=limits)
        diagnostics_retained = _bounded_evidence_append(
            diagnostic_parts,
            execution.stdout + execution.stderr,
            retained=diagnostics_retained,
            cap=diagnostics_cap,
            label="oracle diagnostics",
        )
        completed.extend(execution.completed_node_ids)
        passed = (
            execution.exit_code == 0
            and execution.collected_node_ids == node_ids
            and execution.completed_node_ids == node_ids
        )
    diagnostics = b"".join(diagnostic_parts)
    return OracleResultV1(
        attempt_id=artifact.attempt_id,
        code_artifact_sha256=_sha(_artifact_bytes(artifact)),
        passed=passed and tuple(completed) == (expected_oracle_nodes or node_ids),
        collected_node_ids=node_ids,
        completed_node_ids=tuple(completed),
        diagnostics_sha256=_sha(diagnostics),
    )


def run_pytest(
    code: str,
    tests: str | tuple[str, ...],
    timeout: float = 20,
    *,
    sandbox: VerifierSandboxPort | None = None,
) -> tuple[bool, str]:
    """Compatibility adapter over the sealed structured verifier; never executes on the host."""

    if timeout != SandboxLimitsV1().wall_seconds:
        return (
            False,
            f"unsupported pytest timeout {timeout:g}; certified sandbox timeout is 20 seconds",
        )
    test_tuple = (tests,) if isinstance(tests, str) else tests
    attempt_id = _sha(_canonical({"code": code, "tests": list(test_tuple), "v": 1}))
    backend = sandbox or SealedDockerSandbox(Path(__file__).resolve().parents[2])
    try:
        run = verify_candidate(
            attempt_id=attempt_id,
            verification_profile="rrcv2_general_v1",
            artifact_path="solution.py",
            source=code,
            tests=test_tuple,
            sandbox=backend,
        )
    except (SandboxExecutionError, SandboxUnavailable, ValueError) as exc:
        return False, f"sealed verifier failed: {exc}"
    output = run.repair_evidence.excerpt if run.repair_evidence is not None else ""
    return run.public_accepted, output
