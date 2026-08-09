from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

HANDLERS = ("orders", "products", "reviews", "users")
SHARED = ("src/models.js", "src/utils.js", "src/middleware.js")


def _sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    path.chmod(0o600)


def _manifest(arm: Path, round_id: str, side: str, backend: str) -> dict[str, str]:
    target = arm / "target"
    (target / "src/handlers").mkdir(parents=True)
    handler_hashes: dict[str, str] = {}
    for handler in HANDLERS:
        path = target / f"src/handlers/{handler}.js"
        path.write_text(f"export function {handler}() {{ return true }}\n")
        handler_hashes[handler] = _sha(path.read_bytes())
    files: list[dict[str, object]] = []
    for relative in SHARED:
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((f"export const {path.stem} = true;\n") * 100)
        digest = f"digest:{relative}"
        row: dict[str, object] = {
            "v": 1,
            "round_id": round_id,
            "arm": side,
            "memory_backend": backend,
            "path": relative,
            "raw_sha256": _sha(path.read_bytes()),
            "digest_sha256": _sha(digest),
            "digest": digest if backend == "sqlite" else None,
            "everos_key": f"key:{relative}" if backend == "everos" else None,
            "raw_chars": path.stat().st_size,
            "digest_chars": len(digest),
        }
        files.append(row)
    target_stat = target.resolve().stat()
    manifest: dict[str, object] = {
        "v": 1,
        "round_id": round_id,
        "arm": side,
        "memory_backend": backend,
        "target_root": str(target.resolve()),
        "target_device": target_stat.st_dev,
        "target_inode": target_stat.st_ino,
        "files": files,
    }
    unsigned = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest["seal"] = _sha(unsigned)
    path = arm / "seed-manifest.json"
    path.write_text(json.dumps(manifest, separators=(",", ":")))
    path.chmod(0o600)
    return handler_hashes


def _usage(component: str, session: str, *, agent_id: str | None = None) -> dict[str, object]:
    return {
        "v": 1,
        "event": "native_usage",
        "memory_backend": "placeholder",
        "component": component,
        "agent_id": agent_id,
        "session_id": session,
        "model": "gpt-5.5",
        "transcript_sha256": _sha(session),
        "input_tokens": 90,
        "cached_input_tokens": 20,
        "cache_write_input_tokens": 0,
        "output_tokens": 10,
        "reasoning_output_tokens": 4,
        "total_tokens": 100,
    }


def _arm(root: Path, round_id: str, side: str, backend: str) -> None:
    arm = root / "runs/rrd-demo" / round_id / side
    hashes = _manifest(arm, round_id, side, backend)
    hooks: list[dict[str, object]] = []
    agents = [f"agent-{name}" for name in HANDLERS]
    for handler, agent in zip(HANDLERS, agents, strict=True):
        tool = f"tool-{handler}"
        hooks.extend(
            [
                {
                    "v": 1,
                    "event": "assignment",
                    "memory_backend": backend,
                    "tool_use_id": tool,
                    "handler": f"src/handlers/{handler}.js",
                    "handler_sha256": hashes[handler],
                },
                {
                    "v": 1,
                    "event": "spawned",
                    "memory_backend": backend,
                    "tool_use_id": tool,
                    "agent_id": agent,
                },
                {
                    "v": 1,
                    "event": "shared_context",
                    "memory_backend": backend,
                    "agent_id": agent,
                    "receipts": [
                        {
                            "path": relative,
                            "digest_sha256": _sha(f"digest:{relative}"),
                            "raw_sha256": _sha((arm / "target" / relative).read_bytes()),
                            "hit": True,
                        }
                        for relative in SHARED
                    ],
                },
                {
                    "v": 1,
                    "event": "result_final",
                    "memory_backend": backend,
                    "agent_id": agent,
                    "delivered_chars": 80,
                    "delivered_sha256": _sha(f"report-{agent}"),
                },
            ]
        )
        usage = _usage("worker", f"session-{agent}", agent_id=agent)
        usage["memory_backend"] = backend
        hooks.append(usage)
    hooks.extend(
        [
            *[
                {
                    "v": 1,
                    "event": "wait_result",
                    "memory_backend": backend,
                    "completed_agent_ids": [agent],
                    "result_count": 1,
                    "timed_out": False,
                }
                for agent in agents
            ],
            {
                "v": 1,
                "event": "compression_delivered",
                "memory_backend": backend,
                "receipts": {
                    agent: {
                        "receipt": _sha(agent)[:20],
                        "sha256": _sha(f"report-{agent}"),
                        "bytes": 5000,
                        "path": f"raw-results/{agent}.txt",
                    }
                    for agent in agents[:3]
                },
                "raw_bytes": 20_000,
                "delivered_bytes": 1800,
            },
            {
                "v": 1,
                "event": "compression_bypass",
                "memory_backend": backend,
                "agent_ids": [agents[3]],
                "receipts": {
                    agents[3]: {
                        "receipt": _sha(agents[3])[:20],
                        "sha256": _sha(f"report-{agents[3]}"),
                        "bytes": 500,
                        "path": f"raw-results/{agents[3]}.txt",
                    }
                },
                "raw_bytes": 600,
                "candidate_bytes": 500,
            },
            {
                "v": 1,
                "event": "root_merge",
                "memory_backend": backend,
                "chars": 500,
                "sha256": _sha("root"),
            },
        ]
    )
    root_usage = _usage("root", f"session-{side}-root")
    root_usage["memory_backend"] = backend
    hooks.append(root_usage)
    _jsonl(arm / "hook-events.jsonl", hooks)

    branches = ["miss"] * 4 if side == "a" else ["miss", "hit", "hit", "hit"]
    _jsonl(
        arm / "rrc-events.jsonl",
        [
            {
                "event": "packet",
                "memory_backend": backend,
                "branch": branch,
                "task_id": f"task-{handler}",
                "handler": f"src/handlers/{handler}.js",
            }
            for handler, branch in zip(HANDLERS, branches, strict=True)
        ],
    )
    _jsonl(
        arm / "rrc-model-events.jsonl",
        [
            {
                "parse_status": "ok",
                "usage": {
                    "prompt_tokens": 40,
                    "completion_tokens": 10,
                    "total_tokens": 50,
                },
            }
            for _ in range(branches.count("miss"))
        ],
    )


def _round(root: Path, backend: str = "sqlite") -> str:
    round_id = f"rrd-{backend}-fixture"
    directory = root / "runs/rrd-demo" / round_id
    directory.mkdir(parents=True)
    meta = directory / "round-meta.json"
    meta.write_text(
        json.dumps(
            {
                "v": 2,
                "round_id": round_id,
                "memory_backend": backend,
                "provider": "native-codex",
                "model": "gpt-5.5",
            },
            separators=(",", ":"),
        )
    )
    meta.chmod(0o600)
    _arm(root, round_id, "a", backend)
    _arm(root, round_id, "b", backend)
    return round_id


@pytest.mark.parametrize("backend", ["sqlite", "everos"])
def test_native_codex_meter_reaches_ready_for_both_memory_backends(
    tmp_path: Path, backend: str
) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    round_id = _round(tmp_path, backend)
    snapshot = collect(round_id, memory_backend=backend, root=tmp_path)

    assert snapshot["a"]["ready"] == snapshot["b"]["ready"] == 1
    assert snapshot["a"]["combined_tokens"] == 700
    assert snapshot["b"]["combined_tokens"] == 550
    assert (snapshot["a"]["misses"], snapshot["a"]["hits"]) == (4, 0)
    assert (snapshot["b"]["misses"], snapshot["b"]["hits"]) == (1, 3)


def test_missing_native_worker_usage_is_not_ready(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    round_id = _round(tmp_path)
    path = tmp_path / f"runs/rrd-demo/{round_id}/a/hook-events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    _jsonl(
        path,
        [
            row
            for row in rows
            if not (row.get("event") == "native_usage" and row.get("agent_id") == "agent-orders")
        ],
    )

    side = collect(round_id, memory_backend="sqlite", root=tmp_path)["a"]
    assert side["usage_ok"] == 0
    assert side["ready"] == 0


def test_malformed_native_usage_and_jsonl_are_not_coerced_to_zero(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    round_id = _round(tmp_path)
    hook_path = tmp_path / f"runs/rrd-demo/{round_id}/b/hook-events.jsonl"
    rows = [json.loads(line) for line in hook_path.read_text().splitlines()]
    usage = next(row for row in rows if row.get("component") == "root")
    usage["input_tokens"] = "bad"
    _jsonl(hook_path, rows)
    side = collect(round_id, memory_backend="sqlite", root=tmp_path)["b"]
    assert side["usage_ok"] == 0 and side["ready"] == 0

    with hook_path.open("a") as stream:
        stream.write("{truncated\n")
    side = collect(round_id, memory_backend="sqlite", root=tmp_path)["b"]
    assert side["evidence_ok"] == 0 and side["ready"] == 0


def test_backend_or_compression_mismatch_is_not_ready(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    round_id = _round(tmp_path)
    path = tmp_path / f"runs/rrd-demo/{round_id}/a/hook-events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    compression = next(row for row in rows if row.get("event") == "compression_delivered")
    del compression["receipts"]["agent-orders"]
    _jsonl(path, rows)

    side = collect(round_id, memory_backend="sqlite", root=tmp_path)["a"]
    assert side["protocol_ok"] == 0 and side["ready"] == 0


def test_render_discloses_observational_not_billing_exact(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect, render

    round_id = _round(tmp_path)
    output = render(
        round_id,
        collect(round_id, memory_backend="sqlite", root=tmp_path),
        "sqlite",
    )
    assert "READY" in output
    assert "provider-visible" in output
    assert "billing_exact=false" in output
    assert "hidden_retry_observable=false" in output
