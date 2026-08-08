#!/usr/bin/env python3
"""Evidence-correlated meter for the Codex ContextMesh + RRC paired demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

CM_ROOT = Path(__file__).resolve().parent.parent
WIDTH = 104
HANDLERS = {
    "src/handlers/users.js",
    "src/handlers/products.js",
    "src/handlers/orders.js",
    "src/handlers/reviews.js",
}
SHARED = {"src/models.js", "src/utils.js", "src/middleware.js"}
WORKER_COUNT = 4


def _sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


def _current_file_hash(root: Path, relative: str) -> str | None:
    try:
        canonical_root = root.resolve(strict=True)
        candidate = canonical_root
        parts = Path(relative).parts
        if not parts:
            return None
        for part in parts:
            candidate /= part
            metadata = os.lstat(candidate)
            if stat.S_ISLNK(metadata.st_mode):
                return None
        if not stat.S_ISREG(metadata.st_mode):
            return None
        candidate = candidate.resolve(strict=True)
        candidate.relative_to(canonical_root)
        descriptor = os.open(
            candidate,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return None
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return None
    return digest.hexdigest()


def _source_authority(
    arm: Path,
) -> tuple[dict[str, str], dict[str, tuple[str, str]], bool]:
    target = arm / "target"
    manifest_path = arm / "seed-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
        canonical_target = target.resolve(strict=True)
        unsigned = {key: item for key, item in manifest.items() if key != "seal"}
        seal = _sha(json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        target_stat = canonical_target.stat()
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        return {}, {}, False
    if (
        not isinstance(manifest, dict)
        or manifest.get("v") != 1
        or manifest.get("seal") != seal
        or manifest.get("target_root") != str(canonical_target)
        or manifest.get("target_device") != target_stat.st_dev
        or manifest.get("target_inode") != target_stat.st_ino
    ):
        return {}, {}, False
    rows = manifest.get("files")
    if not isinstance(rows, list) or len(rows) != len(SHARED):
        return {}, {}, False
    shared_hashes: dict[str, tuple[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            return {}, {}, False
        path = row.get("path")
        raw_hash = row.get("raw_sha256")
        digest_hash = row.get("digest_sha256")
        if (
            not isinstance(path, str)
            or path not in SHARED
            or path in shared_hashes
            or not isinstance(raw_hash, str)
            or len(raw_hash) != 64
            or not isinstance(digest_hash, str)
            or len(digest_hash) != 64
            or _current_file_hash(canonical_target, path) != raw_hash
        ):
            return {}, {}, False
        shared_hashes[path] = (raw_hash, digest_hash)
    if set(shared_hashes) != SHARED:
        return {}, {}, False
    handler_hashes = {
        path: value
        for path in HANDLERS
        if (value := _current_file_hash(canonical_target, path)) is not None
    }
    return handler_hashes, shared_hashes, set(handler_hashes) == HANDLERS


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
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _token_total(rows: Sequence[Mapping[str, object]]) -> int:
    return sum(
        _integer(row.get("input_tokens")) + _integer(row.get("output_tokens")) for row in rows
    )


def _digest_saved(manifest_path: Path) -> int:
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    total = 0
    for row in manifest.get("files", []) if isinstance(manifest, dict) else []:
        if not isinstance(row, dict):
            continue
        raw = _integer(row.get("raw_chars"))
        digest = _integer(row.get("digest_chars"))
        avoided_chars = max(0, raw - digest)
        estimated_tokens_per_worker = (avoided_chars + 3) // 4
        total += estimated_tokens_per_worker * WORKER_COUNT
    return total


def collect(round_id: str, *, root: Path = CM_ROOT) -> dict[str, dict[str, int]]:
    """Correlate tokens, assignments, agents, shared receipts, results, and merge."""

    token_rows = _jsonl(root / "runs/rrd-tokens.jsonl")
    snapshot: dict[str, dict[str, int]] = {}
    setup_prefix = f"rrd-demo-{round_id}-setup-"
    setup_rows = [
        row
        for row in token_rows
        if isinstance(row.get("session"), str) and str(row["session"]).startswith(setup_prefix)
    ]
    setup_exact = [row for row in setup_rows if row.get("measurement_state") == "exact"]
    setup_inexact = len(setup_rows) - len(setup_exact)
    for side in ("a", "b"):
        base = f"rrd-demo-{round_id}-{side}"
        sessions = {
            "outer": f"{base}-outer",
            "summarizer": f"{base}-summarizer",
            "planner": f"{base}-planner",
        }
        exact_by_kind: dict[str, list[dict[str, object]]] = {}
        for kind, session in sessions.items():
            exact_by_kind[kind] = [
                row
                for row in token_rows
                if row.get("session") == session and row.get("measurement_state") == "exact"
            ]
        arm_prefixed = [
            row
            for row in token_rows
            if isinstance(row.get("session"), str)
            and str(row["session"]).startswith(base)
            and not str(row["session"]).startswith(setup_prefix)
        ]
        allowed_sessions = set(sessions.values())
        inexact = [
            row
            for row in arm_prefixed
            if row.get("measurement_state") != "exact" or row.get("session") not in allowed_sessions
        ]

        arm = root / "runs/rrd-demo" / round_id / side
        handler_hashes, shared_hashes, source_state_ok = _source_authority(arm)
        hooks = _jsonl(arm / "hook-events.jsonl")
        proxy = _jsonl(arm / "proxy-events.jsonl")
        rrc = _jsonl(arm / "rrc-events.jsonl")
        assignments = [row for row in hooks if row.get("event") == "assignment"]
        assignment_ids = {
            row.get("assignment_id")
            for row in assignments
            if isinstance(row.get("assignment_id"), str)
        }
        handlers = {
            row.get("handler") for row in assignments if isinstance(row.get("handler"), str)
        }
        tool_ids = {
            row.get("tool_use_id") for row in assignments if isinstance(row.get("tool_use_id"), str)
        }
        spawned = [row for row in hooks if row.get("event") == "spawned"]
        agents = {row.get("agent_id") for row in spawned if isinstance(row.get("agent_id"), str)}
        spawned_tool_ids = {
            row.get("tool_use_id")
            for row in spawned
            if isinstance(row.get("tool_use_id"), str) and isinstance(row.get("agent_id"), str)
        }
        starts = [row for row in hooks if row.get("event") == "shared_context"]
        start_agents = {
            row.get("agent_id") for row in starts if isinstance(row.get("agent_id"), str)
        }
        receipt_agents: set[str] = set()
        receipts_ok = True
        for row in starts:
            receipts = row.get("receipts")
            paths: set[object] = set()
            if isinstance(receipts, list) and len(receipts) == len(SHARED):
                for receipt in receipts:
                    if not isinstance(receipt, dict):
                        continue
                    path = receipt.get("path")
                    expected_hashes = shared_hashes.get(path) if isinstance(path, str) else None
                    if (
                        expected_hashes is not None
                        and receipt.get("hit") is True
                        and receipt.get("raw_sha256") == expected_hashes[0]
                        and receipt.get("digest_sha256") == expected_hashes[1]
                    ):
                        paths.add(path)
            agent_id = row.get("agent_id")
            if paths == SHARED and isinstance(agent_id, str):
                receipt_agents.add(agent_id)
            else:
                receipts_ok = False
        finals = [row for row in hooks if row.get("event") == "result_final"]
        final_evidence = {
            str(row["agent_id"]): (
                _integer(row.get("delivered_chars")),
                str(row["delivered_sha256"]),
            )
            for row in finals
            if isinstance(row.get("agent_id"), str)
            and _integer(row.get("delivered_chars")) > 0
            and isinstance(row.get("delivered_sha256"), str)
            and re.fullmatch(r"[a-f0-9]{64}", str(row["delivered_sha256"])) is not None
        }
        final_agents = set(final_evidence)
        wait_agents: set[str] = set()
        wait_evidence: dict[str, tuple[int, str]] = {}
        wait_rows = [row for row in hooks if row.get("event") == "wait_result"]
        waits_ok = bool(wait_rows)
        for row in wait_rows:
            agent_ids = row.get("agent_ids")
            completed_ids = row.get("completed_agent_ids")
            valid_agents = (
                {item for item in agent_ids if isinstance(item, str)}
                if isinstance(agent_ids, list)
                else set()
            )
            valid_completed = (
                {item for item in completed_ids if isinstance(item, str)}
                if isinstance(completed_ids, list)
                else set()
            )
            completed_results = row.get("completed_results")
            valid_results: dict[str, tuple[int, str]] = {}
            if isinstance(completed_results, dict):
                for agent_id, result in completed_results.items():
                    if (
                        isinstance(agent_id, str)
                        and isinstance(result, dict)
                        and _integer(result.get("chars")) > 0
                        and isinstance(result.get("sha256"), str)
                        and re.fullmatch(r"[a-f0-9]{64}", str(result["sha256"])) is not None
                    ):
                        valid_results[agent_id] = (
                            _integer(result["chars"]),
                            str(result["sha256"]),
                        )
            row_ok = (
                row.get("timed_out") is False
                and bool(valid_agents)
                and valid_agents == valid_completed
                and valid_agents == set(valid_results)
                and _integer(row.get("result_count")) == len(valid_results)
                and not any(
                    agent_id in wait_evidence and wait_evidence[agent_id] != evidence
                    for agent_id, evidence in valid_results.items()
                )
            )
            waits_ok = waits_ok and row_ok
            if row_ok:
                wait_agents.update(valid_completed)
                wait_evidence.update(valid_results)
        start_times: dict[str, float] = {}
        for row in starts:
            agent_id, timestamp = row.get("agent_id"), row.get("ts")
            if isinstance(agent_id, str) and isinstance(timestamp, (int, float)):
                start_times[agent_id] = float(timestamp)
        stop_times: dict[str, float] = {}
        for row in finals:
            agent_id, timestamp = row.get("agent_id"), row.get("ts")
            if isinstance(agent_id, str) and isinstance(timestamp, (int, float)):
                stop_times[agent_id] = float(timestamp)
        overlap = int(
            len(start_times) == 4
            and len(stop_times) == 4
            and max(start_times.values()) < min(stop_times.values())
        )
        packets = [row for row in rrc if row.get("event") == "packet"]
        misses = sum(row.get("branch") == "miss" for row in packets)
        hits = sum(row.get("branch") == "hit" for row in packets)
        control_rows = [row for row in hooks if row.get("branch") == "control"]
        controls = len(control_rows)
        packet_rows = packets if packets else control_rows
        packet_ids = {
            row.get("task_id", row.get("assignment_id"))
            for row in packet_rows
            if isinstance(row.get("task_id", row.get("assignment_id")), str)
        }
        packet_handlers = {
            row.get("handler") for row in packet_rows if isinstance(row.get("handler"), str)
        }
        fail_open = sum(
            row.get("event") in {"fail_open", "policy_deny", "compress_fail_open"}
            for row in [*hooks, *rrc, *proxy]
        )
        proxy_receipts = {
            row.get("receipt")
            for row in proxy
            if row.get("event") == "result_compress"
            and isinstance(row.get("receipt"), str)
            and re.fullmatch(r"[a-f0-9]{20}", str(row["receipt"])) is not None
        }
        final_receipts = {
            row.get("compression_receipt")
            for row in finals
            if isinstance(row.get("compression_receipt"), str)
            and re.fullmatch(r"[a-f0-9]{20}", str(row["compression_receipt"])) is not None
        }
        compressions = len(proxy_receipts & final_receipts)
        root_merges = sum(
            row.get("event") == "root_merge"
            and _integer(row.get("chars")) > 0
            and isinstance(row.get("sha256"), str)
            and re.fullmatch(r"[a-f0-9]{64}", str(row["sha256"])) is not None
            for row in hooks
        )
        expected = (4, 0) if side == "a" else (1, 3)
        rrc_ok = (misses, hits) == expected or controls == 4
        correlated = (
            len(assignments) == 4
            and len(assignment_ids) == 4
            and len(tool_ids) == 4
            and all(
                isinstance(row.get("handler_sha256"), str)
                and isinstance(row.get("handler"), str)
                and handler_hashes.get(str(row["handler"])) == row["handler_sha256"]
                for row in assignments
            )
            and handlers == HANDLERS
            and source_state_ok
            and len(spawned) == 4
            and len(agents) == 4
            and spawned_tool_ids == tool_ids
            and len(starts) == 4
            and len(finals) == 4
            and agents == start_agents == receipt_agents == final_agents
            and wait_agents == agents
            and wait_evidence == final_evidence
            and waits_ok
            and receipts_ok
            and packet_ids == assignment_ids
            and packet_handlers == HANDLERS
        )
        outer = _token_total(exact_by_kind["outer"])
        summarizer = _token_total(exact_by_kind["summarizer"])
        planner = _token_total(exact_by_kind["planner"])
        ready = int(
            bool(exact_by_kind["outer"])
            and bool(exact_by_kind["summarizer"])
            and (bool(exact_by_kind["planner"]) or controls == 4)
            and setup_inexact == 0
            and not inexact
            and correlated
            and overlap == 1
            and rrc_ok
            and fail_open == 0
            and root_merges == 1
            and compressions >= 1
        )
        snapshot[side] = {
            "outer_tokens": outer,
            "summarizer_tokens": summarizer,
            "planner_tokens": planner,
            "combined_tokens": outer + summarizer + planner,
            "setup_tokens": _token_total(setup_exact),
            "setup_inexact_records": setup_inexact,
            "requests": sum(len(rows) for rows in exact_by_kind.values()),
            "outer_records": len(exact_by_kind["outer"]),
            "summarizer_records": len(exact_by_kind["summarizer"]),
            "planner_records": len(exact_by_kind["planner"]),
            "workers": len(agents),
            "assignments": len(assignment_ids),
            "handler_set_ok": int(handlers == HANDLERS),
            "source_state_ok": int(source_state_ok),
            "packets": len(packets) if packets else controls,
            "misses": misses,
            "hits": hits,
            "controls": controls,
            "fail_open": fail_open,
            "digest_worker_sessions": len(receipt_agents),
            "digest_saved": _digest_saved(arm / "seed-manifest.json"),
            "task_compressed": compressions,
            "results": len(final_agents & wait_agents),
            "root_merges": root_merges,
            "overlap": overlap,
            "inexact_records": len(inexact),
            "ready": ready,
        }
    return snapshot


def _box(line: str = "") -> str:
    return "│ " + line[: WIDTH - 4].ljust(WIDTH - 4) + " │"


def _value(side: Mapping[str, int], key: str) -> int:
    return int(side.get(key, 0))


def render(round_id: str, snapshot: Mapping[str, Mapping[str, int]]) -> str:
    cold, warm = snapshot.get("a", {}), snapshot.get("b", {})
    ready = bool(_value(cold, "ready") and _value(warm, "ready"))
    delta = _value(cold, "combined_tokens") - _value(warm, "combined_tokens")
    title = f" Codex ContextMesh + ReasonRenderCoding paired meter — {round_id} "
    rows = (
        ("", "COLD + CM (a)", "WARM + CM (b)"),
        (
            "Codex outer (root+workers)",
            f"{_value(cold, 'outer_tokens'):,}",
            f"{_value(warm, 'outer_tokens'):,}",
        ),
        (
            "ContextMesh summarizer",
            f"{_value(cold, 'summarizer_tokens'):,}",
            f"{_value(warm, 'summarizer_tokens'):,}",
        ),
        (
            "RRC planner",
            f"{_value(cold, 'planner_tokens'):,}",
            f"{_value(warm, 'planner_tokens'):,}",
        ),
        (
            "STEADY TOTAL",
            f"{_value(cold, 'combined_tokens'):,}",
            f"{_value(warm, 'combined_tokens'):,}",
        ),
        (
            "workers / assignments / results",
            f"{_value(cold, 'workers')}/4 · {_value(cold, 'assignments')}/4 · {_value(cold, 'results')}/4",
            f"{_value(warm, 'workers')}/4 · {_value(warm, 'assignments')}/4 · {_value(warm, 'results')}/4",
        ),
        (
            "RRC packets",
            f"{_value(cold, 'misses')} MISS / {_value(cold, 'hits')} HIT",
            f"{_value(warm, 'misses')} MISS / {_value(warm, 'hits')} HIT",
        ),
        (
            "CM digest delivery",
            f"{_value(cold, 'digest_worker_sessions')}/4",
            f"{_value(warm, 'digest_worker_sessions')}/4",
        ),
        (
            "compressed results / overlap / merge",
            f"{_value(cold, 'task_compressed')} · {_value(cold, 'overlap')} · {_value(cold, 'root_merges')}",
            f"{_value(warm, 'task_compressed')} · {_value(warm, 'overlap')} · {_value(warm, 'root_merges')}",
        ),
    )
    lines = ["┌" + title.center(WIDTH - 2, "─") + "┐"]
    if ready:
        lines.append(
            _box(
                f"▶ READY · PAIRED SAMPLE · WARM steady-state delta: {delta:,} tokens (not a causal estimate)"
            )
        )
    else:
        lines.append(_box("▶ NOT READY · INCOMPLETE — no comparison claim"))
    lines.append("├" + "─" * (WIDTH - 2) + "┤")
    for label, left, right in rows:
        lines.append(_box(f"{label:<34}{left:>30}{right:>34}"))
    lines.append(_box())
    lines.append(
        _box(f"Setup/seed tokens (excluded from steady totals): {_value(cold, 'setup_tokens'):,}")
    )
    lines.append(
        _box(
            "ContextMesh digest savings are counterfactual estimates, never subtracted from actual totals."
        )
    )
    lines.append(
        _box(
            "Estimated digest tokens avoided across 4 workers: "
            f"a={_value(cold, 'digest_saved'):,} · b={_value(warm, 'digest_saved'):,}"
        )
    )
    lines.append(
        _box(
            f"Fail-open/policy events: a={_value(cold, 'fail_open')} · b={_value(warm, 'fail_open')}"
        )
    )
    if not ready:
        lines.append(_box(f"Readiness: a={_reason(cold, 'a')} · b={_reason(warm, 'b')}"))
    lines.append("└" + "─" * (WIDTH - 2) + "┘")
    return "\n".join(lines)


def _reason(side: Mapping[str, int], label: str) -> str:
    reasons: list[str] = []
    if _value(side, "requests") == 0:
        reasons.append("no exact traffic")
    elif not _value(side, "outer_records"):
        reasons.append("no outer traffic")
    if not _value(side, "summarizer_records"):
        reasons.append("no summarizer traffic")
    if not _value(side, "planner_records") and _value(side, "controls") != 4:
        reasons.append("no planner traffic")
    if _value(side, "inexact_records"):
        reasons.append("inexact/foreign traffic")
    if _value(side, "setup_inexact_records"):
        reasons.append("inexact setup traffic")
    if _value(side, "workers") != 4:
        reasons.append(f"workers {_value(side, 'workers')}/4")
    if _value(side, "assignments") != 4 or not _value(side, "handler_set_ok"):
        reasons.append("assignments/handlers")
    if not _value(side, "source_state_ok"):
        reasons.append("current source/manifest hashes")
    if _value(side, "digest_worker_sessions") != 4:
        reasons.append("digest receipts")
    if _value(side, "results") != 4:
        reasons.append("results")
    expected = (4, 0) if label == "a" else (1, 3)
    if (_value(side, "misses"), _value(side, "hits")) != expected and _value(side, "controls") != 4:
        reasons.append("RRC branches")
    if not _value(side, "overlap"):
        reasons.append("no four-worker overlap")
    if _value(side, "task_compressed") < 1:
        reasons.append("no result compression")
    if _value(side, "root_merges") != 1:
        reasons.append("root merge")
    if _value(side, "fail_open"):
        reasons.append(f"fail-open {_value(side, 'fail_open')}")
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
        print(("\033[2J\033[H" if args.watch else "") + output, flush=True)
        if not args.watch or args.once:
            return 0
        try:
            time.sleep(2)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
