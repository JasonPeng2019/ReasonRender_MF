from __future__ import annotations

import json
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
