# pyright: reportMissingImports=false
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "contextmesh/scripts"))
import rrcv2_v21_analyzer as analyzer  # noqa: E402
from rrc.contract import CostEventV1, canonical_json_bytes  # noqa: E402


def _evidence(
    *,
    model: str = "gpt-5.6-luna",
    input_tokens: int = 100,
    cached: int = 20,
    cache_write: int = 10,
    output: int = 5,
) -> analyzer.UsageEvidence:
    return analyzer.UsageEvidence(
        transcript_sha256="a" * 64,
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        cache_write_input_tokens=cache_write,
        output_tokens=output,
        reasoning_output_tokens=2,
        total_tokens=input_tokens + output,
        effective_model=model,
        effective_reasoning="low",
        source="fixture",
        turns=(
            analyzer.TokenSlice(
                input_tokens=input_tokens,
                cached_input_tokens=cached,
                cache_write_input_tokens=cache_write,
                output_tokens=output,
                reasoning_output_tokens=2,
                total_tokens=input_tokens + output,
            ),
        ),
        turn_usage_attested=True,
    )


def test_fast_luna_cost_partitions_input_without_double_counting() -> None:
    cost, error = analyzer.price_call(model="gpt-5.6-luna", usage=_evidence())
    assert error is None
    assert cost == 70 * 400 + 20 * 40 + 10 * 500 + 5 * 2_400


def test_fast_luna_uses_long_context_rates_at_threshold() -> None:
    usage = _evidence(input_tokens=272_000, cached=2, cache_write=3, output=7)
    cost, error = analyzer.price_call(model="gpt-5.6-luna", usage=usage)
    assert error is None
    assert cost == (272_000 - 5) * 800 + 2 * 80 + 3 * 1_000 + 7 * 3_600


def test_gpt55_cost_fails_closed_for_write_or_long_context() -> None:
    cost, error = analyzer.price_call(model="gpt-5.5", usage=_evidence(cache_write=1))
    assert cost is None and "no separate cache-write" in str(error)
    cost, error = analyzer.price_call(
        model="gpt-5.5", usage=_evidence(input_tokens=272_000, cache_write=0)
    )
    assert cost is None and "below 272K" in str(error)


def test_gpt55_cache_write_sensitivity_aggregates_without_claiming_a_price() -> None:
    usage = _evidence(model="gpt-5.5", cache_write=10)
    scenarios = analyzer._price_scenario_nanodollars(model="gpt-5.5", usage=usage)  # noqa: SLF001
    row = {
        "api_equivalent_fast_nanodollars": None,
        "cost_scenario_nanodollars": scenarios,
        "ordinary_input_tokens": usage.ordinary_input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_input_tokens": usage.cache_write_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
        "provider_visible_tokens": usage.total_tokens,
        "input_tokens": usage.input_tokens,
    }
    total = analyzer._sum_rows([row])  # noqa: SLF001
    assert total["api_equivalent_fast_dollars"] is None
    assert total["scenario_api_equivalent_fast_dollars"] == {
        "cache_write_priced_as_cached_input_dollars": "0.001287500",
        "cache_write_priced_as_uncached_input_dollars": "0.001400000",
    }


def test_gpt55_prices_large_cumulative_session_from_short_turns() -> None:
    turns = (
        analyzer.TokenSlice(150_000, 100_000, 0, 10, 2, 150_010),
        analyzer.TokenSlice(150_000, 120_000, 0, 20, 4, 150_020),
    )
    usage = analyzer.UsageEvidence(
        transcript_sha256="b" * 64,
        input_tokens=300_000,
        cached_input_tokens=220_000,
        cache_write_input_tokens=0,
        output_tokens=30,
        reasoning_output_tokens=6,
        total_tokens=300_030,
        effective_model="gpt-5.5",
        effective_reasoning="medium",
        source="fixture",
        turns=turns,
        turn_usage_attested=True,
    )
    cost, error = analyzer.price_call(model="gpt-5.5", usage=usage)
    assert error is None
    assert cost == 80_000 * 12_500 + 220_000 * 1_250 + 30 * 75_000


def test_native_transcript_reopens_effective_identity_and_cache_write() -> None:
    rows = [
        {"type": "session_meta", "payload": {"id": "session"}},
        {"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "low"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 20,
                        "cache_write_input_tokens": 10,
                        "output_tokens": 5,
                        "reasoning_output_tokens": 2,
                        "total_tokens": 105,
                    }
                },
            },
        },
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    raw = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    evidence = analyzer.parse_native_transcript(raw, source="fixture")
    assert evidence.effective_model == "gpt-5.6-luna"
    assert evidence.ordinary_input_tokens == 70
    assert evidence.cache_write_input_tokens == 10
    assert evidence.turn_usage_attested is False
    assert analyzer.price_call(model="gpt-5.6-luna", usage=evidence) == (
        None,
        "native transcript omitted exact last-turn usage",
    )


def test_codex_exec_transcript_recovers_exact_turn_completion_usage() -> None:
    rows = [
        {"type": "thread.started", "thread_id": "thread"},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "cache_write_input_tokens": 0,
                "output_tokens": 5,
                "reasoning_output_tokens": 2,
            },
        },
    ]
    evidence = analyzer.parse_codex_exec_transcript(
        ("\n".join(json.dumps(row) for row in rows) + "\n").encode(), source="root-events"
    )
    assert evidence.total_tokens == 105
    assert evidence.turn_usage_attested is True


def test_native_transcript_accepts_exact_stop_time_snapshot_before_task_complete() -> None:
    rows = [
        {"type": "turn_context", "payload": {"model": "gpt-5.5", "effort": "medium"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 12,
                        "cached_input_tokens": 2,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 3,
                        "reasoning_output_tokens": 1,
                        "total_tokens": 15,
                    },
                    "last_token_usage": {
                        "input_tokens": 12,
                        "cached_input_tokens": 2,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 3,
                        "reasoning_output_tokens": 1,
                        "total_tokens": 15,
                    },
                },
            },
        },
    ]
    evidence = analyzer.parse_native_transcript(
        ("\n".join(json.dumps(row) for row in rows) + "\n").encode(), source="stop-snapshot"
    )
    assert evidence.total_tokens == 15
    assert evidence.effective_model == "gpt-5.5"


def test_native_transcript_rejects_last_turn_that_differs_from_cumulative_delta() -> None:
    rows = [
        {"type": "turn_context", "payload": {"model": "gpt-5.5", "effort": "medium"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 2,
                        "reasoning_output_tokens": 1,
                        "total_tokens": 12,
                    },
                    "last_token_usage": {
                        "input_tokens": 9,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 3,
                        "reasoning_output_tokens": 1,
                        "total_tokens": 12,
                    },
                },
            },
        },
    ]
    with pytest.raises(analyzer.AnalysisError, match="last-turn usage differs"):
        analyzer.parse_native_transcript(
            ("\n".join(json.dumps(row) for row in rows) + "\n").encode(), source="fixture"
        )


def test_native_transcript_rejects_missing_cache_write_attestation() -> None:
    incomplete = {
        "input_tokens": 100,
        "cached_input_tokens": 80,
        "output_tokens": 5,
        "reasoning_output_tokens": 2,
        "total_tokens": 105,
    }
    rows = [
        {"type": "turn_context", "payload": {"model": "gpt-5.5", "effort": "medium"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": incomplete,
                    "last_token_usage": incomplete,
                },
            },
        },
    ]
    with pytest.raises(analyzer.AnalysisError, match="usage row"):
        analyzer.parse_native_transcript(
            ("\n".join(json.dumps(row) for row in rows) + "\n").encode(), source="fixture"
        )


def test_native_transcript_rejects_overlapping_cache_categories() -> None:
    value = _evidence(cached=70, cache_write=40)
    with pytest.raises(analyzer.AnalysisError, match="usage arithmetic"):
        analyzer._usage(  # noqa: SLF001
            {
                "input_tokens": value.input_tokens,
                "cached_input_tokens": value.cached_input_tokens,
                "cache_write_input_tokens": value.cache_write_input_tokens,
                "output_tokens": value.output_tokens,
                "reasoning_output_tokens": value.reasoning_output_tokens,
                "total_tokens": value.total_tokens,
            }
        )


def test_svg_separates_models_and_token_categories() -> None:
    report = {
        "by_requested_model": {
            "gpt-5.5": {
                "api_equivalent_fast_dollars": "1.250000000",
                "calls": 2,
                "ordinary_input_tokens": 50,
                "cached_input_tokens": 30,
                "cache_write_input_tokens": 0,
                "output_tokens": 20,
                "provider_visible_tokens": 100,
            },
            "gpt-5.6-luna": {
                "api_equivalent_fast_dollars": "0.100000000",
                "calls": 1,
                "ordinary_input_tokens": 40,
                "cached_input_tokens": 5,
                "cache_write_input_tokens": 5,
                "output_tokens": 10,
                "provider_visible_tokens": 60,
            },
        },
        "total": {
            "api_equivalent_fast_dollars": "1.350000000",
            "known_api_equivalent_fast_dollars": "1.350000000",
        },
    }
    svg = analyzer.render_svg(report)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "gpt-5.5" in svg and "gpt-5.6-luna" in svg
    assert "ordinary input" in svg and "cache write" in svg
    assert "Overall API-equivalent Fast cost: $1.350000000" in svg


def test_failure_recovery_groups_pre_stop_root_usage_and_renders_known_cost(
    tmp_path: Path,
) -> None:
    producer_identity = "f" * 64
    root = tmp_path / producer_identity
    cell = root / "round/miss"
    cell.mkdir(parents=True)
    rows = [
        {"type": "thread.started", "thread_id": "thread"},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "cache_write_input_tokens": 0,
                "output_tokens": 5,
                "reasoning_output_tokens": 2,
            },
        },
    ]
    (cell / "root-events.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    report = analyzer.recover_failure(root, RuntimeError("injected failure"))
    assert report["by_case"]["rrcv2-cli-miss-001"]["provider_visible_tokens"] == 105
    assert report["by_stage"]["contextmesh_root_session"]["input_tokens"] == 100
    assert report["total"]["provider_visible_tokens"] is None
    assert report["total"]["known_provider_visible_tokens"] == 105
    assert report["total"]["known_api_equivalent_fast_dollars"] == "0.000725000"
    markdown = analyzer.render_markdown(report)
    svg = analyzer.render_svg(report)
    assert "Known cost subtotal: 0.000725000" in markdown
    assert "known subtotal $0.000725000" in svg


def test_failure_recovery_preserves_usage_before_visible_provider_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "producer"
    cell = root / "round/miss"
    cell.mkdir(parents=True)
    native_rows = [
        {"type": "turn_context", "payload": {"model": "gpt-5.5", "effort": "medium"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 5,
                        "reasoning_output_tokens": 2,
                        "total_tokens": 105,
                    },
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 5,
                        "reasoning_output_tokens": 2,
                        "total_tokens": 105,
                    },
                },
            },
        },
        {"type": "event_msg", "payload": {"type": "turn_failed", "error": "provider"}},
    ]
    (cell / "root-transcript.jsonl").write_text(
        "\n".join(json.dumps(row) for row in native_rows) + "\n"
    )
    report = analyzer.recover_failure(root, RuntimeError("injected failure"))
    assert report["total"]["known_provider_visible_tokens"] == 105
    assert any(
        row.get("kind") == "root_transcript"
        and row.get("status") == "recovered_with_visible_failure"
        for row in report["recovery_inventory"]
    )
    assert any("visible provider failure" in value for value in report["unquantified_components"])


def test_failure_path_writes_canonical_json_markdown_svg_and_inventory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "producer"
    root.mkdir()
    (root / "terminal.json").write_text(
        json.dumps(
            {
                "producer_sha256": "a" * 64,
                "returncode": 1,
                "status": "failure",
                "v": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    assert analyzer.main([str(root)]) == 1
    report = json.loads((root / "v21-analysis.v1.json").read_bytes())
    assert report["analysis_status"] == "failure_or_incomplete"
    assert report["total"]["provider_visible_tokens"] is None
    assert report["unquantified_components"]
    assert (root / "v21-report.md").read_text().startswith("# RRCv2 V21")
    assert (root / "v21-model-economics.svg").read_text().startswith("<svg")
    inventory = json.loads((root / "v21-analysis-inventory.v1.json").read_bytes())
    assert [row["path"] for row in inventory["artifacts"]] == [
        "v21-analysis.v1.json",
        "v21-model-economics.svg",
        "v21-report.md",
    ]
    assert analyzer.main([str(root)]) == 1
    report_path = root / "v21-analysis.v1.json"
    report_path.write_bytes(b"conflict")
    with pytest.raises(analyzer.AnalysisError, match="conflicting immutable"):
        analyzer.main([str(root)])


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _native_raw(model: str, reasoning: str, ordinal: int) -> bytes:
    usage = {
        "input_tokens": 100 + ordinal,
        "cached_input_tokens": 20,
        "cache_write_input_tokens": 0,
        "output_tokens": 5,
        "reasoning_output_tokens": 2,
        "total_tokens": 105 + ordinal,
    }
    rows = [
        {"type": "turn_context", "payload": {"model": model, "effort": reasoning}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": usage, "total_token_usage": usage},
            },
        },
    ]
    return ("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n").encode()


def test_complete_three_cell_success_reconciles_combined_sessions_and_groups(
    tmp_path: Path,
) -> None:
    producer_identity = "f" * 64
    root = tmp_path / producer_identity
    round_root = root / "round"
    round_root.mkdir(parents=True)
    launch = ROOT / ".generated/state/rrcv2-convergence/verify/v21-launch-manifest.v1.json"
    price = ROOT / ".generated/state/rrcv2-convergence/economic/openai-pricing-20260812.v1.json"
    producer = {
        "experiment_id": "rrcv2-cli-smoke-v21",
        "fixture_manifest_sha256": "a" * 64,
        "launch_manifest_sha256": hashlib.sha256(launch.read_bytes()).hexdigest(),
        "predecessor_manifest_sha256": "b" * 64,
        "price_authority_sha256": hashlib.sha256(price.read_bytes()).hexdigest(),
        "producer_sha256": producer_identity,
        "round_token": f"rrcv2-cli-smoke-{producer_identity[:32]}",
        "session_plan_sha256": "c" * 64,
        "session_review_sha256": "d" * 64,
        "v": 21,
    }
    (root / "producer.json").write_bytes(canonical_json_bytes(producer))
    (root / "producer.json").chmod(0o600)
    (root / "terminal.json").write_bytes(
        canonical_json_bytes(
            {
                "producer_sha256": producer_identity,
                "returncode": 0,
                "status": "success",
                "v": 1,
            }
        )
    )
    (root / "terminal.json").chmod(0o600)
    database = round_root / "rrcv2.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE rrcv2p_cells(
            cell_id TEXT,root_cost_event BLOB,combined_session BLOB,
            state TEXT DEFAULT 'combined_committed'
        );
        CREATE TABLE rrcv2_calls(
            call_id TEXT,cost_event BLOB,state TEXT DEFAULT 'call_committed'
        );
        CREATE TABLE rrcv2p_attempts_bindings(transcript BLOB);
        CREATE TABLE rrcv2_oracle_scores(attempt_id TEXT,status TEXT,score INTEGER);
        CREATE TABLE rrcv2_attempts(attempt_id TEXT,state TEXT);
        CREATE TABLE rrcv2_acceptance_markers(attempt_id TEXT,outcome_sha256 TEXT);
        CREATE TABLE rrcv2_receipts(attempt_id TEXT,receipt TEXT);
        CREATE TABLE rrcv2p_cell_tool_events(
            cell_id TEXT,sequence INTEGER,kind TEXT,event_sha256 TEXT,output BLOB
        );
        """
    )
    cases = (
        ("miss", "rrcv2-cli-miss-001", ("spec", "independent_tests", "implement")),
        ("hit", "rrcv2-cli-hit-001", ("prime",)),
        ("near", "rrcv2-cli-near-001", ("spec", "independent_tests", "implement")),
    )
    summary_cells: list[dict[str, object]] = []
    for case_index, (case, task_id, stages) in enumerate(cases, 1):
        cell_id = f"cell-{case}"
        cell = round_root / case
        cell.mkdir()
        root_raw = _native_raw("gpt-5.5", "medium", case_index)
        (cell / "root-transcript.jsonl").write_bytes(root_raw)
        event_ids: list[str] = []

        def event(stage: str, ordinal: int, model: str, reasoning: str, digest: str) -> CostEventV1:
            value = CostEventV1(
                cost_event_id=_digest(f"{case}-{stage}-cost"),
                cell_id=cell_id,
                attempt_id=None
                if stage == "contextmesh_root_session"
                else _digest(f"{case}-attempt"),
                task_id=task_id,
                arm="rrc_warm",
                stage=stage,
                stage_ordinal=ordinal,
                prompt_sha256=_digest(f"{case}-{stage}-prompt"),
                final_message_sha256=_digest(f"{case}-{stage}-final"),
                transcript_sha256=digest,
                requested_provider="openai",
                requested_model=model,
                requested_reasoning=reasoning,
                requested_service_tier="priority",
                identity_attestation="usage_only",
                effective_provider="unattested",
                effective_model="unattested",
                effective_reasoning="unattested",
                effective_service_tier="unattested",
                input_tokens=100 + case_index,
                cached_input_tokens=20,
                output_tokens=5,
                reasoning_output_tokens=2,
                provider_total_tokens=105 + case_index,
            )
            event_ids.append(value.cost_event_id)
            return value

        root_event = event(
            "contextmesh_root_session", 1, "gpt-5.5", "medium", hashlib.sha256(root_raw).hexdigest()
        )
        planner_rows: list[dict[str, object]] = []
        for ordinal, stage in enumerate(stages, 2):
            model = "gpt-5.5" if stage == "spec" else "gpt-5.6-luna"
            reasoning = "low"
            if stage == "implement":
                worker_raw = _native_raw(model, reasoning, case_index)
                connection.execute(
                    "INSERT INTO rrcv2p_attempts_bindings(transcript) VALUES(?)", (worker_raw,)
                )
                digest = hashlib.sha256(worker_raw).hexdigest()
            else:
                digest = _digest(f"{case}-{stage}-transcript")
                planner_rows.append(
                    {
                        "arm": "rrc_warm",
                        "model": model,
                        "prompt": "prompt",
                        "requested_reasoning": reasoning,
                        "requested_service_tier": "priority",
                        "response": "response",
                        "role": "planner",
                        "stage": stage,
                        "task_id": task_id,
                        "transcript_sha256": digest,
                        "usage": {
                            "cached_input_tokens": 20,
                            "cache_write_input_tokens": 0,
                            "completion_tokens": 5,
                            "prompt_tokens": 100 + case_index,
                            "reasoning_output_tokens": 2,
                            "total_tokens": 105 + case_index,
                        },
                    }
                )
            stage_event = event(stage, ordinal, model, reasoning, digest)
            connection.execute(
                "INSERT INTO rrcv2_calls(call_id,cost_event) VALUES(?,?)",
                (stage_event.cost_event_id, stage_event.canonical_bytes()),
            )
        if planner_rows:
            (cell / "rrc-model-events.jsonl").write_text(
                "\n".join(json.dumps(row, separators=(",", ":")) for row in planner_rows) + "\n"
            )
        combined = {
            "all_cost_event_ids": sorted(event_ids),
            "attempts": [
                {
                    "apply_event_sha256": _digest(f"{case}-apply"),
                    "attempt_id": _digest(f"{case}-attempt"),
                    "receipt": _digest(f"{case}-receipt"),
                    "terminal_kind": "accepted",
                    "terminal_outcome_sha256": _digest(f"{case}-terminal"),
                }
            ],
            "cell_id": cell_id,
            "root_cost_event_id": root_event.cost_event_id,
            "root_final_sha256": root_event.final_message_sha256,
            "root_session_id": _digest(f"{case}-session"),
            "root_transcript_sha256": root_event.transcript_sha256,
            "round_id": "round",
            "tool_event_sha256s": [_digest(f"{case}-tool")],
            "v": 1,
            "wait_envelope_sha256": _digest(f"{case}-wait-envelope"),
            "wait_id": _digest(f"{case}-wait"),
        }
        connection.execute(
            "INSERT INTO rrcv2p_cells(cell_id,root_cost_event,combined_session) VALUES(?,?,?)",
            (cell_id, root_event.canonical_bytes(), canonical_json_bytes(combined) + b"\n"),
        )
        connection.execute(
            "INSERT INTO rrcv2_oracle_scores VALUES(?,?,?)",
            (_digest(f"{case}-attempt"), "passed", 1),
        )
        connection.execute(
            "INSERT INTO rrcv2_attempts VALUES(?,?)",
            (_digest(f"{case}-attempt"), "accepted"),
        )
        connection.execute(
            "INSERT INTO rrcv2_acceptance_markers VALUES(?,?)",
            (_digest(f"{case}-attempt"), _digest(f"{case}-terminal")),
        )
        connection.execute(
            "INSERT INTO rrcv2_receipts VALUES(?,?)",
            (_digest(f"{case}-attempt"), _digest(f"{case}-receipt")),
        )
        connection.execute(
            "INSERT INTO rrcv2p_cell_tool_events VALUES(?,?,?,?,?)",
            (
                cell_id,
                1,
                "apply",
                _digest(f"{case}-apply"),
                canonical_json_bytes(
                    {
                        "attempt_id": _digest(f"{case}-attempt"),
                        "receipt": _digest(f"{case}-receipt"),
                    }
                ),
            ),
        )
        summary_cells.append(
            {
                "all_cost_event_ids": sorted(event_ids),
                "attempt_id": _digest(f"{case}-attempt"),
                "branch": "reuse" if case == "hit" else "miss",
                "cell_id": cell_id,
                "deterministic_stages": (
                    ["cache_render_rejection", "tier_minus_one"] if case == "near" else []
                ),
                "root_cost_event_id": root_event.cost_event_id,
                "task_id": task_id,
                "terminal_kind": "accepted",
            }
        )
    connection.commit()
    connection.close()
    (round_root / "summary.json").write_bytes(
        canonical_json_bytes({"cells": summary_cells, "kind": "rrcv2_cli_smoke_summary", "v": 1})
    )
    report = analyzer.analyze(root)
    assert report["analysis_status"] == "functional_success"
    assert report["total"]["calls"] == 10
    assert (
        report["total"]["input_tokens"] + report["total"]["output_tokens"]
        == report["total"]["provider_visible_tokens"]
    )
    assert set(report["by_requested_model"]) == {"gpt-5.5", "gpt-5.6-luna"}
    assert set(report["by_case"]) == {task_id for _case, task_id, _stages in cases}
    assert set(report["by_stage"]) == {
        "contextmesh_root_session",
        "implement",
        "independent_tests",
        "prime",
        "spec",
    }

    terminal_path = root / "terminal.json"
    terminal_raw = terminal_path.read_bytes()
    terminal_path.write_bytes(
        canonical_json_bytes(
            {"producer_sha256": "e" * 64, "returncode": 0, "status": "success", "v": 1}
        )
    )
    with pytest.raises(analyzer.AnalysisError, match="terminal receipt"):
        analyzer.analyze(root)
    terminal_path.write_text(
        json.dumps(
            {
                "producer_sha256": producer_identity,
                "returncode": 0,
                "status": "success",
                "v": 1,
            },
            indent=2,
        )
    )
    with pytest.raises(analyzer.AnalysisError, match="terminal receipt"):
        analyzer.analyze(root)
    terminal_path.write_bytes(terminal_raw)

    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO rrcv2_calls(call_id,cost_event,state) VALUES(?,?,?)",
        (_digest("unquantified-extra-call"), None, "call_started"),
    )
    connection.commit()
    connection.close()
    with pytest.raises(analyzer.AnalysisError, match="durable call inventory"):
        analyzer.analyze(root)
    connection = sqlite3.connect(database)
    connection.execute(
        "DELETE FROM rrcv2_calls WHERE call_id=?", (_digest("unquantified-extra-call"),)
    )
    connection.commit()
    connection.close()

    connection = sqlite3.connect(database)
    rows = connection.execute(
        "SELECT rowid,combined_session FROM rrcv2p_cells ORDER BY rowid"
    ).fetchall()
    original_rows = list(rows)
    for rowid, raw in rows:
        value = json.loads(raw)
        value["attempts"] = [
            {
                **value["attempts"][0],
                "apply_event_sha256": None,
                "receipt": None,
                "terminal_kind": "rejected",
            }
        ]
        connection.execute(
            "UPDATE rrcv2p_cells SET combined_session=? WHERE rowid=?",
            (canonical_json_bytes(value) + b"\n", rowid),
        )
    connection.commit()
    connection.close()
    with pytest.raises(analyzer.AnalysisError, match="functional terminal"):
        analyzer.analyze(root)

    connection = sqlite3.connect(database)
    for rowid, raw in original_rows:
        connection.execute("UPDATE rrcv2p_cells SET combined_session=? WHERE rowid=?", (raw, rowid))
    connection.execute(
        "UPDATE rrcv2_oracle_scores SET attempt_id=? WHERE attempt_id=?",
        (_digest("unrelated-attempt"), _digest("hit-attempt")),
    )
    connection.commit()
    connection.close()
    with pytest.raises(analyzer.AnalysisError, match="functional terminal"):
        analyzer.analyze(root)

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE rrcv2_oracle_scores SET attempt_id=? WHERE attempt_id=?",
        (_digest("hit-attempt"), _digest("unrelated-attempt")),
    )
    call_rows = connection.execute(
        "SELECT rowid,cost_event FROM rrcv2_calls ORDER BY rowid"
    ).fetchall()
    for rowid, raw in call_rows:
        value = json.loads(raw)
        if value["stage"] == "spec":
            value["stage_ordinal"] = 4
        elif value["stage"] == "implement":
            value["stage_ordinal"] = 2
        else:
            continue
        connection.execute(
            "UPDATE rrcv2_calls SET cost_event=? WHERE rowid=?",
            (canonical_json_bytes(value), rowid),
        )
    connection.commit()
    connection.close()
    with pytest.raises(analyzer.AnalysisError, match="functional terminal"):
        analyzer.analyze(root)

    connection = sqlite3.connect(database)
    for rowid, raw in call_rows:
        connection.execute("UPDATE rrcv2_calls SET cost_event=? WHERE rowid=?", (raw, rowid))
    root_rows = connection.execute(
        "SELECT rowid,root_cost_event FROM rrcv2p_cells ORDER BY rowid"
    ).fetchall()
    swapped_tasks = {
        "rrcv2-cli-miss-001": "rrcv2-cli-near-001",
        "rrcv2-cli-near-001": "rrcv2-cli-miss-001",
    }
    for rowid, raw in root_rows:
        value = json.loads(raw)
        if value["task_id"] in swapped_tasks:
            value["task_id"] = swapped_tasks[value["task_id"]]
            connection.execute(
                "UPDATE rrcv2p_cells SET root_cost_event=? WHERE rowid=?",
                (canonical_json_bytes(value), rowid),
            )
    connection.commit()
    connection.close()
    with pytest.raises(analyzer.AnalysisError, match="functional terminal"):
        analyzer.analyze(root)
