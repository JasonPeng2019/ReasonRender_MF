#!/usr/bin/env python3
"""Run the native-Codex baseline/local/EverOS product comparison matrix."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import random
import re
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

CM_ROOT = Path(__file__).resolve().parents[1]
REPO = CM_ROOT.parent
TARGET_TEMPLATE = CM_ROOT / "bench" / "target-template"
RUBRIC_PATH = CM_ROOT / "bench" / "rubric-independent.json"
RUBRIC_SHA256 = "f2c3b64825230d1862cda33b82ea5697cebb2fb00e645fd381d29e76d77c91d2"
NATIVE_HOME = CM_ROOT / ".codex-rrd-native"
RUNS = CM_ROOT / "runs"
CELL_TIMEOUT_SECONDS = 720
MAX_TRANSCRIPT_BYTES = 100_000_000
NEUTRAL_WORKER_DESCRIPTION = (
    "Audit exactly one assigned HTTP handler and report cited findings. Unless an immutable "
    "exact-source bundle explicitly forbids tools, read exactly the assigned handler plus "
    "src/models.js, src/utils.js, and src/middleware.js using one shell call per file whose "
    "complete command is `/usr/bin/nl -ba <path>`; use no wildcard, script, or other tool."
)
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
ABLATION_VARIANTS = ("native", "rrc", "contextmesh", "combined")
ABLATION_REPLICATES = 4
CALIBRATION_REPLICATES = 4
PRICE_TABLE: dict[str, tuple[float, float, float]] = {
    "gpt-5.5": (5.0, 0.50, 30.0),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
}
FROZEN_ROLE_CONFIG = {
    "RRD_CODEX_MODEL": "gpt-5.5",
    "RRD_CODEX_REASONING": "medium",
    "RRD_WORKER_MODEL": "gpt-5.4-mini",
    "RRD_WORKER_REASONING": "low",
}
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
SENTENCE_END_RE = re.compile(r"[.!?](?:[`'\"’”)}\]]*)$")


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


def randomization_seeds(seed: int) -> dict[str, int]:
    return {
        name: int(_sha(f"rrd-ablation:{seed}:{name}")[:16], 16)
        for name in (
            "variant_permutation",
            "replicate_order",
            "block_order",
            "calibration_order",
            "judge_labels",
        )
    }


def ablation_schedule(*, seed: int) -> list[dict[str, Any]]:
    """Return four position-balanced replicates for every worker-count scenario."""

    seeds = randomization_seeds(seed)
    variant_rng = random.Random(seeds["variant_permutation"])
    replicate_rng = random.Random(seeds["replicate_order"])
    block_rng = random.Random(seeds["block_order"])
    blocks: list[list[dict[str, Any]]] = []
    for scenario, handlers in SCENARIOS.items():
        base = list(ABLATION_VARIANTS)
        variant_rng.shuffle(base)
        replicate_order = list(range(ABLATION_REPLICATES))
        replicate_rng.shuffle(replicate_order)
        for replicate_index in replicate_order:
            rotation = base[replicate_index:] + base[:replicate_index]
            replicate = replicate_index + 1
            blocks.append(
                [
                    {
                        "cell_id": f"{scenario}--r{replicate:02d}--{variant}",
                        "scenario": scenario,
                        "handlers": list(handlers),
                        "workers": len(handlers),
                        "replicate": replicate,
                        "position": position,
                        "variant": variant,
                        "rrc_enabled": variant in {"rrc", "combined"},
                        "contextmesh_enabled": variant in {"contextmesh", "combined"},
                    }
                    for position, variant in enumerate(rotation)
                ]
            )
    block_rng.shuffle(blocks)
    return [cell for block in blocks for cell in block]


def calibration_schedule(*, seed: int) -> list[dict[str, Any]]:
    """Return four randomized live/control pairs with balanced first position."""

    rng = random.Random(randomization_seeds(seed)["calibration_order"])
    first_live = [True, True, False, False]
    rng.shuffle(first_live)
    pairs: list[list[dict[str, Any]]] = []
    for replicate, live_first in enumerate(first_live, 1):
        order = ("live", "deterministic") if live_first else ("deterministic", "live")
        pairs.append(
            [
                {
                    "cell_id": f"calibration--r{replicate:02d}--{control}",
                    "scenario": "four-all",
                    "handlers": list(SCENARIOS["four-all"]),
                    "workers": 4,
                    "replicate": replicate,
                    "position": position,
                    "variant": f"calibration-{control}",
                    "rrc_enabled": True,
                    "contextmesh_enabled": True,
                    "rrc_control": control,
                }
                for position, control in enumerate(order)
            ]
        )
    rng.shuffle(pairs)
    return [cell for pair in pairs for cell in pair]


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        raise MatrixError("median has no observations")
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def select_rrc_control(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Apply the preregistered deterministic-versus-live calibration rule."""

    groups: dict[str, list[Mapping[str, Any]]] = {"live": [], "deterministic": []}
    for cell in cells:
        control = cell.get("rrc_control")
        if control in groups:
            groups[str(control)].append(cell)
    expected_replicates = set(range(1, CALIBRATION_REPLICATES + 1))
    basic_shape_valid = len(cells) == 2 * CALIBRATION_REPLICATES and all(
        len(groups[name]) == CALIBRATION_REPLICATES
        and {cell.get("replicate") for cell in groups[name]} == expected_replicates
        for name in groups
    )
    by_replicate: dict[int, dict[str, Mapping[str, Any]]] = {
        replicate: {} for replicate in expected_replicates
    }
    if basic_shape_valid:
        for name, rows in groups.items():
            for cell in rows:
                by_replicate[int(cell["replicate"])][name] = cell
    shape_valid = basic_shape_valid and all(
        set(pair) == set(groups) and {pair[name].get("position") for name in groups} == {0, 1}
        for pair in by_replicate.values()
    )
    complete_replicates = sorted(
        replicate
        for replicate, pair in by_replicate.items()
        if set(pair) == set(groups)
        and all(
            bool(cell.get("valid")) and isinstance(cell.get("semantic"), dict)
            for cell in pair.values()
        )
    )
    excluded_replicates = sorted(expected_replicates - set(complete_replicates))
    conclusive = shape_valid and len(complete_replicates) >= 3
    medians: dict[str, float | None] = {"live": None, "deterministic": None}
    high_recall: dict[str, float | None] = {"live": None, "deterministic": None}
    planner_tokens: dict[str, int | None] = {"live": None, "deterministic": None}
    position_strata: dict[str, dict[str, Any]] = {}

    def summarize(name: str, replicates: Sequence[int]) -> tuple[float, float, int] | None:
        rows = [by_replicate[replicate][name] for replicate in replicates]
        f1_values: list[float] = []
        high_tp = 0
        high_total = 0
        token_values: list[int] = []
        for cell in rows:
            semantic = cell.get("semantic")
            if not isinstance(semantic, dict):
                return None
            f1 = semantic.get("f1")
            tp = semantic.get("high_critical_tp")
            total = semantic.get("high_critical_total")
            if (
                not isinstance(f1, (int, float))
                or isinstance(f1, bool)
                or not math.isfinite(float(f1))
                or not 0 <= float(f1) <= 1
                or not isinstance(tp, int)
                or isinstance(tp, bool)
                or not isinstance(total, int)
                or isinstance(total, bool)
                or total <= 0
                or not 0 <= tp <= total
            ):
                return None
            components = cell.get("components")
            planner = components.get("planner") if isinstance(components, dict) else None
            tokens = planner.get("provider_visible_tokens") if isinstance(planner, dict) else None
            if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
                return None
            f1_values.append(float(f1))
            high_tp += tp
            high_total += total
            token_values.append(tokens)
        if not f1_values or high_total <= 0:
            return None
        return _median(f1_values), high_tp / high_total, sum(token_values)

    if conclusive:
        for name in groups:
            summary = summarize(name, complete_replicates)
            if summary is None:
                conclusive = False
                break
            medians[name], high_recall[name], planner_tokens[name] = summary
    if conclusive:
        for live_position, label in ((0, "live-first"), (1, "deterministic-first")):
            replicates = [
                replicate
                for replicate in complete_replicates
                if by_replicate[replicate]["live"].get("position") == live_position
            ]
            summaries = {name: summarize(name, replicates) for name in groups}
            if not replicates or any(value is None for value in summaries.values()):
                conclusive = False
                break
            live_summary = cast(tuple[float, float, int], summaries["live"])
            deterministic_summary = cast(tuple[float, float, int], summaries["deterministic"])
            position_strata[label] = {
                "replicates": replicates,
                "median_semantic_f1": {
                    "live": live_summary[0],
                    "deterministic": deterministic_summary[0],
                },
                "pooled_high_critical_recall": {
                    "live": live_summary[1],
                    "deterministic": deterministic_summary[1],
                },
                "planner_provider_visible_tokens": {
                    "live": live_summary[2],
                    "deterministic": deterministic_summary[2],
                },
            }
    position_checks_pass = bool(
        conclusive
        and len(position_strata) == 2
        and all(
            stratum["median_semantic_f1"]["deterministic"]
            >= stratum["median_semantic_f1"]["live"] - 0.05
            and stratum["pooled_high_critical_recall"]["deterministic"]
            >= stratum["pooled_high_critical_recall"]["live"]
            for stratum in position_strata.values()
        )
    )
    use_deterministic = bool(
        conclusive
        and position_checks_pass
        and medians["deterministic"] is not None
        and medians["live"] is not None
        and medians["deterministic"] >= medians["live"] - 0.05
        and high_recall["deterministic"] is not None
        and high_recall["live"] is not None
        and high_recall["deterministic"] >= high_recall["live"]
        and planner_tokens["deterministic"] == 0
    )
    return {
        "v": 1,
        "rule": "at least three complete randomized pairs; global and each launch-position stratum require deterministic median semantic F1 >= live-0.05 and deterministic pooled high/critical recall >= live; deterministic planner tokens == 0",
        "conclusive": conclusive,
        "selected": "deterministic" if use_deterministic else "live",
        "complete_pair_replicates": complete_replicates,
        "excluded_pair_replicates": excluded_replicates,
        "position_strata": position_strata,
        "position_checks_pass": position_checks_pass,
        "median_semantic_f1": medians,
        "pooled_high_critical_recall": high_recall,
        "planner_provider_visible_tokens": planner_tokens,
    }


def calibration_record(
    cells: Sequence[Mapping[str, Any]], judgments: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Seal selection plus complete accounting for every calibration attempt."""

    selection = select_rrc_control(cells)
    accounting_fields = (
        "input_tokens",
        "uncached_input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "provider_visible_tokens",
        "api_equivalent_dollars",
    )
    selection["cells"] = [
        {
            "cell_id": cell.get("cell_id"),
            "replicate": cell.get("replicate"),
            "position": cell.get("position"),
            "valid": cell.get("valid"),
            "errors": cell.get("errors", []),
            "rrc_control": cell.get("rrc_control"),
            "planner_tokens": cell.get("components", {})
            .get("planner", {})
            .get("provider_visible_tokens")
            if isinstance(cell.get("components"), dict)
            else None,
            "semantic": cell.get("semantic"),
            "accounting": {
                **{field: cell.get(field) for field in accounting_fields},
                "unquantified_consumption": bool(cell.get("unquantified_consumption")),
            },
        }
        for cell in cells
    ]
    selection["attempt_totals"] = _aggregate_visible_accounting(cells)
    overhead = [
        judgment["evaluation_overhead"]
        for judgment in judgments
        if isinstance(judgment.get("evaluation_overhead"), dict)
    ]
    selection["evaluation_overhead"] = _aggregate_visible_accounting(overhead)
    selection["judge_blocks_attempted"] = len(judgments)
    selection["judge_blocks_valid"] = sum(bool(row.get("valid")) for row in judgments)
    return selection


def api_equivalent_cost(
    *, model: str, input_tokens: int, cached_input_tokens: int, output_tokens: int
) -> float:
    """Return the frozen standard-API-equivalent price for one model turn."""

    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in (input_tokens, cached_input_tokens, output_tokens)
    ):
        raise MatrixError("token price inputs must be nonnegative integers")
    if cached_input_tokens > input_tokens:
        raise MatrixError("cached input tokens exceed input tokens")
    try:
        input_rate, cached_rate, output_rate = PRICE_TABLE[model]
    except KeyError as exc:
        raise MatrixError(f"no frozen price for model: {model}") from exc
    if model == "gpt-5.5" and input_tokens > 272_000:
        input_rate *= 2
        cached_rate *= 2
        output_rate *= 1.5
    uncached = input_tokens - cached_input_tokens
    return (
        uncached * input_rate + cached_input_tokens * cached_rate + output_tokens * output_rate
    ) / 1_000_000


def factor_effects(values: Mapping[str, float]) -> dict[str, float]:
    """Compute the preregistered 0/1-coded absolute 2x2 effects."""

    if set(values) != set(ABLATION_VARIANTS):
        raise MatrixError("factor block must contain exactly four variants")
    native = float(values["native"])
    rrc = float(values["rrc"])
    contextmesh = float(values["contextmesh"])
    combined = float(values["combined"])
    return {
        "rrc": 0.5 * ((rrc - native) + (combined - contextmesh)),
        "contextmesh": 0.5 * ((contextmesh - native) + (combined - rrc)),
        "interaction": combined - rrc - contextmesh + native,
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
Unless an immutable exact-source bundle explicitly says not to use tools, each worker must make exactly four shell calls, one per required file, with each complete command exactly `/usr/bin/nl -ba <path>`; no wildcard, script, or other tool is allowed.
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
    command.extend(
        [
            "-c",
            f"model={json.dumps(os.environ.get('RRD_CODEX_MODEL', 'gpt-5.5'))}",
            "-c",
            f"model_reasoning_effort={json.dumps(os.environ.get('RRD_CODEX_REASONING', 'medium'))}",
        ]
    )
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
    turn_usages: list[dict[str, int]] = []
    task_complete: list[int] = []
    effective_model: str | None = None
    effective_reasoning: str | None = None
    for index, row in enumerate(rows):
        payload = row.get("payload")
        if row.get("type") == "session_meta" and isinstance(payload, dict):
            candidate = payload.get("id") or payload.get("session_id")
            if isinstance(candidate, str) and candidate:
                thread_id = candidate
            candidate_parent = payload.get("parent_thread_id")
            parent = candidate_parent if isinstance(candidate_parent, str) else None
        if row.get("type") == "turn_context" and isinstance(payload, dict):
            model_value = payload.get("model")
            effort_value = payload.get("effort")
            if not isinstance(model_value, str) or not model_value:
                raise MatrixError("turn context model is malformed")
            if not isinstance(effort_value, str) or not effort_value:
                raise MatrixError("turn context reasoning is malformed")
            if effective_model is not None and model_value != effective_model:
                raise MatrixError("transcript effective model changed")
            if effective_reasoning is not None and effort_value != effective_reasoning:
                raise MatrixError("transcript effective reasoning changed")
            effective_model = model_value
            effective_reasoning = effort_value
        if (
            row.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "token_count"
        ):
            info = payload.get("info")
            candidate_usage = info.get("total_token_usage") if isinstance(info, dict) else None
            cumulative = _validate_usage(candidate_usage)
            previous = usage_rows[-1][1] if usage_rows else None
            if previous is not None and any(
                cumulative[field] < previous[field] for field in USAGE_FIELDS
            ):
                raise MatrixError("transcript cumulative usage regressed")
            usage_rows.append((index, cumulative))
            last_usage = info.get("last_token_usage") if isinstance(info, dict) else None
            if last_usage is not None:
                turn_usages.append(_validate_usage(last_usage))
            else:
                turn = {
                    field: cumulative[field] - (previous[field] if previous is not None else 0)
                    for field in USAGE_FIELDS
                }
                turn_usages.append(_validate_usage(turn))
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
    turn_totals = {field: sum(row[field] for row in turn_usages) for field in USAGE_FIELDS}
    if turn_totals != usage:
        raise MatrixError("transcript per-turn usage does not reconcile to cumulative usage")
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
        "turn_usages": turn_usages,
        "model": effective_model,
        "reasoning": effective_reasoning,
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
        if start <= 0 or end < start or SENTENCE_END_RE.search(text) is None:
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
        "lexical_f1": (
            2
            * (len(matches) / total if total else 0.0)
            * (len(matches) / len(claims) if claims else 0.0)
            / (
                (len(matches) / total if total else 0.0)
                + (len(matches) / len(claims) if claims else 0.0)
            )
            if matches and total and claims
            else 0.0
        ),
        "handler_coverage": handler_coverage,
        "category_coverage": categories == matched_categories,
        "unmatched_claims": unmatched,
        "unmatched_high_or_critical": any(
            claim["severity"] in {"high", "critical"} for claim in unmatched
        ),
        "semantic_review_required": semantic_review,
        "metric": "lexical-rubric-match",
    }


def score_semantic_candidate(
    *, label: str, report: str, scenario: str, judgment: Mapping[str, Any]
) -> dict[str, Any]:
    """Fail-closed deterministic scoring of one blind judge candidate mapping."""

    if set(judgment) != {"label", "claims"} or judgment.get("label") != label:
        raise MatrixError("semantic judgment candidate shape or label is invalid")
    quality = score_report(report, scenario=scenario)
    if not quality["valid"]:
        raise MatrixError("semantic candidate report grammar is invalid")
    claims = quality["claims"]
    rows = judgment.get("claims")
    if not isinstance(rows, list) or len(rows) != len(claims):
        raise MatrixError("semantic judgment claim indices do not cover the report")
    indices: list[int] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "index",
            "rubric_id",
            "supported",
            "rationale",
        }:
            raise MatrixError("semantic judgment claim row is malformed")
        index = row.get("index")
        rationale = row.get("rationale")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not isinstance(row.get("supported"), bool)
            or not isinstance(rationale, str)
            or len(rationale.encode("utf-8")) > 512
        ):
            raise MatrixError("semantic judgment claim row types are invalid")
        indices.append(index)
    if sorted(indices) != list(range(len(claims))) or len(indices) != len(set(indices)):
        raise MatrixError("semantic judgment claim indices are invalid")

    rubric = {
        str(finding["stable_id"]): finding
        for finding in _rubric()["findings"]
        if isinstance(finding, dict) and finding.get("handler") in SCENARIOS[scenario]
    }
    consumed: set[str] = set()
    tp = 0
    fp = 0
    high_critical_tp = 0
    high_critical_total = sum(
        finding.get("severity") in {"high", "critical"} for finding in rubric.values()
    )
    for row in sorted(rows, key=lambda value: value["index"]):
        claim = claims[row["index"]]
        rubric_id = row["rubric_id"]
        if rubric_id is not None and (not isinstance(rubric_id, str) or rubric_id not in rubric):
            raise MatrixError("semantic judgment contains an unknown rubric ID")
        finding = rubric.get(rubric_id) if isinstance(rubric_id, str) else None
        mechanical = False
        if finding is not None:
            start, end = _rubric_range(finding)
            mechanical = bool(
                claim["severity"] == finding.get("severity")
                and claim["handler"] == finding.get("handler")
                and not (claim["end"] < start or end < claim["start"])
            )
        accepted = bool(
            row["supported"] and rubric_id is not None and mechanical and rubric_id not in consumed
        )
        if accepted:
            consumed.add(str(rubric_id))
            tp += 1
            if finding is not None and finding.get("severity") in {"high", "critical"}:
                high_critical_tp += 1
        else:
            fp += 1
    fn = len(rubric) - tp
    precision = tp / (tp + fp) if tp + fp else (1.0 if not rubric else 0.0)
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "metric": "blind-model-adjudicated-rubric-match",
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "matched_rubric_ids": sorted(consumed),
        "high_critical_tp": high_critical_tp,
        "high_critical_total": high_critical_total,
        "high_critical_recall": (
            high_critical_tp / high_critical_total if high_critical_total else 1.0
        ),
    }


def semantic_judge_schema(labels: Sequence[str], max_claims: int) -> dict[str, Any]:
    """Strict Structured Outputs schema; mechanical correctness is scored separately."""

    claim_properties = {
        "index": {"type": "integer", "minimum": 0, "maximum": max(0, max_claims)},
        "rubric_id": {
            "anyOf": [
                {"type": "string", "enum": [row["stable_id"] for row in _rubric()["findings"]]},
                {"type": "null"},
            ]
        },
        "supported": {"type": "boolean"},
        "rationale": {"type": "string", "maxLength": 512},
    }
    candidate_properties = {
        "label": {"type": "string", "enum": list(labels)},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": claim_properties,
                "required": list(claim_properties),
                "additionalProperties": False,
            },
        },
    }
    return {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "minItems": len(labels),
                "maxItems": len(labels),
                "items": {
                    "type": "object",
                    "properties": candidate_properties,
                    "required": list(candidate_properties),
                    "additionalProperties": False,
                },
            }
        },
        "required": ["candidates"],
        "additionalProperties": False,
    }


def _judge_prompt(*, scenario: str, candidates: Sequence[tuple[str, str]]) -> str:
    allowed = set(SCENARIOS[scenario])
    sources: list[str] = []
    for relative in (
        *(f"src/handlers/{handler}.js" for handler in SCENARIOS[scenario]),
        "src/models.js",
        "src/utils.js",
        "src/middleware.js",
    ):
        raw = (TARGET_TEMPLATE / relative).read_text()
        sources.append(
            f"## SOURCE {relative} (JSON string; decode escapes for exact text)\n"
            + json.dumps(raw, ensure_ascii=True)
        )
    rubric = [
        row
        for row in _rubric()["findings"]
        if isinstance(row, dict) and row.get("handler") in allowed
    ]
    reports = "\n\n".join(f"## CANDIDATE {label}\n{report}" for label, report in candidates)
    return (
        """You are a blind security-audit adjudicator. The candidate labels carry no treatment meaning.
For every bullet in every candidate, in its original zero-based order, decide whether the entire
indivisible claim is supported by exactly one rubric finding. A conjoined or overstated claim is
supported only when every conjunct, qualifier, and causal statement is supported by that one
finding. Map it to that stable_id or null. Do not emit scores. Use only the supplied sources and
rubric. Return exactly the requested JSON schema and every candidate/claim exactly once.

"""
        + "\n\n".join(sources)
        + "\n\n## RUBRIC\n"
        + json.dumps(rubric, indent=2, sort_keys=True)
        + "\n\n"
        + reports
    )


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


def _denied_source_attempts(
    hooks: Sequence[Mapping[str, Any]],
) -> tuple[set[tuple[str, str]], bool]:
    """Bind every recorded reread attempt to the immediately following policy denial."""

    attempts: set[tuple[str, str]] = set()
    paired_policy_rows: set[int] = set()
    valid = True
    for index, row in enumerate(hooks):
        if row.get("event") != "source_reread_violation":
            continue
        agent_id = row.get("agent_id")
        tool_use_id = row.get("tool_use_id")
        key = (str(agent_id), str(tool_use_id))
        if (
            not isinstance(agent_id, str)
            or not agent_id
            or not isinstance(tool_use_id, str)
            or not tool_use_id
            or key in attempts
            or index + 1 >= len(hooks)
        ):
            valid = False
            continue
        denial = hooks[index + 1]
        if (
            denial.get("event") != "policy_deny"
            or denial.get("source") != "hook"
            or denial.get("error")
            != "worker source was already delivered; file and shell tools are denied"
        ):
            valid = False
            continue
        attempts.add(key)
        paired_policy_rows.add(index + 1)
    if any(
        row.get("event") == "policy_deny" and index not in paired_policy_rows
        for index, row in enumerate(hooks)
    ):
        valid = False
    if sum(row.get("event") == "source_reread_violation" for row in hooks) != len(attempts):
        valid = False
    return attempts, valid


def validate_factor_protocol(
    *,
    hooks: list[dict[str, Any]],
    packets: list[dict[str, Any]],
    handlers: Sequence[str],
    backend: str,
    planner_calls: int,
    rrc_enabled: bool,
    contextmesh_enabled: bool,
    deterministic: bool,
) -> dict[str, Any]:
    """Validate the exact RRC/ContextMesh 2x2 factor boundary."""

    reasons: list[str] = []
    expected_handlers = set(handlers)
    if not hooks or any(row.get("memory_backend") != backend for row in hooks):
        reasons.append("hook backend evidence mismatch")
    assignments = [row for row in hooks if row.get("event") == "assignment"]
    assigned_handlers = {
        str(row.get("handler", "")).removeprefix("src/handlers/").removesuffix(".js")
        for row in assignments
    }
    if len(assignments) != len(handlers) or assigned_handlers != expected_handlers:
        reasons.append("assignment handlers mismatch")
    if any(
        row.get("rrc_enabled") is not rrc_enabled
        or row.get("contextmesh_enabled") is not contextmesh_enabled
        for row in assignments
    ):
        reasons.append("assignment factor evidence mismatch")
    spawned = {
        str(row["agent_id"])
        for row in hooks
        if row.get("event") == "spawned" and isinstance(row.get("agent_id"), str)
    }
    finals = {
        str(row["agent_id"])
        for row in hooks
        if row.get("event") == "result_final" and isinstance(row.get("agent_id"), str)
    }
    if len(spawned) != len(handlers) or finals != spawned:
        reasons.append("worker lifecycle mismatch")
    waited: set[str] = set()
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
        waited.update(completed)
    if waited != spawned:
        reasons.append("wait coverage mismatch")
    if sum(row.get("event") == "root_merge" for row in hooks) != 1:
        reasons.append("root merge mismatch")
    denied_attempts, denial_valid = _denied_source_attempts(hooks)
    if any(
        row.get("event") in {"fail_open", "compress_fail_open", "native_usage_missing"}
        for row in hooks
    ) or (
        not denial_valid
        and any(row.get("event") in {"policy_deny", "source_reread_violation"} for row in hooks)
    ):
        reasons.append("factor failure event")
    if denied_attempts and not contextmesh_enabled:
        reasons.append("ContextMesh-disabled arm emitted a source denial")

    bundles = [row for row in hooks if row.get("event") == "source_bundle"]
    shared = {
        str(row["agent_id"])
        for row in hooks
        if row.get("event") == "shared_context" and isinstance(row.get("agent_id"), str)
    }
    compress_rows = [
        row for row in hooks if row.get("event") in {"compression_delivered", "compression_bypass"}
    ]
    if contextmesh_enabled:
        assignment_ids = [row.get("assignment_id") for row in assignments]
        bundle_ids = [row.get("assignment_id") for row in bundles]
        if (
            len(bundles) != len(handlers)
            or shared != spawned
            or not all(isinstance(value, str) and value for value in assignment_ids)
            or len(set(assignment_ids)) != len(assignment_ids)
            or set(bundle_ids) != set(assignment_ids)
        ):
            reasons.append("ContextMesh exact-source delivery mismatch")
        assignments_by_id = {
            str(row.get("assignment_id")): row
            for row in assignments
            if isinstance(row.get("assignment_id"), str)
        }
        for row in bundles:
            files = row.get("files")
            assignment = assignments_by_id.get(str(row.get("assignment_id")))
            handler_path = assignment.get("handler") if isinstance(assignment, dict) else None
            expected_paths = {handler_path, "src/models.js", "src/utils.js", "src/middleware.js"}
            file_paths = (
                [item.get("path") for item in files if isinstance(item, dict)]
                if isinstance(files, list)
                else []
            )
            by_path = (
                {
                    item.get("path"): item
                    for item in files
                    if isinstance(item, dict) and isinstance(item.get("path"), str)
                }
                if isinstance(files, list)
                else {}
            )
            if (
                not isinstance(handler_path, str)
                or not isinstance(files, list)
                or len(files) != 4
                or len(file_paths) != 4
                or not all(isinstance(path, str) for path in file_paths)
                or len(set(file_paths)) != 4
                or set(by_path) != expected_paths
            ):
                reasons.append("ContextMesh source bundle evidence malformed")
                continue
            if not isinstance(assignment, dict):
                reasons.append("ContextMesh source bundle assignment binding mismatch")
                continue
            if row.get("handler") != handler_path:
                reasons.append("ContextMesh source bundle assignment binding mismatch")
            for path, item in by_path.items():
                raw = _bounded_bytes(TARGET_TEMPLATE / str(path), limit=1_000_000)
                text = raw.decode("utf-8")
                if item != {
                    "path": path,
                    "raw_sha256": _sha(raw),
                    "byte_count": len(raw),
                    "line_count": len(text.splitlines()),
                    "final_newline": text.endswith("\n"),
                }:
                    reasons.append("ContextMesh source bundle metadata mismatch")
            handler_raw = _bounded_bytes(TARGET_TEMPLATE / handler_path, limit=1_000_000)
            if assignment.get("handler_sha256") != _sha(handler_raw) or assignment.get(
                "delivered_bytes"
            ) != len(handler_raw):
                reasons.append("ContextMesh assignment source metadata mismatch")
        delivered: set[str] = set()
        for row in compress_rows:
            receipts = row.get("receipts")
            agent_ids = row.get("agent_ids")
            if isinstance(receipts, dict):
                delivered.update(str(key) for key in receipts)
            if isinstance(agent_ids, list):
                delivered.update(str(item) for item in agent_ids)
        if delivered != spawned:
            reasons.append("ContextMesh compression coverage mismatch")
    elif bundles or shared or compress_rows:
        reasons.append("ContextMesh-disabled arm emitted intervention evidence")

    live_packets = [row for row in packets if row.get("event") == "packet"]
    control_packets = [
        row for row in hooks if row.get("event") == "packet" and row.get("branch") == "control"
    ]
    if rrc_enabled and deterministic:
        handlers_seen = {
            str(row.get("handler", "")).removeprefix("src/handlers/").removesuffix(".js")
            for row in control_packets
        }
        if len(control_packets) != len(handlers) or handlers_seen != expected_handlers:
            reasons.append("deterministic RRC packet mismatch")
        if live_packets or planner_calls != 0:
            reasons.append("deterministic RRC consumed planner")
    elif rrc_enabled:
        handlers_seen = {
            str(row.get("handler", "")).removeprefix("src/handlers/").removesuffix(".js")
            for row in live_packets
        }
        branches = [row.get("branch") for row in live_packets]
        if (
            len(live_packets) != len(handlers)
            or handlers_seen != expected_handlers
            or branches.count("miss") != 1
            or branches.count("hit") != len(handlers) - 1
            or planner_calls != 1
            or control_packets
        ):
            reasons.append("live RRC packet pattern mismatch")
    elif live_packets or control_packets or planner_calls:
        reasons.append("RRC-disabled arm emitted intervention evidence")
    return {
        "valid": not reasons,
        "errors": sorted(set(reasons)),
        "blocked_source_attempts": len(denied_attempts),
    }


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
    completed_spawns = 0
    waited_before_all_spawns = False
    for row in events:
        item = row.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type not in {"agent_message", "reasoning", "error", "collab_tool_call"}:
            reasons.append("root used a non-collaboration tool")
        if (
            item_type == "collab_tool_call"
            and item.get("tool") == "wait"
            and row.get("type") in {"item.started", "item.completed"}
            and completed_spawns < len(handlers)
        ):
            waited_before_all_spawns = True
        if row.get("type") != "item.completed":
            continue
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            final_messages.append(item["text"])
        if item.get("type") != "collab_tool_call" or item.get("status") != "completed":
            continue
        if item.get("tool") not in {"spawn_agent", "wait"}:
            reasons.append("root used an undeclared collaboration tool")
            continue
        receivers = item.get("receiver_thread_ids")
        receiver_ids = (
            {str(value) for value in receivers}
            if isinstance(receivers, list) and all(isinstance(value, str) for value in receivers)
            else set()
        )
        if item.get("tool") == "spawn_agent":
            spawns.append(item)
            completed_spawns += 1
        elif item.get("tool") == "wait":
            waited.update(receiver_ids)
    spawned_ids: set[str] = set()
    assigned_handlers: list[str] = []
    assignments: dict[str, str] = {}
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
            assignments[receivers[0]] = next(iter(mentioned))
    if len(spawns) != len(handlers) or set(assigned_handlers) != expected_handlers:
        reasons.append("spawn assignments mismatch")
    if spawned_ids != worker_ids:
        reasons.append("spawned worker identities mismatch transcripts")
    if waited_before_all_spawns:
        reasons.append("root waited before all workers were spawned")
    if not worker_ids.issubset(waited):
        reasons.append("root wait coverage mismatch")
    if not final.strip() or not final_messages or final_messages[-1].strip() != final.strip():
        reasons.append("root final message mismatch")
    if sum(row.get("type") == "turn.completed" for row in events) != 1:
        reasons.append("root turn completion mismatch")
    return {
        "valid": not reasons,
        "errors": sorted(set(reasons)),
        "assignments": assignments,
        "spawn_before_wait": not waited_before_all_spawns,
    }


def validate_worker_source_protocol(
    *,
    transcripts: Sequence[Mapping[str, Any]],
    assignments: Mapping[str, str],
    contextmesh_enabled: bool,
    hooks: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Enforce denied CM rereads and exact four-file direct reads otherwise."""

    reasons: list[str] = []
    denied_attempts, denial_valid = _denied_source_attempts(hooks)
    observed_denials: set[tuple[str, str]] = set()
    if contextmesh_enabled and not denial_valid:
        reasons.append("ContextMesh source denial evidence is malformed")
    for transcript in transcripts:
        thread_id = str(transcript.get("thread_id", ""))
        handler = assignments.get(thread_id)
        if handler is None:
            reasons.append("worker source protocol has no assignment binding")
            continue
        calls: list[tuple[str, str | None, str | None]] = []
        tool_items: list[tuple[str, str | None, str | None, str | None]] = []
        for row in _strict_jsonl(Path(str(transcript.get("path")))):
            payload = row.get("payload")
            if row.get("type") != "response_item" or not isinstance(payload, dict):
                continue
            if payload.get("type") == "function_call_output":
                call_id = payload.get("call_id")
                output = payload.get("output")
                tool_items.append(
                    (
                        "output",
                        call_id if isinstance(call_id, str) else None,
                        None,
                        output if isinstance(output, str) else None,
                    )
                )
                continue
            if payload.get("type") != "function_call":
                continue
            name = payload.get("name")
            call_id = payload.get("call_id")
            arguments = payload.get("arguments")
            command: str | None = None
            if isinstance(arguments, str):
                try:
                    decoded = json.loads(arguments)
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, dict) and isinstance(decoded.get("cmd"), str):
                    command = decoded["cmd"]
            calls.append((str(name), command, call_id if isinstance(call_id, str) else None))
            tool_items.append(
                (
                    "call",
                    call_id if isinstance(call_id, str) else None,
                    command,
                    str(name),
                )
            )
        expected_paths = {
            f"src/handlers/{handler}.js",
            "src/models.js",
            "src/utils.js",
            "src/middleware.js",
        }
        if contextmesh_enabled:
            if len(tool_items) != 2 * len(calls):
                reasons.append("ContextMesh worker tool evidence is not paired bijectively")
            seen_call_ids: set[str] = set()
            for index in range(0, len(tool_items), 2):
                if index + 1 >= len(tool_items):
                    reasons.append("ContextMesh worker tool evidence is not paired bijectively")
                    break
                call_item = tool_items[index]
                output_item = tool_items[index + 1]
                item_type, call_id, command, name = call_item
                output_type, output_call_id, _unused, output = output_item
                match = (
                    re.fullmatch(
                        r"/usr/bin/nl -ba (src/(?:handlers/[A-Za-z0-9_-]+\.js|models\.js|utils\.js|middleware\.js))",
                        command,
                    )
                    if name == "exec_command" and isinstance(command, str)
                    else None
                )
                key = (thread_id, call_id or "")
                expected_output = (
                    "Command blocked by PreToolUse hook: worker source was already delivered; "
                    f"file and shell tools are denied. Command: {command}"
                )
                if (
                    item_type != "call"
                    or output_type != "output"
                    or output_call_id != call_id
                    or match is None
                    or match.group(1) not in expected_paths
                    or not call_id
                    or call_id in seen_call_ids
                    or key not in denied_attempts
                    or output != expected_output
                ):
                    reasons.append("ContextMesh worker tool attempt was not a proven source denial")
                else:
                    seen_call_ids.add(call_id)
                    observed_denials.add(key)
            continue
        observed: list[str] = []
        for name, command, _call_id in calls:
            match = (
                re.fullmatch(
                    r"/usr/bin/nl -ba (src/(?:handlers/[A-Za-z0-9_-]+\.js|models\.js|utils\.js|middleware\.js))",
                    command,
                )
                if name == "exec_command" and isinstance(command, str)
                else None
            )
            if match is None:
                reasons.append("direct-read worker used a noncanonical or extra tool command")
            else:
                observed.append(match.group(1))
        if len(calls) != 4 or len(observed) != 4 or set(observed) != expected_paths:
            reasons.append("direct-read worker did not read exactly its four required sources")
    if contextmesh_enabled and observed_denials != denied_attempts:
        reasons.append("ContextMesh source denial transcript correlation mismatch")
    return {
        "valid": not reasons,
        "errors": sorted(set(reasons)),
        "blocked_source_attempts": len(observed_denials),
    }


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


def _prepare_factors(
    *, codex_bin: Path, rrc_enabled: bool, contextmesh_enabled: bool, deterministic: bool
) -> tuple[str, Path, dict[str, str]]:
    launcher_env = _launcher_environment("sqlite", codex_bin)
    tui = CM_ROOT / "scripts" / "rrd_demo_tui.sh"
    _run_checked([str(tui), "reset"], env=launcher_env)
    round_id = (RUNS / "rrd-demo" / "round-sqlite").read_text().strip()
    if contextmesh_enabled:
        _run_checked([str(tui), "seed"], env=launcher_env)
    arm = RUNS / "rrd-demo" / round_id / "b"
    values = {
        "RRD_CODEX_MODEL": os.environ.get("RRD_CODEX_MODEL", "gpt-5.5"),
        "RRD_CODEX_REASONING": os.environ.get("RRD_CODEX_REASONING", "medium"),
        "RRD_WORKER_MODEL": os.environ.get("RRD_WORKER_MODEL", "gpt-5.4-mini"),
        "RRD_WORKER_REASONING": os.environ.get("RRD_WORKER_REASONING", "low"),
        "RRD_REPO_ROOT": str(REPO),
        "RRD_MEMORY_BACKEND": "sqlite",
        "RRD_SUMMARY_MODE": "deterministic",
        "RRD_TARGET_ROOT": str(arm / "target"),
        "RRD_SEED_MANIFEST": str(arm / "seed-manifest.json"),
        "RRD_HOOK_EVENTS": str(arm / "hook-events.jsonl"),
        "RRD_RAW_RESULTS": str(arm / "raw-results"),
        "RRD_ENABLE_RRC": "1" if rrc_enabled else "0",
        "RRD_ENABLE_CONTEXTMESH": "1" if contextmesh_enabled else "0",
        "RRC_PLANNER_CODEX_HOME": str(NATIVE_HOME),
        "RRC_DEMO_UV_BIN": launcher_env["RRC_DEMO_UV_BIN"],
        "RRC_DEMO_ROUND": round_id,
        "RRC_DEMO_MODE": "warm",
        "RRC_DEMO_DATABASE": str(arm / "plan-spec.sqlite"),
        "RRC_DEMO_LOCK": str(arm / "plan-spec.lock"),
        "RRC_DEMO_EVENTS": str(arm / "rrc-events.jsonl"),
        "RRC_DEMO_MODEL_EVENTS": str(arm / "rrc-model-events.jsonl"),
        "RRC_STRONG_MODEL": launcher_env["RRD_CODEX_MODEL"],
        "RRC_REQUIRE_EFFECTIVE_MODEL": "1",
        "RRC_PLANNER_TIMEOUT": "90",
        "RRC_LOCK_TIMEOUT": "120",
        "RRC_VISIBILITY_TIMEOUT": "10",
        "RRC_BRIDGE_TIMEOUT": "240",
        "RRD_EXTERNAL_SANDBOX": launcher_env["RRD_EXTERNAL_SANDBOX"],
    }
    if deterministic:
        values["RRC_CONTROL"] = "deterministic"
    return round_id, arm, values


def _optional_jsonl(path: Path) -> list[dict[str, Any]]:
    return _strict_jsonl(path) if path.exists() else []


def _transcript_paths() -> set[Path]:
    sessions = NATIVE_HOME / "sessions"
    return set(sessions.rglob("*.jsonl")) if sessions.exists() else set()


def _root_id(events: Path) -> str:
    for row in _strict_jsonl(events):
        value = row.get("thread_id")
        if row.get("type") == "thread.started" and isinstance(value, str) and value:
            return value
    raise MatrixError("root JSON stream has no thread.started identity")


def _planner_rows(
    path: Path, *, require_effective: bool = False
) -> tuple[list[dict[str, Any]], set[str], list[dict[str, int]]]:
    rows = _strict_jsonl(path)
    identifiers: set[str] = set()
    usages: list[dict[str, int]] = []
    for row in rows:
        if row.get("parse_status") != "ok" or row.get("exit_code") != 0:
            raise MatrixError("planner model event is not successful")
        stdout = row.get("stdout")
        if not isinstance(stdout, str):
            raise MatrixError("planner model event has no stdout")
        if require_effective and (
            row.get("effective_model") != os.environ.get("RRD_CODEX_MODEL", "gpt-5.5")
            or row.get("effective_reasoning") != os.environ.get("RRD_CODEX_REASONING", "medium")
            or not isinstance(row.get("transcript_sha256"), str)
        ):
            raise MatrixError("planner effective model evidence mismatch")
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


def _component_metrics(
    transcripts: Sequence[Mapping[str, Any]], *, model: str, reasoning: str
) -> dict[str, Any]:
    usage_rows: list[Mapping[str, int]] = []
    cost = 0.0
    turn_count = 0
    for transcript in transcripts:
        if transcript.get("model") != model or transcript.get("reasoning") != reasoning:
            raise MatrixError(
                f"effective model/reasoning mismatch: expected {model}/{reasoning}, "
                f"got {transcript.get('model')}/{transcript.get('reasoning')}"
            )
        usage = transcript.get("usage")
        turns = transcript.get("turn_usages")
        if not isinstance(usage, dict) or not isinstance(turns, list) or not turns:
            raise MatrixError("component transcript usage evidence is missing")
        usage_rows.append(usage)
        for turn in turns:
            if not isinstance(turn, dict):
                raise MatrixError("component turn usage is malformed")
            cost += api_equivalent_cost(
                model=model,
                input_tokens=turn["input_tokens"],
                cached_input_tokens=turn["cached_input_tokens"],
                output_tokens=turn["output_tokens"],
            )
            turn_count += 1
    input_tokens = sum(row["input_tokens"] for row in usage_rows)
    cached = sum(row["cached_input_tokens"] for row in usage_rows)
    output = sum(row["output_tokens"] for row in usage_rows)
    reasoning_output = sum(row["reasoning_output_tokens"] for row in usage_rows)
    return {
        "model": model,
        "reasoning": reasoning,
        "sessions": len(transcripts),
        "turns": turn_count,
        "input_tokens": input_tokens,
        "uncached_input_tokens": input_tokens - cached,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning_output,
        "provider_visible_tokens": input_tokens + output,
        "api_equivalent_dollars": cost,
    }


def recover_attempt_usage(paths: Iterable[Path]) -> dict[str, Any]:
    """Best-effort accounting for an invalid/interrupted immutable attempt."""

    recovered: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in sorted(paths):
        try:
            text = _bounded_bytes(path).decode("utf-8")
        except (OSError, UnicodeError, MatrixError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        model: str | None = None
        reasoning: str | None = None
        cumulative: dict[str, int] | None = None
        turns: list[dict[str, int]] = []
        for number, line in enumerate(text.splitlines(), 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"{path.name}:{number}: malformed JSON")
                continue
            if not isinstance(row, dict):
                continue
            payload = row.get("payload")
            if row.get("type") == "turn_context" and isinstance(payload, dict):
                if isinstance(payload.get("model"), str):
                    model = payload["model"]
                if isinstance(payload.get("effort"), str):
                    reasoning = payload["effort"]
            if (
                row.get("type") == "event_msg"
                and isinstance(payload, dict)
                and payload.get("type") == "token_count"
            ):
                info = payload.get("info")
                raw_total = info.get("total_token_usage") if isinstance(info, dict) else None
                raw_last = info.get("last_token_usage") if isinstance(info, dict) else None
                try:
                    candidate = _validate_usage(raw_total)
                    if cumulative is None or all(
                        candidate[field] >= cumulative[field] for field in USAGE_FIELDS
                    ):
                        cumulative = candidate
                    if raw_last is not None:
                        turns.append(_validate_usage(raw_last))
                except MatrixError as exc:
                    errors.append(f"{path.name}:{number}: {exc}")
        if cumulative is None:
            continue
        cost: float | None = None
        if model in PRICE_TABLE:
            priced = turns or [cumulative]
            cost = sum(
                api_equivalent_cost(
                    model=model,
                    input_tokens=turn["input_tokens"],
                    cached_input_tokens=turn["cached_input_tokens"],
                    output_tokens=turn["output_tokens"],
                )
                for turn in priced
            )
        recovered.append(
            {
                "path": str(path),
                "model": model,
                "reasoning": reasoning,
                "usage": cumulative,
                "api_equivalent_dollars": cost,
            }
        )
    if not recovered:
        return {
            "provider_visible_tokens": None,
            "api_equivalent_dollars": None,
            "unquantified_consumption": True,
            "recovered_transcripts": [],
            "recovery_errors": errors,
        }
    usage_rows = [row["usage"] for row in recovered]
    input_tokens = sum(row["input_tokens"] for row in usage_rows)
    cached = sum(row["cached_input_tokens"] for row in usage_rows)
    output = sum(row["output_tokens"] for row in usage_rows)
    known_costs = [row["api_equivalent_dollars"] for row in recovered]
    cost = (
        sum(float(value) for value in known_costs)
        if all(isinstance(value, (int, float)) for value in known_costs)
        else None
    )
    return {
        "input_tokens": input_tokens,
        "uncached_input_tokens": input_tokens - cached,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": sum(row["reasoning_output_tokens"] for row in usage_rows),
        "provider_visible_tokens": input_tokens + output,
        "api_equivalent_dollars": cost,
        "unquantified_consumption": False,
        "recovered_transcripts": recovered,
        "recovery_errors": errors,
    }


def _recover_and_snapshot(
    cell: Path, paths: Iterable[Path], *, expected_sessions: int
) -> dict[str, Any]:
    candidates = sorted(set(paths))
    result = recover_attempt_usage(candidates)
    try:
        result["recovery_artifacts"] = snapshot_artifacts(
            [(path.name, path) for path in candidates],
            destination=cell / "recovery-transcripts",
        )
    except Exception as exc:
        result.setdefault("recovery_errors", []).append(
            f"artifact snapshot: {type(exc).__name__}: {exc}"
        )
    recovered_count = len(result.get("recovered_transcripts", []))
    result["expected_sessions"] = expected_sessions
    result["recovered_session_count"] = recovered_count
    result["recovery_complete"] = recovered_count == expected_sessions
    if recovered_count != expected_sessions:
        result["unquantified_consumption"] = True
    return result


def _sum_component_metrics(components: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fields = (
        "input_tokens",
        "uncached_input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "provider_visible_tokens",
        "api_equivalent_dollars",
    )
    return {field: sum(row[field] for row in components) for field in fields}


def _aggregate_visible_accounting(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fields = (
        "input_tokens",
        "uncached_input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "provider_visible_tokens",
        "api_equivalent_dollars",
    )
    result: dict[str, Any] = {
        field: sum(
            float(row[field]) if field == "api_equivalent_dollars" else int(row[field])
            for row in rows
            if isinstance(row.get(field), (int, float)) and not isinstance(row.get(field), bool)
        )
        for field in fields
    }
    result["blocks"] = len(rows)
    result["unquantified_blocks"] = sum(
        bool(row.get("unquantified_consumption"))
        or any(not isinstance(row.get(field), (int, float)) for field in fields)
        for row in rows
    )
    result["complete"] = result["unquantified_blocks"] == 0
    return result


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
        recovered = _recover_and_snapshot(
            cell,
            _transcript_paths() - before_transcripts,
            expected_sessions=1 + len(handlers) + (0 if baseline else 1),
        )
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
            **recovered,
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


def run_factor_cell(
    run_dir: Path,
    cell_spec: Mapping[str, Any],
    codex_bin: Path,
    *,
    deterministic: bool,
) -> dict[str, Any]:
    """Execute one immutable physical attempt in the hierarchical 2x2 experiment."""

    cell_id = str(cell_spec["cell_id"])
    scenario = str(cell_spec["scenario"])
    variant = str(cell_spec["variant"])
    if variant not in ABLATION_VARIANTS and not variant.startswith("calibration-"):
        raise MatrixError(f"unknown ablation variant: {variant}")
    handlers = SCENARIOS[scenario]
    rrc_enabled = bool(cell_spec["rrc_enabled"])
    contextmesh_enabled = bool(cell_spec["contextmesh_enabled"])
    control_name = "none" if not rrc_enabled else ("deterministic" if deterministic else "live")
    cell = run_dir / "cells" / cell_id
    if cell.exists():
        raise MatrixError(f"physical attempt already exists: {cell_id}")
    cell.mkdir(parents=True, mode=0o700)
    before_transcripts = _transcript_paths()
    attempt_id = _sha(f"{run_dir}\0{cell_id}\0{time.time_ns()}\0{os.getpid()}")[:24]
    _atomic_json(
        cell / "attempt.json",
        {
            "v": 1,
            "attempt_id": attempt_id,
            "cell_id": cell_id,
            "created_at": _now(),
            "preexisting_transcripts": sorted(str(path) for path in before_transcripts),
        },
    )
    prompt = render_prompt(scenario)
    _write_text(cell / "prompt.txt", prompt)
    errors: list[str] = []
    round_id: str | None = None
    arm: Path | None = None
    stack_started = False
    target = cell / "target"
    source_before = _source_hashes()
    combined_values: dict[str, str] | None = None
    exit_code = -1
    timed_out = False
    wall = 0.0
    summary: dict[str, Any]
    try:
        if variant == "native":
            _make_target(target)
        else:
            _stack("sqlite", codex_bin, "up")
            stack_started = True
            round_id, arm, combined_values = _prepare_factors(
                codex_bin=codex_bin,
                rrc_enabled=rrc_enabled,
                contextmesh_enabled=contextmesh_enabled,
                deterministic=deterministic,
            )
            target = arm / "target"
        _reset_native_runtime_state()
        final = (cell / "final.md").resolve()
        profile = cell / "execution.sb"
        _write_text(
            profile,
            execution_profile_text(
                cell=cell, target=target, run_dir=run_dir, baseline=variant == "native"
            ),
        )
        target_before = {
            str(path.relative_to(target)): _sha(path.read_bytes())
            for path in sorted((target / "src").rglob("*.js"))
        }
        command = codex_command(
            codex_bin=codex_bin,
            prompt=prompt,
            final_path=final,
            baseline=variant == "native",
            profile_path=profile,
        )
        env = codex_environment(
            codex_home=NATIVE_HOME, codex_bin=codex_bin, combined=combined_values
        )
        _atomic_json(
            cell / "resolved.json",
            {
                **dict(cell_spec),
                "attempt_id": attempt_id,
                "round_id": round_id,
                "rrc_control": control_name,
                "root_model": os.environ.get("RRD_CODEX_MODEL", "gpt-5.5"),
                "root_reasoning": os.environ.get("RRD_CODEX_REASONING", "medium"),
                "worker_model": os.environ.get("RRD_WORKER_MODEL", "gpt-5.4-mini"),
                "worker_reasoning": os.environ.get("RRD_WORKER_REASONING", "low"),
                "prompt_sha256": _sha(prompt),
                "command_sha256": _sha(json.dumps(command, separators=(",", ":"))),
                "execution_profile_sha256": _sha(profile.read_bytes()),
                "environment_names": sorted(env),
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
        hooks: list[dict[str, Any]] = []
        packets: list[dict[str, Any]] = []
        planner_ids: set[str] = set()
        planner_calls = 0
        if arm is not None:
            hooks = _optional_jsonl(arm / "hook-events.jsonl")
            packets = _optional_jsonl(arm / "rrc-events.jsonl")
            model_path = arm / "rrc-model-events.jsonl"
            if model_path.exists():
                model_rows, planner_ids, _unused = _planner_rows(
                    model_path, require_effective=rrc_enabled and not deterministic
                )
                planner_calls = len(model_rows)
            protocol = validate_factor_protocol(
                hooks=hooks,
                packets=packets,
                handlers=handlers,
                backend="sqlite",
                planner_calls=planner_calls,
                rrc_enabled=rrc_enabled,
                contextmesh_enabled=contextmesh_enabled,
                deterministic=deterministic,
            )
        else:
            protocol = {"valid": True, "errors": []}
        attribution = attribute_transcripts(
            new_transcripts,
            root_id=_root_id(cell / "root-events.jsonl"),
            expected_workers=len(handlers),
            planner_ids=planner_ids,
        )
        root_component = _component_metrics(
            [attribution["root_transcript"]],
            model=os.environ.get("RRD_CODEX_MODEL", "gpt-5.5"),
            reasoning=os.environ.get("RRD_CODEX_REASONING", "medium"),
        )
        worker_component = _component_metrics(
            attribution["worker_transcripts"],
            model=os.environ.get("RRD_WORKER_MODEL", "gpt-5.4-mini"),
            reasoning=os.environ.get("RRD_WORKER_REASONING", "low"),
        )
        planner_component = _component_metrics(
            attribution["planner_transcripts"],
            model=os.environ.get("RRD_CODEX_MODEL", "gpt-5.5"),
            reasoning=os.environ.get("RRD_CODEX_REASONING", "medium"),
        )
        totals = _sum_component_metrics([root_component, worker_component, planner_component])
        report = final.read_text() if final.exists() else ""
        root_events = _strict_jsonl(cell / "root-events.jsonl")
        worker_ids = {str(row["thread_id"]) for row in attribution["worker_transcripts"]}
        root_protocol = validate_root_protocol(
            events=root_events, handlers=handlers, worker_ids=worker_ids, final=report
        )
        worker_source_protocol = validate_worker_source_protocol(
            transcripts=attribution["worker_transcripts"],
            assignments=root_protocol.get("assignments", {}),
            contextmesh_enabled=contextmesh_enabled,
            hooks=hooks,
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
        if exit_code != 0:
            errors.append(f"Codex exited {exit_code}")
        if timed_out:
            errors.append("cell wall timeout")
        if not protocol["valid"]:
            errors.extend(protocol["errors"])
        if not root_protocol["valid"]:
            errors.extend(root_protocol["errors"])
        if not worker_source_protocol["valid"]:
            errors.extend(worker_source_protocol["errors"])
        if not quality["valid"]:
            errors.extend(quality["errors"])
        if not source_unchanged:
            errors.append("source drift during cell")
        if not target_unchanged or not target_clean:
            errors.append("target drift during cell")
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
        evidence_sources = [
            ("root-events.jsonl", cell / "root-events.jsonl"),
            ("codex.stderr.log", cell / "codex.stderr.log"),
            ("execution.sb", profile),
            ("resolved.json", cell / "resolved.json"),
            ("attempt.json", cell / "attempt.json"),
            ("final.md", final),
        ]
        if arm is not None:
            for name, path in (
                ("hook-events.jsonl", arm / "hook-events.jsonl"),
                ("rrc-events.jsonl", arm / "rrc-events.jsonl"),
                ("rrc-model-events.jsonl", arm / "rrc-model-events.jsonl"),
                ("seed-manifest.json", arm / "seed-manifest.json"),
                ("round-meta.json", arm.parent / "round-meta.json"),
            ):
                if path.exists():
                    evidence_sources.append((name, path))
        evidence_inventory = snapshot_artifacts(evidence_sources, destination=cell / "evidence")
        summary = {
            "v": 2,
            **dict(cell_spec),
            "attempt_id": attempt_id,
            "round_id": round_id,
            "rrc_control": control_name,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "wall_seconds": wall,
            "valid": not errors,
            "errors": sorted(set(errors)),
            **totals,
            "components": {
                "root": root_component,
                "worker": worker_component,
                "planner": planner_component,
            },
            "billing_exact": False,
            "cost_kind": "standard-api-equivalent",
            "workers_overlap": attribution["workers_overlap"],
            "blocked_source_attempts": worker_source_protocol["blocked_source_attempts"],
            "protocol": protocol,
            "root_protocol": root_protocol,
            "worker_source_protocol": worker_source_protocol,
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
        recovered = _recover_and_snapshot(
            cell,
            _transcript_paths() - before_transcripts,
            expected_sessions=1 + len(handlers) + (1 if rrc_enabled and not deterministic else 0),
        )
        summary = {
            "v": 2,
            **dict(cell_spec),
            "attempt_id": attempt_id,
            "round_id": round_id,
            "rrc_control": control_name,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "wall_seconds": wall,
            "valid": False,
            "errors": sorted(set(errors)),
            **recovered,
            "billing_exact": False,
            "completed_at": _now(),
        }
    finally:
        if stack_started:
            try:
                _stack("sqlite", codex_bin, "down")
            except Exception as exc:
                summary["valid"] = False
                summary.setdefault("errors", []).append(f"teardown: {type(exc).__name__}: {exc}")
    _write_cell_summary(cell, summary)
    return summary


def run_judge_block(
    run_dir: Path,
    *,
    block_id: str,
    scenario: str,
    cells: Sequence[Mapping[str, Any]],
    codex_bin: Path,
    seed: int,
) -> dict[str, Any]:
    """Blindly adjudicate a fixed block and keep evaluator usage outside product totals."""

    block = run_dir / "judgments" / block_id
    if block.exists():
        raise MatrixError(f"judgment attempt already exists: {block_id}")
    block.mkdir(parents=True, mode=0o700)
    preexisting_transcripts = _transcript_paths()
    _atomic_json(
        block / "attempt.json",
        {
            "v": 1,
            "block_id": block_id,
            "created_at": _now(),
            "preexisting_transcripts": sorted(str(path) for path in preexisting_transcripts),
        },
    )
    reports: list[tuple[str, str, str]] = []
    for cell in cells:
        cell_id = cell.get("cell_id")
        if not isinstance(cell_id, str) or not cell.get("valid"):
            raise MatrixError(f"cannot judge invalid cell in {block_id}")
        report_path = run_dir / "cells" / cell_id / "final.md"
        reports.append((cell_id, "", _bounded_bytes(report_path).decode("utf-8")))
    label_seed = randomization_seeds(seed)["judge_labels"]
    rng = random.Random(label_seed ^ int(_sha(block_id)[:16], 16))
    labels = [
        f"candidate-{_sha(f'{label_seed}:{block_id}:{index}')[:12]}"
        for index in range(len(reports))
    ]
    rng.shuffle(labels)
    labeled = [
        (cell_id, labels[index], report) for index, (cell_id, _old, report) in enumerate(reports)
    ]
    rng.shuffle(labeled)
    label_map = {label: cell_id for cell_id, label, _report in labeled}
    prompt = _judge_prompt(
        scenario=scenario,
        candidates=[(label, report) for _cell, label, report in labeled],
    )
    _write_text(block / "prompt.txt", prompt)
    max_claims = max(
        (
            score_report(report, scenario=scenario)["claim_count"]
            for _cell, _label, report in labeled
        ),
        default=0,
    )
    _atomic_json(block / "schema.json", semantic_judge_schema(list(label_map), max_claims))
    final = block / "judgment.json"
    profile = block / "execution.sb"
    profile_lines = [
        _bounded_bytes(NATIVE_HOME / "credential-deny.sb", limit=100_000).decode("utf-8").rstrip(),
        _seatbelt_rule("file-read*", "subpath", run_dir / "cells"),
        _seatbelt_rule("file-read*", "literal", run_dir / "experiment.json"),
        _seatbelt_rule("file-read*", "literal", run_dir / "calibration-selection.json"),
        _seatbelt_rule("file-read*", "literal", run_dir / "hierarchy-canary.json"),
    ]
    judgments = run_dir / "judgments"
    if judgments.exists():
        profile_lines.extend(
            _seatbelt_rule("file-read*", "subpath", sibling)
            for sibling in sorted(judgments.iterdir())
            if sibling.resolve() != block.resolve()
        )
    _write_text(profile, "\n".join(profile_lines) + "\n")
    command = (
        ["/usr/bin/sandbox-exec", "-f", str(profile), str(codex_bin)]
        if sys.platform == "darwin"
        else [str(codex_bin)]
    )
    command.extend(["--strict-config", "--dangerously-bypass-hook-trust"])
    command.extend(
        [
            "-c",
            f"model={json.dumps(FROZEN_ROLE_CONFIG['RRD_CODEX_MODEL'])}",
            "-c",
            f"model_reasoning_effort={json.dumps(FROZEN_ROLE_CONFIG['RRD_CODEX_REASONING'])}",
        ]
    )
    if sys.platform == "darwin":
        command.append("--dangerously-bypass-approvals-and-sandbox")
    command.extend(
        [
            "-c",
            "features.hooks=false",
            "-c",
            "features.multi_agent=false",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--output-schema",
            str((block / "schema.json").resolve()),
            "--output-last-message",
            str(final.resolve()),
            prompt,
        ]
    )
    _reset_native_runtime_state()
    before = _transcript_paths()
    _atomic_json(block / "launch.json", {"v": 1, "block_id": block_id, "started_at": _now()})
    exit_code, timed_out, wall = _run_codex_process(
        command=command,
        env=codex_environment(codex_home=NATIVE_HOME, codex_bin=codex_bin, combined=None),
        cwd=block,
        cell=block,
    )
    time.sleep(0.2)
    _atomic_json(block / "labels.json", label_map)
    errors: list[str] = []
    metrics: dict[str, Any] = {}
    evaluator: dict[str, Any] | None = None
    transcript_inventory: list[dict[str, Any]] = []
    try:
        if exit_code != 0 or timed_out:
            raise MatrixError(f"judge failed: exit={exit_code}, timeout={timed_out}")
        root_id = _root_id(block / "root-events.jsonl")
        root_events = _strict_jsonl(block / "root-events.jsonl")
        allowed_item_types = {"agent_message", "reasoning", "error"}
        if any(
            isinstance(row.get("item"), dict) and row["item"].get("type") not in allowed_item_types
            for row in root_events
        ):
            raise MatrixError("judge used a tool or emitted an unexpected item")
        attribution = attribute_transcripts(
            _transcript_paths() - before,
            root_id=root_id,
            expected_workers=0,
            planner_ids=set(),
        )
        evaluator = _component_metrics(
            [attribution["root_transcript"]],
            model=os.environ.get("RRD_CODEX_MODEL", "gpt-5.5"),
            reasoning=os.environ.get("RRD_CODEX_REASONING", "medium"),
        )
        transcript_inventory = snapshot_artifacts(
            [
                (
                    f"{attribution['root_transcript']['thread_id']}.jsonl",
                    Path(str(attribution["root_transcript"]["path"])),
                )
            ],
            destination=block / "transcripts",
        )
        value = json.loads(_bounded_bytes(final, limit=2_000_000).decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"candidates"}:
            raise MatrixError("judge top-level output is malformed")
        candidates = value["candidates"]
        if not isinstance(candidates, list):
            raise MatrixError("judge candidates are malformed")
        by_label: dict[str, Mapping[str, Any]] = {}
        for candidate in candidates:
            if not isinstance(candidate, dict) or not isinstance(candidate.get("label"), str):
                raise MatrixError("judge candidate row is malformed")
            label = candidate["label"]
            if label in by_label:
                raise MatrixError("judge contains duplicate candidate label")
            by_label[label] = candidate
        if set(by_label) != set(label_map):
            raise MatrixError("judge candidate labels do not match the blind block")
        reports_by_cell = {cell_id: report for cell_id, _label, report in labeled}
        for label, cell_id in label_map.items():
            metrics[cell_id] = score_semantic_candidate(
                label=label,
                report=reports_by_cell[cell_id],
                scenario=scenario,
                judgment=by_label[label],
            )
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        if evaluator is None:
            evaluator = _recover_and_snapshot(
                block, _transcript_paths() - before, expected_sessions=1
            )
    evidence_sources = [
        ("attempt.json", block / "attempt.json"),
        ("launch.json", block / "launch.json"),
        ("root-events.jsonl", block / "root-events.jsonl"),
        ("codex.stderr.log", block / "codex.stderr.log"),
        ("prompt.txt", block / "prompt.txt"),
        ("schema.json", block / "schema.json"),
        ("labels.json", block / "labels.json"),
    ]
    if final.exists():
        evidence_sources.append(("judgment.json", final))
    evidence_inventory = snapshot_artifacts(evidence_sources, destination=block / "evidence")
    result = {
        "v": 1,
        "block_id": block_id,
        "scenario": scenario,
        "cell_ids": sorted(label_map.values()),
        "valid": not errors,
        "errors": errors,
        "metrics": metrics,
        "evaluation_overhead": evaluator,
        "artifacts": {
            "transcripts": transcript_inventory,
            "evidence": evidence_inventory,
        },
        "exit_code": exit_code,
        "timed_out": timed_out,
        "wall_seconds": wall,
        "completed_at": _now(),
    }
    _atomic_json(block / "summary.json", result)
    _write_text(block / "summary.sha256", _sha(_bounded_bytes(block / "summary.json")) + "\n")
    return result


CORE_ABLATION_SCALARS = (
    "provider_visible_tokens",
    "uncached_input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "api_equivalent_dollars",
    "wall_seconds",
    "blocked_source_attempts",
    "lexical_precision",
    "lexical_recall",
    "lexical_f1",
    "semantic_precision",
    "semantic_recall",
    "semantic_f1",
)
COMPONENT_ABLATION_FIELDS = (
    "provider_visible_tokens",
    "uncached_input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "api_equivalent_dollars",
)
ABLATION_SCALARS = (
    *CORE_ABLATION_SCALARS,
    *tuple(
        f"{role}_{field}"
        for role in ("root", "worker", "planner")
        for field in COMPONENT_ABLATION_FIELDS
    ),
)


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "median": None, "min": None, "max": None, "mean": None, "sd": None}
    return {
        "n": len(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "mean": statistics.mean(values),
        "sd": statistics.stdev(values) if len(values) > 1 else None,
    }


def aggregate_ablation(
    run_dir: Path,
    *,
    cells: Sequence[Mapping[str, Any]],
    judgment_blocks: Sequence[Mapping[str, Any]],
    calibration: Mapping[str, Any],
) -> dict[str, Any]:
    """Compute sealed-cell distributions and complete-block 2x2 estimands."""

    semantic_by_cell: dict[str, Mapping[str, Any]] = {}
    evaluation_overhead: list[Mapping[str, Any]] = []
    for block in judgment_blocks:
        if block.get("valid") and isinstance(block.get("metrics"), dict):
            semantic_by_cell.update(block["metrics"])
        overhead = block.get("evaluation_overhead")
        if isinstance(overhead, dict):
            evaluation_overhead.append(overhead)
    enriched: list[dict[str, Any]] = []
    for raw in cells:
        cell = dict(raw)
        cell_id = str(cell.get("cell_id"))
        semantic = semantic_by_cell.get(cell_id)
        cell["semantic"] = dict(semantic) if isinstance(semantic, Mapping) else None
        quality = cell.get("quality")
        for metric in ("precision", "recall", "f1"):
            cell[f"lexical_{metric}"] = (
                quality.get(f"lexical_{metric}") if isinstance(quality, dict) else None
            )
            cell[f"semantic_{metric}"] = (
                semantic.get(metric) if isinstance(semantic, Mapping) else None
            )
        components = cell.get("components")
        for role in ("root", "worker", "planner"):
            component = components.get(role) if isinstance(components, dict) else None
            for field in COMPONENT_ABLATION_FIELDS:
                cell[f"{role}_{field}"] = (
                    component.get(field) if isinstance(component, dict) else None
                )
        cell["analysis_valid"] = bool(cell.get("valid") and semantic is not None)
        enriched.append(cell)
    distributions: dict[str, Any] = {}
    for scenario in SCENARIOS:
        distributions[scenario] = {}
        for variant in ABLATION_VARIANTS:
            rows = [
                cell
                for cell in enriched
                if cell.get("scenario") == scenario
                and cell.get("variant") == variant
                and cell.get("analysis_valid")
            ]
            distributions[scenario][variant] = {
                field: _distribution(
                    [
                        float(cell[field])
                        for cell in rows
                        if isinstance(cell.get(field), (int, float))
                    ]
                )
                for field in ABLATION_SCALARS
            }
    effects: dict[str, Any] = {}
    for scenario in SCENARIOS:
        block_effects: list[dict[str, Any]] = []
        for replicate in range(1, ABLATION_REPLICATES + 1):
            rows = [
                cell
                for cell in enriched
                if cell.get("scenario") == scenario and cell.get("replicate") == replicate
            ]
            by_variant = {str(cell.get("variant")): cell for cell in rows}
            if set(by_variant) != set(ABLATION_VARIANTS) or not all(
                bool(cell.get("analysis_valid")) for cell in by_variant.values()
            ):
                continue
            scalar_effects: dict[str, Any] = {}
            for field in ABLATION_SCALARS:
                values = {variant: by_variant[variant].get(field) for variant in ABLATION_VARIANTS}
                if not all(isinstance(value, (int, float)) for value in values.values()):
                    raise MatrixError(f"complete block is missing scalar {field}")
                scalar_effects[field] = factor_effects(
                    {variant: float(cast(int | float, value)) for variant, value in values.items()}
                )
            block_effects.append({"replicate": replicate, "effects": scalar_effects})
        summaries: dict[str, Any] = {}
        for field in ABLATION_SCALARS:
            summaries[field] = {
                effect: _distribution(
                    [float(row["effects"][field][effect]) for row in block_effects]
                )
                for effect in ("rrc", "contextmesh", "interaction")
            }
        effects[scenario] = {
            "complete_blocks": len(block_effects),
            "headline": "SUFFICIENT" if len(block_effects) >= 3 else "INSUFFICIENT",
            "blocks": block_effects,
            "summary": summaries,
        }
    overhead_totals = _aggregate_visible_accounting(evaluation_overhead)
    result = {
        "v": 2,
        "generated_at": _now(),
        "billing_exact": False,
        "cost_kind": "standard-api-equivalent",
        "calibration": dict(calibration),
        "scheduled_cells": len(cells),
        "valid_product_cells": sum(bool(cell.get("valid")) for cell in enriched),
        "analysis_valid_cells": sum(bool(cell.get("analysis_valid")) for cell in enriched),
        "cells": enriched,
        "distributions": distributions,
        "factor_effects": effects,
        "evaluation_overhead": overhead_totals,
        "product_totals": {
            field: sum(
                cell[field] for cell in enriched if isinstance(cell.get(field), (int, float))
            )
            for field in (
                "input_tokens",
                "uncached_input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "provider_visible_tokens",
                "api_equivalent_dollars",
            )
        },
    }
    _atomic_json(run_dir / "ablation-aggregate.json", result)
    _write_text(run_dir / "ablation-report.md", render_ablation_report(result))
    return result


def render_ablation_report(result: Mapping[str, Any]) -> str:
    lines = [
        "# Hierarchical ReasonRender × ContextMesh four-way ablation",
        "",
        "Costs are frozen standard-API-equivalent estimates, not the ChatGPT subscription bill.",
        "Semantic quality is blind GPT-5.5 model adjudication against the frozen independent rubric.",
        f"Calibration selected: **{result.get('calibration', {}).get('selected', 'unknown')}**.",
        f"Calibration complete/excluded pairs: {result.get('calibration', {}).get('complete_pair_replicates', [])}/{result.get('calibration', {}).get('excluded_pair_replicates', [])}; position checks passed: {result.get('calibration', {}).get('position_checks_pass')}.",
        f"Valid product cells: {result.get('valid_product_cells')}/{result.get('scheduled_cells')}; semantic-valid: {result.get('analysis_valid_cells')}.",
        "",
        "## Median outcomes by scenario and variant",
        "",
        "| scenario | variant | total tokens | uncached | cached | output | reasoning | blocked rereads | est. $ | lexical P/R/F1 | semantic P/R/F1 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    distributions = result.get("distributions", {})
    for scenario in SCENARIOS:
        for variant in ABLATION_VARIANTS:
            fields = distributions.get(scenario, {}).get(variant, {})
            value = lambda field: fields.get(field, {}).get("median")  # noqa: E731
            lines.append(
                f"| {scenario} | {variant} | {value('provider_visible_tokens')} | "
                f"{value('uncached_input_tokens')} | {value('cached_input_tokens')} | "
                f"{value('output_tokens')} | {value('reasoning_output_tokens')} | "
                f"{value('blocked_source_attempts')} | "
                f"{value('api_equivalent_dollars')} | "
                f"{value('lexical_precision')}/{value('lexical_recall')}/{value('lexical_f1')} | "
                f"{value('semantic_precision')}/{value('semantic_recall')}/{value('semantic_f1')} |"
            )
    lines.extend(["", "## Mean factorial effects (absolute native units)", ""])
    for scenario, data in result.get("factor_effects", {}).items():
        lines.append(
            f"### {scenario} — {data.get('headline')} ({data.get('complete_blocks')} complete blocks)"
        )
        for field in ABLATION_SCALARS:
            summary = data.get("summary", {}).get(field, {})
            lines.append(
                f"- {field}: RRC={summary.get('rrc', {}).get('mean')}; "
                f"ContextMesh={summary.get('contextmesh', {}).get('mean')}; "
                f"interaction={summary.get('interaction', {}).get('mean')}"
            )
    overhead = result.get("evaluation_overhead", {})
    calibration = result.get("calibration", {})
    calibration_attempts = (
        calibration.get("attempt_totals", {}) if isinstance(calibration, dict) else {}
    )
    calibration_overhead = (
        calibration.get("evaluation_overhead", {}) if isinstance(calibration, dict) else {}
    )
    lines.extend(
        [
            "",
            "## Accounting boundary",
            "",
            f"Calibration provider-visible tokens: {calibration_attempts.get('provider_visible_tokens', 0)}.",
            f"Calibration uncached/cached input tokens: {calibration_attempts.get('uncached_input_tokens', 0)}/{calibration_attempts.get('cached_input_tokens', 0)}.",
            f"Calibration output/reasoning tokens: {calibration_attempts.get('output_tokens', 0)}/{calibration_attempts.get('reasoning_output_tokens', 0)}.",
            f"Calibration estimated API-equivalent cost: {calibration_attempts.get('api_equivalent_dollars', 0)}.",
            f"Calibration judge provider-visible tokens (excluded): {calibration_overhead.get('provider_visible_tokens', 0)}.",
            f"Calibration judge estimated API-equivalent cost (excluded): {calibration_overhead.get('api_equivalent_dollars', 0)}.",
            f"Calibration unquantified attempts: {calibration_attempts.get('unquantified_blocks', 0)}.",
            f"Product provider-visible tokens: {result.get('product_totals', {}).get('provider_visible_tokens')}.",
            f"Product judge provider-visible tokens (excluded): {overhead.get('provider_visible_tokens', 0)}.",
            "Reasoning tokens are a subset of output and are not added to totals.",
            "",
        ]
    )
    return "\n".join(lines)


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
        "--worker-model",
        os.environ.get("RRD_WORKER_MODEL", "gpt-5.4-mini"),
        "--worker-reasoning",
        os.environ.get("RRD_WORKER_REASONING", "low"),
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


def _ablation_manifest(run_dir: Path, codex_bin: Path, *, seed: int) -> None:
    plan = REPO / "docs" / "rfc-rrd-hierarchical-four-way-ablation.md"
    _atomic_json(
        run_dir / "experiment.json",
        {
            "v": 4,
            "kind": "hierarchical-four-way-ablation",
            "created_at": _now(),
            "seed": seed,
            "randomization_seeds": randomization_seeds(seed),
            "hierarchy_canary": {
                "cell_id": "hierarchy-canary--four-all--live",
                "scenario": "four-all",
                "workers": 4,
                "rrc_control": "live",
                "rrc_enabled": True,
                "contextmesh_enabled": True,
            },
            "calibration_schedule": calibration_schedule(seed=seed),
            "product_schedule": ablation_schedule(seed=seed),
            "root": {"model": "gpt-5.5", "reasoning": "medium"},
            "planner": {"model": "gpt-5.5", "reasoning": "medium"},
            "worker": {"model": "gpt-5.4-mini", "reasoning": "low"},
            "prices_per_million": {
                model: {"uncached_input": rates[0], "cached_input": rates[1], "output": rates[2]}
                for model, rates in PRICE_TABLE.items()
            },
            "gpt_5_5_long_context_rule": {
                "threshold_input_tokens": 272_000,
                "uncached_and_cached_multiplier": 2,
                "output_multiplier": 1.5,
            },
            "billing_exact": False,
            "codex_binary": str(codex_bin),
            "codex_binary_sha256": _sha(codex_bin.read_bytes()),
            "codex_version": _run_checked((str(codex_bin), "--version"), env=os.environ).strip(),
            "git_head": _run_checked(("git", "rev-parse", "HEAD"), env=os.environ).strip(),
            "git_status_sha256": _sha(_run_checked(("git", "status", "--short"), env=os.environ)),
            "source_hashes": _source_hashes(),
            "target_hashes": _target_hashes(),
            "rubric_sha256": RUBRIC_SHA256,
            "analyzer_sha256": _sha(Path(__file__).read_bytes()),
            "plan_sha256": _sha(plan.read_bytes()),
            "config_sha256": _sha((NATIVE_HOME / "config.toml").read_bytes()),
        },
    )


def validate_ablation_resume(experiment: Mapping[str, Any], *, codex_bin: Path, seed: int) -> None:
    """Require every frozen experiment identity field to match before another paid call."""

    plan = REPO / "docs" / "rfc-rrd-hierarchical-four-way-ablation.md"
    expected = {
        "v": 4,
        "kind": "hierarchical-four-way-ablation",
        "seed": seed,
        "randomization_seeds": randomization_seeds(seed),
        "hierarchy_canary": {
            "cell_id": "hierarchy-canary--four-all--live",
            "scenario": "four-all",
            "workers": 4,
            "rrc_control": "live",
            "rrc_enabled": True,
            "contextmesh_enabled": True,
        },
        "calibration_schedule": calibration_schedule(seed=seed),
        "product_schedule": ablation_schedule(seed=seed),
        "root": {"model": "gpt-5.5", "reasoning": "medium"},
        "planner": {"model": "gpt-5.5", "reasoning": "medium"},
        "worker": {"model": "gpt-5.4-mini", "reasoning": "low"},
        "prices_per_million": {
            model: {"uncached_input": rates[0], "cached_input": rates[1], "output": rates[2]}
            for model, rates in PRICE_TABLE.items()
        },
        "gpt_5_5_long_context_rule": {
            "threshold_input_tokens": 272_000,
            "uncached_and_cached_multiplier": 2,
            "output_multiplier": 1.5,
        },
        "billing_exact": False,
        "codex_binary": str(codex_bin),
        "codex_binary_sha256": _sha(codex_bin.read_bytes()),
        "codex_version": _run_checked((str(codex_bin), "--version"), env=os.environ).strip(),
        "git_head": _run_checked(("git", "rev-parse", "HEAD"), env=os.environ).strip(),
        "git_status_sha256": _sha(_run_checked(("git", "status", "--short"), env=os.environ)),
        "source_hashes": _source_hashes(),
        "target_hashes": _target_hashes(),
        "rubric_sha256": RUBRIC_SHA256,
        "analyzer_sha256": _sha(Path(__file__).read_bytes()),
        "plan_sha256": _sha(plan.read_bytes()),
        "config_sha256": _sha((NATIVE_HOME / "config.toml").read_bytes()),
    }
    for field, value in expected.items():
        if experiment.get(field) != value:
            raise MatrixError(f"ablation resume {field} drifted")


def _load_judgment(
    block: Path,
    *,
    expected_scenario: str | None = None,
    expected_cell_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    data = _bounded_bytes(block / "summary.json", limit=2_000_000)
    seal = _bounded_bytes(block / "summary.sha256", limit=65).decode("ascii").strip()
    if seal != _sha(data):
        raise MatrixError(f"judgment seal mismatch: {block}")
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict) or value.get("block_id") != block.name:
        raise MatrixError(f"judgment identity mismatch: {block}")
    if expected_scenario is not None and value.get("scenario") != expected_scenario:
        raise MatrixError(f"judgment scenario mismatch: {block}")
    if expected_cell_ids is not None and value.get("cell_ids") != sorted(expected_cell_ids):
        raise MatrixError(f"judgment cell identities mismatch: {block}")
    if value.get("valid"):
        artifacts = value.get("artifacts")
        if not isinstance(artifacts, dict):
            raise MatrixError(f"valid judgment has no artifact inventory: {block}")
        for group in ("transcripts", "evidence"):
            rows = artifacts.get(group)
            if not isinstance(rows, list) or not rows:
                raise MatrixError(f"valid judgment has no {group} inventory: {block}")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                    raise MatrixError(f"judgment artifact inventory is malformed: {block}")
                relative = Path(row["name"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise MatrixError(f"judgment artifact name is unsafe: {block}")
                artifact = block / group / relative
                raw = _bounded_bytes(artifact)
                if row.get("bytes") != len(raw) or row.get("sha256") != _sha(raw):
                    raise MatrixError(f"judgment artifact seal mismatch: {artifact}")
    return value


def _resume_or_run_factor(
    run_dir: Path,
    spec: Mapping[str, Any],
    codex_bin: Path,
    *,
    deterministic: bool,
) -> dict[str, Any]:
    cell = run_dir / "cells" / str(spec["cell_id"])
    if (cell / "summary.json").exists() and (cell / "summary.sha256").exists():
        return _load_cell_summary(cell)
    if cell.exists():
        current_transcripts = _transcript_paths()
        prior: set[Path] | None = None
        try:
            attempt = json.loads(
                _bounded_bytes(cell / "attempt.json", limit=100_000).decode("utf-8")
            )
            raw_prior = (
                attempt.get("preexisting_transcripts") if isinstance(attempt, dict) else None
            )
            if isinstance(raw_prior, list) and all(isinstance(value, str) for value in raw_prior):
                prior = {Path(value) for value in raw_prior}
        except (OSError, UnicodeError, json.JSONDecodeError, MatrixError):
            prior = None
        attributable = current_transcripts - prior if prior is not None else set()
        expected_sessions = (
            1
            + len(SCENARIOS[str(spec["scenario"])])
            + (
                1
                if bool(spec["rrc_enabled"]) and str(spec.get("rrc_control", "live")) == "live"
                else 0
            )
        )
        recovered = _recover_and_snapshot(cell, attributable, expected_sessions=expected_sessions)
        if prior is None:
            recovered["unquantified_consumption"] = True
            recovered.setdefault("recovery_errors", []).append(
                "attempt transcript baseline is unavailable; no current session was attributed"
            )
        summary = {
            "v": 2,
            **dict(spec),
            "valid": False,
            "errors": ["interrupted physical attempt; immutable and not retried"],
            **recovered,
            "completed_at": _now(),
        }
        _write_cell_summary(cell, summary)
        return summary
    return run_factor_cell(run_dir, spec, codex_bin, deterministic=deterministic)


def _recover_interrupted_judge(block: Path) -> dict[str, Any] | None:
    if not (block / "launch.json").exists():
        return None
    prior: set[Path] | None = None
    try:
        attempt = json.loads(_bounded_bytes(block / "attempt.json", limit=100_000).decode("utf-8"))
        raw_prior = attempt.get("preexisting_transcripts") if isinstance(attempt, dict) else None
        if isinstance(raw_prior, list) and all(isinstance(value, str) for value in raw_prior):
            prior = {Path(value) for value in raw_prior}
    except (OSError, UnicodeError, json.JSONDecodeError, MatrixError):
        prior = None
    attributable = _transcript_paths() - prior if prior is not None else set()
    recovered = _recover_and_snapshot(block, attributable, expected_sessions=1)
    if prior is None:
        recovered["unquantified_consumption"] = True
        recovered.setdefault("recovery_errors", []).append(
            "judge transcript baseline is unavailable; no current session was attributed"
        )
    return recovered


def _resume_or_run_judge(
    run_dir: Path,
    *,
    block_id: str,
    scenario: str,
    cells: Sequence[Mapping[str, Any]],
    codex_bin: Path,
    seed: int,
) -> dict[str, Any]:
    block = run_dir / "judgments" / block_id
    if (block / "summary.json").exists() and (block / "summary.sha256").exists():
        return _load_judgment(
            block,
            expected_scenario=scenario,
            expected_cell_ids=[str(cell.get("cell_id")) for cell in cells],
        )
    if block.exists():
        recovered = _recover_interrupted_judge(block)
        result = {
            "v": 1,
            "block_id": block_id,
            "scenario": scenario,
            "cell_ids": sorted(str(cell.get("cell_id")) for cell in cells),
            "valid": False,
            "errors": ["interrupted judge attempt; immutable and not retried"],
            "metrics": {},
            "evaluation_overhead": recovered,
            "completed_at": _now(),
        }
        _atomic_json(block / "summary.json", result)
        _write_text(block / "summary.sha256", _sha(_bounded_bytes(block / "summary.json")) + "\n")
        return result
    try:
        return run_judge_block(
            run_dir,
            block_id=block_id,
            scenario=scenario,
            cells=cells,
            codex_bin=codex_bin,
            seed=seed,
        )
    except Exception as exc:
        block.mkdir(parents=True, exist_ok=True, mode=0o700)
        recovered = _recover_interrupted_judge(block)
        result = {
            "v": 1,
            "block_id": block_id,
            "scenario": scenario,
            "cell_ids": sorted(str(cell.get("cell_id")) for cell in cells),
            "valid": False,
            "errors": [f"{type(exc).__name__}: {exc}"],
            "metrics": {},
            "evaluation_overhead": recovered,
            "completed_at": _now(),
        }
        _atomic_json(block / "summary.json", result)
        _write_text(block / "summary.sha256", _sha(_bounded_bytes(block / "summary.json")) + "\n")
        return result


def run_ablation(
    run_dir: Path | None = None, *, resume: bool = False, seed: int = 20260809
) -> tuple[Path, dict[str, Any]]:
    """Run calibration, freeze the RRC control, then execute the 48-cell factorial."""

    codex_bin = _codex_binary()
    for name, expected in FROZEN_ROLE_CONFIG.items():
        if os.environ.get(name, expected) != expected:
            raise MatrixError(
                f"hierarchical ablation requires frozen {name}={expected}; unset the override"
            )
    RUNS.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(RUNS / "native-matrix.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MatrixError("another hierarchical ablation owns the stable Codex home") from exc
        _run_checked(
            _native_config_command(codex_bin), env=_launcher_environment("sqlite", codex_bin)
        )
        _rubric()
        selected = (
            run_dir
            or RUNS / f"hierarchical-ablation-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        ).resolve()
        manifest = selected / "experiment.json"
        if selected.exists():
            if not resume or not manifest.exists():
                raise MatrixError(f"run directory exists or is not resumable: {selected}")
            experiment = json.loads(_bounded_bytes(manifest).decode("utf-8"))
            if not isinstance(experiment, dict):
                raise MatrixError("ablation resume manifest is not an object")
            validate_ablation_resume(experiment, codex_bin=codex_bin, seed=seed)
        else:
            if resume:
                raise MatrixError(f"resume directory does not exist: {selected}")
            selected.mkdir(parents=True, mode=0o700)
            _ablation_manifest(selected, codex_bin, seed=seed)

        canary_spec = {
            "cell_id": "hierarchy-canary--four-all--live",
            "scenario": "four-all",
            "handlers": list(SCENARIOS["four-all"]),
            "workers": 4,
            "replicate": 0,
            "position": 0,
            "variant": "calibration-live",
            "rrc_enabled": True,
            "contextmesh_enabled": True,
            "rrc_control": "live",
            "phase": "hierarchy-canary",
        }
        canary = _resume_or_run_factor(selected, canary_spec, codex_bin, deterministic=False)
        _atomic_json(selected / "hierarchy-canary.json", canary)
        if not canary.get("valid"):
            raise MatrixError(
                "hierarchy canary failed; calibration was not started: "
                + "; ".join(str(value) for value in canary.get("errors", []))
            )
        calibration_cells: list[dict[str, Any]] = []
        for spec in calibration_schedule(seed=seed):
            summary = _resume_or_run_factor(
                selected,
                spec,
                codex_bin,
                deterministic=spec["rrc_control"] == "deterministic",
            )
            calibration_cells.append(summary)
            print(
                json.dumps(
                    {
                        "phase": "calibration",
                        "cell": spec["cell_id"],
                        "valid": summary.get("valid"),
                        "tokens": summary.get("provider_visible_tokens"),
                    }
                ),
                flush=True,
            )
        calibration_judgments: list[dict[str, Any]] = []
        for replicate in range(1, ABLATION_REPLICATES + 1):
            pair = [cell for cell in calibration_cells if cell.get("replicate") == replicate]
            judgment = _resume_or_run_judge(
                selected,
                block_id=f"calibration-r{replicate:02d}",
                scenario="four-all",
                cells=pair,
                codex_bin=codex_bin,
                seed=seed,
            )
            calibration_judgments.append(judgment)
        calibration_semantic = {
            cell_id: metric
            for block in calibration_judgments
            if block.get("valid")
            for cell_id, metric in block.get("metrics", {}).items()
        }
        selection_rows = [
            {**cell, "semantic": calibration_semantic.get(str(cell.get("cell_id")))}
            for cell in calibration_cells
        ]
        selection = calibration_record(selection_rows, calibration_judgments)
        _atomic_json(selected / "calibration-selection.json", selection)
        if not selection["conclusive"]:
            raise MatrixError(
                "planner calibration was inconclusive; product ablation was not started"
            )

        final_cells: list[dict[str, Any]] = []
        deterministic = selection["selected"] == "deterministic"
        for spec in ablation_schedule(seed=seed):
            summary = _resume_or_run_factor(selected, spec, codex_bin, deterministic=deterministic)
            final_cells.append(summary)
            print(
                json.dumps(
                    {
                        "phase": "product",
                        "cell": spec["cell_id"],
                        "valid": summary.get("valid"),
                        "tokens": summary.get("provider_visible_tokens"),
                    }
                ),
                flush=True,
            )
        final_judgments: list[dict[str, Any]] = []
        for scenario in SCENARIOS:
            for replicate in range(1, ABLATION_REPLICATES + 1):
                block_cells = [
                    cell
                    for cell in final_cells
                    if cell.get("scenario") == scenario and cell.get("replicate") == replicate
                ]
                final_judgments.append(
                    _resume_or_run_judge(
                        selected,
                        block_id=f"product-{scenario}-r{replicate:02d}",
                        scenario=scenario,
                        cells=block_cells,
                        codex_bin=codex_bin,
                        seed=seed,
                    )
                )
        result = aggregate_ablation(
            selected,
            cells=final_cells,
            judgment_blocks=final_judgments,
            calibration=selection,
        )
        _atomic_json(
            selected / "calibration-evaluation-overhead.json",
            {
                "blocks": calibration_judgments,
                "totals": _aggregate_visible_accounting(
                    [
                        block["evaluation_overhead"]
                        for block in calibration_judgments
                        if isinstance(block.get("evaluation_overhead"), dict)
                    ]
                ),
            },
        )
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
    action.add_argument(
        "--run-ablation",
        action="store_true",
        help="calibrate live/deterministic RRC, then run 48 position-balanced 2x2 cells",
    )
    action.add_argument("--report", type=Path, help="regenerate a report from an existing run")
    parser.add_argument("--output", type=Path, help="manifest output for --plan")
    parser.add_argument("--run-dir", type=Path, help="new artifact directory for --run-matrix")
    parser.add_argument(
        "--resume", action="store_true", help="resume a source-identical --run-dir without reruns"
    )
    parser.add_argument("--seed", type=int, default=20260809, help="sealed ablation random seed")
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
    if args.run_ablation:
        selected, result = run_ablation(args.run_dir, resume=args.resume, seed=args.seed)
        print(
            json.dumps(
                {
                    "run": str(selected),
                    "valid_cells": result["valid_product_cells"],
                    "analysis_valid_cells": result["analysis_valid_cells"],
                }
            )
        )
        return (
            0
            if result["valid_product_cells"] == 48
            and result["analysis_valid_cells"] == 48
            and result.get("calibration", {}).get("conclusive") is True
            else 1
        )
    selected, result = run_matrix(args.run_dir, resume=args.resume)
    print(json.dumps({"run": str(selected), "valid_cells": result["valid_cells"]}))
    return 0 if result["valid_cells"] == len(MATRIX_ORDER) else 1


if __name__ == "__main__":
    raise SystemExit(main())
