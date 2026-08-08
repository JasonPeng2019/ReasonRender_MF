from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).parents[1]
CM_PLUGIN = REPO / "contextmesh/plugin/contextmesh.ts"
RRC_PLUGIN = REPO / "contextmesh/plugin/reasonrendercoding.ts"
HANDLERS = ("auth", "users", "orders", "admin")


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    return content if isinstance(content, str) else json.dumps(content)


def _sse(delta: dict[str, Any], finish: str) -> bytes:
    chunks = (
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fake",
            "choices": [
                {"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}
            ],
        },
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fake",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
        },
    )
    return (
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    ).encode()


def _tool_calls(calls: list[tuple[str, str, dict[str, object]]]) -> bytes:
    tools = [
        {
            "index": index,
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
        for index, (call_id, name, arguments) in enumerate(calls)
    ]
    return _sse({"tool_calls": tools}, "tool_calls")


def _text(content: str) -> bytes:
    return _sse({"content": content}, "stop")


class CombinedDemoServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), CombinedDemoHandler)
        self.state_lock = threading.Lock()
        self.worker_barrier = threading.Barrier(4)
        self.worker_barrier_passes = 0
        self.worker_first_requests = 0
        self.four_workers_overlapped = False
        self.worker_prompts: list[str] = []
        self.buffers: dict[str, list[dict[str, object]]] = {}


class CombinedDemoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def demo(self) -> CombinedDemoServer:
        return self.server  # type: ignore[return-value]

    def _json(self, status: int, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def _stream(self, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.startswith("/api/v2/memory/"):
            self._memory(payload)
            return
        if self.path.endswith("/chat/completions"):
            self._chat(payload)
            return
        self._json(404, {})

    def _memory(self, payload: dict[str, Any]) -> None:
        if self.path.endswith("/add"):
            session = str(payload.get("session_id", ""))
            messages = payload.get("messages", [])
            with self.demo.state_lock:
                self.demo.buffers[session] = messages if isinstance(messages, list) else []
            self._json(200, {"data": {"status": "accumulated"}})
            return
        if self.path.endswith("/search"):
            filters = payload.get("filters", {})
            session = filters.get("session_id") if isinstance(filters, dict) else None
            with self.demo.state_lock:
                messages = self.demo.buffers.get(str(session), [])
            self._json(200, {"data": {"unprocessed_messages": messages, "episodes": []}})
            return
        self._json(200, {})

    def _chat(self, payload: dict[str, Any]) -> None:
        messages = payload.get("messages", [])
        all_text = "\n".join(_message_text(message) for message in messages)
        if payload.get("stream") is False:
            compressed = (
                "COMPRESSED WORKER REPORT"
                if "compress subagent task reports" in all_text
                else "DIGEST: complete shared-file structure"
            )
            self._json(
                200,
                {
                    "choices": [{"message": {"role": "assistant", "content": compressed}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
            return

        roles = [message.get("role") for message in messages]
        if "You are a title generator" in all_text:
            self._stream(_text("Combined demo"))
            return
        if "SEED_SHARED_FILES" in all_text:
            if "tool" in roles:
                self._stream(_text("DONE"))
            else:
                self._stream(
                    _tool_calls(
                        [
                            (f"seed-{index}", "read", {"filePath": f"src/{name}.js"})
                            for index, name in enumerate(("models", "utils", "middleware"))
                        ]
                    )
                )
            return
        if "ORCHESTRATOR_COMBINED_TEST" in all_text:
            if "tool" in roles:
                self._stream(_text("MERGED AUDIT REPORT"))
            else:
                self._stream(
                    _tool_calls(
                        [
                            (
                                f"task-{name}",
                                "task",
                                {
                                    "description": f"audit {name} handler",
                                    "prompt": (
                                        f"Audit src/handlers/{name}.js. Fully read it plus "
                                        "src/models.js, src/utils.js, and src/middleware.js."
                                    ),
                                    "subagent_type": "worker",
                                },
                            )
                            for name in HANDLERS
                        ]
                    )
                )
            return
        if "WORKER_COMBINED_TEST" in all_text:
            if "tool" in roles:
                report = "\n".join(
                    f"high src/handlers/example.js:{line} deterministic finding"
                    for line in range(1, 90)
                )
                self._stream(_text(report))
                return
            prompt = next(
                (_message_text(message) for message in messages if message.get("role") == "user"),
                "",
            )
            with self.demo.state_lock:
                self.demo.worker_prompts.append(prompt)
                self.demo.worker_first_requests += 1
            try:
                self.demo.worker_barrier.wait(timeout=8)
            except threading.BrokenBarrierError:
                self._stream(_text("worker concurrency barrier failed"))
                return
            with self.demo.state_lock:
                self.demo.worker_barrier_passes += 1
                if self.demo.worker_barrier_passes == 4:
                    self.demo.four_workers_overlapped = True
            handler = next(name for name in HANDLERS if f"/{name}.js" in prompt)
            reads = [
                (
                    f"read-{handler}-target",
                    "read",
                    {"filePath": f"src/handlers/{handler}.js", "offset": 1, "limit": 500},
                ),
                *[
                    (f"read-{handler}-{name}", "read", {"filePath": f"src/{name}.js"})
                    for name in ("models", "utils", "middleware")
                ],
            ]
            self._stream(_tool_calls(reads))
            return
        self._stream(_text("UNKNOWN"))

    def log_message(self, _format: str, *_args: object) -> None:
        return None


@pytest.mark.skipif(
    shutil.which("opencode") is None or shutil.which("bun") is None,
    reason="OpenCode and Bun are required",
)
def test_combined_plugins_preserve_four_real_overlapping_workers(tmp_path: Path) -> None:
    server = CombinedDemoServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    workspace = tmp_path / "target"
    (workspace / "src/handlers").mkdir(parents=True)
    for name in HANDLERS:
        (workspace / f"src/handlers/{name}.js").write_text("export const route = true\n")
    for name in ("models", "utils", "middleware"):
        (workspace / f"src/{name}.js").write_text(
            "\n".join(f"export const {name}_{line} = {line}" for line in range(120)) + "\n"
        )
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=workspace,
        check=True,
    )

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": "fake/fake",
        "default_agent": "orchestrator",
        "share": "disabled",
        "subagent_depth": 1,
        "provider": {
            "fake": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Fake",
                "options": {
                    "baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                    "apiKey": "test",
                },
                "models": {
                    "fake": {
                        "name": "Fake",
                        "limit": {"context": 131072, "output": 16384},
                    }
                },
            }
        },
        "agent": {
            "orchestrator": {
                "mode": "primary",
                "prompt": "ORCHESTRATOR_COMBINED_TEST",
                "steps": 8,
            },
            "worker": {
                "mode": "subagent",
                "prompt": "WORKER_COMBINED_TEST",
                "permission": {"edit": "deny"},
            },
        },
        "plugin": [str(CM_PLUGIN), str(RRC_PLUGIN)],
    }
    config_path = tmp_path / "opencode.json"
    config_path.write_text(json.dumps(config))
    resolution = {
        "branch": "hit",
        "external_ref": "integration-ref",
        "planner_tokens": 0,
        "profile": "lean",
        "rendered_packet": {"specification": "Read-only handler audit"},
    }
    fake_uv = tmp_path / "fake-uv"
    fake_uv.write_text("#!/bin/sh\n" + f"printf '%s\\n' {json.dumps(json.dumps(resolution))}\n")
    fake_uv.chmod(0o755)
    common_env = {
        **os.environ,
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_CONFIG_DIR": str(config_dir),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "PWD": str(workspace),
        "CONTEXTMESH_EVEROS_URL": f"http://127.0.0.1:{server.server_port}",
        "CONTEXTMESH_APP_ID": "combined-integration",
        "CONTEXTMESH_SUMMARIZER_URL": f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
        "CONTEXTMESH_SUMMARIZER_MODEL": "fake",
        "CONTEXTMESH_MIN_LINES": "20",
        "CONTEXTMESH_TASK_COMPRESS_CHARS": "200",
        "CONTEXTMESH_SYNC_SUMMARIZE": "1",
        "OLLAMA_API_KEY": "test",
        "RRC_DEMO_REPO": str(REPO),
        "RRC_DEMO_ROUND": "integration",
        "RRC_DEMO_MODE": "warm",
        "RRC_DEMO_DATABASE": str(tmp_path / "packets.sqlite"),
        "RRC_DEMO_LOCK": str(tmp_path / "packets.lock"),
        "RRC_DEMO_EVENTS": str(tmp_path / "rrc-events.jsonl"),
        "RRC_DEMO_MODEL_EVENTS": str(tmp_path / "rrc-model-events.jsonl"),
        "RRC_EVEROS_URL": f"http://127.0.0.1:{server.server_port}",
        "RRC_STRONG_MODEL": "fake",
        "RRC_DEMO_UV_BIN": str(fake_uv),
    }
    try:
        seed_env = {
            **common_env,
            "OPENCODE_DB": str(tmp_path / "seed.db"),
            "CONTEXTMESH_LOG": str(tmp_path / "seed-contextmesh.jsonl"),
        }
        seed = subprocess.run(
            [
                "opencode",
                "run",
                "--format",
                "json",
                "--auto",
                "--agent",
                "build",
                "SEED_SHARED_FILES",
            ],
            cwd=workspace,
            env=seed_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert seed.returncode == 0, seed.stderr

        metrics = tmp_path / "contextmesh.jsonl"
        database = tmp_path / "opencode.db"
        run_env = {
            **common_env,
            "OPENCODE_DB": str(database),
            "CONTEXTMESH_LOG": str(metrics),
        }
        run = subprocess.run(
            [
                "opencode",
                "run",
                "--format",
                "json",
                "--auto",
                "--agent",
                "orchestrator",
                "Run the combined audit.",
            ],
            cwd=workspace,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert run.returncode == 0, run.stderr
    assert server.four_workers_overlapped
    assert server.worker_barrier_passes == 4
    assert len(server.worker_prompts) == 4
    assert all("[ReasonRenderCoding]" in prompt for prompt in server.worker_prompts)
    connection = sqlite3.connect(database)
    try:
        child_rows = connection.execute(
            "SELECT id FROM session WHERE parent_id IS NOT NULL"
        ).fetchall()
    finally:
        connection.close()
    child_ids = {row[0] for row in child_rows}
    assert len(child_ids) == 4
    events = [json.loads(line) for line in metrics.read_text().splitlines()]
    assert any(event.get("event") == "plugin_loaded" for event in events)
    digest_events = [event for event in events if event.get("event") == "digest_hit"]
    assert len(digest_events) >= 8
    assert {event["sessionID"] for event in digest_events} == child_ids
    assert sum(event.get("event") == "task_compressed" for event in events) == 4
