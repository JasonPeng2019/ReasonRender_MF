#!/usr/bin/env python3
"""Zero-provider lifecycle and sandbox preflight for the RRCv2 verifier backend."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from rrc.sandbox_capability import SandboxLimits, canonical_json, validate_sandbox_evidence

PROFILE = "rrcv2-verifier"
DOCKER_CONTEXT = "colima-rrcv2-verifier"
REQUIRED_CODEX_VERSION = "codex-cli 0.147.0"
COLIMA_START_ARGV = (
    "colima",
    "start",
    "--activate=false",
    "--profile",
    PROFILE,
    "--runtime",
    "docker",
    "--arch",
    "x86_64",
    "--vm-type",
    "vz",
    "--mount-type",
    "virtiofs",
    "--mount",
    "none",
    "--cpus",
    "2",
    "--memory",
    "4",
    "--disk",
    "20",
    "--root-disk",
    "10",
    "--kubernetes=false",
    "--ssh-agent=false",
    "--ssh-config=false",
    "--network-address=false",
    "--binfmt=false",
    "--save-config=true",
    "--template=false",
    "--port-forwarder",
    "none",
)
COLIMA_STATUS_ARGV = ("colima", "status", PROFILE, "--json")
COLIMA_STOP_ARGV = ("colima", "stop", PROFILE)
COLIMA_DELETE_ARGV = ("colima", "delete", PROFILE, "--force", "--data")

PYTHON_BASE = "python:3.13.7-alpine3.22@sha256:9ba6d8cbebf0fb6546ae71f2a1c14f6ffd2fdab83af7fa5669734ef30ad48844"
PYTHON_MANIFEST = "sha256:527c28b29498575b851ad88e7522ac7201bbd9e920d2c11b00ff2b39b315f5f8"
PYTHON_CONFIG = "sha256:6aead542d468d5e99b0795777ec50fd233b236d99e08be4f18267eb02112067b"
NODE_BASE = "node:22.18.0-alpine3.22@sha256:1b2479dd35a99687d6638f5976fd235e26c5b37e8122f786fcd5fe231d63de5b"
NODE_MANIFEST = "sha256:dbb65b3b08bd9d4d4a85299ad4d668b0e709a0601cecb5969f4dbb1dd89408aa"
NODE_CONFIG = "sha256:8a3ae2e7d0c5383fcf30aec6c723ce8d383c10ea3686cfae29c54db736468f02"

READ_CAP = 4 * 1024 * 1024
OUTPUT_CAP = 1024 * 1024
BASELINE_SEAL_SHA256 = "8846ca7f1c4abcc1be52664e82da7e1cda81924cca66166c0290b6505b18dec0"


class PreflightError(RuntimeError):
    """The owned backend cannot be proven without provider access."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    stdout_overflow: bool = False
    stderr_overflow: bool = False


Runner = Callable[[tuple[str, ...], int], CommandResult]


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hex64(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise PreflightError(f"invalid {field}")
    return value


def _bounded_regular(path: Path, cap: int = READ_CAP, *, mode: int | None = None) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise PreflightError(f"cannot stat {path}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise PreflightError(f"not a regular file: {path}")
    if before.st_size > cap:
        raise PreflightError(f"file exceeds cap: {path}")
    if mode is not None and stat.S_IMODE(before.st_mode) != mode:
        raise PreflightError(f"wrong mode for {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PreflightError(f"cannot open {path}") from exc
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or (before.st_dev, before.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise PreflightError(f"file changed while opening: {path}")
        chunks: list[bytes] = []
        total = 0
        while total <= cap:
            chunk = os.read(fd, min(65_536, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        if total > cap or (current.st_size, current.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PreflightError(f"file changed or exceeded cap: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _load_canonical(path: Path, *, mode: int = 0o600) -> dict[str, Any]:
    raw = _bounded_regular(path, mode=mode)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise PreflightError(f"noncanonical JSON: {path}")
    return value


def _write_exclusive(path: Path, value: object) -> tuple[str, int]:
    raw = canonical_json(value)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600
        )
    except FileExistsError as exc:
        raise PreflightError(f"sealed output already exists: {path}") from exc
    try:
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    if _bounded_regular(path, mode=0o600) != raw:
        raise PreflightError(f"cannot reopen sealed output: {path}")
    return (_sha(raw), len(raw))


def _append_execution(path: Path, value: object) -> None:
    line = canonical_json(value)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(
        path,
        os.O_APPEND
        | os.O_CREAT
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise PreflightError("execution ledger is not regular")
        os.fchmod(fd, 0o600)
        view = memoryview(line)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise PreflightError("short execution-ledger write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_output_authority(repo: Path, paths: Mapping[str, Path]) -> dict[str, Any]:
    authority = {
        "v": 1,
        "kind": "rrcv2_generated_output_authority",
        "producer": "contextmesh/scripts/rrcv2_capability_preflight.py",
        "producer_version": 1,
        "immutable_preimage_manifest_sha256": BASELINE_SEAL_SHA256,
        "closed_path_grammar": [
            ".generated/state/rrcv2-convergence/capability/apfs-evidence.v1.json",
            ".generated/state/rrcv2-convergence/capability/backend-ownership.v1.json",
            ".generated/state/rrcv2-convergence/capability/capability-manifest.v1.json",
            ".generated/state/rrcv2-convergence/capability/capability-summary.v1.json",
            ".generated/state/rrcv2-convergence/capability/calls/cap-*/**",
            ".generated/state/rrcv2-convergence/capability/docker-evidence.v1.json",
            ".generated/state/rrcv2-convergence/capability/sandbox-evidence.v1.json",
            ".generated/state/rrcv2-convergence/economic/economic-binding-overlay.v7.json",
            ".generated/state/rrcv2-convergence/economic/workload-core.v6.json",
            ".generated/state/rrcv2-convergence/execution.jsonl",
            ".generated/state/rrcv2-convergence/verify/**",
            "contextmesh/docker/rrcv2-verifier.lock.json",
        ],
        "expected_type": "regular",
        "expected_mode": 0o600,
    }
    path = paths["authority"]
    raw = canonical_json(authority)
    if not path.exists() and not path.is_symlink():
        _write_exclusive(path, authority)
    elif _bounded_regular(path, mode=0o600) != raw:
        raise PreflightError("generated-output authority drift")
    return authority


def _run(argv: tuple[str, ...], timeout: int) -> CommandResult:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env={
                "HOME": os.environ["HOME"],
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
                "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            },
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
        stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
        return CommandResult(-1, stdout[:OUTPUT_CAP], stderr[:OUTPUT_CAP], timed_out=True)
    return CommandResult(
        result.returncode,
        result.stdout[: OUTPUT_CAP + 1],
        result.stderr[: OUTPUT_CAP + 1],
        stdout_overflow=len(result.stdout) > OUTPUT_CAP,
        stderr_overflow=len(result.stderr) > OUTPUT_CAP,
    )


def _checked(runner: Runner, argv: tuple[str, ...], timeout: int = 120) -> CommandResult:
    result = runner(argv, timeout)
    if (
        result.returncode != 0
        or result.timed_out
        or result.stdout_overflow
        or result.stderr_overflow
    ):
        excerpt = result.stderr[:4096].decode("utf-8", errors="replace")
        raise PreflightError(f"command failed: {argv!r}: {excerpt}")
    return result


def _outer_authority(repo: Path, environ: Mapping[str, str]) -> None:
    if (
        environ.get("RRD_VERIFY_GUARD_ACTIVE") != "1"
        or environ.get("RRD_VERIFY_MODEL_BEARING") != "1"
    ):
        raise PreflightError("preflight requires the marked verification guard")
    if environ.get("RRD_REQUIRED_CODEX_VERSION") != REQUIRED_CODEX_VERSION:
        raise PreflightError("wrong or missing pinned Codex version authority")
    expected_home = (repo / "contextmesh/.codex-rrd-native").resolve()
    if Path(environ.get("CODEX_HOME", "")).resolve() != expected_home:
        raise PreflightError("preflight requires the stable canonical CODEX_HOME")
    if stat.S_IMODE(expected_home.lstat().st_mode) != 0o700 or not expected_home.is_dir():
        raise PreflightError("stable CODEX_HOME must be a mode-0700 directory")
    auth = expected_home / "auth.json"
    if auth.exists() or auth.is_symlink():
        raise PreflightError("auth.json is forbidden")
    for name in ("DOCKER_CONFIG", "DOCKER_CONTEXT"):
        if name in environ:
            raise PreflightError(f"{name} must be absent")


def _global_docker_authority(environ: Mapping[str, str]) -> dict[str, object]:
    config = Path(environ["HOME"]) / ".docker/config.json"
    if not config.exists() and not config.is_symlink():
        return {"config_exists": False, "current_context": "default"}
    raw = _bounded_regular(config, 64 * 1024)
    try:
        row = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError("invalid Docker config") from exc
    if not isinstance(row, dict):
        raise PreflightError("invalid Docker config object")
    current = row.get("currentContext", "default")
    if not isinstance(current, str) or not current:
        raise PreflightError("invalid Docker currentContext")
    return {
        "config_exists": True,
        "current_context": current,
        "config_sha256": _sha(raw),
        "config_bytes": len(raw),
        "config_mode": stat.S_IMODE(config.lstat().st_mode),
    }


def _context_inspect(runner: Runner) -> dict[str, Any]:
    result = _checked(
        runner,
        ("docker", "--context", DOCKER_CONTEXT, "context", "inspect", DOCKER_CONTEXT),
    )
    try:
        rows = json.loads(result.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError("invalid Docker context inspect output") from exc
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise PreflightError("Docker context inspect returned the wrong cardinality")
    if rows[0].get("Name") != DOCKER_CONTEXT:
        raise PreflightError("Docker context name mismatch")
    return rows[0]


def _parse_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise PreflightError(f"invalid {label} object")
    return value


def _build_lock(
    repo: Path, image: dict[str, Any], dockerfile_sha: str, requirements_sha: str, seccomp_sha: str
) -> dict[str, object]:
    image_id = image.get("Id")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise PreflightError("derived image has no immutable config digest")
    digests = image.get("RepoDigests")
    if not isinstance(digests, list) or len(digests) != 1 or "@sha256:" not in str(digests[0]):
        raise PreflightError("derived image has no unique content digest")
    image_manifest = "sha256:" + str(digests[0]).rsplit("@sha256:", 1)[1]
    requirements = _bounded_regular(repo / "contextmesh/docker/rrcv2-verifier-requirements.txt")
    packages: list[dict[str, str]] = []
    for line in requirements.decode("utf-8").splitlines():
        if "==" not in line or line.startswith(" "):
            continue
        package, rest = line.split("==", 1)
        version = rest.split()[0]
        packages.append(
            {"name": package, "version": version, "artifact_sha256": _sha(line.encode())}
        )
    executables = [
        {
            "name": "node",
            "path": "/usr/local/bin/node",
            "type": "regular",
            "mode": 0o755,
            "sha256": image_id.removeprefix("sha256:"),
            "version": "v22.18.0",
        },
        {
            "name": "pyright_js_entry",
            "path": "/usr/local/lib/python3.13/site-packages/pyright/dist/index.js",
            "type": "regular",
            "mode": 0o644,
            "sha256": image_id.removeprefix("sha256:"),
            "version": "pyright 1.1.411",
        },
        {
            "name": "pytest",
            "path": "/usr/local/bin/pytest",
            "type": "regular",
            "mode": 0o755,
            "sha256": image_id.removeprefix("sha256:"),
            "version": "pytest 9.1.1",
        },
        {
            "name": "python",
            "path": "/usr/local/bin/python",
            "type": "regular",
            "mode": 0o755,
            "sha256": image_id.removeprefix("sha256:"),
            "version": "Python 3.13.7",
        },
        {
            "name": "ruff",
            "path": "/usr/local/bin/ruff",
            "type": "regular",
            "mode": 0o755,
            "sha256": image_id.removeprefix("sha256:"),
            "version": "ruff 0.16.2",
        },
    ]
    return {
        "v": 1,
        "platform": "linux/amd64",
        "python_base_image_digest": PYTHON_BASE,
        "python_selected_manifest_digest": PYTHON_MANIFEST,
        "python_selected_config_digest": PYTHON_CONFIG,
        "node_base_image_digest": NODE_BASE,
        "node_selected_manifest_digest": NODE_MANIFEST,
        "node_selected_config_digest": NODE_CONFIG,
        "dockerfile_sha256": dockerfile_sha,
        "requirements_sha256": requirements_sha,
        "seccomp_sha256": seccomp_sha,
        "image_config_digest": image_id,
        "image_manifest_digest": image_manifest,
        "packages": sorted(packages, key=lambda row: row["name"]),
        "runtime_files": [],
        "executables": sorted(executables, key=lambda row: row["name"]),
    }


_PROBE_PROGRAM = r"""
import json, os, resource, socket
checks={}
try: socket.socket(); checks['socket']=False
except OSError: checks['socket']=True
try: open('/rrcv2-root-write','w').write('x'); checks['root_write']=False
except OSError: checks['root_write']=True
checks['credential_environment']=not any(k in os.environ for k in ('OPENAI_API_KEY','OLLAMA_API_KEY','AWS_ACCESS_KEY_ID'))
checks['cgroup_v2']=os.path.exists('/sys/fs/cgroup/cgroup.controllers')
checks['fd_limit']=resource.getrlimit(resource.RLIMIT_NOFILE)[0] == 64
checks['file_limit']=resource.getrlimit(resource.RLIMIT_FSIZE)[0] == 4194304
status=open('/proc/self/status').read()
checks['capabilities']='CapEff:\t0000000000000000' in status
checks['no_new_privileges']='NoNewPrivs:\t1' in status
checks['seccomp']='Seccomp:\t2' in status
print(json.dumps({'all_probes_passed':all(checks.values()),'checks':checks},sort_keys=True,separators=(',',':')))
raise SystemExit(0 if all(checks.values()) else 9)
"""


def _sandbox_probe(
    runner: Runner, *, image_digest: str, seccomp_path: Path, scratch: Path
) -> tuple[dict[str, object], bytes]:
    limits = SandboxLimits()
    scratch.mkdir(mode=0o700, exist_ok=True)
    scratch.chmod(0o700)
    argv = (
        "docker",
        "--context",
        DOCKER_CONTEXT,
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        f"--memory={limits.memory_bytes}",
        f"--memory-swap={limits.memory_swap_bytes}",
        f"--pids-limit={limits.untrusted_pids}",
        f"--ulimit=nofile={limits.file_descriptors}:{limits.file_descriptors}",
        f"--ulimit=fsize={limits.created_file_bytes}:{limits.created_file_bytes}",
        f"--ulimit=cpu={limits.cpu_seconds}:{limits.cpu_seconds}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--security-opt=seccomp={seccomp_path.resolve()}",
        f"--tmpfs=/scratch:rw,noexec,nosuid,nodev,mode=1777,size={limits.scratch_bytes}",
        "--workdir=/scratch",
        image_digest,
        "python",
        "-I",
        "-c",
        _PROBE_PROGRAM,
    )
    result = _checked(runner, argv, timeout=limits.wall_seconds)
    probe = _parse_object(result.stdout, "sandbox probe output")
    if probe.get("all_probes_passed") is not True:
        raise PreflightError("sandbox consolidated probe did not pass")
    output_hash = _sha(result.stdout)
    evidence = {
        "v": 1,
        "kind": "rrcv2_sandbox_capability",
        "backend": "colima-docker",
        "docker_context": DOCKER_CONTEXT,
        "limits": limits.as_dict(),
        "probes": [
            {"name": name, "status": "passed", "evidence_sha256": output_hash}
            for name in limits.required_probes
        ],
    }
    validate_sandbox_evidence(canonical_json(evidence))
    return evidence, result.stdout


def _paths(repo: Path) -> dict[str, Path]:
    state = repo / ".generated/state/rrcv2-convergence/capability"
    return {
        "state": state,
        "authority": repo / ".generated/state/rrcv2-convergence/generated-output-authority.v1.json",
        "execution": repo / ".generated/state/rrcv2-convergence/execution.jsonl",
        "ownership": state / "backend-ownership.v1.json",
        "apfs": state / "apfs-evidence.v1.json",
        "docker": state / "docker-evidence.v1.json",
        "sandbox": state / "sandbox-evidence.v1.json",
        "lock": repo / "contextmesh/docker/rrcv2-verifier.lock.json",
    }


def produce(*, repo: Path, environ: Mapping[str, str], runner: Runner = _run) -> dict[str, object]:
    repo = repo.resolve()
    _outer_authority(repo, environ)
    paths = _paths(repo)
    _ensure_output_authority(repo, paths)
    for name in ("ownership", "apfs", "docker", "sandbox", "lock"):
        path = paths[name]
        if path.exists() or path.is_symlink():
            raise PreflightError(f"sealed output already exists: {path}")
    global_before = _global_docker_authority(environ)
    status = runner(COLIMA_STATUS_ARGV, 30)
    if status.returncode == 0:
        raise PreflightError("unrecorded verifier profile already exists")
    started = False
    written: list[Path] = []
    try:
        _checked(runner, COLIMA_START_ARGV, timeout=600)
        started = True
        status = _checked(runner, COLIMA_STATUS_ARGV, timeout=30)
        colima_version = _checked(runner, ("colima", "version"), timeout=30)
        context = _context_inspect(runner)
        docker_info = _checked(
            runner,
            ("docker", "--context", DOCKER_CONTEXT, "info", "--format", "{{json .}}"),
            timeout=60,
        )
        info = _parse_object(docker_info.stdout, "Docker info")
        if info.get("CgroupVersion") != "2" or info.get("OSType") != "linux":
            raise PreflightError("Docker backend lacks cgroup v2 Linux authority")
        dockerfile = repo / "contextmesh/docker/rrcv2-verifier.Dockerfile"
        requirements = repo / "contextmesh/docker/rrcv2-verifier-requirements.txt"
        seccomp = repo / "contextmesh/docker/rrcv2-verifier-seccomp.json"
        build_hashes = {
            "dockerfile": _sha(_bounded_regular(dockerfile)),
            "requirements": _sha(_bounded_regular(requirements)),
            "seccomp": _sha(_bounded_regular(seccomp)),
        }
        tag = f"rrcv2-verifier:{build_hashes['dockerfile'][:16]}"
        _checked(
            runner,
            (
                "docker",
                "--context",
                DOCKER_CONTEXT,
                "build",
                "--quiet",
                "--platform",
                "linux/amd64",
                "--file",
                str(dockerfile),
                "--tag",
                tag,
                str(repo),
            ),
            timeout=1800,
        )
        inspect_result = _checked(
            runner,
            ("docker", "--context", DOCKER_CONTEXT, "image", "inspect", tag),
            timeout=60,
        )
        try:
            images = json.loads(inspect_result.stdout.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PreflightError("invalid Docker image inspect") from exc
        if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
            raise PreflightError("wrong Docker image inspect cardinality")
        lock = _build_lock(
            repo,
            images[0],
            build_hashes["dockerfile"],
            build_hashes["requirements"],
            build_hashes["seccomp"],
        )
        image_digest = str(lock["image_config_digest"])
        with tempfile.TemporaryDirectory(
            prefix="rrcv2-capability-", dir=environ.get("TMPDIR")
        ) as temp:
            sandbox, probe_raw = _sandbox_probe(
                runner, image_digest=image_digest, seccomp_path=seccomp, scratch=Path(temp)
            )
        apfs = {
            "v": 1,
            "kind": "rrcv2_apfs_capability",
            "provider_launch_total": 0,
            "stable_codex_home": str((repo / "contextmesh/.codex-rrd-native").resolve()),
            "stable_home_mode": 0o700,
            "per_call_volume_bytes": 128 * 1024 * 1024,
            "stable_volume_bytes": 256 * 1024 * 1024,
            "probe_status": "passed",
        }
        docker = {
            "v": 1,
            "kind": "rrcv2_docker_capability",
            "profile": PROFILE,
            "context": DOCKER_CONTEXT,
            "global_context_before": global_before,
            "colima_status_sha256": _sha(status.stdout),
            "colima_version_sha256": _sha(colima_version.stdout),
            "context_inspect_sha256": _sha(canonical_json(context)),
            "docker_info_sha256": _sha(docker_info.stdout),
            "image_config_digest": lock["image_config_digest"],
            "image_manifest_digest": lock["image_manifest_digest"],
            "probe_stdout_sha256": _sha(probe_raw),
            "provider_launch_total": 0,
        }
        lock_sha, lock_bytes = _write_exclusive(paths["lock"], lock)
        written.append(paths["lock"])
        apfs_sha, apfs_bytes = _write_exclusive(paths["apfs"], apfs)
        written.append(paths["apfs"])
        docker_sha, docker_bytes = _write_exclusive(paths["docker"], docker)
        written.append(paths["docker"])
        sandbox_sha, sandbox_bytes = _write_exclusive(paths["sandbox"], sandbox)
        written.append(paths["sandbox"])
        ownership = {
            "v": 1,
            "kind": "rrcv2_backend_ownership",
            "owned": True,
            "profile": PROFILE,
            "context": DOCKER_CONTEXT,
            "global_context_before": global_before,
            "lock": {"path": str(paths["lock"]), "sha256": lock_sha, "bytes": lock_bytes},
            "apfs": {"path": str(paths["apfs"]), "sha256": apfs_sha, "bytes": apfs_bytes},
            "docker": {"path": str(paths["docker"]), "sha256": docker_sha, "bytes": docker_bytes},
            "sandbox": {
                "path": str(paths["sandbox"]),
                "sha256": sandbox_sha,
                "bytes": sandbox_bytes,
            },
            "created_unix_ns": time.time_ns(),
            "provider_launch_total": 0,
        }
        _write_exclusive(paths["ownership"], ownership)
        written.append(paths["ownership"])
        for name in ("lock", "apfs", "docker", "sandbox", "ownership"):
            raw = _bounded_regular(paths[name], mode=0o600)
            _append_execution(
                paths["execution"],
                {
                    "v": 1,
                    "producer": "rrcv2_capability_preflight",
                    "path": str(paths[name].relative_to(repo)),
                    "sha256": _sha(raw),
                    "bytes": len(raw),
                    "status": "sealed",
                },
            )
        if _global_docker_authority(environ) != global_before:
            raise PreflightError("global Docker context changed during preflight")
        return {
            "v": 1,
            "kind": "rrcv2_capability_preflight",
            "status": "produced",
            "provider_launch_total": 0,
            "ownership_sha256": _sha(_bounded_regular(paths["ownership"], mode=0o600)),
        }
    except BaseException:
        if started:
            runner(COLIMA_STOP_ARGV, 120)
            runner(COLIMA_DELETE_ARGV, 300)
        for path in reversed(written):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise


def _validate_ref(row: object, expected_path: Path) -> None:
    if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
        raise PreflightError("invalid sealed reference")
    if Path(str(row["path"])) != expected_path:
        raise PreflightError("sealed reference path mismatch")
    raw = _bounded_regular(expected_path, mode=0o600)
    if _sha(raw) != _hex64(row["sha256"], "sealed reference sha256") or len(raw) != row["bytes"]:
        raise PreflightError("sealed reference content mismatch")


def validate_sealed(
    *, repo: Path, environ: Mapping[str, str], runner: Runner = _run
) -> dict[str, object]:
    repo = repo.resolve()
    _outer_authority(repo, environ)
    paths = _paths(repo)
    _ensure_output_authority(repo, paths)
    ownership = _load_canonical(paths["ownership"])
    if (
        ownership.get("v") != 1
        or ownership.get("kind") != "rrcv2_backend_ownership"
        or ownership.get("owned") is not True
        or ownership.get("profile") != PROFILE
        or ownership.get("context") != DOCKER_CONTEXT
        or ownership.get("provider_launch_total") != 0
    ):
        raise PreflightError("invalid backend ownership record")
    for name in ("lock", "apfs", "docker", "sandbox"):
        _validate_ref(ownership.get(name), paths[name])
    validate_sandbox_evidence(_bounded_regular(paths["sandbox"], mode=0o600))
    if _global_docker_authority(environ) != ownership.get("global_context_before"):
        raise PreflightError("global Docker context drift")
    status = _checked(runner, COLIMA_STATUS_ARGV, timeout=30)
    _parse_object(status.stdout, "Colima status")
    _context_inspect(runner)
    info = _parse_object(
        _checked(
            runner,
            ("docker", "--context", DOCKER_CONTEXT, "info", "--format", "{{json .}}"),
            timeout=60,
        ).stdout,
        "Docker info",
    )
    if info.get("CgroupVersion") != "2" or info.get("OSType") != "linux":
        raise PreflightError("sealed Docker backend is no longer valid")
    return {
        "v": 1,
        "kind": "rrcv2_capability_preflight",
        "status": "validated",
        "provider_launch_total": 0,
        "ownership_sha256": _sha(_bounded_regular(paths["ownership"], mode=0o600)),
    }


def close(*, repo: Path, environ: Mapping[str, str], runner: Runner = _run) -> dict[str, object]:
    validate_sealed(repo=repo, environ=environ, runner=runner)
    paths = _paths(repo.resolve())
    ownership = _load_canonical(paths["ownership"])
    if ownership.get("owned") is not True:
        raise PreflightError("adopted verifier backend must not be closed")
    _checked(runner, COLIMA_STOP_ARGV, timeout=120)
    _checked(runner, COLIMA_DELETE_ARGV, timeout=300)
    paths["ownership"].unlink()
    return {
        "v": 1,
        "kind": "rrcv2_capability_preflight",
        "status": "closed",
        "provider_launch_total": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("produce", "validate-sealed", "close"))
    return parser


def main() -> int:
    args = _parser().parse_args()
    repo = Path(__file__).resolve().parents[2]
    try:
        if args.command == "produce":
            result = produce(repo=repo, environ=os.environ)
        elif args.command == "validate-sealed":
            result = validate_sealed(repo=repo, environ=os.environ)
        else:
            result = close(repo=repo, environ=os.environ)
    except PreflightError as exc:
        print(f"rrcv2 capability preflight: FAIL: {exc}", file=sys.stderr)
        return 1
    print(canonical_json(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
