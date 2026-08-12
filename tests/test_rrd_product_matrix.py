from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from pathlib import Path

import pytest
from contextmesh.bench import run_bench


def _transcript(
    path: Path,
    *,
    thread_id: str,
    parent: str | None,
    total: int,
) -> Path:
    rows = [
        {
            "timestamp": "2026-08-09T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": thread_id, "parent_thread_id": parent},
        },
        {
            "timestamp": "2026-08-09T00:00:01Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": total - 2,
                        "cached_input_tokens": 1,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 2,
                        "reasoning_output_tokens": 1,
                        "total_tokens": total,
                    }
                },
            },
        },
        {
            "timestamp": "2026-08-09T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "task_complete"},
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_matrix_contract_has_nine_ordered_cells_and_no_token_ceiling() -> None:
    value = run_bench.matrix()

    assert [cell["cell_id"] for cell in value["cells"]] == [
        "single-users--baseline",
        "single-users--combined-local",
        "single-users--combined-everos",
        "pair-users-products--combined-local",
        "pair-users-products--combined-everos",
        "pair-users-products--baseline",
        "four-all--combined-everos",
        "four-all--baseline",
        "four-all--combined-local",
    ]
    serialized = json.dumps(value).lower()
    assert "ceiling" not in serialized
    assert "token_budget" not in serialized
    assert value["cell_timeout_seconds"] == 720


def test_ablation_schedule_has_48_position_balanced_seeded_cells() -> None:
    cells = run_bench.ablation_schedule(seed=20260809)

    assert len(cells) == 48
    assert len({cell["cell_id"] for cell in cells}) == 48
    assert {cell["variant"] for cell in cells} == {
        "native",
        "rrc",
        "contextmesh",
        "combined",
    }
    for scenario in run_bench.SCENARIOS:
        rows = [cell for cell in cells if cell["scenario"] == scenario]
        assert len(rows) == 16
        positions = {
            variant: sorted(cell["position"] for cell in rows if cell["variant"] == variant)
            for variant in run_bench.ABLATION_VARIANTS
        }
        assert all(value == [0, 1, 2, 3] for value in positions.values())
        assert {cell["replicate"] for cell in rows} == {1, 2, 3, 4}
    assert cells == run_bench.ablation_schedule(seed=20260809)
    assert cells != run_bench.ablation_schedule(seed=20260810)


def test_api_equivalent_cost_separates_cache_and_long_context() -> None:
    normal = run_bench.api_equivalent_cost(
        model="gpt-5.5",
        input_tokens=100_000,
        cached_input_tokens=40_000,
        output_tokens=10_000,
    )
    long = run_bench.api_equivalent_cost(
        model="gpt-5.5",
        input_tokens=300_000,
        cached_input_tokens=40_000,
        output_tokens=10_000,
    )
    mini = run_bench.api_equivalent_cost(
        model="gpt-5.4-mini",
        input_tokens=100_000,
        cached_input_tokens=40_000,
        output_tokens=10_000,
    )

    assert math.isclose(normal, 0.04 * 0.50 + 0.06 * 5 + 0.01 * 30)
    assert math.isclose(long, 0.04 * 1 + 0.26 * 10 + 0.01 * 45)
    assert math.isclose(mini, 0.04 * 0.075 + 0.06 * 0.75 + 0.01 * 4.5)
    with pytest.raises(run_bench.MatrixError, match="cached"):
        run_bench.api_equivalent_cost(
            model="gpt-5.5", input_tokens=1, cached_input_tokens=2, output_tokens=0
        )


def test_factor_effects_use_absolute_preregistered_formulas() -> None:
    value = run_bench.factor_effects(
        {"native": 100.0, "rrc": 90.0, "contextmesh": 80.0, "combined": 60.0}
    )
    assert value == {"rrc": -15.0, "contextmesh": -25.0, "interaction": -10.0}


def test_ablation_aggregate_includes_role_and_quality_factor_metrics(tmp_path: Path) -> None:
    cells: list[dict[str, object]] = []
    metrics: dict[str, object] = {}
    values = {"native": 100, "rrc": 90, "contextmesh": 80, "combined": 60}
    for variant, value in values.items():
        cell_id = f"single-users--r01--{variant}"
        component = {
            "provider_visible_tokens": value,
            "uncached_input_tokens": value - 10,
            "cached_input_tokens": 10,
            "output_tokens": 5,
            "reasoning_output_tokens": 2,
            "api_equivalent_dollars": value / 1000,
        }
        cells.append(
            {
                "cell_id": cell_id,
                "scenario": "single-users",
                "replicate": 1,
                "variant": variant,
                "valid": True,
                **component,
                "wall_seconds": 1.0,
                "blocked_source_attempts": 0,
                "components": {
                    "root": component,
                    "worker": component,
                    "planner": component,
                },
                "quality": {
                    "lexical_precision": 0.8,
                    "lexical_recall": 0.7,
                    "lexical_f1": 0.746,
                },
            }
        )
        metrics[cell_id] = {
            "precision": 0.9,
            "recall": 0.8,
            "f1": 0.847,
        }

    result = run_bench.aggregate_ablation(
        tmp_path,
        cells=cells,
        judgment_blocks=[
            {
                "valid": True,
                "metrics": metrics,
                "evaluation_overhead": None,
            }
        ],
        calibration={
            "selected": "deterministic",
            "conclusive": True,
            "attempt_totals": {
                "provider_visible_tokens": 800,
                "unquantified_blocks": 1,
            },
            "evaluation_overhead": {"provider_visible_tokens": 50},
        },
    )

    summary = result["factor_effects"]["single-users"]
    assert summary["complete_blocks"] == 1
    assert summary["summary"]["root_provider_visible_tokens"]["rrc"]["mean"] == -15
    assert summary["summary"]["semantic_precision"]["interaction"]["mean"] == 0
    report = (tmp_path / "ablation-report.md").read_text()
    assert "lexical P/R/F1" in report and "semantic P/R/F1" in report
    assert "blocked rereads" in report
    assert "Calibration provider-visible tokens: 800" in report
    assert "Calibration judge provider-visible tokens (excluded): 50" in report
    assert "Calibration unquantified attempts: 1" in report


def test_ablation_aggregate_preserves_unquantified_judge_overhead(tmp_path: Path) -> None:
    result = run_bench.aggregate_ablation(
        tmp_path,
        cells=[],
        judgment_blocks=[
            {
                "valid": False,
                "metrics": {},
                "evaluation_overhead": {
                    "provider_visible_tokens": None,
                    "api_equivalent_dollars": None,
                    "unquantified_consumption": True,
                },
            }
        ],
        calibration={"selected": "live", "conclusive": False},
    )

    assert result["evaluation_overhead"]["provider_visible_tokens"] == 0
    assert result["evaluation_overhead"]["unquantified_blocks"] == 1
    assert result["evaluation_overhead"]["complete"] is False


def test_valid_judgment_resume_rehashes_raw_artifacts(tmp_path: Path) -> None:
    block = tmp_path / "judge"
    inventories = {}
    for group in ("transcripts", "evidence"):
        directory = block / group
        directory.mkdir(parents=True)
        artifact = directory / "proof.jsonl"
        artifact.write_text('{"ok":true}\n')
        inventories[group] = [
            {
                "name": artifact.name,
                "bytes": len(artifact.read_bytes()),
                "sha256": run_bench._sha(artifact.read_bytes()),
            }
        ]
    summary = {
        "v": 1,
        "block_id": "judge",
        "scenario": "single-users",
        "cell_ids": ["cell-a"],
        "valid": True,
        "artifacts": inventories,
    }
    run_bench._atomic_json(block / "summary.json", summary)
    run_bench._write_text(
        block / "summary.sha256",
        run_bench._sha((block / "summary.json").read_bytes()) + "\n",
    )

    run_bench._load_judgment(block, expected_scenario="single-users", expected_cell_ids=["cell-a"])
    (block / "evidence" / "proof.jsonl").write_text("mutated\n")
    with pytest.raises(run_bench.MatrixError, match="artifact seal mismatch"):
        run_bench._load_judgment(
            block, expected_scenario="single-users", expected_cell_ids=["cell-a"]
        )


def test_invalid_judgment_resume_normalizes_randomized_cell_order(tmp_path: Path) -> None:
    block = tmp_path / "judge-failed"
    block.mkdir()
    summary = {
        "v": 1,
        "block_id": "judge-failed",
        "scenario": "single-users",
        "cell_ids": ["cell-a", "cell-z"],
        "valid": False,
        "errors": ["provider failure"],
    }
    run_bench._atomic_json(block / "summary.json", summary)
    run_bench._write_text(
        block / "summary.sha256",
        run_bench._sha((block / "summary.json").read_bytes()) + "\n",
    )

    value = run_bench._load_judgment(
        block,
        expected_scenario="single-users",
        expected_cell_ids=["cell-z", "cell-a"],
    )
    assert value["valid"] is False


def test_calibration_schedule_is_paired_and_position_balanced() -> None:
    cells = run_bench.calibration_schedule(seed=20260809)

    assert len(cells) == 8
    assert len({cell["cell_id"] for cell in cells}) == 8
    assert {cell["rrc_control"] for cell in cells} == {"live", "deterministic"}
    assert all(cell["scenario"] == "four-all" for cell in cells)
    for replicate in range(1, 5):
        pair = [cell for cell in cells if cell["replicate"] == replicate]
        assert len(pair) == 2
        assert {cell["position"] for cell in pair} == {0, 1}
    assert sum(cell["position"] == 0 and cell["rrc_control"] == "live" for cell in cells) == 2
    assert cells == run_bench.calibration_schedule(seed=20260809)


def test_deterministic_rrc_selection_uses_frozen_quality_boundary() -> None:
    def cells(control: str, f1: list[float], high_recall: float) -> list[dict[str, object]]:
        return [
            {
                "valid": True,
                "rrc_control": control,
                "replicate": index + 1,
                "position": (index % 2) if control == "live" else 1 - (index % 2),
                "semantic": {
                    "f1": score,
                    "high_critical_tp": int(high_recall * 10),
                    "high_critical_total": 10,
                },
                "components": {
                    "planner": {"provider_visible_tokens": 0 if control == "deterministic" else 100}
                },
            }
            for index, score in enumerate(f1)
        ]

    live = cells("live", [0.80, 0.81, 0.82, 0.83], 0.8)
    deterministic = cells("deterministic", [0.75, 0.76, 0.77, 0.78], 0.8)
    selected = run_bench.select_rrc_control([*live, *deterministic])
    assert selected["selected"] == "deterministic"
    deterministic[0]["semantic"]["high_critical_tp"] = 7  # type: ignore[index]
    selected = run_bench.select_rrc_control([*live, *deterministic])
    assert selected["selected"] == "live"
    assert selected["conclusive"] is True


def test_rrc_selection_uses_three_complete_pairs_and_rejects_two() -> None:
    def cell(control: str, replicate: int) -> dict[str, object]:
        live_position = (replicate - 1) % 2
        return {
            "valid": True,
            "rrc_control": control,
            "replicate": replicate,
            "position": live_position if control == "live" else 1 - live_position,
            "semantic": {
                "f1": 0.5,
                "high_critical_tp": 5,
                "high_critical_total": 10,
            },
            "components": {
                "planner": {"provider_visible_tokens": 0 if control == "deterministic" else 100}
            },
        }

    rows = [
        cell(control, replicate)
        for replicate in range(1, 5)
        for control in ("live", "deterministic")
    ]
    rows[0] = {**rows[0], "valid": False, "semantic": None}

    selected = run_bench.select_rrc_control(rows)
    assert selected["conclusive"] is True
    assert selected["selected"] == "deterministic"
    assert selected["complete_pair_replicates"] == [2, 3, 4]
    assert selected["excluded_pair_replicates"] == [1]

    rows[2] = {**rows[2], "valid": False, "semantic": None}
    selected = run_bench.select_rrc_control(rows)
    assert selected["conclusive"] is False
    assert selected["selected"] == "live"


def test_rrc_selection_is_position_aware_and_rejects_poisoned_metrics() -> None:
    rows: list[dict[str, object]] = []
    for replicate in range(1, 5):
        live_position = (replicate - 1) % 2
        for control in ("live", "deterministic"):
            score = 0.5
            if control == "deterministic" and live_position == 1:
                score = 0.3
            rows.append(
                {
                    "valid": True,
                    "rrc_control": control,
                    "replicate": replicate,
                    "position": live_position if control == "live" else 1 - live_position,
                    "semantic": {
                        "f1": score,
                        "high_critical_tp": 5,
                        "high_critical_total": 10,
                    },
                    "components": {
                        "planner": {
                            "provider_visible_tokens": 0 if control == "deterministic" else 100
                        }
                    },
                }
            )

    selected = run_bench.select_rrc_control(rows)
    assert selected["conclusive"] is True
    assert selected["position_checks_pass"] is False
    assert selected["selected"] == "live"

    rows[0]["semantic"] = {
        "f1": float("nan"),
        "high_critical_tp": 5,
        "high_critical_total": 10,
    }
    selected = run_bench.select_rrc_control(rows)
    assert selected["conclusive"] is False


def test_calibration_record_keeps_failed_attempt_and_judge_accounting(tmp_path: Path) -> None:
    rows: list[dict[str, object]] = []
    for replicate in range(1, 5):
        live_position = (replicate - 1) % 2
        for control in ("live", "deterministic"):
            visible = 100 + replicate
            rows.append(
                {
                    "cell_id": f"calibration--r{replicate:02d}--{control}",
                    "valid": not (replicate == 1 and control == "live"),
                    "errors": ["missing usage"] if replicate == 1 and control == "live" else [],
                    "rrc_control": control,
                    "replicate": replicate,
                    "position": live_position if control == "live" else 1 - live_position,
                    "semantic": None
                    if replicate == 1
                    else {
                        "f1": 0.5,
                        "high_critical_tp": 5,
                        "high_critical_total": 10,
                    },
                    "components": {
                        "planner": {
                            "provider_visible_tokens": 0 if control == "deterministic" else 100
                        }
                    },
                    "input_tokens": visible - 5,
                    "uncached_input_tokens": visible - 15,
                    "cached_input_tokens": 10,
                    "output_tokens": 5,
                    "reasoning_output_tokens": 2,
                    "provider_visible_tokens": visible,
                    "api_equivalent_dollars": visible / 1000,
                }
            )
    judgments = [
        {
            "valid": True,
            "evaluation_overhead": {
                "input_tokens": 10,
                "uncached_input_tokens": 8,
                "cached_input_tokens": 2,
                "output_tokens": 1,
                "reasoning_output_tokens": 1,
                "provider_visible_tokens": 11,
                "api_equivalent_dollars": 0.01,
            },
        }
        for _ in range(3)
    ]
    judgments.append({"valid": False, "evaluation_overhead": None})

    record = run_bench.calibration_record(rows, judgments)

    assert record["conclusive"] is True
    assert record["attempt_totals"]["provider_visible_tokens"] == sum(
        2 * (100 + replicate) for replicate in range(1, 5)
    )
    failed = next(cell for cell in record["cells"] if not cell["valid"])
    assert failed["accounting"]["provider_visible_tokens"] == 101
    assert failed["errors"] == ["missing usage"]
    assert record["evaluation_overhead"]["provider_visible_tokens"] == 33
    assert record["judge_blocks_attempted"] == 4
    assert record["judge_blocks_valid"] == 3

    run_bench.aggregate_ablation(
        tmp_path,
        cells=[],
        judgment_blocks=[],
        calibration=record,
    )
    report = (tmp_path / "ablation-report.md").read_text()
    assert "Calibration complete/excluded pairs: [2, 3, 4]/[1]" in report
    assert "Calibration provider-visible tokens: 820" in report
    assert "Calibration uncached/cached input tokens: 700/80" in report
    assert "Calibration output/reasoning tokens: 40/16" in report
    assert "Calibration estimated API-equivalent cost: 0.82" in report
    assert "Calibration judge provider-visible tokens (excluded): 33" in report
    assert "Calibration judge estimated API-equivalent cost (excluded): 0.03" in report
    assert "Calibration unquantified attempts: 0" in report


def test_semantic_scorer_fails_closed_and_penalizes_duplicates() -> None:
    report = """## src/handlers/users.js
- medium | src/handlers/users.js:17-31 | Registration permits an omitted password.
- medium | src/handlers/users.js:17-31 | Registration permits an omitted password again.
- high | src/handlers/users.js:82-95 | Any user can edit another user's profile.
"""
    judgment = {
        "label": "candidate-k",
        "claims": [
            {"index": 0, "rubric_id": "USR-VAL-001", "supported": True, "rationale": "direct"},
            {"index": 1, "rubric_id": "USR-VAL-001", "supported": True, "rationale": "duplicate"},
            {"index": 2, "rubric_id": "USR-AUTHZ-001", "supported": True, "rationale": "direct"},
        ],
    }

    value = run_bench.score_semantic_candidate(
        label="candidate-k", report=report, scenario="single-users", judgment=judgment
    )

    assert (value["tp"], value["fp"], value["fn"]) == (2, 1, 1)
    assert math.isclose(value["precision"], 2 / 3)
    assert math.isclose(value["recall"], 2 / 3)
    bad = json.loads(json.dumps(judgment))
    bad["claims"][0]["index"] = 99
    with pytest.raises(run_bench.MatrixError, match="claim indices"):
        run_bench.score_semantic_candidate(
            label="candidate-k", report=report, scenario="single-users", judgment=bad
        )


@pytest.mark.parametrize(
    ("scenario", "count"),
    [("single-users", 1), ("pair-users-products", 2), ("four-all", 4)],
)
def test_scenario_prompt_is_exact_and_parameterized(scenario: str, count: int) -> None:
    prompt = run_bench.render_prompt(scenario)
    handlers = run_bench.SCENARIOS[scenario]

    assert f"Spawn exactly {count} worker" in prompt
    assert prompt.count("- worker ") == count
    assert all(f"src/handlers/{handler}.js" in prompt for handler in handlers)
    assert 'agent_type="worker" and fork_context=false' in prompt
    assert "Do not inspect source files in the root session" in prompt
    assert "## src/handlers/<name>.js" in prompt
    assert "- <severity> | src/handlers/<name>.js:<line[-line]> | <one sentence>" in prompt


def test_baseline_command_and_environment_are_neutral(tmp_path: Path) -> None:
    command = run_bench.codex_command(
        codex_bin=Path("/usr/bin/codex"),
        prompt="audit",
        final_path=tmp_path / "final.md",
        baseline=True,
    )
    env = run_bench.codex_environment(
        codex_home=tmp_path / "home",
        codex_bin=Path("/usr/bin/codex"),
        combined=None,
    )

    joined = " ".join(command)
    assert "features.hooks=false" in joined
    assert run_bench.NEUTRAL_WORKER_DESCRIPTION in joined
    assert "ReasonRender" not in command[-1] and "ContextMesh" not in command[-1]
    if sys.platform == "darwin":
        assert command[:3] == [
            "/usr/bin/sandbox-exec",
            "-f",
            str(run_bench.NATIVE_HOME / "credential-deny.sb"),
        ]
        assert "--dangerously-bypass-approvals-and-sandbox" in command
    else:
        assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert str((tmp_path / "final.md").resolve()) in command
    assert not any(key.startswith(("RRD_", "RRC_")) for key in env)


def test_matrix_variants_map_to_launcher_backend_names() -> None:
    assert run_bench.memory_backend("baseline") == "none"
    assert run_bench.memory_backend("combined-local") == "sqlite"
    assert run_bench.memory_backend("combined-everos") == "everos"
    with pytest.raises(run_bench.MatrixError, match="unknown matrix variant"):
        run_bench.memory_backend("combined-localish")


def test_launcher_environment_preserves_reasoning_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RRD_CODEX_REASONING", "high")
    codex = tmp_path / "codex"
    env = run_bench._launcher_environment("sqlite", codex)
    assert env["RRD_CODEX_REASONING"] == "high"
    command = run_bench._native_config_command(codex)
    assert command[command.index("--reasoning") + 1] == "high"


def test_transcript_attribution_binds_root_workers_and_planner(tmp_path: Path) -> None:
    paths = [
        _transcript(tmp_path / "root.jsonl", thread_id="root", parent=None, total=10),
        _transcript(tmp_path / "w1.jsonl", thread_id="w1", parent="root", total=20),
        _transcript(tmp_path / "w2.jsonl", thread_id="w2", parent="root", total=30),
        _transcript(tmp_path / "planner.jsonl", thread_id="planner", parent=None, total=40),
    ]

    value = run_bench.attribute_transcripts(
        paths, root_id="root", expected_workers=2, planner_ids={"planner"}
    )

    assert value["root_usage"]["total_tokens"] == 10
    assert [row["total_tokens"] for row in value["worker_usage"]] == [20, 30]
    assert value["planner_transcript_ids"] == ["planner"]
    assert value["workers_overlap"] is True


def test_transcript_attribution_observes_but_does_not_require_common_worker_overlap(
    tmp_path: Path,
) -> None:
    root = _transcript(tmp_path / "root.jsonl", thread_id="root", parent=None, total=10)
    worker_one = _transcript(tmp_path / "w1.jsonl", thread_id="w1", parent="root", total=20)
    worker_two = _transcript(tmp_path / "w2.jsonl", thread_id="w2", parent="root", total=30)
    rows = [json.loads(line) for line in worker_two.read_text().splitlines()]
    for index, row in enumerate(rows):
        row["timestamp"] = f"2026-08-09T00:01:0{index}Z"
    worker_two.write_text("".join(json.dumps(row) + "\n" for row in rows))

    value = run_bench.attribute_transcripts(
        [root, worker_one, worker_two],
        root_id="root",
        expected_workers=2,
        planner_ids=set(),
    )

    assert value["workers_overlap"] is False


def test_transcript_attribution_rejects_unexplained_and_bad_usage(tmp_path: Path) -> None:
    paths = [
        _transcript(tmp_path / "root.jsonl", thread_id="root", parent=None, total=10),
        _transcript(tmp_path / "worker.jsonl", thread_id="worker", parent="root", total=20),
        _transcript(tmp_path / "other.jsonl", thread_id="other", parent=None, total=30),
    ]
    with pytest.raises(run_bench.MatrixError, match="unexplained"):
        run_bench.attribute_transcripts(
            paths, root_id="root", expected_workers=1, planner_ids=set()
        )

    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"type": "session_meta", "payload": {"id": "bad"}}) + "\n")
    with pytest.raises(run_bench.MatrixError, match="usage"):
        run_bench.parse_transcript(bad)


def test_transcript_parser_rejects_regressed_nonfinal_and_error_usage(tmp_path: Path) -> None:
    valid = _transcript(tmp_path / "valid.jsonl", thread_id="root", parent=None, total=20)
    rows = [json.loads(line) for line in valid.read_text().splitlines()]
    regressed = {
        **rows[1],
        "timestamp": "2026-08-09T00:00:01.500000Z",
        "payload": {
            **rows[1]["payload"],
            "info": {
                "total_token_usage": {
                    "input_tokens": 5,
                    "cached_input_tokens": 1,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 2,
                    "reasoning_output_tokens": 1,
                    "total_tokens": 7,
                }
            },
        },
    }
    rows.insert(2, regressed)
    valid.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(run_bench.MatrixError, match="regressed"):
        run_bench.parse_transcript(valid)

    nonfinal = _transcript(tmp_path / "nonfinal.jsonl", thread_id="root2", parent=None, total=20)
    nonfinal_rows = [json.loads(line) for line in nonfinal.read_text().splitlines()]
    nonfinal_rows.insert(
        -1,
        {
            "timestamp": "2026-08-09T00:00:01.500000Z",
            "type": "response_item",
            "payload": {"type": "message"},
        },
    )
    nonfinal.write_text("".join(json.dumps(row) + "\n" for row in nonfinal_rows))
    with pytest.raises(run_bench.MatrixError, match="model activity"):
        run_bench.parse_transcript(nonfinal)

    failed = _transcript(tmp_path / "failed.jsonl", thread_id="root3", parent=None, total=20)
    failed_rows = [json.loads(line) for line in failed.read_text().splitlines()]
    failed_rows.insert(
        -1,
        {
            "timestamp": "2026-08-09T00:00:01.500000Z",
            "type": "event_msg",
            "payload": {"type": "stream_error"},
        },
    )
    failed.write_text("".join(json.dumps(row) + "\n" for row in failed_rows))
    with pytest.raises(run_bench.MatrixError, match="visible error"):
        run_bench.parse_transcript(failed)

    mismatched = _transcript(
        tmp_path / "mismatched.jsonl", thread_id="root4", parent=None, total=20
    )
    mismatch_rows = [json.loads(line) for line in mismatched.read_text().splitlines()]
    mismatch_rows[1]["payload"]["info"]["last_token_usage"] = {
        "input_tokens": 1,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 1,
        "reasoning_output_tokens": 0,
        "total_tokens": 2,
    }
    mismatched.write_text("".join(json.dumps(row) + "\n" for row in mismatch_rows))
    with pytest.raises(run_bench.MatrixError, match="reconcile"):
        run_bench.parse_transcript(mismatched)


def test_invalid_attempt_recovery_preserves_visible_usage(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path / "worker.jsonl", thread_id="w", parent="r", total=20)
    rows = [json.loads(line) for line in transcript.read_text().splitlines()]
    rows.insert(
        1,
        {
            "timestamp": "2026-08-09T00:00:00.5Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.4-mini", "effort": "low"},
        },
    )
    rows.pop()  # interruption before task_complete must still preserve provider usage
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))

    value = run_bench.recover_attempt_usage([transcript])

    assert value["unquantified_consumption"] is False
    assert value["provider_visible_tokens"] == 20
    assert value["api_equivalent_dollars"] > 0


def test_ablation_rejects_ambient_role_model_override(tmp_path: Path, monkeypatch) -> None:
    codex = tmp_path / "codex"
    codex.write_text("binary")
    monkeypatch.setattr(run_bench, "_codex_binary", lambda: codex)
    monkeypatch.setenv("RRD_WORKER_MODEL", "gpt-5.5")

    with pytest.raises(run_bench.MatrixError, match="frozen RRD_WORKER_MODEL"):
        run_bench.run_ablation(tmp_path / "never-created")

    assert not (tmp_path / "never-created").exists()


def test_ablation_resume_rejects_every_frozen_schedule_and_config_drift(
    tmp_path: Path, monkeypatch
) -> None:
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\necho codex-cli 0.147.0\n")
    codex.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("sealed-config\n")
    monkeypatch.setattr(run_bench, "NATIVE_HOME", home)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_bench._ablation_manifest(run_dir, codex, seed=20260809)
    experiment = json.loads((run_dir / "experiment.json").read_text())

    assert experiment["randomization_seeds"] == run_bench.randomization_seeds(20260809)
    assert len(set(experiment["randomization_seeds"].values())) == 5
    run_bench.validate_ablation_resume(experiment, codex_bin=codex, seed=20260809)
    experiment["product_schedule"][0]["variant"] = "poison"
    with pytest.raises(run_bench.MatrixError, match="product_schedule"):
        run_bench.validate_ablation_resume(experiment, codex_bin=codex, seed=20260809)
    experiment = json.loads((run_dir / "experiment.json").read_text())
    (home / "config.toml").write_text("drifted-config\n")
    with pytest.raises(run_bench.MatrixError, match="config_sha256"):
        run_bench.validate_ablation_resume(experiment, codex_bin=codex, seed=20260809)


def test_planner_rows_deduplicate_identical_finals_and_reject_ambiguity(
    tmp_path: Path,
) -> None:
    usage = {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2}

    def event_file(name: str, inner: list[dict[str, object]]) -> Path:
        path = tmp_path / name
        path.write_text(
            json.dumps(
                {
                    "parse_status": "ok",
                    "exit_code": 0,
                    "stdout": "".join(json.dumps(row) + "\n" for row in inner),
                }
            )
            + "\n"
        )
        return path

    identical = event_file(
        "identical.jsonl",
        [
            {"type": "thread.started", "thread_id": "planner"},
            {"type": "turn.completed", "usage": usage},
            {"type": "turn.completed", "usage": usage},
        ],
    )
    _, identities, usages = run_bench._planner_rows(identical)
    assert identities == {"planner"}
    assert len(usages) == 1 and usages[0]["total_tokens"] == 12

    conflicting = event_file(
        "conflicting.jsonl",
        [
            {"type": "thread.started", "thread_id": "planner"},
            {"type": "turn.completed", "usage": usage},
            {
                "type": "turn.completed",
                "usage": {**usage, "output_tokens": 3},
            },
        ],
    )
    with pytest.raises(run_bench.MatrixError, match="conflicting final usage"):
        run_bench._planner_rows(conflicting)

    multi_identity = event_file(
        "multi-identity.jsonl",
        [
            {"type": "thread.started", "thread_id": "planner-a"},
            {"type": "thread.started", "thread_id": "planner-b"},
            {"type": "turn.completed", "usage": usage},
        ],
    )
    with pytest.raises(run_bench.MatrixError, match="exactly one call identity"):
        run_bench._planner_rows(multi_identity)

    visible_retry = event_file(
        "retry.jsonl",
        [
            {"type": "thread.started", "thread_id": "planner"},
            {"type": "request.retry"},
            {"type": "turn.completed", "usage": usage},
        ],
    )
    with pytest.raises(run_bench.MatrixError, match="visible failure or retry"):
        run_bench._planner_rows(visible_retry)


def test_lexical_quality_matches_unique_supported_users_findings() -> None:
    report = """## src/handlers/users.js
- medium | src/handlers/users.js:17-31 | Registration has missing password validation and hashes undefined passwords.
- high | src/handlers/users.js:87-94 | PATCH has an IDOR that lets a caller edit another user.
- medium | src/handlers/users.js:90-92 | PATCH accepts a duplicate email and malformed profile values.
"""

    value = run_bench.score_report(report, scenario="single-users")

    assert value["valid"] is True
    assert value["rubric_total"] == 3
    assert value["matched"] == 3
    assert value["lexical_recall"] == 1.0
    assert value["lexical_precision"] == 1.0
    assert value["handler_coverage"] is True
    assert value["category_coverage"] is True


def test_report_grammar_accepts_sentence_punctuation_inside_closing_quote() -> None:
    report = """## src/handlers/users.js
- high | src/handlers/users.js:82-95 | Any user can edit another profile despite the route being described as “self-service.”
"""

    value = run_bench.score_report(report, scenario="single-users")

    assert value["valid"] is True


def test_lexical_quality_penalizes_duplicate_wrong_line_and_severity_mismatch() -> None:
    report = """## src/handlers/users.js
- medium | src/handlers/users.js:17-31 | Registration has missing password validation.
- medium | src/handlers/users.js:17-31 | Registration has missing password validation.
- medium | src/handlers/users.js:87-94 | PATCH has an IDOR that lets a caller edit another user.
- high | src/handlers/users.js:1 | An unsupported high-severity claim uses the wrong line.
"""

    value = run_bench.score_report(report, scenario="single-users")

    assert value["valid"] is True
    assert value["matched"] == 1
    assert value["lexical_precision"] == 0.25
    assert len(value["unmatched_claims"]) == 3
    assert value["unmatched_high_or_critical"] is True


def test_lexical_quality_preserves_conjoined_claim_for_manual_review() -> None:
    report = """## src/handlers/users.js
- high | src/handlers/users.js:87-94 | PATCH has an IDOR that lets a caller edit another user and also proves an unrelated assertion.
"""

    value = run_bench.score_report(report, scenario="single-users")

    assert value["matched"] == 1
    assert value["semantic_review_required"] is True
    baseline = {"valid": True, "quality": {**value, "semantic_review_required": False}}
    assert run_bench._eligible({"valid": True, "quality": value}, baseline) is False


def test_combined_protocol_requires_expected_backend_handlers_and_branches() -> None:
    handlers = ("users", "products")
    hooks: list[dict[str, object]] = []
    for index, handler in enumerate(handlers):
        agent = f"agent-{index}"
        hooks.extend(
            [
                {
                    "event": "assignment",
                    "handler": f"src/handlers/{handler}.js",
                    "memory_backend": "sqlite",
                },
                {"event": "spawned", "agent_id": agent, "memory_backend": "sqlite"},
                {"event": "shared_context", "agent_id": agent, "memory_backend": "sqlite"},
                {"event": "result_final", "agent_id": agent, "memory_backend": "sqlite"},
                {"event": "compression_bypass", "agent_ids": [agent], "memory_backend": "sqlite"},
                {
                    "event": "wait_result",
                    "completed_agent_ids": [agent],
                    "result_count": 1,
                    "timed_out": False,
                    "memory_backend": "sqlite",
                },
            ]
        )
    hooks.append({"event": "root_merge", "chars": 10, "memory_backend": "sqlite"})
    packets = [
        {
            "event": "packet",
            "handler": "src/handlers/users.js",
            "branch": "miss",
            "memory_backend": "sqlite",
        },
        {
            "event": "packet",
            "handler": "src/handlers/products.js",
            "branch": "hit",
            "memory_backend": "sqlite",
        },
    ]

    value = run_bench.validate_combined_protocol(
        hooks=hooks,
        packets=packets,
        handlers=handlers,
        backend="sqlite",
        planner_calls=1,
    )

    assert value["valid"] is True
    packets[1]["branch"] = "miss"
    assert (
        run_bench.validate_combined_protocol(
            hooks=hooks,
            packets=packets,
            handlers=handlers,
            backend="sqlite",
            planner_calls=1,
        )["valid"]
        is False
    )


def test_factor_protocol_binds_exact_source_bundle_to_assignment() -> None:
    handler = "src/handlers/users.js"
    assignment_id = "assignment-users"
    files = []
    for relative in (handler, "src/models.js", "src/utils.js", "src/middleware.js"):
        raw = (run_bench.TARGET_TEMPLATE / relative).read_bytes()
        text = raw.decode("utf-8")
        files.append(
            {
                "path": relative,
                "raw_sha256": run_bench._sha(raw),
                "byte_count": len(raw),
                "line_count": len(text.splitlines()),
                "final_newline": text.endswith("\n"),
            }
        )
    handler_raw = (run_bench.TARGET_TEMPLATE / handler).read_bytes()
    hooks = [
        {
            "event": "assignment",
            "assignment_id": assignment_id,
            "handler": handler,
            "handler_sha256": run_bench._sha(handler_raw),
            "delivered_bytes": len(handler_raw),
            "rrc_enabled": False,
            "contextmesh_enabled": True,
            "memory_backend": "sqlite",
        },
        {
            "event": "source_bundle",
            "assignment_id": assignment_id,
            "handler": handler,
            "files": files,
            "memory_backend": "sqlite",
        },
        {"event": "spawned", "agent_id": "worker", "memory_backend": "sqlite"},
        {"event": "shared_context", "agent_id": "worker", "memory_backend": "sqlite"},
        {"event": "result_final", "agent_id": "worker", "memory_backend": "sqlite"},
        {
            "event": "wait_result",
            "completed_agent_ids": ["worker"],
            "result_count": 1,
            "timed_out": False,
            "memory_backend": "sqlite",
        },
        {"event": "compression_bypass", "agent_ids": ["worker"], "memory_backend": "sqlite"},
        {"event": "root_merge", "memory_backend": "sqlite"},
    ]

    value = run_bench.validate_factor_protocol(
        hooks=hooks,
        packets=[],
        handlers=("users",),
        backend="sqlite",
        planner_calls=0,
        rrc_enabled=False,
        contextmesh_enabled=True,
        deterministic=False,
    )
    assert value["valid"] is True

    hooks.insert(
        -2,
        {
            "event": "source_reread_violation",
            "agent_id": "worker",
            "tool_use_id": "call-read",
            "memory_backend": "sqlite",
        },
    )
    hooks.insert(
        -2,
        {
            "event": "policy_deny",
            "source": "hook",
            "error": "worker source was already delivered; file and shell tools are denied",
            "memory_backend": "sqlite",
        },
    )
    value = run_bench.validate_factor_protocol(
        hooks=hooks,
        packets=[],
        handlers=("users",),
        backend="sqlite",
        planner_calls=0,
        rrc_enabled=False,
        contextmesh_enabled=True,
        deterministic=False,
    )
    assert value["valid"] is True
    assert value["blocked_source_attempts"] == 1

    files.append(dict(files[0]))
    assert not run_bench.validate_factor_protocol(
        hooks=hooks,
        packets=[],
        handlers=("users",),
        backend="sqlite",
        planner_calls=0,
        rrc_enabled=False,
        contextmesh_enabled=True,
        deterministic=False,
    )["valid"]
    files.pop()

    files[0] = {**files[0], "path": "src/handlers/wrong.js"}
    poisoned = run_bench.validate_factor_protocol(
        hooks=hooks,
        packets=[],
        handlers=("users",),
        backend="sqlite",
        planner_calls=0,
        rrc_enabled=False,
        contextmesh_enabled=True,
        deterministic=False,
    )
    assert poisoned["valid"] is False


def test_root_protocol_requires_exact_worker_coordination_and_no_source_reads() -> None:
    handlers = ("users", "products")
    events = [
        {"type": "thread.started", "thread_id": "root"},
        {
            "type": "item.completed",
            "item": {
                "type": "collab_tool_call",
                "tool": "spawn_agent",
                "receiver_thread_ids": ["worker-users"],
                "prompt": "Audit src/handlers/users.js plus src/models.js, src/utils.js, and src/middleware.js.",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "collab_tool_call",
                "tool": "spawn_agent",
                "receiver_thread_ids": ["worker-products"],
                "prompt": "Audit src/handlers/products.js plus src/models.js, src/utils.js, and src/middleware.js.",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "collab_tool_call",
                "tool": "wait",
                "receiver_thread_ids": ["worker-users", "worker-products"],
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "the report"},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]

    value = run_bench.validate_root_protocol(
        events=events,
        handlers=handlers,
        worker_ids={"worker-users", "worker-products"},
        final="the report",
    )
    assert value["valid"] is True

    events.insert(
        -2,
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "cat src/handlers/users.js",
                "status": "completed",
            },
        },
    )
    assert (
        run_bench.validate_root_protocol(
            events=events,
            handlers=handlers,
            worker_ids={"worker-users", "worker-products"},
            final="the report",
        )["valid"]
        is False
    )


def test_root_protocol_requires_every_spawn_before_the_first_wait() -> None:
    handlers = ("users", "products")
    events = [
        {"type": "thread.started", "thread_id": "root"},
        {
            "type": "item.completed",
            "item": {
                "type": "collab_tool_call",
                "tool": "spawn_agent",
                "receiver_thread_ids": ["worker-users"],
                "prompt": "Audit src/handlers/users.js.",
                "status": "completed",
            },
        },
        {
            "type": "item.started",
            "item": {
                "type": "collab_tool_call",
                "tool": "wait",
                "receiver_thread_ids": ["worker-users"],
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "collab_tool_call",
                "tool": "wait",
                "receiver_thread_ids": ["worker-users"],
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "collab_tool_call",
                "tool": "spawn_agent",
                "receiver_thread_ids": ["worker-products"],
                "prompt": "Audit src/handlers/products.js.",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "collab_tool_call",
                "tool": "wait",
                "receiver_thread_ids": ["worker-products"],
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "the report"},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]

    value = run_bench.validate_root_protocol(
        events=events,
        handlers=handlers,
        worker_ids={"worker-users", "worker-products"},
        final="the report",
    )

    assert value["valid"] is False
    assert "root waited before all workers were spawned" in value["errors"]

    events[-3]["item"]["command"] = "find src -type f -exec cat {} ;"
    assert (
        run_bench.validate_root_protocol(
            events=events,
            handlers=handlers,
            worker_ids={"worker-users", "worker-products"},
            final="the report",
        )["valid"]
        is False
    )


def test_worker_source_protocol_requires_exact_direct_reads_and_zero_cm_tools(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "worker.jsonl"

    def write(commands: list[str]) -> None:
        rows = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": command}),
                },
            }
            for command in commands
        ]
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))

    exact = [
        "/usr/bin/nl -ba src/handlers/users.js",
        "/usr/bin/nl -ba src/models.js",
        "/usr/bin/nl -ba src/utils.js",
        "/usr/bin/nl -ba src/middleware.js",
    ]
    write(exact)
    row = {"thread_id": "worker-users", "path": str(transcript)}
    assert run_bench.validate_worker_source_protocol(
        transcripts=[row], assignments={"worker-users": "users"}, contextmesh_enabled=False
    )["valid"]
    write(["find src -type f -exec cat {} ;", *exact[1:]])
    assert not run_bench.validate_worker_source_protocol(
        transcripts=[row], assignments={"worker-users": "users"}, contextmesh_enabled=False
    )["valid"]
    write(exact)
    assert not run_bench.validate_worker_source_protocol(
        transcripts=[row], assignments={"worker-users": "users"}, contextmesh_enabled=True
    )["valid"]


def test_contextmesh_worker_source_attempt_must_be_proven_denied(tmp_path: Path) -> None:
    transcript = tmp_path / "worker.jsonl"
    call_id = "call-read"
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": call_id,
                "arguments": json.dumps({"cmd": "/usr/bin/nl -ba src/handlers/users.js"}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": "Command blocked by PreToolUse hook: worker source was already delivered; file and shell tools are denied. Command: /usr/bin/nl -ba src/handlers/users.js",
            },
        },
    ]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
    hooks = [
        {
            "event": "source_reread_violation",
            "agent_id": "worker-users",
            "tool_use_id": call_id,
        },
        {
            "event": "policy_deny",
            "source": "hook",
            "error": "worker source was already delivered; file and shell tools are denied",
        },
    ]

    value = run_bench.validate_worker_source_protocol(
        transcripts=[{"thread_id": "worker-users", "path": str(transcript)}],
        assignments={"worker-users": "users"},
        contextmesh_enabled=True,
        hooks=hooks,
    )
    assert value["valid"] is True
    assert value["blocked_source_attempts"] == 1

    canonical_output = rows[1]["payload"]["output"]  # type: ignore[index]
    rows[1]["payload"]["output"] = f"{canonical_output}\nL1: leaked source"  # type: ignore[index]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert not run_bench.validate_worker_source_protocol(
        transcripts=[{"thread_id": "worker-users", "path": str(transcript)}],
        assignments={"worker-users": "users"},
        contextmesh_enabled=True,
        hooks=hooks,
    )["valid"]

    rows[1]["payload"]["output"] = canonical_output  # type: ignore[index]
    duplicate_output = json.loads(json.dumps(rows[1]))
    duplicate_output["payload"]["output"] = "L1: leaked source"
    transcript.write_text(
        "".join(json.dumps(row) + "\n" for row in [rows[0], duplicate_output, rows[1]])
    )
    assert not run_bench.validate_worker_source_protocol(
        transcripts=[{"thread_id": "worker-users", "path": str(transcript)}],
        assignments={"worker-users": "users"},
        contextmesh_enabled=True,
        hooks=hooks,
    )["valid"]

    duplicate_call = json.loads(json.dumps(rows[0]))
    transcript.write_text(
        "".join(json.dumps(row) + "\n" for row in [rows[0], rows[1], duplicate_call, rows[1]])
    )
    assert not run_bench.validate_worker_source_protocol(
        transcripts=[{"thread_id": "worker-users", "path": str(transcript)}],
        assignments={"worker-users": "users"},
        contextmesh_enabled=True,
        hooks=hooks,
    )["valid"]

    orphan = json.loads(json.dumps(rows[1]))
    orphan["payload"]["call_id"] = "orphan"
    transcript.write_text("".join(json.dumps(row) + "\n" for row in [orphan, rows[0], rows[1]]))
    assert not run_bench.validate_worker_source_protocol(
        transcripts=[{"thread_id": "worker-users", "path": str(transcript)}],
        assignments={"worker-users": "users"},
        contextmesh_enabled=True,
        hooks=hooks,
    )["valid"]


def test_snapshot_artifacts_copies_regular_files_and_records_hashes(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    source.write_text('{"ok":true}\n')
    destination = tmp_path / "sealed"

    rows = run_bench.snapshot_artifacts([("events.jsonl", source)], destination=destination)

    copied = destination / "events.jsonl"
    assert copied.read_bytes() == source.read_bytes()
    assert copied.stat().st_mode & 0o777 == 0o600
    assert rows == [
        {
            "name": "events.jsonl",
            "bytes": len(source.read_bytes()),
            "sha256": run_bench._sha(source.read_bytes()),
        }
    ]


def test_incomplete_summary_is_quarantined_for_resume(tmp_path: Path) -> None:
    cell = tmp_path / "cells" / "single-users--baseline"
    cell.mkdir(parents=True)
    (cell / "summary.json").write_text('{"cell_id":"single-users--baseline"}\n')

    assert run_bench._resumable_cell(cell) is None
    assert not cell.exists()
    quarantined = list(cell.parent.glob(".single-users--baseline.interrupted-*-*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / "summary.json").exists()


def test_native_runtime_reset_removes_prior_sessions_only(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    preserved = home / "config.toml"
    preserved.write_text("config")
    sessions = home / "sessions" / "prior"
    sessions.mkdir(parents=True)
    (sessions / "poison.jsonl").write_text("combined poison")
    (home / "state_5.sqlite").write_text("prior state")
    monkeypatch.setattr(run_bench, "NATIVE_HOME", home)

    run_bench._reset_native_runtime_state()

    assert preserved.read_text() == "config"
    assert not (home / "sessions").exists()
    assert not (home / "state_5.sqlite").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS boundary")
def test_baseline_execution_profile_denies_intervention_reads_and_target_writes(
    tmp_path: Path, monkeypatch
) -> None:
    runs = tmp_path / "runs"
    prior = runs / "rrd-demo" / "prior"
    prior.mkdir(parents=True)
    poison = prior / "seed-manifest.json"
    poison.write_text("INTERVENTION-POISON")
    run_dir = runs / "matrix"
    cell = run_dir / "cells" / "single-users--baseline"
    target = cell / "target"
    target.mkdir(parents=True)
    allowed = target / "allowed.txt"
    allowed.write_text("allowed source")
    monkeypatch.setattr(run_bench, "RUNS", runs)
    profile = cell / "execution.sb"
    run_bench._write_text(
        profile,
        run_bench.execution_profile_text(cell=cell, target=target, run_dir=run_dir, baseline=True),
    )

    denied_read = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", profile, "/bin/cat", poison],
        capture_output=True,
        text=True,
        check=False,
    )
    denied_write = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", profile, "/usr/bin/touch", target / "new"],
        capture_output=True,
        text=True,
        check=False,
    )
    allowed_read = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", profile, "/bin/cat", allowed],
        capture_output=True,
        text=True,
        check=False,
    )

    assert denied_read.returncode != 0
    assert "INTERVENTION-POISON" not in denied_read.stdout
    assert denied_write.returncode != 0
    assert not (target / "new").exists()
    assert allowed_read.returncode == 0 and allowed_read.stdout == "allowed source"


def test_resume_validation_rejects_source_drift(tmp_path: Path, monkeypatch) -> None:
    codex = tmp_path / "codex"
    codex.write_text("binary")
    native_home = tmp_path / "native-home"
    native_home.mkdir()
    (native_home / "credential-deny.sb").write_text("profile")
    monkeypatch.setattr(run_bench, "NATIVE_HOME", native_home)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    experiment = {
        **run_bench.matrix(),
        "codex_binary": str(codex),
        "codex_binary_sha256": run_bench._sha(codex.read_bytes()),
        "rubric_sha256": run_bench.RUBRIC_SHA256,
        "sandbox_profile_sha256": run_bench._sha(
            (run_bench.NATIVE_HOME / "credential-deny.sb").read_bytes()
        ),
        "source_hashes": run_bench._source_hashes(),
        "target_hashes": run_bench._target_hashes(),
    }
    (run_dir / "experiment.json").write_text(json.dumps(experiment))

    run_bench.validate_resume(run_dir, codex)
    experiment["source_hashes"] = {"drift": "yes"}
    (run_dir / "experiment.json").write_text(json.dumps(experiment))
    with pytest.raises(run_bench.MatrixError, match="source hashes"):
        run_bench.validate_resume(run_dir, codex)


def test_aggregate_never_labels_a_token_increase_as_savings(tmp_path: Path) -> None:
    eligible_quality = {
        "lexical_recall": 1.0,
        "lexical_precision": 1.0,
        "handler_coverage": True,
        "category_coverage": True,
        "unmatched_high_or_critical": False,
    }
    for scenario, variant in run_bench.MATRIX_ORDER:
        cell = tmp_path / "cells" / f"{scenario}--{variant}"
        cell.mkdir(parents=True)
        tokens = 100 if variant == "baseline" else 120
        final = b"report\n"
        (cell / "final.md").write_bytes(final)
        inventories = {}
        for group in ("transcripts", "evidence"):
            directory = cell / group
            directory.mkdir()
            artifact = directory / "proof.txt"
            artifact.write_bytes(group.encode())
            inventories[group] = [
                {
                    "name": artifact.name,
                    "bytes": artifact.stat().st_size,
                    "sha256": run_bench._sha(artifact.read_bytes()),
                }
            ]
        run_bench._write_cell_summary(
            cell,
            {
                "cell_id": cell.name,
                "scenario": scenario,
                "variant": variant,
                "valid": True,
                "errors": [],
                "provider_visible_tokens": tokens,
                "quality": eligible_quality,
                "artifacts": inventories,
                "final_sha256": run_bench._sha(final),
            },
        )

    result = run_bench.aggregate(tmp_path)

    assert all(row["quality_eligible"] is True for row in result["comparisons"])
    assert all(row["savings_eligible"] is False for row in result["comparisons"])
    assert all(row["label"] == "observed delta" for row in result["comparisons"])

    first = tmp_path / "cells" / "single-users--baseline" / "evidence" / "proof.txt"
    first.write_text("mutated")
    with pytest.raises(run_bench.MatrixError, match="artifact seal mismatch"):
        run_bench.aggregate(tmp_path)


def test_matrix_lock_precedes_native_home_mutation(tmp_path: Path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    lock = runs / "native-matrix.lock"
    ready = tmp_path / "ready"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,os,sys,time; "
                "fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600); "
                "fcntl.flock(fd,fcntl.LOCK_EX); open(sys.argv[2],'w').close(); time.sleep(10)"
            ),
            str(lock),
            str(ready),
        ]
    )
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists()
        codex = tmp_path / "codex"
        codex.write_text("binary")
        monkeypatch.setattr(run_bench, "RUNS", runs)
        monkeypatch.setattr(run_bench, "_codex_binary", lambda: codex)
        called = False

        def fail_if_called(*_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("stable configuration mutated before the global lock")

        monkeypatch.setattr(run_bench, "_run_checked", fail_if_called)
        with pytest.raises(run_bench.MatrixError, match="another native matrix"):
            run_bench.run_matrix(tmp_path / "run")
        assert called is False
    finally:
        holder.terminate()
        holder.wait(timeout=2)
