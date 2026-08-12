from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SHARED_PATHS = ("src/models.js", "src/utils.js", "src/middleware.js")
ROOT = Path(__file__).resolve().parents[1]


def _product_hook_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    scripts = ROOT / "contextmesh/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    guard = importlib.import_module("rrcv2_product_guard")
    smoke = importlib.import_module("rrcv2_product_smoke")

    repo = tmp_path / "repo"
    hook = repo / "contextmesh/scripts/rrd_codex_hook.py"
    hook.parent.mkdir(parents=True)
    hook.write_bytes((ROOT / "contextmesh/scripts/rrd_codex_hook.py").read_bytes())
    producer = (
        repo
        / ".generated/state/rrcv2-convergence/verify/cli-smoke/rrcv2-cli-smoke-v20"
        / guard.PRODUCER_SHA256
    )
    round_root = producer / "round"
    for relative in ("tmp", "cancellation", "cancellation/children", "miss"):
        (round_root / relative).mkdir(parents=True, exist_ok=True)
    base = guard.product_environment(
        repo=repo,
        native_home=repo / "contextmesh/.codex-rrd-native",
        user_home=Path.home(),
        codex_bin=(Path.home() / ".local/bin/codex").resolve(strict=True),
        uv_bin=Path("/usr/local/bin/uv").resolve(strict=True),
        round_root=round_root,
        producer_root=producer,
        nonce="a" * 32,
    )
    task = smoke._task(
        guard.load_fixture(ROOT / "tests/fixtures/rrcv2_cli_smoke/manifest.json")["cases"][0]
    )  # noqa: SLF001
    environment = smoke._cell_environment(  # noqa: SLF001
        base, cell=round_root / "miss", task=task, prompt=b"prompt"
    )
    environment["PYTHONPATH"] = str(ROOT)
    return environment


def _run_product_hook(
    raw: bytes, environment: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            Path(environment["RRD_REPO_ROOT"]) / "contextmesh/scripts/rrd_codex_hook.py",
        ],
        input=raw,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        timeout=10,
        check=False,
    )


def test_hook_cli_self_bootstraps_the_exact_repository_before_pythonpath(
    tmp_path: Path,
) -> None:
    script = ROOT / "contextmesh/scripts/rrd_codex_hook.py"
    poison = tmp_path / "poison"
    (poison / "rrc").mkdir(parents=True)
    (poison / "rrc/__init__.py").write_text("raise RuntimeError('poison rrc imported')\n")
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_input": {"message": "Compare src/handlers/users.js and src/handlers/orders.js"},
        }
    )
    base = {
        "HOME": str(tmp_path),
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TERM": "dumb",
        "TMPDIR": str(tmp_path),
        "PYTHONNOUSERSITE": "1",
    }

    for environment in (
        base,
        {**base, "PYTHONPATH": os.pathsep.join((str(poison), str(ROOT)))},
    ):
        completed = subprocess.run(
            [sys.executable, str(script)],
            input=payload,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=environment,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        output = json.loads(completed.stdout)["hookSpecificOutput"]
        assert output["hookEventName"] == "PreToolUse"
        assert output["permissionDecision"] == "deny"
        assert "exactly one" in output["permissionDecisionReason"]


def test_hook_repository_bootstrap_keeps_one_exact_index_zero_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextmesh.scripts import rrd_codex_hook

    repository = str(ROOT)
    monkeypatch.setattr(sys, "path", ["/poison", repository, "/other", repository])
    assert rrd_codex_hook._install_repository_path() == ROOT  # noqa: SLF001
    assert sys.path[0] == repository
    assert sys.path.count(repository) == 1


class MemoryServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        self.records: dict[str, str] = {}
        super().__init__(("127.0.0.1", 0), MemoryHandler)


class MemoryHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload: dict[str, Any] = json.loads(self.rfile.read(length))
        if self.path.endswith("/add"):
            key = str(payload["session_id"])
            self.server.records[key] = payload["messages"][0]["content"]  # type: ignore[attr-defined]
            value = {"data": {"status": "accumulated"}}
        else:
            key = str(payload["filters"]["session_id"])
            content = self.server.records.get(key)  # type: ignore[attr-defined]
            messages = [] if content is None else [{"content": content}]
            value = {"data": {"unprocessed_messages": messages}}
        encoded = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def target(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    (root / "src/handlers").mkdir(parents=True)
    (root / "src/handlers/users.js").write_text("export function users() { return true }\n")
    for name in ("models", "utils", "middleware"):
        (root / f"src/{name}.js").write_text(
            "\n".join(
                f"export function {name}_{index}() {{ return {index} }}" for index in range(120)
            )
            + "\n"
        )
    return root


def test_legacy_audit_assignment_never_enters_rrcv2_packet_bridge(
    tmp_path: Path, target: Path, monkeypatch
) -> None:
    from contextmesh.scripts.rrd_codex_hook import handle

    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("RRD_TARGET_ROOT", str(target))
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(events))
    monkeypatch.setenv("RRC_CONTROL", "deterministic")
    monkeypatch.setenv("RRD_ENABLE_RRC", "1")
    monkeypatch.setenv("RRD_ENABLE_CONTEXTMESH", "0")
    monkeypatch.setenv("EXAMPLE_API_KEY", "SECRET-MUST-NOT-BE-LOGGED")
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "root",
        "tool_name": "spawn_agent",
        "tool_use_id": "call-users",
        "tool_input": {
            "message": "Audit src/handlers/users.js and report findings.",
            "agent_type": "worker",
            "fork_context": False,
        },
    }

    output = handle(payload)

    assert output is not None
    updated = output["hookSpecificOutput"]["updatedInput"]  # type: ignore[index]
    assert updated["agent_type"] == "worker"
    assert updated["fork_context"] is False
    assert updated["message"] == "Audit src/handlers/users.js and report findings."
    assert "ContextMesh" not in updated["message"]
    assert "export function users" not in updated["message"]
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert {row["event"] for row in rows} == {"assignment"}
    assert "SECRET-MUST-NOT-BE-LOGGED" not in events.read_text()
    assert stat.S_IMODE(events.stat().st_mode) == 0o600


def test_contextmesh_spawn_delivers_one_line_indexed_source_bundle_without_digest(
    tmp_path: Path, target: Path, monkeypatch
) -> None:
    from contextmesh.scripts import rrd_codex_hook

    manifest = tmp_path / "manifest.json"
    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("RRD_TARGET_ROOT", str(target))
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(events))
    monkeypatch.setenv("RRD_SUMMARY_MODE", "deterministic")
    monkeypatch.setenv("RRD_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("RRD_SEED_MANIFEST", str(manifest))
    monkeypatch.setenv("RRD_ENABLE_CONTEXTMESH", "1")
    monkeypatch.setenv("RRD_ENABLE_RRC", "0")
    assert (
        rrd_codex_hook._seed(
            argparse.Namespace(
                target_root=target,
                round_id="rrd-sqlite-bundle",
                arm="a",
                manifest=manifest,
                memory_backend="sqlite",
            )
        )
        == 0
    )

    output = rrd_codex_hook.handle(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "root",
            "tool_name": "spawn_agent",
            "tool_use_id": "call-users",
            "tool_input": {
                "message": "Audit src/handlers/users.js and report findings.",
                "agent_type": "worker",
                "fork_context": False,
            },
        }
    )

    assert output is not None
    message = output["hookSpecificOutput"]["updatedInput"]["message"]  # type: ignore[index]
    assert "UNTRUSTED_CONTEXTMESH_DIGEST" not in message
    assert "ReasonRenderCoding validated packet" not in message
    for relative in ("src/handlers/users.js", *SHARED_PATHS):
        assert message.count(f"path={relative} ") == 1
    assert "L1:export function users() { return true }" in message
    assert "L120:export function models_119() { return 119 }" in message
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    bundle = next(row for row in rows if row["event"] == "source_bundle")
    assert len(bundle["files"]) == 4
    assert all(row["final_newline"] is True for row in bundle["files"])
    assert all(row["line_count"] > 0 for row in bundle["files"])


def test_line_indexed_bundle_is_byte_reversible_for_mixed_newlines() -> None:
    from contextmesh.scripts import rrd_codex_hook

    raw = b"alpha\r\nbeta\ngamma"
    indexed, line_count, final_newline = rrd_codex_hook._line_indexed(raw)
    reconstructed = re.sub(r"(?m)^L\d+:", "", indexed).encode("utf-8")

    assert reconstructed == raw
    assert line_count == 3
    assert final_newline is False


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"[]",
        json.dumps({"hook_event_name": "Unknown"}).encode(),
        b"x" * 2_000_001,
    ],
)
def test_product_hook_malformed_unknown_and_oversize_inputs_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    environment = _product_hook_environment(tmp_path, monkeypatch)
    completed = _run_product_hook(raw, environment)

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    value = json.loads(completed.stdout)
    output = value["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"


def test_product_hook_rejects_unknown_prefix_and_literal_mutation_before_tool_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextmesh.scripts import rrd_codex_hook

    environment = _product_hook_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(
        rrd_codex_hook,
        "__file__",
        str(Path(environment["RRD_REPO_ROOT"]) / "contextmesh/scripts/rrd_codex_hook.py"),
    )
    validated = rrd_codex_hook._validated_product_environment(environment)  # noqa: SLF001
    assert "PYTHONPATH" not in validated
    assert validated["RRD_REPO_ROOT"] == environment["RRD_REPO_ROOT"]
    for mutation in (
        {"RRC_SURPRISE": "1"},
        {"RRD_WORKER_MODEL": "gpt-5.5"},
        {"RRCV2_PRODUCT_SMOKE": "0"},
    ):
        poisoned = {**environment, **mutation}
        if mutation.get("RRCV2_PRODUCT_SMOKE") == "0":
            with pytest.raises(rrd_codex_hook.HookError, match="literal"):
                rrd_codex_hook._validated_product_environment(poisoned)  # noqa: SLF001
            continue
        completed = _run_product_hook(
            json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "exec_command",
                    "tool_input": {"cmd": "touch forbidden-sentinel"},
                }
            ).encode(),
            poisoned,
        )
        assert completed.returncode == 0
        assert json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_product_hook_non_tool_lifecycle_exception_is_terminal_not_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _product_hook_environment(tmp_path, monkeypatch)
    completed = _run_product_hook(
        json.dumps({"hook_event_name": "SubagentStart", "agent_id": "missing"}).encode(),
        environment,
    )
    assert completed.returncode == 1
    assert completed.stdout == b""


def test_product_run_group_publishes_before_release_and_marks_terminal(
    tmp_path: Path,
) -> None:
    from contextmesh.scripts import rrd_codex_hook

    registry = tmp_path / "children"
    registry.mkdir(mode=0o700)
    sentinel = tmp_path / "ran"
    environment = {
        **os.environ,
        "RRCV2_PRODUCT_SMOKE": "1",
        "RRCV2_SMOKE_RUN_NONCE": "c" * 32,
        "RRCV2_SMOKE_CHILD_REGISTRY": str(registry),
        "RRCV2_SMOKE_CANCELLATION_REQUEST": str(tmp_path / "request"),
    }
    completed = rrd_codex_hook._run_group(  # noqa: SLF001
        [sys.executable, "-c", f"from pathlib import Path; Path({str(sentinel)!r}).touch()"],
        timeout=5,
        env=environment,
        cwd=tmp_path,
    )
    assert completed.returncode == 0
    assert sentinel.exists()
    rows = [json.loads(path.read_bytes()) for path in registry.iterdir()]
    assert len(rows) == 1
    assert rows[0]["state"] == "terminal"
    assert rows[0]["returncode"] == 0
    assert rows[0]["nonce"] == "c" * 32


def test_product_run_group_registration_failure_never_releases_child(tmp_path: Path) -> None:
    from contextmesh.scripts import rrd_codex_hook

    registry = tmp_path / "children"
    registry.write_text("not a directory")
    sentinel = tmp_path / "ran"
    environment = {
        **os.environ,
        "RRCV2_PRODUCT_SMOKE": "1",
        "RRCV2_SMOKE_RUN_NONCE": "d" * 32,
        "RRCV2_SMOKE_CHILD_REGISTRY": str(registry),
        "RRCV2_SMOKE_CANCELLATION_REQUEST": str(tmp_path / "request"),
    }
    with pytest.raises(rrd_codex_hook.HookError, match="registry"):
        rrd_codex_hook._run_group(  # noqa: SLF001
            [sys.executable, "-c", f"from pathlib import Path; Path({str(sentinel)!r}).touch()"],
            timeout=5,
            env=environment,
            cwd=tmp_path,
        )
    assert not sentinel.exists()


def test_contextmesh_worker_source_tool_attempt_is_denied_and_recorded(
    tmp_path: Path, monkeypatch
) -> None:
    from contextmesh.scripts import rrd_codex_hook

    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(events))
    monkeypatch.setenv("RRD_ENABLE_CONTEXTMESH", "1")

    with pytest.raises(rrd_codex_hook.PolicyError, match="already delivered"):
        rrd_codex_hook.handle(
            {
                "hook_event_name": "PreToolUse",
                "agent_id": "worker-users",
                "agent_type": "worker",
                "tool_name": "exec_command",
                "tool_use_id": "read-users",
                "tool_input": {"cmd": "sed -n '1,200p' src/handlers/users.js"},
            }
        )

    row = json.loads(events.read_text())
    assert row["event"] == "source_reread_violation"
    assert row["agent_id"] == "worker-users"


def test_rrcv2_root_denies_direct_reads_and_allows_only_exact_result_reader(
    tmp_path: Path, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextmesh.scripts import rrd_codex_hook

    events = tmp_path / "events.jsonl"
    repository = Path(__file__).parents[1].resolve()
    uv = tmp_path / "uv"
    database = tmp_path / "rrcv2.sqlite3"
    monkeypatch.setenv("RRD_ENABLE_RRC", "1")
    monkeypatch.setenv("RRD_ENABLE_CONTEXTMESH", "1")
    monkeypatch.setenv("RRC_DEMO_DATABASE", str(database))
    monkeypatch.setenv("RRD_REPO_ROOT", str(repository))
    monkeypatch.setenv("RRD_TARGET_ROOT", str(target))
    monkeypatch.setenv("RRC_DEMO_UV_BIN", str(uv))
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(events))

    with pytest.raises(rrd_codex_hook.PolicyError, match="exact confined result-reader"):
        rrd_codex_hook.handle(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "exec_command",
                "tool_use_id": "direct-read",
                "tool_input": {"cmd": "cat results/accepted-code.v1.utf8"},
            }
        )

    command = (
        f"{uv} run --locked --project {repository / 'pyproject.toml'} python "
        f"{repository / 'contextmesh/scripts/rrd_result_reader.py'} apply "
        f"--attempt-id {'a' * 64} --receipt {'b' * 64}"
    )
    assert (
        rrd_codex_hook.handle(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "exec_command",
                "tool_use_id": "apply-result",
                "tool_input": {"cmd": command, "workdir": str(target)},
            }
        )
        is None
    )
    with pytest.raises(rrd_codex_hook.PolicyError, match="exact confined result-reader"):
        rrd_codex_hook.handle(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "exec_command",
                "tool_use_id": "wrong-result",
                "tool_input": {"cmd": command + " --database /tmp/foreign"},
            }
        )

    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert [row["event"] for row in rows] == [
        "rrcv2_result_reader_allowed",
    ]


def test_active_hook_contains_no_legacy_packet_resolver() -> None:
    from contextmesh.scripts import rrd_codex_hook

    source = Path(rrd_codex_hook.__file__).read_text()
    assert "_resolve_packet" not in source
    assert "_deterministic_packet" not in source
    assert "PlanSpecPacket" not in source


def test_pretool_denies_ambiguous_handler_assignment(
    target: Path, tmp_path: Path, monkeypatch
) -> None:
    from contextmesh.scripts.rrd_codex_hook import PolicyError, handle

    monkeypatch.setenv("RRD_TARGET_ROOT", str(target))
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(tmp_path / "events.jsonl"))
    with pytest.raises(PolicyError, match="exactly one"):
        handle(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "spawn_agent",
                "tool_input": {
                    "message": "Compare src/handlers/users.js and src/handlers/orders.js"
                },
            }
        )


def test_hook_cli_emits_the_installed_codex_pretool_deny_schema(
    target: Path, tmp_path: Path
) -> None:
    script = Path(__file__).parents[1] / "contextmesh/scripts/rrd_codex_hook.py"
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "spawn_agent",
        "tool_input": {"message": "Compare src/handlers/users.js and src/handlers/orders.js"},
    }
    result = subprocess.run(
        ["python3", str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "RRD_TARGET_ROOT": str(target),
            "RRD_HOOK_EVENTS": str(tmp_path / "events.jsonl"),
        },
        check=False,
    )

    assert result.returncode == 0
    output = json.loads(result.stdout)
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert "exactly one" in specific["permissionDecisionReason"]


def test_subagent_start_does_not_reopen_or_redeliver_missing_manifest(
    target: Path, tmp_path: Path
) -> None:
    script = Path(__file__).parents[1] / "contextmesh/scripts/rrd_codex_hook.py"
    result = subprocess.run(
        ["python3", str(script)],
        input=json.dumps(
            {
                "hook_event_name": "SubagentStart",
                "agent_id": "agent-users",
                "agent_type": "worker",
            }
        ),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "RRD_TARGET_ROOT": str(target),
            "RRD_HOOK_EVENTS": str(tmp_path / "events.jsonl"),
            "RRD_SEED_MANIFEST": str(tmp_path / "missing-manifest.json"),
        },
        check=False,
    )

    assert result.returncode == 0
    specific = json.loads(result.stdout)["hookSpecificOutput"]
    assert specific["hookEventName"] == "SubagentStart"
    assert "exact-source bundle" in specific["additionalContext"]
    assert all(path not in specific["additionalContext"] for path in SHARED_PATHS)
    event = json.loads((tmp_path / "events.jsonl").read_text())
    assert event["event"] == "shared_context"


def test_pretool_fifo_manifest_fails_open_without_blocking(target: Path, tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "contextmesh/scripts/rrd_codex_hook.py"
    manifest = tmp_path / "seed-manifest.json"
    os.mkfifo(manifest)
    result = subprocess.run(
        ["python3", str(script)],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "root",
                "tool_name": "spawn_agent",
                "tool_use_id": "call-users",
                "tool_input": {
                    "message": "Audit src/handlers/users.js",
                    "agent_type": "worker",
                    "fork_context": False,
                },
            }
        ),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "RRD_TARGET_ROOT": str(target),
            "RRD_HOOK_EVENTS": str(tmp_path / "events.jsonl"),
            "RRD_SEED_MANIFEST": str(manifest),
            "RRD_MEMORY_BACKEND": "sqlite",
        },
        timeout=2,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    event = json.loads((tmp_path / "events.jsonl").read_text())
    assert event["event"] == "fail_open"
    assert event["hook_event"] == "PreToolUse"


def test_world_readable_seed_manifest_fails_open(target: Path, tmp_path: Path, monkeypatch) -> None:
    from contextmesh.scripts import rrd_codex_hook

    manifest = tmp_path / "seed-manifest.json"
    manifest.write_text('{"v":1,"seal":"invalid"}')
    manifest.chmod(0o644)
    monkeypatch.setenv("RRD_SEED_MANIFEST", str(manifest))

    with pytest.raises(rrd_codex_hook.HookError, match="permissions must be 0600"):
        rrd_codex_hook._manifest()


def test_hook_event_fifo_is_best_effort_and_nonblocking(tmp_path: Path, monkeypatch) -> None:
    from contextmesh.scripts.rrd_codex_hook import _append_event

    events = tmp_path / "events.jsonl"
    os.mkfifo(events)
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(events))
    started = time.monotonic()
    _append_event("fail_open", error="fixture")
    assert time.monotonic() - started < 0.25


def test_pretool_special_shared_source_uses_policy_deny_schema(
    target: Path, tmp_path: Path
) -> None:
    script = Path(__file__).parents[1] / "contextmesh/scripts/rrd_codex_hook.py"
    (target / "src/models.js").unlink()
    os.mkfifo(target / "src/models.js")
    target_stat = target.resolve().stat()
    manifest: dict[str, object] = {
        "v": 1,
        "round_id": "rrd-test",
        "arm": "a",
        "memory_backend": "sqlite",
        "target_root": str(target.resolve()),
        "target_device": target_stat.st_dev,
        "target_inode": target_stat.st_ino,
        "files": [{"path": path} for path in SHARED_PATHS],
    }
    manifest["seal"] = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o600)
    result = subprocess.run(
        ["python3", str(script)],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "root",
                "tool_name": "spawn_agent",
                "tool_use_id": "call-users",
                "tool_input": {
                    "message": "Audit src/handlers/users.js",
                    "agent_type": "worker",
                    "fork_context": False,
                },
            }
        ),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "RRD_TARGET_ROOT": str(target),
            "RRD_HOOK_EVENTS": str(tmp_path / "events.jsonl"),
            "RRD_SEED_MANIFEST": str(manifest_path),
            "RRD_MEMORY_BACKEND": "sqlite",
            "RRD_ENABLE_RRC": "0",
        },
        timeout=2,
        check=False,
    )

    assert result.returncode == 0
    specific = json.loads(result.stdout)["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert "not a regular file" in specific["permissionDecisionReason"]
    event = json.loads((tmp_path / "events.jsonl").read_text())
    assert event["event"] == "policy_deny"


def test_confined_file_rejects_symlinked_source_components(target: Path, tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_codex_hook import PolicyError, _confined_file

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "users.js").write_text("not trusted\n")
    (target / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PolicyError, match="symlinked"):
        _confined_file(target, "linked/users.js")


def test_confined_file_rejects_fifo_without_blocking(target: Path) -> None:
    from contextmesh.scripts.rrd_codex_hook import PolicyError, _confined_file

    fifo = target / "src/handlers/fifo.js"
    os.mkfifo(fifo)
    started = time.monotonic()
    with pytest.raises(PolicyError, match="not a regular file"):
        _confined_file(target, "src/handlers/fifo.js")
    assert time.monotonic() - started < 0.25


def test_confined_file_policy_rejects_oversized_source(target: Path, monkeypatch) -> None:
    from contextmesh.scripts import rrd_codex_hook

    source = target / "src/handlers/large.js"
    source.write_bytes(b"x" * 11)
    monkeypatch.setattr(rrd_codex_hook, "MAX_SOURCE_BYTES", 10)
    with pytest.raises(rrd_codex_hook.PolicyError, match="too large"):
        rrd_codex_hook._confined_file(target, "src/handlers/large.js")


def test_everos_seed_and_spawn_bundle_authenticate_three_shared_records_without_delivering_digests(
    tmp_path: Path, target: Path, monkeypatch
) -> None:
    from contextmesh.scripts.rrd_codex_hook import _seed, handle

    server = MemoryServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manifest = tmp_path / "manifest.json"
    monkeypatch.setenv("RRD_TARGET_ROOT", str(target))
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("RRD_SUMMARY_MODE", "deterministic")
    monkeypatch.setenv("RRC_EVEROS_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("RRD_MEMORY_BACKEND", "everos")
    monkeypatch.setenv("RRD_SEED_MANIFEST", str(manifest))
    monkeypatch.setenv("RRD_ENABLE_RRC", "0")
    try:
        assert (
            _seed(
                argparse.Namespace(
                    target_root=target,
                    round_id="rrd-unit",
                    arm="a",
                    manifest=manifest,
                    memory_backend="everos",
                )
            )
            == 0
        )
        output = handle(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "root",
                "tool_name": "spawn_agent",
                "tool_use_id": "call-users",
                "tool_input": {
                    "message": "Audit src/handlers/users.js",
                    "agent_type": "worker",
                    "fork_context": False,
                },
            }
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert output is not None
    context = output["hookSpecificOutput"]["updatedInput"]["message"]  # type: ignore[index]
    assert "UNTRUSTED_CONTEXTMESH_DIGEST" not in context
    assert context.count("<<<LINE_INDEXED_SOURCE") == 4
    assert all(path in context for path in ("src/models.js", "src/utils.js", "src/middleware.js"))
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    row = next(item for item in rows if item["event"] == "source_bundle")
    assert row["memory_backend"] == "everos"
    assert len(row["files"]) == 4


def test_local_seed_and_spawn_bundle_never_attempt_everos_http(
    tmp_path: Path, target: Path, monkeypatch
) -> None:
    from contextmesh.scripts import rrd_codex_hook

    attempts: list[object] = []

    def forbidden(*args: object, **kwargs: object) -> object:
        attempts.append((args, kwargs))
        raise AssertionError("local digest path attempted HTTP")

    manifest = tmp_path / "manifest.json"
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(rrd_codex_hook.urllib.request, "urlopen", forbidden)
    monkeypatch.setenv("RRD_TARGET_ROOT", str(target))
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(events))
    monkeypatch.setenv("RRD_SUMMARY_MODE", "deterministic")
    monkeypatch.setenv("RRC_EVEROS_URL", "http://127.0.0.1:9/poison")
    monkeypatch.setenv("RRD_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("RRD_SEED_MANIFEST", str(manifest))
    monkeypatch.setenv("RRD_ENABLE_RRC", "0")

    assert (
        rrd_codex_hook._seed(
            argparse.Namespace(
                target_root=target,
                round_id="rrd-sqlite-unit",
                arm="a",
                manifest=manifest,
                memory_backend="sqlite",
            )
        )
        == 0
    )
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    output = rrd_codex_hook.handle(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "root",
            "tool_name": "spawn_agent",
            "tool_use_id": "call-users",
            "tool_input": {
                "message": "Audit src/handlers/users.js",
                "agent_type": "worker",
                "fork_context": False,
            },
        }
    )

    assert attempts == []
    assert output is not None
    context = output["hookSpecificOutput"]["updatedInput"]["message"]  # type: ignore[index]
    assert "UNTRUSTED_CONTEXTMESH_DIGEST" not in context
    assert context.count("<<<LINE_INDEXED_SOURCE") == 4
    seeded = json.loads(manifest.read_text())
    assert seeded["memory_backend"] == "sqlite"
    assert all(isinstance(row["digest"], str) for row in seeded["files"])
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    row = next(item for item in rows if item["event"] == "source_bundle")
    assert row["memory_backend"] == "sqlite"

    monkeypatch.setenv("RRD_MEMORY_BACKEND", "everos")
    with pytest.raises(rrd_codex_hook.HookError, match="memory backend"):
        rrd_codex_hook.handle(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "root",
                "tool_name": "spawn_agent",
                "tool_use_id": "cross-backend-agent",
                "tool_input": {
                    "message": "Audit src/handlers/users.js",
                    "agent_type": "worker",
                    "fork_context": False,
                },
            }
        )


def test_subagent_stop_records_native_compression_receipt_without_a_continuation(
    tmp_path: Path, monkeypatch
) -> None:
    from contextmesh.scripts.rrd_codex_hook import handle

    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(events))
    output = handle(
        {
            "hook_event_name": "SubagentStop",
            "agent_id": "agent-users",
            "stop_hook_active": False,
            "last_assistant_message": (
                "short report\n\n[ContextMesh] Full worker report preserved locally; "
                "receipt=0123456789abcdefabcd."
            ),
        }
    )

    assert output == {}
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    row = rows[0]
    assert row["event"] == "result_final"
    assert row["compressed"] is True
    assert row["compression_receipt"] == "0123456789abcdefabcd"
    assert row["delivered_chars"] > 0
    assert len(row["delivered_sha256"]) == 64
    assert rows[1]["event"] == "native_usage_missing"


def test_wait_hook_records_completed_agents_but_fails_open_on_terminal_error(
    tmp_path: Path, monkeypatch
) -> None:
    from contextmesh.scripts.rrd_codex_hook import handle

    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(events))
    monkeypatch.setenv("RRD_RAW_RESULTS", str(tmp_path / "raw-results"))
    assert (
        handle(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "multi_agent_v1wait_agent",
                "tool_use_id": "call-wait",
                "tool_input": {"targets": ["agent-users", "agent-orders"]},
                "tool_response": json.dumps(
                    {
                        "status": {
                            "agent-users": {"completed": "users report"},
                            "agent-orders": {"failed": "worker error"},
                        },
                        "timed_out": False,
                    }
                ),
            }
        )
        is None
    )

    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert rows[0]["event"] == "wait_result"
    assert rows[0]["agent_ids"] == ["agent-orders", "agent-users"]
    assert rows[0]["completed_agent_ids"] == ["agent-users"]
    assert rows[0]["completed_results"] == {
        "agent-users": {
            "chars": len("users report"),
            "sha256": hashlib.sha256(b"users report").hexdigest(),
        }
    }
    assert rows[0]["result_count"] == 1
    assert rows[0]["timed_out"] is False
    assert rows[1]["event"] == "compress_fail_open"


def test_stop_records_strict_native_transcript_usage(tmp_path: Path, monkeypatch) -> None:
    from contextmesh.scripts.rrd_codex_hook import handle

    home = tmp_path / "home"
    home.mkdir()
    transcript = home / "rollout.jsonl"
    transcript.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "thread-root"}})
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 90,
                            "cached_input_tokens": 20,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 10,
                            "reasoning_output_tokens": 4,
                            "total_tokens": 100,
                        }
                    },
                },
            }
        )
        + "\n"
        + json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}})
        + "\n"
    )
    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(events))

    assert (
        handle(
            {
                "hook_event_name": "Stop",
                "last_assistant_message": "final report",
                "transcript_path": str(transcript),
            }
        )
        == {}
    )

    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["root_merge", "native_usage"]
    usage = rows[1]
    assert usage["component"] == "root"
    assert usage["session_id"] == "thread-root"
    assert usage["total_tokens"] == 100


def test_native_usage_rejects_nonfinal_regressed_and_failed_transcripts(
    tmp_path: Path, monkeypatch
) -> None:
    from contextmesh.scripts.rrd_codex_hook import HookError, _native_usage

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    base_usage = {
        "input_tokens": 90,
        "cached_input_tokens": 20,
        "cache_write_input_tokens": 0,
        "output_tokens": 10,
        "reasoning_output_tokens": 4,
        "total_tokens": 100,
    }

    def write(rows: list[dict[str, object]]) -> Path:
        path = home / "rollout.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    prefix = [
        {"type": "session_meta", "payload": {"id": "thread-root"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": base_usage},
            },
        },
    ]
    payload = {"transcript_path": str(home / "rollout.jsonl")}
    write([*prefix, {"type": "response_item", "payload": {"type": "message"}}])
    with pytest.raises(HookError, match="activity after final"):
        _native_usage(payload, component="root")

    regressed = {**base_usage, "input_tokens": 40, "total_tokens": 50}
    write(
        [
            *prefix,
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": regressed},
                },
            },
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        ]
    )
    with pytest.raises(HookError, match="regressed"):
        _native_usage(payload, component="root")

    write(
        [
            *prefix,
            {"type": "event_msg", "payload": {"type": "stream_error"}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        ]
    )
    with pytest.raises(HookError, match="failure event"):
        _native_usage(payload, component="root")


def test_everos_body_read_has_a_wall_clock_deadline() -> None:
    from contextmesh.scripts.rrd_codex_hook import _read_http_body

    class TrickleHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.03)
            except OSError:
                pass

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), TrickleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with urllib.request.urlopen(  # noqa: S310 - loopback deadline fixture
            f"http://127.0.0.1:{server.server_port}", timeout=1
        ) as response:
            with pytest.raises(TimeoutError, match="wall-clock"):
                _read_http_body(response, limit=1_000, deadline=time.monotonic() + 0.1)
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert elapsed < 0.5
