from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _db(path: Path, workers: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE session (id TEXT, parent_id TEXT)")
    db.execute("INSERT INTO session VALUES ('root', NULL)")
    db.executemany(
        "INSERT INTO session VALUES (?, 'root')",
        [(f"worker-{index}",) for index in range(workers)],
    )
    db.commit()
    db.close()


def test_combined_meter_uses_disjoint_token_authorities(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    round_id = "rrd-test"
    _jsonl(
        tmp_path / "runs/tokens.jsonl",
        [
            {
                "session": f"rrd-demo-{round_id}-a",
                "measurement_state": "exact",
                "input_tokens": 800,
                "output_tokens": 200,
            },
            {
                "session": f"rrd-demo-{round_id}-a-summarizer",
                "measurement_state": "exact",
                "input_tokens": 50,
                "output_tokens": 50,
            },
            {
                "session": f"rrd-demo-{round_id}-b",
                "measurement_state": "exact",
                "input_tokens": 700,
                "output_tokens": 200,
            },
            {
                "session": "unrelated-session",
                "measurement_state": "estimated",
                "input_tokens": 9999,
                "output_tokens": 9999,
            },
        ],
    )
    for side, rows in {
        "a": [{"event": "packet", "branch": "miss", "planner_tokens": 100} for _ in range(4)],
        "b": [
            {"event": "packet", "branch": "miss", "planner_tokens": 100},
            *[{"event": "packet", "branch": "hit", "planner_tokens": 0} for _ in range(3)],
        ],
    }.items():
        arm = tmp_path / f"runs/rrd-demo/{round_id}/{side}"
        _jsonl(arm / "rrc-events.jsonl", rows)
        planner_calls = 4 if side == "a" else 1
        _jsonl(
            arm / "rrc-model-events.jsonl",
            [{"parse_status": "ok", "usage": {"total_tokens": 100}} for _ in range(planner_calls)],
        )
        _jsonl(
            arm / "contextmesh.jsonl",
            [
                {"event": "plugin_loaded"},
                *[
                    {
                        "event": "digest_hit",
                        "sessionID": f"worker-{index}",
                        "savedTokens": 125,
                    }
                    for index in range(4)
                ],
                {"event": "task_compressed"},
            ],
        )
        _db(arm / "opencode.db", workers=4)

    snapshot = collect(round_id, root=tmp_path)

    assert snapshot["a"]["opencode_tokens"] == 1100
    assert snapshot["a"]["planner_tokens"] == 400
    assert snapshot["a"]["combined_tokens"] == 1500
    assert snapshot["b"]["opencode_tokens"] == 900
    assert snapshot["b"]["planner_tokens"] == 100
    assert snapshot["b"]["combined_tokens"] == 1000
    assert snapshot["a"]["workers"] == snapshot["b"]["workers"] == 4
    assert snapshot["b"]["misses"] == 1 and snapshot["b"]["hits"] == 3
    assert snapshot["b"]["digest_saved"] == 500
    assert snapshot["b"]["digest_worker_sessions"] == 4
    assert snapshot["b"]["plugin_loaded"] == 1
    assert snapshot["b"]["task_compressed"] == 1
    assert snapshot["a"]["ready"] == snapshot["b"]["ready"] == 1


def test_failed_packet_still_counts_planner_usage_and_one_failure(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    round_id = "rrd-failed"
    arm = tmp_path / f"runs/rrd-demo/{round_id}/a"
    _jsonl(
        tmp_path / "runs/tokens.jsonl",
        [
            {
                "session": f"rrd-demo-{round_id}-a",
                "measurement_state": "exact",
                "input_tokens": 10,
                "output_tokens": 5,
            }
        ],
    )
    _jsonl(
        arm / "rrc-model-events.jsonl",
        [{"parse_status": "ok", "usage": {"total_tokens": 321}}],
    )
    _jsonl(
        arm / "rrc-events.jsonl",
        [
            {"event": "fail_open", "failure_id": "round-call-1", "source": "python"},
            {
                "event": "fail_open",
                "failure_id": "round-call-1",
                "source": "opencode_plugin",
            },
        ],
    )

    snapshot = collect(round_id, root=tmp_path)["a"]

    assert snapshot["planner_tokens"] == 321
    assert snapshot["combined_tokens"] == 336
    assert snapshot["fail_open"] == 1
    assert snapshot["ready"] == 0


def test_inexact_arm_traffic_makes_the_comparison_not_ready(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    round_id = "rrd-inexact"
    _jsonl(
        tmp_path / "runs/tokens.jsonl",
        [
            {
                "session": f"rrd-demo-{round_id}-a",
                "measurement_state": "estimated",
                "input_tokens": 10,
                "output_tokens": 5,
            }
        ],
    )

    snapshot = collect(round_id, root=tmp_path)["a"]

    assert snapshot["inexact_records"] == 1
    assert snapshot["ready"] == 0


def test_missing_contextmesh_evidence_makes_complete_rrc_arms_not_ready(tmp_path: Path) -> None:
    from contextmesh.scripts.rrd_combined_meter import collect

    round_id = "rrd-no-contextmesh"
    _jsonl(
        tmp_path / "runs/tokens.jsonl",
        [
            {
                "session": f"rrd-demo-{round_id}-{side}",
                "measurement_state": "exact",
                "input_tokens": 10,
                "output_tokens": 5,
            }
            for side in ("a", "b")
        ],
    )
    for side in ("a", "b"):
        arm = tmp_path / f"runs/rrd-demo/{round_id}/{side}"
        branches = ["miss"] * 4 if side == "a" else ["miss", "hit", "hit", "hit"]
        _jsonl(
            arm / "rrc-events.jsonl",
            [{"event": "packet", "branch": branch} for branch in branches],
        )
        _jsonl(arm / "rrc-model-events.jsonl", [])
        _db(arm / "opencode.db", workers=4)

    snapshot = collect(round_id, root=tmp_path)

    assert snapshot["a"]["ready"] == snapshot["b"]["ready"] == 0
    assert snapshot["a"]["plugin_loaded"] == snapshot["b"]["plugin_loaded"] == 0
    assert snapshot["a"]["digest_worker_sessions"] == 0


def test_combined_meter_labels_both_products_and_multiagent_state() -> None:
    from contextmesh.scripts.rrd_combined_meter import render

    side = {
        "opencode_tokens": 900,
        "planner_tokens": 100,
        "combined_tokens": 1000,
        "requests": 9,
        "workers": 4,
        "packets": 4,
        "misses": 1,
        "hits": 3,
        "fail_open": 0,
        "digest_hits": 12,
        "digest_saved": 39088,
        "task_compressed": 2,
        "plugin_loaded": 1,
        "digest_worker_sessions": 4,
        "inexact_records": 0,
        "ready": 1,
    }
    output = render("rrd-test", {"a": {**side, "misses": 4, "hits": 0}, "b": side})

    assert "ContextMesh + ReasonRenderCoding" in output
    assert "COLD + CM" in output and "WARM + CM" in output
    assert "workers" in output and "4/4" in output
    assert "RRC packets" in output and "1 MISS / 3 HIT" in output
    assert "ContextMesh saved" in output
    assert "READY" in output


def test_combined_meter_marks_incomplete_runs_not_ready() -> None:
    from contextmesh.scripts.rrd_combined_meter import render

    empty = {
        key: 0
        for key in (
            "opencode_tokens",
            "planner_tokens",
            "combined_tokens",
            "requests",
            "workers",
            "packets",
            "misses",
            "hits",
            "fail_open",
            "digest_hits",
            "digest_saved",
            "task_compressed",
            "inexact_records",
            "ready",
        )
    }
    output = render("rrd-empty", {"a": empty, "b": empty})
    assert "NOT READY" in output
    assert "INCOMPLETE" in output
