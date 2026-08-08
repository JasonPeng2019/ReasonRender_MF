from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from rrc.contract import ArmMode
from rrc.demo import format_meter, run_demo_arm
from rrc.pipeline.stubs import FakeModel, InMemoryRetrieval, fake_completion

SCRIPT = Path(__file__).parents[1] / "contextmesh" / "RRDdemo.sh"
TUI_SCRIPT = SCRIPT.parent / "scripts" / "rrd_demo_tui.sh"


def _spec(function: str, number: int) -> str:
    value = str(number)
    return json.dumps(
        {
            "plan": f"Implement {function} and always return {value}.",
            "signature": f"def {function}(value: int) -> int",
            "contract": f"Return the constant {value} for every integer input.",
            "tests": [f"def test_behavior():\n    assert {function}(99) == {value}"],
            "slots": {
                "entity": None,
                "identifiers": [function],
                "types": [],
                "fields": [],
                "constants": [value],
                "edge_values": [],
                "values": {"function": function, "number": value},
            },
        },
        separators=(",", ":"),
    )


def test_cold_demo_runs_the_public_pipeline_twice_without_reuse(tmp_path: Path) -> None:
    model = FakeModel(
        {
            "spec": [
                fake_completion(_spec("return_two", 2), tokens=10),
                fake_completion(_spec("return_three", 3), tokens=10),
            ],
            "implement": [
                fake_completion("def return_two(value: int) -> int:\n    return 2", tokens=4),
                fake_completion("def return_three(value: int) -> int:\n    return 3", tokens=4),
            ],
        }
    )

    evidence = run_demo_arm(
        round_id="test-round",
        mode=ArmMode.COLD,
        model=model,
        retrieval=InMemoryRetrieval(),
        evidence_path=tmp_path / "a" / "evidence.json",
    )

    assert evidence["pipeline"] == "rrc.pipeline.solve"
    assert evidence["proof_pass"] is True
    assert evidence["branches"] == ["miss", "miss"]
    assert evidence["stages"] == [["spec", "implement"], ["spec", "implement"]]
    assert evidence["total_tokens"] == 28


def test_warm_demo_misses_then_reuses_without_a_second_spec(tmp_path: Path) -> None:
    model = FakeModel(
        {
            "spec": [fake_completion(_spec("return_two", 2), tokens=10)],
            "implement": [
                fake_completion("def return_two(value: int) -> int:\n    return 2", tokens=4),
                fake_completion("def return_three(value: int) -> int:\n    return 3", tokens=4),
            ],
        }
    )
    retrieval = InMemoryRetrieval()
    after_first: list[bool] = []

    evidence = run_demo_arm(
        round_id="test-round",
        mode=ArmMode.WARM,
        model=model,
        retrieval=retrieval,
        evidence_path=tmp_path / "b" / "evidence.json",
        after_first=lambda: after_first.append(True),
    )

    assert evidence["pipeline"] == "rrc.pipeline.solve"
    assert evidence["proof_pass"] is True
    assert evidence["branches"] == ["miss", "reuse"]
    assert evidence["stages"] == [["spec", "implement"], ["implement"]]
    assert evidence["total_tokens"] == 18
    assert after_first == [True]


def test_meter_matches_the_box_format_of_the_contextmesh_demo(tmp_path: Path) -> None:
    cold = {
        "proof_pass": True,
        "total_tokens": 28,
        "model_calls": 4,
        "branches": ["miss", "miss"],
    }
    warm = {
        "proof_pass": True,
        "total_tokens": 18,
        "model_calls": 3,
        "branches": ["miss", "reuse"],
    }

    output = format_meter(cold, warm, round_id="test-round")

    assert output.startswith("┌")
    assert "ReasonRenderCoding live token meter — round test-round" in output
    assert "COLD (demo-a)" in output and "WARM (demo-b)" in output
    assert "SPEC REUSE REMOVED 10 TOKENS" in output
    assert "MISS → REUSE" in output
    assert output.endswith("┘")


def test_rejected_spec_has_an_actionable_failure_reason(tmp_path: Path) -> None:
    model = FakeModel({"spec": ["{}", "{}"]})

    evidence = run_demo_arm(
        round_id="bad-spec",
        mode=ArmMode.COLD,
        model=model,
        retrieval=InMemoryRetrieval(),
        evidence_path=tmp_path / "evidence.json",
    )

    assert evidence["proof_pass"] is False
    assert evidence["failure_reasons"] == [
        "bad-spec-proof-first: SPEC output failed strict schema/task validation",
        "bad-spec-proof-second: SPEC output failed strict schema/task validation",
    ]


def test_report_distinguishes_bad_spec_tests_from_bad_implementation(tmp_path: Path) -> None:
    first = json.loads(_spec("return_two", 2))
    first["tests"] = ["def test_bad_expectation():\n    assert return_two(99) == 999"]
    second = json.loads(_spec("return_three", 3))
    second["tests"] = ["def test_bad_expectation():\n    assert return_three(99) == 999"]
    model = FakeModel(
        {
            "spec": [json.dumps(first), json.dumps(second)],
            "implement": [
                "def return_two(value: int) -> int:\n    return 2",
                "def return_three(value: int) -> int:\n    return 3",
            ],
            "repair": [
                "def return_two(value: int) -> int:\n    return 2",
                "def return_three(value: int) -> int:\n    return 3",
            ],
        }
    )

    evidence = run_demo_arm(
        round_id="bad-tests",
        mode=ArmMode.COLD,
        model=model,
        retrieval=InMemoryRetrieval(),
        evidence_path=tmp_path / "evidence.json",
    )

    assert evidence["failure_reasons"] == [
        "bad-tests-proof-first: model-authored SPEC tests rejected code that passed the hidden oracle",
        "bad-tests-proof-second: model-authored SPEC tests rejected code that passed the hidden oracle",
    ]


@pytest.mark.parametrize("side", ["a", "b"])
def test_shell_sides_launch_the_codex_tui(tmp_path: Path, side: str) -> None:
    root = tmp_path / "repo"
    contextmesh = root / "contextmesh"
    scripts = contextmesh / "scripts"
    scripts.mkdir(parents=True)
    copied_script = contextmesh / "RRDdemo.sh"
    copied_script.write_bytes(SCRIPT.read_bytes())
    copied_script.chmod(0o755)
    start = scripts / "rrd_start_stack.sh"
    start.write_text("#!/bin/sh\nexit 0\n")
    start.chmod(0o755)
    calls = tmp_path / "tui-calls"
    tui = scripts / "rrd_demo_tui.sh"
    tui.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$RRC_DEMO_CALLS"\n')
    tui.chmod(0o755)
    env = os.environ.copy()
    env["RRC_DEMO_CALLS"] = str(calls)

    result = subprocess.run(
        [contextmesh / "RRDdemo.sh", side],
        cwd=contextmesh,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert calls.read_text() == f"{side}\n"


def test_rrd_script_describes_the_same_three_terminal_codex_tui_flow() -> None:
    script = SCRIPT.read_text()

    assert "open the COLD Codex TUI" in script
    assert "open the WARM Codex TUI" in script
    assert 'exec "$ROOT/scripts/rrd_demo_tui.sh" "$cmd"' in script
    assert "headless" not in script.lower()


def test_rrd_prompt_uses_codex_native_worker_language() -> None:
    prompt = (SCRIPT.parent / "RRD-demo-prompt.txt").read_text()

    assert "Spawn ONE worker subagent per handler file" in prompt
    assert "launch them in parallel" in prompt
    assert "wait for all four" in prompt.lower()
    assert "task tool" not in prompt
    assert "subagent_type" not in prompt


def test_contextmesh_shared_read_key_is_identical_for_store_and_reread_lookup() -> None:
    plugin = (SCRIPT.parent / "plugin/contextmesh.ts").read_bytes()

    assert b"\x00" not in plugin
    text = plugin.decode()
    assert "function sharedReadKey(" in text
    assert text.count("sharedReadKey(input.sessionID, filePath || envelopePath)") == 1
    assert text.count("sharedReadKey(input.sessionID, filePath)") == 2


def test_rrd_prep_resets_seeds_and_copies_the_audit_prompt(tmp_path: Path) -> None:
    root = tmp_path / "contextmesh"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "RRDdemo.sh")
    prompt = "four-worker canonical audit\n"
    (root / "RRD-demo-prompt.txt").write_text(prompt)
    events = tmp_path / "events"
    (scripts / "rrd_start_stack.sh").write_text('#!/bin/sh\necho start >> "$RRC_PREP_EVENTS"\n')
    (scripts / "rrd_demo_tui.sh").write_text(
        "#!/bin/sh\n"
        'echo "$1" >> "$RRC_PREP_EVENTS"\n'
        'if [ "$1" = reset ]; then mkdir -p "$(dirname "$0")/../runs/rrd-demo"; '
        'echo rrd-test > "$(dirname "$0")/../runs/rrd-demo/round"; fi\n'
    )
    (scripts / "rrd_demo_preflight.sh").write_text(
        '#!/bin/sh\necho preflight >> "$RRC_PREP_EVENTS"\n'
    )
    for script in scripts.iterdir():
        script.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    clipboard = tmp_path / "clipboard"
    pbcopy = fake_bin / "pbcopy"
    pbcopy.write_text('#!/bin/sh\ncat > "$RRC_PREP_CLIPBOARD"\n')
    pbcopy.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "RRC_PREP_EVENTS": str(events),
            "RRC_PREP_CLIPBOARD": str(clipboard),
        }
    )

    result = subprocess.run(
        [root / "RRDdemo.sh", "prep"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert events.read_text().splitlines() == ["start", "preflight", "reset", "seed"]
    assert clipboard.read_text() == prompt
    assert (root / "runs/RRD-demo-prompt.txt").read_text() == prompt


def test_rrd_codex_assets_are_present_and_wired_to_the_real_pipeline() -> None:
    repo = SCRIPT.parents[1]
    launcher = (repo / "contextmesh/scripts/rrd_demo_tui.sh").read_text()
    wrapper = (repo / "contextmesh/RRDdemo.sh").read_text()
    hook = (repo / "contextmesh/scripts/rrd_codex_hook.py").read_text()
    prompt = (repo / "contextmesh/RRD-demo-prompt.txt").read_text()

    assert 'exec "$CODEX_BIN" --dangerously-bypass-hook-trust' in launcher
    assert 'env_key = "OLLAMA_API_KEY"' in launcher
    assert 'wire_api = "responses"' in launcher
    assert "multi_agent_v2 = false" in launcher
    assert "plugins = false" in launcher
    assert "[agents.worker]" in launcher
    assert '"$ROUND_DIR/bundle/rrd_codex_hook.py" 8790' in launcher
    assert 'CODEX_HOME="$DEMO/codex-home"' in launcher
    assert "opencode" not in launcher.lower()
    assert "codex login" not in launcher.lower()
    assert "rrc.multiagent_demo" in hook
    assert 'payload.get("hook_event_name")' in hook
    assert "SubagentStart" in hook and "SubagentStop" in hook
    assert "fail_open" in hook
    assert wrapper.index('"$ROOT/scripts/rrd_demo_preflight.sh"') < wrapper.index(
        '"$ROOT/scripts/rrd_demo_tui.sh" reset'
    )
    assert 'rrd_demo_preflight.sh" || true' not in wrapper
    assert "ONE worker subagent per handler" in prompt


@pytest.mark.parametrize(("side", "mode"), [("a", "cold"), ("b", "warm")])
def test_rrd_tui_reaches_the_real_codex_entrypoint(tmp_path: Path, side: str, mode: str) -> None:
    root = tmp_path / "contextmesh"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(TUI_SCRIPT, scripts / "rrd_demo_tui.sh")
    shutil.copy2(SCRIPT.parent / "scripts/rrd_codex_hook.py", scripts / "rrd_codex_hook.py")
    (root / ".env.local").write_text(
        "OLLAMA_API_KEY=test-key\nCONTEXTMESH_MODEL=test-ollama-model\n"
    )
    template = root / "bench" / "target-template"
    template.mkdir(parents=True)
    (template / "README.md").write_text("demo\n")
    (root / "RRD-demo-prompt.txt").write_text("website prompt\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("curl", "uv"):
        executable = fake_bin / name
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    marker = tmp_path / "codex-env"
    codex = fake_bin / "codex"
    codex.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "--version" ]; then echo codex-cli-test; exit 0; fi\n'
        'printf "%s|%s|%s|%s\\n" "$RRC_DEMO_MODE" "$CODEX_HOME" '
        '"$RRC_PLANNER_CODEX_HOME" "$*" > "$RRC_TUI_MARKER"\n'
        "exit 73\n"
    )
    codex.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "RRD_CODEX_BIN": str(codex),
            "RRC_TUI_MARKER": str(marker),
        }
    )

    reset = subprocess.run(
        [scripts / "rrd_demo_tui.sh", "reset"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert reset.returncode == 0, reset.stderr
    round_id = (root / "runs/rrd-demo/round").read_text().strip()
    (root / f"runs/rrd-demo/{round_id}/.seeded").touch()

    result = subprocess.run(
        [scripts / "rrd_demo_tui.sh", side],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 73
    assert f"side {side} ({mode.upper()}, session: rrd-demo-{round_id}-{side})" in result.stdout
    recorded_mode, codex_home, planner_home, arguments = marker.read_text().strip().split("|")
    assert recorded_mode == mode
    assert codex_home == str(root / f"runs/rrd-demo/{round_id}/{side}/codex-home")
    assert planner_home == str(root / f"runs/rrd-demo/{round_id}/{side}/planner-home")
    assert arguments == "--dangerously-bypass-hook-trust"
    config = (Path(codex_home) / "config.toml").read_text()
    assert 'env_key = "OLLAMA_API_KEY"' in config
    assert 'wire_api = "responses"' in config
    assert "[agents.worker]" in config
    assert "plugins = false" in config
    assert ":8790/ollama/" in config
    assert "test-key" not in config
    assert "opencode" not in config.lower()
