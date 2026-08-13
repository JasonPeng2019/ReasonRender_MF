from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import harness.staged_codex as staged_codex
import pytest
from harness.staged_codex import ARMS, TERRA_MODEL, WORKER_MODEL, prepare_staged_round, run_staged_round

ROOT = Path(__file__).resolve().parents[2]


def test_qwen_stream_session_and_completion_are_retained(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join(
            (
                json.dumps({"type": "system", "subtype": "init", "session_id": "qwen-123"}),
                json.dumps({
                    "type": "result", "subtype": "success", "session_id": "qwen-123",
                    "is_error": False, "result": "done", "usage": {"input_tokens": 1, "output_tokens": 1},
                }),
            )
        ) + "\n",
        encoding="utf-8",
    )

    assert staged_codex._stream_thread_id(stream) == "qwen-123"
    assert staged_codex._stream_completed(stream) is True


def test_qwen_api_error_stream_remains_resumable_but_incomplete(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n".join(
            (
                json.dumps({"type": "system", "subtype": "init", "session_id": "qwen-123"}),
                json.dumps({
                    "type": "result", "subtype": "success", "session_id": "qwen-123",
                    "is_error": False, "result": "[API Error: bad request]",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }),
            )
        ) + "\n",
        encoding="utf-8",
    )

    assert staged_codex._stream_thread_id(stream) == "qwen-123"
    assert staged_codex._stream_completed(stream) is False


def test_model_environment_prefers_the_fixture_compiler_python(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "machine-python")

    environment = staged_codex._model_environment(ROOT, "raw")

    assert environment["PATH"].split(os.pathsep)[0] == str(ROOT / ".codex" / "dev" / ".venv" / "Scripts")


def test_staged_round_prepares_codex_terra_and_direct_qwen_deepseek_commands(tmp_path) -> None:
    payload = prepare_staged_round(ROOT, "staged-r1", tmp_path)

    assert set(payload["arms"]) == set(ARMS)
    for arm in ARMS:
        stages = payload["arms"][arm]["stages"]
        assert len(stages) == 37
        for stage in stages:
            parent = stage["orchestrator"]
            assert parent["model"] == TERRA_MODEL
            assert "--dangerously-bypass-approvals-and-sandbox" in parent["command"]
            assert parent["command"][:2] == ["codex", "exec"]
            assert len(stage["workers"]) == 4
            assert all(worker["model"] == WORKER_MODEL for worker in stage["workers"])
            assert all(worker["command"][1:3] == ["-m", "harness.qwen_delegate"] for worker in stage["workers"])
            assert all(worker["command"][worker["command"].index("--qwen-bin") + 1] == "qwen" for worker in stage["workers"])
            assert "model_auto_compact_token_limit=230000" in parent["command"]
            if arm == "raw":
                assert stage["broker"] is None
                assert all("--contextmesh" not in worker["command"] for worker in stage["workers"])
            else:
                assert stage["broker"] is not None
                assert all("--contextmesh" in worker["command"] for worker in stage["workers"])


def test_raw_staged_controller_keeps_completed_streams_and_merges_only_accepted_writes(tmp_path, monkeypatch) -> None:
    log = tmp_path / "fake-invocations.jsonl"
    fake = tmp_path / "fake_codex.py"
    fake.write_text(
        '''import json, os, re, sys, uuid
from pathlib import Path

args = sys.argv[1:]
model = args[args.index("--model") + 1]
final = Path(args[args.index("--output-last-message") + 1])
body = sys.stdin.read()
log = Path(os.environ["FAKE_CODEX_LOG"])
log.parent.mkdir(parents=True, exist_ok=True)
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"model": model, "cwd": os.getcwd()}) + "\\n")
if model.endswith("terra"):
    request = json.loads((Path.cwd() / ".terra-plan-request.json").read_text(encoding="utf-8"))
    plans = Path.cwd() / ".terra-plans"
    plans.mkdir(exist_ok=True)
    for task in request["tasks"]:
        facts = [fact for values in task["required_plan_fact_anchors"].values() for fact in values]
        (plans / f'{task["worker_id"]}.json').write_text(json.dumps({
            "task_id": task["task_id"], "worker_id": task["worker_id"],
            "plan_steps": ["Apply the declared contract in the owned paths."], "source_facts": facts,
        }), encoding="utf-8")
else:
    marker = '{\\n  "acceptance_cmd"'
    packet = json.loads(body[body.index(marker):])
    for relative in packet["owned_write_paths"]:
        path = Path.cwd() / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative.startswith("tests/"):
            path.write_text("def test_ok():\\n    assert True\\n", encoding="utf-8")
        elif relative.endswith("rollout.py"):
            found = re.search(r"advance STAGE_REVISION to '([^']+)'", packet["objective"])
            path.write_text(f'STAGE_REVISION = "{found.group(1) if found else "stage-00"}"\\n', encoding="utf-8")
        else:
            path.write_text("VALUE = 1\\n", encoding="utf-8")
final.parent.mkdir(parents=True, exist_ok=True)
final.write_text("done\\n", encoding="utf-8")
print(json.dumps({"type": "thread.started", "thread_id": str(uuid.uuid4())}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 10, "reasoning_output_tokens": 5}}))
''',
        encoding="utf-8",
    )

    def fake_command(model, final, *, bridge, repo):
        return (sys.executable, str(fake), "exec", "--model", model, "--output-last-message", str(final), "-")

    monkeypatch.setattr(staged_codex, "_command", fake_command)
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    # This test exercises retained direct-Codex dispatch, not the complete
    # append-only workload. One independent stage gives the same Terra + four
    # DeepSeek lifecycle without repeatedly materializing the large catalog.
    stage_index = next(index for index, stage in enumerate(staged_codex.staged_workloads()) if stage.stage_id == "stage-31")
    stage = staged_codex.staged_workloads()[stage_index]
    ledger = staged_codex.staged_overlap_ledgers()[stage_index]
    monkeypatch.setattr(staged_codex, "staged_workloads", lambda: (stage,))
    monkeypatch.setattr(staged_codex, "staged_overlap_ledgers", lambda: (ledger,))
    first = run_staged_round(ROOT, "controller-r1", tmp_path, arms=("raw",))
    second = run_staged_round(ROOT, "controller-r1", tmp_path, arms=("raw",))

    result_root = tmp_path / "controller-r1" / "staged-codex" / "raw"
    assert first["progress"]["arms"]["raw"]["stages"]["stage-31"]["status"] == "complete"
    assert second["progress"]["arms"]["raw"]["stages"]["stage-31"]["status"] == "complete"
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 5  # one Terra turn plus one concurrent four-Luna batch
    assert (result_root / "baseline" / "ruleforge" / "rules" / "compliance.py").is_file()
    packet = json.loads((result_root / "stages" / "stage-31" / "workers" / "worker-01" / "packet.json").read_text(encoding="utf-8"))
    read_set = json.loads((result_root / "stages" / "stage-31" / "workers" / "worker-01" / "local-read-set.json").read_text(encoding="utf-8"))
    assert packet["worker_id"] == "worker-01"
    assert read_set["allowed_local_read_paths"] == sorted(packet["initial_read_paths"])


def test_retained_runner_recovery_requires_an_explicit_scaffold_override(tmp_path, monkeypatch) -> None:
    stage_index = next(index for index, stage in enumerate(staged_codex.staged_workloads()) if stage.stage_id == "stage-31")
    stage = staged_codex.staged_workloads()[stage_index]
    ledger = staged_codex.staged_overlap_ledgers()[stage_index]
    monkeypatch.setattr(staged_codex, "staged_workloads", lambda: (stage,))
    monkeypatch.setattr(staged_codex, "staged_overlap_ledgers", lambda: (ledger,))
    prepare_staged_round(ROOT, "controller-r2", tmp_path)
    monkeypatch.setattr(staged_codex, "_runner_snapshot", lambda _repo: {"schema_version": 1, "files": {}, "sha256": "changed"})
    with pytest.raises(RuntimeError, match="runner source changed"):
        run_staged_round(ROOT, "controller-r2", tmp_path, arms=("raw",))


def test_resumed_contextmesh_worker_keeps_the_bridge_configuration(tmp_path) -> None:
    command = staged_codex._resume_command(WORKER_MODEL, tmp_path / "final.md", "thread-1", bridge=True, repo=ROOT)

    assert command[1:3] == ("-m", "harness.qwen_delegate")
    assert "--contextmesh" in command
    assert command[command.index("--resume-session-id") + 1] == "thread-1"


def test_only_contextmesh_brief_unavailable_completion_is_resumable(tmp_path) -> None:
    final = tmp_path / "final.md"
    final.write_text("CONTEXTMESH_BRIEF_UNAVAILABLE\n", encoding="utf-8")

    assert staged_codex._contextmesh_brief_unavailable(final)

    final.write_text("tests failed\n", encoding="utf-8")
    assert not staged_codex._contextmesh_brief_unavailable(final)


def test_completed_degraded_stage_is_retained(tmp_path) -> None:
    plans = staged_codex.staged_workloads()[0].plans
    for plan in plans:
        worker_root = tmp_path / "workers" / plan.worker_id
        worker_root.mkdir(parents=True)
        (worker_root / "stream.jsonl").write_text('{"type":"turn.completed","usage":{}}\n', encoding="utf-8")

    assert staged_codex._completed_degraded_stage(tmp_path, plans, {"status": "degraded"})
    (tmp_path / "workers" / plans[0].worker_id / "final.md").write_text("CONTEXTMESH_BRIEF_UNAVAILABLE\n", encoding="utf-8")
    assert not staged_codex._completed_degraded_stage(tmp_path, plans, {"status": "degraded"})
    (tmp_path / "workers" / plans[0].worker_id / "final.md").unlink()
    (tmp_path / "workers" / plans[0].worker_id / "stream.jsonl").write_text("", encoding="utf-8")
    assert not staged_codex._completed_degraded_stage(tmp_path, plans, {"status": "degraded"})


def test_product_repair_replaces_only_the_active_ledger_and_retains_the_bad_one(tmp_path) -> None:
    stage = staged_codex.staged_workloads()[0]
    entries = staged_codex.staged_overlap_ledgers()[0]
    arm_root = tmp_path / "contextmesh"
    stage_root, _ = staged_codex._stage_paths(arm_root, stage, entries)
    ledger = stage_root / "ledger.json"
    ledger.write_text("old-product-ledger\n", encoding="utf-8")

    staged_codex._stage_paths(arm_root, stage, entries, replace_product_error_ledger=True)

    assert (stage_root / "ledger.product-error-pre-repair.json").read_text(encoding="utf-8") == "old-product-ledger\n"
    assert "old-product-ledger" not in ledger.read_text(encoding="utf-8")


def test_prelaunch_ledger_drift_is_replaced_but_started_stage_drift_is_retained(tmp_path) -> None:
    stage = staged_codex.staged_workloads()[0]
    entries = staged_codex.staged_overlap_ledgers()[0]
    arm_root = tmp_path / "contextmesh"
    stage_root, _ = staged_codex._stage_paths(arm_root, stage, entries)
    ledger = stage_root / "ledger.json"
    ledger.write_text("[]\n", encoding="utf-8")

    staged_codex._stage_paths(arm_root, stage, entries)

    assert (stage_root / "ledger.prelaunch-stale.json").read_text(encoding="utf-8") == "[]\n"
    (stage_root / "workers" / "worker-01").mkdir(parents=True)
    (stage_root / "workers" / "worker-01" / "stream.jsonl").write_text("{}\n", encoding="utf-8")
    ledger.write_text("[]\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="ledger drift after DeepSeek launch"):
        staged_codex._stage_paths(arm_root, stage, entries)


def test_boundary_evidence_binds_the_next_stage_to_only_accepted_writes(tmp_path) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "ruleforge").mkdir()
    changed = baseline / "ruleforge" / "changed.py"
    changed.write_text("VALUE = 1\n", encoding="utf-8")
    staged_codex._run_git(baseline, "init")
    staged_codex._run_git(baseline, "config", "user.email", "test@example.invalid")
    staged_codex._run_git(baseline, "config", "user.name", "test")
    staged_codex._run_git(baseline, "add", "--all")
    staged_codex._run_git(baseline, "commit", "-m", "base")
    source_commit = staged_codex._run_git(baseline, "rev-parse", "HEAD")
    changed.write_text("VALUE = 2\n", encoding="utf-8")
    boundary_commit = staged_codex._stage_commit(baseline, "accepted", ("ruleforge/changed.py",))

    evidence = staged_codex._retain_boundary_evidence(
        baseline,
        tmp_path / "stage",
        source_commit=source_commit,
        boundary_commit=boundary_commit,
        paths=("ruleforge/changed.py",),
    )

    assert evidence["boundary_commit"] == boundary_commit
    assert evidence["path_sha256"]["ruleforge/changed.py"]
    assert "-VALUE = 1" in (tmp_path / "stage" / "boundary.diff").read_text(encoding="utf-8")
    assert "+VALUE = 2" in (tmp_path / "stage" / "boundary.diff").read_text(encoding="utf-8")


def test_broker_parent_uses_prior_source_commit_not_accepted_boundary() -> None:
    state = {
        "source_commit": "source-read-by-workers",
        "boundary_commit": "post-merge-boundary",
    }

    assert staged_codex._broker_parent_commit(state) == "source-read-by-workers"
    assert staged_codex._broker_parent_commit({}, "current-source") == "current-source"


def test_append_only_cohort_starts_only_at_an_independent_stage() -> None:
    stages, ledgers = staged_codex._stage_cohort("stage-35")

    assert [stage.stage_id for stage in stages] == ["stage-35", "stage-36", "stage-37"]
    assert len(ledgers) == 3
    with pytest.raises(ValueError, match="independent stage"):
        staged_codex._stage_cohort("stage-08")


def test_from_stage_preparation_uses_a_fresh_independent_baseline(tmp_path) -> None:
    payload = prepare_staged_round(ROOT, "from-stage-r1", tmp_path, from_stage="stage-35")

    root = tmp_path / "from-stage-r1" / "staged-codex"
    assert payload["cohort"]["lineage_mode"] == "fresh_independent_baseline"
    assert [stage["stage_id"] for stage in payload["arms"]["raw"]["stages"]] == ["stage-35", "stage-36", "stage-37"]
    assert (root / "raw" / "baseline").is_dir()
    assert not (root / "raw" / "stage-01-source").exists()


def test_promotion_requires_each_overlap_owner_publication_and_all_peer_services(tmp_path, monkeypatch) -> None:
    stage = staged_codex.staged_workloads()[0]
    entries = staged_codex.staged_overlap_ledgers()[0]
    monkeypatch.setattr(staged_codex, "staged_overlap_ledgers", lambda: (entries,))
    events = []
    for entry in entries:
        events.extend(
            [
                {"event": "source_claim_raw", "brief_id": entry.brief_id, "owner_id": entry.source_owner, "ts": 1},
                {"event": "brief_published", "brief_id": entry.brief_id, "owner_id": entry.source_owner, "refresh_kind": "raw", "ts": 2},
                *[
                    {"event": "brief_served", "brief_id": entry.brief_id, "peer_id": peer, "ts": 3}
                    for peer in entry.peer_workers
                ],
            ]
        )
    event_path = tmp_path / "contextmesh" / "broker" / "events.jsonl"
    event_path.parent.mkdir(parents=True)
    event_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    valid, evidence = staged_codex._broker_evidence(tmp_path, "contextmesh", (stage,))

    assert valid is True
    assert all(row["owner_claim_valid"] and row["publication_valid"] and all(row["peer_service"].values()) for row in evidence)
    event_path.write_text("\n".join(json.dumps(event) for event in events[:-1]) + "\n", encoding="utf-8")
    valid, _evidence = staged_codex._broker_evidence(tmp_path, "contextmesh", (stage,))
    assert valid is False


def test_dispatch_prefetches_only_broker_ready_worker_briefs(monkeypatch) -> None:
    plans = staged_codex.staged_workloads()[0].plans
    entries = staged_codex.staged_overlap_ledgers()[0]

    class ReadyClient:
        def __init__(self, host, port) -> None:
            assert host == "127.0.0.1" and port == 9999

        async def get_ready_worker_briefs(self, requests, worker_id):
            return {requests[0]["brief_id"]: {"brief_id": requests[0]["brief_id"], "facts": [worker_id]}}

    monkeypatch.setattr(staged_codex, "SharedBrokerClient", ReadyClient)
    prefetched = staged_codex._prefetched_context({"host": "127.0.0.1", "port": 9999}, plans, entries)

    assert set(prefetched) == {plan.worker_id for plan in plans}
    assert all(len(briefs) == 1 for briefs in prefetched.values())


def test_sealed_contextmesh_packet_does_not_expose_a_broker() -> None:
    plan = staged_codex.staged_workloads()[0].plans[0]
    entries = staged_codex.staged_overlap_ledgers()[0]
    related = {
        entry.brief_id: {"brief_id": entry.brief_id, "facts": ["fact"]}
        for entry in entries
        if plan.worker_id == entry.source_owner or plan.worker_id in entry.peer_workers
    }

    assert not staged_codex._needs_contextmesh_bridge(plan, entries, related)
    assert staged_codex._needs_contextmesh_bridge(plan, entries, {})


def test_measured_delivery_preserves_actual_terra_plan_artifacts() -> None:
    stage = next(item for item in staged_codex.staged_workloads() if item.stage_id == "stage-18")
    raw_like = tuple(
        staged_codex.TerraPlan(plan.task_id, plan.worker_id, ("raw coordinator wording",), ("fact",))
        for plan in stage.plans
    )
    rrc_like = tuple(
        staged_codex.TerraPlan(plan.task_id, plan.worker_id, ("rrc coordinator wording",), ("fact",))
        for plan in stage.plans
    )

    assert raw_like != rrc_like


def test_usage_total_sums_collector_and_already_aggregated_stage_totals() -> None:
    total = staged_codex._usage_total(
        (
            {"totals": {"input_new_tokens": 10, "output_tokens": 2, "reasoning_output_tokens": 1, "turns": 1}},
            {"input_new_tokens": 20, "output_tokens": 3, "reasoning_output_tokens": 4, "turns": 1},
        )
    )

    assert total["input_new_tokens"] == 30
    assert total["output_tokens"] == 5
    assert total["reasoning_output_tokens"] == 5
    assert total["turns"] == 2


def test_controller_lease_rejects_a_concurrent_parent_and_releases_its_own_marker(tmp_path) -> None:
    lease, token = staged_codex._controller_lease(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="another staged controller lease is active"):
            staged_codex._controller_lease(tmp_path)
    finally:
        staged_codex._release_controller_lease(lease, token)

    assert not lease.exists()


def test_controller_lease_recovers_a_windows_stale_pid_probe(tmp_path, monkeypatch) -> None:
    stale = tmp_path / "controller-lease.json"
    stale.write_text('{"pid": 42, "token": "old"}\n', encoding="utf-8")
    monkeypatch.setattr(staged_codex.os, "kill", lambda *_args: (_ for _ in ()).throw(OSError(87, "invalid parameter")))

    lease, token = staged_codex._controller_lease(tmp_path)
    staged_codex._release_controller_lease(lease, token)

    assert not stale.exists()


def test_stage_deadline_grants_a_fresh_finite_budget(tmp_path) -> None:
    budget_path = tmp_path / "stage-budgets" / "stage-37.json"
    deadline = staged_codex._stage_deadline(budget_path, "stage-37")

    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    assert budget["stage_id"] == "stage-37"
    assert budget["deadline_unix"] - time.time() == pytest.approx(staged_codex.STAGE_TIMEOUT_SECONDS, abs=1.0)
    assert deadline - time.monotonic() == pytest.approx(staged_codex.STAGE_TIMEOUT_SECONDS, abs=1.0)


def test_stage_deadline_resumes_with_the_same_stage_remaining_budget(tmp_path) -> None:
    budget_path = tmp_path / "stage-budgets" / "stage-37.json"
    budget_path.parent.mkdir(parents=True)
    budget_path.write_text(
        json.dumps({"schema_version": 1, "stage_id": "stage-37", "started_unix": time.time() - 100, "deadline_unix": time.time() + 30}),
        encoding="utf-8",
    )

    deadline = staged_codex._stage_deadline(budget_path, "stage-37")

    assert deadline - time.monotonic() == pytest.approx(30, abs=1.0)


def test_stage_deadline_replaces_a_previous_stage_budget(tmp_path) -> None:
    budget_path = tmp_path / "stage-budgets" / "stage-37.json"
    budget_path.parent.mkdir(parents=True)
    budget_path.write_text(
        json.dumps({"schema_version": 1, "stage_id": "stage-36", "started_unix": time.time() - 2000, "deadline_unix": time.time() - 1000}),
        encoding="utf-8",
    )

    deadline = staged_codex._stage_deadline(budget_path, "stage-37")

    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    assert budget["stage_id"] == "stage-37"
    assert budget["deadline_unix"] - time.time() == pytest.approx(staged_codex.STAGE_TIMEOUT_SECONDS, abs=1.0)
    assert deadline - time.monotonic() == pytest.approx(staged_codex.STAGE_TIMEOUT_SECONDS, abs=1.0)
