#!/usr/bin/env python3
"""Produce or validate the functional-only RRCv2 economic binding authorities."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path

from rrc.dispatch_permit import (
    AuthorityRef,
    canonical_json,
    generated_output_authority_v2,
    generated_output_authority_v3,
    product_config_manifest,
    read_authority,
)
from rrc.economic_authority import (
    economic_binding_overlay_v10,
    setup_accounting_v1,
    setup_accounting_v2,
    validate_economic_binding_overlay_v10,
    validate_workload_core_v6,
    worker_context_attestation_v1,
    workload_core_v6,
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _ref(path: Path) -> AuthorityRef:
    raw = path.read_bytes()
    return AuthorityRef(path=path, sha256=_sha(raw), bytes=len(raw))


def _atomic_0600(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp.unlink(missing_ok=True)


def _paths(repo: Path) -> dict[str, Path]:
    base = repo / ".generated/state/rrcv2-convergence"
    return {
        "workload": repo / "contextmesh/bench/rrcv2_workload.json",
        "oracles": repo / "contextmesh/bench/rrcv2_oracles.json",
        "manifest": base / "capability/capability-manifest.v1.json",
        "summary": base / "capability/capability-summary.v1.json",
        "inventory": base / "verify/capability-evidence-inventory.v1.json",
        "sandbox_v2": base / "verify/sandbox-evidence.v2.json",
        "abandoned": base / "verify/aborted-capability-attempt-1/archive-manifest.json",
        "redesign": base / "capability/explore-strong/redesign-evidence.v2.json",
        "analyzer": repo / "contextmesh/bench/rrcv2_analyzer.py",
        "economic_file": repo / "rrc/economic_authority.py",
        "permit_file": repo / "rrc/dispatch_permit.py",
        "matrix_file": repo / "contextmesh/scripts/rrcv2_capability_matrix.py",
        "sandbox_probe_file": repo / "contextmesh/scripts/rrcv2_sandbox_probe_v2.py",
        "output_authority_v1": base / "generated-output-authority.v1.json",
        "output_authority_v2": base / "verify/generated-output-authority.v2.json",
        "output_authority_v3": base / "verify/generated-output-authority.v3.json",
        "core": base / "economic/workload-core.v6.json",
        "setup": base / "verify/setup-accounting.v1.json",
        "setup_v2": base / "verify/setup-accounting.v2.json",
        "worker_attestation": base / "verify/native-worker-context-attestation.v1.json",
        "root_rollout": base / "capability/calls/cap-08-root-strong-medium-native/rollout.jsonl",
        "worker_rollout": base / "capability/calls/cap-09-worker-small-low-native/rollout.jsonl",
        "root_environment": base
        / "capability/calls/cap-08-root-strong-medium-native/environment.json",
        "worker_environment": base
        / "capability/calls/cap-09-worker-small-low-native/environment.json",
        "overlay": base / "economic/economic-binding-overlay.v10.json",
        "config": base / "verify/product-config-manifest.v15.json",
        "execution": base / "execution.jsonl",
    }


def _register_outputs(repo: Path) -> None:
    paths = _paths(repo)
    ledger = paths["execution"]
    fd = os.open(
        ledger,
        os.O_APPEND
        | os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.lseek(fd, 0, os.SEEK_SET)
        existing = {
            row.get("path")
            for line in os.read(fd, 1024 * 1024 + 1).splitlines()
            if isinstance((row := json.loads(line)), dict)
        }
        for name in (
            "output_authority_v3",
            "core",
            "setup",
            "setup_v2",
            "worker_attestation",
            "overlay",
            "config",
        ):
            path = paths[name]
            relative = str(path.relative_to(repo))
            if relative in existing:
                continue
            raw = read_authority(_ref(path))
            row = {
                "v": 1,
                "path": relative,
                "producer": "rrcv2_economic_overlay",
                "sha256": _sha(raw),
                "bytes": len(raw),
                "status": "sealed",
            }
            os.write(fd, canonical_json(row))
        os.fsync(fd)
    finally:
        os.close(fd)


def _expected(
    repo: Path,
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    paths = _paths(repo)
    workload_raw = paths["workload"].read_bytes()
    oracles_raw = paths["oracles"].read_bytes()
    workload = json.loads(workload_raw)
    core = workload_core_v6(
        source_workload_path="contextmesh/bench/rrcv2_workload.json",
        source_workload_raw=workload_raw,
        source_oracles_path="contextmesh/bench/rrcv2_oracles.json",
        source_oracles_raw=oracles_raw,
    )
    core_raw = canonical_json(core)
    summary_raw = paths["summary"].read_bytes()
    summary = json.loads(summary_raw)
    abandoned_raw = paths["abandoned"].read_bytes()
    abandoned = json.loads(abandoned_raw)
    setup = setup_accounting_v1(
        abandoned_manifest_sha256=_sha(abandoned_raw),
        abandoned_usage=abandoned["usage"],
        abandoned_rejections_without_usage=abandoned["provider_schema_rejections_without_usage"],
        capability_summary_sha256=_sha(summary_raw),
        canonical_usage=summary["results"],
    )
    output_authority = generated_output_authority_v2(
        supersedes_sha256=_sha(paths["output_authority_v1"].read_bytes())
    )
    output_authority_v3 = generated_output_authority_v3(
        supersedes_sha256=_sha(canonical_json(output_authority))
    )
    setup_v2 = setup_accounting_v2(
        abandoned_manifest_sha256=_sha(abandoned_raw),
        abandoned_usage=abandoned["usage"],
        abandoned_rejections_without_usage=abandoned["provider_schema_rejections_without_usage"],
        redesign_evidence_sha256=_sha(paths["redesign"].read_bytes()),
        redesign_usage=json.loads(paths["redesign"].read_bytes())["provider_usage"],
        capability_summary_sha256=_sha(summary_raw),
        canonical_usage=summary["results"],
    )
    worker_attestation = worker_context_attestation_v1(
        root_rollout_raw=paths["root_rollout"].read_bytes(),
        worker_rollout_raw=paths["worker_rollout"].read_bytes(),
        root_environment_raw=paths["root_environment"].read_bytes(),
        worker_environment_raw=paths["worker_environment"].read_bytes(),
    )
    overlay = economic_binding_overlay_v10(
        workload_core_sha256=_sha(core_raw),
        source_workload_sha256=_sha(workload_raw),
        source_oracles_sha256=_sha(oracles_raw),
        capability_manifest_sha256=_sha(paths["manifest"].read_bytes()),
        capability_summary_sha256=_sha(summary_raw),
        capability_evidence_inventory_sha256=_sha(paths["inventory"].read_bytes()),
        sandbox_evidence_v2_sha256=_sha(paths["sandbox_v2"].read_bytes()),
        setup_accounting_sha256=_sha(canonical_json(setup)),
        generated_output_authority_v2_sha256=_sha(canonical_json(output_authority)),
        redesign_evidence_sha256=_sha(paths["redesign"].read_bytes()),
        rate_payload_sha256=workload["economic_evidence"]["rate_payload_sha256"],
        analyzer_file_sha256=_sha(paths["analyzer"].read_bytes()),
        analysis_sha256=workload["protocol"]["analysis_sha256"],
        setup_accounting_v2_sha256=_sha(canonical_json(setup_v2)),
        worker_context_attestation_sha256=_sha(canonical_json(worker_attestation)),
        generated_output_authority_v3_sha256=_sha(canonical_json(output_authority_v3)),
    )
    config = product_config_manifest(
        economic_authority_file_sha256=_sha(paths["economic_file"].read_bytes()),
        dispatch_permit_file_sha256=_sha(paths["permit_file"].read_bytes()),
        capability_matrix_file_sha256=_sha(paths["matrix_file"].read_bytes()),
        sandbox_probe_file_sha256=_sha(paths["sandbox_probe_file"].read_bytes()),
    )
    return (
        output_authority_v3,
        core,
        setup,
        setup_v2,
        worker_attestation,
        overlay,
        config,
    )


def produce(repo: Path) -> dict[str, object]:
    paths = _paths(repo)
    expected = _expected(repo)
    for name, value in zip(
        (
            "output_authority_v3",
            "core",
            "setup",
            "setup_v2",
            "worker_attestation",
            "overlay",
            "config",
        ),
        expected,
        strict=True,
    ):
        path = paths[name]
        raw = canonical_json(value)
        if path.exists() and read_authority(_ref(path)) != raw:
            raise RuntimeError(f"refusing to replace mismatched authority: {path}")
        if not path.exists():
            _atomic_0600(path, raw)
    _register_outputs(repo)
    return validate(repo)


def validate(repo: Path) -> dict[str, object]:
    paths = _paths(repo)
    (
        expected_output_authority_v3,
        expected_core,
        expected_setup,
        expected_setup_v2,
        expected_worker_attestation,
        expected_overlay,
        expected_config,
    ) = _expected(repo)
    output_authority_raw = read_authority(_ref(paths["output_authority_v3"]))
    core_raw = read_authority(_ref(paths["core"]))
    overlay_raw = read_authority(_ref(paths["overlay"]))
    setup_raw = read_authority(_ref(paths["setup"]))
    setup_v2_raw = read_authority(_ref(paths["setup_v2"]))
    worker_attestation_raw = read_authority(_ref(paths["worker_attestation"]))
    config_raw = read_authority(_ref(paths["config"]))
    if output_authority_raw != canonical_json(expected_output_authority_v3):
        raise RuntimeError("generated output authority v3 is stale")
    if core_raw != canonical_json(expected_core):
        raise RuntimeError("workload core is stale")
    if overlay_raw != canonical_json(expected_overlay):
        raise RuntimeError("economic binding overlay is stale")
    if setup_raw != canonical_json(expected_setup):
        raise RuntimeError("setup accounting is stale")
    if setup_v2_raw != canonical_json(expected_setup_v2):
        raise RuntimeError("setup accounting v2 is stale")
    if worker_attestation_raw != canonical_json(expected_worker_attestation):
        raise RuntimeError("worker context attestation is stale")
    if config_raw != canonical_json(expected_config):
        raise RuntimeError("product config manifest is stale")
    validate_workload_core_v6(
        expected_core,
        source_workload_raw=paths["workload"].read_bytes(),
        source_oracles_raw=paths["oracles"].read_bytes(),
    )
    validate_economic_binding_overlay_v10(
        expected_overlay,
        workload_core_ref=_ref(paths["core"]),
        source_workload_ref=_ref(paths["workload"]),
        source_oracles_ref=_ref(paths["oracles"]),
        capability_manifest_ref=_ref(paths["manifest"]),
        capability_summary_ref=_ref(paths["summary"]),
        capability_evidence_inventory_ref=_ref(paths["inventory"]),
        sandbox_evidence_v2_ref=_ref(paths["sandbox_v2"]),
        setup_accounting_ref=_ref(paths["setup"]),
        setup_accounting_v2_ref=_ref(paths["setup_v2"]),
        worker_context_attestation_ref=_ref(paths["worker_attestation"]),
        root_rollout_ref=_ref(paths["root_rollout"]),
        worker_rollout_ref=_ref(paths["worker_rollout"]),
        root_environment_ref=_ref(paths["root_environment"]),
        worker_environment_ref=_ref(paths["worker_environment"]),
        generated_output_authority_v3_ref=_ref(paths["output_authority_v3"]),
        generated_output_authority_v2_ref=_ref(paths["output_authority_v2"]),
        generated_output_authority_v1_ref=_ref(paths["output_authority_v1"]),
        abandoned_manifest_ref=_ref(paths["abandoned"]),
        redesign_evidence_ref=_ref(paths["redesign"]),
        analyzer_ref=_ref(paths["analyzer"]),
    )
    return {
        "v": 1,
        "kind": "rrcv2_economic_overlay_validation",
        "workload_core_sha256": _sha(core_raw),
        "economic_binding_overlay_sha256": _sha(overlay_raw),
        "product_config_manifest_sha256": _sha(config_raw),
        "economic_claim_eligible": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("produce", "validate-sealed"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    repo = args.repo.resolve()
    result = produce(repo) if args.mode == "produce" else validate(repo)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
