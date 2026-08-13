from __future__ import annotations

import json

from harness.staged_local_proof import run_local_three_stage_proof


def test_local_three_stage_proof_retains_broker_lineage_and_rrc_delta_hits(tmp_path) -> None:
    report = run_local_three_stage_proof(tmp_path / "proof")

    assert report["proof"] == {
        "later_unchanged_reuse": True,
        "later_diff_refresh": True,
        "later_rrc_hits_zero_reconstruction": True,
    }
    assert report["cohort"] == {
        "start_stage": "stage-35",
        "stage_ids": ["stage-35", "stage-36", "stage-37"],
        "lineage_mode": "fresh_independent_baseline",
    }
    assert len(report["stages"]) == 3
    assert all(stage["delivery_plan_count"] == 4 for stage in report["stages"])
    assert [stage["rrc_reconstruction_reads"] for stage in report["stages"][1:]] == [0, 0]
    assert report["broker_event_counts"]["stage_installed"] == 2
    assert report["broker_event_counts"]["brief_reused_unchanged"] > 0
    assert report["broker_event_counts"]["brief_refresh_diff"] > 0
    assert report["stages"][-1]["brief_modes"]["ruleforge/policy_catalog.py"] == "unchanged_reuse"
    assert report["source_mass"]["cumulative_duplicate_source_bytes"] >= 720_000
    retained = json.loads((tmp_path / "proof" / "report.json").read_text(encoding="utf-8"))
    assert retained == report
