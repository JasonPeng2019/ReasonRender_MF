"""Worker-local stdio MCP bridge to one arm-local shared brief broker."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Mapping

from contextmesh.mcp.file_brief import SUMMARY_FIELDS
from contextmesh.mcp.shared_broker import SharedBrokerClient


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    host: str
    port: int
    worker_id: str

    @classmethod
    def from_environment(cls) -> "BridgeConfig":
        host = os.environ.get("CONTEXTMESH_BROKER_HOST", "")
        worker_id = os.environ.get("CONTEXTMESH_WORKER_ID", "")
        try:
            port = int(os.environ.get("CONTEXTMESH_BROKER_PORT", ""))
        except ValueError as error:
            raise ValueError("CONTEXTMESH_BROKER_PORT must be an integer") from error
        if not host or not worker_id or port < 1 or port > 65535:
            raise ValueError("bridge requires broker host, port, and worker id")
        return cls(host=host, port=port, worker_id=worker_id)


_TOOLS = (
    {
        "name": "claim_source",
        "description": (
            "Declared owner only: receive one raw overlapping source body for an initial/invalidated revision, "
            "a prior complete brief plus Git diff for a later changed revision, or an unchanged_reuse summary with no source."
        ),
        "inputSchema": {"type": "object", "required": ["brief_id"], "properties": {"brief_id": {"type": "string"}}},
    },
    {
        "name": "publish_file_brief",
        "description": (
            "Declared source owner only: publish exactly the five source-summary strings. "
            "The claim's schema/path/binding metadata belongs to the broker and must not be copied into brief. "
            "If copied, it is ignored rather than sent to peers."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["brief_id", "source_hash", "brief"],
            "properties": {
                "brief_id": {"type": "string"},
                "source_hash": {"type": "string"},
                "brief": {
                    "type": "object",
                    "required": list(SUMMARY_FIELDS),
                    "properties": {field: {"type": "string"} for field in SUMMARY_FIELDS},
                },
            },
        },
    },
    {
        "name": "read_source_chunk",
        "description": "Declared source owner only: retrieve the next sequential raw chunk after claim_source for a large source.",
        "inputSchema": {
            "type": "object",
            "required": ["brief_id", "chunk_index"],
            "properties": {"brief_id": {"type": "string"}, "chunk_index": {"type": "integer", "minimum": 1}},
        },
    },
    {
        "name": "get_file_brief",
        "description": "Declared reader only: a peer or the source owner of an unchanged later-stage reuse may receive a complete brief; raw source is never returned.",
        "inputSchema": {"type": "object", "required": ["brief_id"], "properties": {"brief_id": {"type": "string"}}},
    },
    {
        "name": "get_file_briefs",
        "description": "Declared reader only: retrieve every already-declared brief in one call; raw source is never returned.",
        "inputSchema": {
            "type": "object",
            "required": ["brief_ids"],
            "properties": {"brief_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
        },
    },
)


def _summary_only(value: object) -> dict[str, object]:
    """Keep five owner-authored source facts while ignoring copied audit metadata."""

    if not isinstance(value, Mapping):
        raise ValueError("brief must be an object")
    missing = [field for field in SUMMARY_FIELDS if field not in value]
    if missing:
        raise ValueError(f"brief is missing required summary fields: {', '.join(missing)}")
    return {field: value[field] for field in SUMMARY_FIELDS}


def _result(request_id: object, result: Mapping[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def handle_request(
    client: SharedBrokerClient, worker_id: str, request: Mapping[str, Any]
) -> dict[str, object] | None:
    """Serve the small MCP surface without giving workers a generic file read tool."""

    request_id = request.get("id")
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        # The client selects the MCP revision during initialization.  Returning
        # a hard-coded older revision makes a current Codex client silently drop
        # an otherwise valid stdio server after it starts.
        init_params = request.get("params")
        version = init_params.get("protocolVersion", "2024-11-05") if isinstance(init_params, Mapping) else "2024-11-05"
        if not isinstance(version, str):
            version = "2024-11-05"
        return _result(
            request_id,
            {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": {"name": "contextmesh-bridge", "version": "1"}},
        )
    if method == "tools/list":
        return _result(request_id, {"tools": list(_TOOLS)})
    if method != "tools/call":
        return _error(request_id, -32601, f"unknown method {method}")
    params = request.get("params")
    if not isinstance(params, Mapping):
        return _error(request_id, -32602, "tools/call params must be an object")
    name, arguments = params.get("name"), params.get("arguments", {})
    if not isinstance(name, str) or not isinstance(arguments, Mapping):
        return _error(request_id, -32602, "tool name and arguments are required")
    try:
        if name == "claim_source":
            result = await client.claim_source(str(arguments["brief_id"]), worker_id)
        elif name == "read_source_chunk":
            result = await client.read_source_chunk(
                str(arguments["brief_id"]), worker_id, arguments["chunk_index"]
            )
        elif name == "publish_file_brief":
            await client.publish_file_brief(
                str(arguments["brief_id"]), worker_id, str(arguments["source_hash"]), _summary_only(arguments["brief"])
            )
            result = {"published": True}
        elif name == "get_file_brief":
            result = await client.get_file_brief(str(arguments["brief_id"]), worker_id)
        elif name == "get_file_briefs":
            brief_ids = arguments["brief_ids"]
            if not isinstance(brief_ids, list) or not all(isinstance(brief_id, str) for brief_id in brief_ids):
                raise ValueError("brief_ids must be an array of strings")
            result = await client.get_file_briefs(brief_ids, worker_id)
        else:
            return _error(request_id, -32602, f"unknown tool {name}")
    except (KeyError, ValueError) as error:
        return _error(request_id, -32602, str(error))
    except Exception as error:  # BrokerError is deliberately visible to the worker.
        return _error(request_id, -32000, str(error))
    return _result(
        request_id,
        {"content": [{"type": "text", "text": json.dumps(result, sort_keys=True, separators=(",", ":"))}]},
    )


async def serve_stdio(config: BridgeConfig) -> None:
    """Run one worker's bridge over stdio, forwarding every request to its broker."""

    client = SharedBrokerClient(config.host, config.port)
    # The broker retains this non-secret identity event for the measured
    # topology proof.  A no-provider health/model-surface probe intentionally
    # uses an unreachable endpoint, so failure here must not hide the MCP
    # surface that probe is testing.
    try:
        await asyncio.wait_for(
            client.bridge_identity(
                config.worker_id,
                os.getpid(),
                f"{config.host}:{config.port}",
            ),
            timeout=0.25,
        )
    except Exception:
        pass
    output_lock = asyncio.Lock()
    pending: set[asyncio.Task[None]] = set()

    async def respond(request: Mapping[str, Any]) -> None:
        try:
            response = await handle_request(client, config.worker_id, request)
        except (ValueError, json.JSONDecodeError) as error:
            response = _error(None, -32700, str(error))
        if response is not None:
            # A peer read is allowed to wait for a different source owner. Do
            # not let that wait keep this same worker's later publication from
            # reaching the broker; serialize only writes to the JSON-RPC pipe.
            async with output_lock:
                sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
                sys.stdout.flush()

    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        try:
            request = json.loads(line)
            if not isinstance(request, Mapping):
                raise ValueError("request must be an object")
        except (ValueError, json.JSONDecodeError) as error:
            async with output_lock:
                sys.stdout.write(json.dumps(_error(None, -32700, str(error)), sort_keys=True, separators=(",", ":")) + "\n")
                sys.stdout.flush()
            continue
        task = asyncio.create_task(respond(request))
        pending.add(task)
        task.add_done_callback(pending.discard)
    if pending:
        await asyncio.gather(*pending)


def main() -> int:
    try:
        asyncio.run(serve_stdio(BridgeConfig.from_environment()))
    except ValueError as error:
        print(f"ContextMesh bridge configuration error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess in the runner.
    raise SystemExit(main())
