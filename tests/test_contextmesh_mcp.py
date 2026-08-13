from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _load_server():
    path = Path(__file__).resolve().parents[1] / "contextmesh" / "mcp" / "server.py"
    spec = importlib.util.spec_from_file_location("contextmesh_mcp_server_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_packet_insufficient_is_one_per_full_arm_read(tmp_path: Path, monkeypatch) -> None:
    server = _load_server()
    source = tmp_path / "read.py"
    source.write_text("line 1\nline 2\n", encoding="utf-8")
    missing = tmp_path / "missing.py"
    log_path = tmp_path / "events.jsonl"

    monkeypatch.setenv("REASONRENDER_ARM", "full")
    mesh = server.ContextMesh(min_lines=1000, log_path=log_path)
    successful = asyncio.run(mesh.read(str(source)))
    nonexistent = asyncio.run(mesh.read(str(missing)))

    monkeypatch.setenv("REASONRENDER_ARM", "contextmesh")
    asyncio.run(mesh.read(str(source), offset=1, limit=1))

    monkeypatch.setenv("REASONRENDER_ARM", "raw")
    asyncio.run(mesh.read(str(source)))

    monkeypatch.delenv("REASONRENDER_ARM")
    asyncio.run(mesh.read(str(source)))

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    events = [row for row in rows if row["event"] == "packet_insufficient"]

    assert f"<path>{source}</path>" in successful
    assert "ContextMesh read failed" in nonexistent
    assert len(events) == 2
    assert [row["path"] for row in events] == [str(source), str(missing)]
    missing_packet = next(
        index for index, row in enumerate(rows) if row["event"] == "packet_insufficient" and row["path"] == str(missing)
    )
    missing_error = next(
        index for index, row in enumerate(rows) if row["event"] == "errors" and row["path"] == str(missing)
    )
    assert missing_packet < missing_error


def test_directory_reads_are_listed_without_a_fail_open_error(tmp_path: Path, monkeypatch) -> None:
    server = _load_server()
    directory = tmp_path / "rules"
    directory.mkdir()
    (directory / "nested").mkdir()
    (directory / "policy.py").write_text("pass\n", encoding="utf-8")
    log_path = tmp_path / "events.jsonl"

    monkeypatch.setenv("REASONRENDER_ARM", "full")
    mesh = server.ContextMesh(log_path=log_path)
    result = asyncio.run(mesh.read(str(directory)))

    events = [json.loads(line)["event"] for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert "<type>directory</type>" in result
    assert "nested/" in result and "policy.py" in result
    assert events == ["packet_insufficient", "directory_listing"]
    assert mesh.counters["errors"] == 0


def test_cli_usage_is_retained_for_weighted_arm_accounting(tmp_path: Path) -> None:
    server = _load_server()
    log_path = tmp_path / "events.jsonl"
    mesh = server.ContextMesh(log_path=log_path)

    mesh._record_cli_usage(
        {
            "modelUsage": {
                "claude-sonnet": {
                    "inputTokens": 3,
                    "cacheReadInputTokens": 11,
                    "cacheCreationInputTokens": 5,
                    "outputTokens": 7,
                    "costUSD": 0.123,
                },
                "claude-haiku": {"inputTokens": 2, "outputTokens": 1, "costUSD": 0.001},
            }
        }
    )

    event = json.loads(log_path.read_text(encoding="utf-8"))
    assert event == {
        "event": "summarizer_usage",
        "in_new": 5,
        "cache_read": 11,
        "cache_write": 5,
        "out": 8,
        "provider_cost_usd": 0.124,
        "pid": event["pid"],
        "ts": event["ts"],
    }


def test_codex_usage_is_read_from_terminal_jsonl_without_calling_a_provider(tmp_path: Path) -> None:
    server = _load_server()
    totals = server.ContextMesh._codex_usage(
        b'{"type":"thread.started","thread_id":"t"}\n'
        b'{"type":"turn.completed","usage":{"input_tokens":12,"cached_input_tokens":7,'
        b'"cache_write_input_tokens":3,"output_tokens":5,"reasoning_output_tokens":2}}\n'
        b'{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,'
        b'"cache_write_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0}}\n'
    )

    assert totals == {
        "input_tokens": 13,
        "cached_input_tokens": 7,
        "cache_write_input_tokens": 3,
        "output_tokens": 6,
        "reasoning_output_tokens": 2,
    }


def test_deterministic_digest_stays_local_and_below_the_contextmesh_ratio(
    tmp_path: Path, monkeypatch
) -> None:
    server = _load_server()
    monkeypatch.setenv("CONTEXTMESH_SUMMARIZER_COMMAND", "deterministic")
    mesh = server.ContextMesh(log_path=tmp_path / "events.jsonl")
    content = "\n".join(
        ["from package import value"]
        + [f"def function_{index}(): return {index}" for index in range(60)]
    )

    digest = asyncio.run(mesh._chat_complete("ignored", content, 8000))

    assert digest is not None
    assert len(digest) <= int(len(content) * 0.25)
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events == ["summarizer_deterministic"]


def test_rrcv2_packet_marker_does_not_suppress_packet_insufficient_event(tmp_path: Path, monkeypatch) -> None:
    server = _load_server()
    source = tmp_path / "read.py"
    source.write_text("line 1\n", encoding="utf-8")
    log_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("REASONRENDER_ARM", "full")
    monkeypatch.setenv("CONTEXTMESH_RRC_PACKET", "1")

    mesh = server.ContextMesh(min_lines=1000, log_path=log_path)
    asyncio.run(mesh.read(str(source)))

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [(row["event"], row["path"]) for row in rows] == [("packet_insufficient", str(source))]


def test_four_simultaneous_reads_share_one_digest_gate(tmp_path: Path) -> None:
    server = _load_server()
    source = tmp_path / "shared.py"
    source.write_text("\n".join(f"line {number}" for number in range(90)), encoding="utf-8")
    stored = {}

    async def lookup(key: str):
        await asyncio.sleep(0)
        return stored.get(key)

    async def store(key: str, record):
        stored[key] = record
        return True

    async def summarize(_system: str, content: str, _max_tokens: int):
        await asyncio.sleep(0.02)
        return "digest: " + content.splitlines()[0]

    mesh = server.ContextMesh(
        lookup=lookup,
        store=store,
        summarize=summarize,
        min_lines=1,
        max_digest_ratio=0.9,
        gate_timeout_ms=200,
        log_path=tmp_path / "metrics.jsonl",
    )
    async def four_reads():
        return await asyncio.gather(*(mesh.read(str(source)) for _ in range(4)))

    replies = asyncio.run(four_reads())

    assert sum("Structural digest" in reply for reply in replies) == 3
    assert mesh.counters["read_raw"] == 1
    assert mesh.counters["digest_gate_hit"] == 3
    assert mesh.counters["digest_gate_wait"] == 3
    assert not mesh.gate
    raw = asyncio.run(mesh.read(str(source), offset=3, limit=2))
    assert "3: line 2" in raw and "Structural digest" not in raw


def test_rejected_or_failed_digest_releases_waiters_raw(tmp_path: Path) -> None:
    server = _load_server()
    source = tmp_path / "shared.py"
    source.write_text("\n".join(f"line {number}" for number in range(80)), encoding="utf-8")

    async def absent(_key: str):
        await asyncio.sleep(0)
        return None

    async def stored(_key: str, _record):
        return True

    async def too_large(_system: str, content: str, _max_tokens: int):
        await asyncio.sleep(0.01)
        return content

    mesh = server.ContextMesh(
        lookup=absent,
        store=stored,
        summarize=too_large,
        min_lines=1,
        max_digest_ratio=0.2,
        gate_timeout_ms=200,
        log_path=tmp_path / "metrics.jsonl",
    )
    async def four_reads():
        return await asyncio.gather(*(mesh.read(str(source)) for _ in range(4)))

    replies = asyncio.run(four_reads())
    assert all("Structural digest" not in reply for reply in replies)
    assert mesh.counters["digest_rejected"] == 1
    assert mesh.counters["digest_gate_wait"] == 3
    assert not mesh.gate


def test_real_provider_output_is_trimmed_to_the_digest_ratio(tmp_path: Path) -> None:
    server = _load_server()
    source = tmp_path / "shared.py"
    source.write_text("\n".join(f"line {number}" for number in range(80)), encoding="utf-8")
    records = {}

    async def absent(_key: str):
        return None

    async def stored(key: str, record):
        records[key] = record
        return True

    async def provider_ignored_cap(_system: str, content: str, _max_tokens: int):
        return content

    mesh = server.ContextMesh(
        lookup=absent,
        store=stored,
        min_lines=1,
        max_digest_ratio=0.2,
        log_path=tmp_path / "metrics.jsonl",
    )
    mesh._summarize = provider_ignored_cap

    async def exercise():
        await mesh.read(str(source))
        while mesh.gate:
            await asyncio.sleep(0)
        return await mesh.read(str(source))

    reply = asyncio.run(exercise())
    events = [json.loads(line)["event"] for line in (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]

    assert "Structural digest" in reply
    assert mesh.counters["digest_stored"] == 1
    assert events.count("digest_trimmed") == 1
    assert len(next(iter(records.values())).digest or "") <= len(source.read_text(encoding="utf-8")) * 0.2


def test_timeout_and_stdio_tool_surface_fail_open(tmp_path: Path) -> None:
    server = _load_server()
    source = tmp_path / "shared.py"
    source.write_text("\n".join(f"line {number}" for number in range(70)), encoding="utf-8")

    async def absent(_key: str):
        return None

    async def stored(_key: str, _record):
        return True

    async def slow(_system: str, _content: str, _max_tokens: int):
        await asyncio.sleep(0.05)
        return "short digest"

    mesh = server.ContextMesh(
        lookup=absent,
        store=stored,
        summarize=slow,
        min_lines=1,
        max_digest_ratio=0.9,
        gate_timeout_ms=5,
        log_path=tmp_path / "metrics.jsonl",
    )

    async def exercise():
        owner = asyncio.create_task(mesh.read(str(source)))
        while not mesh.gate:
            await asyncio.sleep(0)
        waiter = await mesh.read(str(source))
        return await owner, waiter

    _owner, waiter = asyncio.run(exercise())
    assert "Structural digest" not in waiter
    assert mesh.counters["digest_gate_timeout"] == 1

    response = asyncio.run(
        server.handle_request(
            mesh,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    )
    assert [tool["name"] for tool in response["result"]["tools"]] == ["read", "expand_result"]
    encoded = json.dumps(response)
    assert "contextmesh" not in encoded.lower() or "read" in encoded

    async def task_result_lookup(key: str):
        return "full task report" if key == "taskresult:task-1" else None

    result_mesh = server.ContextMesh(task_result_lookup=task_result_lookup, log_path=tmp_path / "result.jsonl")
    result_response = asyncio.run(
        server.handle_request(
            result_mesh,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "expand_result", "arguments": {"task_id": "task-1"}},
            },
        )
    )
    assert result_response["result"]["content"][0]["text"] == "full task report"


def test_persistence_failure_releases_all_waiters_as_raw(tmp_path: Path) -> None:
    server = _load_server()
    source = tmp_path / "shared.py"
    source.write_text("\n".join(f"line {number}" for number in range(70)), encoding="utf-8")

    async def absent(_key: str):
        await asyncio.sleep(0)
        return None

    async def summarize(_system: str, _content: str, _max_tokens: int):
        await asyncio.sleep(0.01)
        return "short digest"

    async def fail_store(_key: str, _record):
        await asyncio.sleep(0.01)
        raise RuntimeError("persistence down")

    mesh = server.ContextMesh(
        lookup=absent,
        store=fail_store,
        summarize=summarize,
        min_lines=1,
        max_digest_ratio=0.9,
        gate_timeout_ms=200,
        log_path=tmp_path / "metrics.jsonl",
    )

    async def four_reads():
        return await asyncio.gather(*(mesh.read(str(source)) for _ in range(4)))

    replies = asyncio.run(four_reads())
    assert all("Structural digest" not in reply for reply in replies)
    assert mesh.counters["digest_gate_hit"] == 0
    assert mesh.counters["errors"] == 1
    assert not mesh.gate


def test_stdio_process_four_reader_smoke(tmp_path: Path) -> None:
    """One spawned MCP process gives the raw owner plus three gated digests."""
    class FakeProvider(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            if self.path.endswith("/memory/search"):
                response = {"data": {"unprocessed_messages": []}}
            elif self.path.endswith("/memory/add"):
                response = {"data": {"status": "accumulated"}}
            else:
                time.sleep(0.02)
                assert payload["messages"][0]["role"] == "system"
                response = {"choices": [{"message": {"content": "shared-file digest"}}]}
            body = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    source = tmp_path / "shared.py"
    source.write_text("\n".join(f"line {number}" for number in range(80)), encoding="utf-8")
    metrics = tmp_path / "metrics.jsonl"
    provider = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    host, port = provider.server_address
    try:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
        ] + [
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": "read", "arguments": {"path": str(source)}},
            }
            for request_id in range(2, 6)
        ]
        environment = os.environ | {
            "CONTEXTMESH_EVEROS_URL": f"http://{host}:{port}",
            "CONTEXTMESH_SUMMARIZER_URL": f"http://{host}:{port}/chat/completions",
                "CONTEXTMESH_SUMMARIZER_API_KEY": "smoke",
            "CONTEXTMESH_MIN_LINES": "1",
            "CONTEXTMESH_LOG": str(metrics),
        }
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, str(root / "contextmesh" / "mcp" / "server.py")],
            cwd=root,
            env=environment,
            input="\n".join(json.dumps(request) for request in requests) + "\n",
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
    finally:
        provider.shutdown()
        thread.join(timeout=2)
        provider.server_close()

    replies = [json.loads(line) for line in completed.stdout.splitlines()]
    calls = [reply for reply in replies if reply.get("id") in {2, 3, 4, 5}]
    texts = [reply["result"]["content"][0]["text"] for reply in calls]
    assert sum("Structural digest" in text for text in texts) == 3
    metric_rows = [json.loads(line) for line in metrics.read_text(encoding="utf-8").splitlines()]
    events = [row["event"] for row in metric_rows]
    assert events.count("read_raw") == 1
    assert events.count("digest_gate_hit") == 3
    assert events.count("digest_gate_wait") == 3
    assert len({row["pid"] for row in metric_rows}) == 1
