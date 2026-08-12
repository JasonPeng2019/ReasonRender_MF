#!/usr/bin/env python3
"""Fail-closed functional meter for the canonical ContextMesh + RRCv2 demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, cast

from rrc.attempts import AttemptRepository
from rrc.cell_journal import SQLiteCellJournal
from rrc.contextmesh import parse_receipt_record
from rrc.contract import CostEventV1
from rrc.journal import SQLiteRRCRepository, parse_accepted_commit

CM_ROOT = Path(__file__).resolve().parents[1]
WORKER_COUNT = 1
MAX_META = 4096
MAX_JSONL = 50_000_000
MAX_SOURCE = 2 * 1024 * 1024
MAX_DATABASE = 512 * 1024 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_COST_FIELDS = {
    "arm",
    "attempt_id",
    "cached_input_tokens",
    "cell_id",
    "cost_event_id",
    "effective_model",
    "effective_provider",
    "effective_reasoning",
    "effective_service_tier",
    "final_message_sha256",
    "identity_attestation",
    "input_tokens",
    "output_tokens",
    "prompt_sha256",
    "provider_total_tokens",
    "reasoning_output_tokens",
    "requested_model",
    "requested_provider",
    "requested_reasoning",
    "requested_service_tier",
    "stage",
    "stage_ordinal",
    "task_id",
    "transcript_sha256",
    "v",
}
_FAILURE_EVENTS = {
    "fail_open",
    "native_usage_missing",
    "policy_deny",
    "rrcv2_callback_rejected",
    "rrcv2_root_accounting_failed",
    "rrcv2_wait_operational_fallback",
    "source_reread_violation",
}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_regular(path: Path, limit: int, *, mode: int | None = None) -> bytes | None:
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_size > limit
            or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
        ):
            return None
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            after = os.fstat(descriptor)
            if (
                not stat.S_ISREG(after.st_mode)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or after.st_size > limit
                or (mode is not None and stat.S_IMODE(after.st_mode) != mode)
            ):
                return None
            raw = bytearray()
            while len(raw) <= limit:
                block = os.read(descriptor, min(65_536, limit + 1 - len(raw)))
                if not block:
                    break
                raw.extend(block)
        finally:
            os.close(descriptor)
        final = os.lstat(path)
    except OSError:
        return None
    if (
        len(raw) > limit
        or len(raw) != after.st_size
        or (final.st_dev, final.st_ino) != (before.st_dev, before.st_ino)
    ):
        return None
    return bytes(raw)


def _object(path: Path, limit: int, *, mode: int | None = None) -> dict[str, object] | None:
    raw = _read_regular(path, limit, mode=mode)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _jsonl(path: Path) -> tuple[list[dict[str, object]], bool]:
    raw = _read_regular(path, MAX_JSONL)
    if raw is None:
        return [], False
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
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


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage(row: Mapping[str, object]) -> dict[str, int] | None:
    names = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    values = {name: _count(row.get(name)) for name in names}
    if any(value is None for value in values.values()):
        return None
    result = cast(dict[str, int], values)
    if (
        result["cached_input_tokens"] > result["input_tokens"]
        or result["reasoning_output_tokens"] > result["output_tokens"]
        or result["total_tokens"] != result["input_tokens"] + result["output_tokens"]
    ):
        return None
    result["uncached_input_tokens"] = result["input_tokens"] - result["cached_input_tokens"]
    return result


def _cost(raw: bytes) -> CostEventV1:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("cost event is not strict JSON") from exc
    if not isinstance(value, dict) or set(value) != _COST_FIELDS or value.get("v") != 1:
        raise ValueError("cost event schema differs")
    event = CostEventV1(**cast(dict[str, Any], value))
    if event.canonical_bytes() != raw:
        raise ValueError("cost event is not canonical")
    return event


def _safe_target(root: Path, relative: str) -> bytes | None:
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        return None
    try:
        canonical = root.resolve(strict=True)
        current = canonical
        for part in parts:
            current /= part
            observed = os.lstat(current)
            if stat.S_ISLNK(observed.st_mode):
                return None
    except OSError:
        return None
    return _read_regular(current, MAX_SOURCE, mode=0o644)


def _sum(rows: Sequence[Mapping[str, int]]) -> dict[str, int]:
    names = (
        "input_tokens",
        "cached_input_tokens",
        "uncached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    return {name: sum(row[name] for row in rows) for name in names}


def _empty_arm() -> dict[str, int]:
    return {
        "ready": 0,
        "root_tokens": 0,
        "worker_tokens": 0,
        "pipeline_tokens": 0,
        "combined_tokens": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "uncached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "workers": 0,
        "attempts": 0,
        "accepted": 0,
        "applied": 0,
        "oracle_scored": 0,
        "oracle_passed": 0,
        "misses": 0,
        "reuses": 0,
        "primes": 0,
        "receipts": 0,
        "failures": 0,
        "evidence_ok": 0,
        "protocol_ok": 0,
        "authority_ok": 0,
        "usage_ok": 0,
        "parity_ok": 0,
        "request_estimate_eligible": 0,
    }


def _arm(
    round_dir: Path,
    side: str,
    backend: str,
    repository: SQLiteRRCRepository,
) -> tuple[dict[str, int], dict[str, bool]]:
    metrics = _empty_arm()
    arm = round_dir / side
    hooks, hooks_ok = _jsonl(arm / "hook-events.jsonl")
    metrics["evidence_ok"] = int(
        hooks_ok and bool(hooks) and all(row.get("memory_backend") == backend for row in hooks)
    )
    if not metrics["evidence_ok"]:
        return metrics, {}

    prepared_rows = [row for row in hooks if row.get("event") == "rrcv2_assignment_prepared"]
    prepared: dict[str, dict[str, object]] = {}
    tool_to_attempt: dict[str, str] = {}
    for row in prepared_rows:
        attempt_id = row.get("attempt_id")
        tool_use_id = row.get("tool_use_id")
        task_id = row.get("task_id")
        branch = row.get("branch")
        if (
            not isinstance(attempt_id, str)
            or _HEX64.fullmatch(attempt_id) is None
            or not isinstance(tool_use_id, str)
            or not tool_use_id
            or not isinstance(task_id, str)
            or not task_id
            or branch not in {"miss", "reuse", "prime"}
            or attempt_id in prepared
            or tool_use_id in tool_to_attempt
        ):
            continue
        prepared[attempt_id] = row
        tool_to_attempt[tool_use_id] = attempt_id

    spawn_by_attempt: dict[str, str] = {}
    for row in hooks:
        if row.get("event") != "rrcv2_spawn_bound":
            continue
        attempt_id = row.get("attempt_id")
        agent_id = row.get("agent_id")
        tool_use_id = row.get("tool_use_id")
        if (
            isinstance(attempt_id, str)
            and isinstance(agent_id, str)
            and isinstance(tool_use_id, str)
            and tool_to_attempt.get(tool_use_id) == attempt_id
            and attempt_id not in spawn_by_attempt
        ):
            spawn_by_attempt[attempt_id] = agent_id
    started = {
        cast(str, row.get("attempt_id")): cast(str, row.get("agent_id"))
        for row in hooks
        if row.get("event") == "rrcv2_subagent_started"
        and isinstance(row.get("attempt_id"), str)
        and isinstance(row.get("agent_id"), str)
    }
    submitted = {
        cast(str, row.get("attempt_id")): row
        for row in hooks
        if row.get("event") == "rrcv2_worker_submitted"
        and isinstance(row.get("attempt_id"), str)
        and isinstance(row.get("agent_id"), str)
        and isinstance(row.get("transcript_sha256"), str)
    }
    usage_rows = [row for row in hooks if row.get("event") == "native_usage"]
    root_rows = [row for row in usage_rows if row.get("component") == "root"]
    worker_rows = [row for row in usage_rows if row.get("component") == "worker"]
    worker_usage = {
        cast(str, row.get("agent_id")): row
        for row in worker_rows
        if isinstance(row.get("agent_id"), str)
    }
    root_usage = _usage(root_rows[0]) if len(root_rows) == 1 else None
    session_ids = {
        row.get("session_id")
        for row in [*root_rows, *worker_rows]
        if isinstance(row.get("session_id"), str) and row.get("session_id")
    }

    waits = [row for row in hooks if row.get("event") == "rrcv2_wait_substituted"]
    readers = [row for row in hooks if row.get("event") == "rrcv2_result_reader_allowed"]
    root_merges = [
        row
        for row in hooks
        if row.get("event") == "root_merge" and (_count(row.get("chars")) or 0) > 0
    ]
    combined_rows = [row for row in hooks if row.get("event") == "rrcv2_combined_session_committed"]
    failures = sum(row.get("event") in _FAILURE_EVENTS for row in hooks)
    metrics["failures"] = failures
    metrics["workers"] = len(spawn_by_attempt)
    metrics["attempts"] = len(prepared)

    mode = "cold" if side == "a" else "warm"
    expected_branches = {"miss": 1}
    branches = [cast(str, row["branch"]) for row in prepared.values()]
    metrics["misses"] = branches.count("miss")
    metrics["reuses"] = branches.count("reuse")
    metrics["primes"] = branches.count("prime")

    protocol_ok = (
        len(prepared) == WORKER_COUNT
        and len(spawn_by_attempt) == WORKER_COUNT
        and started == spawn_by_attempt
        and set(submitted) == set(prepared)
        and all(
            submitted[attempt_id].get("agent_id") == spawn_by_attempt[attempt_id]
            for attempt_id in prepared
        )
        and len(waits) >= 1
        and all(
            1
            <= (count if (count := _count(row.get("target_count"))) is not None else 0)
            <= WORKER_COUNT
            and isinstance(row.get("wait_id"), str)
            and _HEX64.fullmatch(cast(str, row.get("wait_id"))) is not None
            and isinstance(row.get("wait_sha256"), str)
            and _HEX64.fullmatch(cast(str, row.get("wait_sha256"))) is not None
            for row in waits
        )
        and len(readers) == WORKER_COUNT
        and len(root_merges) == 1
        and len(combined_rows) == 1
        and failures == 0
        and {branch: branches.count(branch) for branch in set(branches)} == expected_branches
    )
    metrics["protocol_ok"] = int(protocol_ok)

    attempts = AttemptRepository(repository)
    cost_events: list[CostEventV1] = []
    scores: dict[str, bool] = {}
    authority_ok = True
    cell_id = f"rrcv2-{round_dir.name}-{side}"
    for attempt_id, row in prepared.items():
        try:
            registered = attempts.load_registered_input(attempt_id)
            terminal = repository.load_terminal_intent(attempt_id)
            if terminal is None or terminal[0] != "accepted":
                raise ValueError("attempt is not accepted")
            accepted = parse_accepted_commit(terminal[1])
            if (
                registered.attempt.owner_scope == ""
                or registered.task_envelope.task.task_id != row["task_id"]
                or registered.expected_tool_use_id != row["tool_use_id"]
                or accepted.mode != mode
                or accepted.transport != "contextmesh"
                or accepted.outcome.attempt_id != attempt_id
                or accepted.outcome.task_id != row["task_id"]
                or accepted.outcome.branch != row["branch"]
                or accepted.receipt_record is None
                or registered.target_root.resolve(strict=True)
                != (arm / "target").resolve(strict=True)
            ):
                raise ValueError("attempt authority differs from hook evidence")
            receipt_record = parse_receipt_record(accepted.receipt_record)
            receipt, payload = repository.load_receipt(attempt_id, receipt_record.receipt)
            if (
                receipt != receipt_record
                or payload.artifact_path != accepted.artifact_record.artifact_path
                or payload.source_sha256 != accepted.artifact_record.source_sha256
            ):
                raise ValueError("receipt differs from accepted authority")
            source = repository.load_accepted_source(
                attempt_id, accepted.outcome.artifact_record_sha256
            ).encode("utf-8", errors="strict")
            target = _safe_target(registered.target_root, accepted.artifact_record.artifact_path)
            if target != source or _sha(source) != accepted.artifact_record.source_sha256:
                raise ValueError("accepted source was not applied exactly")
            inventory = repository.load_call_inventory(attempt_id)
            if tuple(call_id for call_id, _state, _raw in inventory) != tuple(
                accepted.outcome.cost_event_ids
            ):
                raise ValueError("terminal cost inventory differs")
            attempt_costs: list[CostEventV1] = []
            for call_id, state, raw in inventory:
                if state != "call_committed" or raw is None:
                    raise ValueError("attempt has unquantified provider usage")
                event = _cost(raw)
                if (
                    event.cost_event_id != call_id
                    or event.attempt_id != attempt_id
                    or event.task_id != row["task_id"]
                    or event.arm != mode
                    or event.cell_id != cell_id
                ):
                    raise ValueError("cost event differs from attempt authority")
                attempt_costs.append(event)
            implements = [event for event in attempt_costs if event.stage == "implement"]
            agent_id = spawn_by_attempt[attempt_id]
            worker_row = worker_usage.get(agent_id)
            worker_totals = _usage(worker_row) if worker_row is not None else None
            submitted_row = submitted[attempt_id]
            if (
                len(implements) != 1
                or worker_row is None
                or worker_totals is None
                or implements[0].transcript_sha256 != submitted_row.get("transcript_sha256")
                or implements[0].transcript_sha256 != worker_row.get("transcript_sha256")
                or implements[0].input_tokens != worker_totals["input_tokens"]
                or implements[0].cached_input_tokens != worker_totals["cached_input_tokens"]
                or implements[0].output_tokens != worker_totals["output_tokens"]
                or implements[0].reasoning_output_tokens != worker_totals["reasoning_output_tokens"]
                or implements[0].provider_total_tokens != worker_totals["total_tokens"]
            ):
                raise ValueError("worker usage differs from its committed CostEvent")
            oracle = repository.load_oracle_score(attempt_id)
            if oracle is None or oracle[0] not in {"passed", "failed"} or oracle[1] is None:
                raise ValueError("attempt has no exact semantic-oracle score")
            scores[cast(str, row["task_id"])] = oracle[1]
            cost_events.extend(attempt_costs)
            metrics["accepted"] += 1
            metrics["applied"] += 1
            metrics["receipts"] += 1
            metrics["oracle_scored"] += 1
            metrics["oracle_passed"] += int(oracle[1])
        except (OSError, RuntimeError, TypeError, ValueError):
            authority_ok = False
    metrics["authority_ok"] = int(
        authority_ok
        and metrics["accepted"] == WORKER_COUNT
        and metrics["applied"] == WORKER_COUNT
        and metrics["receipts"] == WORKER_COUNT
        and metrics["oracle_scored"] == WORKER_COUNT
    )

    root_cost: CostEventV1 | None = None
    try:
        cells = SQLiteCellJournal(repository)
        combined = cells.load_combined_session(cell_id)
        root_cost = cells.load_root_cost_event(cell_id)
        if combined is None or root_cost is None or root_usage is None:
            raise ValueError("cell has no exact combined/root-cost authority")
        combined_attempt_ids = {row.attempt_id for row in combined.attempts}
        expected_cost_ids = {
            root_cost.cost_event_id,
            *(event.cost_event_id for event in cost_events),
        }
        matching_waits = [
            row
            for row in waits
            if row.get("wait_id") == combined.wait_id
            and row.get("wait_sha256") == combined.wait_envelope_sha256
        ]
        if (
            combined.cell_id != cell_id
            or combined.round_id != round_dir.name
            or combined_attempt_ids != set(prepared)
            or set(combined.all_cost_event_ids) != expected_cost_ids
            or combined.root_cost_event_id != root_cost.cost_event_id
            or combined.root_session_id != root_rows[0].get("session_id")
            or combined.root_transcript_sha256 != root_rows[0].get("transcript_sha256")
            or combined.root_final_sha256 != root_cost.final_message_sha256
            or len(matching_waits) != 1
            or combined_rows[0].get("cell_id") != cell_id
            or combined_rows[0].get("root_cost_event_id") != root_cost.cost_event_id
            or combined_rows[0].get("combined_session_sha256") != _sha(combined.canonical_bytes())
            or root_cost.input_tokens != root_usage["input_tokens"]
            or root_cost.cached_input_tokens != root_usage["cached_input_tokens"]
            or root_cost.output_tokens != root_usage["output_tokens"]
            or root_cost.reasoning_output_tokens != root_usage["reasoning_output_tokens"]
            or root_cost.provider_total_tokens != root_usage["total_tokens"]
        ):
            raise ValueError("combined root accounting differs from hook/attempt authority")
    except (OSError, RuntimeError, TypeError, ValueError):
        root_cost = None
        metrics["authority_ok"] = 0

    native_worker_agents = set(worker_usage)
    usage_ok = (
        root_usage is not None
        and root_cost is not None
        and len(worker_rows) == WORKER_COUNT
        and len(worker_usage) == WORKER_COUNT
        and native_worker_agents == set(spawn_by_attempt.values())
        and len(session_ids) == WORKER_COUNT + 1
        and all(_usage(row) is not None for row in worker_rows)
    )
    metrics["usage_ok"] = int(usage_ok)
    if root_cost is not None:
        metrics["root_tokens"] = root_cost.provider_total_tokens
    worker_costs = [event for event in cost_events if event.stage == "implement"]
    pipeline_costs = [event for event in cost_events if event.stage != "implement"]
    metrics["worker_tokens"] = sum(event.provider_total_tokens for event in worker_costs)
    metrics["pipeline_tokens"] = sum(event.provider_total_tokens for event in pipeline_costs)
    all_usage: list[dict[str, int]] = []
    if root_cost is not None:
        all_usage.append(
            {
                "input_tokens": root_cost.input_tokens,
                "cached_input_tokens": root_cost.cached_input_tokens,
                "uncached_input_tokens": root_cost.input_tokens - root_cost.cached_input_tokens,
                "output_tokens": root_cost.output_tokens,
                "reasoning_output_tokens": root_cost.reasoning_output_tokens,
                "total_tokens": root_cost.provider_total_tokens,
            }
        )
    all_usage.extend(
        {
            "input_tokens": event.input_tokens,
            "cached_input_tokens": event.cached_input_tokens,
            "uncached_input_tokens": event.input_tokens - event.cached_input_tokens,
            "output_tokens": event.output_tokens,
            "reasoning_output_tokens": event.reasoning_output_tokens,
            "total_tokens": event.provider_total_tokens,
        }
        for event in cost_events
    )
    totals = _sum(all_usage) if all_usage else _sum(())
    for name, value in totals.items():
        if name == "total_tokens":
            metrics["combined_tokens"] = value
        else:
            metrics[name] = value
    metrics["ready"] = int(
        metrics["evidence_ok"]
        and metrics["protocol_ok"]
        and metrics["authority_ok"]
        and metrics["usage_ok"]
    )
    return metrics, scores


def _snapshot_database(source: Path, destination: Path) -> bool:
    try:
        before = os.lstat(source)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > MAX_DATABASE
        ):
            return False
        input_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=1.0)
        output_db = sqlite3.connect(destination)
        try:
            input_db.backup(output_db)
        finally:
            output_db.close()
            input_db.close()
        os.chmod(destination, 0o600)
        after = os.lstat(source)
        return (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    except (OSError, sqlite3.Error):
        return False


def collect(
    round_id: str, *, memory_backend: str | None = None, root: Path = CM_ROOT
) -> dict[str, dict[str, int]]:
    round_dir = root / "runs" / "rrd-demo" / round_id
    meta = _object(round_dir / "round-meta.json", MAX_META, mode=0o600)
    backend = memory_backend or (str(meta.get("memory_backend")) if meta else "")
    meta_ok = bool(
        meta
        and set(meta) == {"v", "round_id", "memory_backend", "provider", "model"}
        and meta.get("v") == 2
        and meta.get("round_id") == round_id
        and meta.get("memory_backend") == backend
        and meta.get("provider") == "native-codex"
        and isinstance(meta.get("model"), str)
        and round_id.startswith(f"rrd-{backend}-")
    )
    if not meta_ok:
        return {"a": _empty_arm(), "b": _empty_arm()}
    with tempfile.TemporaryDirectory(prefix="rrcv2-meter-") as temporary:
        snapshot_path = Path(temporary) / "rrcv2.sqlite3"
        if not _snapshot_database(round_dir / "rrcv2.sqlite3", snapshot_path):
            return {"a": _empty_arm(), "b": _empty_arm()}
        try:
            with SQLiteRRCRepository(snapshot_path) as repository:
                a, a_scores = _arm(round_dir, "a", backend, repository)
                b, b_scores = _arm(round_dir, "b", backend, repository)
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            return {"a": _empty_arm(), "b": _empty_arm()}
    parity = int(bool(a_scores) and a_scores == b_scores)
    a["parity_ok"] = parity
    b["parity_ok"] = parity
    if not parity:
        a["ready"] = 0
        b["ready"] = 0
    return {"a": a, "b": b}


def render(
    round_id: str, snapshot: Mapping[str, Mapping[str, int]], memory_backend: str | None = None
) -> str:
    a, b = snapshot.get("a", {}), snapshot.get("b", {})
    ready = bool(a.get("ready") and b.get("ready"))
    delta = int(a.get("combined_tokens", 0)) - int(b.get("combined_tokens", 0))
    percent = 100 * delta / int(a.get("combined_tokens", 0)) if a.get("combined_tokens") else 0
    state = "READY" if ready else "NOT READY"
    lines = [
        f"Canonical ContextMesh + RRCv2 functional meter — {round_id}",
        f"state: {state} · memory={memory_backend or 'unknown'} · functional-only",
        "no demonstrated RRCv2 savings: effective identity or tier unattested",
        "",
        f"{'':26} {'COLD (a)':>14} {'WARM (b)':>14}",
        f"{'root tokens':26} {int(a.get('root_tokens', 0)):>14,} {int(b.get('root_tokens', 0)):>14,}",
        f"{'native worker tokens':26} {int(a.get('worker_tokens', 0)):>14,} {int(b.get('worker_tokens', 0)):>14,}",
        f"{'RRC pipeline tokens':26} {int(a.get('pipeline_tokens', 0)):>14,} {int(b.get('pipeline_tokens', 0)):>14,}",
        f"{'TOTAL provider-visible':26} {int(a.get('combined_tokens', 0)):>14,} {int(b.get('combined_tokens', 0)):>14,}",
        f"{'input / cached':26} {int(a.get('input_tokens', 0)):>6,}/{int(a.get('cached_input_tokens', 0)):<7,} {int(b.get('input_tokens', 0)):>6,}/{int(b.get('cached_input_tokens', 0)):<7,}",
        f"{'uncached / output':26} {int(a.get('uncached_input_tokens', 0)):>6,}/{int(a.get('output_tokens', 0)):<7,} {int(b.get('uncached_input_tokens', 0)):>6,}/{int(b.get('output_tokens', 0)):<7,}",
        f"{'reasoning output':26} {int(a.get('reasoning_output_tokens', 0)):>14,} {int(b.get('reasoning_output_tokens', 0)):>14,}",
        f"{'workers / accepted / applied':26} {int(a.get('workers', 0))}/{int(a.get('accepted', 0))}/{int(a.get('applied', 0)):>7} {int(b.get('workers', 0))}/{int(b.get('accepted', 0))}/{int(b.get('applied', 0)):>7}",
        f"{'MISS / REUSE / PRIME':26} {int(a.get('misses', 0))}/{int(a.get('reuses', 0))}/{int(a.get('primes', 0)):>7} {int(b.get('misses', 0))}/{int(b.get('reuses', 0))}/{int(b.get('primes', 0)):>7}",
        f"{'oracle pass@1':26} {int(a.get('oracle_passed', 0))}/{int(a.get('oracle_scored', 0)):>9} {int(b.get('oracle_passed', 0))}/{int(b.get('oracle_scored', 0)):>9}",
        "",
        f"observed functional WARM token delta: {delta:,} ({percent:.1f}% vs COLD)",
        "billing_exact=false · request_estimate_eligible=false · no economic/savings claim",
    ]
    if not ready:
        for label, side in (("a", a), ("b", b)):
            bad = [
                name.removesuffix("_ok")
                for name in ("evidence_ok", "protocol_ok", "authority_ok", "usage_ok", "parity_ok")
                if not side.get(name)
            ]
            if side.get("failures"):
                bad.append(f"{side['failures']} failure/policy events")
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
