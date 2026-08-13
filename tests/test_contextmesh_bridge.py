from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

from contextmesh.mcp import bridge as bridge_module
from contextmesh.mcp.bridge import BridgeConfig, handle_request
from contextmesh.mcp.shared_broker import SharedBriefBroker, SharedBrokerClient, SharedBrokerServer
from harness.four_worker_plan import build_overlap_ledger, freeze_worker_plans, manifest_sha256


def _entry():
    plans = freeze_worker_plans()
    return next(item for item in build_overlap_ledger(plans, manifest_sha256(plans)) if item.canonical_path == "ruleforge/evaluator.py")


def _brief() -> dict[str, str]:
    facts = "; ".join(_entry().required_facts)
    return {
        "purpose_and_api": "evaluate accepts value and returns it.",
        "data_and_dependencies": "No imports, collaborators, mutation, or side effects.",
        "behaviour_and_failures": "Direct return preserves the provided value.",
        "plan_step_facts": facts,
        "anchors": "evaluate_1@1-1; evaluate_30@30-30",
    }


def _source_body() -> str:
    return "\n".join(f"def evaluate_{index}(value): return value" for index in range(1, 31)) + "\n"


def test_distinct_stdio_bridges_forward_to_the_same_broker(tmp_path: Path) -> None:
    entry = _entry()
    owner_id, peer_id = entry.source_owner, entry.peer_workers[0]
    source_path = tmp_path / entry.canonical_path
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_source_body(), encoding="utf-8")
    broker = SharedBriefBroker(tmp_path, [entry])

    async def exercise() -> None:
        server = SharedBrokerServer(broker)
        host, port = await server.start()
        owner, peer = SharedBrokerClient(host, port), SharedBrokerClient(host, port)
        try:
            tools = await handle_request(owner, owner_id, {"id": 1, "method": "tools/list"})
            assert [tool["name"] for tool in tools["result"]["tools"]] == [  # type: ignore[index]
                "claim_source",
                "publish_file_brief",
                "read_source_chunk",
                "get_file_brief",
                "get_file_briefs",
            ]
            claim_reply = await handle_request(owner, owner_id, {"id": 2, "method": "tools/call", "params": {"name": "claim_source", "arguments": {"brief_id": entry.brief_id}}})
            claim = json.loads(claim_reply["result"]["content"][0]["text"])  # type: ignore[index]
            assert claim["brief_template"]["schema_version"] == "file-brief/v1"
            waiting = asyncio.create_task(handle_request(peer, peer_id, {"id": 3, "method": "tools/call", "params": {"name": "get_file_brief", "arguments": {"brief_id": entry.brief_id}}}))
            await handle_request(owner, owner_id, {"id": 4, "method": "tools/call", "params": {"name": "publish_file_brief", "arguments": {"brief_id": entry.brief_id, "source_hash": claim["source_hash"], "brief": {"schema_version": "file-brief/v1", **_brief()}}}})
            received = await waiting
            text = received["result"]["content"][0]["text"]  # type: ignore[index]
            assert "evaluate accepts value" in text
            assert entry.canonical_path not in text
        finally:
            await server.close()

    asyncio.run(exercise())


def test_one_owner_claim_is_published_before_any_next_claim(tmp_path: Path) -> None:
    entry = _entry()
    owner_id, peer_id = entry.source_owner, entry.peer_workers[0]
    source_path = tmp_path / entry.canonical_path
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_source_body(), encoding="utf-8")
    broker = SharedBriefBroker(tmp_path, [entry])

    async def call(client, worker_id: str, request_id: int, name: str, arguments: dict) -> dict:
        reply = await handle_request(client, worker_id, {"id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        return json.loads(reply["result"]["content"][0]["text"])  # type: ignore[index]

    async def exercise() -> None:
        server = SharedBrokerServer(broker)
        host, port = await server.start()
        owner, peer = SharedBrokerClient(host, port), SharedBrokerClient(host, port)
        try:
            claim = await call(owner, owner_id, 1, "claim_source", {"brief_id": entry.brief_id})
            assert "def evaluate_1" in claim["source_content"]
            await call(owner, owner_id, 2, "publish_file_brief", {
                "brief_id": entry.brief_id, "source_hash": claim["source_hash"], "brief": _brief(),
            })
            received = await call(peer, peer_id, 3, "get_file_briefs", {"brief_ids": [entry.brief_id]})
            assert "evaluate accepts value" in str(received)
            assert "def evaluate_1" not in str(received)
            reused = await call(owner, owner_id, 4, "claim_source", {"brief_id": entry.brief_id})
            assert reused["claim_kind"] == "unchanged_reuse"
        finally:
            await server.close()

    asyncio.run(exercise())


def test_two_subprocess_bridges_record_one_shared_broker_identity(tmp_path: Path) -> None:
    entry = _entry()
    source_path = tmp_path / entry.canonical_path
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_source_body(), encoding="utf-8")
    broker = SharedBriefBroker(tmp_path, [entry])

    async def exercise() -> None:
        server = SharedBrokerServer(broker)
        host, port = await server.start()
        env = os.environ.copy()
        env.update({"CONTEXTMESH_BROKER_HOST": host, "CONTEXTMESH_BROKER_PORT": str(port), "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        processes = []
        try:
            for worker_id in (entry.source_owner, entry.peer_workers[0]):
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "contextmesh.mcp.bridge", stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env={**env, "CONTEXTMESH_WORKER_ID": worker_id}, cwd=Path(__file__).resolve().parents[1],
                )
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
                await process.stdin.drain()
                assert await asyncio.wait_for(process.stdout.readline(), timeout=2)
                processes.append(process)
            events = [item for item in broker.events if item["event"] == "bridge_started"]
            assert len(events) == 2
            assert len({item["bridge_pid"] for item in events}) == 2
            assert len({item["broker_identity"] for item in events}) == 1
        finally:
            for process in processes:
                assert process.stdin is not None
                process.stdin.close()
                await process.wait()
            await server.close()

    asyncio.run(exercise())


def test_bridge_negotiates_the_client_mcp_protocol_version() -> None:
    async def exercise() -> None:
        class UnusedClient:
            pass

        reply = await handle_request(
            UnusedClient(),  # type: ignore[arg-type]
            "worker-01",
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        )
        assert reply == {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "contextmesh-bridge", "version": "1"},
            },
        }

    asyncio.run(exercise())


def test_stdio_bridge_does_not_serialize_a_publish_behind_a_waiting_read(monkeypatch) -> None:
    """A waiting peer read must not deadlock a later owner publication."""

    class Input:
        def __init__(self) -> None:
            self.buffer = self
            self.lines = [b'{"id": 1}\n', b'{"id": 2}\n', b""]

        def readline(self) -> bytes:
            return self.lines.pop(0)

    class Output:
        def __init__(self) -> None:
            self.values: list[str] = []

        def write(self, value: str) -> int:
            self.values.append(value)
            return len(value)

        def flush(self) -> None:
            pass

    started = asyncio.Event()
    publish_processed = asyncio.Event()
    release_wait = asyncio.Event()

    async def fake_handle(_client, _worker_id: str, request):
        if request["id"] == 1:
            started.set()
            await release_wait.wait()
        else:
            await started.wait()
            publish_processed.set()
        return {"jsonrpc": "2.0", "id": request["id"], "result": {}}

    stdin, stdout = Input(), Output()
    monkeypatch.setattr(bridge_module.sys, "stdin", stdin)
    monkeypatch.setattr(bridge_module.sys, "stdout", stdout)
    monkeypatch.setattr(bridge_module, "handle_request", fake_handle)

    async def exercise() -> None:
        server = asyncio.create_task(
            bridge_module.serve_stdio(BridgeConfig(host="127.0.0.1", port=1, worker_id="worker-01"))
        )
        await asyncio.wait_for(publish_processed.wait(), timeout=1)
        release_wait.set()
        await server

    asyncio.run(exercise())
    assert [json.loads(value)["id"] for value in stdout.values] == [2, 1]


def test_publish_rejection_reaches_the_mcp_surface_with_the_exact_reason(tmp_path: Path) -> None:
    entry = _entry()
    owner_id = entry.source_owner
    source_path = tmp_path / entry.canonical_path
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_source_body(), encoding="utf-8")
    broker = SharedBriefBroker(tmp_path, [entry])

    async def exercise() -> None:
        server = SharedBrokerServer(broker)
        host, port = await server.start()
        owner = SharedBrokerClient(host, port)
        try:
            claim_reply = await handle_request(owner, owner_id, {"id": 1, "method": "tools/call", "params": {"name": "claim_source", "arguments": {"brief_id": entry.brief_id}}})
            claim = json.loads(claim_reply["result"]["content"][0]["text"])  # type: ignore[index]
            incomplete = _brief()
            incomplete["plan_step_facts"] = "No source contract is present."
            reply = await handle_request(owner, owner_id, {"id": 2, "method": "tools/call", "params": {"name": "publish_file_brief", "arguments": {"brief_id": entry.brief_id, "source_hash": claim["source_hash"], "brief": incomplete}}})
            assert reply["error"]["code"] == -32000
            assert "missing required_source_fact" in reply["error"]["message"]
            assert "brief_incomplete" in reply["error"]["message"]
        finally:
            await server.close()

    asyncio.run(exercise())
