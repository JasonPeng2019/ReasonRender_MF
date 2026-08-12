from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "contextmesh/scripts/rrcv2_capability_preflight.py"


def _load():
    spec = importlib.util.spec_from_file_location("rrcv2_capability_preflight_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    docker = repo / "contextmesh/docker"
    docker.mkdir(parents=True)
    for name, body in (
        ("rrcv2-verifier.Dockerfile", "FROM scratch\n"),
        ("rrcv2-verifier-requirements.txt", "pytest==9.1.1 --hash=sha256:" + "a" * 64 + "\n"),
        (
            "rrcv2-verifier-seccomp.json",
            json.dumps(
                {
                    "defaultAction": "SCMP_ACT_ERRNO",
                    "syscalls": [
                        {
                            "names": [
                                "socket",
                                "socketpair",
                                "connect",
                                "bind",
                                "listen",
                                "accept",
                                "accept4",
                            ],
                            "action": "SCMP_ACT_ERRNO",
                            "errnoRet": 1,
                        }
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        ),
    ):
        (docker / name).write_text(body)
    (repo / "contextmesh/.codex-rrd-native").mkdir(parents=True)
    (repo / "contextmesh/.codex-rrd-native").chmod(0o700)
    return repo


class FakeRunner:
    def __init__(self, module, *, already_running: bool = False) -> None:
        self.module = module
        self.already_running = already_running
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...], timeout: int):
        del timeout
        self.calls.append(argv)
        if argv[:3] == ("colima", "status", "rrcv2-verifier"):
            if not self.already_running:
                return self.module.CommandResult(1, b"", b"not running")
            return self.module.CommandResult(
                0,
                b'{"status":"Running","runtime":"docker","arch":"x86_64"}\n',
                b"",
            )
        if argv[:2] == ("colima", "start"):
            self.already_running = True
            return self.module.CommandResult(0, b"started\n", b"")
        if argv[:2] == ("colima", "stop"):
            self.already_running = False
            return self.module.CommandResult(0, b"", b"")
        if argv[:2] == ("colima", "delete"):
            return self.module.CommandResult(0, b"", b"")
        if argv[:4] == ("docker", "--context", "colima-rrcv2-verifier", "context"):
            return self.module.CommandResult(
                0,
                b'[{"Name":"colima-rrcv2-verifier","Endpoints":{"docker":{"Host":"unix:///owned.sock"}}}]\n',
                b"",
            )
        if argv[:4] == ("docker", "--context", "colima-rrcv2-verifier", "build"):
            return self.module.CommandResult(0, b"built\n", b"")
        if argv[:4] == ("docker", "--context", "colima-rrcv2-verifier", "image"):
            return self.module.CommandResult(
                0,
                b'[{"Id":"sha256:'
                + b"b" * 64
                + b'","RepoDigests":["rrcv2@sha256:'
                + b"c" * 64
                + b'"]}]\n',
                b"",
            )
        if argv[:4] == ("docker", "--context", "colima-rrcv2-verifier", "info"):
            return self.module.CommandResult(
                0, b'{"CgroupVersion":"2","OSType":"linux","Architecture":"x86_64"}\n', b""
            )
        if argv[:4] == ("docker", "--context", "colima-rrcv2-verifier", "run"):
            return self.module.CommandResult(0, b'{"all_probes_passed":true}\n', b"")
        if argv[0] == "colima" and argv[1] == "version":
            return self.module.CommandResult(0, b"colima version 0.10.1\n", b"")
        raise AssertionError(argv)


def _env(repo: Path) -> dict[str, str]:
    return {
        "HOME": str(repo / "home"),
        "CODEX_HOME": str(repo / "contextmesh/.codex-rrd-native"),
        "RRD_VERIFY_GUARD_ACTIVE": "1",
        "RRD_VERIFY_MODEL_BEARING": "1",
        "RRD_REQUIRED_CODEX_VERSION": "codex-cli 0.147.0",
    }


def test_produce_uses_only_exact_colima_profile_and_qualified_docker(tmp_path: Path) -> None:
    module = _load()
    repo = _fixture_repo(tmp_path)
    runner = FakeRunner(module)

    result = module.produce(repo=repo, environ=_env(repo), runner=runner)

    assert result["kind"] == "rrcv2_capability_preflight"
    assert result["provider_launch_total"] == 0
    assert any(call == module.COLIMA_START_ARGV for call in runner.calls)
    docker_calls = [call for call in runner.calls if call[0] == "docker"]
    assert docker_calls
    assert all(
        call[:3] == ("docker", "--context", "colima-rrcv2-verifier") for call in docker_calls
    )
    state = repo / ".generated/state/rrcv2-convergence/capability"
    for name in (
        "backend-ownership.v1.json",
        "apfs-evidence.v1.json",
        "docker-evidence.v1.json",
        "sandbox-evidence.v1.json",
    ):
        path = state / name
        assert path.is_file()
        assert path.stat().st_mode & 0o777 == 0o600
    assert (repo / "contextmesh/docker/rrcv2-verifier.lock.json").is_file()


def test_second_produce_fails_before_host_mutation(tmp_path: Path) -> None:
    module = _load()
    repo = _fixture_repo(tmp_path)
    first = FakeRunner(module)
    module.produce(repo=repo, environ=_env(repo), runner=first)
    second = FakeRunner(module, already_running=True)

    with pytest.raises(module.PreflightError, match="already exists"):
        module.produce(repo=repo, environ=_env(repo), runner=second)
    assert second.calls == []


def test_missing_outer_guard_rejects_without_command(tmp_path: Path) -> None:
    module = _load()
    repo = _fixture_repo(tmp_path)
    runner = FakeRunner(module)
    env = _env(repo)
    env.pop("RRD_VERIFY_GUARD_ACTIVE")

    with pytest.raises(module.PreflightError, match="marked verification guard"):
        module.produce(repo=repo, environ=env, runner=runner)
    assert runner.calls == []


def test_validate_sealed_is_zero_mutation_and_rejects_tamper(tmp_path: Path) -> None:
    module = _load()
    repo = _fixture_repo(tmp_path)
    producer = FakeRunner(module)
    module.produce(repo=repo, environ=_env(repo), runner=producer)
    validator = FakeRunner(module, already_running=True)

    result = module.validate_sealed(repo=repo, environ=_env(repo), runner=validator)
    assert result["provider_launch_total"] == 0
    assert not any(
        call[:2] in {("colima", "start"), ("colima", "stop")} for call in validator.calls
    )

    sandbox = repo / ".generated/state/rrcv2-convergence/capability/sandbox-evidence.v1.json"
    sandbox.write_bytes(sandbox.read_bytes() + b" ")
    with pytest.raises(module.PreflightError):
        module.validate_sealed(
            repo=repo, environ=_env(repo), runner=FakeRunner(module, already_running=True)
        )


def test_owned_close_stops_and_deletes_exact_profile(tmp_path: Path) -> None:
    module = _load()
    repo = _fixture_repo(tmp_path)
    producer = FakeRunner(module)
    module.produce(repo=repo, environ=_env(repo), runner=producer)
    closer = FakeRunner(module, already_running=True)

    result = module.close(repo=repo, environ=_env(repo), runner=closer)

    assert result["status"] == "closed"
    assert ("colima", "stop", "rrcv2-verifier") in closer.calls
    assert ("colima", "delete", "rrcv2-verifier", "--force", "--data") in closer.calls
    assert not (
        repo / ".generated/state/rrcv2-convergence/capability/backend-ownership.v1.json"
    ).exists()


def test_preflight_refuses_ambient_docker_context_variables(tmp_path: Path) -> None:
    module = _load()
    repo = _fixture_repo(tmp_path)
    env = _env(repo)
    env["DOCKER_CONTEXT"] = "default"
    runner = FakeRunner(module)
    with pytest.raises(module.PreflightError, match="DOCKER_CONTEXT"):
        module.produce(repo=repo, environ=env, runner=runner)
    assert runner.calls == []
