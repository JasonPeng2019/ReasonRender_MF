# pyright: reportMissingImports=false
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_v21_published_analysis_is_immutable_and_not_v22_authority() -> None:
    root = (
        ROOT
        / ".generated/state/rrcv2-convergence/verify/cli-smoke/rrcv2-cli-smoke-v21"
        / "47513926018fbb6544b9390ea8ddcf0e13efef37f0b9f28b503feeb4a81db3ff"
    )
    expected = {
        "v21-analysis.v1.json": "29539dae5cfb7e064737276cf6b90dd2058f125557ea8698b35742030cd989e3",
        "v21-analysis-inventory.v1.json": "98174da35dd2baaa237fb2c2051dccb60f425a29d5a6681fd4019056c2dbf1da",
        "v21-model-economics.svg": "8aa074cf04e6a41cc5b785a7dfcbc5397eb366c03a440eed791294f67797641d",
        "v21-report.md": "f95a5310fb6e9980c820b2b9deea4fe1cbe31a89c57dc3d913076d2ca24723a9",
    }
    assert {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in expected
    } == expected
    with pytest.raises(analyzer.AnalysisError, match="V22 producer root identity"):
        analyzer._authority_hashes(root)  # noqa: SLF001


sys.path.insert(0, str(ROOT / "contextmesh/scripts"))
import rrcv2_product_guard as product_guard  # noqa: E402
import rrcv2_v22_analyzer as analyzer  # noqa: E402
from rrc.contract import CostEventV1, canonical_json_bytes  # noqa: E402


def _write_v22_producer(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "producer.json"
    path.write_bytes(product_guard.producer_value())
    path.chmod(0o600)


def test_v22_entrypoint_rejects_v21_root_without_writing(tmp_path: Path) -> None:
    source = (
        ROOT
        / ".generated/state/rrcv2-convergence/verify/cli-smoke/rrcv2-cli-smoke-v21"
        / "47513926018fbb6544b9390ea8ddcf0e13efef37f0b9f28b503feeb4a81db3ff"
    )
    root = tmp_path / source.name
    shutil.copytree(source, root)
    before = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }
    with pytest.raises(analyzer.AnalysisError, match="V22 producer root identity"):
        analyzer.main([str(root)])
    after = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not any(path.name.startswith("v22-") for path in root.iterdir())


@pytest.mark.parametrize(
    "field",
    [
        "fixture_manifest_sha256",
        "launch_manifest_sha256",
        "predecessor_manifest_sha256",
        "price_authority_sha256",
        "session_plan_sha256",
        "session_review_sha256",
    ],
)
def test_v22_entrypoint_rejects_mutated_producer_field_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    root = tmp_path / product_guard.PRODUCER_SHA256
    _write_v22_producer(root)
    monkeypatch.setattr(analyzer, "_EXPECTED_PRODUCER_ROOT", root)
    producer_path = root / "producer.json"
    producer = json.loads(producer_path.read_bytes())
    producer[field] = "0" * 64
    producer_path.write_bytes(canonical_json_bytes(producer))
    terminal = root / "terminal.json"
    terminal.write_bytes(
        canonical_json_bytes(
            {
                "producer_sha256": product_guard.PRODUCER_SHA256,
                "returncode": 1,
                "status": "failure",
                "v": 1,
            }
        )
    )
    terminal.chmod(0o600)
    before = {path.name for path in root.iterdir()}
    with pytest.raises(analyzer.AnalysisError, match="V22 economic authority linkage"):
        analyzer.main([str(root)])
    assert {path.name for path in root.iterdir()} == before
    assert not any(path.name.startswith("v22-") for path in root.iterdir())


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


def test_planner_identity_rejects_unattested_effective_model_claim() -> None:
    event = CostEventV1(
        cost_event_id="1" * 64,
        cell_id="cell",
        attempt_id="2" * 64,
        arm="rrc_warm",
        task_id="task",
        stage="spec",
        stage_ordinal=2,
        prompt_sha256="3" * 64,
        final_message_sha256="4" * 64,
        transcript_sha256="5" * 64,
        requested_provider="openai",
        requested_model="gpt-5.5",
        requested_reasoning="low",
        requested_service_tier="priority",
        identity_attestation="native_partial",
        effective_provider="unattested",
        effective_model="gpt-5.5",
        effective_reasoning="unattested",
        effective_service_tier="unattested",
        input_tokens=1,
        cached_input_tokens=0,
        output_tokens=1,
        reasoning_output_tokens=0,
        provider_total_tokens=2,
    )
    log = {
        "stage": "spec",
        "task_id": "task",
        "model": "gpt-5.5",
        "requested_reasoning": "low",
        "requested_service_tier": "priority",
    }
    with pytest.raises(analyzer.AnalysisError, match="planner evidence identity"):
        analyzer._validate_planner_identity(event, log)  # noqa: SLF001


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
    assert set(report["by_requested_model"]) == {"gpt-5.5"}
    assert any("orphan provider usage" in value for value in report["unquantified_components"])
    markdown = analyzer.render_markdown(report)
    svg = analyzer.render_svg(report)
    assert "Known cost subtotal: 0.000725000" in markdown
    assert "gpt-5.6-luna" not in markdown
    assert "known subtotal $0.000725000" in svg


def test_failure_recovery_reports_requested_model_conflict_without_losing_usage(
    tmp_path: Path,
) -> None:
    root = tmp_path / ("f" * 64)
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
    raw = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    (cell / "root-events.jsonl").write_bytes(raw)
    event = CostEventV1(
        cost_event_id=_digest("conflict-cost"),
        cell_id="cell-miss",
        attempt_id=None,
        task_id="rrcv2-cli-miss-001",
        arm="rrc_warm",
        stage="contextmesh_root_session",
        stage_ordinal=1,
        prompt_sha256=_digest("conflict-prompt"),
        final_message_sha256=_digest("conflict-final"),
        transcript_sha256=hashlib.sha256(raw).hexdigest(),
        requested_provider="openai",
        requested_model="gpt-5.6-luna",
        requested_reasoning="low",
        requested_service_tier="priority",
        identity_attestation="usage_only",
        effective_provider="unattested",
        effective_model="unattested",
        effective_reasoning="unattested",
        effective_service_tier="unattested",
        input_tokens=100,
        cached_input_tokens=80,
        output_tokens=5,
        reasoning_output_tokens=2,
        provider_total_tokens=105,
    )
    connection = sqlite3.connect(root / "round/rrcv2.sqlite3")
    connection.executescript(
        """
        CREATE TABLE rrcv2p_cells(
            root_call_id TEXT,state TEXT,root_cost_event BLOB,cell_id TEXT,
            combined_session BLOB
        );
        CREATE TABLE rrcv2_calls(call_id TEXT,state TEXT,cost_event BLOB);
        CREATE TABLE rrcv2p_attempts_bindings(transcript BLOB);
        """
    )
    connection.execute(
        "INSERT INTO rrcv2p_cells VALUES(?,?,?,?,NULL)",
        (event.cost_event_id, "root_committed", event.canonical_bytes(), "cell-miss"),
    )
    connection.commit()
    connection.close()

    report = analyzer.recover_failure(root, RuntimeError("injected failure"))
    assert report["known_subtotal"]["provider_visible_tokens"] == 105
    assert set(report["by_requested_model"]) == {"gpt-5.6-luna"}
    assert any(
        "CostEvent requested model conflicts" in value
        for value in report["unquantified_components"]
    )


@pytest.mark.parametrize("same_launch", [True, False])
def test_failure_recovery_coalesces_only_authority_proven_root_alias(
    tmp_path: Path, same_launch: bool
) -> None:
    root = tmp_path / ("f" * 64)
    cell = root / "round/miss"
    cell.mkdir(parents=True)
    usage = {
        "input_tokens": 100,
        "cached_input_tokens": 80,
        "cache_write_input_tokens": 0,
        "output_tokens": 5,
        "reasoning_output_tokens": 2,
        "total_tokens": 105,
    }
    launch_id = "launch-one"
    native = [
        {"type": "session_meta", "payload": {"id": launch_id}},
        {"type": "turn_context", "payload": {"model": "gpt-5.5", "effort": "medium"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": usage, "total_token_usage": usage},
            },
        },
    ]
    cli = [
        {"type": "thread.started", "thread_id": launch_id if same_launch else "launch-two"},
        {
            "type": "turn.completed",
            "usage": {key: usage[key] for key in usage if key != "total_tokens"},
        },
    ]
    (cell / "root-transcript.jsonl").write_text("\n".join(json.dumps(row) for row in native) + "\n")
    (cell / "root-events.jsonl").write_text("\n".join(json.dumps(row) for row in cli) + "\n")
    report = analyzer.recover_failure(root, RuntimeError("injected failure"))
    if same_launch:
        assert report["known_subtotal"]["provider_visible_tokens"] == 105
        assert any(
            row.get("kind") == "root_cli_log" and row.get("status") == "supplemental_after_stop"
            for row in report["recovery_inventory"]
        )
    else:
        assert report["known_subtotal"]["provider_visible_tokens"] == 210
        assert any(
            "not an authority-proven alias" in value for value in report["unquantified_components"]
        )


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer_identity = product_guard.PRODUCER_SHA256
    root = tmp_path / producer_identity
    _write_v22_producer(root)
    monkeypatch.setattr(analyzer, "_EXPECTED_PRODUCER_ROOT", root)
    (root / "terminal.json").write_bytes(
        canonical_json_bytes(
            {
                "producer_sha256": producer_identity,
                "returncode": 1,
                "status": "failure",
                "v": 1,
            }
        )
    )
    (root / "terminal.json").chmod(0o600)
    assert analyzer.main([str(root)]) == 1
    report = json.loads((root / "v22-analysis.v1.json").read_bytes())
    assert report["analysis_status"] == "failure_or_incomplete"
    assert report["total"]["provider_visible_tokens"] is None
    assert report["unquantified_components"]
    assert not any("terminal receipt differs" in row for row in report["unquantified_components"])
    assert report["case_execution"] == {"hit": "not_run", "miss": "not_run", "near": "not_run"}
    markdown = (root / "v22-report.md").read_text()
    assert markdown.startswith("# RRCv2 V22")
    assert "Overall provider-visible tokens: unknown" in markdown
    assert "Known recovered token subtotal: 0" in markdown
    assert (root / "v22-model-economics.svg").read_text().startswith("<svg")
    inventory = json.loads((root / "v22-analysis-inventory.v1.json").read_bytes())
    assert [row["path"] for row in inventory["artifacts"]] == [
        "v22-analysis.v1.json",
        "v22-model-economics.svg",
        "v22-report.md",
    ]
    with pytest.raises(analyzer.AnalysisError, match="inventory already commits"):
        analyzer.main([str(root)])
    report_path = root / "v22-analysis.v1.json"
    report_path.write_bytes(b"conflict")
    with pytest.raises(analyzer.AnalysisError, match="inventory already commits"):
        analyzer.main([str(root)])


@pytest.mark.parametrize(
    ("completed", "started", "artifact_case", "expected"),
    [
        (
            {"cell-miss"},
            {"cell-miss", "cell-hit"},
            "hit",
            {"miss": "completed", "hit": "stopped_or_incomplete", "near": "not_run"},
        ),
        (
            {"cell-miss", "cell-hit"},
            {"cell-miss", "cell-hit", "cell-near"},
            "near",
            {"miss": "completed", "hit": "completed", "near": "stopped_or_incomplete"},
        ),
    ],
)
def test_failure_case_execution_distinguishes_completed_stopped_and_unstarted(
    tmp_path: Path,
    completed: set[str],
    started: set[str],
    artifact_case: str,
    expected: dict[str, str],
) -> None:
    case = tmp_path / artifact_case
    case.mkdir()
    (case / "root-events.jsonl").write_text("started")
    assert (
        analyzer._case_execution(  # noqa: SLF001
            tmp_path,
            completed_cell_ids=completed,
            started_cell_ids=started,
        )
        == expected
    )


@pytest.mark.parametrize("fail_after", [1, 2, 3])
def test_failure_publication_resumes_before_inventory_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_after: int
) -> None:
    producer_identity = product_guard.PRODUCER_SHA256
    root = tmp_path / producer_identity
    _write_v22_producer(root)
    monkeypatch.setattr(analyzer, "_EXPECTED_PRODUCER_ROOT", root)
    terminal = root / "terminal.json"
    terminal.write_bytes(
        canonical_json_bytes(
            {
                "producer_sha256": producer_identity,
                "returncode": 9,
                "status": "failure",
                "v": 1,
            }
        )
    )
    terminal.chmod(0o600)
    original = analyzer._write  # noqa: SLF001
    calls = 0

    def interrupt(path: Path, raw: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == fail_after:
            raise OSError("simulated publication crash")
        original(path, raw)

    monkeypatch.setattr(analyzer, "_write", interrupt)
    with pytest.raises(OSError, match="simulated publication crash"):
        analyzer.main([str(root)])
    assert not (root / "v22-analysis-inventory.v1.json").exists()
    monkeypatch.setattr(analyzer, "_write", original)
    assert analyzer.main([str(root)]) == 1
    inventory = json.loads((root / "v22-analysis-inventory.v1.json").read_bytes())
    assert len(inventory["artifacts"]) == 3


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer_identity = "f" * 64
    root = tmp_path / producer_identity
    round_root = root / "round"
    round_root.mkdir(parents=True)
    monkeypatch.setattr(
        analyzer,
        "_authority_hashes",
        lambda _root: {"producer_identity": producer_identity},
    )
    producer = {
        "experiment_id": "rrcv2-cli-smoke-v22",
        "fixture_manifest_sha256": "a" * 64,
        "launch_manifest_sha256": "1" * 64,
        "predecessor_manifest_sha256": "b" * 64,
        "price_authority_sha256": "2" * 64,
        "producer_sha256": producer_identity,
        "round_token": f"rrcv2-cli-smoke-{producer_identity[:32]}",
        "session_plan_sha256": "c" * 64,
        "session_review_sha256": "d" * 64,
        "v": 22,
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
        ("hit", "rrcv2-cli-hit-001", ("implement",)),
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
        "spec",
    }
    assert set(report["by_effective_model"]) == {"gpt-5.5", "gpt-5.6-luna", "unattested"}
    assert all(
        row["grouped_effective_model"] == "unattested"
        for row in report["calls"]
        if row["role"] == "planner"
    )

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
    changed = False
    for rowid, raw in call_rows:
        value = json.loads(raw)
        if value["task_id"] == "rrcv2-cli-hit-001" and value["stage"] == "implement":
            value["stage"] = "prime"
            connection.execute(
                "UPDATE rrcv2_calls SET cost_event=? WHERE rowid=?",
                (canonical_json_bytes(value), rowid),
            )
            changed = True
    assert changed
    connection.commit()
    connection.close()
    with pytest.raises(analyzer.AnalysisError):
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
