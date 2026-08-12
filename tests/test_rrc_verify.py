from __future__ import annotations

import ast
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import runpy
import shutil
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import Callable, TypedDict, cast

import pytest
import rrc.pipeline.sandbox as sandbox_module
from rrc.pipeline.sandbox import (
    CommandObservation,
    SandboxExecutionError,
    SandboxInvocationEvidenceV1,
    SandboxLimitsV1,
    SandboxResultV1,
    SandboxTestFileV1,
    SandboxTierExecutionV1,
    SealedDockerSandbox,
)
from rrc.pipeline.verify import (
    MAX_ARTIFACT_BYTES,
    ArtifactRecordV1,
    CodeArtifactV1,
    PytestExecutionEvidenceV1,
    RepairEvidenceV1,
    VerificationResultV1,
    VerificationRunV1,
    VerificationTestsV1,
    VerificationTierRowV1,
    _canonical,
    _diagnostic_excerpt,
    _normalized_type_prelude,
    _synthetic_type_prelude,
    artifact_record_bytes,
    code_artifact_bytes,
    collection_evidence_bytes,
    parse_code_artifact,
    parse_collection_evidence,
    parse_repair_evidence,
    parse_verification_result,
    reopen_verification_evidence,
    repair_evidence_bytes,
    score_oracle,
    tier_execution_evidence_bytes,
    verification_result_bytes,
    verify_candidate,
)


def test_product_smoke_sandbox_child_registry_gates_execution_and_labels_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "children"
    registry.mkdir(mode=0o700)
    request = tmp_path / "request"
    nonce = "e" * 32
    monkeypatch.setenv("RRCV2_PRODUCT_SMOKE", "1")
    monkeypatch.setenv("RRCV2_SMOKE_RUN_NONCE", nonce)
    monkeypatch.setenv("RRCV2_SMOKE_CHILD_REGISTRY", str(registry))
    monkeypatch.setenv("RRCV2_SMOKE_CANCELLATION_REQUEST", str(request))
    command = (sys.executable, "-c", "print('registered')")
    process, row_path = sandbox_module._registered_popen(  # noqa: SLF001
        command, env=dict(os.environ)
    )
    stdout, stderr = process.communicate(timeout=5)
    sandbox_module._finish_registered(row_path, process.returncode)  # noqa: SLF001
    assert (process.returncode, stdout, stderr) == (0, b"registered\n", b"")
    row = json.loads(next(registry.iterdir()).read_bytes())
    assert row["state"] == "terminal" and row["returncode"] == 0
    argv = ("docker", "--context", "rrcv2-verifier", "run", "--rm", "image")
    labelled = sandbox_module._smoke_label_argv(argv)  # noqa: SLF001
    assert labelled == (
        "docker",
        "--context",
        "rrcv2-verifier",
        "run",
        "--label",
        f"org.contextmesh.rrcv2-smoke={nonce}",
        "--rm",
        "image",
    )


def test_product_smoke_sandbox_registry_failure_does_not_release_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "children"
    registry.write_text("poison")
    sentinel = tmp_path / "ran"
    monkeypatch.setenv("RRCV2_PRODUCT_SMOKE", "1")
    monkeypatch.setenv("RRCV2_SMOKE_RUN_NONCE", "f" * 32)
    monkeypatch.setenv("RRCV2_SMOKE_CHILD_REGISTRY", str(registry))
    monkeypatch.setenv("RRCV2_SMOKE_CANCELLATION_REQUEST", str(tmp_path / "request"))
    with pytest.raises(sandbox_module.SandboxExecutionError, match="registry"):
        sandbox_module._registered_popen(  # noqa: SLF001
            (
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(sentinel)!r}).touch()",
            ),
            env=dict(os.environ),
        )
    assert not sentinel.exists()


ATTEMPT = "a" * 64
SOURCE = "def add(x: int, y: int) -> int:\n    return x + y\n"
TEST = "def test_add():\n    assert add(1, 2) == 3\n"


class _ReopenAuthorities(TypedDict):
    expected_result_sha256: str
    artifact_raw: bytes | None
    expected_artifact_record_sha256: str | None
    artifact_record_raw: bytes | None
    artifact_blob_raw: bytes | None
    expected_tier_execution_sha256s: tuple[str, ...]
    tier_execution_raws: tuple[bytes, ...]
    expected_execution_sha256: str | None
    expected_repair_sha256: str | None


def _reopen_authorities(run: VerificationRunV1) -> _ReopenAuthorities:
    result_raw = verification_result_bytes(run.result)
    tier_raws = tuple(tier_execution_evidence_bytes(row) for row in run.tier_execution_evidence)
    execution_raw = (
        collection_evidence_bytes(run.execution_evidence)
        if run.execution_evidence is not None
        else None
    )
    repair_raw = (
        repair_evidence_bytes(run.repair_evidence) if run.repair_evidence is not None else None
    )
    if not run.public_accepted:
        return {
            "expected_result_sha256": hashlib.sha256(result_raw).hexdigest(),
            "artifact_raw": code_artifact_bytes(run.artifact) if run.artifact is not None else None,
            "expected_artifact_record_sha256": None,
            "artifact_record_raw": None,
            "artifact_blob_raw": None,
            "expected_tier_execution_sha256s": tuple(
                hashlib.sha256(raw).hexdigest() for raw in tier_raws
            ),
            "tier_execution_raws": tier_raws,
            "expected_execution_sha256": (
                hashlib.sha256(execution_raw).hexdigest() if execution_raw is not None else None
            ),
            "expected_repair_sha256": (
                hashlib.sha256(repair_raw).hexdigest() if repair_raw is not None else None
            ),
        }
    assert run.artifact is not None
    assert execution_raw is not None
    blob = run.artifact.source.encode()
    blob_sha = hashlib.sha256(blob).hexdigest()
    record = ArtifactRecordV1(
        attempt_id=run.artifact.attempt_id,
        artifact_path=run.artifact.artifact_path,
        source_sha256=blob_sha,
        source_bytes=len(blob),
        blob_path=".rrcv2/artifacts/accepted-code.v1.utf8",
        blob_sha256=blob_sha,
        blob_bytes=len(blob),
    )
    record_raw = artifact_record_bytes(record)
    return {
        "expected_result_sha256": hashlib.sha256(result_raw).hexdigest(),
        "artifact_raw": code_artifact_bytes(run.artifact),
        "expected_artifact_record_sha256": hashlib.sha256(record_raw).hexdigest(),
        "artifact_record_raw": record_raw,
        "artifact_blob_raw": blob,
        "expected_tier_execution_sha256s": tuple(
            hashlib.sha256(raw).hexdigest() for raw in tier_raws
        ),
        "tier_execution_raws": tier_raws,
        "expected_execution_sha256": hashlib.sha256(execution_raw).hexdigest(),
        "expected_repair_sha256": None,
    }


def test_canonical_json_recursively_normalizes_nfc_and_rejects_ambiguous_values() -> None:
    composed = _canonical({"x": "é"})
    decomposed = _canonical({"x": "e\u0301"})
    assert composed == decomposed == b'{"x":"\xc3\xa9"}'
    assert hashlib.sha256(composed).hexdigest() == (
        "97f06f396a709c3a29824e1cc794eeb98e2d1a262d7d455439d286d42803f0fe"
    )
    with pytest.raises(ValueError, match="collide"):
        _canonical({"é": 1, "e\u0301": 2})
    for invalid in (1.0, float("nan"), {1: "not a string key"}, {"x": object()}):
        with pytest.raises((TypeError, ValueError)):
            _canonical(invalid)


def test_code_artifact_parser_requires_exact_canonical_authority() -> None:
    artifact = CodeArtifactV1(ATTEMPT, "unicodé/solution.py", SOURCE.removesuffix("\n"))
    raw = code_artifact_bytes(artifact)
    assert parse_code_artifact(raw) == artifact
    poisoned = json.loads(raw)
    poisoned["artifact_path"] = "unicode\u0301/solution.py"
    poisoned_raw = json.dumps(
        poisoned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    with pytest.raises(ValueError):
        parse_code_artifact(poisoned_raw)
    terminal_lf = _canonical(
        {
            "artifact_path": "solution.py",
            "attempt_id": ATTEMPT,
            "source": "def f() -> int:\n    return 1\n",
            "v": 1,
        }
    )
    with pytest.raises(ValueError, match="terminal LF"):
        parse_code_artifact(terminal_lf)


def test_frozen_code_artifact_and_verification_result_goldens() -> None:
    artifact = CodeArtifactV1(
        "1" * 64,
        "solution.py",
        "def get_order(x: int) -> int:\n    return 1",
    )
    artifact_raw = code_artifact_bytes(artifact)
    artifact_sha = hashlib.sha256(artifact_raw).hexdigest()
    assert (len(artifact_raw), artifact_sha) == (
        172,
        "8b854e1756bdc4299c2d71bd7a60e263299dc775c7b6aa4004fbe0f8fcb9e5a6",
    )
    passed_sha = hashlib.sha256(b"passed\n").hexdigest()
    empty_sha = hashlib.sha256(b"").hexdigest()

    def row(name):  # type: ignore[no-untyped-def]
        return VerificationTierRowV1(name, "passed", artifact_sha, passed_sha, empty_sha)

    direct = VerificationResultV1(
        "1" * 64,
        "rrcv2_general_v1",
        artifact_sha,
        tuple(row(name) for name in ("assembly", "ruff", "pyright", "pytest")),
        True,
    )
    spec = VerificationResultV1(
        "1" * 64,
        "rrcv2_synthetic_v1",
        artifact_sha,
        tuple(
            row(name)
            for name in (
                "assembly",
                "ruff",
                "signature_conformance",
                "pyright",
                "pytest",
            )
        ),
        True,
    )
    assert (
        len(verification_result_bytes(direct)),
        hashlib.sha256(verification_result_bytes(direct)).hexdigest(),
    ) == (
        1433,
        "afbc5b65311e44c75e2587881917467bc1e290d1000ab618aadbd101cff3ab4d",
    )
    assert (
        len(verification_result_bytes(spec)),
        hashlib.sha256(verification_result_bytes(spec)).hexdigest(),
    ) == (
        1745,
        "8ff3406942b1e2ff209cda3596bb2181373e048b33c61ed51c6cc7c110dbab97",
    )
    assert parse_verification_result(verification_result_bytes(direct)) == direct
    assert parse_verification_result(verification_result_bytes(spec)) == spec


def test_frozen_execution_and_repair_evidence_goldens() -> None:
    execution = PytestExecutionEvidenceV1(
        ATTEMPT,
        "rrcv2_general_v1",
        "b" * 64,
        (("test_public_00.py", "c" * 64),),
        ("test_public_00.py::test_value",),
        ("test_public_00.py::test_value",),
    )
    repair = RepairEvidenceV1(
        ATTEMPT,
        "b" * 64,
        "pytest",
        hashlib.sha256(b"failed detail").hexdigest(),
        "failed detail",
    )
    execution_raw = collection_evidence_bytes(execution)
    repair_raw = repair_evidence_bytes(repair)
    assert (len(execution_raw), hashlib.sha256(execution_raw).hexdigest()) == (
        461,
        "4c1aab12943824b2e9a32e2555c39ad98d387e74097a876ed8f5c796e33d1ade",
    )
    assert (len(repair_raw), hashlib.sha256(repair_raw).hexdigest()) == (
        316,
        "3beb6819a3c0055f01b0ffe10d2461374e9434e5e80deaa888d478a9f2564632",
    )
    assert parse_collection_evidence(execution_raw) == execution
    assert parse_repair_evidence(repair_raw) == repair


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: {**value, "unknown": 1},
        lambda value: {**value, "completed_node_ids": ["wrong::node", "wrong::node"]},
        lambda value: {**value, "verification_result_sha256": "0" * 63},
    ),
)
def test_execution_evidence_reopen_rejects_mutated_authority(mutate) -> None:  # type: ignore[no-untyped-def]
    evidence = PytestExecutionEvidenceV1(
        ATTEMPT,
        "rrcv2_general_v1",
        "b" * 64,
        (("test_public_00.py", "c" * 64),),
        ("test_public_00.py::test_value",),
        ("test_public_00.py::test_value",),
    )
    value = json.loads(collection_evidence_bytes(evidence))
    poisoned = json.dumps(
        mutate(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    with pytest.raises(ValueError):
        parse_collection_evidence(poisoned)


def test_execution_evidence_reopens_wrong_completion_as_negative_observation() -> None:
    evidence = PytestExecutionEvidenceV1(
        ATTEMPT,
        "rrcv2_general_v1",
        "b" * 64,
        (("test_public_00.py", "c" * 64),),
        ("test_public_00.py::test_value",),
        ("wrong::node",),
    )
    assert parse_collection_evidence(collection_evidence_bytes(evidence)) == evidence


def test_repair_evidence_reopen_rejects_excerpt_hash_drift() -> None:
    repair = RepairEvidenceV1(
        ATTEMPT,
        "b" * 64,
        "pytest",
        hashlib.sha256(b"failed detail").hexdigest(),
        "failed detail",
    )
    value = json.loads(repair_evidence_bytes(repair))
    value["excerpt"] = "different"
    poisoned = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ValueError):
        parse_repair_evidence(poisoned)


def test_result_aware_reopen_accepts_exact_positive_and_negative_tuples() -> None:
    accepted = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=FakeSandbox(),
    )
    failed = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=FakeSandbox({"pyright": SandboxResultV1("pyright", 1, b"type failure", b"")}),
    )
    failed_pytest = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=FakeSandbox(
            {
                "pytest": SandboxResultV1(
                    "pytest",
                    0,
                    b"wrong completion",
                    b"",
                    collected_node_ids=("test_public_00.py::test_add",),
                    completed_node_ids=("wrong::node",),
                )
            }
        ),
    )
    assert accepted.execution_evidence is not None
    assert accepted.artifact is not None
    assert failed.artifact is not None
    assert failed_pytest.artifact is not None
    assert failed.repair_evidence is not None
    assert failed_pytest.execution_evidence is not None
    assert failed_pytest.repair_evidence is not None
    assert reopen_verification_evidence(
        result_raw=verification_result_bytes(accepted.result),
        **_reopen_authorities(accepted),
        execution_raw=collection_evidence_bytes(accepted.execution_evidence),
        repair_raw=None,
    ) == (accepted.result, accepted.execution_evidence, None)
    assert reopen_verification_evidence(
        result_raw=verification_result_bytes(failed.result),
        **_reopen_authorities(failed),
        execution_raw=None,
        repair_raw=repair_evidence_bytes(failed.repair_evidence),
    ) == (failed.result, None, failed.repair_evidence)
    assert reopen_verification_evidence(
        result_raw=verification_result_bytes(failed_pytest.result),
        **_reopen_authorities(failed_pytest),
        execution_raw=collection_evidence_bytes(failed_pytest.execution_evidence),
        repair_raw=repair_evidence_bytes(failed_pytest.repair_evidence),
    ) == (
        failed_pytest.result,
        failed_pytest.execution_evidence,
        failed_pytest.repair_evidence,
    )

    assembly_failed = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source="def broken(:\n",
        tests=(TEST,),
        sandbox=FakeSandbox(),
    )
    assert assembly_failed.artifact is None
    assert assembly_failed.repair_evidence is not None
    assert reopen_verification_evidence(
        result_raw=verification_result_bytes(assembly_failed.result),
        **_reopen_authorities(assembly_failed),
        execution_raw=None,
        repair_raw=repair_evidence_bytes(assembly_failed.repair_evidence),
    ) == (assembly_failed.result, None, assembly_failed.repair_evidence)


def test_result_aware_reopen_rejects_every_sidecar_binding_drift() -> None:
    accepted = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=FakeSandbox(),
    )
    failed = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=FakeSandbox({"pyright": SandboxResultV1("pyright", 1, b"type failure", b"")}),
    )
    assert accepted.execution_evidence is not None
    assert accepted.artifact is not None
    assert failed.artifact is not None
    assert failed.repair_evidence is not None
    accepted_raw = verification_result_bytes(accepted.result)
    failed_raw = verification_result_bytes(failed.result)
    execution = accepted.execution_evidence
    repair = failed.repair_evidence
    poisoned_executions = (
        replace(execution, attempt_id="b" * 64),
        replace(execution, verification_profile="rrcv2_synthetic_v1"),
        replace(execution, verification_result_sha256="b" * 64),
        replace(execution, completed_node_ids=()),
        replace(execution, completed_node_ids=("wrong::node",)),
    )
    for poisoned in poisoned_executions:
        with pytest.raises(ValueError):
            reopen_verification_evidence(
                result_raw=accepted_raw,
                **_reopen_authorities(accepted),
                execution_raw=collection_evidence_bytes(poisoned),
                repair_raw=None,
            )
    poisoned_repairs = (
        replace(repair, attempt_id="b" * 64),
        replace(repair, verification_result_sha256="b" * 64),
        replace(repair, failed_tier="assembly"),
    )
    for poisoned in poisoned_repairs:
        with pytest.raises(ValueError):
            reopen_verification_evidence(
                result_raw=failed_raw,
                **_reopen_authorities(failed),
                execution_raw=None,
                repair_raw=repair_evidence_bytes(poisoned),
            )
    with pytest.raises(ValueError):
        reopen_verification_evidence(
            result_raw=accepted_raw,
            **_reopen_authorities(accepted),
            execution_raw=None,
            repair_raw=None,
        )
    with pytest.raises(ValueError):
        reopen_verification_evidence(
            result_raw=failed_raw,
            **_reopen_authorities(failed),
            execution_raw=None,
            repair_raw=None,
        )


def test_tier_execution_sidecars_are_durable_and_require_trusted_inventory_hashes() -> None:
    run = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=FakeSandbox(),
    )
    assert [row.verification_tier for row in run.tier_execution_evidence] == [
        "ruff",
        "pyright",
        "pytest",
        "pytest",
    ]
    raws = tuple(tier_execution_evidence_bytes(row) for row in run.tier_execution_evidence)
    assert all(raw in run.evidence_blobs for raw in raws)
    authorities = _reopen_authorities(run)
    poisoned = replace(
        run.tier_execution_evidence[0],
        sandbox=replace(
            run.tier_execution_evidence[0].sandbox,
            backend_before_sha256="9" * 64,
            backend_after_sha256="9" * 64,
        ),
    )
    authorities["tier_execution_raws"] = (
        tier_execution_evidence_bytes(poisoned),
        *raws[1:],
    )
    assert run.execution_evidence is not None
    with pytest.raises(ValueError, match="trusted inventory"):
        reopen_verification_evidence(
            result_raw=verification_result_bytes(run.result),
            **authorities,
            execution_raw=collection_evidence_bytes(run.execution_evidence),
            repair_raw=None,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("capability_sha256", "0" * 64),
        ("runtime_lock_sha256", "0" * 64),
        ("image_config_digest", "sha256:" + "0" * 64),
    ),
)
def test_tier_execution_sidecar_rejects_stable_wrong_runtime_authority(
    field: str, value: str
) -> None:
    run = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=FakeSandbox(),
    )
    assert run.execution_evidence is not None
    authorities = _reopen_authorities(run)
    first = run.tier_execution_evidence[0]
    poisoned = replace(first, sandbox=replace(first.sandbox, **{field: value}))
    poisoned_raw = tier_execution_evidence_bytes(poisoned)
    raws = (poisoned_raw, *authorities["tier_execution_raws"][1:])
    authorities["tier_execution_raws"] = raws
    authorities["expected_tier_execution_sha256s"] = tuple(
        hashlib.sha256(raw).hexdigest() for raw in raws
    )
    with pytest.raises(ValueError, match="tier sandbox execution authority"):
        reopen_verification_evidence(
            result_raw=verification_result_bytes(run.result),
            **authorities,
            execution_raw=collection_evidence_bytes(run.execution_evidence),
            repair_raw=None,
        )


def test_result_reopen_rejects_every_forged_tier_hash_relation() -> None:
    accepted = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=FakeSandbox(),
    )
    failed = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=FakeSandbox({"pyright": SandboxResultV1("pyright", 1, b"type failure", b"")}),
    )
    assert accepted.execution_evidence is not None
    assert accepted.artifact is not None
    assert failed.artifact is not None
    assert failed.repair_evidence is not None
    accepted_row = accepted.result.tiers[0]
    failed_row = failed.result.tiers[-1]
    poisoned_results = (
        replace(
            accepted.result,
            tiers=(replace(accepted_row, input_sha256="0" * 64), *accepted.result.tiers[1:]),
        ),
        replace(
            accepted.result,
            tiers=(replace(accepted_row, output_sha256="0" * 64), *accepted.result.tiers[1:]),
        ),
        replace(
            accepted.result,
            tiers=(
                replace(accepted_row, diagnostics_sha256="0" * 64),
                *accepted.result.tiers[1:],
            ),
        ),
        replace(
            failed.result,
            tiers=(*failed.result.tiers[:-1], replace(failed_row, output_sha256="0" * 64)),
        ),
        replace(
            failed.result,
            tiers=(
                *failed.result.tiers[:-1],
                replace(failed_row, diagnostics_sha256="0" * 64),
            ),
        ),
    )
    for poisoned in poisoned_results:
        result_raw = verification_result_bytes(poisoned)
        with pytest.raises(ValueError):
            parse_verification_result(result_raw)
        if poisoned.public_accepted:
            execution = replace(
                accepted.execution_evidence,
                verification_result_sha256=hashlib.sha256(result_raw).hexdigest(),
            )
            execution_raw = collection_evidence_bytes(execution)
            repair_raw = None
        else:
            execution_raw = None
            repair = replace(
                failed.repair_evidence,
                verification_result_sha256=hashlib.sha256(result_raw).hexdigest(),
            )
            repair_raw = repair_evidence_bytes(repair)
        with pytest.raises(ValueError):
            reopen_verification_evidence(
                result_raw=result_raw,
                **_reopen_authorities(accepted if poisoned.public_accepted else failed),
                execution_raw=execution_raw,
                repair_raw=repair_raw,
            )


@pytest.mark.parametrize("accepted", (True, False))
def test_result_reopen_rejects_fully_rebound_artifact_without_trusted_artifact_row(
    accepted: bool,
) -> None:
    run = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=(
            FakeSandbox()
            if accepted
            else FakeSandbox({"pyright": SandboxResultV1("pyright", 1, b"type failure", b"")})
        ),
    )
    assert run.artifact is not None
    forged_artifact = CodeArtifactV1(ATTEMPT, "other.py", "def other() -> int:\n    return 2")
    forged_artifact_raw = code_artifact_bytes(forged_artifact)
    forged_artifact_sha = hashlib.sha256(forged_artifact_raw).hexdigest()
    forged_result = replace(
        run.result,
        code_artifact_sha256=forged_artifact_sha,
        tiers=tuple(replace(row, input_sha256=forged_artifact_sha) for row in run.result.tiers),
    )
    forged_result_raw = verification_result_bytes(forged_result)
    forged_result_sha = hashlib.sha256(forged_result_raw).hexdigest()
    if accepted:
        assert run.execution_evidence is not None
        execution_raw = collection_evidence_bytes(
            replace(run.execution_evidence, verification_result_sha256=forged_result_sha)
        )
        repair_raw = None
    else:
        assert run.repair_evidence is not None
        execution_raw = None
        repair_raw = repair_evidence_bytes(
            replace(run.repair_evidence, verification_result_sha256=forged_result_sha)
        )
    authorities = _reopen_authorities(run)
    authorities["expected_result_sha256"] = forged_result_sha
    authorities["expected_execution_sha256"] = (
        hashlib.sha256(execution_raw).hexdigest() if execution_raw is not None else None
    )
    authorities["expected_repair_sha256"] = (
        hashlib.sha256(repair_raw).hexdigest() if repair_raw is not None else None
    )
    if accepted:
        forged_blob = forged_artifact.source.encode()
        forged_blob_sha = hashlib.sha256(forged_blob).hexdigest()
        forged_record = ArtifactRecordV1(
            attempt_id=forged_artifact.attempt_id,
            artifact_path=forged_artifact.artifact_path,
            source_sha256=forged_blob_sha,
            source_bytes=len(forged_blob),
            blob_path=".rrcv2/artifacts/accepted-code.v1.utf8",
            blob_sha256=forged_blob_sha,
            blob_bytes=len(forged_blob),
        )
        authorities["artifact_raw"] = forged_artifact_raw
        authorities["artifact_record_raw"] = artifact_record_bytes(forged_record)
        authorities["artifact_blob_raw"] = forged_blob
        # The independently selected outcome hash intentionally remains the original record hash.
    with pytest.raises(ValueError, match="CodeArtifact|ArtifactRecord|tier execution"):
        reopen_verification_evidence(
            result_raw=forged_result_raw,
            **authorities,
            execution_raw=execution_raw,
            repair_raw=repair_raw,
        )


def _fixture_attested(result: SandboxResultV1) -> SandboxResultV1:
    argv = ("fixture-sandbox", result.tier)
    observation = CommandObservation(result.exit_code, result.stdout, result.stderr, argv)
    identity = "1" * 64
    evidence = SandboxTierExecutionV1(
        tier=result.tier,
        backend_before_sha256=identity,
        backend_after_sha256=identity,
        capability_sha256=sandbox_module.CAPABILITY_SHA256,
        runtime_lock_sha256=sandbox_module.RUNTIME_LOCK_SHA256,
        image_config_digest=sandbox_module.IMAGE_CONFIG_DIGEST,
        invocations=(
            SandboxInvocationEvidenceV1(
                argv_sha256=sandbox_module._sha(sandbox_module._canonical(list(argv))),
                exit_code=result.exit_code,
                stdout_sha256=sandbox_module._sha(result.stdout),
                stderr_sha256=sandbox_module._sha(result.stderr),
            ),
        ),
        normalized_source_sha256=(
            sandbox_module._sha(result.normalized_source)
            if result.normalized_source is not None
            else None
        ),
    )
    return replace(result, invocations=(observation,), execution_evidence=evidence)


class FakeSandbox:
    def __init__(self, overrides: dict[str, SandboxResultV1] | None = None) -> None:
        self.overrides = overrides or {}
        self.calls: list[str] = []
        self.sources_seen: list[bytes] = []
        self.test_files_seen: list[tuple[str, ...]] = []

    def run(  # type: ignore[no-untyped-def]
        self,
        *,
        tier,
        verification_profile,
        artifact_path,
        source,
        tests,
        selected_node_ids=(),
        limits,
    ):
        self.calls.append(tier)
        self.sources_seen.append(source)
        self.test_files_seen.append(tuple(test.filename for test in tests))
        if tier in self.overrides:
            return _fixture_attested(self.overrides[tier])
        if tier == "ruff":
            normalized = source
            if source.startswith(b"from __future__ import annotations\n"):
                text = source.decode()
                marker = "\ndef "
                body_at = text.find(marker)
                if body_at >= 0:
                    raw_prefix = text[: body_at + 1]
                    normalized = (
                        _normalized_type_prelude(raw_prefix) + text[body_at + 1 :]
                    ).encode()
            return _fixture_attested(
                SandboxResultV1("ruff", 0, b"", b"", normalized_source=normalized)
            )
        if tier == "pyright":
            return _fixture_attested(SandboxResultV1("pyright", 0, b"", b""))
        if tier == "pytest_collect":
            nodes = tuple(
                f"{test.filename}::{node.name}"
                for test in tests
                for node in ast.parse(test.source).body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            )
            return _fixture_attested(
                SandboxResultV1("pytest_collect", 0, b"", b"", collected_node_ids=nodes)
            )
        completed = selected_node_ids or tuple(
            f"{test.filename}::{node.name}"
            for test in tests
            for node in ast.parse(test.source).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
        return _fixture_attested(
            SandboxResultV1(
                "pytest",
                0,
                b"",
                b"",
                collected_node_ids=completed,
                completed_node_ids=completed,
            )
        )


def test_synthetic_type_prelude_has_exact_frozen_empty_and_domain_goldens() -> None:
    body = "def get_order(x: int) -> int:\n    return 1"
    empty = _synthetic_type_prelude(
        "def get_order(x: int) -> int: ...\n",
        target="get_order",
        body_source=body,
    ).encode()
    domain = _synthetic_type_prelude(
        "def get_order(x: pkg.User) -> pkg.User: ...\n",
        target="get_order",
        body_source=body,
    ).encode()
    assert (len(empty), hashlib.sha256(empty).hexdigest()) == (
        143,
        "f7cacbca7a7b6475c64b097140d98399ac5259361e0a3f976d61ff43aa3163c3",
    )
    assert (len(domain), hashlib.sha256(domain).hexdigest()) == (
        186,
        "8d185af6ed05fa9a9356511438285b9d86737fef3d2ccf90cb58bbd98f9036e6",
    )


def test_normalized_type_prelude_matches_pinned_ruff_nested_sibling_spacing() -> None:
    prelude = _synthetic_type_prelude(
        "def f(x: aaa.User, y: aaa.Team, z: zzz.Item) -> aaa.User: ...\n",
        target="f",
        body_source=("def f(x: aaa.User, y: aaa.Team, z: zzz.Item) -> aaa.User:\n    return x\n"),
    )
    assert "class Team:\n            pass\n\n        class User:" in _normalized_type_prelude(
        prelude
    )
    assert "class aaa:" in _normalized_type_prelude(prelude)
    assert "\n\n    class zzz:" in _normalized_type_prelude(prelude)


def test_synthetic_domain_prelude_is_controller_supplied_to_every_sandbox_tier() -> None:
    sandbox = FakeSandbox()
    signature = (
        "def normalize(value: dict[str, list[pkg.User | None]]) "
        "-> dict[str, list[pkg.User | None]]: ...\n"
    )
    body = (
        "def normalize(value: dict[str, list[pkg.User | None]]) "
        "-> dict[str, list[pkg.User | None]]:\n"
        "    return value\n"
    )
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source=body,
        signature_source=signature,
        synthetic_target="normalize",
        tests=("def test_normalize():\n    assert normalize({}) == {}\n",),
        sandbox=sandbox,
    )
    prelude = _synthetic_type_prelude(
        signature,
        target="normalize",
        body_source=body,
    ).encode()
    assert result.public_accepted is True
    normalized_prelude = _normalized_type_prelude(prelude.decode()).encode()
    assert result.normalized_source == (normalized_prelude.decode() + body).removesuffix("\n")
    assert sandbox.calls == ["ruff", "pyright", "pytest_collect", "pytest"]
    assert sandbox.sources_seen[0].startswith(prelude)
    assert all(source.startswith(normalized_prelude) for source in sandbox.sources_seen[1:])
    assert result.normalized_source.count("import ") == 4


@pytest.mark.parametrize(
    ("signature", "body"),
    (
        ("def f(x: math.User) -> int: ...\n", "def f(x: math.User) -> int:\n    return 1\n"),
        ("def f(x: re.Match) -> int: ...\n", "def f(x: re.Match) -> int:\n    return 1\n"),
        ("def f(x: dict.User) -> int: ...\n", "def f(x: dict.User) -> int:\n    return 1\n"),
        (
            "def f(x: TYPE_CHECKING.User) -> int: ...\n",
            "def f(x: TYPE_CHECKING.User) -> int:\n    return 1\n",
        ),
        ("def f(x: f.User) -> int: ...\n", "def f(x: f.User) -> int:\n    return 1\n"),
        ("def f(x: pkg.Box[int]) -> int: ...\n", "def f(x: pkg.Box[int]) -> int:\n    return 1\n"),
        ("def f(x: dict[str]) -> int: ...\n", "def f(x: dict[str]) -> int:\n    return 1\n"),
        (
            "def f(x: list[str, int]) -> int: ...\n",
            "def f(x: list[str, int]) -> int:\n    return 1\n",
        ),
        ("def f(x: pkg.make()) -> int: ...\n", "def f(x: pkg.make()) -> int:\n    return 1\n"),
        ("def f(x: pkg._User) -> int: ...\n", "def f(x: pkg._User) -> int:\n    return 1\n"),
        ("def f(x: 'pkg.User') -> int: ...\n", "def f(x: 'pkg.User') -> int:\n    return 1\n"),
        (
            "def f(x: pkg.User) -> int: ...\n",
            "pkg = 1\ndef f(x: pkg.User) -> int:\n    return 1\n",
        ),
        (
            "def f(x: pkg.User) -> pkg.User: ...\n",
            "def f(x: pkg.User) -> pkg.User:\n    return pkg.User\n",
        ),
    ),
)
def test_synthetic_type_prelude_rejects_reserved_or_unsafe_shapes_before_sandbox(
    signature: str,
    body: str,
) -> None:
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source=body,
        signature_source=signature,
        synthetic_target="f",
        tests=("def test_f():\n    assert f(None) == 1\n",),
        sandbox=sandbox,
    )
    assert result.public_accepted is False
    assert [row.name for row in result.tiers] == ["assembly"]
    assert sandbox.calls == []


def test_synthetic_verifier_rejects_a_ruff_mutation_of_the_trusted_prelude() -> None:
    class MutatingRuffSandbox(FakeSandbox):
        def run(self, **kwargs):  # type: ignore[no-untyped-def]
            result = super().run(**kwargs)
            if kwargs["tier"] == "ruff":
                return _fixture_attested(
                    replace(
                        result,
                        normalized_source=kwargs["source"].replace(
                            b"import json as json", b"import json", 1
                        ),
                    )
                )
            return result

    sandbox = MutatingRuffSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source="def f(x: pkg.User) -> int:\n    return 1\n",
        signature_source="def f(x: pkg.User) -> int: ...\n",
        synthetic_target="f",
        tests=("def test_f():\n    assert f(None) == 1\n",),
        sandbox=sandbox,
    )
    assert result.public_accepted is False
    assert [row.name for row in result.tiers] == ["assembly", "ruff"]
    assert sandbox.calls == ["ruff"]


def test_synthetic_verifier_rejects_ast_equivalent_prefix_byte_mutation() -> None:
    class MutatingRuffSandbox(FakeSandbox):
        def run(self, **kwargs):  # type: ignore[no-untyped-def]
            result = super().run(**kwargs)
            if kwargs["tier"] == "ruff":
                return _fixture_attested(
                    replace(
                        result,
                        normalized_source=kwargs["source"].replace(
                            b"import json as json\n", b"import json as json  # changed\n", 1
                        ),
                    )
                )
            return result

    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source="def f(x: pkg.User) -> int:\n    return 1\n",
        signature_source="def f(x: pkg.User) -> int: ...\n",
        synthetic_target="f",
        tests=("def test_f():\n    assert f(None) == 1\n",),
        sandbox=MutatingRuffSandbox(),
    )
    assert result.public_accepted is False
    assert [row.name for row in result.tiers] == ["assembly", "ruff"]


def test_verifier_emits_the_frozen_direct_and_spec_authorities() -> None:
    source = "def get_order(x: int) -> int:\n    return 1"
    direct = verify_candidate(
        attempt_id="1" * 64,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=source,
        tests=("def test_public():\n    assert get_order(1) == 1\n",),
        sandbox=FakeSandbox(),
    )
    spec = verify_candidate(
        attempt_id="1" * 64,
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source=source,
        signature_source="def get_order(x: int) -> int: ...\n",
        synthetic_target="get_order",
        tests=("def test_public():\n    assert get_order(1) == 1\n",),
        sandbox=FakeSandbox(),
    )
    assert (
        len(verification_result_bytes(direct.result)),
        hashlib.sha256(verification_result_bytes(direct.result)).hexdigest(),
    ) == (
        1433,
        "afbc5b65311e44c75e2587881917467bc1e290d1000ab618aadbd101cff3ab4d",
    )
    assert (
        len(verification_result_bytes(spec.result)),
        hashlib.sha256(verification_result_bytes(spec.result)).hexdigest(),
    ) == (
        1745,
        "8ff3406942b1e2ff209cda3596bb2181373e048b33c61ed51c6cc7c110dbab97",
    )


def test_structured_verifier_runs_ordered_general_profile_tiers() -> None:
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=sandbox,
    )
    assert result.public_accepted is True
    assert [tier.name for tier in result.tiers] == ["assembly", "ruff", "pyright", "pytest"]
    assert sandbox.calls == ["ruff", "pyright", "pytest_collect", "pytest"]
    assert result.normalized_source == SOURCE.removesuffix("\n")
    assert len(result.code_artifact_sha256) == 64


def test_categorized_test_authority_preserves_fixed_file_identity() -> None:
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        test_suite=VerificationTestsV1(
            spec=("def test_spec():\n    assert add(1, 1) == 2\n",),
            independent=("def test_independent():\n    assert add(2, 2) == 4\n",),
            public=("def test_public():\n    assert add(3, 3) == 6\n",),
        ),
        sandbox=sandbox,
    )
    assert result.public_accepted is True
    assert sandbox.test_files_seen[-2:] == [
        ("test_spec_00.py", "test_independent_00.py", "test_public_00.py"),
        ("test_spec_00.py", "test_independent_00.py", "test_public_00.py"),
    ]
    assert result.collection_evidence is not None
    assert result.collection_evidence.test_sources == tuple(
        (filename, hashlib.sha256(source.encode()).hexdigest())
        for filename, source in (
            ("test_spec_00.py", "def test_spec():\n    assert add(1, 1) == 2\n"),
            (
                "test_independent_00.py",
                "def test_independent():\n    assert add(2, 2) == 4\n",
            ),
            ("test_public_00.py", "def test_public():\n    assert add(3, 3) == 6\n"),
        )
    )
    assert (
        result.collection_evidence.verification_result_sha256
        == hashlib.sha256(verification_result_bytes(result.result)).hexdigest()
    )
    assert result.collection_evidence.collected_node_ids == (
        "test_spec_00.py::test_spec",
        "test_independent_00.py::test_independent",
        "test_public_00.py::test_public",
    )
    assert (
        result.collection_evidence.completed_node_ids
        == result.collection_evidence.collected_node_ids
    )
    assert collection_evidence_bytes(result.collection_evidence) in result.evidence_blobs


def test_failed_verification_emits_exact_bounded_typed_repair_evidence() -> None:
    raw = ("bad\r\ne\N{COMBINING ACUTE ACCENT}" + "x" * 40_000).encode()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=FakeSandbox({"pyright": SandboxResultV1("pyright", 1, raw, b"")}),
    )
    evidence = result.repair_evidence
    assert result.public_accepted is False
    assert evidence is not None
    assert evidence.failed_tier == "pyright"
    assert (
        evidence.verification_result_sha256
        == hashlib.sha256(verification_result_bytes(result.result)).hexdigest()
    )
    assert "\r" not in evidence.excerpt
    assert "é" in evidence.excerpt
    assert len(evidence.excerpt.encode()) <= 32 * 1024
    assert evidence.excerpt_sha256 == hashlib.sha256(evidence.excerpt.encode()).hexdigest()
    assert repair_evidence_bytes(evidence) in result.evidence_blobs


def test_successful_verification_has_no_repair_evidence() -> None:
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=FakeSandbox(),
    )
    assert result.repair_evidence is None


def test_oracle_scoring_is_separate_from_public_acceptance_result() -> None:
    public = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=FakeSandbox(),
    )
    assert public.public_accepted is True and public.artifact is not None
    oracle = score_oracle(
        artifact=public.artifact,
        verification_profile="rrcv2_general_v1",
        oracle_tests=("def test_oracle():\n    assert add(9, 9) == 18\n",),
        sandbox=FakeSandbox(
            {"pytest": SandboxResultV1("pytest", 1, b"FAILED oracle\n", b"", completed_node_ids=())}
        ),
    )
    assert oracle.passed is False
    assert public.public_accepted is True
    assert [row.name for row in public.tiers] == ["assembly", "ruff", "pyright", "pytest"]


def test_oracle_rejects_unknown_profile_before_sandbox() -> None:
    sandbox = FakeSandbox()
    with pytest.raises(ValueError, match="unsupported verification profile"):
        score_oracle(
            artifact=CodeArtifactV1(ATTEMPT, "solution.py", SOURCE.removesuffix("\n")),
            verification_profile="bogus",  # type: ignore[arg-type]
            oracle_tests=(TEST,),
            sandbox=sandbox,
        )
    assert sandbox.calls == []


@pytest.mark.parametrize(("delta", "accepted"), ((-1, True), (0, True), (1, False)))
def test_oracle_source_plus_tests_has_exact_one_mib_boundary(delta: int, accepted: bool) -> None:
    test = "def test_f():\n    assert f() == 1\n#" + "t" * 1024
    prefix = "def f() -> int:\n    return 1\n#"
    padding = MAX_ARTIFACT_BYTES + delta - len(test.encode()) - len(prefix.encode())
    artifact = CodeArtifactV1(ATTEMPT, "solution.py", prefix + "x" * padding)
    sandbox = FakeSandbox()
    if accepted:
        assert score_oracle(
            artifact=artifact,
            verification_profile="rrcv2_general_v1",
            oracle_tests=(test,),
            sandbox=sandbox,
        ).passed
        assert sandbox.calls == ["pytest_collect", "pytest"]
    else:
        with pytest.raises(ValueError, match="aggregate caps"):
            score_oracle(
                artifact=artifact,
                verification_profile="rrcv2_general_v1",
                oracle_tests=(test,),
                sandbox=sandbox,
            )
        assert sandbox.calls == []


def test_oracle_has_exact_sixty_four_test_count_boundary() -> None:
    artifact = CodeArtifactV1(ATTEMPT, "solution.py", "def f() -> int:\n    return 1")
    tests = tuple(f"def test_{index}():\n    assert f() == 1\n" for index in range(65))
    assert score_oracle(
        artifact=artifact,
        verification_profile="rrcv2_general_v1",
        oracle_tests=tests[:64],
        sandbox=FakeSandbox(),
    ).passed
    sandbox = FakeSandbox()
    with pytest.raises(ValueError, match="bounded unique"):
        score_oracle(
            artifact=artifact,
            verification_profile="rrcv2_general_v1",
            oracle_tests=tests,
            sandbox=sandbox,
        )
    assert sandbox.calls == []


def _python_with_exact_ast_nodes(name: str, target: int) -> str:
    prefix = f"def test_{name}():\n    assert True\n"
    base = sum(1 for _ in ast.walk(ast.parse(prefix)))
    assignments, remainder = divmod(target - base, 4)
    source = prefix + "a=0\n" * assignments + "pass\n" * remainder
    assert sum(1 for _ in ast.walk(ast.parse(source))) == target
    assert len(source.encode()) <= 16 * 1024
    return source


@pytest.mark.parametrize(("delta", "accepted"), ((-1, True), (0, True), (1, False)))
def test_oracle_source_plus_tests_has_exact_ast_node_boundary(delta: int, accepted: bool) -> None:
    artifact = CodeArtifactV1(ATTEMPT, "solution.py", "def f() -> int:\n    return 1")
    source_nodes = sum(1 for _ in ast.walk(ast.parse(artifact.source)))
    test_total = 100_000 + delta - source_nodes
    quotient, remainder = divmod(test_total, 7)
    tests = tuple(
        _python_with_exact_ast_nodes(str(index), quotient + (index < remainder))
        for index in range(7)
    )
    sandbox = FakeSandbox()
    if accepted:
        assert score_oracle(
            artifact=artifact,
            verification_profile="rrcv2_general_v1",
            oracle_tests=tests,
            sandbox=sandbox,
        ).passed
    else:
        with pytest.raises(ValueError, match="aggregate caps"):
            score_oracle(
                artifact=artifact,
                verification_profile="rrcv2_general_v1",
                oracle_tests=tests,
                sandbox=sandbox,
            )
        assert sandbox.calls == []


def test_synthetic_oracle_reopens_the_single_prelude_bearing_artifact_authority() -> None:
    signature = "def normalize(value: pkg.User | None) -> pkg.User | None: ...\n"
    public = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source=("def normalize(value: pkg.User | None) -> pkg.User | None:\n    return value\n"),
        signature_source=signature,
        synthetic_target="normalize",
        tests=("def test_public():\n    assert normalize(None) is None\n",),
        sandbox=FakeSandbox(),
    )
    assert public.artifact is not None and "class pkg:" in public.artifact.source
    oracle = score_oracle(
        artifact=public.artifact,
        verification_profile="rrcv2_synthetic_v1",
        oracle_tests=("def test_oracle():\n    assert normalize(None) is None\n",),
        sandbox=FakeSandbox(),
        synthetic_target="normalize",
        signature_source=signature,
    )
    assert oracle.passed is True


def test_spec_driven_signature_failure_stops_before_typecheck() -> None:
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=sandbox,
        signature_source="def add(x: str, y: str) -> str: ...",
    )
    assert result.public_accepted is False
    assert [tier.name for tier in result.tiers] == [
        "assembly",
        "ruff",
        "signature_conformance",
    ]
    assert sandbox.calls == ["ruff"]


def test_failed_ruff_stops_before_later_tiers() -> None:
    sandbox = FakeSandbox({"ruff": SandboxResultV1("ruff", 1, b"", b"lint failure")})
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=sandbox,
    )
    assert result.public_accepted is False
    assert [tier.name for tier in result.tiers] == ["assembly", "ruff"]
    assert sandbox.calls == ["ruff"]


@pytest.mark.parametrize(
    "ruff_result",
    (
        SandboxResultV1("ruff", 1, b"lint failure", b""),
        SandboxResultV1(
            "ruff",
            0,
            b"",
            b"",
            normalized_source=b"def add(x: int, y: int) -> int:\r\n    return x + y\r\n",
        ),
    ),
)
def test_ruff_rejections_return_canonical_reopenable_artifact(
    ruff_result: SandboxResultV1,
) -> None:
    run = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=FakeSandbox({"ruff": ruff_result}),
    )
    assert run.public_accepted is False
    assert run.artifact is not None
    assert run.repair_evidence is not None
    artifact_raw = code_artifact_bytes(run.artifact)
    assert not run.artifact.source.endswith("\n")
    assert run.result.code_artifact_sha256 == hashlib.sha256(artifact_raw).hexdigest()
    assert (
        reopen_verification_evidence(
            result_raw=verification_result_bytes(run.result),
            **_reopen_authorities(run),
            execution_raw=None,
            repair_raw=repair_evidence_bytes(run.repair_evidence),
        )[0]
        == run.result
    )


@pytest.mark.parametrize(
    "collect",
    (
        SandboxResultV1("pytest_collect", 0, b"", b"", collected_node_ids=()),
        SandboxResultV1(
            "pytest_collect",
            0,
            b"",
            b"",
            collected_node_ids=("x", "x"),
        ),
    ),
)
def test_zero_or_duplicate_collection_never_accepts(collect: SandboxResultV1) -> None:
    sandbox = FakeSandbox({"pytest_collect": collect})
    if len(set(collect.collected_node_ids)) != len(collect.collected_node_ids):
        with pytest.raises(ValueError, match="duplicate"):
            verify_candidate(
                attempt_id=ATTEMPT,
                verification_profile="rrcv2_general_v1",
                artifact_path="solution.py",
                source=SOURCE,
                tests=(TEST,),
                sandbox=sandbox,
            )
    else:
        result = verify_candidate(
            attempt_id=ATTEMPT,
            verification_profile="rrcv2_general_v1",
            artifact_path="solution.py",
            source=SOURCE,
            tests=(TEST,),
            sandbox=sandbox,
        )
        assert result.public_accepted is False
        assert result.tiers[-1].name == "pytest"


def test_missing_or_wrong_completion_inventory_never_accepts() -> None:
    sandbox = FakeSandbox(
        {"pytest": SandboxResultV1("pytest", 0, b"", b"", completed_node_ids=("wrong",))}
    )
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=sandbox,
    )
    assert result.public_accepted is False


def test_execution_collection_must_equal_the_sealed_collection_inventory() -> None:
    sandbox = FakeSandbox(
        {
            "pytest": SandboxResultV1(
                "pytest",
                0,
                b"",
                b"",
                collected_node_ids=("wrong",),
                completed_node_ids=("test_public_00.py::test_add",),
            )
        }
    )
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=sandbox,
    )
    assert result.public_accepted is False


def test_synthetic_profile_executes_each_collected_node_in_a_fresh_call() -> None:
    sandbox = FakeSandbox(
        {
            "pytest_collect": SandboxResultV1(
                "pytest_collect",
                0,
                b"",
                b"",
                collected_node_ids=(
                    "test_public_00.py::test_a",
                    "test_public_01.py::test_b",
                ),
            )
        }
    )
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source=SOURCE,
        signature_source="def add(x: int, y: int) -> int: ...\n",
        synthetic_target="add",
        tests=(
            "def test_a():\n    assert add(1, 2) == 3\n",
            "def test_b():\n    assert add(2, 3) == 5\n",
        ),
        sandbox=sandbox,
    )
    assert result.public_accepted is True
    assert sandbox.calls[-3:] == ["pytest_collect", "pytest", "pytest"]


def test_synthetic_aggregate_output_is_bounded_before_retention() -> None:
    class AggregateSandbox(FakeSandbox):
        def run(self, **kwargs):  # type: ignore[no-untyped-def]
            result = super().run(**kwargs)
            if kwargs["tier"] == "pytest":
                node = kwargs["selected_node_ids"][0]
                return _fixture_attested(
                    replace(result, stdout=b"x" * 700_000, collected_node_ids=(node,))
                )
            return result

    with pytest.raises(ValueError, match="aggregate synthetic pytest evidence"):
        verify_candidate(
            attempt_id=ATTEMPT,
            verification_profile="rrcv2_synthetic_v1",
            artifact_path="solution.py",
            source=SOURCE,
            signature_source="def add(x: int, y: int) -> int: ...\n",
            synthetic_target="add",
            tests=(
                "def test_a():\n    assert add(1, 2) == 3\n",
                "def test_b():\n    assert add(2, 3) == 5\n",
            ),
            sandbox=AggregateSandbox(),
        )


def test_synthetic_flow_without_signature_rejects_before_sandbox() -> None:
    sandbox = FakeSandbox()
    with pytest.raises(ValueError, match="profile/flow/signature"):
        verify_candidate(
            attempt_id=ATTEMPT,
            verification_profile="rrcv2_synthetic_v1",
            artifact_path="solution.py",
            source=SOURCE,
            tests=(TEST,),
            synthetic_target="add",
            sandbox=sandbox,
        )
    assert sandbox.calls == []


def test_unattested_or_oversize_sandbox_evidence_fails_closed() -> None:
    base = SandboxResultV1("ruff", 0, b"", b"", normalized_source=SOURCE.encode())
    for poisoned in (
        replace(base, backend_attested=False),
        replace(base, stdout=b"x" * ((1024 * 1024) + 1)),
    ):
        sandbox = FakeSandbox({"ruff": poisoned})
        with pytest.raises(ValueError):
            verify_candidate(
                attempt_id=ATTEMPT,
                verification_profile="rrcv2_general_v1",
                artifact_path="solution.py",
                source=SOURCE,
                tests=(TEST,),
                sandbox=sandbox,
                limits=SandboxLimitsV1(),
            )


def test_ruff_tier_shares_one_cpu_and_wall_budget_across_all_commands() -> None:
    class RecordingSupervisor:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple[str, ...], float]] = []

        def run(  # type: ignore[no-untyped-def]
            self,
            argv,
            *,
            container_name,
            timeout_seconds,
            stdout_cap,
            stderr_cap,
        ):
            del container_name, stdout_cap, stderr_cap
            self.calls.append((argv, timeout_seconds))
            return CommandObservation(0, b"", b"")

        def attest_image(self, authority, seccomp_path):  # type: ignore[no-untyped-def]
            raise AssertionError("attestation is outside this tier-budget unit test")

    class Workspace:
        input_volume = "input"
        output_volume = "output"
        sentinel_volume = "sentinel"

        @staticmethod
        def extract(path: str) -> bytes:
            assert path == sandbox_module._physical_artifact_path("solution.py")
            return SOURCE.encode()

    supervisor = RecordingSupervisor()
    sandbox = SealedDockerSandbox(Path(__file__).resolve().parents[1], supervisor=supervisor)
    result = sandbox._run_workspace(
        tier="ruff",
        artifact_path="solution.py",
        tests=(),
        selected_node_ids=(),
        limits=SandboxLimitsV1(),
        image="sha256:" + "1" * 64,
        seccomp=Path("seccomp.json"),
        workspace=Workspace(),  # type: ignore[arg-type]
    )
    assert result.exit_code == 0
    assert len(supervisor.calls) == 4
    cpu_limits = [
        next(arg for arg in argv if arg.startswith("--ulimit=cpu=")) for argv, _ in supervisor.calls
    ]
    assert cpu_limits == [
        "--ulimit=cpu=3:3",
        "--ulimit=cpu=3:3",
        "--ulimit=cpu=2:2",
        "--ulimit=cpu=2:2",
    ]
    assert all(0 < timeout <= 20 for _, timeout in supervisor.calls)


@pytest.mark.parametrize("artifact_path", ("solution.py", "tools/runner"))
def test_general_materialization_imports_one_shared_candidate_loader(
    tmp_path: Path, artifact_path: str
) -> None:
    (tmp_path / "attempt").mkdir()
    input_dir, _ = SealedDockerSandbox._materialize(
        tmp_path / "attempt",
        verification_profile="rrcv2_general_v1",
        artifact_path=artifact_path,
        source=(
            b"import sys\n\n"
            b"MODULE_FILE = __file__\n"
            b"SPEC_ORIGIN = __spec__.origin\n"
            b"LOADER_PATH = __loader__.path\n"
            b"SOURCE_BYTES = open(__file__, 'rb').read()\n\n"
            b"counter = 0\n\n"
            b"def observation():\n"
            b"    global counter\n"
            b"    counter += 1\n"
            b"    return sys.modules.get(__name__) is not None, counter\n\n"
            b"def runtime_metadata():\n"
            b"    return __file__, __spec__.origin, __loader__.path\n"
        ),
        tests=(
            SandboxTestFileV1("public", 0, b"def test_first():\n    assert True\n"),
            SandboxTestFileV1("public", 1, b"def test_second():\n    assert True\n"),
        ),
    )

    assert (input_dir / "controller/__init__.py").read_bytes() == b""
    assert (input_dir / "test_public_00.py").read_bytes() == (
        b"def test_first():\n    assert True\n"
    )
    assert (input_dir / "test_public_01.py").read_bytes() == (
        b"def test_second():\n    assert True\n"
    )
    assert not tuple((input_dir / "controller").glob(".rrcv2-model-public-*.src"))
    runner = runpy.run_path(str(input_dir / "controller/runner.py"))
    load = cast(Callable[[str], dict[str, object]], runner["load"])
    candidate_path = str(input_dir / sandbox_module._physical_artifact_path(artifact_path))
    assert "rrcv2_candidate" not in sys.modules
    try:
        first = load(candidate_path)
        second = load(candidate_path)
        first_observation = cast(Callable[[], tuple[bool, int]], first["observation"])
        second_observation = cast(Callable[[], tuple[bool, int]], second["observation"])
        assert first_observation() == (True, 1)
        assert second_observation() == (True, 2)
        source_bytes = (
            input_dir / sandbox_module._physical_artifact_path(artifact_path)
        ).read_bytes()
        assert first["MODULE_FILE"] == candidate_path
        assert first["SPEC_ORIGIN"] == candidate_path
        assert first["LOADER_PATH"] == candidate_path
        assert first["SOURCE_BYTES"] == source_bytes
        runtime_metadata = cast(Callable[[], tuple[str, str, str]], first["runtime_metadata"])
        assert runtime_metadata() == (candidate_path, candidate_path, candidate_path)
    finally:
        sys.modules.pop("rrcv2_candidate", None)


@pytest.mark.parametrize(
    "artifact_path",
    (
        "a" * 255,
        "a" * 256,
        "é" * 128,
        "/".join((*("deep" for _ in range(512)), "solution.py")),
    ),
)
def test_logical_artifact_paths_use_one_bounded_physical_mapping(
    tmp_path: Path, artifact_path: str
) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    input_dir, output_dir = SealedDockerSandbox._materialize(
        root,
        verification_profile="rrcv2_general_v1",
        artifact_path=artifact_path,
        source=b"def value():\n    return 1\n",
        tests=(SandboxTestFileV1("public", 0, b"def test_value():\n    assert True\n"),),
    )
    physical = sandbox_module._physical_artifact_path(artifact_path)
    assert physical.startswith("task/artifact-") and physical.endswith(".py")
    assert len(Path(physical).name.encode("utf-8")) <= 255
    assert (input_dir / physical).read_bytes() == b"def value():\n    return 1\n"
    assert (output_dir / physical).read_bytes() == b"def value():\n    return 1\n"


def test_synthetic_pytest_plugin_does_not_import_the_general_loader(
    tmp_path: Path,
) -> None:
    (tmp_path / "attempt").mkdir()
    input_dir, _ = SealedDockerSandbox._materialize(
        tmp_path / "attempt",
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source=b"def value() -> int:\n    return 1\n",
        tests=(SandboxTestFileV1("public", 0, b"def test_value():\n    assert value() == 1\n"),),
    )
    sys.path.insert(0, str(input_dir))
    original_argv = sys.argv
    sys.argv = ["pytest", "/input/test_public_00.py::test_value"]
    try:
        plugin = runpy.run_path(str(input_dir / "conftest.py"))
        assert plugin["PROFILE"] == "rrcv2_synthetic_v1"
        assert plugin["_rrcv2_load_test_module"] is None
        assert plugin["_rrcv2_finder"] is None
        assert plugin["_rrcv2_selected_execution"] is True
        assert plugin["_rrcv2_expected_collectors"] == set()
    finally:
        sys.argv = original_argv
        if "plugin" in locals():
            cast(Callable[[object], None], plugin["pytest_unconfigure"])(None)
        if sys.path and sys.path[0] == str(input_dir):
            sys.path.pop(0)
        for name in tuple(sys.modules):
            if name == "controller" or name.startswith("controller."):
                sys.modules.pop(name, None)


def test_general_loader_preserves_future_imports_and_hides_candidate_tests(
    tmp_path: Path,
) -> None:
    (tmp_path / "attempt").mkdir()
    test_source = (
        b"from __future__ import annotations\n\n"
        b"from pathlib import Path\n\n"
        b"assert Path(__file__).name == 'test_public_00.py'\n\n"
        b"def annotated(value: int) -> list[int]:\n"
        b"    return [value]\n\n"
        b"def test_value():\n"
        b"    assert annotated.__annotations__ == "
        b"{'value': 'int', 'return': 'list[int]'}\n"
        b"    assert observation() == (True, 1)\n"
    )
    input_dir, _ = SealedDockerSandbox._materialize(
        tmp_path / "attempt",
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=(
            b"import sys\n\n"
            b"counter = 0\n\n"
            b"def observation():\n"
            b"    global counter\n"
            b"    counter += 1\n"
            b"    return sys.modules.get(__name__) is not None, counter\n\n"
            b"def test_helper(required: int):\n"
            b"    return required\n"
        ),
        tests=(SandboxTestFileV1("public", 0, test_source),),
    )
    runner = runpy.run_path(str(input_dir / "controller/runner.py"))
    load_test_module = cast(
        Callable[[str, str, types.ModuleType], None], runner["load_test_module"]
    )
    test_path = str(input_dir / "test_public_00.py")
    test_loader = importlib.machinery.SourceFileLoader("test_public_00", test_path)
    test_spec = importlib.util.spec_from_file_location(
        "test_public_00", test_path, loader=test_loader
    )
    assert test_spec is not None
    test_module = importlib.util.module_from_spec(test_spec)
    module_globals = test_module.__dict__
    assert "rrcv2_candidate" not in sys.modules
    assert test_module.__name__ not in sys.modules
    sys.modules[test_module.__name__] = test_module
    try:
        load_test_module(
            str(input_dir / sandbox_module._physical_artifact_path("solution.py")),
            test_path,
            test_module,
        )
        assert module_globals["__file__"] == test_path
        assert "test_helper" not in module_globals
        assert "observation" not in module_globals
        sys.path.insert(0, str(input_dir))
        plugin = runpy.run_path(str(input_dir / "conftest.py"))
        finish = cast(Callable[[object], None], plugin["pytest_collection_finish"])

        class Session:
            items: tuple[object, ...] = ()

        finish(Session())
        assert module_globals["__file__"] == test_path
        assert getattr(module_globals["__spec__"], "origin") == test_path
        assert getattr(module_globals["__loader__"], "path") == test_path
        assert "test_helper" in module_globals
        assert "observation" in module_globals
        assert callable(module_globals["test_value"])
        cast(Callable[[], None], module_globals["test_value"])()
    finally:
        if "plugin" in locals():
            cast(Callable[[object], None], plugin["pytest_unconfigure"])(None)
        if sys.path and sys.path[0] == str(input_dir):
            sys.path.pop(0)
        for name in tuple(sys.modules):
            if name == "controller" or name.startswith("controller."):
                sys.modules.pop(name, None)
        sys.modules.pop("rrcv2_candidate", None)
        sys.modules.pop(test_module.__name__, None)


def test_pytest_collection_restores_candidate_exports_for_fixture_only_modules(
    tmp_path: Path,
) -> None:
    (tmp_path / "attempt").mkdir()
    input_dir, _ = SealedDockerSandbox._materialize(
        tmp_path / "attempt",
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=b"def value():\n    return 7\n",
        tests=(SandboxTestFileV1("public", 0, b"def test_value():\n    assert True\n"),),
    )
    sys.path.insert(0, str(input_dir))
    plugin = runpy.run_path(str(input_dir / "conftest.py"))
    finish = cast(Callable[[object], None], plugin["pytest_collection_finish"])
    fixture_module = types.ModuleType("rrcv2_fixture_only_test_module")
    fixture_module.__dict__["\x00rrcv2_candidate_exports_v1"] = {"value": 7}
    sys.modules[fixture_module.__name__] = fixture_module

    class Session:
        items: tuple[object, ...] = ()

    try:
        finish(Session())
        assert fixture_module.value == 7
    finally:
        cast(Callable[[object], None], plugin["pytest_unconfigure"])(None)
        if sys.path and sys.path[0] == str(input_dir):
            sys.path.pop(0)
        for name in tuple(sys.modules):
            if name == "controller" or name.startswith("controller."):
                sys.modules.pop(name, None)
        sys.modules.pop(fixture_module.__name__, None)


def test_pytest_completion_requires_clean_setup_call_and_terminal_teardown(
    tmp_path: Path,
) -> None:
    (tmp_path / "attempt").mkdir()
    input_dir, _ = SealedDockerSandbox._materialize(
        tmp_path / "attempt",
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=b"def value():\n    return 7\n",
        tests=(SandboxTestFileV1("public", 0, b"def test_value():\n    assert True\n"),),
    )
    sys.path.insert(0, str(input_dir))
    plugin = runpy.run_path(str(input_dir / "conftest.py"))
    report = cast(Callable[[object], None], plugin["pytest_runtest_logreport"])

    def row(
        nodeid: str,
        when: str,
        *,
        passed: bool = False,
        failed: bool = False,
        skipped: bool = False,
        wasxfail: str | None = None,
    ) -> object:
        return types.SimpleNamespace(
            nodeid=nodeid,
            when=when,
            passed=passed,
            failed=failed,
            skipped=skipped,
            wasxfail=wasxfail,
        )

    try:
        valid = "test_public_00.py::test_valid"
        for phase in ("setup", "call", "teardown"):
            report(row(valid, phase, passed=True))
        invalid_reports = (
            (
                row("teardown-fail", "setup", passed=True),
                row("teardown-fail", "call", passed=True),
                row("teardown-fail", "teardown", failed=True),
            ),
            (row("setup-skip", "setup", skipped=True),),
            (
                row("call-fail", "setup", passed=True),
                row("call-fail", "call", failed=True),
                row("call-fail", "teardown", passed=True),
            ),
            (
                row("call-skip", "setup", passed=True),
                row("call-skip", "call", skipped=True),
                row("call-skip", "teardown", passed=True),
            ),
            (
                row("xfail", "setup", passed=True),
                row("xfail", "call", skipped=True, wasxfail="reason"),
                row("xfail", "teardown", passed=True),
            ),
            (
                row("xpass", "setup", passed=True),
                row("xpass", "call", passed=True, wasxfail="reason"),
                row("xpass", "teardown", passed=True),
            ),
            (
                row("teardown-skip", "setup", passed=True),
                row("teardown-skip", "call", passed=True),
                row("teardown-skip", "teardown", skipped=True),
            ),
        )
        for rows in invalid_reports:
            for observed in rows:
                report(observed)
        assert plugin["_rrcv2_completed"] == [valid]
        expected_collectors = cast(set[str], plugin["_rrcv2_expected_collectors"])
        expected_collectors.add("duplicate.py")
        collect_report = cast(Callable[[object], None], plugin["pytest_collectreport"])
        clean_duplicate = types.SimpleNamespace(
            nodeid="duplicate.py",
            passed=True,
            failed=False,
            skipped=False,
            wasxfail=None,
        )
        collect_report(clean_duplicate)
        collect_report(clean_duplicate)
        assert (
            cast(types.FunctionType, collect_report).__globals__["_rrcv2_collection_invalid"]
            is True
        )
        expected_collectors.add("test_public_00.py")
        collect_report(
            types.SimpleNamespace(
                nodeid="test_public_00.py",
                passed=False,
                failed=False,
                skipped=True,
                wasxfail=None,
            )
        )
        session = types.SimpleNamespace(exitstatus=0)
        cast(Callable[[object, object], None], plugin["pytest_sessionfinish"])(session, 0)
        assert session.exitstatus == 1
    finally:
        cast(Callable[[object], None], plugin["pytest_unconfigure"])(None)
        if sys.path and sys.path[0] == str(input_dir):
            sys.path.pop(0)
        for name in tuple(sys.modules):
            if name == "controller" or name.startswith("controller."):
                sys.modules.pop(name, None)


def test_bad_artifact_fails_at_assembly_without_sandbox_call() -> None:
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="../escape.py",
        source=SOURCE,
        tests=(TEST,),
        sandbox=sandbox,
    )
    assert result.public_accepted is False
    assert [tier.name for tier in result.tiers] == ["assembly"]
    assert sandbox.calls == []


def test_crlf_candidate_rejects_before_sandbox_and_never_creates_artifact() -> None:
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source="def f() -> int:\r\n    return 1\r\n",
        tests=("def test_f():\n    assert f() == 1\n",),
        sandbox=sandbox,
    )
    assert result.public_accepted is False
    assert result.artifact is None
    assert result.tiers[-1].name == "assembly"
    assert sandbox.calls == []


def _source_for_code_artifact_size(size: int) -> str:
    prefix = "def f() -> int:\n    return 1\n#"

    def raw(source: str) -> bytes:
        return _canonical(
            {
                "artifact_path": "solution.py",
                "attempt_id": ATTEMPT,
                "source": source,
                "v": 1,
            }
        )

    padding = size - len(raw(prefix))
    assert padding >= 0
    source = prefix + "x" * padding
    assert len(raw(source)) == size
    return source


@pytest.mark.parametrize(("delta", "accepted"), ((-1, True), (0, True), (1, False)))
def test_canonical_code_artifact_has_exact_one_mib_boundary(delta: int, accepted: bool) -> None:
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=_source_for_code_artifact_size(MAX_ARTIFACT_BYTES + delta),
        tests=("def test_f():\n    assert f() == 1\n",),
        sandbox=sandbox,
    )
    assert result.public_accepted is accepted
    if accepted:
        assert result.artifact is not None
        assert len(code_artifact_bytes(result.artifact)) == MAX_ARTIFACT_BYTES + delta
    else:
        assert result.artifact is None
        assert sandbox.calls == []


@pytest.mark.parametrize(("delta", "accepted"), ((-1, True), (0, True), (1, False)))
def test_source_plus_tests_aggregate_has_exact_one_mib_boundary(delta: int, accepted: bool) -> None:
    test = "def test_f():\n    assert f() == 1\n#" + "t" * 1024
    prefix = "def f() -> int:\n    return 1\n#"
    padding = MAX_ARTIFACT_BYTES + delta - len(test.encode()) - len(prefix.encode())
    source = prefix + "x" * padding
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=source,
        tests=(test,),
        sandbox=sandbox,
    )
    assert len(source.encode()) + len(test.encode()) == MAX_ARTIFACT_BYTES + delta
    assert result.public_accepted is accepted
    assert bool(sandbox.calls) is accepted


@pytest.mark.parametrize(("delta", "accepted"), ((-1, True), (0, True), (1, False)))
def test_post_ruff_source_plus_tests_aggregate_reapplies_exact_boundary(
    delta: int, accepted: bool
) -> None:
    test = "def test_f():\n    assert f() == 1\n#" + "t" * 1024
    prefix = "def f() -> int:\n    return 1\n#"
    padding = MAX_ARTIFACT_BYTES + delta - len(test.encode()) - len(prefix.encode())
    normalized = prefix + "x" * padding
    sandbox = FakeSandbox(
        {
            "ruff": SandboxResultV1(
                "ruff",
                0,
                b"",
                b"",
                normalized_source=normalized.encode(),
            )
        }
    )
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source="def f() -> int:\n    return 1\n",
        tests=(test,),
        sandbox=sandbox,
    )
    assert result.public_accepted is accepted
    assert [row.name for row in result.tiers] == (
        ["assembly", "ruff", "pyright", "pytest"] if accepted else ["assembly", "ruff"]
    )


def test_ruff_growth_reapplies_artifact_and_aggregate_caps() -> None:
    test = "def test_f():\n    assert f() == 1\n#" + "t" * 1024
    oversized_artifact = _source_for_code_artifact_size(MAX_ARTIFACT_BYTES + 1)
    aggregate_prefix = "def f() -> int:\n    return 1\n#"
    aggregate_padding = MAX_ARTIFACT_BYTES - len(test.encode()) - len(aggregate_prefix.encode()) + 1
    oversized_aggregate = aggregate_prefix + "x" * aggregate_padding
    for normalized in (oversized_artifact, oversized_aggregate):
        sandbox = FakeSandbox(
            {
                "ruff": SandboxResultV1(
                    "ruff",
                    0,
                    b"",
                    b"",
                    normalized_source=normalized.encode(),
                )
            }
        )
        result = verify_candidate(
            attempt_id=ATTEMPT,
            verification_profile="rrcv2_general_v1",
            artifact_path="solution.py",
            source="def f() -> int:\n    return 1\n",
            tests=(test,),
            sandbox=sandbox,
        )
        assert result.public_accepted is False
        assert result.tiers[-1].name == "ruff"
        assert sandbox.calls == ["ruff"]


@pytest.mark.parametrize(
    "artifact_path", ("pkg//solution.py", "pkg/./solution.py", "unicode\u0301/solution.py")
)
def test_noncanonical_artifact_path_alias_rejects_before_sandbox(artifact_path: str) -> None:
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path=artifact_path,
        source=SOURCE,
        tests=(TEST,),
        sandbox=sandbox,
    )
    assert result.public_accepted is False
    assert sandbox.calls == []


@pytest.mark.parametrize(
    "artifact_path", ("scripts/generate-client.py", "tools/runner", "unicodé/solution.py")
)
def test_general_artifact_path_accepts_every_canonical_relative_posix_target(
    artifact_path: str,
) -> None:
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path=artifact_path,
        source=SOURCE,
        tests=(TEST,),
        sandbox=FakeSandbox(),
    )
    assert result.public_accepted is True


def test_general_profile_accepts_class_and_import_without_synthetic_policy() -> None:
    source = """\
import math


class Calculator:
    def root(self, value: int) -> int:
        return math.isqrt(value)
"""
    signature = """\
class Calculator:
    def root(self, value: int) -> int: ...
"""
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="pkg/solution.py",
        source=source,
        signature_source=signature,
        tests=("def test_root():\n    assert Calculator().root(9) == 3\n",),
        sandbox=sandbox,
    )
    assert result.public_accepted is True


def test_class_container_or_method_header_drift_fails_signature_tier() -> None:
    source = """\
class Calculator:
    @staticmethod
    def root(value: int) -> int:
        return value
"""
    required = """\
class Calculator:
    def root(self, value: int) -> int: ...
"""
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=source,
        signature_source=required,
        tests=("def test_root():\n    assert Calculator().root(1) == 1\n",),
        sandbox=sandbox,
    )
    assert result.public_accepted is False
    assert result.tiers[-1].name == "signature_conformance"
    assert sandbox.calls == ["ruff"]


def test_general_signature_matches_the_plain_receiver_annotation_exactly() -> None:
    accepted = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source="class Box:\n    def get(self: Box, x: int) -> int:\n        return x\n",
        signature_source="class Box:\n    def get(self: Box, x: int) -> int: ...\n",
        tests=("def test_get():\n    assert Box().get(1) == 1\n",),
        sandbox=FakeSandbox(),
    )
    drifted = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source="class Box:\n    def get(self: object, x: int) -> int:\n        return x\n",
        signature_source="class Box:\n    def get(self: Box, x: int) -> int: ...\n",
        tests=("def test_get():\n    assert Box().get(1) == 1\n",),
        sandbox=FakeSandbox(),
    )
    assert accepted.public_accepted is True
    assert drifted.public_accepted is False
    assert drifted.tiers[-1].name == "signature_conformance"


@pytest.mark.parametrize(
    "signature_source",
    (
        "class Box:\n    def f(self, x: int) -> int: ...\nclass Box:\n    def g(self) -> int: ...\n",
        "def f(x: int) -> int: ...\ndef f(x: int) -> int: ...\n",
        "class Box:\n    def f(self, x: int) -> int: ...\n    def f(self, x: int) -> int: ...\n",
        "def f(x: int): ...\n",
        "def f(x) -> int: ...\n",
        "def f(x: int) -> int:\n    return x\n",
        "def f(x: int) -> int: ...  # type: ignore\n",
        "import os\ndef f(x: int) -> int: ...\n",
        "@decorator(*items)\ndef f(x: int) -> int: ...\n",
        "class Box:\n    @decorators.staticmethod\n    def f(self, x: int) -> int: ...\n",
        "class Box(list[int | None]):\n    def f(self, x: int) -> int: ...\n",
    ),
)
def test_general_signature_stub_grammar_rejects_invalid_or_duplicate_declarations(
    signature_source: str,
) -> None:
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source="def f(x: int) -> int:\n    return x\n",
        signature_source=signature_source,
        tests=("def test_f():\n    assert f(1) == 1\n",),
        sandbox=sandbox,
    )
    assert result.public_accepted is False
    assert sandbox.calls == []


def test_general_signature_annotation_has_exact_512_byte_boundary() -> None:
    allowed = "A" * 512
    rejected = "A" * 513
    accepted_result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=f"def f(x: {allowed}) -> int:\n    return 1\n",
        signature_source=f"def f(x: {allowed}) -> int: ...\n",
        tests=("def test_f():\n    assert f(None) == 1\n",),
        sandbox=FakeSandbox(),
    )
    sandbox = FakeSandbox()
    rejected_result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=f"def f(x: {rejected}) -> int:\n    return 1\n",
        signature_source=f"def f(x: {rejected}) -> int: ...\n",
        tests=("def test_f():\n    assert f(None) == 1\n",),
        sandbox=sandbox,
    )
    assert accepted_result.public_accepted is True
    assert rejected_result.public_accepted is False
    assert sandbox.calls == []


def test_general_signature_has_exact_64_kib_boundary_and_no_raw_substring_filter() -> None:
    base = '@decorator(note="type: ignore")\ndef f(x: int) -> int: ...\n'
    allowed = base + " " * ((64 * 1024) - len(base.encode()))
    source = '@decorator(note="type: ignore")\ndef f(x: int) -> int:\n    return x\n'
    accepted = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=source,
        signature_source=allowed,
        tests=("def test_f():\n    assert f(1) == 1\n",),
        sandbox=FakeSandbox(),
    )
    sandbox = FakeSandbox()
    rejected = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=source,
        signature_source=allowed + " ",
        tests=("def test_f():\n    assert f(1) == 1\n",),
        sandbox=sandbox,
    )
    assert len(allowed.encode()) == 64 * 1024
    assert accepted.public_accepted is True
    assert rejected.public_accepted is False
    assert sandbox.calls == []


@pytest.mark.parametrize(
    ("signature_source", "source"),
    (
        (
            "async def fetch(x: int) -> int: ...\n",
            "async def fetch(x: int) -> int:\n    return x\n",
        ),
        (
            "class Box:\n    def get(self: Box, x: int) -> int: ...\n",
            "class Box:\n    def get(self: Box, x: int) -> int:\n        return x\n",
        ),
        (
            "class Box:\n    @classmethod\n    def make(cls: type[Box], x: int) -> Box: ...\n",
            "class Box:\n    @classmethod\n    def make(cls: type[Box], x: int) -> Box:\n        return cls()\n",
        ),
        (
            "class Box:\n    @staticmethod\n    def get(x: int) -> int: ...\n",
            "class Box:\n    @staticmethod\n    def get(x: int) -> int:\n        return x\n",
        ),
        (
            "class Box:\n    async def get(self, x: int) -> int: ...\n",
            "class Box:\n    async def get(self, x: int) -> int:\n        return x\n",
        ),
        (
            'def format_value(x: int, /, y: str = "x", *, enabled: bool = True) -> str: ...\n',
            'def format_value(x: int, /, y: str = "x", *, enabled: bool = True) -> str:\n'
            "    return y\n",
        ),
        (
            "@decorator(flag=True)\ndef render(x: int) -> int: ...\n",
            "@decorator(flag=True)\ndef render(x: int) -> int:\n    return x\n",
        ),
        (
            "@entity(version=1)\n"
            "class Box(Base, metaclass=Meta, frozen=True):\n"
            "    def get(self, x: int) -> int: ...\n",
            "@entity(version=1)\n"
            "class Box(Base, metaclass=Meta, frozen=True):\n"
            "    def get(self, x: int) -> int:\n"
            "        return x\n"
            "\n"
            "    def helper(self) -> int:\n"
            "        return 1\n",
        ),
    ),
)
def test_general_signature_golden_matrix_accepts_exact_public_headers(
    signature_source: str, source: str
) -> None:
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=source,
        signature_source=signature_source,
        tests=("def test_placeholder():\n    assert True\n",),
        sandbox=FakeSandbox(),
    )
    assert result.public_accepted is True


@pytest.mark.parametrize(
    ("signature_source", "source"),
    (
        (
            "async def f(x: int) -> int: ...\n",
            "def f(x: int) -> int:\n    return x\n",
        ),
        (
            "def f(x: int) -> int: ...\n",
            "class Box:\n    def f(self, x: int) -> int:\n        return x\n",
        ),
        (
            "class Box:\n    def f(self, x: int) -> int: ...\n",
            "class Box:\n    @staticmethod\n    def f(x: int) -> int:\n        return x\n",
        ),
        (
            "class Box:\n    @classmethod\n    def f(cls: type[Box], x: int) -> int: ...\n",
            "class Box:\n    @classmethod\n    def f(cls: object, x: int) -> int:\n        return x\n",
        ),
        (
            "class Box:\n    def f(self, x: int) -> int: ...\n",
            "class Box:\n    def f(this, x: int) -> int:\n        return x\n",
        ),
        (
            "class Box:\n    async def f(self, x: int) -> int: ...\n",
            "class Box:\n    def f(self, x: int) -> int:\n        return x\n",
        ),
        (
            "class Box:\n    @staticmethod\n    def f(x: int) -> int: ...\n",
            "class Box:\n    @staticmethod\n    def f(x: str) -> int:\n        return 1\n",
        ),
        (
            "class Box:\n    @classmethod\n    def f(cls, x: int) -> int: ...\n",
            "class Box:\n    def f(self, x: int) -> int:\n        return x\n",
        ),
        (
            "def f(x: int, /) -> int: ...\n",
            "def f(x: int) -> int:\n    return x\n",
        ),
        (
            "def f(left: int, /, right: str) -> int: ...\n",
            "def f(other: int, /, right: str) -> int:\n    return other\n",
        ),
        (
            "def f(first: int, second: str, /) -> int: ...\n",
            "def f(second: str, first: int, /) -> int:\n    return first\n",
        ),
        (
            "def f(x: int = 1, /) -> int: ...\n",
            "def f(x: int = 2, /) -> int:\n    return x\n",
        ),
        (
            "def f(x: int, /) -> int: ...\n",
            "def f(x: int = 1, /) -> int:\n    return x\n",
        ),
        (
            "def f(x: int, /) -> int: ...\n",
            "def f(x: str, /) -> int:\n    return 1\n",
        ),
        (
            "def f(left: int, right: str) -> int: ...\n",
            "def f(other: int, right: str) -> int:\n    return other\n",
        ),
        (
            "def f(left: int, right: str) -> int: ...\n",
            "def f(right: str, left: int) -> int:\n    return left\n",
        ),
        (
            "def f(x: int) -> int: ...\n",
            "def f(*, x: int) -> int:\n    return x\n",
        ),
        (
            "def f(*, enabled: bool) -> int: ...\n",
            "def f(*, active: bool) -> int:\n    return 1\n",
        ),
        (
            "def f(*, first: int, second: str) -> int: ...\n",
            "def f(*, second: str, first: int) -> int:\n    return first\n",
        ),
        (
            "def f(*, enabled: bool) -> int: ...\n",
            "def f(*, enabled: bool = True) -> int:\n    return 1\n",
        ),
        (
            "def f(*, count: int = 1) -> int: ...\n",
            "def f(*, count: int = 2) -> int:\n    return count\n",
        ),
        (
            "def f(*, value: int) -> int: ...\n",
            "def f(*, value: str) -> int:\n    return 1\n",
        ),
        (
            "def f(x: int) -> int: ...\n",
            "def f(x: int = 1) -> int:\n    return x\n",
        ),
        (
            "def f(x: int = 1) -> int: ...\n",
            "def f(x: int = 2) -> int:\n    return x\n",
        ),
        (
            "def f(x: int) -> int: ...\n",
            "def f(x: str) -> int:\n    return 1\n",
        ),
        (
            "def f(x: int) -> int: ...\n",
            "def f(x: int) -> str:\n    return str(x)\n",
        ),
        (
            "@first\n@second(flag=True)\ndef f(x: int) -> int: ...\n",
            "@second(flag=True)\n@first\ndef f(x: int) -> int:\n    return x\n",
        ),
        (
            "def expected(x: int) -> int: ...\n",
            "def actual(x: int) -> int:\n    return x\n",
        ),
        (
            "class Box:\n    def f(self) -> int: ...\n",
            "class Crate:\n    def f(self) -> int:\n        return 1\n",
        ),
        (
            "class Box:\n    def expected(self) -> int: ...\n",
            "class Box:\n    def actual(self) -> int:\n        return 1\n",
        ),
        (
            "class Box(Base):\n    def f(self) -> int: ...\n",
            "class Box(Other):\n    def f(self) -> int:\n        return 1\n",
        ),
        (
            "class Box(First, Second):\n    def f(self) -> int: ...\n",
            "class Box(Second, First):\n    def f(self) -> int:\n        return 1\n",
        ),
        (
            "class Box(metaclass=Meta):\n    def f(self) -> int: ...\n",
            "class Box(metaclass=Other):\n    def f(self) -> int:\n        return 1\n",
        ),
        (
            "@entity(version=1)\nclass Box:\n    def f(self) -> int: ...\n",
            "@entity(version=2)\nclass Box:\n    def f(self) -> int:\n        return 1\n",
        ),
        (
            "@first\n@second(flag=True)\nclass Box:\n    def f(self) -> int: ...\n",
            "@second(flag=True)\n@first\nclass Box:\n    def f(self) -> int:\n        return 1\n",
        ),
        (
            "class Box(frozen=True):\n    def f(self) -> int: ...\n",
            "class Box(frozen=False):\n    def f(self) -> int:\n        return 1\n",
        ),
        (
            "class Box(frozen=True):\n    def f(self) -> int: ...\n",
            "class Box(slots=True):\n    def f(self) -> int:\n        return 1\n",
        ),
        (
            "class Box(frozen=True, slots=True):\n    def f(self) -> int: ...\n",
            "class Box(slots=True, frozen=True):\n    def f(self) -> int:\n        return 1\n",
        ),
        (
            "class Box:\n    @logged(level=1)\n    def f(self) -> int: ...\n",
            "class Box:\n    @logged(level=2)\n    def f(self) -> int:\n        return 1\n",
        ),
        (
            "def f(x: int) -> int: ...\n",
            "def f(x: int, y: int = 0) -> int:\n    return x + y\n",
        ),
    ),
)
def test_general_signature_golden_matrix_rejects_one_field_header_drift(
    signature_source: str, source: str
) -> None:
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=source,
        signature_source=signature_source,
        tests=("def test_placeholder():\n    assert True\n",),
        sandbox=sandbox,
    )
    assert result.public_accepted is False
    assert result.tiers[-1].name == "signature_conformance"
    assert sandbox.calls == ["ruff"]


def test_candidate_type_ignore_directive_fails_signature_before_pyright() -> None:
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source="def f(x: int) -> int:  # type: ignore\n    return x\n",
        signature_source="def f(x: int) -> int: ...\n",
        tests=("def test_f():\n    assert f(1) == 1\n",),
        sandbox=sandbox,
    )
    assert result.public_accepted is False
    assert result.tiers[-1].name == "signature_conformance"
    assert sandbox.calls == ["ruff"]


@pytest.mark.parametrize(
    "source",
    (
        "def f(x: int) -> int:\n    return x\n\ndef f(x: int) -> int:\n    return x\n",
        "class Box:\n    def f(self, x: int) -> int:\n        return x\n\n"
        "class Box:\n    def f(self, x: int) -> int:\n        return x\n",
        "class Box:\n"
        "    def f(self, x: int) -> int:\n"
        "        return x\n\n"
        "    def f(self, x: int) -> int:\n"
        "        return x\n",
        "def f(x):  # type: (int) -> int\n    return x\n",
    ),
)
def test_candidate_duplicate_or_type_comment_metadata_never_satisfies_spec(
    source: str,
) -> None:
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=source,
        signature_source="def f(x: int) -> int: ...\n",
        tests=("def test_f():\n    assert f(1) == 1\n",),
        sandbox=sandbox,
    )
    assert result.public_accepted is False
    assert result.tiers[-1].name == "signature_conformance"
    assert sandbox.calls == ["ruff"]


def test_candidate_type_parameter_syntax_is_rejected_by_the_python_311_profile() -> None:
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source="def f[T](x: T) -> T:\n    return x\n",
        signature_source="def f(x: int) -> int: ...\n",
        tests=("def test_f():\n    assert f(1) == 1\n",),
        sandbox=sandbox,
    )
    assert result.public_accepted is False
    assert result.tiers[-1].name == "assembly"
    assert sandbox.calls == []


def test_failure_diagnostic_excerpt_removes_volatile_timing_and_addresses() -> None:
    first = _diagnostic_excerpt(
        b"FAILED /input/test_public_00.py::test_f - assert 1 == 2\n"
        b"= 1 failed in 0.17s =\nobject at 0x1234abcd\n",
        tier="pytest",
    )
    second = _diagnostic_excerpt(
        b"FAILED /input/test_public_00.py::test_f - assert 1 == 2\n"
        b"= 1 failed in 9.99s =\nobject at 0xdeadbeef\n",
        tier="pytest",
    )
    assert first == second


@pytest.mark.parametrize(
    ("source", "tests"),
    (
        (
            "import os\n\ndef f() -> int:\n    return 1\n",
            ("def test_f():\n    assert f() == 1\n",),
        ),
        (
            "def f() -> int:\n    return 1\n",
            ("import os\n\ndef test_f():\n    assert f() == 1\n",),
        ),
    ),
)
def test_synthetic_profile_rejects_unsafe_candidate_or_test_before_sandbox(
    source: str, tests: tuple[str, ...]
) -> None:
    sandbox = FakeSandbox()
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source=source,
        signature_source="def f() -> int: ...\n",
        synthetic_target="f",
        tests=tests,
        sandbox=sandbox,
    )
    assert result.public_accepted is False
    assert result.tiers[-1].name == "assembly"
    assert sandbox.calls == []


def test_duplicate_or_crlf_tests_reject_before_sandbox() -> None:
    sandbox = FakeSandbox()
    duplicate = (TEST, TEST)
    duplicate_result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=duplicate,
        sandbox=sandbox,
    )
    assert duplicate_result.public_accepted is False
    assert duplicate_result.tiers[-1].name == "assembly"
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        tests=(TEST.replace("\n", "\r\n"),),
        sandbox=sandbox,
    )
    assert result.public_accepted is False
    assert result.tiers[-1].name == "assembly"


def test_identical_test_source_is_allowed_across_distinct_categories_only() -> None:
    same = "def test_same():\n    assert add(1, 2) == 3\n"
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=SOURCE,
        test_suite=VerificationTestsV1(spec=(same,), public=(same,)),
        sandbox=FakeSandbox(),
    )
    assert result.public_accepted is True
    assert result.collection_evidence is not None
    assert tuple(name for name, _ in result.collection_evidence.test_sources) == (
        "test_spec_00.py",
        "test_public_00.py",
    )


def test_docker_volume_acquisition_rolls_back_every_partial_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    removed: list[tuple[str, str]] = []

    def control(*argv: str, timeout: int = 30) -> bytes:
        del timeout
        if argv and argv[0] == "cp":
            raise sandbox_module.SandboxUnavailable("injected copy failure")
        return b""

    def remove(kind, name):  # type: ignore[no-untyped-def]
        removed.append((kind, name))

    monkeypatch.setattr(sandbox_module, "_docker_control", control)
    monkeypatch.setattr(sandbox_module, "_docker_remove_owned", remove)
    workspace = sandbox_module._DockerVolumeWorkspace("sha256:" + "a" * 64, input_dir, output_dir)
    with pytest.raises(sandbox_module.SandboxUnavailable, match="copy failure"):
        workspace.__enter__()
    assert [kind for kind, _ in removed].count("container") == 1
    assert [kind for kind, _ in removed].count("volume") == 3


def test_runtime_image_lock_rejects_mutated_inventory(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    for relative in (
        "contextmesh/docker/rrcv2-verifier.Dockerfile",
        "contextmesh/docker/rrcv2-verifier-requirements.txt",
        "contextmesh/docker/rrcv2-verifier-seccomp.json",
        ".generated/state/rrcv2-convergence/verify/sandbox-evidence.v2.json",
        "rrc/pipeline/verifier_image_lock.v2.json",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / relative, target)
    capability = tmp_path / ".generated/state/rrcv2-convergence/verify/sandbox-evidence.v2.json"
    capability.chmod(0o600)
    lock_path = tmp_path / "rrc/pipeline/verifier_image_lock.v2.json"
    lock = json.loads(lock_path.read_text())
    lock["executables"][0]["sha256"] = "0" * 64
    lock_path.write_bytes(sandbox_module._canonical(lock))
    with pytest.raises(sandbox_module.SandboxUnavailable, match="lock hash"):
        sandbox_module._closed_lock(tmp_path)


def test_runtime_sandbox_capability_rejects_semantically_false_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path(__file__).resolve().parents[1]
    capability_path = repo / ".generated/state/rrcv2-convergence/verify/sandbox-evidence.v2.json"
    value = json.loads(capability_path.read_text())
    by_name = {row["name"]: row for row in value["probes"]}
    by_name["scratch_limit"]["observation"]["written"] = 1
    poisoned = sandbox_module._canonical(value)
    original = sandbox_module._bounded_regular

    def read(path: Path, cap: int, *, mode: int | None = None) -> bytes:
        if path == capability_path:
            return poisoned
        return original(path, cap, mode=mode)

    monkeypatch.setattr(sandbox_module, "_bounded_regular", read)
    with pytest.raises(sandbox_module.SandboxUnavailable, match="scratch boundary"):
        sandbox_module._validate_capability_v2(repo)


def test_backend_ownership_mutation_rejects_before_any_backend_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path(__file__).resolve().parents[1]
    original = sandbox_module._bounded_regular

    def poisoned(path: Path, cap: int, *, mode: int | None = None) -> bytes:
        raw = original(path, cap, mode=mode)
        if path.name == "backend-ownership.v1.json":
            value = json.loads(raw)
            value["context"] = "foreign-context"
            return sandbox_module._canonical(value)
        return raw

    monkeypatch.setattr(sandbox_module, "_bounded_regular", poisoned)
    monkeypatch.setattr(
        sandbox_module,
        "_bounded_backend_command",
        lambda *_args, **_kwargs: pytest.fail("backend observer ran after ownership rejection"),
    )
    with pytest.raises(sandbox_module.SandboxUnavailable, match="ownership identity"):
        sandbox_module._backend_identity_snapshot(repo)


def test_backend_observer_environment_uses_only_capability_probed_tool_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/poison:/usr/sbin:/sbin:/usr/local/bin:/usr/bin:/bin")
    environment = sandbox_module._docker_env()
    assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert "/usr/sbin" not in environment["PATH"]


def test_stable_wrong_colima_profile_config_is_rejected() -> None:
    with pytest.raises(sandbox_module.SandboxUnavailable, match="profile config"):
        sandbox_module._validate_colima_profile_config(b"poisoned-profile-config\n")


def test_stable_wrong_docker_daemon_info_is_rejected() -> None:
    wrong = sandbox_module._canonical(
        {
            "Architecture": "x86_64",
            "CgroupVersion": "2",
            "CpuCfsPeriod": True,
            "CpuCfsQuota": True,
            "MemoryLimit": True,
            "NCPU": 99,
            "Name": sandbox_module.DOCKER_CONTEXT,
            "OSType": "linux",
            "PidsLimit": True,
            "SecurityOptions": ["name=seccomp,profile=builtin", "name=cgroupns"],
            "SwapLimit": True,
        }
    )
    with pytest.raises(sandbox_module.SandboxUnavailable, match="daemon settings"):
        sandbox_module._validate_docker_daemon_info(wrong)


def test_backend_identity_failure_prevents_image_or_tier_launch() -> None:
    class NoLaunchSupervisor:
        calls = 0

        def run(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise AssertionError("Docker tier launched")

        def attest_image(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise AssertionError("image attestation launched")

    supervisor = NoLaunchSupervisor()

    def reject(_repo: Path) -> bytes:
        raise sandbox_module.SandboxUnavailable("injected endpoint drift")

    sandbox = SealedDockerSandbox(
        Path(__file__).resolve().parents[1],
        supervisor=supervisor,
        backend_identity=reject,
    )
    with pytest.raises(sandbox_module.SandboxUnavailable, match="endpoint drift"):
        sandbox.run(
            tier="ruff",
            verification_profile="rrcv2_general_v1",
            artifact_path="solution.py",
            source=SOURCE.encode(),
            tests=(),
            limits=SandboxLimitsV1(),
        )
    assert supervisor.calls == 0


def test_backend_identity_is_reopened_after_tier_and_drift_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots = iter((b"before", b"after"))

    class Supervisor:
        @staticmethod
        def attest_image(authority, seccomp_path):  # type: ignore[no-untyped-def]
            del authority, seccomp_path
            return None

        @staticmethod
        def run(
            argv,
            *,
            container_name,
            timeout_seconds,
            stdout_cap,
            stderr_cap,
        ):  # type: ignore[no-untyped-def]
            del argv, container_name, timeout_seconds, stdout_cap, stderr_cap
            raise AssertionError("the patched tier must not invoke Docker")

    class Workspace:
        def __init__(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            pass

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        @staticmethod
        def prove_sentinel() -> None:
            return None

    monkeypatch.setattr(sandbox_module, "_validate_capability_v2", lambda _repo: None)
    monkeypatch.setattr(
        sandbox_module,
        "_closed_lock",
        lambda _repo: {"image_config_digest": "sha256:" + "1" * 64},
    )
    monkeypatch.setattr(sandbox_module, "_DockerVolumeWorkspace", Workspace)
    monkeypatch.setattr(
        SealedDockerSandbox,
        "_materialize",
        lambda *_args, **_kwargs: (tmp_path / "input", tmp_path / "output"),
    )
    monkeypatch.setattr(
        SealedDockerSandbox,
        "_run_workspace",
        lambda *_args, **_kwargs: SandboxResultV1("ruff", 0, b"", b"", b"source"),
    )
    sandbox = SealedDockerSandbox(
        tmp_path,
        supervisor=Supervisor(),
        backend_identity=lambda _repo: next(snapshots),
    )
    with pytest.raises(sandbox_module.SandboxUnavailable, match="drifted during"):
        sandbox.run(
            tier="ruff",
            verification_profile="rrcv2_general_v1",
            artifact_path="solution.py",
            source=SOURCE.encode(),
            tests=(),
            limits=SandboxLimitsV1(),
        )


_REAL = pytest.mark.skipif(
    os.environ.get("RRD_VERIFY_MODEL_BEARING") != "1",
    reason="real Docker verification requires the marked outer guard",
)


@pytest.mark.sandbox_real
@_REAL
def test_real_backend_identity_reopens_owned_context_profile_and_endpoint() -> None:
    repo = Path(__file__).resolve().parents[1]
    before = sandbox_module._backend_identity_snapshot(repo)
    after = sandbox_module._backend_identity_snapshot(repo)
    assert before == after
    profile = sandbox_module._bounded_regular(
        Path(os.environ["HOME"]) / ".colima/rrcv2-verifier/colima.yaml",
        1024 * 1024,
    )
    sandbox_module._validate_colima_profile_config(profile)
    with pytest.raises(sandbox_module.SandboxUnavailable, match="config bytes drifted"):
        sandbox_module._validate_colima_profile_config(profile + b"unknownStableSetting: true\n")
    info_raw = sandbox_module._bounded_backend_command(
        (
            "docker",
            "--context",
            sandbox_module.DOCKER_CONTEXT,
            "info",
            "--format",
            "{{json .}}",
        )
    )
    sandbox_module._validate_docker_daemon_info(info_raw)
    for field, value in (
        ("ServerVersion", "0.0.0-poisoned"),
        ("DockerRootDir", "/foreign"),
        ("ExtraStableField", True),
    ):
        poisoned = json.loads(info_raw)
        poisoned[field] = value
        with pytest.raises(sandbox_module.SandboxUnavailable, match="daemon"):
            sandbox_module._validate_docker_daemon_info(sandbox_module._canonical(poisoned))


@pytest.mark.sandbox_real
@_REAL
@pytest.mark.parametrize(
    ("source", "expected_names"),
    (
        ("def f(:\n    pass\n", ("assembly",)),
        ("def f() -> int:\n    return missing\n", ("assembly", "ruff")),
        ("def f() -> int:\n    return 'wrong'\n", ("assembly", "ruff", "pyright")),
        (
            "def f() -> int:\n    return 1\n",
            ("assembly", "ruff", "pyright", "pytest"),
        ),
    ),
)
def test_real_verifier_stops_syntax_lint_type_or_behavior_failure_at_exact_tier(
    source: str,
    expected_names: tuple[str, ...],
) -> None:
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=source,
        tests=("def test_f():\n    assert f() == 2\n",),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is False
    assert tuple(row.name for row in result.tiers) == expected_names
    assert result.tiers[-1].status == "failed"
    assert all(row.status == "passed" for row in result.tiers[:-1])


@pytest.mark.sandbox_real
@_REAL
def test_real_failed_pytest_diagnostic_authority_is_repeatable() -> None:
    sandbox = SealedDockerSandbox(Path(__file__).resolve().parents[1])
    rows = []
    for _ in range(2):
        result = verify_candidate(
            attempt_id=ATTEMPT,
            verification_profile="rrcv2_general_v1",
            artifact_path="solution.py",
            source="def f() -> int:\n    return 1\n",
            tests=("def test_f():\n    assert f() == 2\n",),
            sandbox=sandbox,
        )
        assert result.public_accepted is False
        rows.append(result.tiers[-1])
    assert rows[0].diagnostics_sha256 == rows[1].diagnostics_sha256


@pytest.mark.sandbox_real
@_REAL
def test_real_sealed_backend_accepts_general_code_and_enforces_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-container")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    monkeypatch.setenv("PYTEST_PLUGINS", "hostile_plugin")
    sentinel = tmp_path / "real-host-sentinel"
    sentinel.write_text("must-not-be-readable", encoding="utf-8")
    sentinel.chmod(0o600)
    source = (
        """\
import os
import resource
import socket


def identity_decorator(function):
    return function


@identity_decorator
def doubled(value: int) -> int:
    return value * 2


class Ledger:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    def create(self, key: str, value: int) -> None:
        self.values[key] = value

    def read(self, key: str) -> int:
        return self.values[key]


def containment_holds() -> tuple[bool, ...]:
    credentials_absent = not any(
        name in os.environ
        for name in (
            "OPENAI_API_KEY",
            "OLLAMA_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "PYTEST_ADDOPTS",
            "PYTEST_PLUGINS",
        )
    )
    try:
        open(__HOST_SENTINEL__, encoding="utf-8").read()
        external_read_denied = False
    except OSError:
        external_read_denied = True
    mounted_sentinel_exists = os.path.exists(__MOUNTED_SENTINEL__)
    try:
        open(__MOUNTED_SENTINEL__, encoding="utf-8").read()
        mounted_read_denied = False
    except PermissionError:
        mounted_read_denied = True
    except OSError:
        mounted_read_denied = False
    try:
        open(__INPUT_ARTIFACT__, "w", encoding="utf-8").write("changed")
        input_write_denied = False
    except OSError:
        input_write_denied = True
    try:
        open("/input/test_public_00.py", "w", encoding="utf-8").write("changed")
        test_write_denied = False
    except OSError:
        test_write_denied = True
    try:
        open("/input/pyrightconfig.json", "w", encoding="utf-8").write("changed")
        config_write_denied = False
    except OSError:
        config_write_denied = True
    try:
        socket.socket()
        socket_denied = False
    except OSError:
        socket_denied = True
    try:
        os.fork()
        fork_denied = False
    except OSError:
        fork_denied = True
    file_descriptors_bounded = resource.getrlimit(resource.RLIMIT_NOFILE)[0] == 64
    file_bytes_bounded = resource.getrlimit(resource.RLIMIT_FSIZE)[0] == 4194304
    memory_bounded = open("/sys/fs/cgroup/memory.max", encoding="utf-8").read().strip() == "536870912"
    scratch = os.statvfs("/scratch")
    scratch_bounded = scratch.f_blocks * scratch.f_frsize == 33554432
    return (
        credentials_absent,
        external_read_denied,
        mounted_sentinel_exists,
        mounted_read_denied,
        input_write_denied,
        test_write_denied,
        config_write_denied,
        socket_denied,
        fork_denied,
        file_descriptors_bounded,
        file_bytes_bounded,
        memory_bounded,
        scratch_bounded,
    )
""".replace("__HOST_SENTINEL__", repr(str(sentinel)))
        .replace("__MOUNTED_SENTINEL__", repr(sandbox_module.DENIED_SENTINEL_PATH))
        .replace(
            "__INPUT_ARTIFACT__",
            repr(f"/input/{sandbox_module._physical_artifact_path('solution.py')}"),
        )
    )
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=source,
        tests=(
            "def test_containment():\n    assert all(containment_holds())\n    assert doubled(3) == 6\n",
            """\
import pytest


@pytest.fixture
def ledger():
    return Ledger()


def test_crud_fixture(ledger):
    ledger.create("one", 1)
    assert ledger.read("one") == 1
""",
        ),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is True
    assert [row.status for row in result.tiers] == ["passed"] * 4
    assert sentinel.read_text(encoding="utf-8") == "must-not-be-readable"


@pytest.mark.sandbox_real
@_REAL
@pytest.mark.parametrize(
    "artifact_path",
    (
        "scripts/generate-client.py",
        "pkg/it's.py",
        "pkg/line\nbreak.py",
        "pkg/café.py",
        "tools/runner",
    ),
)
def test_real_general_profile_accepts_every_canonical_target_path(
    artifact_path: str,
) -> None:
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path=artifact_path,
        source="def value() -> int:\n    return 1\n",
        tests=("def test_value():\n    assert value() == 1\n",),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is True


@pytest.mark.sandbox_real
@_REAL
def test_real_general_profile_accepts_filesystem_oversize_logical_path() -> None:
    artifact_path = "a" * 256
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path=artifact_path,
        source="def value() -> int:\n    return 1\n",
        tests=("def test_value():\n    assert value() == 1\n",),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is True
    assert result.artifact is not None
    assert result.artifact.artifact_path == artifact_path


@pytest.mark.sandbox_real
@_REAL
def test_real_general_candidate_has_normal_file_backed_module_metadata() -> None:
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="pkg/solution.py",
        source=(
            "from importlib.machinery import SourceFileLoader\n"
            "from pathlib import Path\n\n"
            "MODULE_FILE = __file__\n"
            "SOURCE_BYTES = Path(__file__).read_bytes()\n\n"
            "def candidate_metadata() -> tuple[str, str, str, bytes]:\n"
            "    assert __spec__ is not None\n"
            "    assert __spec__.origin is not None\n"
            "    assert isinstance(__loader__, SourceFileLoader)\n"
            "    return __file__, __spec__.origin, __loader__.path, Path(__file__).read_bytes()\n"
        ),
        tests=(
            "def test_candidate_metadata():\n"
            "    file, origin, loader_path, source = candidate_metadata()\n"
            "    assert file == MODULE_FILE == origin == loader_path\n"
            "    assert file.startswith('/input/task/artifact-')\n"
            "    assert file.endswith('.py')\n"
            "    assert source == SOURCE_BYTES\n"
            "    assert b'def candidate_metadata' in source\n",
        ),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is True


@pytest.mark.sandbox_real
@_REAL
def test_real_verifier_rejects_crlf_candidate_before_durable_artifact() -> None:
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source="def f() -> int:\r\n    return 1\r\n",
        tests=("def test_f():\n    assert f() == 1\n",),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is False
    assert result.artifact is None
    assert all("\r" not in blob.decode("utf-8") for blob in result.evidence_blobs)


@pytest.mark.sandbox_real
@_REAL
def test_real_sealed_backend_accepts_positive_synthetic_facade_code() -> None:
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source=("def integer_root(value: int) -> int:\n    return math.isqrt(value)\n"),
        signature_source="def integer_root(value: int) -> int: ...\n",
        synthetic_target="integer_root",
        tests=("def test_root():\n    assert integer_root(17) == 4\n",),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is True
    assert result.artifact is not None
    assert "import math as math" in result.artifact.source
    expected = (
        "from __future__ import annotations\n\n"
        "import json as json\nimport math as math\nimport re as re\n\n"
        "TYPE_CHECKING = False\nif TYPE_CHECKING:\n    pass\n\n\n"
        "def integer_root(value: int) -> int:\n    return math.isqrt(value)"
    )
    assert result.artifact.source == expected
    assert (len(expected.encode()), hashlib.sha256(expected.encode()).hexdigest()) == (
        209,
        "266d40aeec40d9fd1b6dd8d1f39cde824ff3d7b2f0725de6854274fd912caa11",
    )


@pytest.mark.sandbox_real
@_REAL
def test_real_sealed_backend_accepts_nested_synthetic_domain_annotations() -> None:
    signature = (
        "def normalize(value: dict[str, list[pkg.User | None]]) "
        "-> dict[str, list[pkg.User | None]]: ...\n"
    )
    body = (
        "def normalize(value: dict[str, list[pkg.User | None]]) "
        "-> dict[str, list[pkg.User | None]]:\n"
        "    return value\n"
    )
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source=body,
        signature_source=signature,
        synthetic_target="normalize",
        tests=("def test_normalize():\n    assert normalize({}) == {}\n",),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is True
    assert result.artifact is not None
    assert result.artifact.source.endswith("    return value")
    assert "class pkg:" in result.artifact.source
    expected = (
        "from __future__ import annotations\n\n"
        "import json as json\nimport math as math\nimport re as re\n\n"
        "TYPE_CHECKING = False\nif TYPE_CHECKING:\n\n"
        "    class pkg:\n        class User:\n            pass\n\n\n"
        "def normalize(\n"
        "    value: dict[str, list[pkg.User | None]],\n"
        ") -> dict[str, list[pkg.User | None]]:\n"
        "    return value"
    )
    assert result.artifact.source == expected
    assert (len(expected.encode()), hashlib.sha256(expected.encode()).hexdigest()) == (
        303,
        "cac76f17edfdebf82ddc61607e00a6892748bdf0a736057b609cdd730be5f117",
    )


@pytest.mark.sandbox_real
@_REAL
def test_real_sealed_backend_emits_frozen_simple_direct_and_spec_authorities() -> None:
    sandbox = SealedDockerSandbox(Path(__file__).resolve().parents[1])
    source = "def get_order(x: int) -> int:\n    return 1"
    public_test = "def test_public():\n    assert get_order(1) == 1\n"
    direct = verify_candidate(
        attempt_id="1" * 64,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=source,
        tests=(public_test,),
        sandbox=sandbox,
    )
    spec = verify_candidate(
        attempt_id="1" * 64,
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source=source,
        signature_source="def get_order(x: int) -> int: ...\n",
        synthetic_target="get_order",
        tests=(public_test,),
        sandbox=sandbox,
    )
    assert direct.artifact is not None and spec.artifact is not None
    assert code_artifact_bytes(direct.artifact) == code_artifact_bytes(spec.artifact)
    assert hashlib.sha256(code_artifact_bytes(direct.artifact)).hexdigest() == (
        "8b854e1756bdc4299c2d71bd7a60e263299dc775c7b6aa4004fbe0f8fcb9e5a6"
    )
    assert hashlib.sha256(verification_result_bytes(direct.result)).hexdigest() == (
        "afbc5b65311e44c75e2587881917467bc1e290d1000ab618aadbd101cff3ab4d"
    )
    assert hashlib.sha256(verification_result_bytes(spec.result)).hexdigest() == (
        "8ff3406942b1e2ff209cda3596bb2181373e048b33c61ed51c6cc7c110dbab97"
    )


@pytest.mark.sandbox_real
@_REAL
def test_real_synthetic_runner_denies_transitive_facade_module_escape() -> None:
    prelude = _synthetic_type_prelude(
        "def leak() -> str: ...\n",
        target="leak",
        body_source="def leak() -> str:\n    return json.loads('\\\"ok\\\"')\n",
    )
    result = SealedDockerSandbox(Path(__file__).resolve().parents[1]).run(
        tier="pytest",
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source=(
            prelude
            + "def leak() -> str:\n"
            + "    return json.codecs.sys.modules['os'].environ.get('HOME', '')\n"
        ).encode(),
        tests=(
            SandboxTestFileV1(
                "public",
                0,
                b"def test_leak():\n    assert leak() == ''\n",
            ),
        ),
        selected_node_ids=(),
        limits=SandboxLimitsV1(),
    )
    assert result.exit_code != 0
    assert result.completed_node_ids == ()


@pytest.mark.sandbox_real
@_REAL
def test_real_synthetic_runner_executes_model_test_with_safe_builtins() -> None:
    result = SealedDockerSandbox(Path(__file__).resolve().parents[1]).run(
        tier="pytest",
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source=b"def value() -> int:\n    return 1\n",
        tests=(
            SandboxTestFileV1(
                "public",
                0,
                b"def test_value():\n    open('/input/task/solution.py', encoding='utf-8')\n",
            ),
        ),
        selected_node_ids=(),
        limits=SandboxLimitsV1(),
    )
    assert result.exit_code != 0
    assert result.completed_node_ids == ()


@pytest.mark.sandbox_real
@_REAL
def test_real_oracle_invocations_are_fresh_and_never_create_acceptance_tiers() -> None:
    artifact = CodeArtifactV1(
        ATTEMPT,
        "solution.py",
        """\
from pathlib import Path


def first_visit() -> bool:
    marker = Path("/scratch/oracle-state")
    existed = marker.exists()
    marker.write_text("state", encoding="utf-8")
    return not existed
""".removesuffix("\n"),
    )
    sandbox = SealedDockerSandbox(Path(__file__).resolve().parents[1])
    for _ in range(2):
        result = score_oracle(
            artifact=artifact,
            verification_profile="rrcv2_general_v1",
            oracle_tests=("def test_fresh():\n    assert first_visit()\n",),
            sandbox=sandbox,
        )
        assert result.passed is True


@pytest.mark.sandbox_real
@_REAL
def test_real_sealed_backend_rejects_premature_success_without_completion() -> None:
    sandbox = SealedDockerSandbox(Path(__file__).resolve().parents[1])
    with pytest.raises(SandboxExecutionError, match="completion marker"):
        sandbox.run(
            tier="pytest",
            verification_profile="rrcv2_general_v1",
            artifact_path="solution.py",
            source=b"def value() -> int:\n    return 1\n",
            tests=(
                SandboxTestFileV1("public", 0, b"import os\n\ndef test_exit():\n    os._exit(0)\n"),
            ),
            selected_node_ids=(),
            limits=SandboxLimitsV1(),
        )


@pytest.mark.sandbox_real
@_REAL
def test_real_collection_rejects_zero_tests() -> None:
    result = SealedDockerSandbox(Path(__file__).resolve().parents[1]).run(
        tier="pytest_collect",
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=b"def value() -> int:\n    return 1\n",
        tests=(
            SandboxTestFileV1(
                "public",
                0,
                b"def helper():\n    return value()\n",
            ),
        ),
        selected_node_ids=(),
        limits=SandboxLimitsV1(),
    )
    assert result.exit_code != 0
    assert result.collected_node_ids == ()


@pytest.mark.sandbox_real
@_REAL
@pytest.mark.parametrize(
    "test_source",
    (
        b"def test_exit():\n    raise SystemExit(0)\n",
        b"import pytest\n\ndef test_exit():\n    pytest.exit('stop')\n",
    ),
)
def test_real_system_exit_and_pytest_exit_never_complete_the_node(
    test_source: bytes,
) -> None:
    result = SealedDockerSandbox(Path(__file__).resolve().parents[1]).run(
        tier="pytest",
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=b"def value() -> int:\n    return 1\n",
        tests=(SandboxTestFileV1("public", 0, test_source),),
        selected_node_ids=(),
        limits=SandboxLimitsV1(),
    )
    assert result.exit_code != 0
    assert result.completed_node_ids == ()


@pytest.mark.sandbox_real
@_REAL
def test_real_teardown_failure_never_completes_the_node() -> None:
    result = SealedDockerSandbox(Path(__file__).resolve().parents[1]).run(
        tier="pytest",
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=b"def value() -> int:\n    return 1\n",
        tests=(
            SandboxTestFileV1(
                "public",
                0,
                (
                    b"import pytest\n\n"
                    b"@pytest.fixture\n"
                    b"def broken_teardown():\n"
                    b"    yield\n"
                    b"    raise RuntimeError('teardown failed')\n\n"
                    b"def test_value(broken_teardown):\n"
                    b"    assert value() == 1\n"
                ),
            ),
        ),
        selected_node_ids=(),
        limits=SandboxLimitsV1(),
    )
    assert result.exit_code != 0
    assert result.collected_node_ids == ("test_public_00.py::test_value",)
    assert result.completed_node_ids == ()


@pytest.mark.sandbox_real
@_REAL
def test_real_nonstrict_xpass_is_never_publicly_accepted() -> None:
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source="def value() -> int:\n    return 1\n",
        tests=(
            "import pytest\n\n"
            "@pytest.mark.xfail(reason='must never count as terminal pass')\n"
            "def test_value():\n"
            "    assert value() == 1\n",
        ),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is False
    assert result.collection_evidence is not None
    assert result.collection_evidence.collected_node_ids == ("test_public_00.py::test_value",)
    assert result.collection_evidence.completed_node_ids == ()


@pytest.mark.sandbox_real
@_REAL
def test_real_module_level_skip_with_passing_sibling_is_rejected_at_collection() -> None:
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source="def value() -> int:\n    return 1\n",
        test_suite=VerificationTestsV1(
            public=(
                "import pytest\n\n"
                "pytest.skip('module skip is forbidden', allow_module_level=True)\n",
                "def test_value():\n    assert value() == 1\n",
            )
        ),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is False
    assert result.tiers[-1].name == "pytest"
    assert result.execution_evidence is not None
    assert result.execution_evidence.collected_node_ids == ("test_public_01.py::test_value",)
    assert result.execution_evidence.completed_node_ids == ()


@pytest.mark.sandbox_real
@_REAL
def test_real_partial_general_completion_is_durable_and_rejected() -> None:
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source="def value() -> int:\n    return 1\n",
        tests=(
            "def test_pass():\n    assert value() == 1\n\n"
            "def test_fail():\n    assert value() == 2\n",
        ),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is False
    assert result.collection_evidence is not None
    assert result.collection_evidence.collected_node_ids == (
        "test_public_00.py::test_pass",
        "test_public_00.py::test_fail",
    )
    assert result.collection_evidence.completed_node_ids == ("test_public_00.py::test_pass",)


@pytest.mark.sandbox_real
@_REAL
def test_real_general_test_files_share_one_candidate_module_state() -> None:
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=(
            "import sys\n\n"
            "counter = 0\n\n"
            "def next_observation() -> tuple[bool, int]:\n"
            "    global counter\n"
            "    counter += 1\n"
            "    return sys.modules.get(__name__) is not None, counter\n"
            "\n"
            "def test_helper(required: int) -> int:\n"
            "    return required\n"
        ),
        test_suite=VerificationTestsV1(
            public=(
                "from __future__ import annotations\n\n"
                "from pathlib import Path\n\n"
                "_SELF_MARKER = 'rrcv2-public-source-marker-first'\n"
                "assert _SELF_MARKER in Path(__file__).read_text(encoding='utf-8')\n\n"
                "def test_first():\n    assert next_observation() == (True, 1)\n",
                "from __future__ import annotations\n\n"
                "from pathlib import Path\n\n"
                "_SELF_MARKER = 'rrcv2-public-source-marker-second'\n"
                "assert _SELF_MARKER in Path(__file__).read_text(encoding='utf-8')\n\n"
                "def test_second():\n    assert next_observation() == (True, 2)\n",
            )
        ),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is True
    assert result.collection_evidence is not None
    assert result.collection_evidence.collected_node_ids == (
        "test_public_00.py::test_first",
        "test_public_01.py::test_second",
    )
    assert (
        result.collection_evidence.completed_node_ids
        == result.collection_evidence.collected_node_ids
    )


@pytest.mark.sandbox_real
@_REAL
def test_real_general_fixture_only_module_keeps_candidate_globals() -> None:
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source="def value() -> int:\n    return 7\n",
        test_suite=VerificationTestsV1(
            public=(
                "import pytest\n\n"
                "@pytest.fixture(scope='session')\n"
                "def shared_value():\n"
                "    return value()\n",
                "from test_public_00 import shared_value\n\n"
                "def test_shared(shared_value):\n"
                "    assert shared_value == 7\n",
            )
        ),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is True
    assert result.collection_evidence is not None
    assert result.collection_evidence.collected_node_ids == ("test_public_01.py::test_shared",)
    assert result.collection_evidence.completed_node_ids == ("test_public_01.py::test_shared",)


@pytest.mark.sandbox_real
@_REAL
def test_real_independent_test_cannot_read_an_outside_sentinel_or_open_socket(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "independent-secret"
    sentinel.write_text("secret", encoding="utf-8")
    test_source = f"""\
import socket
import os


def test_independent_containment():
    try:
        open({str(sentinel)!r}, encoding="utf-8").read()
    except OSError:
        outside_denied = True
    else:
        outside_denied = False
    mounted_exists = os.path.exists({sandbox_module.DENIED_SENTINEL_PATH!r})
    try:
        open({sandbox_module.DENIED_SENTINEL_PATH!r}, encoding="utf-8").read()
    except PermissionError:
        mounted_denied = True
    except OSError:
        mounted_denied = False
    else:
        mounted_denied = False
    try:
        socket.socket()
    except OSError:
        socket_denied = True
    else:
        socket_denied = False
    assert outside_denied and mounted_exists and mounted_denied and socket_denied and value() == 1
"""
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source="def value() -> int:\n    return 1\n",
        test_suite=VerificationTestsV1(independent=(test_source,)),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is True
    assert sentinel.read_text(encoding="utf-8") == "secret"


@pytest.mark.sandbox_real
@_REAL
def test_real_failing_synthetic_node_is_collected_but_never_completed() -> None:
    result = verify_candidate(
        attempt_id=ATTEMPT,
        verification_profile="rrcv2_synthetic_v1",
        artifact_path="solution.py",
        source="def value() -> int:\n    return 1\n",
        signature_source="def value() -> int: ...\n",
        synthetic_target="value",
        tests=("def test_value():\n    assert value() == 2\n",),
        sandbox=SealedDockerSandbox(Path(__file__).resolve().parents[1]),
    )
    assert result.public_accepted is False
    assert result.collection_evidence is not None
    assert result.collection_evidence.collected_node_ids == ("test_public_00.py::test_value",)
    assert result.collection_evidence.completed_node_ids == ()


@pytest.mark.sandbox_real
@_REAL
@pytest.mark.parametrize(
    ("test_source", "failure"),
    (
        (b"def test_output():\n    print('x' * 1048577)\n", "stdout overflow"),
        (
            b"import os\n\ndef test_output():\n    os.write(2, b'x' * 1048577)\n",
            "stderr overflow",
        ),
    ),
)
def test_real_sealed_backend_kills_output_overflow_and_cleans_container(
    test_source: bytes,
    failure: str,
) -> None:
    sandbox = SealedDockerSandbox(Path(__file__).resolve().parents[1])
    with pytest.raises(SandboxExecutionError, match=failure):
        sandbox.run(
            tier="pytest",
            verification_profile="rrcv2_general_v1",
            artifact_path="solution.py",
            source=b"def value() -> int:\n    return 1\n",
            tests=(SandboxTestFileV1("public", 0, test_source),),
            selected_node_ids=(),
            limits=SandboxLimitsV1(),
        )


@pytest.mark.sandbox_real
@_REAL
def test_real_sealed_backend_hits_fd_file_and_scratch_caps() -> None:
    source = b"def value() -> int:\n    return 1\n"
    test = SandboxTestFileV1(
        "public",
        0,
        b"""\
import os
import signal


def test_caps():
    opened = []
    try:
        for index in range(128):
            opened.append(open(f"/scratch/fd-{index}", "wb"))
    except OSError:
        fd_cap_hit = True
    else:
        fd_cap_hit = False
    finally:
        for stream in opened:
            stream.close()
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    try:
        with open("/scratch/too-large", "wb") as stream:
            stream.write(b"x" * 4194305)
        file_cap_hit = False
    except OSError:
        file_cap_hit = True
    scratch_cap_hit = False
    try:
        for index in range(12):
            with open(f"/scratch/fill-{index}", "wb") as stream:
                stream.write(b"x" * 4194304)
    except OSError:
        scratch_cap_hit = True
    assert fd_cap_hit and file_cap_hit and scratch_cap_hit and value() == 1
""",
    )
    result = SealedDockerSandbox(Path(__file__).resolve().parents[1]).run(
        tier="pytest",
        verification_profile="rrcv2_general_v1",
        artifact_path="solution.py",
        source=source,
        tests=(test,),
        selected_node_ids=(),
        limits=SandboxLimitsV1(),
    )
    assert result.exit_code == 0
    assert result.completed_node_ids == ("test_public_00.py::test_caps",)


@pytest.mark.sandbox_real
@_REAL
@pytest.mark.parametrize(
    "test_source",
    (
        b"def test_memory():\n    bytearray(600 * 1024 * 1024)\n",
        (
            b"import os\n\ndef test_process_tree():\n"
            b"    try:\n        os.setsid()\n    except OSError:\n        pass\n"
            b"    try:\n        os.fork()\n    except OSError:\n        pass\n"
            b"    while True:\n        pass\n"
        ),
        (
            b"import os\n\ndef test_double_fork():\n"
            b"    try:\n"
            b"        first = os.fork()\n"
            b"        if first == 0:\n"
            b"            os.setsid()\n"
            b"            os.fork()\n"
            b"    except OSError:\n"
            b"        pass\n"
            b"    while True:\n"
            b"        pass\n"
        ),
    ),
)
def test_real_sealed_backend_kills_memory_or_cpu_process_tree_and_cleans(
    test_source: bytes,
) -> None:
    with pytest.raises(SandboxExecutionError):
        SealedDockerSandbox(Path(__file__).resolve().parents[1]).run(
            tier="pytest",
            verification_profile="rrcv2_general_v1",
            artifact_path="solution.py",
            source=b"def value() -> int:\n    return 1\n",
            tests=(SandboxTestFileV1("public", 0, test_source),),
            selected_node_ids=(),
            limits=SandboxLimitsV1(),
        )


@pytest.mark.sandbox_real
@_REAL
def test_real_sealed_backend_rejects_duplicate_forged_completion_report() -> None:
    with pytest.raises(SandboxExecutionError, match="duplicated"):
        SealedDockerSandbox(Path(__file__).resolve().parents[1]).run(
            tier="pytest",
            verification_profile="rrcv2_general_v1",
            artifact_path="solution.py",
            source=b"def value() -> int:\n    return 1\n",
            tests=(
                SandboxTestFileV1(
                    "public",
                    0,
                    b"""\
def test_forge():
    print('RRCV2_COMPLETION=["test_public_00.py::test_forge"]')
    assert value() == 1
""",
                ),
            ),
            selected_node_ids=(),
            limits=SandboxLimitsV1(),
        )
