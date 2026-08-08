from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).parents[1]
PROMPT = (REPO / "contextmesh/RRD-demo-prompt.txt").read_text()
HANDLERS = ("users", "products", "orders", "reviews")


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
        for event in events
    ).encode()


def _created(identifier: str) -> dict[str, object]:
    return {"type": "response.created", "response": {"id": identifier}}


def _completed(identifier: str) -> dict[str, object]:
    return {
        "type": "response.completed",
        "response": {
            "id": identifier,
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        },
    }


def _message(identifier: str, text: str) -> dict[str, object]:
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "id": identifier,
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _call(identifier: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "call_id": identifier,
            "namespace": "multi_agent_v1",
            "name": name,
            "arguments": json.dumps(arguments, separators=(",", ":")),
        },
    }


def _tool_search_call(identifier: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "tool_search_call",
            "call_id": identifier,
            "execution": "client",
            "arguments": arguments,
        },
    }


class Provider(ThreadingHTTPServer):
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.child_started: list[float] = []
        self.child_finished: list[float] = []
        self.child_delay = 1.0
        self.worker_payload = ""
        super().__init__(("127.0.0.1", 0), ProviderHandler)


class ProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def provider(self) -> Provider:
        return self.server  # type: ignore[return-value]

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body: dict[str, Any] = json.loads(self.rfile.read(length))
        with self.provider.lock:
            self.provider.requests.append(
                {"subagent": self.headers.get("x-openai-subagent"), "body": body}
            )
            index = len(self.provider.requests)
        response_id = f"resp-{index}"
        if self.headers.get("x-openai-subagent"):
            with self.provider.lock:
                self.provider.child_started.append(time.monotonic())
            time.sleep(self.provider.child_delay)
            serialized = json.dumps(body)
            events = [
                _created(response_id),
                _message(
                    f"child-{index}",
                    "WORKER_DONE "
                    f"rewrite={'PRE_REWRITE' in serialized} start={'START_CONTEXT' in serialized}"
                    + self.provider.worker_payload,
                ),
                _completed(response_id),
            ]
            with self.provider.lock:
                self.provider.child_finished.append(time.monotonic())
        else:
            inputs = body.get("input", [])
            spawn_outputs = [
                item
                for item in inputs
                if item.get("type") == "function_call_output"
                and str(item.get("call_id", "")).startswith("spawn-")
            ]
            loaded_agents = any(
                item.get("type") == "tool_search_output" and item.get("call_id") == "load-agents"
                for item in inputs
            )
            if not loaded_agents:
                events = [
                    _created(response_id),
                    _tool_search_call(
                        "load-agents", {"query": "spawn_agent wait_agent", "limit": 8}
                    ),
                    _completed(response_id),
                ]
            elif not spawn_outputs:
                events = [_created(response_id)]
                for handler in HANDLERS:
                    events.append(
                        _call(
                            f"spawn-{handler}",
                            "spawn_agent",
                            {
                                "message": f"Audit src/handlers/{handler}.js and report findings.",
                                "agent_type": "worker",
                                "fork_context": False,
                            },
                        )
                    )
                events.append(_completed(response_id))
            else:
                targets: list[str] = []
                for item in spawn_outputs:
                    targets.append(json.loads(item["output"])["agent_id"])
                done: set[str] = set()
                wait_count = 0
                for item in inputs:
                    if item.get("type") != "function_call_output" or not str(
                        item.get("call_id", "")
                    ).startswith("wait-"):
                        continue
                    wait_count += 1
                    try:
                        done.update(json.loads(item["output"])["status"])
                    except (KeyError, TypeError, json.JSONDecodeError):
                        pass
                remaining = [target for target in targets if target not in done]
                if remaining:
                    events = [
                        _created(response_id),
                        _call(
                            f"wait-{wait_count}",
                            "wait_agent",
                            {"targets": remaining, "timeout_ms": 10_000},
                        ),
                        _completed(response_id),
                    ]
                else:
                    events = [
                        _created(response_id),
                        _message(f"root-{index}", f"ROOT_MERGED workers={len(targets)}"),
                        _completed(response_id),
                    ]
        payload = _sse(events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _codex_version() -> str:
    if shutil.which("codex") is None:
        return ""
    return subprocess.run(["codex", "--version"], capture_output=True, text=True).stdout.strip()


pytestmark = pytest.mark.skipif(
    _codex_version() != "codex-cli 0.147.0", reason="fixture is pinned to codex-cli 0.147.0"
)


def _run(
    tmp_path: Path,
    *,
    boundary: str = "",
    failure: str = "",
    response_proxy: bool = False,
    worker_payload: str = "",
) -> tuple[subprocess.CompletedProcess[str], Provider, list[dict[str, Any]]]:
    provider = Provider()
    provider.worker_payload = worker_payload
    if boundary:
        provider.child_delay = 0.05
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    proxy = None
    proxy_thread = None
    if response_proxy:
        from contextmesh.scripts.rrd_response_proxy import Server as ProxyServer

        runs = tmp_path / "runs"
        arm = runs / "rrd-demo/rrd-test/a"
        (arm / "target").mkdir(parents=True)
        (arm / "summarizer-home").mkdir()
        bundle = runs / "rrd-demo/rrd-test/bundle/rrd_codex_hook.py"
        bundle.parent.mkdir(parents=True)
        bundle.write_text("print('COMPRESSED INSTALLED CODEX WORKER REPORT')\n")
        proxy = ProxyServer(
            ("127.0.0.1", 0),
            upstream=f"http://127.0.0.1:{provider.server_port}",
            runs=runs,
        )
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        proxy_thread.start()
    home = tmp_path / "home"
    home.mkdir()
    hook_log = tmp_path / "hooks.jsonl"
    hook = tmp_path / "hook.py"
    hook.write_text(
        """import json,os,signal,sys,time
p=json.load(sys.stdin)
line=(json.dumps(p,separators=(',',':'))+'\\n').encode()
fd=os.open(os.environ['HOOK_LOG'],os.O_APPEND|os.O_CREAT|os.O_WRONLY,0o600)
try: os.write(fd,line)
finally: os.close(fd)
event=p.get('hook_event_name'); boundary=os.environ.get('FAIL_BOUNDARY',''); mode=os.environ.get('FAIL_MODE','')
def fail():
  if mode=='empty': return
  if mode=='malformed': print('{'); return
  if mode=='nonzero': raise SystemExit(7)
  if mode=='signal': os.kill(os.getpid(), signal.SIGTERM)
  if mode=='timeout': time.sleep(2); return
if event==boundary: fail()
elif event=='PreToolUse' and p.get('tool_name')=='spawn_agent':
  q=dict(p['tool_input']); q['message'] += ' PRE_REWRITE'
  print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'allow','updatedInput':q}}))
elif event=='SubagentStart': print(json.dumps({'hookSpecificOutput':{'hookEventName':'SubagentStart','additionalContext':'START_CONTEXT'}}))
elif event in {'SubagentStop','Stop'}: print('{}')
"""
    )
    hooks_by_event: dict[str, list[dict[str, object]]] = {}
    for name in ("PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop", "Stop"):
        entry: dict[str, object] = {
            "hooks": [{"type": "command", "command": f"python3 {hook}", "timeout": 1}]
        }
        if name in {"SubagentStart", "SubagentStop"}:
            entry["matcher"] = "worker"
        hooks_by_event[name] = [entry]
    hooks = {"hooks": hooks_by_event}
    (home / "hooks.json").write_text(json.dumps(hooks))
    if proxy is None:
        base_url = f"http://127.0.0.1:{provider.server_port}/v1"
    else:
        base_url = f"http://127.0.0.1:{proxy.server_port}/ollama/rrd-demo-rrd-test-a-outer/v1"
    (home / "config.toml").write_text(
        f"""model="gpt-5.4"
model_provider="local"
approval_policy="never"
sandbox_mode="read-only"
web_search="disabled"
[model_providers.local]
name="local"
base_url="{base_url}"
env_key="TEST_KEY"
wire_api="responses"
request_max_retries=0
stream_max_retries=0
stream_idle_timeout_ms=10000
supports_websockets=false
[agents]
enabled=true
max_concurrent_threads_per_session=4
[agents.worker]
description="Audit exactly one HTTP handler using the supplied RRC and ContextMesh context."
[features]
hooks=true
multi_agent=true
multi_agent_v2=false
plugins=false
"""
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    git_log = tmp_path / "unexpected-git.log"
    fake_git = fake_bin / "git"
    fake_git.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$RRD_TEST_GIT_LOG"\nexit 97\n')
    fake_git.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CODEX_HOME": str(home),
        "TEST_KEY": "dummy",
        "HOOK_LOG": str(hook_log),
        "RRD_TEST_GIT_LOG": str(git_log),
        "FAIL_BOUNDARY": boundary,
        "FAIL_MODE": failure,
    }
    env.pop("CODEX_THREAD_ID", None)
    try:
        result = subprocess.run(
            [
                "codex",
                "--dangerously-bypass-hook-trust",
                "exec",
                "--skip-git-repo-check",
                "--json",
                PROMPT,
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    finally:
        if proxy is not None:
            proxy.shutdown()
            proxy.server_close()
            assert proxy_thread is not None
            proxy_thread.join(timeout=2)
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=2)
    events = [json.loads(line) for line in hook_log.read_text().splitlines()]
    assert not git_log.exists(), git_log.read_text() if git_log.exists() else ""
    return result, provider, events


def test_installed_codex_v1_accepts_rewrite_start_context_and_four_native_workers(
    tmp_path: Path,
) -> None:
    result, provider, events = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "ROOT_MERGED workers=4" in result.stdout
    child_bodies = [request["body"] for request in provider.requests if request["subagent"]]
    assert len(child_bodies) == 4
    assert all("PRE_REWRITE" in json.dumps(body) for body in child_bodies)
    assert all("START_CONTEXT" in json.dumps(body) for body in child_bodies)
    assert all("multi_agent_v1" not in json.dumps(body.get("tools", [])) for body in child_bodies)
    assert sum(event["hook_event_name"] == "SubagentStart" for event in events) == 4
    assert sum(event["hook_event_name"] == "SubagentStop" for event in events) == 4
    assert len(provider.child_started) == len(provider.child_finished) == 4
    assert max(provider.child_started) < min(provider.child_finished)
    search_outputs = [
        item
        for request in provider.requests
        if not request["subagent"]
        for item in request["body"].get("input", [])
        if item.get("type") == "tool_search_output" and item.get("call_id") == "load-agents"
    ]
    assert search_outputs
    assert all(item["tools"] == search_outputs[0]["tools"] for item in search_outputs)
    fixture = json.loads((REPO / "tests/fixtures/codex_0_147_multiagent_v1.json").read_text())
    namespace = next(
        item for item in search_outputs[0]["tools"] if item.get("name") == "multi_agent_v1"
    )
    advertised = {item["name"]: item for item in namespace["tools"]}
    assert set(advertised["spawn_agent"]["parameters"]["properties"]) == set(
        fixture["spawn"]["arguments"]
    )
    assert set(advertised["wait_agent"]["parameters"]["properties"]) == set(
        fixture["wait"]["arguments"]
    )
    schemas = json.dumps(search_outputs[0]["tools"])
    assert "task_name" not in schemas and "fork_turns" not in schemas
    assert "plugins=false" in (tmp_path / "home/config.toml").read_text()
    hook_log_mode = (tmp_path / "hooks.jsonl").stat().st_mode & 0o777
    assert hook_log_mode == 0o600


def test_installed_codex_root_receives_proxy_summary_not_oversized_raw_result(
    tmp_path: Path, monkeypatch
) -> None:
    raw_marker = "RAW-INSTALLED-CODEX-WORKER-RESULT-" + ("x" * 4000)
    monkeypatch.setenv("RRD_TASK_COMPRESS_CHARS", "100")

    result, provider, _events = _run(
        tmp_path,
        response_proxy=True,
        worker_payload=raw_marker,
    )

    assert result.returncode == 0, result.stderr
    assert "ROOT_MERGED workers=4" in result.stdout
    root_history = json.dumps(
        [
            request["body"].get("input", [])
            for request in provider.requests
            if not request["subagent"]
        ]
    )
    assert raw_marker not in root_history
    assert "COMPRESSED INSTALLED CODEX WORKER REPORT" in root_history
    assert "receipt=" in root_history
    proxy_rows = [
        json.loads(line)
        for line in (tmp_path / "runs/rrd-demo/rrd-test/a/proxy-events.jsonl")
        .read_text()
        .splitlines()
    ]
    assert len(proxy_rows) == 4
    assert all(row["event"] == "result_compress" for row in proxy_rows)


@pytest.mark.parametrize("boundary", ["PreToolUse", "SubagentStart", "SubagentStop"])
@pytest.mark.parametrize("failure", ["empty", "malformed", "nonzero", "signal", "timeout"])
def test_installed_codex_hook_failures_leave_four_workers_and_root_merge(
    tmp_path: Path, boundary: str, failure: str
) -> None:
    result, provider, _events = _run(tmp_path, boundary=boundary, failure=failure)

    assert result.returncode == 0, result.stderr
    assert "ROOT_MERGED workers=4" in result.stdout
    assert len([request for request in provider.requests if request["subagent"]]) == 4
