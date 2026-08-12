from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from contextmesh.scripts.rrcv2_sandbox_probe_v2 import ProbeError
from contextmesh.scripts.rrcv2_sandbox_probe_v2 import validate as validate_v2
from rrc.sandbox_capability import (
    CapabilityError,
    SandboxLimits,
    canonical_json,
    docker_run_argv,
    validate_sandbox_evidence,
)


def _evidence() -> dict[str, object]:
    limits = SandboxLimits()
    return {
        "v": 1,
        "kind": "rrcv2_sandbox_capability",
        "backend": "colima-docker",
        "docker_context": "colima-rrcv2-verifier",
        "limits": limits.as_dict(),
        "probes": [
            {"name": name, "status": "passed", "evidence_sha256": "a" * 64}
            for name in limits.required_probes
        ],
    }


def test_frozen_limits_and_untrusted_docker_argv(tmp_path: Path) -> None:
    limits = SandboxLimits()
    seccomp = tmp_path / "seccomp.json"
    seccomp.write_text("{}\n")
    argv = docker_run_argv(
        image="sha256:" + "b" * 64,
        input_dir=tmp_path / "input",
        scratch_dir=tmp_path / "scratch",
        seccomp_path=seccomp,
        trusted_tool=False,
        command=("python", "/input/runner.py"),
    )

    assert argv[:3] == ("docker", "--context", "colima-rrcv2-verifier")
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--memory=536870912" in argv
    assert "--memory-swap=536870912" in argv
    assert "--pids-limit=1" in argv
    assert "--ulimit=nofile=64:64" in argv
    assert "--ulimit=fsize=4194304:4194304" in argv
    assert "--ulimit=cpu=10:10" in argv
    assert "--tmpfs=/scratch:rw,noexec,nosuid,nodev,mode=1777,size=33554432" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert f"--security-opt=seccomp={seccomp.resolve()}" in argv
    assert argv[-2:] == ("python", "/input/runner.py")
    assert limits.stdout_bytes == limits.stderr_bytes == 1_048_576


def test_trusted_tool_uses_only_the_larger_frozen_pid_tier(tmp_path: Path) -> None:
    argv = docker_run_argv(
        image="sha256:" + "b" * 64,
        input_dir=tmp_path / "input",
        scratch_dir=tmp_path / "scratch",
        seccomp_path=tmp_path / "seccomp.json",
        trusted_tool=True,
        command=("/usr/local/bin/ruff", "--version"),
    )
    assert "--pids-limit=64" in argv
    assert "--pids-limit=1" not in argv


def test_sandbox_evidence_is_closed_canonical_and_all_passed() -> None:
    evidence = _evidence()
    raw = canonical_json(evidence)
    assert validate_sandbox_evidence(raw) == evidence

    for mutation in (
        lambda row: row.update(extra=True),
        lambda row: row["probes"].pop(),  # type: ignore[union-attr]
        lambda row: row["probes"][0].update(status="skipped"),  # type: ignore[index,union-attr]
        lambda row: row["limits"].update(memory_bytes=1),  # type: ignore[union-attr]
        lambda row: row.update(docker_context="default"),
    ):
        changed = json.loads(raw)
        mutation(changed)
        with pytest.raises(CapabilityError):
            validate_sandbox_evidence(canonical_json(changed))


def test_noncanonical_sandbox_evidence_rejects() -> None:
    raw = json.dumps(_evidence(), indent=2).encode() + b"\n"
    with pytest.raises(CapabilityError, match="canonical"):
        validate_sandbox_evidence(raw)


def test_real_v2_probe_rows_are_individual_and_semantically_closed(tmp_path: Path) -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / ".generated/state/rrcv2-convergence/verify/sandbox-evidence.v2.json"
    )
    value = json.loads(source.read_text())
    target = tmp_path / ".generated/state/rrcv2-convergence/verify/sandbox-evidence.v2.json"
    target.parent.mkdir(parents=True)

    def write(row: object) -> None:
        target.write_bytes(canonical_json(row))
        target.chmod(0o600)

    write(value)
    assert validate_v2(tmp_path) == value
    assert len({row["argv_sha256"] for row in value["probes"]}) == 20

    mutations = []
    changed = copy.deepcopy(value)
    changed["probes"][0]["status"] = "skipped"
    mutations.append(changed)
    changed = copy.deepcopy(value)
    next(row for row in changed["probes"] if row["name"] == "scratch_limit")["observation"][
        "written"
    ] = 0
    mutations.append(changed)
    changed = copy.deepcopy(value)
    next(row for row in changed["probes"] if row["name"] == "stdout_limit")["observation"][
        "failure"
    ] = None
    mutations.append(changed)
    changed = copy.deepcopy(value)
    changed["probes"].pop()
    mutations.append(changed)
    for changed in mutations:
        write(changed)
        with pytest.raises(ProbeError):
            validate_v2(tmp_path)
