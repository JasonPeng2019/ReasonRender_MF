from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

HANDLERS = ("users", "products", "orders", "reviews")
SHARED = ("src/models.js", "src/utils.js", "src/middleware.js")


def _jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _valid_arm(root: Path, round_id: str, side: str) -> None:
    arm = root / f"runs/rrd-demo/{round_id}/{side}"
    target = arm / "target"
    (target / "src/handlers").mkdir(parents=True)
    handler_hashes: dict[str, str] = {}
    for handler in HANDLERS:
        path = target / f"src/handlers/{handler}.js"
        path.write_text(f"export const {handler} = true\n")
        handler_hashes[handler] = hashlib.sha256(path.read_bytes()).hexdigest()
    shared_hashes: dict[str, tuple[str, str]] = {}
    manifest_rows: list[dict[str, object]] = []
    for path_string in SHARED:
        path = target / path_string
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"export const source = '{path_string}'\n" * 200)
        raw_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        digest_hash = hashlib.sha256(f"digest:{path_string}".encode()).hexdigest()
        shared_hashes[path_string] = (raw_hash, digest_hash)
        manifest_rows.append(
            {
                "path": path_string,
                "raw_sha256": raw_hash,
                "digest_sha256": digest_hash,
                "raw_chars": 4000,
                "digest_chars": 1000,
            }
        )
    target_stat = target.resolve().stat()
    manifest: dict[str, object] = {
        "v": 1,
        "round_id": round_id,
        "arm": side,
        "target_root": str(target.resolve()),
        "target_device": target_stat.st_dev,
        "target_inode": target_stat.st_ino,
        "files": manifest_rows,
    }
    serialized = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest["seal"] = hashlib.sha256(serialized.encode()).hexdigest()
    (arm / "seed-manifest.json").write_text(json.dumps(manifest))
    hooks: list[dict[str, object]] = []
    compression_receipt = "a" * 20
    for index, handler in enumerate(HANDLERS):
        tool = f"call-{handler}"
        agent = f"agent-{handler}"
        hooks.extend(
            [
                {
                    "event": "assignment",
                    "ts": 1 + index * 0.01,
                    "assignment_id": f"assignment-{handler}",
                    "tool_use_id": tool,
                    "handler": f"src/handlers/{handler}.js",
                    "handler_sha256": handler_hashes[handler],
                },
                {"event": "spawned", "ts": 2, "tool_use_id": tool, "agent_id": agent},
                {
                    "event": "shared_context",
                    "ts": 3 + index * 0.01,
                    "agent_id": agent,
                    "receipts": [
                        {
                            "path": path,
                            "hit": True,
                            "raw_sha256": shared_hashes[path][0],
                            "digest_sha256": shared_hashes[path][1],
                        }
                        for path in SHARED
                    ],
                },
                {
                    "event": "result_final",
                    "ts": 8 + index * 0.01,
                    "agent_id": agent,
                    "compressed": index == 0,
                    "compression_receipt": compression_receipt if index == 0 else None,
                    "delivered_chars": len(f"report-{agent}"),
                    "delivered_sha256": hashlib.sha256(f"report-{agent}".encode()).hexdigest(),
                },
            ]
        )
    hooks.extend(
        [
            {
                "event": "wait_result",
                "ts": 9,
                "agent_ids": [f"agent-{name}" for name in HANDLERS],
                "completed_agent_ids": [f"agent-{name}" for name in HANDLERS],
                "completed_results": {
                    f"agent-{name}": {
                        "chars": len(f"report-agent-{name}"),
                        "sha256": hashlib.sha256(f"report-agent-{name}".encode()).hexdigest(),
                    }
                    for name in HANDLERS
                },
                "result_count": 4,
                "timed_out": False,
            },
            {"event": "root_merge", "ts": 10, "chars": 500, "sha256": "d" * 64},
        ]
    )
    _jsonl(arm / "hook-events.jsonl", hooks)
    _jsonl(
        arm / "proxy-events.jsonl",
        [{"event": "result_compress", "receipt": compression_receipt, "ts": 7}],
    )
    branches = ["miss"] * 4 if side == "a" else ["miss", "hit", "hit", "hit"]
    _jsonl(
        arm / "rrc-events.jsonl",
        [
            {
                "event": "packet",
                "branch": branch,
                "task_id": f"assignment-{handler}",
                "handler": f"src/handlers/{handler}.js",
            }
            for handler, branch in zip(HANDLERS, branches, strict=True)
        ],
    )


def _valid_round(root: Path, round_id: str = "rrd-test") -> None:
    rows: list[dict[str, object]] = []
    for side in ("a", "b"):
        rows.extend(
            [
                {
                    "session": f"rrd-demo-{round_id}-{side}-outer",
                    "measurement_state": "exact",
                    "input_tokens": 800 if side == "a" else 700,
                    "output_tokens": 200,
                },
                {
                    "session": f"rrd-demo-{round_id}-{side}-summarizer",
                    "measurement_state": "exact",
                    "input_tokens": 50,
                    "output_tokens": 25,
                },
                {
                    "session": f"rrd-demo-{round_id}-{side}-planner",
                    "measurement_state": "exact",
                    "input_tokens": 400 if side == "a" else 100,
                    "output_tokens": 40 if side == "a" else 10,
                },
            ]
        )
        _valid_arm(root, round_id, side)
    rows.append(
        {
            "session": f"rrd-demo-{round_id}-setup-seed-a",
            "measurement_state": "exact",
            "input_tokens": 50,
            "output_tokens": 10,
        }
    )
    _jsonl(root / "runs/rrd-tokens.jsonl", rows)


def test_codex_meter_correlates_evidence_and_uses_disjoint_tollgate_totals(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    _valid_round(tmp_path)
    snapshot = collect("rrd-test", root=tmp_path)

    assert snapshot["a"]["outer_tokens"] == 1000
    assert snapshot["a"]["summarizer_tokens"] == 75
    assert snapshot["a"]["planner_tokens"] == 440
    assert snapshot["a"]["combined_tokens"] == 1515
    assert snapshot["b"]["combined_tokens"] == 1085
    assert snapshot["a"]["setup_tokens"] == snapshot["b"]["setup_tokens"] == 60
    assert snapshot["a"]["workers"] == snapshot["b"]["workers"] == 4
    assert snapshot["a"]["overlap"] == snapshot["b"]["overlap"] == 1
    assert snapshot["b"]["misses"] == 1 and snapshot["b"]["hits"] == 3
    assert snapshot["a"]["digest_worker_sessions"] == 4
    assert snapshot["a"]["results"] == 4
    assert snapshot["a"]["ready"] == snapshot["b"]["ready"] == 1


def test_inexact_or_foreign_arm_traffic_makes_meter_not_ready(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    _valid_round(tmp_path)
    with (tmp_path / "runs/rrd-tokens.jsonl").open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "session": "rrd-demo-rrd-test-a-foreign",
                    "measurement_state": "estimated",
                    "input_tokens": 1,
                    "output_tokens": 1,
                }
            )
            + "\n"
        )

    side = collect("rrd-test", root=tmp_path)["a"]
    assert side["inexact_records"] == 1
    assert side["ready"] == 0


def test_missing_auxiliary_tollgate_partition_is_not_ready(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    _valid_round(tmp_path)
    token_path = tmp_path / "runs/rrd-tokens.jsonl"
    rows = [json.loads(line) for line in token_path.read_text().splitlines()]
    rows = [row for row in rows if row["session"] != "rrd-demo-rrd-test-a-summarizer"]
    _jsonl(token_path, rows)

    side = collect("rrd-test", root=tmp_path)["a"]
    assert side["summarizer_records"] == 0
    assert side["ready"] == 0


def test_each_core_evidence_loss_falsifies_readiness(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    _valid_round(tmp_path)
    hook_path = tmp_path / "runs/rrd-demo/rrd-test/a/hook-events.jsonl"
    rows = [json.loads(line) for line in hook_path.read_text().splitlines()]
    rows = [row for row in rows if row["event"] != "root_merge"]
    _jsonl(hook_path, rows)

    side = collect("rrd-test", root=tmp_path)["a"]
    assert side["root_merges"] == 0
    assert side["ready"] == 0


def test_spawn_and_packet_ids_must_correlate_to_assignments(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    _valid_round(tmp_path)
    hook_path = tmp_path / "runs/rrd-demo/rrd-test/a/hook-events.jsonl"
    hooks = [json.loads(line) for line in hook_path.read_text().splitlines()]
    next(row for row in hooks if row["event"] == "spawned")["tool_use_id"] = "unrelated-call"
    _jsonl(hook_path, hooks)

    assert collect("rrd-test", root=tmp_path)["a"]["ready"] == 0


def test_wait_timeout_or_missing_delivered_result_falsifies_readiness(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    _valid_round(tmp_path)
    hook_path = tmp_path / "runs/rrd-demo/rrd-test/a/hook-events.jsonl"
    hooks = [json.loads(line) for line in hook_path.read_text().splitlines()]
    next(row for row in hooks if row["event"] == "wait_result")["timed_out"] = True
    _jsonl(hook_path, hooks)
    assert collect("rrd-test", root=tmp_path)["a"]["ready"] == 0

    next(row for row in hooks if row["event"] == "wait_result")["timed_out"] = False
    next(row for row in hooks if row["event"] == "result_final")["delivered_chars"] = 0
    _jsonl(hook_path, hooks)
    assert collect("rrd-test", root=tmp_path)["a"]["ready"] == 0


def test_current_source_mutation_invalidates_previously_valid_receipts(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    _valid_round(tmp_path)
    shared = tmp_path / "runs/rrd-demo/rrd-test/a/target/src/models.js"
    shared.write_text(shared.read_text() + "// changed after hook evidence\n")

    side = collect("rrd-test", root=tmp_path)["a"]
    assert side["source_state_ok"] == 0
    assert side["ready"] == 0


def test_current_source_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import _current_file_hash

    target = tmp_path / "target"
    (target / "src/handlers").mkdir(parents=True)
    os.mkfifo(target / "src/handlers/users.js")
    started = time.monotonic()
    assert _current_file_hash(target, "src/handlers/users.js") is None
    assert time.monotonic() - started < 0.25


def test_serial_worker_lifetimes_and_missing_compression_are_not_ready(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    _valid_round(tmp_path)
    hook_path = tmp_path / "runs/rrd-demo/rrd-test/b/hook-events.jsonl"
    rows = [json.loads(line) for line in hook_path.read_text().splitlines()]
    _jsonl(hook_path.parent / "proxy-events.jsonl", [])
    for index, row in enumerate(row for row in rows if row["event"] == "result_final"):
        row["ts"] = 2.5 + index * 0.01
    _jsonl(hook_path, rows)

    side = collect("rrd-test", root=tmp_path)["b"]
    assert side["overlap"] == 0
    assert side["task_compressed"] == 0
    assert side["ready"] == 0


def test_meter_labels_single_pair_noncausal_and_never_mentions_opencode(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect, render

    _valid_round(tmp_path)
    output = render("rrd-test", collect("rrd-test", root=tmp_path))

    assert "Codex ContextMesh + ReasonRenderCoding" in output
    assert "READY" in output and "PAIRED SAMPLE" in output
    assert "not a causal estimate" in output
    assert "Setup/seed tokens (excluded" in output
    assert "counterfactual" in output
    assert "across 4 workers" in output
    assert "OpenCode" not in output


def test_combined_meter_marks_empty_runs_not_ready() -> None:
    from contextmesh.scripts.rrd_combined_meter import render

    output = render("rrd-empty", {"a": {}, "b": {}})
    assert "NOT READY" in output
    assert "INCOMPLETE" in output
