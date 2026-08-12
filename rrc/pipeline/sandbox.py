"""Fail-closed execution boundary for the RRCv2 verifier.

The verifier never executes model-authored bytes on the host.  ``SealedDockerSandbox`` consumes the
M0 capability authority and immutable image lock, materializes one bounded attempt tree, and runs a
single certified tier in the task-local Colima Docker context.  The narrow port is deliberately easy
to fake in unit tests, while the production adapter owns process supervision, output caps, timeout
cleanup, read-only mounts, and exact collection/completion evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Final, Literal, Protocol

from rrc.sandbox_capability import (
    DOCKER_CONTEXT,
    CapabilityError,
    SandboxLimits,
    validate_sandbox_evidence_v2,
)

MAX_STREAM_BYTES: Final = 1024 * 1024
MAX_AUTHORITY_BYTES: Final = 4 * 1024 * 1024
RUNTIME_LOCK_SHA256: Final = "24207fe1093a856d6f2cc688b2f75f3db08ce3ea14d2f491719d03b31a075a03"
CAPABILITY_SHA256: Final = "6380159212462ec4de123181ef8128515bcb07cfa4092c80b6754b773cf43963"
IMAGE_CONFIG_DIGEST: Final = (
    "sha256:57e2b83a26884cc0327afa06b79b1b803fd848178d841de72e63adb07dd91851"
)
PROFILE_CONFIG_SHA256: Final = "e50d6846153a5e2f0cd4894a8f75f188b11b70590d62dab49cb667bf2d6c4472"
DOCKER_INFO_STABLE_SHA256: Final = (
    "c0cc589fa2063ba7eee0f0f3d7b8a48b393f90d681ec0c430bad87f7c5927ea8"
)
DOCKER_INFO_DYNAMIC_FIELDS: Final = frozenset(
    {
        "Containers",
        "ContainersPaused",
        "ContainersRunning",
        "ContainersStopped",
        "Images",
        "NEventsListener",
        "NFd",
        "NGoroutines",
        "SystemTime",
    }
)
DOCKER_INFO_FIELDS: Final = DOCKER_INFO_DYNAMIC_FIELDS | frozenset(
    {
        "Architecture",
        "CDISpecDirs",
        "CPUSet",
        "CPUShares",
        "CgroupDriver",
        "CgroupVersion",
        "ClientInfo",
        "Containerd",
        "ContainerdCommit",
        "CpuCfsPeriod",
        "CpuCfsQuota",
        "Debug",
        "DefaultRuntime",
        "DockerRootDir",
        "Driver",
        "DriverStatus",
        "ExperimentalBuild",
        "FirewallBackend",
        "GenericResources",
        "HttpProxy",
        "HttpsProxy",
        "ID",
        "IPv4Forwarding",
        "IndexServerAddress",
        "InitBinary",
        "InitCommit",
        "Isolation",
        "KernelVersion",
        "Labels",
        "LiveRestoreEnabled",
        "LoggingDriver",
        "MemTotal",
        "MemoryLimit",
        "NCPU",
        "Name",
        "NoProxy",
        "OSType",
        "OSVersion",
        "OomKillDisable",
        "OperatingSystem",
        "PidsLimit",
        "Plugins",
        "RegistryConfig",
        "RuncCommit",
        "Runtimes",
        "SecurityOptions",
        "ServerVersion",
        "SwapLimit",
        "Swarm",
        "Warnings",
    }
)
_SMOKE_TRAMPOLINE: Final = """\
import os,signal,sys
gate=int(sys.argv[1]); request=sys.argv[2]; argv=sys.argv[3:]
signal.pthread_sigmask(signal.SIG_UNBLOCK,{signal.SIGTERM,signal.SIGINT,signal.SIGHUP})
released=os.read(gate,1); os.close(gate)
if released != b'1' or os.path.lexists(request): raise SystemExit(125)
os.execvpe(argv[0],argv,os.environ)
"""
IMAGE_LOCK_FIELDS: Final = {
    "v",
    "platform",
    "python_selected_manifest_digest",
    "python_selected_config_digest",
    "node_selected_manifest_digest",
    "node_selected_config_digest",
    "dockerfile_sha256",
    "requirements_sha256",
    "seccomp_sha256",
    "image_config_digest",
    "image_manifest_digest",
    "packages",
    "runtime_files",
    "executables",
    "environment",
    "user",
    "capability_sha256",
}
BACKEND_PROFILE: Final = "rrcv2-verifier"
DENIED_SENTINEL_PATH: Final = "/rrcv2-denied/sentinel.v1"
DENIED_SENTINEL_BYTES: Final = b"rrcv2-mounted-known-readable-denied-sentinel-v1\n"
BACKEND_OWNERSHIP_FIELDS: Final = {
    "apfs",
    "context",
    "created_unix_ns",
    "docker",
    "global_context_before",
    "kind",
    "lock",
    "owned",
    "profile",
    "provider_launch_total",
    "sandbox",
    "v",
}
BACKEND_DOCKER_FIELDS: Final = {
    "colima_status_sha256",
    "colima_version_sha256",
    "context",
    "context_inspect_sha256",
    "docker_info_sha256",
    "global_context_before",
    "image_config_digest",
    "image_manifest_digest",
    "kind",
    "probe_stdout_sha256",
    "profile",
    "provider_launch_total",
    "v",
}
Tier = Literal["ruff", "pyright", "pytest_collect", "pytest"]
Profile = Literal["rrcv2_general_v1", "rrcv2_synthetic_v1"]
TestCategory = Literal["spec", "independent", "public", "oracle"]

_IMAGE_ATTEST_SCRIPT: Final = r"""
import hashlib
import importlib.metadata
import json
import os
import stat
import subprocess

commands = {
    "node": ["/usr/local/bin/node", "--version"],
    "pyright_js_entry": [
        "/usr/local/bin/node",
        "/usr/local/lib/python3.13/site-packages/pyright/dist/index.js",
        "--version",
    ],
    "pytest": ["/usr/local/bin/pytest", "--version"],
    "python": ["/usr/local/bin/python3.13", "--version"],
    "ruff": ["/usr/local/bin/ruff", "--version"],
}
paths = {
    "node": "/usr/local/bin/node",
    "pyright_js_entry": "/usr/local/lib/python3.13/site-packages/pyright/dist/index.js",
    "pytest": "/usr/local/bin/pytest",
    "python": "/usr/local/bin/python3.13",
    "ruff": "/usr/local/bin/ruff",
}

def file_row(name, path):
    info = os.lstat(path)
    raw = open(path, "rb").read()
    return {
        "mode": stat.S_IMODE(info.st_mode),
        "name": name,
        "path": path,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "type": "regular" if stat.S_ISREG(info.st_mode) else "other",
        "version": subprocess.check_output(commands[name], text=True).strip(),
    }

runtime_paths = ("/usr/lib/libgcc_s.so.1", "/usr/lib/libstdc++.so.6")
runtime_files = []
for path in runtime_paths:
    info = os.lstat(path)
    raw = open(path, "rb").read()
    runtime_files.append({
        "mode": stat.S_IMODE(info.st_mode),
        "path": path,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "type": "regular" if stat.S_ISREG(info.st_mode) else "other",
    })

value = {
    "executables": [file_row(name, paths[name]) for name in sorted(paths)],
    "packages": sorted(
        [
            {"name": item.metadata["Name"].lower(), "version": item.version}
            for item in importlib.metadata.distributions()
        ],
        key=lambda item: (item["name"], item["version"]),
    ),
    "runtime_files": runtime_files,
}
print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
"""


class SandboxUnavailable(RuntimeError):
    """The certified backend or one of its immutable authorities is unavailable."""


class SandboxExecutionError(RuntimeError):
    """The outer supervisor could not obtain bounded, attributable execution evidence."""


@dataclass(frozen=True)
class SandboxLimitsV1:
    memory_bytes: int = 512 * 1024 * 1024
    cpu_seconds: int = 10
    wall_seconds: int = 20
    file_descriptors: int = 64
    processes: int = 1
    scratch_bytes: int = 32 * 1024 * 1024
    file_bytes: int = 4 * 1024 * 1024
    stdout_bytes: int = MAX_STREAM_BYTES
    stderr_bytes: int = MAX_STREAM_BYTES

    def __post_init__(self) -> None:
        expected = SandboxLimits()
        if (
            self.memory_bytes != expected.memory_bytes
            or self.cpu_seconds != expected.cpu_seconds
            or self.wall_seconds != expected.wall_seconds
            or self.file_descriptors != expected.file_descriptors
            or self.processes != expected.untrusted_pids
            or self.scratch_bytes != expected.scratch_bytes
            or self.file_bytes != expected.created_file_bytes
            or self.stdout_bytes != expected.stdout_bytes
            or self.stderr_bytes != expected.stderr_bytes
        ):
            raise ValueError("sandbox limits differ from the certified RRCv2 profile")


@dataclass(frozen=True)
class SandboxInvocationEvidenceV1:
    argv_sha256: str
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str
    v: int = 1


@dataclass(frozen=True)
class SandboxTierExecutionV1:
    tier: Tier
    backend_before_sha256: str
    backend_after_sha256: str
    capability_sha256: str
    runtime_lock_sha256: str
    image_config_digest: str
    invocations: tuple[SandboxInvocationEvidenceV1, ...]
    normalized_source_sha256: str | None
    v: int = 1


@dataclass(frozen=True)
class SandboxResultV1:
    tier: Tier
    exit_code: int
    stdout: bytes
    stderr: bytes
    normalized_source: bytes | None = None
    collected_node_ids: tuple[str, ...] = ()
    completed_node_ids: tuple[str, ...] = ()
    backend_attested: bool = True
    invocations: tuple[CommandObservation, ...] = ()
    execution_evidence: SandboxTierExecutionV1 | None = None


@dataclass(frozen=True)
class SandboxTestFileV1:
    category: TestCategory
    index: int
    source: bytes

    @property
    def filename(self) -> str:
        return f"test_{self.category}_{self.index:02d}.py"


class VerifierSandboxPort(Protocol):
    """Sealed execution backend consumed by :func:`verify_candidate`."""

    def run(
        self,
        *,
        tier: Tier,
        verification_profile: Profile,
        artifact_path: str,
        source: bytes,
        tests: tuple[SandboxTestFileV1, ...],
        selected_node_ids: tuple[str, ...] = (),
        limits: SandboxLimitsV1,
    ) -> SandboxResultV1:
        """Run exactly one certified tier and return bounded outer-supervisor evidence."""

        ...


@dataclass(frozen=True)
class CommandObservation:
    returncode: int
    stdout: bytes
    stderr: bytes
    argv: tuple[str, ...] = ()


class DockerSupervisorPort(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        container_name: str,
        timeout_seconds: float,
        stdout_cap: int,
        stderr_cap: int,
    ) -> CommandObservation: ...

    def attest_image(self, authority: dict[str, object], seccomp_path: Path) -> None: ...


def _docker_env() -> dict[str, str]:
    home = os.environ.get("HOME")
    if not home:
        raise SandboxUnavailable("HOME is required for the task-local Docker context")
    if "DOCKER_CONTEXT" in os.environ or "DOCKER_CONFIG" in os.environ:
        raise SandboxUnavailable("ambient Docker context/config overrides are forbidden")
    return {
        "HOME": home,
        # Keep backend observers on the capability-probed tool roots. In particular,
        # excluding /usr/sbin prevents Colima's optional macOS system_profiler probe
        # from turning backend re-attestation into an Activation-Lock network wait;
        # the owned Colima status bytes are identical with that optional probe absent.
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1.0
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _write_smoke_row(path: Path, value: dict[str, object]) -> None:
    raw = _canonical(value)
    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise SandboxExecutionError("smoke child registration write was incomplete")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)


def _smoke_registry() -> Path:
    root = Path(os.environ["RRCV2_SMOKE_CHILD_REGISTRY"])
    metadata = os.lstat(root)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
    ):
        raise SandboxExecutionError("smoke child registry is not an owned mode-0700 directory")
    return root


def _registered_popen(
    argv: tuple[str, ...],
    *,
    env: dict[str, str],
) -> tuple[subprocess.Popen[bytes], Path | None]:
    if os.environ.get("RRCV2_PRODUCT_SMOKE") != "1":
        return (
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=env,
            ),
            None,
        )
    nonce = os.environ.get("RRCV2_SMOKE_RUN_NONCE", "")
    if re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise SandboxExecutionError("smoke child nonce is invalid")
    blocked = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    gate_read, gate_write = os.pipe()
    os.set_inheritable(gate_read, True)
    process: subprocess.Popen[bytes] | None = None
    try:
        launched = (
            os.fsdecode(sys.executable),
            "-c",
            _SMOKE_TRAMPOLINE,
            str(gate_read),
            os.environ["RRCV2_SMOKE_CANCELLATION_REQUEST"],
            *argv,
        )
        process = subprocess.Popen(
            launched,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
            pass_fds=(gate_read,),
        )
        os.close(gate_read)
        gate_read = -1
        path = _smoke_registry() / f"child-{nonce}-{process.pid}-{uuid.uuid4().hex}.json"
        _write_smoke_row(
            path,
            {
                "argv_sha256": _sha(_canonical(list(argv))),
                "nonce": nonce,
                "pgid": process.pid,
                "pid": process.pid,
                "returncode": None,
                "state": "live",
                "v": 1,
            },
        )
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        os.write(gate_write, b"1")
        os.close(gate_write)
        gate_write = -1
        return process, path
    except BaseException:
        if process is not None:
            _terminate_group(process)
            process.wait(timeout=5)
        raise
    finally:
        for descriptor in (gate_read, gate_write):
            if descriptor >= 0:
                os.close(descriptor)
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def _finish_registered(path: Path | None, returncode: int) -> None:
    if path is None:
        return
    raw = _bounded_regular(path, 4096, mode=0o600)
    value = _canonical_object(raw, label="smoke child registry row")
    if value.get("state") != "live" or value.get("returncode") is not None:
        raise SandboxExecutionError("smoke child registry row is not live")
    value["state"] = "terminal"
    value["returncode"] = returncode
    temporary = path.with_name("." + path.name + ".terminal")
    _write_smoke_row(temporary, value)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _smoke_label_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    if os.environ.get("RRCV2_PRODUCT_SMOKE") != "1":
        return argv
    nonce = os.environ.get("RRCV2_SMOKE_RUN_NONCE", "")
    if re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise SandboxExecutionError("smoke Docker label nonce is invalid")
    try:
        command_index = argv.index("run")
    except ValueError:
        return argv
    return (
        *argv[: command_index + 1],
        "--label",
        f"org.contextmesh.rrcv2-smoke={nonce}",
        *argv[command_index + 1 :],
    )


class SubprocessDockerSupervisor:
    """Bounded host-side supervisor for Docker CLI and container cleanup."""

    def _remove(self, name: str) -> None:
        _docker_remove_owned("container", name)

    def _require_absent(self, name: str) -> None:
        try:
            result = subprocess.run(
                ("docker", "--context", DOCKER_CONTEXT, "inspect", name),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                env=_docker_env(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxExecutionError("container cleanup could not be proven") from exc
        if (
            result.returncode == 0
            or len(result.stdout) > 65_536
            or len(result.stderr) > 65_536
            or b"no such" not in result.stderr.lower()
        ):
            raise SandboxExecutionError("container survived its certified invocation")

    def run(
        self,
        argv: tuple[str, ...],
        *,
        container_name: str,
        timeout_seconds: float,
        stdout_cap: int,
        stderr_cap: int,
    ) -> CommandObservation:
        argv = _smoke_label_argv(argv)
        registry_path: Path | None = None
        try:
            process, registry_path = _registered_popen(argv, env=_docker_env())
        except OSError as exc:
            raise SandboxUnavailable("Docker verifier could not start") from exc
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + timeout_seconds
        failure: str | None = None
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "wall timeout"
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 65_536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    stream = str(key.data)
                    captured[stream].extend(chunk)
                    cap = stdout_cap if stream == "stdout" else stderr_cap
                    if len(captured[stream]) > cap:
                        failure = f"{stream} overflow"
                        break
                if failure is not None:
                    break
            if failure is not None:
                _terminate_group(process)
                self._remove(container_name)
            try:
                returncode = process.wait(
                    timeout=2 if failure else max(0.1, deadline - time.monotonic())
                )
            except subprocess.TimeoutExpired as exc:
                _terminate_group(process)
                self._remove(container_name)
                raise SandboxExecutionError("Docker client did not terminate") from exc
        finally:
            selector.close()
            if process.poll() is None:
                _terminate_group(process)
                self._remove(container_name)
                process.wait(timeout=2)
            if process.poll() is not None:
                _finish_registered(registry_path, process.returncode)
        self._require_absent(container_name)
        if failure is not None:
            raise SandboxExecutionError(f"sandbox {failure}")
        stdout = bytes(captured["stdout"])
        stderr = bytes(captured["stderr"])
        if returncode == 125:
            detail = stderr[:4096].decode("utf-8", errors="replace").strip()
            raise SandboxUnavailable(f"Docker rejected the certified verifier invocation: {detail}")
        return CommandObservation(returncode, stdout, stderr)

    def attest_image(self, authority: dict[str, object], seccomp_path: Path) -> None:
        image = str(authority["image_config_digest"])
        try:
            result = subprocess.run(
                (
                    "docker",
                    "--context",
                    DOCKER_CONTEXT,
                    "image",
                    "inspect",
                    image,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                env=_docker_env(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailable("verifier image attestation failed") from exc
        if result.returncode != 0 or len(result.stdout) > MAX_STREAM_BYTES:
            raise SandboxUnavailable("verifier image identity drifted")
        try:
            rows = json.loads(result.stdout.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxUnavailable("verifier image inspection is invalid") from exc
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise SandboxUnavailable("verifier image inspection cardinality drifted")
        row = rows[0]
        expected_repo_digest = "rrcv2-verifier@" + str(authority["image_manifest_digest"])
        config = row.get("Config")
        if (
            row.get("Id") != image
            or row.get("Architecture") != "amd64"
            or row.get("Os") != "linux"
            or row.get("RepoDigests") != [expected_repo_digest]
            or not isinstance(config, dict)
            or config.get("User") != authority["user"]
            or config.get("Env") != authority["environment"]
        ):
            raise SandboxUnavailable("verifier image metadata drifted")
        name = f"rrcv2-attest-{uuid.uuid4().hex}"
        frozen = SandboxLimits()
        argv = (
            "docker",
            "--context",
            DOCKER_CONTEXT,
            "run",
            "--rm",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cpus=1",
            f"--memory={frozen.memory_bytes}",
            f"--memory-swap={frozen.memory_swap_bytes}",
            f"--pids-limit={frozen.trusted_tool_pids}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--security-opt=seccomp={seccomp_path}",
            f"--tmpfs=/scratch:rw,noexec,nosuid,nodev,mode=1777,size={frozen.scratch_bytes}",
            image,
            "/usr/local/bin/python3.13",
            "-I",
            "-c",
            _IMAGE_ATTEST_SCRIPT,
        )
        observed = self.run(
            argv,
            container_name=name,
            timeout_seconds=20,
            stdout_cap=MAX_STREAM_BYTES,
            stderr_cap=MAX_STREAM_BYTES,
        )
        if observed.returncode != 0:
            raise SandboxUnavailable("in-container verifier attestation failed")
        try:
            inventory = json.loads(observed.stdout.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxUnavailable("in-container verifier inventory is invalid") from exc
        expected_inventory = {
            "executables": authority["executables"],
            "packages": authority["packages"],
            "runtime_files": authority["runtime_files"],
        }
        if inventory != expected_inventory:
            raise SandboxUnavailable("in-container verifier inventory drifted")


def _bounded_regular(path: Path, cap: int, *, mode: int | None = None) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise SandboxUnavailable(f"missing sandbox authority: {path.name}") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size > cap
        or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
    ):
        raise SandboxUnavailable(f"invalid sandbox authority: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SandboxUnavailable(f"sandbox authority could not be opened: {path.name}") from exc
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or (before.st_dev, before.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise SandboxUnavailable("sandbox authority changed while opening")
        chunks: list[bytes] = []
        size = 0
        while size <= cap:
            chunk = os.read(fd, min(65_536, cap + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(fd)
        if size > cap or (current.st_size, current.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise SandboxUnavailable("sandbox authority changed or exceeded its cap")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_hex64(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _canonical_object(raw: bytes, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxUnavailable(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise SandboxUnavailable(f"{label} is not a canonical object")
    return value


def _bounded_backend_command(argv: tuple[str, ...], *, timeout: float = 30) -> bytes:
    """Run one read-only backend observer with wall and output bounds."""

    registry_path: Path | None = None
    try:
        process, registry_path = _registered_popen(argv, env=_docker_env())
    except OSError as exc:
        raise SandboxUnavailable("backend identity observer could not start") from exc
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    failure: str | None = None
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "wall timeout"
                break
            for key, _mask in selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                stream = str(key.data)
                captured[stream].extend(chunk)
                if len(captured[stream]) > 65_536:
                    failure = f"{stream} overflow"
                    break
            if failure is not None:
                break
        if failure is not None:
            _terminate_group(process)
        try:
            returncode = process.wait(
                timeout=2 if failure else max(0.1, deadline - time.monotonic())
            )
        except subprocess.TimeoutExpired as exc:
            _terminate_group(process)
            raise SandboxUnavailable("backend identity observer did not terminate") from exc
    finally:
        selector.close()
        if process.poll() is None:
            _terminate_group(process)
            process.wait(timeout=2)
        if process.poll() is not None:
            _finish_registered(registry_path, process.returncode)
    stderr = bytes(captured["stderr"])
    if failure is not None or returncode != 0:
        detail = stderr[:4096].decode("utf-8", errors="replace").strip()
        raise SandboxUnavailable(f"backend identity observer failed: {failure or detail}")
    return bytes(captured["stdout"])


def _sealed_reference(
    repo: Path,
    row: object,
    *,
    relative: str,
) -> tuple[bytes, dict[str, object]]:
    expected = (repo / relative).resolve()
    if (
        not isinstance(row, dict)
        or set(row) != {"bytes", "path", "sha256"}
        or Path(str(row.get("path"))) != expected
        or isinstance(row.get("bytes"), bool)
        or not isinstance(row.get("bytes"), int)
        or int(row["bytes"]) <= 0
        or not _is_hex64(row.get("sha256"))
    ):
        raise SandboxUnavailable(f"backend {relative} reference is invalid")
    raw = _bounded_regular(expected, MAX_AUTHORITY_BYTES, mode=0o600)
    if len(raw) != row["bytes"] or _sha(raw) != row["sha256"]:
        raise SandboxUnavailable(f"backend {relative} reference drifted")
    return raw, _canonical_object(raw, label=f"backend {relative}")


def _global_docker_authority(home: Path) -> tuple[dict[str, object], bytes | None]:
    path = home / ".docker/config.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return {"config_exists": False, "current_context": "default"}, None
    raw = _bounded_regular(path, 64 * 1024)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxUnavailable("global Docker config is invalid JSON") from exc
    if not isinstance(value, dict):
        raise SandboxUnavailable("global Docker config is not an object")
    current = value.get("currentContext", "default")
    if not isinstance(current, str) or not current:
        raise SandboxUnavailable("global Docker currentContext is invalid")
    mode = stat.S_IMODE(path.lstat().st_mode)
    return (
        {
            "config_bytes": len(raw),
            "config_exists": True,
            "config_mode": mode,
            "config_sha256": _sha(raw),
            "current_context": current,
        },
        raw,
    )


def _docker_context_metadata(home: Path, name: str) -> tuple[dict[str, object], bytes, Path]:
    root = home / ".docker/contexts/meta"
    try:
        root_info = root.lstat()
        if not stat.S_ISDIR(root_info.st_mode):
            raise SandboxUnavailable("Docker context metadata root is not a directory")
        entries: list[os.DirEntry[str]] = []
        with os.scandir(root) as iterator:
            for entry in iterator:
                entries.append(entry)
                if len(entries) > 128:
                    raise SandboxUnavailable("Docker context metadata exceeds its scan cap")
        entries.sort(key=lambda row: row.name)
    except OSError as exc:
        raise SandboxUnavailable("Docker context metadata cannot be scanned") from exc
    matches: list[tuple[dict[str, object], bytes, Path]] = []
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        path = Path(entry.path) / "meta.json"
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        raw = _bounded_regular(path, 64 * 1024)
        try:
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxUnavailable("Docker context metadata is invalid JSON") from exc
        if not isinstance(value, dict):
            raise SandboxUnavailable("Docker context metadata is not an object")
        if value.get("Name") == name:
            matches.append((value, raw, path))
    if len(matches) != 1:
        raise SandboxUnavailable(f"Docker context {name!r} is absent or ambiguous")
    return matches[0]


def _context_inspect_authority(
    *, home: Path, metadata: dict[str, object], metadata_path: Path
) -> dict[str, object]:
    digest_dir = metadata_path.parent
    return {
        **metadata,
        "Storage": {
            "MetadataPath": str(digest_dir),
            "TLSPath": str(home / ".docker/contexts/tls" / digest_dir.name),
        },
        "TLSMaterial": {},
    }


def _validate_colima_profile_config(raw: bytes) -> None:
    if _sha(raw) != PROFILE_CONFIG_SHA256:
        raise SandboxUnavailable("owned Colima profile config bytes drifted")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SandboxUnavailable("owned Colima profile config is not UTF-8") from exc
    expected = {
        (0, "cpu"): "2",
        (0, "disk"): "20",
        (0, "memory"): "4",
        (0, "arch"): "x86_64",
        (0, "runtime"): "docker",
        (0, "autoActivate"): "false",
        (2, "enabled"): "false",
        (2, "address"): "false",
        (0, "forwardAgent"): "false",
        (0, "vmType"): "vz",
        (0, "portForwarder"): "none",
        (0, "rosetta"): "false",
        (0, "binfmt"): "false",
        (0, "mountType"): "virtiofs",
        (0, "sshConfig"): "false",
        (0, "mounts"): "null",
        (0, "rootDisk"): "10",
    }
    for (indent, key), value in expected.items():
        pattern = rf"^{re.escape(' ' * indent + key)}:[ \t]*{re.escape(value)}[ \t]*$"
        if len(re.findall(pattern, text, flags=re.MULTILINE)) != 1:
            raise SandboxUnavailable(f"owned Colima profile config field drifted: {key}")


def _validate_docker_daemon_info(raw: bytes) -> dict[str, object]:
    """Return the closed frozen projection after validating every daemon-info field."""

    try:
        info = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxUnavailable("task-local Docker daemon info is invalid") from exc
    if (
        not isinstance(info, dict)
        or set(info) != DOCKER_INFO_FIELDS
        or info.get("Name") != DOCKER_CONTEXT
        or info.get("CgroupVersion") != "2"
        or info.get("OSType") != "linux"
        or info.get("Architecture") != "x86_64"
        or info.get("NCPU") != 2
        or info.get("MemoryLimit") is not True
        or info.get("SwapLimit") is not True
        or info.get("CpuCfsPeriod") is not True
        or info.get("CpuCfsQuota") is not True
        or info.get("PidsLimit") is not True
        or not isinstance(info.get("SecurityOptions"), list)
        or "name=seccomp,profile=builtin" not in info["SecurityOptions"]
        or "name=cgroupns" not in info["SecurityOptions"]
    ):
        raise SandboxUnavailable("task-local Docker daemon settings drifted")
    counters = DOCKER_INFO_DYNAMIC_FIELDS - {"SystemTime"}
    if (
        any(
            isinstance(info[field], bool) or not isinstance(info[field], int) or info[field] < 0
            for field in counters
        )
        or not isinstance(info["SystemTime"], str)
        or not info["SystemTime"]
        or info["Containers"]
        != info["ContainersPaused"] + info["ContainersRunning"] + info["ContainersStopped"]
    ):
        raise SandboxUnavailable("task-local Docker daemon dynamic evidence is invalid")
    projection = {
        key: nested for key, nested in info.items() if key not in DOCKER_INFO_DYNAMIC_FIELDS
    }
    if _sha(_canonical(projection)) != DOCKER_INFO_STABLE_SHA256:
        raise SandboxUnavailable("task-local Docker daemon stable authority drifted")
    return projection


def _backend_identity_snapshot(repo: Path) -> bytes:
    """Reopen the exact owned Colima/context authority before and after one tier."""

    ownership_path = (
        repo / ".generated/state/rrcv2-convergence/capability/backend-ownership.v1.json"
    )
    ownership_raw = _bounded_regular(ownership_path, 64 * 1024, mode=0o600)
    ownership = _canonical_object(ownership_raw, label="backend ownership")
    created_unix_ns = ownership.get("created_unix_ns")
    if (
        set(ownership) != BACKEND_OWNERSHIP_FIELDS
        or ownership.get("v") != 1
        or ownership.get("kind") != "rrcv2_backend_ownership"
        or ownership.get("owned") is not True
        or ownership.get("profile") != BACKEND_PROFILE
        or ownership.get("context") != DOCKER_CONTEXT
        or ownership.get("provider_launch_total") != 0
        or isinstance(created_unix_ns, bool)
        or not isinstance(created_unix_ns, int)
        or created_unix_ns <= 0
    ):
        raise SandboxUnavailable("backend ownership identity drifted")

    lock_raw, legacy_lock = _sealed_reference(
        repo,
        ownership.get("lock"),
        relative="contextmesh/docker/rrcv2-verifier.lock.json",
    )
    apfs_raw, apfs = _sealed_reference(
        repo,
        ownership.get("apfs"),
        relative=".generated/state/rrcv2-convergence/capability/apfs-evidence.v1.json",
    )
    docker_raw, docker = _sealed_reference(
        repo,
        ownership.get("docker"),
        relative=".generated/state/rrcv2-convergence/capability/docker-evidence.v1.json",
    )
    sandbox_raw, sandbox = _sealed_reference(
        repo,
        ownership.get("sandbox"),
        relative=".generated/state/rrcv2-convergence/capability/sandbox-evidence.v1.json",
    )
    if (
        legacy_lock.get("v") != 1
        or legacy_lock.get("platform") != "linux/amd64"
        or not isinstance(legacy_lock.get("image_config_digest"), str)
        or set(docker) != BACKEND_DOCKER_FIELDS
        or docker.get("v") != 1
        or docker.get("kind") != "rrcv2_docker_capability"
        or docker.get("profile") != BACKEND_PROFILE
        or docker.get("context") != DOCKER_CONTEXT
        or docker.get("provider_launch_total") != 0
        or docker.get("image_config_digest") != legacy_lock.get("image_config_digest")
        or docker.get("image_manifest_digest") != legacy_lock.get("image_manifest_digest")
        or apfs.get("v") != 1
        or apfs.get("kind") != "rrcv2_apfs_capability"
        or apfs.get("provider_launch_total") != 0
        or sandbox.get("v") != 1
        or sandbox.get("kind") != "rrcv2_sandbox_capability"
        or sandbox.get("backend") != "colima-docker"
        or sandbox.get("docker_context") != DOCKER_CONTEXT
    ):
        raise SandboxUnavailable("backend sealed evidence relation drifted")

    environment = _docker_env()
    home = Path(environment["HOME"]).resolve()
    global_authority, global_raw = _global_docker_authority(home)
    if ownership.get("global_context_before") != global_authority:
        raise SandboxUnavailable("global Docker config drifted from backend ownership")
    if docker.get("global_context_before") != global_authority:
        raise SandboxUnavailable("Docker evidence disagrees with global context authority")
    current_name = str(global_authority["current_context"])
    current_meta_sha: str | None = None
    if current_name != "default":
        _current, current_raw, _current_path = _docker_context_metadata(home, current_name)
        current_meta_sha = _sha(current_raw)

    context, context_raw, context_path = _docker_context_metadata(home, DOCKER_CONTEXT)
    endpoints = context.get("Endpoints")
    docker_endpoint = endpoints.get("docker") if isinstance(endpoints, dict) else None
    expected_host = f"unix://{home}/.colima/{BACKEND_PROFILE}/docker.sock"
    if (
        set(context) != {"Endpoints", "Metadata", "Name"}
        or context.get("Name") != DOCKER_CONTEXT
        or not isinstance(docker_endpoint, dict)
        or set(docker_endpoint) != {"Host", "SkipTLSVerify"}
        or docker_endpoint.get("Host") != expected_host
        or docker_endpoint.get("SkipTLSVerify") is not False
    ):
        raise SandboxUnavailable("task-local Docker context endpoint drifted")
    inspect = _context_inspect_authority(home=home, metadata=context, metadata_path=context_path)
    if _sha(_canonical(inspect)) != docker.get("context_inspect_sha256"):
        raise SandboxUnavailable("task-local Docker context metadata drifted")

    status_raw = _bounded_backend_command(
        ("colima", "status", BACKEND_PROFILE, "--json"), timeout=30
    )
    if _sha(status_raw) != docker.get("colima_status_sha256"):
        raise SandboxUnavailable("owned Colima profile state drifted")
    try:
        status = json.loads(status_raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxUnavailable("owned Colima profile status is invalid") from exc
    if (
        not isinstance(status, dict)
        or status.get("display_name") != f"colima [profile={BACKEND_PROFILE}]"
        or status.get("arch") != "x86_64"
        or status.get("runtime") != "docker"
        or status.get("mount_type") != "virtiofs"
        or status.get("docker_socket") != expected_host
        or status.get("kubernetes") is not False
        or status.get("cpu") != 2
        or status.get("memory") != 4_294_967_296
        or status.get("disk") != 21_474_836_480
    ):
        raise SandboxUnavailable("owned Colima profile settings drifted")

    profile_config = _bounded_regular(
        home / ".colima" / BACKEND_PROFILE / "colima.yaml", 1024 * 1024
    )
    _validate_colima_profile_config(profile_config)
    version_raw = _bounded_backend_command(("colima", "version"), timeout=30)
    if _sha(version_raw) != docker.get("colima_version_sha256"):
        raise SandboxUnavailable("owned Colima executable version drifted")
    info_raw = _bounded_backend_command(
        ("docker", "--context", DOCKER_CONTEXT, "info", "--format", "{{json .}}"),
        timeout=30,
    )
    if not _is_hex64(docker.get("docker_info_sha256")):
        raise SandboxUnavailable("sealed Docker daemon evidence is malformed")
    info_projection = _validate_docker_daemon_info(info_raw)
    return _canonical(
        {
            "apfs_sha256": _sha(apfs_raw),
            "context_inspect_sha256": _sha(_canonical(inspect)),
            "context_metadata_sha256": _sha(context_raw),
            "current_context_metadata_sha256": current_meta_sha,
            "docker_sha256": _sha(docker_raw),
            "global_config_sha256": _sha(global_raw) if global_raw is not None else None,
            "legacy_lock_sha256": _sha(lock_raw),
            "ownership_sha256": _sha(ownership_raw),
            "profile_config_sha256": _sha(profile_config),
            "colima_version_sha256": _sha(version_raw),
            "docker_info_projection_sha256": _sha(_canonical(info_projection)),
            "producer_docker_info_sha256": docker["docker_info_sha256"],
            "sandbox_sha256": _sha(sandbox_raw),
            "status_sha256": _sha(status_raw),
            "v": 1,
        }
    )


def _closed_lock(repo: Path) -> dict[str, object]:
    path = repo / "rrc/pipeline/verifier_image_lock.v2.json"
    raw = _bounded_regular(path, MAX_AUTHORITY_BYTES)
    if _sha(raw) != RUNTIME_LOCK_SHA256:
        raise SandboxUnavailable("verifier runtime lock hash drifted")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxUnavailable("verifier lock is invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != IMAGE_LOCK_FIELDS or _canonical(value) != raw:
        raise SandboxUnavailable("verifier lock is not a closed canonical object")
    if value.get("v") != 2 or value.get("platform") != "linux/amd64":
        raise SandboxUnavailable("verifier lock identity drifted")
    hashes = {
        "dockerfile_sha256": repo / "contextmesh/docker/rrcv2-verifier.Dockerfile",
        "requirements_sha256": repo / "contextmesh/docker/rrcv2-verifier-requirements.txt",
        "seccomp_sha256": repo / "contextmesh/docker/rrcv2-verifier-seccomp.json",
    }
    for field, source in hashes.items():
        if value.get(field) != _sha(_bounded_regular(source, MAX_AUTHORITY_BYTES)):
            raise SandboxUnavailable(f"verifier {field} drifted")
    capability = _bounded_regular(
        repo / ".generated/state/rrcv2-convergence/verify/sandbox-evidence.v2.json",
        MAX_AUTHORITY_BYTES,
        mode=0o600,
    )
    if value.get("capability_sha256") != _sha(capability):
        raise SandboxUnavailable("verifier capability binding drifted")
    image = value.get("image_config_digest")
    if (
        not isinstance(image, str)
        or not image.startswith("sha256:")
        or len(image) != 71
        or any(char not in "0123456789abcdef" for char in image[7:])
    ):
        raise SandboxUnavailable("verifier image digest is invalid")
    frozen_digests = {
        "image_config_digest": IMAGE_CONFIG_DIGEST,
        "image_manifest_digest": IMAGE_CONFIG_DIGEST,
        "python_selected_manifest_digest": "sha256:527c28b29498575b851ad88e7522ac7201bbd9e920d2c11b00ff2b39b315f5f8",
        "python_selected_config_digest": "sha256:6aead542d468d5e99b0795777ec50fd233b236d99e08be4f18267eb02112067b",
        "node_selected_manifest_digest": "sha256:dbb65b3b08bd9d4d4a85299ad4d668b0e709a0601cecb5969f4dbb1dd89408aa",
        "node_selected_config_digest": "sha256:8a3ae2e7d0c5383fcf30aec6c723ce8d383c10ea3686cfae29c54db736468f02",
    }
    if any(value.get(field) != expected for field, expected in frozen_digests.items()):
        raise SandboxUnavailable("verifier manifest/config selection drifted")
    executables = value.get("executables")
    packages = value.get("packages")
    runtime_files = value.get("runtime_files")
    environment = value.get("environment")
    if (
        not isinstance(executables, list)
        or [row.get("name") for row in executables if isinstance(row, dict)]
        != ["node", "pyright_js_entry", "pytest", "python", "ruff"]
        or any(
            not isinstance(row, dict)
            or set(row) != {"mode", "name", "path", "sha256", "size", "type", "version"}
            or row.get("type") != "regular"
            or isinstance(row.get("mode"), bool)
            or not isinstance(row.get("mode"), int)
            or isinstance(row.get("size"), bool)
            or not isinstance(row.get("size"), int)
            or row.get("size", 0) <= 0
            or not _is_hex64(row.get("sha256"))
            for row in executables
        )
        or not isinstance(packages, list)
        or not packages
        or any(
            not isinstance(row, dict)
            or set(row) != {"name", "version"}
            or not isinstance(row.get("name"), str)
            or not isinstance(row.get("version"), str)
            for row in packages
        )
        or packages
        != sorted(packages, key=lambda row: (str(row.get("name")), str(row.get("version"))))
        or not isinstance(runtime_files, list)
        or [row.get("path") for row in runtime_files if isinstance(row, dict)]
        != ["/usr/lib/libgcc_s.so.1", "/usr/lib/libstdc++.so.6"]
        or any(
            not isinstance(row, dict)
            or set(row) != {"mode", "path", "sha256", "size", "type"}
            or row.get("type") != "regular"
            or not _is_hex64(row.get("sha256"))
            for row in runtime_files
        )
        or not isinstance(environment, list)
        or any(not isinstance(item, str) or not item for item in environment)
        or value.get("user") != "65532:65532"
    ):
        raise SandboxUnavailable("verifier package/runtime/executable inventory is malformed")
    return value


def _validate_capability_v2(repo: Path) -> None:
    path = repo / ".generated/state/rrcv2-convergence/verify/sandbox-evidence.v2.json"
    raw = _bounded_regular(path, MAX_AUTHORITY_BYTES, mode=0o600)
    try:
        validate_sandbox_evidence_v2(raw)
    except CapabilityError as exc:
        raise SandboxUnavailable(str(exc)) from exc


_PYTEST_PLUGIN = b"""\
import importlib.machinery
import importlib.util
import json
import sys
from controller.config import CANDIDATE_PATH, PROFILE, TEST_PATHS

if PROFILE == "rrcv2_general_v1":
    from controller.runner import load_test_module as _rrcv2_load_test_module
else:
    _rrcv2_load_test_module = None

_rrcv2_collected = []
_rrcv2_completed = []
_rrcv2_phases = {}
_rrcv2_invalid_nodes = set()
_RRCV2_EXPORT_KEY = "\\x00rrcv2_candidate_exports_v1"
_rrcv2_target_paths = set(TEST_PATHS.values())
_rrcv2_selected_execution = any(
    "::" in argument and argument.split("::", 1)[0] in _rrcv2_target_paths
    for argument in sys.argv[1:]
)
_rrcv2_expected_collectors = (
    set()
    if _rrcv2_selected_execution
    else {
        argument.removeprefix("/input/")
        for argument in sys.argv[1:]
        if argument in _rrcv2_target_paths
    }
)
_rrcv2_collector_counts = {}
_rrcv2_collection_invalid = False


class _RRCV2TestLoader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        if _rrcv2_load_test_module is None:
            raise RuntimeError("general test loader used outside the general profile")
        _rrcv2_load_test_module(CANDIDATE_PATH, self.path, module)


class _RRCV2TestFinder:
    def find_spec(self, fullname, path=None, target=None):
        del path, target
        test_path = TEST_PATHS.get(fullname)
        if test_path is None:
            return None
        loader = _RRCV2TestLoader(fullname, test_path)
        return importlib.util.spec_from_file_location(fullname, test_path, loader=loader)


_rrcv2_finder = None
if PROFILE == "rrcv2_general_v1":
    _rrcv2_finder = _RRCV2TestFinder()
    sys.meta_path.insert(0, _rrcv2_finder)


def _rrcv2_restore_generated_modules():
    for module in tuple(sys.modules.values()):
        namespace = getattr(module, "__dict__", None)
        if not isinstance(namespace, dict):
            continue
        exports = namespace.get(_RRCV2_EXPORT_KEY)
        if isinstance(exports, dict):
            namespace.update(exports)


def pytest_collection_finish(session):
    global _rrcv2_collected, _rrcv2_collection_invalid
    _rrcv2_collected = [item.nodeid for item in session.items]
    if _rrcv2_collector_counts != {
        nodeid: 1 for nodeid in _rrcv2_expected_collectors
    }:
        _rrcv2_collection_invalid = True
    _rrcv2_restore_generated_modules()
    print("RRCV2_COLLECTION=" + json.dumps(_rrcv2_collected, ensure_ascii=False, separators=(",", ":")))


def pytest_collectreport(report):
    global _rrcv2_collection_invalid
    if report.nodeid not in _rrcv2_expected_collectors:
        return
    count = _rrcv2_collector_counts.get(report.nodeid, 0) + 1
    _rrcv2_collector_counts[report.nodeid] = count
    if (
        count != 1
        or
        not report.passed
        or report.failed
        or report.skipped
        or getattr(report, "wasxfail", None) is not None
    ):
        _rrcv2_collection_invalid = True

def pytest_runtest_logreport(report):
    nodeid = report.nodeid
    expected = {
        "setup": (),
        "call": ("setup",),
        "teardown": ("setup", "call"),
    }
    phases = _rrcv2_phases.setdefault(nodeid, [])
    clean = (
        report.when in expected
        and report.passed
        and not report.failed
        and not report.skipped
        and getattr(report, "wasxfail", None) is None
        and tuple(phases) == expected.get(report.when)
        and nodeid not in _rrcv2_invalid_nodes
    )
    if not clean:
        _rrcv2_invalid_nodes.add(nodeid)
        while nodeid in _rrcv2_completed:
            _rrcv2_completed.remove(nodeid)
        return
    phases.append(report.when)
    if report.when == "teardown":
        _rrcv2_completed.append(nodeid)

def pytest_sessionfinish(session, exitstatus):
    del exitstatus
    if _rrcv2_collection_invalid:
        session.exitstatus = 1
    print("RRCV2_COMPLETION=" + json.dumps(_rrcv2_completed, ensure_ascii=False, separators=(",", ":")))


def pytest_unconfigure(config):
    del config
    if _rrcv2_finder is not None and _rrcv2_finder in sys.meta_path:
        sys.meta_path.remove(_rrcv2_finder)
"""

_GENERAL_RUNNER = b"""\
import ast as _ast
import importlib.machinery as _importlib_machinery
import importlib.util as _importlib_util
import symtable as _symtable
import sys as _sys
from _pytest.assertion.rewrite import rewrite_asserts as _rewrite_asserts

_CANDIDATE_NAME = "rrcv2_candidate"
_EXPORT_KEY = "\\x00rrcv2_candidate_exports_v1"
_candidate_path = None
_candidate_module = None


def load(path):
    global _candidate_path, _candidate_module
    if _candidate_module is None:
        if _CANDIDATE_NAME in _sys.modules:
            raise RuntimeError("candidate module identity is already occupied")
        loader = _importlib_machinery.SourceFileLoader(_CANDIDATE_NAME, path)
        spec = _importlib_util.spec_from_file_location(
            _CANDIDATE_NAME, path, loader=loader
        )
        if (
            spec is None
            or spec.loader is None
            or not spec.has_location
            or spec.origin != path
        ):
            raise RuntimeError("candidate module is not a file-backed module")
        module = _importlib_util.module_from_spec(spec)
        _sys.modules[_CANDIDATE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            _sys.modules.pop(_CANDIDATE_NAME, None)
            raise
        _candidate_path = path
        _candidate_module = module
    elif path != _candidate_path:
        raise RuntimeError("one general pytest suite may load only one candidate path")
    return {
        name: value
        for name, value in vars(_candidate_module).items()
        if not name.startswith("_")
    }


def _read_test(path):
    with open(path, "rb") as source_file:
        source = source_file.read(16385)
    if len(source) > 16384:
        raise ValueError("general test source exceeds its runtime cap")
    return source


def load_test_module(candidate_path, test_path, module):
    exports = load(candidate_path)
    source = _read_test(test_path)
    text = source.decode("utf-8", errors="strict")
    module_globals = vars(module)
    module_name = module_globals.get("__name__")
    if not isinstance(module_name, str) or not module_name:
        raise RuntimeError("general test module identity is missing")
    test_spec = module_globals.get("__spec__")
    test_loader = module_globals.get("__loader__")
    if (
        module_globals.get("__file__") != test_path
        or getattr(test_spec, "origin", None) != test_path
        or getattr(test_loader, "path", None) != test_path
    ):
        raise RuntimeError("general test module source identity is inconsistent")
    symbols = _symtable.symtable(text, test_path, "exec")
    bound_names = {
        symbol.get_name()
        for symbol in symbols.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
    }
    module_globals.update(exports)
    tree = _ast.parse(source, filename=test_path, mode="exec")
    _rewrite_asserts(tree, source, test_path)
    exec(compile(tree, test_path, "exec", dont_inherit=True), module_globals)
    restore = {}
    for name, value in exports.items():
        if name not in bound_names and module_globals.get(name) is value:
            restore[name] = value
            del module_globals[name]
    module_globals[_EXPORT_KEY] = restore
"""

_SYNTHETIC_RUNNER = b"""\
import __future__ as _future
import builtins as _builtins
import json as _json
import math as _math
import re as _re


class _Facade:
    __slots__ = ("_values",)

    def __init__(self, values):
        object.__setattr__(self, "_values", values)

    def __getattribute__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        values = object.__getattribute__(self, "_values")
        try:
            return values[name]
        except KeyError as error:
            raise AttributeError(name) from error


_FACADES = {
    "__future__": _Facade({"annotations": _future.annotations}),
    "json": _Facade({"dumps": _json.dumps, "loads": _json.loads}),
    "math": _Facade(
        {
            "ceil": _math.ceil,
            "fabs": _math.fabs,
            "floor": _math.floor,
            "gcd": _math.gcd,
            "isclose": _math.isclose,
            "isqrt": _math.isqrt,
            "lcm": _math.lcm,
            "sqrt": _math.sqrt,
        }
    ),
    "re": _Facade(
        {
            "escape": _re.escape,
            "fullmatch": _re.fullmatch,
            "match": _re.match,
            "search": _re.search,
            "split": _re.split,
            "sub": _re.sub,
        }
    ),
}
_SAFE_NAMES = (
    "abs",
    "all",
    "any",
    "bool",
    "dict",
    "enumerate",
    "float",
    "int",
    "len",
    "list",
    "max",
    "min",
    "range",
    "reversed",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
    "AssertionError",
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
    "ZeroDivisionError",
    "OverflowError",
)


def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
    del globals, locals, fromlist
    if level != 0 or name not in _FACADES:
        raise ImportError("synthetic import is not permitted")
    return _FACADES[name]


_SAFE_BUILTINS = {name: getattr(_builtins, name) for name in _SAFE_NAMES}
_SAFE_BUILTINS["__import__"] = _restricted_import


def _read(path):
    with open(path, "rb") as source_file:
        source = source_file.read(1048577)
    if len(source) > 1048576:
        raise ValueError("synthetic source exceeds its runtime cap")
    return source


def _load_namespace(path):
    source = _read(path)
    namespace = {
        "__builtins__": _SAFE_BUILTINS,
        "__name__": "rrcv2_candidate",
        "__package__": None,
    }
    exec(compile(source, path, "exec"), namespace)
    return namespace


def load(path):
    namespace = _load_namespace(path)
    reserved = {"TYPE_CHECKING", "json", "math", "re"}
    return {
        name: value
        for name, value in namespace.items()
        if not name.startswith("_") and name not in reserved
    }


def load_test(candidate_path, test_path):
    namespace = _load_namespace(candidate_path)
    before = set(namespace)
    exec(compile(_read(test_path), test_path, "exec"), namespace)
    return {
        name: value
        for name, value in namespace.items()
        if name not in before and name.startswith("test_") and callable(value)
    }
"""


def _write_private(path: Path, raw: bytes, *, writable: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    path.chmod(0o666 if writable else 0o444)


def _physical_artifact_path(artifact_path: str) -> str:
    """Map an unrestricted canonical logical path to one host/Linux-safe file path."""

    digest = hashlib.sha256(artifact_path.encode("utf-8", errors="strict")).hexdigest()
    return f"task/artifact-{digest}.py"


def _read_output(path: Path, cap: int) -> bytes:
    return _bounded_regular(path, cap)


def _parse_marker(stdout: bytes, prefix: bytes) -> tuple[str, ...]:
    if stdout.count(prefix) != 1:
        excerpt = stdout[-4096:].decode("utf-8", errors="replace")
        raise SandboxExecutionError(f"pytest completion marker is missing or duplicated: {excerpt}")
    start = stdout.index(prefix) + len(prefix)
    end = stdout.find(b"\n", start)
    row = stdout[start:] if end < 0 else stdout[start:end]
    try:
        value = json.loads(row.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxExecutionError("pytest completion marker is invalid") from exc
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise SandboxExecutionError("pytest completion inventory is invalid")
    return tuple(value)


def _docker_control(*argv: str, timeout: int = 30) -> bytes:
    adjusted = tuple(argv)
    if os.environ.get("RRCV2_PRODUCT_SMOKE") == "1":
        nonce = os.environ.get("RRCV2_SMOKE_RUN_NONCE", "")
        if re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
            raise SandboxExecutionError("smoke Docker label nonce is invalid")
        label = ("--label", f"org.contextmesh.rrcv2-smoke={nonce}")
        if adjusted[:2] == ("volume", "create"):
            adjusted = (*adjusted[:2], *label, *adjusted[2:])
        elif adjusted and adjusted[0] in {"create", "run"}:
            adjusted = (adjusted[0], *label, *adjusted[1:])
    try:
        result = subprocess.run(
            ("docker", "--context", DOCKER_CONTEXT, *adjusted),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=_docker_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxUnavailable("Docker workspace control failed") from exc
    if len(result.stdout) > 65_536 or len(result.stderr) > 65_536:
        raise SandboxUnavailable("Docker workspace control output exceeded its cap")
    if result.returncode != 0:
        detail = result.stderr[:4096].decode("utf-8", errors="replace").strip()
        raise SandboxUnavailable(f"Docker workspace control failed: {detail}")
    return result.stdout


def _docker_remove_owned(kind: Literal["container", "volume"], name: str) -> None:
    remove_argv = ("rm", "-f", name) if kind == "container" else ("volume", "rm", "-f", name)
    inspect_argv = ("inspect", name) if kind == "container" else ("volume", "inspect", name)
    try:
        result = subprocess.run(
            ("docker", "--context", DOCKER_CONTEXT, *remove_argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            env=_docker_env(),
            check=False,
        )
        inspect = subprocess.run(
            ("docker", "--context", DOCKER_CONTEXT, *inspect_argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            env=_docker_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxExecutionError(f"owned {kind} cleanup could not be proven") from exc
    if (
        len(result.stdout) > 65_536
        or len(result.stderr) > 65_536
        or len(inspect.stdout) > 65_536
        or len(inspect.stderr) > 65_536
        or inspect.returncode == 0
        or (result.returncode != 0 and b"no such" not in result.stderr.lower())
        or b"no such" not in inspect.stderr.lower()
    ):
        raise SandboxExecutionError(f"owned {kind} survived or cleanup evidence was inexact")


class _DockerVolumeWorkspace:
    """Copy attempt bytes into daemon-local volumes without exposing host paths to the VM."""

    def __init__(self, image: str, input_dir: Path, output_dir: Path) -> None:
        suffix = uuid.uuid4().hex
        self.image = image
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.sentinel_dir = input_dir.parent / "denied-sentinel"
        self.input_volume = f"rrcv2-input-{suffix}"
        self.output_volume = f"rrcv2-output-{suffix}"
        self.sentinel_volume = f"rrcv2-sentinel-{suffix}"
        self._created: list[str] = []

    def _materialize_sentinel(self) -> None:
        self.sentinel_dir.mkdir(mode=0o700)
        path = self.sentinel_dir / "sentinel.v1"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags, 0o600)
        try:
            offset = 0
            while offset < len(DENIED_SENTINEL_BYTES):
                offset += os.write(fd, DENIED_SENTINEL_BYTES[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        path.chmod(0o600)

    def prove_sentinel(self) -> None:
        """Prove the mounted sentinel is root-readable and remains byte-identical."""

        name = f"rrcv2-sentinel-check-{uuid.uuid4().hex}"
        try:
            observed = _docker_control(
                "run",
                "--rm",
                "--name",
                name,
                "--network=none",
                "--read-only",
                "--user=0:0",
                f"--mount=type=volume,src={self.sentinel_volume},dst=/rrcv2-denied,readonly",
                self.image,
                "/usr/local/bin/python",
                "-I",
                "-c",
                (f"import sys;sys.stdout.buffer.write(open({DENIED_SENTINEL_PATH!r},'rb').read())"),
            )
            if observed != DENIED_SENTINEL_BYTES:
                raise SandboxUnavailable("mounted denied sentinel bytes drifted")
        finally:
            _docker_remove_owned("container", name)

    def _stage(self, volume: str, destination: str, source: Path) -> None:
        name = f"rrcv2-stage-{uuid.uuid4().hex}"
        failure: BaseException | None = None
        try:
            _docker_control(
                "create",
                "--name",
                name,
                f"--mount=type=volume,src={volume},dst={destination}",
                self.image,
                "/usr/local/bin/python",
                "-I",
                "-c",
                "pass",
            )
            _docker_control("cp", f"{source}/.", f"{name}:{destination}")
        except BaseException as exc:
            failure = exc
        finally:
            try:
                _docker_remove_owned("container", name)
            except BaseException as cleanup_error:
                if failure is not None:
                    raise SandboxExecutionError(
                        "staging failed and its container could not be cleaned"
                    ) from cleanup_error
                raise
        if failure is not None:
            raise failure

    def _cleanup(self) -> None:
        failures: list[BaseException] = []
        for volume in reversed(self._created):
            try:
                _docker_remove_owned("volume", volume)
            except BaseException as exc:
                failures.append(exc)
        self._created.clear()
        if failures:
            raise SandboxExecutionError("verifier volume cleanup failed") from failures[0]

    def __enter__(self) -> _DockerVolumeWorkspace:
        try:
            self._materialize_sentinel()
            for volume in (self.input_volume, self.output_volume, self.sentinel_volume):
                _docker_control(
                    "volume",
                    "create",
                    "--label",
                    "org.contextmesh.owner=rrcv2-verifier",
                    volume,
                )
                self._created.append(volume)
            self._stage(self.input_volume, "/input", self.input_dir)
            self._stage(self.output_volume, "/output", self.output_dir)
            self._stage(self.sentinel_volume, "/rrcv2-denied", self.sentinel_dir)
            self.prove_sentinel()
            return self
        except BaseException as exc:
            try:
                self._cleanup()
            except BaseException as cleanup_error:
                raise SandboxExecutionError(
                    "workspace acquisition failed and rollback was incomplete"
                ) from cleanup_error
            raise exc

    def extract(self, artifact_path: str) -> bytes:
        name = f"rrcv2-extract-{uuid.uuid4().hex}"
        target = self.output_dir / "extracted.py"
        try:
            _docker_control(
                "create",
                "--name",
                name,
                f"--mount=type=volume,src={self.output_volume},dst=/output,readonly",
                self.image,
                "/usr/local/bin/python",
                "-I",
                "-c",
                "pass",
            )
            _docker_control("cp", f"{name}:/output/{artifact_path}", str(target))
            return _read_output(target, 1024 * 1024)
        finally:
            _docker_remove_owned("container", name)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._cleanup()


class SealedDockerSandbox:
    """Production verifier adapter backed only by the sealed local OCI image."""

    def __init__(
        self,
        repo: Path,
        *,
        supervisor: DockerSupervisorPort | None = None,
        backend_identity: Callable[[Path], bytes] | None = None,
    ) -> None:
        self.repo = repo.resolve()
        self.supervisor = supervisor or SubprocessDockerSupervisor()
        self.backend_identity = backend_identity or _backend_identity_snapshot

    def _authority(self) -> tuple[str, Path, bytes]:
        backend_snapshot = self.backend_identity(self.repo)
        _validate_capability_v2(self.repo)
        lock = _closed_lock(self.repo)
        image = str(lock["image_config_digest"])
        seccomp = (self.repo / "contextmesh/docker/rrcv2-verifier-seccomp.json").resolve()
        self.supervisor.attest_image(lock, seccomp)
        return image, seccomp, backend_snapshot

    def _argv(
        self,
        *,
        image: str,
        input_volume: str,
        output_volume: str,
        sentinel_volume: str,
        seccomp: Path,
        trusted: bool,
        output_writable: bool,
        name: str,
        command: tuple[str, ...],
        cpu_seconds: int | None = None,
    ) -> tuple[str, ...]:
        frozen = SandboxLimits()
        cpu_limit = frozen.cpu_seconds if cpu_seconds is None else cpu_seconds
        if cpu_limit < 1 or cpu_limit > frozen.cpu_seconds:
            raise SandboxExecutionError("tier CPU allocation is invalid")
        pids = frozen.trusted_tool_pids if trusted else frozen.untrusted_pids
        output_mode = "" if output_writable else ",readonly"
        return (
            "docker",
            "--context",
            DOCKER_CONTEXT,
            "run",
            "--rm",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cpus=1",
            f"--memory={frozen.memory_bytes}",
            f"--memory-swap={frozen.memory_swap_bytes}",
            f"--pids-limit={pids}",
            f"--ulimit=nofile={frozen.file_descriptors}:{frozen.file_descriptors}",
            f"--ulimit=fsize={frozen.created_file_bytes}:{frozen.created_file_bytes}",
            f"--ulimit=cpu={cpu_limit}:{cpu_limit}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--security-opt=seccomp={seccomp}",
            f"--tmpfs=/scratch:rw,noexec,nosuid,nodev,mode=1777,size={frozen.scratch_bytes}",
            f"--mount=type=volume,src={input_volume},dst=/input,readonly",
            f"--mount=type=volume,src={output_volume},dst=/output{output_mode}",
            f"--mount=type=volume,src={sentinel_volume},dst=/rrcv2-denied,readonly",
            "--workdir=/scratch",
            image,
            *command,
        )

    def _execute(
        self,
        *,
        image: str,
        input_volume: str,
        output_volume: str,
        sentinel_volume: str,
        seccomp: Path,
        trusted: bool,
        output_writable: bool,
        limits: SandboxLimitsV1,
        command: tuple[str, ...],
        timeout_seconds: float | None = None,
        cpu_seconds: int | None = None,
    ) -> CommandObservation:
        name = f"rrcv2-tier-{uuid.uuid4().hex}"
        argv = self._argv(
            image=image,
            input_volume=input_volume,
            output_volume=output_volume,
            sentinel_volume=sentinel_volume,
            seccomp=seccomp,
            trusted=trusted,
            output_writable=output_writable,
            name=name,
            command=command,
            cpu_seconds=cpu_seconds,
        )
        observed = self.supervisor.run(
            argv,
            container_name=name,
            timeout_seconds=(limits.wall_seconds if timeout_seconds is None else timeout_seconds),
            stdout_cap=limits.stdout_bytes,
            stderr_cap=limits.stderr_bytes,
        )
        return replace(observed, argv=argv)

    @staticmethod
    def _materialize(
        root: Path,
        *,
        verification_profile: Profile,
        artifact_path: str,
        source: bytes,
        tests: tuple[SandboxTestFileV1, ...],
    ) -> tuple[Path, Path]:
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir(mode=0o700)
        output_dir.mkdir(mode=0o777)
        controller_dir = input_dir / "controller"
        controller_dir.mkdir(mode=0o755)
        physical_artifact = _physical_artifact_path(artifact_path)
        input_artifact = input_dir / physical_artifact
        output_artifact = output_dir / physical_artifact
        input_artifact.parent.mkdir(parents=True, exist_ok=True)
        output_artifact.parent.mkdir(parents=True, exist_ok=True)
        _write_private(input_artifact, source)
        _write_private(output_artifact, source, writable=True)
        _write_private(controller_dir / "candidate.py", source)
        _write_private(controller_dir / "__init__.py", b"")
        candidate_path = f"/input/{physical_artifact}"
        test_paths = {Path(test.filename).stem: f"/input/{test.filename}" for test in tests}
        controller_config = (
            f"PROFILE = {verification_profile!r}\n"
            f"CANDIDATE_PATH = {candidate_path!r}\n"
            f"TEST_PATHS = {test_paths!r}\n"
        ).encode("utf-8")
        _write_private(controller_dir / "config.py", controller_config)
        _write_private(input_dir / "conftest.py", _PYTEST_PLUGIN)
        if verification_profile == "rrcv2_synthetic_v1":
            _write_private(controller_dir / "runner.py", _SYNTHETIC_RUNNER)
        else:
            _write_private(controller_dir / "runner.py", _GENERAL_RUNNER)
        for test in tests:
            if verification_profile == "rrcv2_synthetic_v1":
                artifact_literal = json.dumps(candidate_path, ensure_ascii=False)
                model_test = f".rrcv2-model-{test.category}-{test.index:02d}.src"
                _write_private(controller_dir / model_test, test.source)
                model_test_literal = json.dumps(
                    f"/input/controller/{model_test}", ensure_ascii=False
                )
                body = (
                    "import runpy as _rrcv2_runpy\n"
                    "_rrcv2_load_test = "
                    "_rrcv2_runpy.run_path('/input/controller/runner.py')['load_test']\n"
                    "globals().update(\n"
                    f"    _rrcv2_load_test({artifact_literal}, {model_test_literal})\n"
                    ")\n"
                    "del _rrcv2_load_test, _rrcv2_runpy\n"
                ).encode()
            else:
                body = test.source
            _write_private(input_dir / test.filename, body)
        config = _canonical(
            {
                "executionEnvironments": [{"root": "/input"}],
                "include": ["controller/candidate.py"],
                "pythonPlatform": "Linux",
                "pythonVersion": "3.11",
                "typeCheckingMode": "basic",
                "venvPath": "/rrcv2-no-venv",
            }
        )
        _write_private(input_dir / "pyrightconfig.json", config)
        return input_dir, output_dir

    def run(
        self,
        *,
        tier: Tier,
        verification_profile: Profile,
        artifact_path: str,
        source: bytes,
        tests: tuple[SandboxTestFileV1, ...],
        selected_node_ids: tuple[str, ...] = (),
        limits: SandboxLimitsV1,
    ) -> SandboxResultV1:
        image, seccomp, backend_snapshot = self._authority()
        post_backend_snapshot: bytes
        try:
            with tempfile.TemporaryDirectory(prefix="rrcv2-verifier-") as directory:
                root = Path(directory)
                input_dir, output_dir = self._materialize(
                    root,
                    verification_profile=verification_profile,
                    artifact_path=artifact_path,
                    source=source,
                    tests=tests,
                )
                with _DockerVolumeWorkspace(image, input_dir, output_dir) as workspace:
                    try:
                        result = self._run_workspace(
                            tier=tier,
                            artifact_path=artifact_path,
                            tests=tests,
                            selected_node_ids=selected_node_ids,
                            limits=limits,
                            image=image,
                            seccomp=seccomp,
                            workspace=workspace,
                        )
                    finally:
                        workspace.prove_sentinel()
        finally:
            post_backend_snapshot = self.backend_identity(self.repo)
            if post_backend_snapshot != backend_snapshot:
                raise SandboxUnavailable("backend identity drifted during verifier tier")
        if not result.invocations or any(not row.argv for row in result.invocations):
            raise SandboxUnavailable("verifier tier lacks exact invocation evidence")
        capability_sha256 = _sha(
            _bounded_regular(
                self.repo / ".generated/state/rrcv2-convergence/verify/sandbox-evidence.v2.json",
                MAX_AUTHORITY_BYTES,
                mode=0o600,
            )
        )
        evidence = SandboxTierExecutionV1(
            tier=tier,
            backend_before_sha256=_sha(backend_snapshot),
            backend_after_sha256=_sha(post_backend_snapshot),
            capability_sha256=capability_sha256,
            runtime_lock_sha256=RUNTIME_LOCK_SHA256,
            image_config_digest=image,
            invocations=tuple(
                SandboxInvocationEvidenceV1(
                    argv_sha256=_sha(_canonical(list(row.argv))),
                    exit_code=row.returncode,
                    stdout_sha256=_sha(row.stdout),
                    stderr_sha256=_sha(row.stderr),
                )
                for row in result.invocations
            ),
            normalized_source_sha256=(
                _sha(result.normalized_source) if result.normalized_source is not None else None
            ),
        )
        return replace(result, execution_evidence=evidence)

    def _run_workspace(
        self,
        *,
        tier: Tier,
        artifact_path: str,
        tests: tuple[SandboxTestFileV1, ...],
        selected_node_ids: tuple[str, ...],
        limits: SandboxLimitsV1,
        image: str,
        seccomp: Path,
        workspace: _DockerVolumeWorkspace,
    ) -> SandboxResultV1:
        physical_artifact = _physical_artifact_path(artifact_path)
        if tier == "ruff":
            commands = (
                (
                    "/usr/local/bin/ruff",
                    "format",
                    "--isolated",
                    "--target-version",
                    "py311",
                    f"/output/{physical_artifact}",
                ),
                (
                    "/usr/local/bin/ruff",
                    "check",
                    "--isolated",
                    "--target-version",
                    "py311",
                    "--select",
                    "E4,E7,E9,F,I",
                    "--fix",
                    "--no-unsafe-fixes",
                    f"/output/{physical_artifact}",
                ),
                (
                    "/usr/local/bin/ruff",
                    "format",
                    "--isolated",
                    "--target-version",
                    "py311",
                    f"/output/{physical_artifact}",
                ),
                (
                    "/usr/local/bin/ruff",
                    "check",
                    "--isolated",
                    "--target-version",
                    "py311",
                    "--select",
                    "E4,E7,E9,F,I",
                    f"/output/{physical_artifact}",
                ),
            )
            stdout = bytearray()
            stderr = bytearray()
            returncode = 0
            invocations: list[CommandObservation] = []
            deadline = time.monotonic() + limits.wall_seconds
            cpu_allocations = (3, 3, 2, 2)
            for command, cpu_seconds in zip(commands, cpu_allocations, strict=True):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SandboxExecutionError("aggregate Ruff wall timeout")
                observed = self._execute(
                    image=image,
                    input_volume=workspace.input_volume,
                    output_volume=workspace.output_volume,
                    sentinel_volume=workspace.sentinel_volume,
                    seccomp=seccomp,
                    trusted=True,
                    output_writable=True,
                    limits=limits,
                    command=command,
                    timeout_seconds=remaining,
                    cpu_seconds=cpu_seconds,
                )
                invocations.append(observed)
                if time.monotonic() > deadline:
                    raise SandboxExecutionError("aggregate Ruff wall timeout")
                stdout.extend(observed.stdout)
                stderr.extend(observed.stderr)
                if len(stdout) > limits.stdout_bytes or len(stderr) > limits.stderr_bytes:
                    raise SandboxExecutionError("aggregate Ruff output exceeded its cap")
                if observed.returncode != 0:
                    returncode = observed.returncode
                    break
            normalized = workspace.extract(physical_artifact) if returncode == 0 else None
            return SandboxResultV1(
                tier,
                returncode,
                bytes(stdout),
                bytes(stderr),
                normalized_source=normalized,
                invocations=tuple(invocations),
            )
        if tier == "pyright":
            observed = self._execute(
                image=image,
                input_volume=workspace.input_volume,
                output_volume=workspace.output_volume,
                sentinel_volume=workspace.sentinel_volume,
                seccomp=seccomp,
                trusted=True,
                output_writable=False,
                limits=limits,
                command=(
                    "/usr/local/bin/node",
                    "/usr/local/lib/python3.13/site-packages/pyright/dist/index.js",
                    "--project",
                    "/input/pyrightconfig.json",
                ),
            )
            return SandboxResultV1(
                tier,
                observed.returncode,
                observed.stdout,
                observed.stderr,
                invocations=(observed,),
            )
        test_paths = tuple(f"/input/{test.filename}" for test in tests)
        if selected_node_ids:
            if tier != "pytest" or any(
                node.startswith("/") or ".." in Path(node).parts for node in selected_node_ids
            ):
                raise SandboxExecutionError("invalid selected pytest node inventory")
            test_paths = tuple(f"/input/{node}" for node in selected_node_ids)
        command = (
            "/usr/local/bin/python",
            "-I",
            "-m",
            "pytest",
            "-q",
            "-s",
            "-c",
            "/dev/null",
            "--disable-warnings",
            "--rootdir=/input",
            *(("--collect-only",) if tier == "pytest_collect" else ()),
            *test_paths,
        )
        observed = self._execute(
            image=image,
            input_volume=workspace.input_volume,
            output_volume=workspace.output_volume,
            sentinel_volume=workspace.sentinel_volume,
            seccomp=seccomp,
            trusted=False,
            output_writable=False,
            limits=limits,
            command=command,
        )
        collected = _parse_marker(observed.stdout, b"RRCV2_COLLECTION=")
        completed = (
            () if tier == "pytest_collect" else _parse_marker(observed.stdout, b"RRCV2_COMPLETION=")
        )
        return SandboxResultV1(
            tier,
            observed.returncode,
            observed.stdout,
            observed.stderr,
            collected_node_ids=collected,
            completed_node_ids=completed,
            invocations=(observed,),
        )


def validate_sandbox_result(
    result: SandboxResultV1,
    *,
    expected_tier: Tier,
    limits: SandboxLimitsV1,
) -> None:
    """Reject incomplete, unbounded, or cross-tier backend evidence."""

    if result.tier != expected_tier or not result.backend_attested:
        raise ValueError("sandbox result is not attested for the requested tier")
    evidence = result.execution_evidence
    if (
        evidence is None
        or evidence.v != 1
        or evidence.tier != expected_tier
        or evidence.backend_before_sha256 != evidence.backend_after_sha256
        or not _is_hex64(evidence.backend_before_sha256)
        or evidence.capability_sha256 != CAPABILITY_SHA256
        or evidence.runtime_lock_sha256 != RUNTIME_LOCK_SHA256
        or evidence.image_config_digest != IMAGE_CONFIG_DIGEST
        or not evidence.invocations
        or len(evidence.invocations) != len(result.invocations)
    ):
        raise ValueError("sandbox tier execution authority is missing or malformed")
    for authority, observation in zip(evidence.invocations, result.invocations, strict=True):
        if (
            authority.v != 1
            or not observation.argv
            or authority.argv_sha256 != _sha(_canonical(list(observation.argv)))
            or authority.exit_code != observation.returncode
            or authority.stdout_sha256 != _sha(observation.stdout)
            or authority.stderr_sha256 != _sha(observation.stderr)
        ):
            raise ValueError("sandbox invocation evidence relation is invalid")
    if (
        b"".join(row.stdout for row in result.invocations) != result.stdout
        or b"".join(row.stderr for row in result.invocations) != result.stderr
        or result.exit_code != result.invocations[-1].returncode
        or evidence.normalized_source_sha256
        != (_sha(result.normalized_source) if result.normalized_source is not None else None)
    ):
        raise ValueError("sandbox tier result differs from its invocation evidence")
    if isinstance(result.exit_code, bool) or not isinstance(result.exit_code, int):
        raise ValueError("sandbox exit code is invalid")
    if len(result.stdout) > limits.stdout_bytes or len(result.stderr) > limits.stderr_bytes:
        raise ValueError("sandbox output exceeds the frozen bound")
    if len(set(result.collected_node_ids)) != len(result.collected_node_ids):
        raise ValueError("sandbox collection contains duplicate node IDs")
    if len(set(result.completed_node_ids)) != len(result.completed_node_ids):
        raise ValueError("sandbox completion contains duplicate node IDs")
