from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SHARED_PATHS = ("src/models.js", "src/utils.js", "src/middleware.js")


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


def test_pretool_rewrites_one_codex_spawn_with_current_handler_and_rrc_packet(
    tmp_path: Path, target: Path, monkeypatch
) -> None:
    from contextmesh.scripts.rrd_codex_hook import handle

    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("RRD_TARGET_ROOT", str(target))
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(events))
    monkeypatch.setenv("RRC_CONTROL", "deterministic")
    monkeypatch.setenv("OLLAMA_API_KEY", "SECRET-MUST-NOT-BE-LOGGED")
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
    assert "ReasonRenderCoding validated packet" in updated["message"]
    assert "ContextMesh current handler" in updated["message"]
    assert "export function users" in updated["message"]
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert {row["event"] for row in rows} == {"packet", "assignment"}
    assert "SECRET-MUST-NOT-BE-LOGGED" not in events.read_text()
    assert stat.S_IMODE(events.stat().st_mode) == 0o600


def test_rrc_bridge_clamps_nested_timeouts_below_the_outer_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    from contextmesh.scripts import rrd_codex_hook

    observed: dict[str, object] = {}

    def fake_run(command, *, timeout, env, cwd):
        observed.update(command=list(command), timeout=timeout, env=env, cwd=cwd)
        return subprocess.CompletedProcess(
            list(command), 0, '{"rendered_packet":{"signature":"audit"}}\n', ""
        )

    monkeypatch.delenv("RRC_CONTROL", raising=False)
    monkeypatch.setenv("RRC_BRIDGE_TIMEOUT", "40")
    monkeypatch.setenv("RRC_PLANNER_TIMEOUT", "100")
    monkeypatch.setenv("RRC_LOCK_TIMEOUT", "100")
    monkeypatch.setenv("RRC_VISIBILITY_TIMEOUT", "12")
    monkeypatch.setenv("RRC_DEMO_UV_BIN", "uv")
    monkeypatch.setenv("RRC_DEMO_MODE", "warm")
    monkeypatch.setenv("RRC_DEMO_ROUND", "rrd-test")
    monkeypatch.setenv("RRC_DEMO_DATABASE", str(tmp_path / "packets.sqlite"))
    monkeypatch.setenv("RRC_DEMO_LOCK", str(tmp_path / "packets.lock"))
    monkeypatch.setenv("RRC_DEMO_EVENTS", str(tmp_path / "rrc-events.jsonl"))
    monkeypatch.setenv("RRC_DEMO_MODEL_EVENTS", str(tmp_path / "model-events.jsonl"))
    monkeypatch.setenv("RRC_STRONG_MODEL", "test-model")
    monkeypatch.setenv("RRD_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(rrd_codex_hook, "_run_group", fake_run)

    packet = rrd_codex_hook._resolve_packet(
        {"session_id": "root", "tool_use_id": "call-users"},
        "src/handlers/users.js",
        "Audit src/handlers/users.js",
    )

    command = observed["command"]
    assert isinstance(command, list)
    planner = float(command[command.index("--planner-timeout") + 1])
    lock = float(command[command.index("--lock-timeout") + 1])
    visibility = float(command[command.index("--visibility-timeout") + 1])
    assert observed["timeout"] == 40
    assert planner + visibility + 8 <= 40
    assert lock + 8 <= 40
    assert packet["signature"] == "audit"


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


def test_subagent_start_failure_tells_worker_to_read_shared_files_directly(
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
    assert all(path in specific["additionalContext"] for path in SHARED_PATHS)
    event = json.loads((tmp_path / "events.jsonl").read_text())
    assert event["event"] == "fail_open"


def test_subagent_start_policy_error_uses_start_fail_open_schema(
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
            "RRD_SEED_MANIFEST": str(manifest_path),
        },
        timeout=2,
        check=False,
    )

    assert result.returncode == 0
    specific = json.loads(result.stdout)["hookSpecificOutput"]
    assert specific["hookEventName"] == "SubagentStart"
    assert "Read src/models.js" in specific["additionalContext"]
    event = json.loads((tmp_path / "events.jsonl").read_text())
    assert event["event"] == "fail_open"
    assert event["hook_event"] == "SubagentStart"


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


def test_seed_and_subagent_start_authenticate_exactly_three_shared_digests(
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
    monkeypatch.setenv("RRD_SEED_MANIFEST", str(manifest))
    try:
        assert (
            _seed(
                argparse.Namespace(
                    target_root=target,
                    round_id="rrd-unit",
                    arm="a",
                    manifest=manifest,
                )
            )
            == 0
        )
        output = handle(
            {
                "hook_event_name": "SubagentStart",
                "agent_id": "agent-users",
                "agent_type": "worker",
            }
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert output is not None
    context = output["hookSpecificOutput"]["additionalContext"]  # type: ignore[index]
    assert context.count("<<<UNTRUSTED_CONTEXTMESH_DIGEST") == 3
    assert all(path in context for path in ("src/models.js", "src/utils.js", "src/middleware.js"))
    row = json.loads((tmp_path / "events.jsonl").read_text())
    assert row["event"] == "shared_context"
    assert len(row["receipts"]) == 3


def test_subagent_stop_records_proxy_compression_receipt_without_a_continuation(
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
    row = json.loads(events.read_text())
    assert row["event"] == "result_final"
    assert row["compressed"] is True
    assert row["compression_receipt"] == "0123456789abcdefabcd"
    assert row["delivered_chars"] > 0
    assert len(row["delivered_sha256"]) == 64


def test_wait_hook_records_only_observed_completed_agents(tmp_path: Path, monkeypatch) -> None:
    from contextmesh.scripts.rrd_codex_hook import handle

    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("RRD_HOOK_EVENTS", str(events))
    assert (
        handle(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "multi_agent_v1wait_agent",
                "tool_use_id": "call-wait",
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

    row = json.loads(events.read_text())
    assert row["event"] == "wait_result"
    assert row["agent_ids"] == ["agent-orders", "agent-users"]
    assert row["completed_agent_ids"] == ["agent-users"]
    assert row["completed_results"] == {
        "agent-users": {
            "chars": len("users report"),
            "sha256": hashlib.sha256(b"users report").hexdigest(),
        }
    }
    assert row["result_count"] == 1
    assert row["timed_out"] is False


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
