#!/usr/bin/env python3
"""Historical non-RRCv2 four-handler audit meter (fixture compatibility only)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

CM_ROOT = Path(__file__).resolve().parents[1]
HANDLERS = {"orders", "products", "reviews", "users"}
SHARED = {"src/models.js", "src/utils.js", "src/middleware.js"}
WORKER_COUNT = 4
MAX_META = 4096
MAX_MANIFEST = 3_000_000
MAX_JSONL = 50_000_000


def _sha(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _read_regular(path: Path, limit: int, *, mode: int | None = None) -> bytes | None:
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > limit
            or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
        ):
            return None
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            after = os.fstat(descriptor)
            if (
                not stat.S_ISREG(after.st_mode)
                or after.st_size > limit
                or (mode is not None and stat.S_IMODE(after.st_mode) != mode)
            ):
                return None
            chunks: list[bytes] = []
            total = 0
            while total <= limit:
                chunk = os.read(descriptor, min(64 * 1024, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        finally:
            os.close(descriptor)
    except OSError:
        return None
    raw = b"".join(chunks)
    return raw if len(raw) <= limit else None


def _object(path: Path, limit: int, *, mode: int | None = None) -> dict[str, object] | None:
    raw = _read_regular(path, limit, mode=mode)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _jsonl(path: Path) -> tuple[list[dict[str, object]], bool]:
    raw = _read_regular(path, MAX_JSONL)
    if raw is None:
        return [], False
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return [], False
    rows: list[dict[str, object]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return [], False
        if not isinstance(row, dict):
            return [], False
        rows.append(row)
    return rows, True


def _current_hash(root: Path, relative: str) -> str | None:
    parts = Path(relative).parts
    if not parts or relative.startswith("/") or ".." in parts:
        return None
    try:
        canonical = root.resolve(strict=True)
        candidate = canonical
        for part in parts:
            candidate /= part
            metadata = os.lstat(candidate)
            if stat.S_ISLNK(metadata.st_mode):
                return None
        raw = _read_regular(candidate, 1_000_000)
    except OSError:
        return None
    return _sha(raw) if raw is not None else None


def _source_authority(arm: Path, backend: str) -> tuple[dict[str, str], dict[str, str], bool]:
    manifest = _object(arm / "seed-manifest.json", MAX_MANIFEST, mode=0o600)
    target = arm / "target"
    if manifest is None:
        return {}, {}, False
    unsigned = {key: value for key, value in manifest.items() if key != "seal"}
    try:
        canonical = target.resolve(strict=True)
        metadata = canonical.stat()
    except OSError:
        return {}, {}, False
    if (
        manifest.get("v") != 1
        or manifest.get("memory_backend") != backend
        or manifest.get("target_root") != str(canonical)
        or manifest.get("target_device") != metadata.st_dev
        or manifest.get("target_inode") != metadata.st_ino
        or manifest.get("seal")
        != _sha(json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    ):
        return {}, {}, False
    handlers = {
        name: _current_hash(canonical, f"src/handlers/{name}.js") or "" for name in HANDLERS
    }
    if not all(re.fullmatch(r"[a-f0-9]{64}", value) for value in handlers.values()):
        return {}, {}, False
    shared: dict[str, str] = {}
    files = manifest.get("files")
    if not isinstance(files, list):
        return {}, {}, False
    for row in files:
        if not isinstance(row, dict):
            return {}, {}, False
        relative = row.get("path")
        raw_hash = row.get("raw_sha256")
        digest_hash = row.get("digest_sha256")
        if (
            not isinstance(relative, str)
            or relative not in SHARED
            or row.get("memory_backend") != backend
            or not isinstance(raw_hash, str)
            or not isinstance(digest_hash, str)
            or _current_hash(canonical, relative) != raw_hash
        ):
            return {}, {}, False
        if backend == "sqlite":
            digest = row.get("digest")
            if not isinstance(digest, str) or _sha(digest) != digest_hash:
                return {}, {}, False
        shared[relative] = digest_hash
    return handlers, shared, set(shared) == SHARED


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage(row: Mapping[str, object]) -> int | None:
    input_tokens = _count(row.get("input_tokens"))
    output_tokens = _count(row.get("output_tokens"))
    total_tokens = _count(row.get("total_tokens"))
    if input_tokens is None or output_tokens is None or total_tokens is None:
        return None
    if total_tokens != input_tokens + output_tokens:
        return None
    for name, maximum in (
        ("cached_input_tokens", input_tokens),
        ("cache_write_input_tokens", input_tokens),
        ("reasoning_output_tokens", output_tokens),
    ):
        child = _count(row.get(name))
        if child is None or child > maximum:
            return None
    return total_tokens


def _planner_usage(rows: Sequence[Mapping[str, object]]) -> tuple[int, int, bool]:
    success = [row for row in rows if row.get("parse_status") == "ok"]
    total = 0
    for row in success:
        usage = row.get("usage")
        if not isinstance(usage, dict):
            return 0, len(success), False
        prompt = _count(usage.get("prompt_tokens"))
        completion = _count(usage.get("completion_tokens"))
        combined = _count(usage.get("total_tokens"))
        if prompt is None or completion is None or combined != prompt + completion:
            return 0, len(success), False
        assert combined is not None
        total += combined
    failed = any(row.get("parse_status") != "ok" for row in rows)
    return total, len(success), not failed


def _arm(round_dir: Path, side: str, backend: str) -> dict[str, int]:
    arm = round_dir / side
    handler_hashes, shared_hashes, sources_ok = _source_authority(arm, backend)
    hooks, hooks_ok = _jsonl(arm / "hook-events.jsonl")
    packets, packets_ok = _jsonl(arm / "rrc-events.jsonl")
    model_rows, models_ok = _jsonl(arm / "rrc-model-events.jsonl")
    evidence_ok = hooks_ok and packets_ok and models_ok
    evidence_ok = evidence_ok and all(row.get("memory_backend") == backend for row in hooks)
    evidence_ok = evidence_ok and all(row.get("memory_backend") == backend for row in packets)

    assignments = [row for row in hooks if row.get("event") == "assignment"]
    assigned_handlers = {
        str(row.get("handler", "")).removeprefix("src/handlers/").removesuffix(".js")
        for row in assignments
    }
    assignment_tools = {
        row.get("tool_use_id")
        for row in assignments
        if isinstance(row.get("tool_use_id"), str)
        and row.get("handler_sha256")
        == handler_hashes.get(
            str(row.get("handler", "")).removeprefix("src/handlers/").removesuffix(".js")
        )
    }
    spawned_rows = [row for row in hooks if row.get("event") == "spawned"]
    spawned = {
        row.get("agent_id")
        for row in spawned_rows
        if isinstance(row.get("agent_id"), str) and row.get("tool_use_id") in assignment_tools
    }
    shared_rows = [row for row in hooks if row.get("event") == "shared_context"]
    shared_agents: set[str] = set()
    for row in shared_rows:
        receipts = row.get("receipts")
        if not isinstance(receipts, list):
            continue
        observed = {
            receipt.get("path"): receipt.get("digest_sha256")
            for receipt in receipts
            if isinstance(receipt, dict)
        }
        if observed == shared_hashes and isinstance(row.get("agent_id"), str):
            shared_agents.add(str(row["agent_id"]))
    finals = {
        row.get("agent_id")
        for row in hooks
        if row.get("event") == "result_final"
        and isinstance(row.get("agent_id"), str)
        and _count(row.get("delivered_chars")) is not None
        and isinstance(row.get("delivered_sha256"), str)
    }
    waits = [row for row in hooks if row.get("event") == "wait_result"]
    wait_completed: list[str] = []
    waits_valid = bool(waits)
    for row in waits:
        completed = row.get("completed_agent_ids")
        if (
            not isinstance(completed, list)
            or not all(isinstance(agent_id, str) for agent_id in completed)
            or row.get("result_count") != len(completed)
            or row.get("timed_out") is not False
        ):
            waits_valid = False
            continue
        wait_completed.extend(completed)
    wait_ok = (
        waits_valid
        and len(wait_completed) == WORKER_COUNT
        and len(set(wait_completed)) == WORKER_COUNT
        and set(wait_completed) == spawned
    )
    compressed_agents: set[str] = set()
    compression_rows = [row for row in hooks if row.get("event") == "compression_delivered"]
    for row in compression_rows:
        receipts = row.get("receipts")
        if not isinstance(receipts, dict):
            continue
        if all(
            isinstance(value, dict)
            and re.fullmatch(r"[a-f0-9]{20}", str(value.get("receipt", "")))
            and re.fullmatch(r"[a-f0-9]{64}", str(value.get("sha256", "")))
            and (_count(value.get("bytes")) or 0) > 0
            for value in receipts.values()
        ):
            compressed_agents.update(str(key) for key in receipts)
    bypass_rows = [row for row in hooks if row.get("event") == "compression_bypass"]
    bypassed_agents: set[str] = set()
    for row in bypass_rows:
        agents = row.get("agent_ids")
        receipts = row.get("receipts")
        if (
            isinstance(agents, list)
            and all(isinstance(agent_id, str) for agent_id in agents)
            and isinstance(receipts, dict)
            and set(agents) == set(receipts)
            and (_count(row.get("raw_bytes")) or 0) > 0
        ):
            bypassed_agents.update(agents)
    compression_ok = (compressed_agents | bypassed_agents) == spawned
    root_merge = sum(
        row.get("event") == "root_merge" and (_count(row.get("chars")) or 0) > 0 for row in hooks
    )

    usage_rows = [row for row in hooks if row.get("event") == "native_usage"]
    root_usage = [row for row in usage_rows if row.get("component") == "root"]
    worker_usage = [row for row in usage_rows if row.get("component") == "worker"]
    worker_usage_ids = {row.get("agent_id") for row in worker_usage}
    native_values = [_usage(row) for row in [*root_usage, *worker_usage]]
    usage_ok = (
        len(root_usage) == 1
        and len(worker_usage) == WORKER_COUNT
        and worker_usage_ids == spawned
        and len({row.get("session_id") for row in [*root_usage, *worker_usage]}) == WORKER_COUNT + 1
        and all(value is not None for value in native_values)
    )
    native_tokens = sum(value or 0 for value in native_values)

    packet_rows = [row for row in packets if row.get("event") == "packet"]
    misses = sum(row.get("branch") == "miss" for row in packet_rows)
    hits = sum(row.get("branch") == "hit" for row in packet_rows)
    expected = (WORKER_COUNT, 0) if side == "a" else (1, WORKER_COUNT - 1)
    packet_handlers = {
        str(row.get("handler", "")).removeprefix("src/handlers/").removesuffix(".js")
        for row in packet_rows
    }
    packets_valid = (
        len(packet_rows) == WORKER_COUNT
        and packet_handlers == HANDLERS
        and all(row.get("branch") in {"miss", "hit"} for row in packet_rows)
        and (misses, hits) == expected
    )
    planner_tokens, planner_calls, planner_ok = _planner_usage(model_rows)
    planner_ok = planner_ok and planner_calls == expected[0]
    failures = sum(
        row.get("event")
        in {"fail_open", "policy_deny", "compress_fail_open", "native_usage_missing"}
        for row in hooks
    )
    protocol_ok = (
        len(assignments) == WORKER_COUNT
        and assigned_handlers == HANDLERS
        and len(assignment_tools) == WORKER_COUNT
        and len(spawned) == WORKER_COUNT
        and shared_agents == spawned
        and finals == spawned
        and wait_ok
        and compression_ok
        and root_merge == 1
        and packets_valid
    )
    ready = int(
        sources_ok and evidence_ok and protocol_ok and usage_ok and planner_ok and failures == 0
    )
    return {
        "ready": ready,
        "root_tokens": _usage(root_usage[0]) or 0 if len(root_usage) == 1 else 0,
        "worker_tokens": sum(_usage(row) or 0 for row in worker_usage),
        "planner_tokens": planner_tokens,
        "combined_tokens": native_tokens + planner_tokens,
        "workers": len(spawned),
        "assignments": len(assignment_tools),
        "shared_contexts": len(shared_agents),
        "results": len(finals),
        "compressions": len(compression_rows),
        "misses": misses,
        "hits": hits,
        "planner_calls": planner_calls,
        "source_state_ok": int(sources_ok),
        "evidence_ok": int(evidence_ok),
        "protocol_ok": int(protocol_ok),
        "usage_ok": int(usage_ok),
        "planner_ok": int(planner_ok),
        "failures": failures,
    }


def collect(
    round_id: str, *, memory_backend: str | None = None, root: Path = CM_ROOT
) -> dict[str, dict[str, int]]:
    round_dir = root / "runs" / "rrd-demo" / round_id
    meta = _object(round_dir / "round-meta.json", MAX_META, mode=0o600)
    backend = memory_backend or (str(meta.get("memory_backend")) if meta else "")
    meta_ok = bool(
        meta
        and meta.get("v") == 2
        and meta.get("round_id") == round_id
        and meta.get("memory_backend") == backend
        and meta.get("provider") == "native-codex"
        and isinstance(meta.get("model"), str)
        and round_id.startswith(f"rrd-{backend}-")
    )
    snapshot = {side: _arm(round_dir, side, backend) for side in ("a", "b")}
    if not meta_ok:
        for side in snapshot.values():
            side["ready"] = 0
            side["evidence_ok"] = 0
    return snapshot


def render(
    round_id: str, snapshot: Mapping[str, Mapping[str, int]], memory_backend: str | None = None
) -> str:
    a, b = snapshot.get("a", {}), snapshot.get("b", {})
    ready = bool(a.get("ready") and b.get("ready"))
    delta = int(a.get("combined_tokens", 0)) - int(b.get("combined_tokens", 0))
    percent = 100 * delta / int(a.get("combined_tokens", 0)) if a.get("combined_tokens") else 0
    state = "READY" if ready else "NOT READY"
    lines = [
        f"ContextMesh + ReasonRenderCoding native Codex meter — {round_id}",
        f"state: {state} · memory={memory_backend or 'unknown'} · observational usage only",
        "",
        f"{'':24} {'COLD (a)':>14} {'WARM (b)':>14}",
        f"{'root tokens':24} {int(a.get('root_tokens', 0)):>14,} {int(b.get('root_tokens', 0)):>14,}",
        f"{'worker tokens':24} {int(a.get('worker_tokens', 0)):>14,} {int(b.get('worker_tokens', 0)):>14,}",
        f"{'RRC planner tokens':24} {int(a.get('planner_tokens', 0)):>14,} {int(b.get('planner_tokens', 0)):>14,}",
        f"{'TOTAL provider-visible':24} {int(a.get('combined_tokens', 0)):>14,} {int(b.get('combined_tokens', 0)):>14,}",
        f"{'workers':24} {int(a.get('workers', 0)):>13}/4 {int(b.get('workers', 0)):>13}/4",
        f"{'RRC MISS / HIT':24} {int(a.get('misses', 0)):>6} / {int(a.get('hits', 0)):<6} {int(b.get('misses', 0)):>6} / {int(b.get('hits', 0)):<6}",
        f"{'compression deliveries':24} {int(a.get('compressions', 0)):>14} {int(b.get('compressions', 0)):>14}",
        "",
        f"observed WARM delta: {delta:,} tokens ({percent:.1f}% vs COLD)",
        "billing_exact=false · hidden_retry_observable=false · cached input is not subtracted",
    ]
    if not ready:
        for label, side in (("a", a), ("b", b)):
            bad = [
                name.removesuffix("_ok")
                for name in (
                    "source_state_ok",
                    "evidence_ok",
                    "protocol_ok",
                    "usage_ok",
                    "planner_ok",
                )
                if not side.get(name)
            ]
            if side.get("failures"):
                bad.append(f"{side['failures']} fail-open/policy events")
            lines.append(f"{label}: " + (", ".join(bad) or "incomplete"))
    width = max(len(line) for line in lines) + 2
    return "\n".join(
        ["┌" + "─" * width + "┐"]
        + ["│ " + line.ljust(width - 1) + "│" for line in lines]
        + ["└" + "─" * width + "┘"]
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", required=True)
    parser.add_argument("--memory-backend", choices=("everos", "sqlite"), required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    while True:
        snapshot = collect(args.round, memory_backend=args.memory_backend)
        print(
            ("\033[2J\033[H" if args.watch else "")
            + render(args.round, snapshot, args.memory_backend),
            flush=True,
        )
        if not args.watch or args.once:
            return 0
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
