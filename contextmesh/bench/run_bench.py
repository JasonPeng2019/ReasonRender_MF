#!/usr/bin/env python3
"""Run the native-Codex baseline/local/EverOS product comparison matrix."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CM_ROOT = Path(__file__).resolve().parents[1]
REPO = CM_ROOT.parent
TARGET_TEMPLATE = CM_ROOT / "bench" / "target-template"
RUBRIC_PATH = CM_ROOT / "bench" / "rubric-independent.json"
RUBRIC_SHA256 = "f2c3b64825230d1862cda33b82ea5697cebb2fb00e645fd381d29e76d77c91d2"
NATIVE_HOME = CM_ROOT / ".codex-rrd-native"
RUNS = CM_ROOT / "runs"
CELL_TIMEOUT_SECONDS = 720
MAX_TRANSCRIPT_BYTES = 100_000_000
NEUTRAL_WORKER_DESCRIPTION = "Audit exactly one assigned HTTP handler and report cited findings."
SCENARIOS: dict[str, tuple[str, ...]] = {
    "single-users": ("users",),
    "pair-users-products": ("users", "products"),
    "four-all": ("orders", "products", "reviews", "users"),
}
MATRIX_ORDER: tuple[tuple[str, str], ...] = (
    ("single-users", "baseline"),
    ("single-users", "combined-local"),
    ("single-users", "combined-everos"),
    ("pair-users-products", "combined-local"),
    ("pair-users-products", "combined-everos"),
    ("pair-users-products", "baseline"),
    ("four-all", "combined-everos"),
    ("four-all", "baseline"),
    ("four-all", "combined-local"),
)
ACTIVE_RUNTIME_MANIFEST = CM_ROOT / "active-runtime-files.txt"
SOURCE_PATHS = (
    "contextmesh/active-runtime-files.txt",
    *tuple(line for line in ACTIVE_RUNTIME_MANIFEST.read_text().splitlines() if line),
)

USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
CLAIM_RE = re.compile(
    r"^- (critical|high|medium|low) \| "
    r"(src/handlers/([a-z0-9_-]+)\.js):(\d+)(?:-(\d+))? \| (\S.*)$",
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"^## (src/handlers/([a-z0-9_-]+)\.js)$")
STOPWORDS = {
    "a",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "by",
    "can",
    "for",
    "from",
    "has",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "so",
    "that",
    "the",
    "their",
    "this",
    "to",
    "without",
}


class MatrixError(RuntimeError):
    """A cell cannot produce comparable evidence."""


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short metadata write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(value)
    os.chmod(path, 0o600)


def _seatbelt_rule(operation: str, kind: str, path: Path) -> str:
    return f"(deny {operation} ({kind} {json.dumps(str(path.resolve()))}))"


def execution_profile_text(*, cell: Path, target: Path, run_dir: Path, baseline: bool) -> str:
    """Build the exact per-cell macOS outer sandbox overlay."""
    base = _bounded_bytes(NATIVE_HOME / "credential-deny.sb", limit=100_000).decode("utf-8")
    rules = [base.rstrip(), _seatbelt_rule("file-write*", "subpath", target)]
    if baseline:
        rules.extend(
            _seatbelt_rule("file-read*", "literal", REPO / relative) for relative in SOURCE_PATHS
        )
        for path in sorted(RUNS.iterdir()):
            resolved = path.resolve()
            if resolved == run_dir.resolve():
                rules.append(_seatbelt_rule("file-read*", "literal", run_dir / "experiment.json"))
                cells = run_dir / "cells"
                if cells.exists():
                    rules.extend(
                        _seatbelt_rule("file-read*", "subpath", sibling)
                        for sibling in sorted(cells.iterdir())
                        if sibling.resolve() != cell.resolve()
                    )
            elif resolved != (NATIVE_HOME / "sessions").resolve():
                kind = "subpath" if path.is_dir() else "literal"
                rules.append(_seatbelt_rule("file-read*", kind, path))
    return "\n".join(rules) + "\n"


def _reset_native_runtime_state() -> None:
    """Remove prior isolated-session state while preserving keyring-bound config identity."""
    prefixes = ("goals_", "history", "logs_", "memories_", "queue_", "state_")
    names = {"log", "sessions", "shell_snapshots", "thread-writer-locks", "tmp"}
    for path in list(NATIVE_HOME.iterdir()):
        if path.name not in names and not path.name.startswith(prefixes):
            continue
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode):
            raise MatrixError(f"native runtime state is a symlink: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            shutil.rmtree(path)
        elif stat.S_ISREG(metadata.st_mode):
            path.unlink()
        else:
            raise MatrixError(f"native runtime state is special: {path}")


def _bounded_bytes(path: Path, *, limit: int = MAX_TRANSCRIPT_BYTES) -> bytes:
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise MatrixError(f"unbounded or special file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_size > limit
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise MatrixError(f"file changed or is not bounded regular data: {path}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise MatrixError(f"file exceeds byte limit: {path}")
        return data
    finally:
        os.close(descriptor)


def _strict_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = _bounded_bytes(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MatrixError(f"non-UTF-8 JSONL: {path}") from exc
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MatrixError(f"malformed JSONL {path}:{number}") from exc
        if not isinstance(value, dict):
            raise MatrixError(f"non-object JSONL {path}:{number}")
        rows.append(value)
    return rows


def snapshot_artifacts(
    sources: Iterable[tuple[str, Path]], *, destination: Path
) -> list[dict[str, Any]]:
    """Copy bounded evidence into the cell and return its immutable inventory."""
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    inventory: list[dict[str, Any]] = []
    for name, source in sources:
        data = _bounded_bytes(source)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short artifact write")
                view = view[written:]
        finally:
            os.close(descriptor)
        inventory.append({"name": name, "bytes": len(data), "sha256": _sha(data)})
    return sorted(inventory, key=lambda row: str(row["name"]))


def _write_cell_summary(cell: Path, summary: Mapping[str, Any]) -> None:
    path = cell / "summary.json"
    _atomic_json(path, dict(summary))
    _write_text(cell / "summary.sha256", _sha(_bounded_bytes(path)) + "\n")


def _load_cell_summary(cell: Path) -> dict[str, Any]:
    path = cell / "summary.json"
    try:
        data = _bounded_bytes(path, limit=2_000_000)
        expected = _bounded_bytes(cell / "summary.sha256", limit=65).decode("ascii").strip()
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MatrixError(f"invalid sealed cell summary: {path}") from exc
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or expected != _sha(data):
        raise MatrixError(f"cell summary seal mismatch: {path}")
    if not isinstance(value, dict) or value.get("cell_id") != cell.name:
        raise MatrixError(f"invalid cell summary identity: {path}")
    tokens = value.get("provider_visible_tokens")
    if tokens is not None and (
        not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0
    ):
        raise MatrixError(f"invalid cell token total: {path}")
    if value.get("valid"):
        artifacts = value.get("artifacts")
        if not isinstance(artifacts, dict):
            raise MatrixError(f"valid cell has no artifact inventory: {path}")
        for group in ("transcripts", "evidence"):
            rows = artifacts.get(group)
            if not isinstance(rows, list) or not rows:
                raise MatrixError(f"valid cell has no {group} inventory: {path}")
            for row in rows:
                if not isinstance(row, dict):
                    raise MatrixError(f"invalid {group} inventory row: {path}")
                name = row.get("name")
                relative = Path(name) if isinstance(name, str) else Path("/")
                if relative.is_absolute() or ".." in relative.parts or not name:
                    raise MatrixError(f"unsafe {group} artifact name: {path}")
                artifact = cell / group / relative
                artifact_data = _bounded_bytes(artifact)
                if row.get("bytes") != len(artifact_data) or row.get("sha256") != _sha(
                    artifact_data
                ):
                    raise MatrixError(f"{group} artifact seal mismatch: {artifact}")
        final = _bounded_bytes(cell / "final.md")
        if value.get("final_sha256") != _sha(final):
            raise MatrixError(f"final report seal mismatch: {path}")
    return value


def _resumable_cell(cell: Path) -> dict[str, Any] | None:
    summary = cell / "summary.json"
    seal = cell / "summary.sha256"
    if not summary.exists() and not seal.exists():
        return None
    try:
        return _load_cell_summary(cell)
    except (MatrixError, OSError):
        interrupted = cell.with_name(
            f".{cell.name}.interrupted-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"
        )
        os.replace(cell, interrupted)
        return None


def _source_hashes() -> dict[str, str]:
    return {name: _sha((REPO / name).read_bytes()) for name in SOURCE_PATHS}


def _target_hashes() -> dict[str, str]:
    return {
        str(path.relative_to(TARGET_TEMPLATE)): _sha(path.read_bytes())
        for path in sorted(TARGET_TEMPLATE.rglob("*"))
        if path.is_file()
    }


def matrix() -> dict[str, Any]:
    cells = [
        {
            "cell_id": f"{scenario}--{variant}",
            "scenario": scenario,
            "handlers": list(SCENARIOS[scenario]),
            "workers": len(SCENARIOS[scenario]),
            "variant": variant,
            "provider": "native-codex",
            "model": os.environ.get("RRD_CODEX_MODEL", "gpt-5.5"),
            "reasoning": os.environ.get("RRD_CODEX_REASONING", "medium"),
        }
        for scenario, variant in MATRIX_ORDER
    ]
    return {
        "v": 3,
        "created_at": int(time.time()),
        "billing_exact": False,
        "hidden_retry_observable": False,
        "cell_timeout_seconds": CELL_TIMEOUT_SECONDS,
        "cells": cells,
    }


def render_prompt(scenario: str) -> str:
    try:
        handlers = SCENARIOS[scenario]
    except KeyError as exc:
        raise MatrixError(f"unknown scenario: {scenario}") from exc
    assignments = "\n".join(
        f"- worker {index}: src/handlers/{handler}.js" for index, handler in enumerate(handlers, 1)
    )
    parallel = (
        "Launch all workers before making any wait call so their lifetimes overlap."
        if len(handlers) > 1
        else "Launch the one worker, then wait for it."
    )
    return f"""Audit the declared HTTP handlers for input-validation, authorization, and error-handling bugs.
Do not inspect source files in the root session and do not perform the audits yourself.
Spawn exactly {len(handlers)} worker subagent{"s" if len(handlers) != 1 else ""}, one per assignment:
{assignments}
For every spawn set agent_type="worker" and fork_context=false. {parallel}
Each worker must fully inspect only its assigned handler plus src/models.js, src/utils.js, and src/middleware.js, must not spawn another agent, and must not edit files.
Wait until every worker completes, then merge their supported findings without adding your own.
Output only this grammar, in assignment order, with one indivisible finding per bullet:
## src/handlers/<name>.js
- <severity> | src/handlers/<name>.js:<line[-line]> | <one sentence>
Severity must be exactly critical, high, medium, or low. Omit a heading only when its worker explicitly reports no findings.
"""


def codex_command(
    *,
    codex_bin: Path,
    prompt: str,
    final_path: Path,
    baseline: bool,
    profile_path: Path | None = None,
) -> list[str]:
    profile = profile_path or NATIVE_HOME / "credential-deny.sb"
    command = (
        ["/usr/bin/sandbox-exec", "-f", str(profile), str(codex_bin)]
        if sys.platform == "darwin"
        else [str(codex_bin)]
    )
    command.extend(["--strict-config", "--dangerously-bypass-hook-trust"])
    if sys.platform == "darwin":
        command.append("--dangerously-bypass-approvals-and-sandbox")
    if baseline:
        command.extend(
            [
                "-c",
                "features.hooks=false",
                "-c",
                f"agents.worker.description={json.dumps(NEUTRAL_WORKER_DESCRIPTION)}",
            ]
        )
    command.extend(
        [
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--output-last-message",
            str(final_path.resolve()),
            prompt,
        ]
    )
    return command


def codex_environment(
    *, codex_home: Path, codex_bin: Path, combined: Mapping[str, str] | None
) -> dict[str, str]:
    result = {
        "HOME": os.environ.get("HOME", "/private/tmp"),
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "CODEX_HOME": str(codex_home),
    }
    if combined is not None:
        result.update(combined)
        result["RRD_CODEX_BIN"] = str(codex_bin)
    return result


def memory_backend(variant: str) -> str:
    try:
        return {
            "baseline": "none",
            "combined-local": "sqlite",
            "combined-everos": "everos",
        }[variant]
    except KeyError as exc:
        raise MatrixError(f"unknown matrix variant: {variant}") from exc


def _timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise MatrixError("transcript timestamp is missing")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise MatrixError("transcript timestamp is malformed") from exc


def _validate_usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise MatrixError("transcript usage is missing")
    result: dict[str, int] = {}
    for field in USAGE_FIELDS:
        number = value.get(field)
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise MatrixError(f"transcript usage has invalid {field}")
        result[field] = number
    if (
        result["cached_input_tokens"] > result["input_tokens"]
        or result["cache_write_input_tokens"] > result["input_tokens"]
        or result["reasoning_output_tokens"] > result["output_tokens"]
        or result["total_tokens"] != result["input_tokens"] + result["output_tokens"]
    ):
        raise MatrixError("transcript usage arithmetic is invalid")
    return result


def parse_transcript(path: Path) -> dict[str, Any]:
    rows = _strict_jsonl(path)
    if not rows:
        raise MatrixError("transcript is empty")
    thread_id: str | None = None
    parent: str | None = None
    usage_rows: list[tuple[int, dict[str, int]]] = []
    task_complete: list[int] = []
    for index, row in enumerate(rows):
        payload = row.get("payload")
        if row.get("type") == "session_meta" and isinstance(payload, dict):
            candidate = payload.get("id") or payload.get("session_id")
            if isinstance(candidate, str) and candidate:
                thread_id = candidate
            candidate_parent = payload.get("parent_thread_id")
            parent = candidate_parent if isinstance(candidate_parent, str) else None
        if (
            row.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "token_count"
        ):
            info = payload.get("info")
            candidate_usage = info.get("total_token_usage") if isinstance(info, dict) else None
            usage_rows.append((index, _validate_usage(candidate_usage)))
        payload_type = payload.get("type") if isinstance(payload, dict) else None
        if row.get("type") == "event_msg" and payload_type == "task_complete":
            task_complete.append(index)
        names = {str(row.get("type", "")).lower(), str(payload_type or "").lower()}
        if any("error" in name or "failed" in name or "aborted" in name for name in names):
            raise MatrixError("transcript contains a visible error or failure event")
    if thread_id is None:
        raise MatrixError("transcript thread identity is missing")
    if not usage_rows:
        raise MatrixError("transcript usage is missing")
    for (_previous_index, previous), (_index, current) in zip(usage_rows, usage_rows[1:]):
        if any(current[field] < previous[field] for field in USAGE_FIELDS):
            raise MatrixError("transcript cumulative usage regressed")
    final_index, usage = usage_rows[-1]
    if len(task_complete) != 1 or task_complete[0] <= final_index:
        raise MatrixError("transcript usage is not followed by one task completion")
    for row in rows[final_index + 1 : task_complete[0]]:
        if row.get("type") != "world_state":
            raise MatrixError("transcript has model activity after final usage")
    if any(row.get("type") != "world_state" for row in rows[task_complete[0] + 1 :]):
        raise MatrixError("transcript has rows after task completion")
    return {
        "thread_id": thread_id,
        "parent_thread_id": parent,
        "start_ts": _timestamp(rows[0].get("timestamp")),
        "end_ts": _timestamp(rows[-1].get("timestamp")),
        "path": str(path),
        "sha256": _sha(path.read_bytes()),
        "usage": usage,
    }


def attribute_transcripts(
    paths: Iterable[Path], *, root_id: str, expected_workers: int, planner_ids: set[str]
) -> dict[str, Any]:
    parsed = [parse_transcript(path) for path in paths]
    by_id: dict[str, dict[str, Any]] = {}
    for row in parsed:
        identifier = str(row["thread_id"])
        if identifier in by_id:
            raise MatrixError(f"duplicate transcript identity: {identifier}")
        by_id[identifier] = row
    root = by_id.get(root_id)
    if root is None:
        raise MatrixError("root transcript is missing")
    workers = sorted(
        (row for row in parsed if row.get("parent_thread_id") == root_id),
        key=lambda row: str(row["thread_id"]),
    )
    if len(workers) != expected_workers:
        raise MatrixError(
            f"expected {expected_workers} direct worker transcripts, got {len(workers)}"
        )
    allowed = {root_id, *planner_ids, *(str(row["thread_id"]) for row in workers)}
    unexplained = sorted(set(by_id) - allowed)
    if unexplained:
        raise MatrixError(f"unexplained new transcripts: {unexplained}")
    overlap = expected_workers <= 1 or max(row["start_ts"] for row in workers) < min(
        row["end_ts"] for row in workers
    )
    if not overlap:
        raise MatrixError("worker transcript lifetimes did not overlap")
    return {
        "root_usage": root["usage"],
        "worker_usage": [row["usage"] for row in workers],
        "root_transcript": root,
        "worker_transcripts": workers,
        "planner_transcripts": sorted(
            (row for row in parsed if str(row["thread_id"]) in planner_ids),
            key=lambda row: str(row["thread_id"]),
        ),
        "planner_transcript_ids": sorted(set(by_id) & planner_ids),
        "workers_overlap": overlap,
    }


def _rubric() -> dict[str, Any]:
    if _sha(RUBRIC_PATH.read_bytes()) != RUBRIC_SHA256:
        raise MatrixError("tracked rubric hash drifted")
    value = json.loads(RUBRIC_PATH.read_text())
    if not isinstance(value, dict) or not isinstance(value.get("findings"), list):
        raise MatrixError("tracked rubric is malformed")
    expected = value.get("source_sha256")
    if not isinstance(expected, dict) or not all(
        isinstance(path, str) and isinstance(digest, str) for path, digest in expected.items()
    ):
        raise MatrixError("tracked rubric source authority is malformed")
    actual = {path: _sha((TARGET_TEMPLATE / path).read_bytes()) for path in expected}
    if expected != actual:
        raise MatrixError("tracked rubric source hashes drifted")
    return value


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) >= 3 and token not in STOPWORDS
    }


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _rubric_range(finding: Mapping[str, object]) -> tuple[int, int]:
    match = re.fullmatch(
        r"src/handlers/[a-z0-9_-]+\.js:(\d+)(?:-(\d+))?", str(finding.get("file_line", ""))
    )
    if match is None:
        raise MatrixError("rubric line range is malformed")
    start = int(match.group(1))
    return start, int(match.group(2) or start)


def _claim_edges(
    claims: list[dict[str, Any]], findings: list[dict[str, Any]]
) -> dict[int, list[tuple[int, int]]]:
    edges: dict[int, list[tuple[int, int]]] = {}
    for claim_index, claim in enumerate(claims):
        normalized_claim = _normalized(str(claim["text"]))
        claim_tokens = _tokens(str(claim["text"]))
        for finding_index, finding in enumerate(findings):
            if claim["handler"] != finding.get("handler"):
                continue
            if claim["severity"] != finding.get("severity"):
                continue
            start, end = _rubric_range(finding)
            if claim["end"] < start or end < claim["start"]:
                continue
            aliases = finding.get("aliases")
            if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
                raise MatrixError("rubric aliases are malformed")
            exact = any(_normalized(alias) in normalized_claim for alias in aliases)
            evidence = str(finding.get("exploit_impact", "")) + " " + " ".join(aliases)
            overlap = len(claim_tokens & _tokens(evidence))
            if exact or overlap >= 3:
                edges.setdefault(claim_index, []).append(
                    (finding_index, 100 + overlap if exact else overlap)
                )
    return edges


def _maximum_matches(
    claims: list[dict[str, Any]], findings: list[dict[str, Any]]
) -> list[tuple[int, int]]:
    edges = _claim_edges(claims, findings)
    # Rubric findings are partitioned by handler (at most eight), so an exact
    # mask DP is small and provides deterministic maximum-cardinality/weight matching.
    matches: list[tuple[int, int]] = []
    for handler in SCENARIOS["four-all"]:
        claim_ids = [index for index, claim in enumerate(claims) if claim["handler"] == handler]
        finding_ids = [
            index for index, finding in enumerate(findings) if finding.get("handler") == handler
        ]
        local = {global_id: local_id for local_id, global_id in enumerate(finding_ids)}
        states: dict[int, tuple[int, int, list[tuple[int, int]]]] = {0: (0, 0, [])}
        for claim_id in claim_ids:
            updated = dict(states)
            for mask, (count, weight, selected) in states.items():
                for finding_id, edge_weight in edges.get(claim_id, []):
                    if finding_id not in local:
                        continue
                    bit = 1 << local[finding_id]
                    if mask & bit:
                        continue
                    candidate = (
                        count + 1,
                        weight + edge_weight,
                        [*selected, (claim_id, finding_id)],
                    )
                    current = updated.get(mask | bit)
                    if current is None or candidate[:2] > current[:2]:
                        updated[mask | bit] = candidate
            states = updated
        best = max(states.values(), key=lambda value: value[:2])
        matches.extend(best[2])
    return matches


def score_report(report: str, *, scenario: str) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise MatrixError(f"unknown scenario: {scenario}")
    allowed = set(SCENARIOS[scenario])
    claims: list[dict[str, Any]] = []
    errors: list[str] = []
    headings: set[str] = set()
    semantic_review = False
    for number, raw in enumerate(report.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        heading = HEADING_RE.fullmatch(line)
        if heading:
            handler = heading.group(2)
            if handler not in allowed or handler in headings:
                errors.append(f"invalid heading at line {number}")
            headings.add(handler)
            continue
        match = CLAIM_RE.fullmatch(line)
        if match is None:
            errors.append(f"malformed claim at line {number}")
            continue
        severity, path, handler, start_text, end_text, text = match.groups()
        if handler not in allowed:
            errors.append(f"out-of-scope handler at line {number}")
            continue
        if handler not in headings:
            errors.append(f"claim before heading at line {number}")
        start = int(start_text)
        end = int(end_text or start_text)
        if start <= 0 or end < start or not text.endswith((".", "!", "?")):
            errors.append(f"invalid claim shape at line {number}")
            continue
        semantic_review = semantic_review or " and also " in text.lower() or ";" in text
        claims.append(
            {
                "line": number,
                "severity": severity.lower(),
                "path": path,
                "handler": handler,
                "start": start,
                "end": end,
                "text": text,
                "raw": line,
            }
        )
    rubric_findings = [
        finding
        for finding in _rubric()["findings"]
        if isinstance(finding, dict) and finding.get("handler") in allowed
    ]
    matches = _maximum_matches(claims, rubric_findings)
    matched_claims = {claim for claim, _finding in matches}
    matched_findings = {finding for _claim, finding in matches}
    unmatched = [claim for index, claim in enumerate(claims) if index not in matched_claims]
    handler_coverage = all(
        any(rubric_findings[finding].get("handler") == handler for finding in matched_findings)
        for handler in allowed
    )
    categories = {str(finding.get("category")) for finding in rubric_findings}
    matched_categories = {str(rubric_findings[index].get("category")) for index in matched_findings}
    total = len(rubric_findings)
    return {
        "valid": not errors,
        "errors": errors,
        "claims": claims,
        "claim_count": len(claims),
        "rubric_total": total,
        "matched": len(matches),
        "matched_rubric_ids": sorted(
            str(rubric_findings[index].get("stable_id")) for index in matched_findings
        ),
        "lexical_recall": len(matches) / total if total else 0.0,
        "lexical_precision": len(matches) / len(claims) if claims else 0.0,
        "handler_coverage": handler_coverage,
        "category_coverage": categories == matched_categories,
        "unmatched_claims": unmatched,
        "unmatched_high_or_critical": any(
            claim["severity"] in {"high", "critical"} for claim in unmatched
        ),
        "semantic_review_required": semantic_review,
        "metric": "lexical-rubric-match",
    }


def validate_combined_protocol(
    *,
    hooks: list[dict[str, Any]],
    packets: list[dict[str, Any]],
    handlers: Sequence[str],
    backend: str,
    planner_calls: int,
) -> dict[str, Any]:
    reasons: list[str] = []
    expected_handlers = set(handlers)
    if not hooks or any(row.get("memory_backend") != backend for row in hooks):
        reasons.append("hook backend evidence mismatch")
    if not packets or any(row.get("memory_backend") != backend for row in packets):
        reasons.append("packet backend evidence mismatch")
    assignments = {
        str(row.get("handler", "")).removeprefix("src/handlers/").removesuffix(".js")
        for row in hooks
        if row.get("event") == "assignment"
    }
    if assignments != expected_handlers:
        reasons.append("assignment handlers mismatch")
    spawned = {
        str(row["agent_id"])
        for row in hooks
        if row.get("event") == "spawned" and isinstance(row.get("agent_id"), str)
    }
    shared = {
        str(row["agent_id"])
        for row in hooks
        if row.get("event") == "shared_context" and isinstance(row.get("agent_id"), str)
    }
    finals = {
        str(row["agent_id"])
        for row in hooks
        if row.get("event") == "result_final" and isinstance(row.get("agent_id"), str)
    }
    if len(spawned) != len(handlers) or shared != spawned or finals != spawned:
        reasons.append("worker lifecycle mismatch")
    waited: list[str] = []
    for row in hooks:
        if row.get("event") != "wait_result":
            continue
        completed = row.get("completed_agent_ids")
        if (
            not isinstance(completed, list)
            or not all(isinstance(item, str) for item in completed)
            or row.get("result_count") != len(completed)
            or row.get("timed_out") is not False
        ):
            reasons.append("invalid wait evidence")
            continue
        waited.extend(completed)
    if len(waited) != len(handlers) or set(waited) != spawned:
        reasons.append("wait coverage mismatch")
    delivered: set[str] = set()
    for row in hooks:
        if row.get("event") == "compression_delivered" and isinstance(row.get("receipts"), dict):
            delivered.update(str(key) for key in row["receipts"])
        if row.get("event") == "compression_bypass" and isinstance(row.get("agent_ids"), list):
            delivered.update(str(item) for item in row["agent_ids"])
    if delivered != spawned:
        reasons.append("compression coverage mismatch")
    if sum(row.get("event") == "root_merge" for row in hooks) != 1:
        reasons.append("root merge mismatch")
    if any(
        row.get("event")
        in {"fail_open", "policy_deny", "compress_fail_open", "native_usage_missing"}
        for row in hooks
    ):
        reasons.append("combined failure event")
    packet_rows = [row for row in packets if row.get("event") == "packet"]
    packet_handlers = {
        str(row.get("handler", "")).removeprefix("src/handlers/").removesuffix(".js")
        for row in packet_rows
    }
    branches = [row.get("branch") for row in packet_rows]
    if (
        len(packet_rows) != len(handlers)
        or packet_handlers != expected_handlers
        or branches.count("miss") != 1
        or branches.count("hit") != len(handlers) - 1
    ):
        reasons.append("RRC packet pattern mismatch")
    if planner_calls != 1:
        reasons.append("RRC planner call mismatch")
    return {"valid": not reasons, "errors": reasons}


def validate_root_protocol(
    *,
    events: list[dict[str, Any]],
    handlers: Sequence[str],
    worker_ids: set[str],
    final: str,
) -> dict[str, Any]:
    """Validate that the root only coordinated the declared direct workers."""
    reasons: list[str] = []
    expected_handlers = set(handlers)
    spawns: list[dict[str, Any]] = []
    waited: set[str] = set()
    final_messages: list[str] = []
    for row in events:
        item = row.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") == "command_execution":
            command = item.get("command")
            if isinstance(command, str) and re.search(
                r"(?:^|[ /'\"])(?:\./)?src/(?:handlers/|models\.js|utils\.js|middleware\.js)",
                command,
            ):
                reasons.append("root directly inspected audited source")
        if row.get("type") != "item.completed":
            continue
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            final_messages.append(item["text"])
        if item.get("type") != "collab_tool_call" or item.get("status") != "completed":
            continue
        receivers = item.get("receiver_thread_ids")
        receiver_ids = (
            {str(value) for value in receivers}
            if isinstance(receivers, list) and all(isinstance(value, str) for value in receivers)
            else set()
        )
        if item.get("tool") == "spawn_agent":
            spawns.append(item)
        elif item.get("tool") == "wait":
            waited.update(receiver_ids)
    spawned_ids: set[str] = set()
    assigned_handlers: list[str] = []
    for spawn in spawns:
        receivers = spawn.get("receiver_thread_ids")
        if (
            not isinstance(receivers, list)
            or len(receivers) != 1
            or not isinstance(receivers[0], str)
        ):
            reasons.append("spawn did not create exactly one worker")
            continue
        spawned_ids.add(receivers[0])
        prompt = spawn.get("prompt")
        mentioned = {
            handler
            for handler in handlers
            if isinstance(prompt, str) and f"src/handlers/{handler}.js" in prompt
        }
        if len(mentioned) != 1:
            reasons.append("spawn prompt does not bind exactly one assigned handler")
        else:
            assigned_handlers.extend(mentioned)
    if len(spawns) != len(handlers) or set(assigned_handlers) != expected_handlers:
        reasons.append("spawn assignments mismatch")
    if spawned_ids != worker_ids:
        reasons.append("spawned worker identities mismatch transcripts")
    if not worker_ids.issubset(waited):
        reasons.append("root wait coverage mismatch")
    if not final.strip() or not final_messages or final_messages[-1].strip() != final.strip():
        reasons.append("root final message mismatch")
    if sum(row.get("type") == "turn.completed" for row in events) != 1:
        reasons.append("root turn completion mismatch")
    return {"valid": not reasons, "errors": sorted(set(reasons))}


def _codex_binary() -> Path:
    candidate = os.environ.get("RRD_CODEX_BIN") or shutil.which("codex")
    if not candidate:
        raise MatrixError("Codex is not installed")
    return Path(candidate).resolve(strict=True)


def _run_checked(
    command: Sequence[str], *, env: Mapping[str, str], cwd: Path = REPO, timeout: int = 240
) -> str:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise MatrixError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{result.stderr[-1000:]}"
        )
    return result.stdout


def _launcher_environment(backend: str, codex_bin: Path) -> dict[str, str]:
    return {
        "HOME": os.environ.get("HOME", "/private/tmp"),
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "RRD_MEMORY_BACKEND": backend,
        "RRD_CODEX_BIN": str(codex_bin),
        "RRD_EXTERNAL_SANDBOX": "1" if sys.platform == "darwin" else "0",
        "RRD_CODEX_MODEL": os.environ.get("RRD_CODEX_MODEL", "gpt-5.5"),
        "RRD_CODEX_REASONING": os.environ.get("RRD_CODEX_REASONING", "medium"),
        "RRC_DEMO_UV_BIN": str(Path(shutil.which("uv") or "uv").resolve()),
    }


def _stack(backend: str, codex_bin: Path, action: str) -> str:
    script = CM_ROOT / "scripts" / ("rrd_start_stack.sh" if action == "up" else "rrd_stop_stack.sh")
    return _run_checked([str(script)], env=_launcher_environment(backend, codex_bin), timeout=300)


def _make_target(destination: Path) -> None:
    shutil.copytree(TARGET_TEMPLATE, destination)
    for command in (
        ("git", "init", "-q"),
        ("git", "add", "-A"),
        (
            "git",
            "-c",
            "user.email=matrix@reasonrendercoding",
            "-c",
            "user.name=matrix",
            "commit",
            "-qm",
            "sealed native matrix target",
        ),
    ):
        _run_checked(command, env=os.environ, cwd=destination, timeout=30)


def _prepare_combined(backend: str, codex_bin: Path) -> tuple[str, Path, dict[str, str]]:
    launcher_env = _launcher_environment(backend, codex_bin)
    tui = CM_ROOT / "scripts" / "rrd_demo_tui.sh"
    _run_checked([str(tui), "reset"], env=launcher_env)
    round_file = RUNS / "rrd-demo" / f"round-{backend}"
    round_id = round_file.read_text().strip()
    _run_checked([str(tui), "seed"], env=launcher_env)
    arm = RUNS / "rrd-demo" / round_id / "b"
    combined = {
        "RRD_CODEX_MODEL": os.environ.get("RRD_CODEX_MODEL", "gpt-5.5"),
        "RRD_REPO_ROOT": str(REPO),
        "RRD_MEMORY_BACKEND": backend,
        "RRD_SUMMARY_MODE": "deterministic",
        "RRD_TARGET_ROOT": str(arm / "target"),
        "RRD_SEED_MANIFEST": str(arm / "seed-manifest.json"),
        "RRD_HOOK_EVENTS": str(arm / "hook-events.jsonl"),
        "RRD_RAW_RESULTS": str(arm / "raw-results"),
        "RRC_PLANNER_CODEX_HOME": str(NATIVE_HOME),
        "RRC_DEMO_UV_BIN": launcher_env["RRC_DEMO_UV_BIN"],
        "RRC_DEMO_ROUND": round_id,
        "RRC_DEMO_MODE": "warm",
        "RRC_DEMO_DATABASE": str(arm / "plan-spec.sqlite"),
        "RRC_DEMO_LOCK": str(arm / "plan-spec.lock"),
        "RRC_DEMO_EVENTS": str(arm / "rrc-events.jsonl"),
        "RRC_DEMO_MODEL_EVENTS": str(arm / "rrc-model-events.jsonl"),
        "RRC_STRONG_MODEL": launcher_env["RRD_CODEX_MODEL"],
        "RRC_PLANNER_TIMEOUT": "90",
        "RRC_LOCK_TIMEOUT": "120",
        "RRC_VISIBILITY_TIMEOUT": "10",
        "RRC_BRIDGE_TIMEOUT": "240",
        "RRD_EXTERNAL_SANDBOX": launcher_env["RRD_EXTERNAL_SANDBOX"],
    }
    if backend == "everos":
        combined["RRC_EVEROS_URL"] = "http://127.0.0.1:8000"
    return round_id, arm, combined


def _transcript_paths() -> set[Path]:
    sessions = NATIVE_HOME / "sessions"
    return set(sessions.rglob("*.jsonl")) if sessions.exists() else set()


def _root_id(events: Path) -> str:
    for row in _strict_jsonl(events):
        value = row.get("thread_id")
        if row.get("type") == "thread.started" and isinstance(value, str) and value:
            return value
    raise MatrixError("root JSON stream has no thread.started identity")


def _planner_rows(path: Path) -> tuple[list[dict[str, Any]], set[str], list[dict[str, int]]]:
    rows = _strict_jsonl(path)
    identifiers: set[str] = set()
    usages: list[dict[str, int]] = []
    for row in rows:
        if row.get("parse_status") != "ok" or row.get("exit_code") != 0:
            raise MatrixError("planner model event is not successful")
        stdout = row.get("stdout")
        if not isinstance(stdout, str):
            raise MatrixError("planner model event has no stdout")
        row_identifiers: set[str] = set()
        completed: dict[tuple[int, ...], dict[str, int]] = {}
        for line in stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MatrixError("planner stdout contains malformed JSON") from exc
            if not isinstance(value, dict):
                raise MatrixError("planner stdout row is not an object")
            event_type = value.get("type")
            if isinstance(event_type, str) and any(
                marker in event_type.lower() for marker in ("error", "failed", "retry")
            ):
                raise MatrixError("planner stdout contains a visible failure or retry")
            if event_type == "thread.started":
                thread_id = value.get("thread_id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise MatrixError("planner thread identity is malformed")
                row_identifiers.add(thread_id)
            if event_type == "turn.completed":
                raw = value.get("usage")
                if not isinstance(raw, dict):
                    raise MatrixError("planner usage is malformed")
                converted = {
                    "input_tokens": raw.get("input_tokens"),
                    "cached_input_tokens": raw.get("cached_input_tokens", 0),
                    "cache_write_input_tokens": raw.get("cache_write_input_tokens", 0),
                    "output_tokens": raw.get("output_tokens"),
                    "reasoning_output_tokens": raw.get("reasoning_output_tokens", 0),
                    "total_tokens": (raw.get("input_tokens") or 0)
                    + (raw.get("output_tokens") or 0),
                }
                usage = _validate_usage(converted)
                completed[tuple(usage[field] for field in USAGE_FIELDS)] = usage
        if len(row_identifiers) != 1:
            raise MatrixError("planner model event must contain exactly one call identity")
        if not completed:
            raise MatrixError("planner event has no final usage")
        if len(completed) != 1:
            raise MatrixError("planner event has conflicting final usage")
        identifiers.update(row_identifiers)
        usages.append(next(iter(completed.values())))
    return rows, identifiers, usages


def _run_codex_process(
    *, command: Sequence[str], env: Mapping[str, str], cwd: Path, cell: Path
) -> tuple[int, bool, float]:
    stdout_path = cell / "root-events.jsonl"
    stderr_path = cell / "codex.stderr.log"
    started = time.monotonic()
    timed_out = False
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=dict(env),
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            process.wait(timeout=CELL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    os.chmod(stdout_path, 0o600)
    os.chmod(stderr_path, 0o600)
    return process.returncode, timed_out, round(time.monotonic() - started, 3)


def _combined_protocol(
    arm: Path, handlers: Sequence[str], backend: str
) -> tuple[dict[str, Any], list[dict[str, int]], set[str]]:
    hooks = _strict_jsonl(arm / "hook-events.jsonl")
    packets = _strict_jsonl(arm / "rrc-events.jsonl")
    model_rows, planner_ids, planner_usage = _planner_rows(arm / "rrc-model-events.jsonl")
    protocol = validate_combined_protocol(
        hooks=hooks,
        packets=packets,
        handlers=handlers,
        backend=backend,
        planner_calls=len(model_rows),
    )
    return protocol, planner_usage, planner_ids


def _usage_total(rows: Iterable[Mapping[str, int]]) -> int:
    return sum(row["total_tokens"] for row in rows)


def run_cell(run_dir: Path, scenario: str, variant: str, codex_bin: Path) -> dict[str, Any]:
    cell_id = f"{scenario}--{variant}"
    cell = run_dir / "cells" / cell_id
    if cell.exists():
        raise MatrixError(f"cell directory already exists: {cell_id}")
    cell.mkdir(parents=True, mode=0o700)
    prompt = render_prompt(scenario)
    _write_text(cell / "prompt.txt", prompt)
    handlers = SCENARIOS[scenario]
    baseline = variant == "baseline"
    backend = memory_backend(variant)
    errors: list[str] = []
    round_id: str | None = None
    arm: Path | None = None
    stack_started = False
    source_before = _source_hashes()
    before_transcripts = _transcript_paths()
    target = cell / "target"
    combined_values: dict[str, str] | None = None
    exit_code = -1
    timed_out = False
    wall = 0.0
    try:
        if baseline:
            _make_target(target)
        else:
            _stack(backend, codex_bin, "up")
            stack_started = True
            round_id, arm, combined_values = _prepare_combined(backend, codex_bin)
            target = arm / "target"
        _reset_native_runtime_state()
        final = (cell / "final.md").resolve()
        profile = cell / "execution.sb"
        _write_text(
            profile,
            execution_profile_text(cell=cell, target=target, run_dir=run_dir, baseline=baseline),
        )
        target_before = {
            str(path.relative_to(target)): _sha(path.read_bytes())
            for path in sorted((target / "src").rglob("*.js"))
        }
        command = codex_command(
            codex_bin=codex_bin,
            prompt=prompt,
            final_path=final,
            baseline=baseline,
            profile_path=profile,
        )
        env = codex_environment(
            codex_home=NATIVE_HOME, codex_bin=codex_bin, combined=combined_values
        )
        _atomic_json(
            cell / "resolved.json",
            {
                "cell_id": cell_id,
                "scenario": scenario,
                "variant": variant,
                "backend": backend,
                "handlers": list(handlers),
                "workers": len(handlers),
                "prompt_sha256": _sha(prompt),
                "command_sha256": _sha(json.dumps(command, separators=(",", ":"))),
                "execution_profile_sha256": _sha(profile.read_bytes()),
                "environment_names": sorted(env),
                "baseline_neutral": baseline
                and not any(key.startswith(("RRD_", "RRC_")) for key in env),
                "round_id": round_id,
                "source_hashes": source_before,
                "target_hashes": target_before,
            },
        )
        exit_code, timed_out, wall = _run_codex_process(
            command=command, env=env, cwd=target, cell=cell
        )
        if final.exists():
            os.chmod(final, 0o600)
        time.sleep(0.2)
        new_transcripts = _transcript_paths() - before_transcripts
        planner_usage: list[dict[str, int]] = []
        planner_ids: set[str] = set()
        protocol: dict[str, Any]
        if baseline:
            protocol = {"valid": True, "errors": []}
        else:
            if arm is None:
                raise MatrixError("combined arm was not prepared")
            protocol, planner_usage, planner_ids = _combined_protocol(arm, handlers, backend)
        attribution = attribute_transcripts(
            new_transcripts,
            root_id=_root_id(cell / "root-events.jsonl"),
            expected_workers=len(handlers),
            planner_ids=planner_ids,
        )
        report = final.read_text() if final.exists() else ""
        root_events = _strict_jsonl(cell / "root-events.jsonl")
        worker_ids = {str(row["thread_id"]) for row in attribution["worker_transcripts"]}
        root_protocol = validate_root_protocol(
            events=root_events,
            handlers=handlers,
            worker_ids=worker_ids,
            final=report,
        )
        quality = score_report(report, scenario=scenario)
        source_unchanged = source_before == _source_hashes()
        target_unchanged = target_before == {
            str(path.relative_to(target)): _sha(path.read_bytes())
            for path in sorted((target / "src").rglob("*.js"))
        }
        target_clean = not _run_checked(
            ("git", "status", "--porcelain"), env=os.environ, cwd=target
        ).strip()
        native_rows = [attribution["root_usage"], *attribution["worker_usage"]]
        transcript_inventory = snapshot_artifacts(
            [
                (f"{row['thread_id']}.jsonl", Path(str(row["path"])))
                for row in [
                    attribution["root_transcript"],
                    *attribution["worker_transcripts"],
                    *attribution["planner_transcripts"],
                ]
            ],
            destination=cell / "transcripts",
        )
        evidence_sources: list[tuple[str, Path]] = [
            ("root-events.jsonl", cell / "root-events.jsonl"),
            ("codex.stderr.log", cell / "codex.stderr.log"),
            ("execution.sb", profile),
            ("resolved.json", cell / "resolved.json"),
            ("final.md", final),
        ]
        if arm is not None:
            evidence_sources.extend(
                [
                    ("hook-events.jsonl", arm / "hook-events.jsonl"),
                    ("rrc-events.jsonl", arm / "rrc-events.jsonl"),
                    ("rrc-model-events.jsonl", arm / "rrc-model-events.jsonl"),
                    ("seed-manifest.json", arm / "seed-manifest.json"),
                    ("round-meta.json", arm.parent / "round-meta.json"),
                ]
            )
        evidence_inventory = snapshot_artifacts(evidence_sources, destination=cell / "evidence")
        if exit_code != 0:
            errors.append(f"Codex exited {exit_code}")
        if timed_out:
            errors.append("cell wall timeout")
        if not protocol["valid"]:
            errors.extend(protocol["errors"])
        if not root_protocol["valid"]:
            errors.extend(root_protocol["errors"])
        if not quality["valid"]:
            errors.extend(quality["errors"])
        if not source_unchanged:
            errors.append("source drift during cell")
        if not target_unchanged or not target_clean:
            errors.append("target drift during cell")
        summary = {
            "v": 1,
            "cell_id": cell_id,
            "scenario": scenario,
            "variant": variant,
            "backend": backend,
            "handlers": list(handlers),
            "workers": len(handlers),
            "round_id": round_id,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "wall_seconds": wall,
            "valid": not errors,
            "errors": errors,
            "root_tokens": attribution["root_usage"]["total_tokens"],
            "worker_tokens": _usage_total(attribution["worker_usage"]),
            "planner_tokens": _usage_total(planner_usage),
            "provider_visible_tokens": _usage_total([*native_rows, *planner_usage]),
            "cached_input_tokens": sum(
                row["cached_input_tokens"] for row in [*native_rows, *planner_usage]
            ),
            "billing_exact": False,
            "hidden_retry_observable": False,
            "workers_overlap": attribution["workers_overlap"],
            "protocol": protocol,
            "root_protocol": root_protocol,
            "quality": quality,
            "source_hashes_unchanged": source_unchanged,
            "target_hashes_unchanged": target_unchanged,
            "target_git_clean": target_clean,
            "artifacts": {
                "transcripts": transcript_inventory,
                "evidence": evidence_inventory,
            },
            "final_sha256": _sha(report),
            "final_chars": len(report),
            "completed_at": _now(),
        }
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        summary = {
            "v": 1,
            "cell_id": cell_id,
            "scenario": scenario,
            "variant": variant,
            "backend": backend,
            "handlers": list(handlers),
            "workers": len(handlers),
            "round_id": round_id,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "wall_seconds": wall,
            "valid": False,
            "errors": errors,
            "provider_visible_tokens": None,
            "billing_exact": False,
            "hidden_retry_observable": False,
            "completed_at": _now(),
        }
    finally:
        if stack_started:
            try:
                _stack(backend, codex_bin, "down")
            except Exception as exc:
                summary["valid"] = False
                summary.setdefault("errors", []).append(f"teardown: {type(exc).__name__}: {exc}")
    _write_cell_summary(cell, summary)
    return summary


def _eligible(combined: Mapping[str, Any], baseline: Mapping[str, Any]) -> bool:
    if not combined.get("valid") or not baseline.get("valid"):
        return False
    quality = combined.get("quality")
    baseline_quality = baseline.get("quality")
    if not isinstance(quality, dict) or not isinstance(baseline_quality, dict):
        return False
    return bool(
        quality.get("lexical_recall", 0) >= 0.70
        and quality.get("lexical_precision", 0) >= 0.90
        and quality.get("handler_coverage")
        and quality.get("category_coverage")
        and not quality.get("unmatched_high_or_critical")
        and not quality.get("semantic_review_required")
        and quality.get("lexical_recall", 0) >= baseline_quality.get("lexical_recall", 0)
        and quality.get("lexical_precision", 0) >= baseline_quality.get("lexical_precision", 0)
    )


def aggregate(run_dir: Path) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    for scenario, variant in MATRIX_ORDER:
        path = run_dir / "cells" / f"{scenario}--{variant}" / "summary.json"
        if not path.exists():
            cells.append(
                {
                    "cell_id": f"{scenario}--{variant}",
                    "scenario": scenario,
                    "variant": variant,
                    "valid": False,
                    "errors": ["cell not attempted"],
                    "provider_visible_tokens": None,
                }
            )
        else:
            cells.append(_load_cell_summary(path.parent))
    comparisons: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        by_variant = {cell["variant"]: cell for cell in cells if cell["scenario"] == scenario}
        baseline = by_variant["baseline"]
        for variant in ("combined-local", "combined-everos"):
            combined = by_variant[variant]
            baseline_tokens = baseline.get("provider_visible_tokens")
            combined_tokens = combined.get("provider_visible_tokens")
            reduction: float | None = None
            if (
                isinstance(baseline_tokens, int)
                and baseline_tokens > 0
                and isinstance(combined_tokens, int)
            ):
                reduction = (baseline_tokens - combined_tokens) / baseline_tokens
            quality_eligible = _eligible(combined, baseline)
            savings_eligible = bool(quality_eligible and reduction is not None and reduction > 0)
            comparisons.append(
                {
                    "scenario": scenario,
                    "variant": variant,
                    "baseline_tokens": baseline_tokens,
                    "combined_tokens": combined_tokens,
                    "observed_reduction": reduction,
                    "quality_eligible": quality_eligible,
                    "savings_eligible": savings_eligible,
                    "label": "token savings" if savings_eligible else "observed delta",
                }
            )
    result = {
        "v": 1,
        "generated_at": _now(),
        "billing_exact": False,
        "hidden_retry_observable": False,
        "cells": cells,
        "comparisons": comparisons,
        "attempted": sum("cell not attempted" not in cell.get("errors", []) for cell in cells),
        "valid_cells": sum(bool(cell.get("valid")) for cell in cells),
        "all_started_provider_visible_tokens": sum(
            cell["provider_visible_tokens"]
            for cell in cells
            if isinstance(cell.get("provider_visible_tokens"), int)
        ),
    }
    _atomic_json(run_dir / "aggregate.json", result)
    _write_text(run_dir / "report.md", render_report(result))
    return result


def _percent(value: object) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{100 * value:.1f}%"


def render_report(result: Mapping[str, Any]) -> str:
    lines = [
        "# Native Codex RRD controlled matrix",
        "",
        "Provider-visible usage is observational, includes cached input, and is not exact billing.",
        "Quality is deterministic lexical rubric-match quality, not semantic adjudication.",
        "Valid means the execution/evidence protocol passed; it does not mean savings-quality eligible.",
        "",
        "| cell | valid | workers | root | workers | planner | total | recall | precision | wall s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    cells = result.get("cells")
    if isinstance(cells, list):
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            quality_value = cell.get("quality")
            quality: dict[str, Any] = quality_value if isinstance(quality_value, dict) else {}
            lines.append(
                f"| {cell.get('cell_id')} | {str(bool(cell.get('valid'))).lower()} | "
                f"{cell.get('workers', 'n/a')} | {cell.get('root_tokens', 'n/a')} | "
                f"{cell.get('worker_tokens', 'n/a')} | {cell.get('planner_tokens', 'n/a')} | "
                f"{cell.get('provider_visible_tokens', 'n/a')} | {_percent(quality.get('lexical_recall'))} | "
                f"{_percent(quality.get('lexical_precision'))} | {cell.get('wall_seconds', 'n/a')} |"
            )
    lines.extend(
        [
            "",
            "## Comparisons",
            "",
            "| scenario | combined | baseline tokens | combined tokens | delta | eligibility |",
            "|---|---|---:|---:|---:|---|",
        ]
    )
    comparisons = result.get("comparisons")
    if isinstance(comparisons, list):
        for row in comparisons:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"| {row.get('scenario')} | {row.get('variant')} | {row.get('baseline_tokens')} | "
                f"{row.get('combined_tokens')} | {_percent(row.get('observed_reduction'))} | "
                f"{row.get('label')} |"
            )
    lines.extend(
        [
            "",
            f"Valid cells: {result.get('valid_cells')}/{len(MATRIX_ORDER)}.",
            f"All-started provider-visible tokens: {result.get('all_started_provider_visible_tokens')}.",
            "",
        ]
    )
    return "\n".join(lines)


def _initialize_run(run_dir: Path, codex_bin: Path) -> None:
    run_dir.mkdir(parents=True, mode=0o700)
    os.chmod(run_dir, 0o700)
    config = NATIVE_HOME / "config.toml"
    hooks = NATIVE_HOME / "hooks.json"
    profile = NATIVE_HOME / "credential-deny.sb"
    _atomic_json(
        run_dir / "experiment.json",
        {
            **matrix(),
            "created_at_utc": _now(),
            "git_head": _run_checked(("git", "rev-parse", "HEAD"), env=os.environ).strip(),
            "git_status_sha256": _sha(_run_checked(("git", "status", "--short"), env=os.environ)),
            "codex_binary": str(codex_bin),
            "codex_binary_sha256": _sha(codex_bin.read_bytes()),
            "codex_version": _run_checked((str(codex_bin), "--version"), env=os.environ).strip(),
            "config_sha256": _sha(config.read_bytes()),
            "hooks_sha256": _sha(hooks.read_bytes()),
            "sandbox_profile_sha256": _sha(profile.read_bytes()),
            "rubric_sha256": _sha(RUBRIC_PATH.read_bytes()),
            "source_hashes": _source_hashes(),
            "target_hashes": _target_hashes(),
        },
    )


def validate_resume(run_dir: Path, codex_bin: Path) -> None:
    experiment_path = run_dir / "experiment.json"
    try:
        experiment = json.loads(_bounded_bytes(experiment_path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MatrixError("resume experiment is malformed") from exc
    if not isinstance(experiment, dict):
        raise MatrixError("resume experiment is not an object")
    expected = matrix()
    checks = (
        (experiment.get("cells") == expected["cells"], "matrix cells"),
        (
            experiment.get("cell_timeout_seconds") == CELL_TIMEOUT_SECONDS,
            "wall timeout",
        ),
        (experiment.get("codex_binary") == str(codex_bin), "Codex binary path"),
        (
            experiment.get("codex_binary_sha256") == _sha(codex_bin.read_bytes()),
            "Codex binary hash",
        ),
        (experiment.get("rubric_sha256") == RUBRIC_SHA256, "rubric hash"),
        (
            experiment.get("sandbox_profile_sha256")
            == _sha((NATIVE_HOME / "credential-deny.sb").read_bytes()),
            "sandbox profile hash",
        ),
        (experiment.get("source_hashes") == _source_hashes(), "source hashes"),
        (experiment.get("target_hashes") == _target_hashes(), "target hashes"),
    )
    for valid, label in checks:
        if not valid:
            raise MatrixError(f"resume {label} drifted")


def _native_config_command(codex_bin: Path) -> tuple[str, ...]:
    return (
        "python3",
        str(CM_ROOT / "scripts" / "rrd_native_config.py"),
        "check",
        "--root",
        str(CM_ROOT),
        "--codex-bin",
        str(codex_bin),
        "--model",
        os.environ.get("RRD_CODEX_MODEL", "gpt-5.5"),
        "--reasoning",
        os.environ.get("RRD_CODEX_REASONING", "medium"),
    )


def run_matrix(run_dir: Path | None = None, *, resume: bool = False) -> tuple[Path, dict[str, Any]]:
    codex_bin = _codex_binary()
    RUNS.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = RUNS / "native-matrix.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MatrixError("another native matrix owns the stable Codex home") from exc
        check_env = _launcher_environment("sqlite", codex_bin)
        _run_checked(_native_config_command(codex_bin), env=check_env)
        _rubric()
        selected = (
            run_dir or RUNS / f"native-matrix-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        ).resolve()
        if selected.exists():
            if not resume:
                raise MatrixError(f"run directory exists: {selected}")
            validate_resume(selected, codex_bin)
        else:
            if resume:
                raise MatrixError(f"resume directory does not exist: {selected}")
            _initialize_run(selected, codex_bin)
        for scenario, variant in MATRIX_ORDER:
            cell = selected / "cells" / f"{scenario}--{variant}"
            summary_path = cell / "summary.json"
            seal_path = cell / "summary.sha256"
            if summary_path.exists() or seal_path.exists():
                summary = _resumable_cell(cell)
                if summary is not None:
                    print(
                        json.dumps(
                            {
                                "cell": cell.name,
                                "resumed": True,
                                "valid": summary.get("valid"),
                            },
                            separators=(",", ":"),
                        ),
                        flush=True,
                    )
                    continue
            if cell.exists():
                interrupted = cell.with_name(
                    f".{cell.name}.interrupted-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"
                )
                os.replace(cell, interrupted)
            summary = run_cell(selected, scenario, variant, codex_bin)
            print(
                json.dumps(
                    {
                        "cell": summary["cell_id"],
                        "valid": summary["valid"],
                        "tokens": summary.get("provider_visible_tokens"),
                        "errors": summary.get("errors"),
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
        result = aggregate(selected)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    return selected, result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true", help="write the nine-cell offline manifest")
    action.add_argument("--canary", action="store_true", help="run the native local model canary")
    action.add_argument(
        "--run-matrix", action="store_true", help="execute all nine controlled cells"
    )
    action.add_argument("--report", type=Path, help="regenerate a report from an existing run")
    parser.add_argument("--output", type=Path, help="manifest output for --plan")
    parser.add_argument("--run-dir", type=Path, help="new artifact directory for --run-matrix")
    parser.add_argument(
        "--resume", action="store_true", help="resume a source-identical --run-dir without reruns"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.plan:
        output = (
            args.output
            or RUNS / f"native-matrix-plan-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
        )
        _atomic_json(output, matrix())
        print(output)
        return 0
    if args.canary:
        result = subprocess.run([CM_ROOT / "RRDdemo-local.sh", "canary"], check=False)
        return result.returncode
    if args.report is not None:
        result = aggregate(args.report)
        print(json.dumps({"run": str(args.report), "valid_cells": result["valid_cells"]}))
        return 0
    if args.resume and args.run_dir is None:
        raise MatrixError("--resume requires --run-dir")
    selected, result = run_matrix(args.run_dir, resume=args.resume)
    print(json.dumps({"run": str(selected), "valid_cells": result["valid_cells"]}))
    return 0 if result["valid_cells"] == len(MATRIX_ORDER) else 1


if __name__ == "__main__":
    raise SystemExit(main())
