"""Aggregate one completed Codex comparison with an incremental next cohort."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


ARMS = ("raw", "contextmesh", "full")
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "input_new_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
COMPUTE_FIELDS = ("primary_compute", "marginal_compute", "worker_marginal_compute", "orchestrator_marginal_compute")


class CumulativeComparisonError(ValueError):
    """Raised when a completed or incremental comparison cannot be composed."""


def _report(path: str | Path) -> Mapping[str, Any]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
        raise CumulativeComparisonError(f"unreadable report {source}: {error}") from error
    if not isinstance(value, Mapping) or not isinstance(value.get("arms"), Mapping):
        raise CumulativeComparisonError(f"invalid comparison report: {source}")
    return value


def _arm(report: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = report["arms"].get(name)
    if not isinstance(value, Mapping) or value.get("valid") is not True:
        raise CumulativeComparisonError(f"{name} arm is not a valid retained result")
    return value


def _sum(left: Mapping[str, Any], right: Mapping[str, Any], field: str) -> int:
    return int(left.get(field, 0)) + int(right.get(field, 0))


def aggregate(base_report: str | Path, incremental_report: str | Path, minimum_raw_input_tokens: int = 2_000_000) -> dict[str, Any]:
    """Return a cumulative report while retaining each batch as separately auditable."""

    base_path = Path(base_report).resolve()
    incremental_path = Path(incremental_report).resolve()
    if base_path == incremental_path:
        raise CumulativeComparisonError("base and incremental reports must be different retained runs")
    base = _report(base_path)
    incremental = _report(incremental_path)
    arms: dict[str, dict[str, Any]] = {}
    for name in ARMS:
        before = _arm(base, name)
        added = _arm(incremental, name)
        before_usage = before.get("usage", {}).get("totals", {})
        added_usage = added.get("usage", {}).get("totals", {})
        if not isinstance(before_usage, Mapping) or not isinstance(added_usage, Mapping):
            raise CumulativeComparisonError(f"{name} arm has malformed usage totals")
        arms[name] = {
            "base": {field: int(before.get(field, 0)) for field in COMPUTE_FIELDS},
            "incremental": {field: int(added.get(field, 0)) for field in COMPUTE_FIELDS},
            "cumulative": {
                **{field: _sum(before_usage, added_usage, field) for field in TOKEN_FIELDS},
                **{field: _sum(before, added, field) for field in COMPUTE_FIELDS},
            },
        }
    raw = arms["raw"]["cumulative"]
    contextmesh = arms["contextmesh"]["cumulative"]
    full = arms["full"]["cumulative"]
    raw_input = raw["input_tokens"]
    return {
        "schema_version": 1,
        "source_reports": {"base": str(base_path), "incremental": str(incremental_path)},
        "counting_rule": "Each retained stream appears once: V23 is base and only new cohort streams are incremental.",
        "arms": arms,
        "target": {
            "raw_input_tokens": raw_input,
            "minimum_raw_input_tokens": minimum_raw_input_tokens,
            "met": raw_input >= minimum_raw_input_tokens,
        },
        "savings": {
            "contextmesh_worker_vs_raw": 1 - contextmesh["worker_marginal_compute"] / raw["worker_marginal_compute"],
            "rrc_terra_vs_raw": 1 - full["orchestrator_marginal_compute"] / raw["orchestrator_marginal_compute"],
            "full_total_vs_raw": 1 - full["marginal_compute"] / raw["marginal_compute"],
            "full_total_vs_contextmesh": 1 - full["marginal_compute"] / contextmesh["marginal_compute"],
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-report", required=True)
    parser.add_argument("--incremental-report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--minimum-raw-input-tokens", type=int, default=2_000_000)
    args = parser.parse_args(argv)
    try:
        result = aggregate(args.base_report, args.incremental_report, args.minimum_raw_input_tokens)
    except CumulativeComparisonError as error:
        print(f"cumulative comparison error: {error}")
        return 2
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
