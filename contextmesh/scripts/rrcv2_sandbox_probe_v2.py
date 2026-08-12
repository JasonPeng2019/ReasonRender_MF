#!/usr/bin/env python3
"""Seal individually executed verifier-sandbox probes without any provider access."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Final

from rrc.dispatch_permit import canonical_json
from rrc.sandbox_capability import SandboxLimits

CONTEXT: Final = "colima-rrcv2-verifier"
CAP: Final = 1024 * 1024
EXPECTED_PROBES: Final = SandboxLimits().required_probes


class ProbeError(RuntimeError):
    """One sandbox property was not actually observed."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


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
    finally:
        temp.unlink(missing_ok=True)


def _sealed_raw(path: Path) -> bytes:
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size > 4 * 1024 * 1024
    ):
        raise ProbeError("sandbox evidence v2 is not a bounded mode-0600 regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or (before.st_dev, before.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise ProbeError("sandbox evidence v2 changed while opening")
        chunks: list[bytes] = []
        total = 0
        while total <= 4 * 1024 * 1024:
            chunk = os.read(fd, min(65_536, 4 * 1024 * 1024 + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        if total > 4 * 1024 * 1024 or (current.st_size, current.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ProbeError("sandbox evidence v2 changed or exceeded cap")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _base(image: str, name: str, code: str, *, pids: int = 1) -> tuple[str, ...]:
    limits = SandboxLimits()
    seccomp = Path("contextmesh/docker/rrcv2-verifier-seccomp.json").resolve()
    return (
        "docker",
        "--context",
        CONTEXT,
        "run",
        "--rm",
        "--name",
        name,
        "--network=none",
        "--read-only",
        "--cpus=1",
        f"--memory={limits.memory_bytes}",
        f"--memory-swap={limits.memory_swap_bytes}",
        f"--pids-limit={pids}",
        f"--ulimit=nofile={limits.file_descriptors}:{limits.file_descriptors}",
        f"--ulimit=fsize={limits.created_file_bytes}:{limits.created_file_bytes}",
        f"--ulimit=cpu={limits.cpu_seconds}:{limits.cpu_seconds}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--security-opt=seccomp={seccomp}",
        f"--tmpfs=/scratch:rw,noexec,nosuid,nodev,mode=1777,size={limits.scratch_bytes}",
        "--workdir=/scratch",
        image,
        "python",
        "-I",
        "-c",
        code,
    )


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run(
    argv: tuple[str, ...], *, timeout: float, stdout_cap: int = CAP, stderr_cap: int = CAP
) -> tuple[int, bytes, bytes, bool, str | None]:
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env={
            "HOME": os.environ["HOME"],
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    values = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    failure: str | None = None
    timed_out = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                failure = "wall_timeout"
                break
            for key, _mask in selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                name = str(key.data)
                values[name].extend(chunk)
                cap = stdout_cap if name == "stdout" else stderr_cap
                if len(values[name]) > cap:
                    failure = f"{name}_overflow"
                    break
            if failure:
                break
        if failure:
            _kill_group(process)
        remaining = max(0.1, deadline - time.monotonic())
        returncode = process.wait(timeout=remaining if not timed_out else 2)
    finally:
        selector.close()
        if process.poll() is None:
            _kill_group(process)
            process.wait(timeout=2)
    return returncode, bytes(values["stdout"]), bytes(values["stderr"]), timed_out, failure


def _remove_container(name: str) -> None:
    subprocess.run(
        ("docker", "--context", CONTEXT, "rm", "-f", name),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )


def _json_pass(stdout: bytes) -> dict[str, object]:
    try:
        value = json.loads(stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeError("probe output is not JSON") from exc
    if not isinstance(value, dict) or value.get("passed") is not True:
        raise ProbeError("probe did not report passed")
    return value


def _success_probe(image: str, probe: str, code: str) -> dict[str, object]:
    name = f"rrcv2-{probe.replace('_', '-')}-v2"
    argv = _base(image, name, code)
    rc, stdout, stderr, timed_out, failure = _run(argv, timeout=20)
    if rc != 0 or timed_out or failure is not None:
        raise ProbeError(f"{probe} failed: rc={rc} failure={failure} stderr={stderr[:200]!r}")
    observation = _json_pass(stdout)
    return {
        "name": probe,
        "status": "passed",
        "argv_sha256": _sha(canonical_json(list(argv))),
        "returncode": rc,
        "stdout_sha256": _sha(stdout),
        "stderr_sha256": _sha(stderr),
        "observation": observation,
    }


def _expected_failure_probe(
    image: str, probe: str, code: str, *, timeout: float = 20
) -> dict[str, object]:
    name = f"rrcv2-{probe.replace('_', '-')}-v2"
    argv = _base(image, name, code)
    started = time.monotonic()
    rc, stdout, stderr, timed_out, failure = _run(argv, timeout=timeout)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    _remove_container(name)
    if probe == "wall_timeout":
        passed = timed_out and failure == "wall_timeout" and elapsed_ms < 10_000
    elif probe in {"stdout_limit", "stderr_limit"}:
        passed = failure == f"{probe.removesuffix('_limit')}_overflow" and not timed_out
    else:
        passed = rc != 0 and not timed_out
    if not passed:
        raise ProbeError(
            f"{probe} boundary was not observed: rc={rc} timeout={timed_out} failure={failure}"
        )
    return {
        "name": probe,
        "status": "passed",
        "argv_sha256": _sha(canonical_json(list(argv))),
        "returncode": rc,
        "stdout_sha256": _sha(stdout),
        "stderr_sha256": _sha(stderr),
        "observation": {
            "elapsed_ms": elapsed_ms,
            "failure": failure,
            "timed_out": timed_out,
            "captured_stdout_bytes": len(stdout),
            "captured_stderr_bytes": len(stderr),
        },
    }


def _probe_rows(image: str) -> list[dict[str, object]]:
    programs = {
        "capabilities_dropped": "import json; s=open('/proc/self/status').read(); print(json.dumps({'passed':'CapEff:\\t0000000000000000' in s}))",
        "cgroup_v2": "import json,os; print(json.dumps({'passed':os.path.isfile('/sys/fs/cgroup/cgroup.controllers')}))",
        "credential_environment_removed": "import json,os; ks=('OPENAI_API_KEY','OLLAMA_API_KEY','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY'); print(json.dumps({'passed':not any(k in os.environ for k in ks)}))",
        "external_read_denied": "import json; ok=False\ntry: open('/host-secret').read()\nexcept OSError: ok=True\nprint(json.dumps({'passed':ok}))",
        "external_write_denied": "import json; ok=False\ntry: open('/etc/rrcv2-write','w').write('x')\nexcept OSError: ok=True\nprint(json.dumps({'passed':ok}))",
        "fd_limit": "import json,resource; print(json.dumps({'passed':resource.getrlimit(resource.RLIMIT_NOFILE)[0]==64}))",
        "file_size_limit": "import json,os; ok=False\ntry:\n f=open('/scratch/large','wb'); f.write(b'x'*(4194305)); f.flush()\nexcept OSError: ok=True\nsize=os.path.getsize('/scratch/large') if os.path.exists('/scratch/large') else -1\nprint(json.dumps({'passed':ok and size<=4194304,'size':size}))",
        "loopback_denied": "import json,socket; ok=False\ntry: socket.socket(socket.AF_INET,socket.SOCK_STREAM)\nexcept OSError: ok=True\nprint(json.dumps({'passed':ok}))",
        "network_denied": "import json,socket; ok=False\ntry: socket.create_connection(('1.1.1.1',80),.2)\nexcept OSError: ok=True\nprint(json.dumps({'passed':ok}))",
        "no_new_privileges": "import json; s=open('/proc/self/status').read(); print(json.dumps({'passed':'NoNewPrivs:\\t1' in s}))",
        "process_limit": "import json,os; ok=False\ntry: os.fork()\nexcept OSError: ok=True\nprint(json.dumps({'passed':ok}))",
        "read_only_root": "import json; ok=False\ntry: open('/rrcv2-root-write','w').write('x')\nexcept OSError: ok=True\nprint(json.dumps({'passed':ok}))",
        "scratch_limit": "import json,os; ok=False; n=0; i=0\ntry:\n while True:\n  f=open(f'/scratch/fill-{i}','wb'); f.write(b'x'*4194304); f.flush(); f.close(); n+=4194304; i+=1\nexcept OSError: ok=True\nprint(json.dumps({'passed':ok and n==33554432,'written':n,'files':i}))",
        "seccomp_socket_denied": "import json,socket; s=open('/proc/self/status').read(); ok=False\ntry: socket.socket()\nexcept OSError: ok=True\nprint(json.dumps({'passed':ok and 'Seccomp:\\t2' in s}))",
    }
    rows = [_success_probe(image, name, code) for name, code in programs.items()]
    rows.append(
        _expected_failure_probe(
            image,
            "cpu_limit",
            "x=0\nwhile True: x+=1",
            timeout=16,
        )
    )
    rows.append(
        _expected_failure_probe(
            image,
            "memory_limit",
            "x=bytearray(600*1024*1024); print(len(x))",
        )
    )
    rows.append(
        _expected_failure_probe(
            image,
            "stdout_limit",
            "import os; os.write(1,b'x'*(1048577))",
        )
    )
    rows.append(
        _expected_failure_probe(
            image,
            "stderr_limit",
            "import os; os.write(2,b'x'*(1048577))",
        )
    )
    rows.append(
        _expected_failure_probe(image, "wall_timeout", "import time; time.sleep(30)", timeout=2)
    )
    cleanup_name = "rrcv2-process-tree-cleanup-v2"
    cleanup_argv = _base(
        image,
        cleanup_name,
        "import os,time\npid=os.fork()\nif pid==0: time.sleep(60)\ntime.sleep(60)",
        pids=64,
    )
    rc, stdout, stderr, timed_out, failure = _run(cleanup_argv, timeout=2)
    _remove_container(cleanup_name)
    inspect = subprocess.run(
        ("docker", "--context", CONTEXT, "inspect", cleanup_name),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if not timed_out or failure != "wall_timeout" or inspect.returncode == 0:
        raise ProbeError("process tree cleanup did not remove the timed-out container")
    rows.append(
        {
            "name": "process_tree_cleanup",
            "status": "passed",
            "argv_sha256": _sha(canonical_json(list(cleanup_argv))),
            "returncode": rc,
            "stdout_sha256": _sha(stdout),
            "stderr_sha256": _sha(stderr),
            "observation": {
                "timed_out": timed_out,
                "failure": failure,
                "container_absent_after_cleanup": True,
            },
        }
    )
    rows.sort(key=lambda row: str(row["name"]))
    if tuple(str(row["name"]) for row in rows) != EXPECTED_PROBES:
        raise ProbeError("probe set is missing, extra, duplicated, or reordered")
    return rows


def produce(repo: Path) -> dict[str, object]:
    if (
        os.environ.get("RRD_VERIFY_GUARD_ACTIVE") != "1"
        or os.environ.get("RRD_VERIFY_MODEL_BEARING") != "1"
    ):
        raise ProbeError("sandbox probe requires the marked verification guard")
    lock = json.loads((repo / "contextmesh/docker/rrcv2-verifier.lock.json").read_text())
    image = lock.get("image_config_digest")
    if not isinstance(image, str) or not image.startswith("sha256:"):
        raise ProbeError("verifier lock lacks an image digest")
    rows = _probe_rows(image)
    value = {
        "v": 2,
        "kind": "rrcv2_sandbox_capability",
        "backend": "colima-docker",
        "docker_context": CONTEXT,
        "limits": SandboxLimits().as_dict(),
        "probes": rows,
        "provider_launch_total": 0,
    }
    path = repo / ".generated/state/rrcv2-convergence/verify/sandbox-evidence.v2.json"
    if path.exists() or path.is_symlink():
        raise ProbeError("sandbox evidence v2 already exists")
    _atomic_0600(path, canonical_json(value))
    return value


def validate(repo: Path) -> dict[str, object]:
    path = repo / ".generated/state/rrcv2-convergence/verify/sandbox-evidence.v2.json"
    raw = _sealed_raw(path)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeError("sandbox evidence v2 is invalid JSON") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise ProbeError("sandbox evidence v2 is not canonical")
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
        or value.get("backend") != "colima-docker"
        or value.get("docker_context") != CONTEXT
        or value.get("limits") != SandboxLimits().as_dict()
        or value.get("provider_launch_total") != 0
    ):
        raise ProbeError("sandbox evidence v2 identity/limits differ")
    rows = value.get("probes")
    if (
        not isinstance(rows, list)
        or tuple(row.get("name") for row in rows if isinstance(row, dict)) != EXPECTED_PROBES
    ):
        raise ProbeError("sandbox evidence v2 probe set differs")
    row_fields = {
        "name",
        "status",
        "argv_sha256",
        "returncode",
        "stdout_sha256",
        "stderr_sha256",
        "observation",
    }
    for row in rows:
        if not isinstance(row, dict) or set(row) != row_fields or row.get("status") != "passed":
            raise ProbeError("sandbox evidence v2 has malformed probe row")
        for field in ("argv_sha256", "stdout_sha256", "stderr_sha256"):
            digest = row.get(field)
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ProbeError("sandbox evidence v2 has invalid digest")
        if isinstance(row.get("returncode"), bool) or not isinstance(row.get("returncode"), int):
            raise ProbeError("sandbox evidence v2 has invalid return code")
        observation = row.get("observation")
        if not isinstance(observation, dict):
            raise ProbeError("sandbox evidence v2 lacks an observation")
        name = row["name"]
        if name in {"stdout_limit", "stderr_limit"}:
            pass
        elif name in {
            "cpu_limit",
            "memory_limit",
            "wall_timeout",
            "process_tree_cleanup",
        }:
            if row["returncode"] == 0:
                raise ProbeError(f"sandbox negative probe did not fail: {name}")
        elif observation.get("passed") is not True or row["returncode"] != 0:
            raise ProbeError(f"sandbox positive probe did not pass: {name}")
    by_name = {str(row["name"]): row for row in rows}
    if by_name["file_size_limit"]["observation"].get("size") != 4_194_304:
        raise ProbeError("file-size probe did not reach the exact cap")
    if by_name["scratch_limit"]["observation"].get("written") != 33_554_432:
        raise ProbeError("scratch probe did not reach the exact cap")
    for name in ("cpu_limit", "memory_limit"):
        observation = by_name[name]["observation"]
        if observation.get("timed_out") is not False or observation.get("failure") is not None:
            raise ProbeError(
                f"{name} was a wall/controller failure rather than resource enforcement"
            )
    for name in ("stdout_limit", "stderr_limit"):
        stream = name.removesuffix("_limit")
        observation = by_name[name]["observation"]
        if (
            observation.get("failure") != f"{stream}_overflow"
            or observation.get(f"captured_{stream}_bytes") != CAP + 1
        ):
            raise ProbeError(f"{name} did not cross its exact boundary")
    if by_name["wall_timeout"]["observation"].get("failure") != "wall_timeout":
        raise ProbeError("wall timeout was not observed")
    cleanup = by_name["process_tree_cleanup"]["observation"]
    if cleanup.get("container_absent_after_cleanup") is not True:
        raise ProbeError("process tree remained after cleanup")
    return value


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", nargs="?", choices=("produce", "validate-sealed"), default="produce"
    )
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    value = produce(repo) if args.mode == "produce" else validate(repo)
    print(json.dumps({"status": "passed", "sha256": _sha(canonical_json(value))}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
