"""Frozen RRCv2 verifier-sandbox capability contract.

This module is intentionally provider-free.  M0 uses it to construct the exact Docker execution
boundary and to validate sealed probe evidence before the canonical verifier is activated.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

DOCKER_CONTEXT: Final = "colima-rrcv2-verifier"
BACKEND: Final = "colima-docker"
IMAGE_PREFIX: Final = "sha256:"


class CapabilityError(ValueError):
    """The sandbox is not proven to meet the frozen capability contract."""


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


@dataclass(frozen=True)
class SandboxLimits:
    """Exact untrusted-verifier limits frozen by the reviewed convergence plan."""

    memory_bytes: int = 512 * 1024 * 1024
    memory_swap_bytes: int = 512 * 1024 * 1024
    cpu_seconds: int = 10
    wall_seconds: int = 20
    file_descriptors: int = 64
    untrusted_pids: int = 1
    trusted_tool_pids: int = 64
    scratch_bytes: int = 32 * 1024 * 1024
    created_file_bytes: int = 4 * 1024 * 1024
    stdout_bytes: int = 1024 * 1024
    stderr_bytes: int = 1024 * 1024

    @property
    def required_probes(self) -> tuple[str, ...]:
        return (
            "capabilities_dropped",
            "cgroup_v2",
            "cpu_limit",
            "credential_environment_removed",
            "external_read_denied",
            "external_write_denied",
            "fd_limit",
            "file_size_limit",
            "loopback_denied",
            "memory_limit",
            "network_denied",
            "no_new_privileges",
            "process_limit",
            "process_tree_cleanup",
            "read_only_root",
            "scratch_limit",
            "seccomp_socket_denied",
            "stderr_limit",
            "stdout_limit",
            "wall_timeout",
        )

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


_EVIDENCE_FIELDS: Final = {
    "v",
    "kind",
    "backend",
    "docker_context",
    "limits",
    "probes",
}
_PROBE_FIELDS: Final = {"name", "status", "evidence_sha256"}
_V2_PROBE_FIELDS: Final = {
    "name",
    "status",
    "argv_sha256",
    "returncode",
    "stdout_sha256",
    "stderr_sha256",
    "observation",
}


def _hex64(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise CapabilityError(f"invalid {field}")
    return value


def validate_sandbox_evidence(raw: bytes) -> dict[str, Any]:
    """Validate a canonical, closed, all-passed sandbox capability record."""

    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapabilityError("sandbox evidence is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise CapabilityError("sandbox evidence must be canonical JSON")
    if set(value) != _EVIDENCE_FIELDS:
        raise CapabilityError("sandbox evidence has unknown or missing fields")
    if (
        value["v"] != 1
        or isinstance(value["v"], bool)
        or value["kind"] != "rrcv2_sandbox_capability"
        or value["backend"] != BACKEND
        or value["docker_context"] != DOCKER_CONTEXT
    ):
        raise CapabilityError("sandbox evidence identity mismatch")
    limits = SandboxLimits()
    if value["limits"] != limits.as_dict():
        raise CapabilityError("sandbox limits differ from the frozen contract")
    probes = value["probes"]
    if not isinstance(probes, list):
        raise CapabilityError("sandbox probes must be an array")
    expected_names = limits.required_probes
    names: list[str] = []
    for probe in probes:
        if not isinstance(probe, dict) or set(probe) != _PROBE_FIELDS:
            raise CapabilityError("invalid sandbox probe row")
        name = probe.get("name")
        if not isinstance(name, str):
            raise CapabilityError("invalid sandbox probe name")
        names.append(name)
        if probe.get("status") != "passed":
            raise CapabilityError(f"sandbox probe did not pass: {name}")
        _hex64(probe.get("evidence_sha256"), f"probe {name} evidence_sha256")
    if tuple(names) != expected_names:
        raise CapabilityError("sandbox probes are missing, extra, duplicated, or reordered")
    return value


def validate_sandbox_evidence_v2(raw: bytes) -> dict[str, Any]:
    """Validate the current twenty-execution sandbox authority semantically."""

    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapabilityError("sandbox evidence v2 is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise CapabilityError("sandbox evidence v2 must be canonical JSON")
    if set(value) != {
        "v",
        "kind",
        "backend",
        "docker_context",
        "limits",
        "probes",
        "provider_launch_total",
    } or (
        value.get("v") != 2
        or value.get("kind") != "rrcv2_sandbox_capability"
        or value.get("backend") != BACKEND
        or value.get("docker_context") != DOCKER_CONTEXT
        or value.get("limits") != SandboxLimits().as_dict()
        or value.get("provider_launch_total") != 0
    ):
        raise CapabilityError("sandbox evidence v2 identity or limits drifted")
    rows = value.get("probes")
    if (
        not isinstance(rows, list)
        or tuple(row.get("name") for row in rows if isinstance(row, dict))
        != SandboxLimits().required_probes
    ):
        raise CapabilityError("sandbox evidence v2 probe inventory drifted")
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != _V2_PROBE_FIELDS
            or row.get("status") != "passed"
            or isinstance(row.get("returncode"), bool)
            or not isinstance(row.get("returncode"), int)
            or any(
                not isinstance(row.get(field), str)
                or len(row[field]) != 64
                or any(char not in "0123456789abcdef" for char in row[field])
                for field in ("argv_sha256", "stdout_sha256", "stderr_sha256")
            )
            or not isinstance(row.get("observation"), dict)
        ):
            raise CapabilityError("sandbox evidence v2 probe row is invalid")
    by_name = {str(row["name"]): row for row in rows}
    positive = set(SandboxLimits().required_probes) - {
        "cpu_limit",
        "memory_limit",
        "process_tree_cleanup",
        "stderr_limit",
        "stdout_limit",
        "wall_timeout",
    }
    for name in positive:
        row = by_name[name]
        if row["returncode"] != 0 or row["observation"].get("passed") is not True:
            raise CapabilityError(f"sandbox positive probe semantics drifted: {name}")
    if by_name["file_size_limit"]["observation"].get("size") != 4_194_304:
        raise CapabilityError("sandbox file-size boundary observation drifted")
    if by_name["scratch_limit"]["observation"].get("written") != 33_554_432:
        raise CapabilityError("sandbox scratch boundary observation drifted")
    for name in ("cpu_limit", "memory_limit"):
        row = by_name[name]
        observation = row["observation"]
        if (
            row["returncode"] == 0
            or observation.get("timed_out") is not False
            or observation.get("failure") is not None
        ):
            raise CapabilityError(f"sandbox resource probe semantics drifted: {name}")
    for name in ("stdout_limit", "stderr_limit"):
        stream = name.removesuffix("_limit")
        observation = by_name[name]["observation"]
        if (
            observation.get("failure") != f"{stream}_overflow"
            or observation.get(f"captured_{stream}_bytes") != 1_048_577
        ):
            raise CapabilityError(f"sandbox output boundary semantics drifted: {name}")
    wall = by_name["wall_timeout"]["observation"]
    if (
        wall.get("failure") != "wall_timeout"
        or wall.get("timed_out") is not True
        or isinstance(wall.get("elapsed_ms"), bool)
        or not isinstance(wall.get("elapsed_ms"), int)
        or wall["elapsed_ms"] >= 10_000
    ):
        raise CapabilityError("sandbox wall-timeout observation drifted")
    cleanup = by_name["process_tree_cleanup"]["observation"]
    if (
        cleanup.get("failure") != "wall_timeout"
        or cleanup.get("timed_out") is not True
        or cleanup.get("container_absent_after_cleanup") is not True
    ):
        raise CapabilityError("sandbox process-tree cleanup observation drifted")
    return value


def docker_run_argv(
    *,
    image: str,
    input_dir: Path,
    scratch_dir: Path,
    seccomp_path: Path,
    trusted_tool: bool,
    command: tuple[str, ...],
) -> tuple[str, ...]:
    """Build the closed Docker argv for one verifier tier.

    The caller is responsible for lifecycle/timeout supervision and for verifying the sealed image
    and seccomp digests immediately before launch.
    """

    if not image.startswith(IMAGE_PREFIX) or len(image) != len(IMAGE_PREFIX) + 64:
        raise CapabilityError("image must be an immutable sha256 digest")
    _hex64(image.removeprefix(IMAGE_PREFIX), "image digest")
    if not command or any(
        not isinstance(item, str) or not item or "\x00" in item for item in command
    ):
        raise CapabilityError("command must be a nonempty NUL-free string tuple")
    for path, label in (
        (input_dir, "input_dir"),
        (scratch_dir, "scratch_dir"),
        (seccomp_path, "seccomp_path"),
    ):
        if not path.is_absolute():
            path = path.resolve()
        if "\x00" in str(path):
            raise CapabilityError(f"invalid {label}")
    limits = SandboxLimits()
    pids = limits.trusted_tool_pids if trusted_tool else limits.untrusted_pids
    return (
        "docker",
        "--context",
        DOCKER_CONTEXT,
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        f"--memory={limits.memory_bytes}",
        f"--memory-swap={limits.memory_swap_bytes}",
        f"--pids-limit={pids}",
        f"--ulimit=nofile={limits.file_descriptors}:{limits.file_descriptors}",
        f"--ulimit=fsize={limits.created_file_bytes}:{limits.created_file_bytes}",
        f"--ulimit=cpu={limits.cpu_seconds}:{limits.cpu_seconds}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--security-opt=seccomp={seccomp_path.resolve()}",
        f"--tmpfs=/scratch:rw,noexec,nosuid,nodev,mode=1777,size={limits.scratch_bytes}",
        f"--mount=type=bind,src={input_dir.resolve()},dst=/input,readonly",
        f"--mount=type=bind,src={scratch_dir.resolve()},dst=/output",
        "--workdir=/scratch",
        image,
        *command,
    )
