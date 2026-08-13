from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness import runner

ROOT = Path(__file__).resolve().parents[2]

STREAM = "\n".join(
    (
        json.dumps(
            {
                "type": "assistant",
                "session_id": "parent",
                "message": {
                    "usage": {
                        "input_tokens": 1,
                        "cache_read_input_tokens": 1,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 1,
                    },
                    "content": [],
                },
            }
        ),
        json.dumps({"type": "result", "subtype": "success", "session_id": "parent"}),
    )
) + "\n"


def manifest() -> dict[str, object]:
    packet = {
        "signature": "def evaluate_{rule_name}(input: NormalizedInput) -> Decision",
        "acceptance": ["register {rule_name}", "return {error_code} for rejected input"],
        "write_paths": [
            "ruleforge/rules/{domain}.py",
            "tests/test_{domain}_rule.py",
        ],
        "read_first": ["ruleforge/domain.py", "ruleforge/registry.py"],
        "plan": {"steps": ["use {source_field}", "compare {expected_value}"]},
    }
    tasks = [
        {
            "task_id": f"task-{index}",
            "text": f"Implement task {index}.",
            "slot_values": {
                "domain": f"domain-{index}",
                "source_field": f"field_{index}",
                "rule_name": f"rule_{index}",
                "error_code": f"ERR_{index}",
                "expected_value": str(index),
            },
        }
        for index in range(1, 5)
    ]
    return {"generic_packet": packet, "tasks": tasks}


def fake_materializer(tmp_path: Path, *, with_manifest: bool = False):
    calls: list[str] = []

    def materialize(plan: runner.ArmPlan) -> Path:
        calls.append(plan.arm)
        source = tmp_path / f"source-{plan.arm}"
        source.mkdir()
        (source / "marker.txt").write_text(plan.arm, encoding="utf-8")
        if with_manifest:
            plan.manifest_path.write_text(json.dumps(manifest()), encoding="utf-8")
        return source

    return materialize, calls


def test_build_arm_plan_rejects_unknown_arm(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="raw, contextmesh, full"):
        runner.build_arm_plan(tmp_path, "other", 1)


def test_safety_ceilings_are_symmetric_across_arms(tmp_path: Path) -> None:
    plans = [runner.build_arm_plan(tmp_path, arm, 3) for arm in runner.ARMS]

    assert [plan.safety for plan in plans] == [
        {"orchestrator": 25, "worker": 15},
        {"orchestrator": 25, "worker": 15},
        {"orchestrator": 25, "worker": 15},
    ]
    assert [(plan.orchestrator_max_turns, plan.worker_max_turns) for plan in plans] == [
        (25, 15),
        (25, 15),
        (25, 15),
    ]


def _flag(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


def test_claude_command_policy_keeps_worker_registration_out_of_inline_flags(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("REASONRENDER_MAX_BUDGET_USD", "7.5")
    expected_tools = {
        "raw": "Task,Bash,Read,Glob,Grep,Write,Edit",
        "contextmesh": "Task,Bash,Write,Edit,mcp__contextmesh__read",
        "full": "Task,Bash,Write,Edit,mcp__contextmesh__read",
    }

    for arm, tools in expected_tools.items():
        plan = runner.build_arm_plan(tmp_path, arm, "r1")
        command = plan.command
        assert command[:6] == ["claude", "-p", "--verbose", "--model", "sonnet", "--tools"]
        assert _flag(command, "--tools") == tools
        assert _flag(command, "--settings") == str(plan.settings_path)
        assert _flag(command, "--max-turns") == "25"
        assert _flag(command, "--max-budget-usd") == "7.5"
        assert _flag(command, "--output-format") == "stream-json"
        assert "--include-hook-events" in command
        assert _flag(command, "--append-system-prompt") == runner.PARENT_DISPATCH_SYSTEM_PROMPT
        assert (
            "The first parent response must be exactly one Bash tool call with no text or other tool."
            in runner.PARENT_DISPATCH_SYSTEM_PROMPT
        )
        assert (
            "Later parent responses before all four background workers are issued must contain only Task/Agent calls."
            in runner.PARENT_DISPATCH_SYSTEM_PROMPT
        )
        assert (
            "No waiting, reading, discovery, editing, or text is permitted until four background workers are issued."
            in runner.PARENT_DISPATCH_SYSTEM_PROMPT
        )
        expected_command = [
            "claude",
            "-p",
            "--verbose",
            "--model",
            "sonnet",
            "--tools",
            tools,
            "--settings",
            str(plan.settings_path),
            "--max-turns",
            "25",
            "--max-budget-usd",
            "7.5",
            "--output-format",
            "stream-json",
            "--include-hook-events",
            "--dangerously-skip-permissions",
            "--append-system-prompt",
            runner.PARENT_DISPATCH_SYSTEM_PROMPT,
        ]
        if arm in {"contextmesh", "full"}:
            expected_command.extend(("--mcp-config", str(plan.mcp_config_path)))
        if arm == "full":
            expected_command.extend(("--disallowed-tools", "Read,Glob,Grep"))
        assert command == expected_command
        assert "--agents" not in command
        assert str(plan.agents_path) not in command
        if arm == "full":
            assert _flag(command, "--disallowed-tools") == "Read,Glob,Grep"
            assert all(name not in tools.split(",") for name in runner.DISALLOWED_TOOLS)
        else:
            assert "--disallowed-tools" not in command
        if arm == "raw":
            assert plan.mcp_config_path is None
            assert "--mcp-config" not in command
        else:
            assert plan.mcp_config_path is not None
            assert _flag(command, "--mcp-config") == str(plan.mcp_config_path)
            assert plan.mcp_config_path.is_absolute()


def test_frozen_settings_use_absolute_existing_repo_hook_paths(tmp_path: Path) -> None:
    plans = [
        runner.build_arm_plan(ROOT, arm, "frozen", metrics_root=tmp_path / "metrics")
        for arm in runner.ARMS
    ]

    for plan in plans:
        runner._freeze_run_files(plan)
        setting = json.loads(plan.settings_path.read_text(encoding="utf-8"))
        stop_command = setting["hooks"]["SubagentStop"][0]["hooks"][0]["command"]
        bash_command = setting["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        completion_path = Path(stop_command.split('"', 2)[1])
        bash_path = Path(bash_command.split('"', 2)[1])

        assert completion_path.is_absolute() and completion_path.is_file()
        assert bash_path.is_absolute() and bash_path.is_file()
        assert completion_path.name == "completion_gate.py"
        assert bash_path.name == "block_bash_reads.py"
        assert "$CLAUDE_PROJECT_DIR" not in stop_command
        assert "$CLAUDE_PROJECT_DIR" not in bash_command

    frozen = [plan.settings_path.read_bytes() for plan in plans]
    assert frozen[0] == frozen[1] == frozen[2]


def test_plan_retains_all_arm_artifact_paths(tmp_path: Path) -> None:
    for arm in runner.ARMS:
        plan = runner.build_arm_plan(tmp_path, arm, "r1")
        assert plan.prompt_path == plan.run_root / "prompt.md"
        assert plan.manifest_path == plan.run_root / "manifest.json"
        assert plan.settings_path == plan.run_root / "settings.json"
        assert plan.stderr_path == plan.run_root / "stderr.log"
        assert plan.gate_path == plan.run_root / "gate.jsonl"
        assert plan.gate_state_dir == plan.run_root / "gate-state"
        assert plan.cm_path == plan.run_root / "cm.jsonl"
        assert plan.agents_path == plan.run_root / "agents.json"


def test_prepare_uses_fake_source_and_keeps_arms_isolated(tmp_path: Path) -> None:
    materialize, calls = fake_materializer(tmp_path)
    raw = runner.build_arm_plan(tmp_path, "raw", 1)
    full = runner.build_arm_plan(tmp_path, "full", 1)

    runner.prepare_arm(raw, materialize)
    runner.prepare_arm(full, materialize)
    (raw.target / "marker.txt").write_text("changed", encoding="utf-8")

    assert (raw.target / "marker.txt").read_text(encoding="utf-8") == "changed"
    assert (full.target / "marker.txt").read_text(encoding="utf-8") == "full"
    assert (tmp_path / "source-raw" / "marker.txt").read_text(encoding="utf-8") == "raw"
    assert calls == ["raw", "full"]


def test_prepare_rejects_repeated_target(tmp_path: Path) -> None:
    materialize, _ = fake_materializer(tmp_path)
    plan = runner.build_arm_plan(tmp_path, "raw", 1)

    runner.prepare_arm(plan, materialize)
    with pytest.raises(FileExistsError, match="target workspace already exists"):
        runner.prepare_arm(plan, materialize)


def test_real_materializer_isolated_contract_through_fake(tmp_path: Path, monkeypatch) -> None:
    calls: list[Path] = []

    def fake_ruleforge(output: Path) -> dict[str, object]:
        calls.append(output)
        workspace = output / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "fixture.txt").write_text("workspace", encoding="utf-8")
        value = manifest()
        (output / "manifest.json").write_text(json.dumps(value), encoding="utf-8")
        return {"workspace": str(workspace), **value}

    monkeypatch.setattr(runner, "ruleforge_materialize", fake_ruleforge)
    plan = runner.build_arm_plan(tmp_path, "contextmesh", "r1")

    runner._prepare_run(plan, runner._ruleforge_materializer, require_manifest=True)

    assert calls == [plan.run_root / "materialized"]
    assert (plan.run_root / "materialized" / "workspace" / "fixture.txt").is_file()
    assert (plan.target / "fixture.txt").read_text(encoding="utf-8") == "workspace"
    assert json.loads(plan.manifest_path.read_text(encoding="utf-8")) == manifest()
    assert json.loads(plan.settings_path.read_text(encoding="utf-8"))["hooks"]
    agents = json.loads(plan.agents_path.read_text(encoding="utf-8"))
    assert agents["artifact_kind"] == "frozen_evidence"
    assert agents["executable"] is False
    assert str(plan.agents_path) not in plan.command
    assert plan.prompt_path.is_file()
    prompt = plan.prompt_path.read_text(encoding="utf-8")
    assert all(f"task-{index}" in prompt for index in range(1, 5))
    assert all(f"GATE_TASK_ID: task-{index}" in prompt for index in range(1, 5))
    assert "exactly four" in prompt
    assert "one to four parent tool turns" in prompt
    assert "first parent response must be exactly one Bash tool call with no text or other tool" in prompt
    assert "render all four task packets" in prompt
    assert "do not wait, read, discover, edit, or emit text" in prompt
    assert "subagent_type=worker" in prompt
    assert "run_in_background=true" in prompt
    assert "text-only response" in prompt
    assert "general-purpose" not in prompt
    assert "mcp__contextmesh__read" in prompt
    assert str(plan.target / "ruleforge" / "evaluator.py") in prompt
    assert "must not edit files" in prompt
    assert "domain-1" in prompt and "field_4" in prompt
    assert "ruleforge/rules/domain-1.py" in prompt
    assert "return ERR_1 for rejected input" in prompt
    mcp = json.loads(plan.mcp_config_path.read_text(encoding="utf-8"))
    assert Path(mcp["mcpServers"]["contextmesh"]["args"][0]).is_absolute()


def test_prepared_arms_materialize_registered_worker_contracts(tmp_path: Path) -> None:
    materialize, _ = fake_materializer(tmp_path)

    expected = {
        "raw": {
            "tools": "Read, Glob, Grep, Write, Edit, Bash",
            "body": "READ_EVIDENCE",
        },
        "contextmesh": {
            "tools": "Write, Edit, Bash, mcp__contextmesh__read",
            "body": "Before editing or testing, call `mcp__contextmesh__read` exactly once",
        },
        "full": {
            "tools": "Write, Edit, Bash, mcp__contextmesh__read",
            "body": "Repository discovery is unavailable",
        },
    }

    for arm in runner.ARMS:
        plan = runner.build_arm_plan(tmp_path, arm, f"worker-{arm}")
        runner._prepare_run(plan, materialize, require_manifest=False)
        worker = plan.target / ".claude" / "agents" / "worker.md"
        text = worker.read_text(encoding="utf-8")
        frontmatter, body = text.split("---\n", 2)[1:]
        fields = dict(line.split(": ", 1) for line in frontmatter.splitlines() if ": " in line)

        assert worker.is_file()
        assert fields["name"] == "worker"
        assert fields["description"]
        assert fields["maxTurns"] == "15"
        assert fields["permissionMode"] == "bypassPermissions"
        assert fields["tools"] == expected[arm]["tools"]
        assert expected[arm]["body"] in body


def test_execute_requires_named_opt_in_and_does_not_materialize(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv(runner.PROVIDER_OPT_IN_ENV, raising=False)
    called = False

    def materialize(_: runner.ArmPlan) -> Path:
        nonlocal called
        called = True
        raise AssertionError("provider preparation must be gated")

    assert (
        runner.main(
            ["--repo-root", str(tmp_path), "--execute", "arm", "raw", "1"],
            materializer=materialize,
            process_runner=lambda **_: (_ for _ in ()).throw(AssertionError("not run")),
        )
        == 2
    )
    assert runner.PROVIDER_OPT_IN_ENV in capsys.readouterr().err
    assert called is False
    assert not (tmp_path / "metrics").exists()


def test_execute_freezes_artifacts_environment_and_usage(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(runner.PROVIDER_OPT_IN_ENV, "1")
    materialize, _ = fake_materializer(tmp_path, with_manifest=True)
    plan = runner.build_arm_plan(tmp_path, "raw", 1)
    seen: dict[str, object] = {}

    def process(command, *, cwd, input, env):
        seen.update(command=command, cwd=cwd, input=input, env=env)
        manifest_value = json.loads(Path(env["REASONRENDER_GATE_MANIFEST"]).read_text(encoding="utf-8"))
        Path(env["REASONRENDER_GATE_LOG_PATH"]).write_text(
            "".join(
                json.dumps({"session_id": "gate", "task_id": task["task_id"], "cycle": 0, "verdict": "pass"}) + "\n"
                for task in manifest_value["tasks"]
            ),
            encoding="utf-8",
        )
        return 0, STREAM, "provider stderr\n"

    assert runner.execute_arm(plan, materializer=materialize, process_runner=process) == 0
    environment = seen["env"]
    assert isinstance(environment, dict)
    assert environment["CLAUDE_PROJECT_DIR"] == str(plan.repo_root)
    assert environment["REASONRENDER_ARM"] == "raw"
    assert environment["CONTEXTMESH_APP_ID"] == "reasonrender-1-raw"
    assert environment["REASONRENDER_GATE_MANIFEST"] == str(plan.manifest_path)
    assert environment["REASONRENDER_GATE_STATE_DIR"] == str(plan.gate_state_dir)
    assert environment["REASONRENDER_GATE_LOG_PATH"] == str(plan.gate_path)
    assert environment["CONTEXTMESH_LOG"] == str(plan.cm_path)
    assert seen["cwd"] == plan.target
    assert seen["input"] == plan.prompt_path.read_text(encoding="utf-8")
    assert plan.stream_path.read_text(encoding="utf-8") == STREAM
    assert plan.stderr_path.read_text(encoding="utf-8") == "provider stderr\n"
    for path in (
        plan.prompt_path,
        plan.manifest_path,
        plan.settings_path,
        plan.agents_path,
        plan.stream_path,
        plan.stderr_path,
        plan.usage_path,
        plan.turns_path,
        plan.gate_path,
        plan.cm_path,
    ):
        assert path.is_file()
    assert plan.gate_state_dir.is_dir()
    assert plan.cm_path.read_text(encoding="utf-8") == '{"event":"no_contextmesh"}\n'
    usage = json.loads(plan.usage_path.read_text(encoding="utf-8"))
    assert usage["provider_arm"] == "raw"
    assert usage["run_kind"] == "provider_arm"
    assert isinstance(usage["wall_clock_ms"], int)
    assert usage["valid"] is True


def test_execute_rejects_missing_completion_gate_ledger(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(runner.PROVIDER_OPT_IN_ENV, "1")
    materialize, _ = fake_materializer(tmp_path, with_manifest=True)
    plan = runner.build_arm_plan(tmp_path, "raw", "missing-gate")

    with pytest.raises(ValueError, match="completion gate ledger lacks passing records"):
        runner.execute_arm(plan, materializer=materialize, process_runner=lambda _command, **_: (0, STREAM, ""))

    assert plan.stream_path.is_file()
    assert plan.usage_path.is_file()
    assert plan.gate_path.is_file()


def test_execute_rejects_empty_contextmesh_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(runner.PROVIDER_OPT_IN_ENV, "1")
    materialize, _ = fake_materializer(tmp_path, with_manifest=True)
    plan = runner.build_arm_plan(tmp_path, "contextmesh", "empty-contextmesh")

    def process(_command, *, env, **_kwargs):
        manifest_value = json.loads(Path(env["REASONRENDER_GATE_MANIFEST"]).read_text(encoding="utf-8"))
        Path(env["REASONRENDER_GATE_LOG_PATH"]).write_text(
            "".join(
                json.dumps({"session_id": "gate", "task_id": task["task_id"], "cycle": 0, "verdict": "pass"}) + "\n"
                for task in manifest_value["tasks"]
            ),
            encoding="utf-8",
        )
        return 0, STREAM, ""

    with pytest.raises(ValueError, match="ContextMesh evidence lacks the required read_raw event"):
        runner.execute_arm(plan, materializer=materialize, process_runner=process)

    assert plan.stream_path.is_file()
    assert plan.usage_path.is_file()
    assert plan.cm_path.is_file()


def test_round_execute_runs_raw_contextmesh_full_sequentially(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(runner.PROVIDER_OPT_IN_ENV, "1")
    source_root = tmp_path / "sources"
    source_root.mkdir()
    events: list[str] = []

    def materialize(plan: runner.ArmPlan) -> Path:
        events.append(f"materialize:{plan.arm}")
        source = source_root / plan.arm
        source.mkdir()
        plan.manifest_path.write_text(json.dumps(manifest()), encoding="utf-8")
        return source

    def process(command, *, cwd, input, env):
        arm = Path(env["REASONRENDER_GATE_MANIFEST"]).parent.name
        events.append(f"execute:{arm}")
        manifest_value = json.loads(Path(env["REASONRENDER_GATE_MANIFEST"]).read_text(encoding="utf-8"))
        gate_records = [
            {
                "session_id": f"{arm}-gate",
                "task_id": task["task_id"],
                "cycle": 0,
                "verdict": "pass",
            }
            for task in manifest_value["tasks"]
        ]
        Path(env["REASONRENDER_GATE_LOG_PATH"]).write_text(
            "".join(json.dumps(record) + "\n" for record in gate_records),
            encoding="utf-8",
        )
        if arm != "raw":
            Path(env["CONTEXTMESH_LOG"]).write_text('{"event":"read_raw"}\n', encoding="utf-8")
        return 0, STREAM, ""

    assert (
        runner.main(
            ["--repo-root", str(tmp_path), "round", "2", "--execute"],
            materializer=materialize,
            process_runner=process,
        )
        == 0
    )
    assert events == [
        "materialize:raw",
        "execute:raw",
        "materialize:contextmesh",
        "execute:contextmesh",
        "materialize:full",
        "execute:full",
    ]
    round_root = tmp_path / "metrics" / "2"
    report = json.loads((round_root / "report.json").read_text(encoding="utf-8"))
    assert set(report["arms"]) == set(runner.ARMS)
    assert (round_root / "REPORT.md").is_file()
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(records) == 4
    assert records[-1]["status"] == "reported"
    assert records[-1]["report"] == str(round_root / "report.json")
    assert records[-1]["report_markdown"] == str(round_root / "REPORT.md")


def test_round_execute_retains_insufficient_evidence_and_continues_unfinished_arms(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(runner.PROVIDER_OPT_IN_ENV, "1")
    materialize, _ = fake_materializer(tmp_path, with_manifest=True)

    def process(command, *, cwd, input, env):
        manifest_value = json.loads(Path(env["REASONRENDER_GATE_MANIFEST"]).read_text(encoding="utf-8"))
        records = [
            {
                "session_id": "gate",
                "task_id": task["task_id"],
                "cycle": 0,
                "verdict": "pass",
            }
            for task in manifest_value["tasks"]
        ]
        Path(env["REASONRENDER_GATE_LOG_PATH"]).write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return 0, STREAM, ""

    assert runner.main(
        ["--repo-root", str(tmp_path), "round", "insufficient", "--execute"],
        materializer=materialize,
        process_runner=process,
    ) == 0
    round_root = tmp_path / "metrics" / "insufficient"
    assert not (round_root / "report.json").exists()
    assert not (round_root / "REPORT.md").exists()
    progress = json.loads((round_root / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "degraded"
    assert progress["arms"]["contextmesh"]["status"] == "degraded"
    assert (round_root / "full" / "stream.jsonl").is_file()


def test_round_preserves_a_completed_arm_and_resumes_only_unfinished_arms(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(runner.PROVIDER_OPT_IN_ENV, "1")
    retained = runner.build_arm_plan(tmp_path, "raw", "resume")
    retained.run_root.mkdir(parents=True)
    retained.stream_path.write_text(STREAM, encoding="utf-8")
    retained.usage_path.write_text(
        json.dumps({"valid": True, "run_kind": "provider_arm"}), encoding="utf-8"
    )
    materialize, calls = fake_materializer(tmp_path, with_manifest=True)
    executed: list[str] = []

    def process(_command, *, env, **_kwargs):
        arm = env["REASONRENDER_ARM"]
        executed.append(arm)
        manifest_value = json.loads(Path(env["REASONRENDER_GATE_MANIFEST"]).read_text(encoding="utf-8"))
        Path(env["REASONRENDER_GATE_LOG_PATH"]).write_text(
            "".join(
                json.dumps({"session_id": arm, "task_id": task["task_id"], "cycle": 0, "verdict": "pass"}) + "\n"
                for task in manifest_value["tasks"]
            ),
            encoding="utf-8",
        )
        if arm != "raw":
            Path(env["CONTEXTMESH_LOG"]).write_text('{"event":"read_raw"}\n', encoding="utf-8")
        return 0, STREAM, ""

    assert runner.main(
        ["--repo-root", str(tmp_path), "round", "resume", "--execute"],
        materializer=materialize,
        process_runner=process,
    ) == 0
    assert calls == ["contextmesh", "full"]
    assert executed == ["contextmesh", "full"]
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[0]["status"] == "retained_complete"
    progress = json.loads((tmp_path / "metrics" / "resume" / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "degraded"
    assert progress["arms"]["report"]["status"] == "degraded"


def test_round_stops_only_for_a_retained_product_traceback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(runner.PROVIDER_OPT_IN_ENV, "1")
    materialize, calls = fake_materializer(tmp_path, with_manifest=True)

    def process(_command, **_kwargs):
        raise RuntimeError('Traceback (most recent call last): File "C:/repo/contextmesh/mcp/server.py", line 1')

    assert runner.main(
        ["--repo-root", str(tmp_path), "round", "product-error", "--execute"],
        materializer=materialize,
        process_runner=process,
    ) == 2
    assert calls == ["raw"]
    progress = json.loads(
        (tmp_path / "metrics" / "product-error" / "progress.json").read_text(encoding="utf-8")
    )
    assert progress["status"] == "blocking_product_error"


def test_round_prepares_arms_in_required_order(tmp_path: Path, monkeypatch, capsys) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    calls: list[str] = []

    def materialize(plan: runner.ArmPlan) -> Path:
        calls.append(plan.arm)
        source = source_root / plan.arm
        source.mkdir()
        return source

    monkeypatch.setattr(runner, "_dry_run_materializer", materialize)

    assert runner.main(["--repo-root", str(tmp_path), "round", "2"]) == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert calls == ["raw", "contextmesh", "full"]
    assert [item["arm"] for item in output] == ["raw", "contextmesh", "full"]
    assert all(item["provider_execution"] is False for item in output)


def test_round_prepares_arms_in_requested_order(tmp_path: Path, monkeypatch, capsys) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    calls: list[str] = []

    def materialize(plan: runner.ArmPlan) -> Path:
        calls.append(plan.arm)
        source = source_root / plan.arm
        source.mkdir()
        return source

    monkeypatch.setattr(runner, "_dry_run_materializer", materialize)

    assert (
        runner.main(
            [
                "--repo-root",
                str(tmp_path),
                "round",
                "rotated",
                "--arms",
                "full",
                "raw",
                "contextmesh",
            ]
        )
        == 0
    )
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert calls == ["full", "raw", "contextmesh"]
    assert [item["arm"] for item in output] == ["full", "raw", "contextmesh"]


def test_round_rejects_invalid_arm_order_before_materialization(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    calls: list[str] = []

    def materialize(plan: runner.ArmPlan) -> Path:
        calls.append(plan.arm)
        raise AssertionError("materialization should not start")

    monkeypatch.setattr(runner, "_dry_run_materializer", materialize)

    assert (
        runner.main(
            [
                "--repo-root",
                str(tmp_path),
                "round",
                "invalid",
                "--arms",
                "raw",
                "raw",
                "contextmesh",
            ]
        )
        == 2
    )
    assert calls == []
    assert "--arms must be a permutation" in capsys.readouterr().err


def test_codex_pilot_is_one_terra_and_one_worker_batch_of_four_deepseek(tmp_path: Path) -> None:
    plan = runner.build_codex_pilot_plan(tmp_path, "r1")

    assert plan.run_root == tmp_path / "metrics" / "r1" / "codex-pilot"
    assert plan.orchestrator.model == "gpt-5.6-terra"
    assert [worker.model for worker in plan.workers] == ["deepseek-v4-flash:0731-cloud"] * 4
    assert [worker.role for worker in plan.worker_batch] == [
        "worker-01",
        "worker-02",
        "worker-03",
        "worker-04",
    ]
    assert len(plan.dispatch_batches) == 1
    assert len(plan.dispatch_batches[0]) == 4
    assert len({worker.target for worker in plan.workers}) == 4
    assert all(
        str(item.artifact_root).startswith(str(plan.run_root))
        and item.stream_path.parent == item.artifact_root
        and item.final_path.parent == item.artifact_root
        for item in (plan.orchestrator, *plan.workers)
    )


def test_codex_pilot_commands_are_headless_high_fast_priority_json_and_persistent(
    tmp_path: Path,
) -> None:
    plan = runner.build_codex_pilot_plan(tmp_path, 1)

    for invocation in (plan.orchestrator, *plan.workers):
        command = invocation.command
        assert command[:2] == ["codex", "exec"]
        assert "--json" in command
        assert ["--output-last-message", str(invocation.final_path)] == command[
            command.index("--output-last-message") : command.index("--output-last-message") + 2
        ]
        assert "--dangerously-bypass-approvals-and-sandbox" in command
        assert command[-1] == "-"
        assert "--ephemeral" not in command
        if invocation.role == "orchestrator":
            assert command[2:5] == ["--ignore-user-config", "--enable", "fast_mode"]
            assert ["--model", invocation.model] == command[5:7]
            assert ["--config", "model_reasoning_effort=high"] == command[7:9]
            assert ["--config", "model_auto_compact_token_limit=230000"] == command[9:11]
            assert ["--config", "service_tier=priority"] == command[11:13]
        else:
            assert "--strict-config" in command
            assert "--oss" in command and "ollama" in command
            assert "model_context_window=1048576" in command
            assert "model_auto_compact_token_limit=230000" in command


def test_pilot_cli_only_prints_metadata_and_never_falls_back_to_claude(
    tmp_path: Path, capsys
) -> None:
    assert runner.main(["--repo-root", str(tmp_path), "pilot", "r1"]) == 0

    metadata = json.loads(capsys.readouterr().out)
    assert metadata["label"] == "Codex-pilot"
    assert metadata["provider_execution"] is False
    assert metadata["topology"] == "1+4"
    assert metadata["worker_batches"] == 1
    assert metadata["claude_cache_metrics"] is None
    assert len(metadata["worker_plans"]) == 4
    assert not (tmp_path / "metrics").exists()


def test_direct_script_invocation_can_import_the_ruleforge_materializer(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "harness" / "runner.py"),
            "--repo-root",
            str(tmp_path),
            "--metrics-root",
            str(tmp_path / "metrics"),
            "pilot",
            "direct-script",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["label"] == "Codex-pilot"


def test_provider_process_has_a_bounded_wall_clock_and_retains_timeout_output(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    def stalled(*_args, **kwargs):
        captured.update(kwargs)
        raise subprocess.TimeoutExpired("claude", 17, output="partial stream", stderr="partial error")

    monkeypatch.setenv(runner.PROVIDER_TIMEOUT_ENV, "17")
    monkeypatch.setattr(runner.subprocess, "run", stalled)

    result = runner._run_process(["claude"], cwd=tmp_path, input="prompt", env={})

    assert captured["timeout"] == 17
    assert result == (
        124,
        "partial stream",
        "partial error\nprovider arm timed out after 17 seconds\n",
    )


def test_invalid_provider_timeout_uses_the_safe_default(monkeypatch) -> None:
    monkeypatch.setenv(runner.PROVIDER_TIMEOUT_ENV, "not-a-number")
    assert runner._provider_timeout_seconds() == 600
    monkeypatch.setenv(runner.PROVIDER_TIMEOUT_ENV, "0")
    assert runner._provider_timeout_seconds() == 600


def test_codex_pilot_has_no_pacing_or_sequential_dispatch_regression() -> None:
    source = inspect.getsource(runner)

    assert "sleep(" not in source
    assert "CONTEXTMESH_REQUEST_MIN_INTERVAL_SECONDS" not in source
    assert "claude" in source
