#!/usr/bin/env python3
"""Live meter for the combined ContextMesh + ReasonRenderCoding worker demo."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

CM_ROOT = Path(__file__).resolve().parent.parent
WIDTH = 96


def _jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _worker_ids(database: Path) -> set[str]:
    if not database.exists():
        return set()
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=0.2)
        try:
            rows = connection.execute(
                "SELECT id FROM session WHERE parent_id IS NOT NULL"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return set()
    return {row[0] for row in rows if isinstance(row[0], str) and row[0]}


def collect(round_id: str, *, root: Path = CM_ROOT) -> dict[str, dict[str, int]]:
    """Collect disjoint actual-token and counterfactual-saving authorities."""

    proxy_rows = _jsonl(root / "runs/tokens.jsonl")
    snapshot: dict[str, dict[str, int]] = {}
    for side in ("a", "b"):
        sessions = {
            f"rrd-demo-{round_id}-{side}",
            f"rrd-demo-{round_id}-{side}-summarizer",
        }
        exact = [
            row
            for row in proxy_rows
            if row.get("session") in sessions and row.get("measurement_state") == "exact"
        ]
        inexact = [
            row
            for row in proxy_rows
            if row.get("session") in sessions and row.get("measurement_state") != "exact"
        ]
        opencode_tokens = sum(
            _integer(row.get("input_tokens")) + _integer(row.get("output_tokens")) for row in exact
        )

        arm = root / "runs/rrd-demo" / round_id / side
        rrc_rows = _jsonl(arm / "rrc-events.jsonl")
        model_rows = _jsonl(arm / "rrc-model-events.jsonl")
        packets = [row for row in rrc_rows if row.get("event") == "packet"]
        metrics = _jsonl(arm / "contextmesh.jsonl")
        planner_tokens = sum(
            _integer(usage.get("total_tokens"))
            for row in model_rows
            if isinstance((usage := row.get("usage")), Mapping)
        )
        digest_hits = [row for row in metrics if row.get("event") == "digest_hit"]
        plugin_loaded = sum(1 for row in metrics if row.get("event") == "plugin_loaded")
        failure_keys: set[str] = set()
        anonymous_failures = 0
        for row in rrc_rows:
            if row.get("event") != "fail_open":
                continue
            failure_id = row.get("failure_id")
            if isinstance(failure_id, str) and failure_id:
                failure_keys.add(failure_id)
            else:
                anonymous_failures += 1
        misses = sum(1 for row in packets if row.get("branch") == "miss")
        hits = sum(1 for row in packets if row.get("branch") == "hit")
        fail_open = len(failure_keys) + anonymous_failures
        worker_ids = _worker_ids(arm / "opencode.db")
        workers = len(worker_ids)
        digest_worker_sessions = len(
            {
                session_id
                for row in digest_hits
                if isinstance((session_id := row.get("sessionID")), str)
                and session_id in worker_ids
            }
        )
        expected_misses, expected_hits = (4, 0) if side == "a" else (1, 3)
        ready = int(
            len(exact) > 0
            and not inexact
            and workers == 4
            and len(packets) == 4
            and misses == expected_misses
            and hits == expected_hits
            and fail_open == 0
            and plugin_loaded > 0
            and digest_worker_sessions == 4
        )
        snapshot[side] = {
            "opencode_tokens": opencode_tokens,
            "planner_tokens": planner_tokens,
            "combined_tokens": opencode_tokens + planner_tokens,
            "requests": len(exact),
            "workers": workers,
            "packets": len(packets),
            "misses": misses,
            "hits": hits,
            "fail_open": fail_open,
            "digest_hits": len(digest_hits),
            "digest_worker_sessions": digest_worker_sessions,
            "digest_saved": sum(_integer(row.get("savedTokens")) for row in digest_hits),
            "task_compressed": sum(1 for row in metrics if row.get("event") == "task_compressed"),
            "plugin_loaded": plugin_loaded,
            "inexact_records": len(inexact),
            "ready": ready,
        }
    return snapshot


def _box(line: str = "") -> str:
    return "│ " + line[: WIDTH - 4].ljust(WIDTH - 4) + " │"


def _value(side: Mapping[str, int], key: str) -> int:
    return int(side.get(key, 0))


def render(round_id: str, snapshot: Mapping[str, Mapping[str, int]]) -> str:
    """Render one combined, non-double-counted COLD-vs-WARM snapshot."""

    cold = snapshot.get("a", {})
    warm = snapshot.get("b", {})
    cold_total = _value(cold, "combined_tokens")
    warm_total = _value(warm, "combined_tokens")
    delta = cold_total - warm_total
    percent = (100.0 * delta / cold_total) if cold_total else 0.0
    ready = bool(_value(cold, "ready") and _value(warm, "ready"))
    title = f" ContextMesh + ReasonRenderCoding multi-agent meter — {round_id} "
    rows = (
        ("", "COLD + CM (a)", "WARM + CM (b)"),
        (
            "OpenCode + CM summary",
            f"{_value(cold, 'opencode_tokens'):,}",
            f"{_value(warm, 'opencode_tokens'):,}",
        ),
        (
            "RRC planner tokens",
            f"{_value(cold, 'planner_tokens'):,}",
            f"{_value(warm, 'planner_tokens'):,}",
        ),
        ("TOTAL consumed", f"{cold_total:,}", f"{warm_total:,}"),
        (
            "workers",
            f"{_value(cold, 'workers')}/4",
            f"{_value(warm, 'workers')}/4",
        ),
        (
            "RRC packets",
            f"{_value(cold, 'misses')} MISS / {_value(cold, 'hits')} HIT",
            f"{_value(warm, 'misses')} MISS / {_value(warm, 'hits')} HIT",
        ),
        (
            "CM worker evidence",
            f"{_value(cold, 'digest_worker_sessions')}/4",
            f"{_value(warm, 'digest_worker_sessions')}/4",
        ),
    )
    lines = ["┌" + title.center(WIDTH - 2, "─") + "┐"]
    if ready:
        lines.append(
            _box(
                f"▶ READY · RRC WARM DELTA: {delta:,} consumed tokens ({percent:.1f}% vs COLD; "
                "Tollgate + direct Codex usage)"
            )
        )
    else:
        lines.append(_box("▶ NOT READY · INCOMPLETE — token comparison is not ready"))
    lines.append("├" + "─" * (WIDTH - 2) + "┤")
    for label, cold_value, warm_value in rows:
        lines.append(_box(f"{label:<21}{cold_value:>27}{warm_value:>31}"))
    lines.append(_box())
    lines.append(
        _box(
            "ContextMesh saved (counterfactual, not in totals): "
            f"a={_value(cold, 'digest_saved'):,} / b={_value(warm, 'digest_saved'):,} tokens; "
            f"digest hits a={_value(cold, 'digest_hits')} b={_value(warm, 'digest_hits')}"
        )
    )
    lines.append(
        _box(
            f"Task compression: a={_value(cold, 'task_compressed')} b={_value(warm, 'task_compressed')} · "
            f"RRC fail-open: a={_value(cold, 'fail_open')} b={_value(warm, 'fail_open')}"
        )
    )
    if not ready:
        lines.append(
            _box(f"Readiness: a={_readiness_reason(cold, 'a')} · b={_readiness_reason(warm, 'b')}")
        )
    lines.append("└" + "─" * (WIDTH - 2) + "┘")
    return "\n".join(lines)


def _readiness_reason(side: Mapping[str, int], label: str) -> str:
    if _value(side, "ready"):
        return "READY"
    expected = (4, 0) if label == "a" else (1, 3)
    reasons: list[str] = []
    if _value(side, "requests") == 0:
        reasons.append("no exact traffic")
    if _value(side, "inexact_records"):
        reasons.append(f"{_value(side, 'inexact_records')} inexact")
    if _value(side, "workers") != 4:
        reasons.append(f"workers {_value(side, 'workers')}/4")
    if _value(side, "packets") != 4:
        reasons.append(f"packets {_value(side, 'packets')}/4")
    if (_value(side, "misses"), _value(side, "hits")) != expected:
        reasons.append(f"branch {_value(side, 'misses')}M/{_value(side, 'hits')}H")
    if _value(side, "fail_open"):
        reasons.append(f"fail-open {_value(side, 'fail_open')}")
    if _value(side, "plugin_loaded") == 0:
        reasons.append("CM plugin not loaded")
    if _value(side, "digest_worker_sessions") != 4:
        reasons.append(f"CM workers {_value(side, 'digest_worker_sessions')}/4")
    return ", ".join(reasons) or "incomplete"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    while True:
        output = render(args.round, collect(args.round))
        prefix = "\033[2J\033[H" if args.watch else ""
        print(prefix + output, flush=True)
        if not args.watch or args.once:
            return 0
        try:
            time.sleep(2)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
