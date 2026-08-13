from __future__ import annotations

import json
from pathlib import Path

from harness.cumulative_codex import aggregate


def _report(path: Path, offset: int) -> None:
    arms = {}
    for name, multiplier in (("raw", 4), ("contextmesh", 3), ("full", 2)):
        arms[name] = {
            "valid": True,
            "primary_compute": multiplier * 100 + offset,
            "marginal_compute": multiplier * 100 + offset,
            "worker_marginal_compute": multiplier * 80 + offset,
            "orchestrator_marginal_compute": multiplier * 20,
            "usage": {
                "totals": {
                    "input_tokens": multiplier * 1_000_000 + offset,
                    "cached_input_tokens": multiplier * 10,
                    "cache_write_input_tokens": 0,
                    "input_new_tokens": multiplier * 100,
                    "output_tokens": multiplier * 10,
                    "reasoning_output_tokens": multiplier,
                }
            },
        }
    path.write_text(json.dumps({"arms": arms}), encoding="utf-8")


def test_cumulative_report_adds_only_the_new_cohort_to_the_retained_base(tmp_path: Path) -> None:
    base = tmp_path / "v23.json"
    incremental = tmp_path / "growth.json"
    _report(base, 10)
    _report(incremental, 20)

    result = aggregate(base, incremental, minimum_raw_input_tokens=8_000_000)

    assert result["arms"]["raw"]["cumulative"]["input_tokens"] == 8_000_030
    assert result["arms"]["raw"]["cumulative"]["marginal_compute"] == 830
    assert result["target"] == {
        "raw_input_tokens": 8_000_030,
        "minimum_raw_input_tokens": 8_000_000,
        "met": True,
    }
    assert result["savings"]["full_total_vs_raw"] > 0
