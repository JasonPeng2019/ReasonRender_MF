from __future__ import annotations

import hashlib
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
PROMPT = (REPO / "contextmesh/demo-prompt.txt").read_text()
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
) -> tuple[subprocess.CompletedProcess[str], Provider, list[dict[str, Any]]]:
    provider = Provider()
    if boundary:
        provider.child_delay = 0.05
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
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
    fixture_url = f"http://127.0.0.1:{provider.server_port}/v1"
    (home / "config.toml").write_text(
        f"""model="gpt-5.4"
model_provider="local"
approval_policy="never"
sandbox_mode="read-only"
web_search="disabled"
[model_providers.local]
name="local"
base_url="{fixture_url}"
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


@pytest.mark.parametrize("boundary", ["PreToolUse", "SubagentStart", "SubagentStop"])
@pytest.mark.parametrize("failure", ["empty", "malformed", "nonzero", "signal", "timeout"])
def test_installed_codex_hook_failures_leave_four_workers_and_root_merge(
    tmp_path: Path, boundary: str, failure: str
) -> None:
    result, provider, _events = _run(tmp_path, boundary=boundary, failure=failure)

    assert result.returncode == 0, result.stderr
    assert "ROOT_MERGED workers=4" in result.stdout
    assert len([request for request in provider.requests if request["subagent"]]) == 4


def _capability_matrix():
    from contextmesh.scripts import rrcv2_capability_matrix

    return rrcv2_capability_matrix


def test_installed_codex_v1_stable_keyring_home_capability(tmp_path: Path) -> None:
    from contextmesh.scripts import rrcv2_capability_preflight

    stable = (REPO / "contextmesh/.codex-rrd-native").resolve()
    assert Path(os.environ["CODEX_HOME"]).resolve() == stable
    assert not (stable / "auth.json").exists()
    assert (
        rrcv2_capability_preflight.validate_sealed(repo=REPO, environ=os.environ)[
            "provider_launch_total"
        ]
        == 0
    )

    status = subprocess.run(
        ["codex", "-c", 'cli_auth_credentials_store="keyring"', "login", "status"],
        env={
            "HOME": os.environ["HOME"],
            "CODEX_HOME": str(stable),
            "PATH": os.environ["PATH"],
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
            "TMPDIR": str(tmp_path),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert status.returncode == 0
    assert "Logged in using ChatGPT" in status.stdout + status.stderr

    fresh = tmp_path / "fresh-codex-home"
    fresh.mkdir(mode=0o700)
    fresh_status = subprocess.run(
        ["codex", "-c", 'cli_auth_credentials_store="keyring"', "login", "status"],
        env={
            "HOME": os.environ["HOME"],
            "CODEX_HOME": str(fresh),
            "PATH": os.environ["PATH"],
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
            "TMPDIR": str(tmp_path),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert fresh_status.returncode != 0
    assert "Logged in using ChatGPT" not in fresh_status.stdout + fresh_status.stderr
    assert not (fresh / "auth.json").exists()


def test_installed_codex_v1_rrcv2_worker_evidence_capability() -> None:
    summary = _capability_matrix().ensure(REPO)
    worker = summary["results"][8]
    assert worker["call_id"] == "cap-09-worker-small-low-native"
    assert worker["surface_id"] == "worker_small_low_native"
    assert worker["identity_attestation"] == "native_partial"
    assert worker["effective_provider"] == "openai"
    assert worker["effective_model"] == "gpt-5.6-luna"
    assert worker["effective_reasoning"] == "low"
    assert worker["effective_service_tier"] == "unattested"
    assert worker["provider_total_tokens"] == worker["input_tokens"] + worker["output_tokens"]


def test_installed_codex_v1_rrcv2_worker_does_not_inherit_root_source() -> None:
    summary = _capability_matrix().ensure(REPO)
    worker = summary["results"][8]
    rollout = (
        REPO
        / ".generated/state/rrcv2-convergence/capability/calls"
        / worker["call_id"]
        / "rollout.jsonl"
    ).read_bytes()
    assert b"RRCV2_WORKER_CAPABILITY" in rollout
    assert b"CAPABILITY_PRETOOL_REWRITE" in rollout
    assert b"RRCV2_ROOT_PRIVATE_SENTINEL_8d0cbf61" not in rollout


def test_installed_codex_v1_rrcv2_wait_substitution_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextmesh.scripts import rrcv2_capability_hook

    monkeypatch.setattr(rrcv2_capability_hook, "CALL_ROOT", tmp_path)

    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "multi_agent_v1wait_agent",
        "tool_input": {"capability_synthetic": True},
        "tool_response": {
            "status": {
                "agent-b": {"pending": True},
                "agent-a": {"completed": "receipt-a"},
            },
            "timed_out": False,
        },
    }
    first = rrcv2_capability_hook.handle(payload)
    second = rrcv2_capability_hook.handle(payload)
    assert first == second
    assert first is not None and first["continue"] is False
    stop_reason = first["stopReason"]
    assert isinstance(stop_reason, str)
    replacement = json.loads(stop_reason)
    assert replacement["results"] == [
        {"agent_id": "agent-a", "receipt": "receipt-a", "state": "accepted"},
        {"agent_id": "agent-b", "state": "pending"},
    ]


def test_installed_codex_v1_rrcv2_stage_read_isolation_capability() -> None:
    summary = _capability_matrix().ensure(REPO)
    for row in summary["results"][:7]:
        stdout = (
            REPO
            / ".generated/state/rrcv2-convergence/capability/calls"
            / row["call_id"]
            / "stdout.jsonl"
        ).read_text()
        assert '"type":"command_execution"' not in stdout
        assert '"type":"mcp_tool_call"' not in stdout
        assert '"type":"collab_tool_call"' not in stdout


def test_installed_codex_v1_rrcv2_nested_spec_spawn_capability() -> None:
    summary = _capability_matrix().ensure(REPO)
    assert summary["exact_call_count"] == 9
    assert len(summary["results"]) == 9
    assert set(summary["surface_input_tokens_max"]) == {
        "root_strong_medium_native",
        "small_code_low",
        "small_metadata_low",
        "small_spec_low",
        "small_tests_low",
        "strong_code_medium",
        "strong_spec_low",
        "worker_small_low_native",
    }
    assert max(summary["surface_input_tokens_max"].values()) <= 65_536
    root_dir = (
        REPO
        / ".generated/state/rrcv2-convergence/capability/calls/cap-08-root-strong-medium-native"
    )
    hook_events = [
        json.loads(line) for line in (root_dir / "hook-events.jsonl").read_text().splitlines()
    ]
    names = {row["hook_event_name"] for row in hook_events}
    assert {"PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop", "Stop"} <= names


def test_installed_codex_rrcv2_contextmesh_credibility_suite() -> None:
    """Preserve failed v19 and consume the single reviewed v20 credibility attempt."""

    failed_v19 = (
        REPO
        / ".generated/state/rrcv2-convergence/verify/cli-smoke/rrcv2-cli-smoke-v19"
        / "d3e90e2ffbecf3b25891fa527c521c08c9505a514d04e6faeb26fdb071e0a57c"
    )
    assert hashlib.sha256((failed_v19 / "producer.json").read_bytes()).hexdigest() == (
        "7b96ad27236e5f3d1254c16bf54f6ef7da570e5ee007d98200620e4ffb0f4f06"
    )
    assert hashlib.sha256((failed_v19 / "terminal.json").read_bytes()).hexdigest() == (
        "fd3d673c9b1594fe368c33be0e275bae5de5fc8005e59ef821fda34b28d5344e"
    )
    assert json.loads((failed_v19 / "terminal.json").read_bytes())["status"] == "failure"

    preimage = (
        b'{"experiment_id":"rrcv2-cli-smoke-v20","fixture_manifest_sha256":'
        b'"483db5cdc34b2ab16d99dd578ca87981e4b82e3d6ea53550be841fa7eedaaf39",'
        b'"v":20}'
    )
    assert len(preimage) == 139
    producer_sha = hashlib.sha256(preimage).hexdigest()
    assert producer_sha == "7a66a30bbd2e7004724ba4aee1de7a879d87eb9212653e6a36d52d2cf1514744"
    token = "rrcv2-cli-smoke-" + producer_sha[:32]
    producer_root = (
        REPO
        / ".generated/state/rrcv2-convergence/verify/cli-smoke/rrcv2-cli-smoke-v20"
        / producer_sha
    )
    assert not producer_root.exists(), "the one reviewed v20 producer was already consumed"
    environment = dict(os.environ)
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "RRC_EVEROS_URL",
        "RRCV2_EVEROS_TARGET",
    ):
        environment.pop(name, None)
    completed = subprocess.run(
        [
            "bash",
            "contextmesh/scripts/rrd_demo_tui.sh",
            "smoke",
            "--fixture",
            "tests/fixtures/rrcv2_cli_smoke/manifest.json",
            "--round-id",
            token,
            "--timeout-ms",
            "900000",
        ],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        timeout=930,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads((producer_root / "terminal.json").read_bytes()) == {
        "producer_sha256": producer_sha,
        "returncode": 0,
        "status": "success",
        "v": 1,
    }
    summary = json.loads((producer_root / "round/summary.json").read_bytes())
    assert summary["kind"] == "rrcv2_cli_smoke_summary"
    assert [row["task_id"] for row in summary["cells"]] == [
        "rrcv2-cli-miss-001",
        "rrcv2-cli-hit-001",
        "rrcv2-cli-near-001",
    ]
    assert [row["branch"] for row in summary["cells"]] == ["miss", "reuse", "miss"]
    assert [len(row["all_cost_event_ids"]) for row in summary["cells"]] == [4, 2, 4]
    assert sum(len(row["all_cost_event_ids"]) for row in summary["cells"]) == 10
    assert {"cache_render_rejection", "tier_minus_one"} <= set(
        summary["cells"][2]["deterministic_stages"]
    )
    rows = [
        json.loads(path.read_bytes())
        for path in (producer_root / "round/cancellation/children").iterdir()
    ]
    assert rows and all(row["state"] == "terminal" for row in rows)
    assert not (REPO / "contextmesh/.codex-rrd-native/auth.json").exists()
