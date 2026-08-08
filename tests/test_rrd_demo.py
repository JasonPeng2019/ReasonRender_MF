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
def test_shell_sides_launch_the_opencode_tui(tmp_path: Path, side: str) -> None:
    root = tmp_path / "repo"
    contextmesh = root / "contextmesh"
    scripts = contextmesh / "scripts"
    scripts.mkdir(parents=True)
    copied_script = contextmesh / "RRDdemo.sh"
    copied_script.write_bytes(SCRIPT.read_bytes())
    copied_script.chmod(0o755)
    start = scripts / "start_stack.sh"
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


def test_rrd_script_describes_the_same_three_terminal_tui_flow() -> None:
    script = SCRIPT.read_text()

    assert "open the COLD opencode TUI" in script
    assert "open the WARM opencode TUI" in script
    assert 'exec "$ROOT/scripts/rrd_demo_tui.sh" "$cmd"' in script
    assert "headless" not in script.lower()


def test_rrd_prep_resets_seeds_and_copies_the_audit_prompt(tmp_path: Path) -> None:
    root = tmp_path / "contextmesh"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "RRDdemo.sh")
    prompt = "four-worker canonical audit\n"
    (root / "RRD-demo-prompt.txt").write_text(prompt)
    events = tmp_path / "events"
    (scripts / "start_stack.sh").write_text('#!/bin/sh\necho start >> "$RRC_PREP_EVENTS"\n')
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


def test_rrd_opencode_assets_are_present_and_wired_to_the_real_pipeline() -> None:
    repo = SCRIPT.parents[1]
    launcher = (repo / "contextmesh/scripts/rrd_demo_tui.sh").read_text()
    wrapper = (repo / "contextmesh/RRDdemo.sh").read_text()
    plugin = (repo / "contextmesh/plugin/reasonrendercoding.ts").read_text()
    prompt = (repo / "contextmesh/RRD-demo-prompt.txt").read_text()
    canonical_prompt = (repo / "contextmesh/demo-prompt.txt").read_text()
    config_a = json.loads((repo / "contextmesh/configs/rrd-arm-a.json").read_text())
    config_b = json.loads((repo / "contextmesh/configs/rrd-arm-b.json").read_text())

    assert 'exec "$OC"' in launcher
    assert 'OPENCODE_CONFIG="$ROOT/configs/rrd-arm-$ARM.json"' in launcher
    assert 'RRC_DEMO_MODE="$MODE"' in launcher
    assert 'CONTEXTMESH_PLUGIN_PATH="file://$ROOT/plugin/contextmesh.ts"' in launcher
    assert "python -m rrc.multiagent_demo resolve" in plugin
    assert 'input.tool !== "task"' in plugin
    assert "output.args.prompt =" in plugin
    assert "fail_open" in plugin
    assert "reasonrender_coding" not in plugin
    assert wrapper.index('"$ROOT/scripts/rrd_demo_preflight.sh"') < wrapper.index(
        '"$ROOT/scripts/rrd_demo_tui.sh" reset'
    )
    assert 'rrd_demo_preflight.sh" || true' not in wrapper
    assert prompt == canonical_prompt
    assert "ONE worker subagent per handler" in prompt
    for config in (config_a, config_b):
        assert config["default_agent"] == "orchestrator"
        assert config["subagent_depth"] == 1
        assert config["agent"]["worker"]["mode"] == "subagent"
        assert "all four" in config["agent"]["orchestrator"]["prompt"].lower()
        assert config["plugin"] == [
            "{env:CONTEXTMESH_PLUGIN_PATH}",
            "{env:RRC_PLUGIN_PATH}",
        ]


@pytest.mark.parametrize(("side", "mode"), [("a", "cold"), ("b", "warm")])
def test_rrd_tui_reaches_the_real_opencode_entrypoint(tmp_path: Path, side: str, mode: str) -> None:
    root = tmp_path / "contextmesh"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(TUI_SCRIPT, scripts / "rrd_demo_tui.sh")
    (root / ".env.local").write_text(
        "OLLAMA_API_KEY=test-key\nCONTEXTMESH_MODEL=test-outer-model\n"
    )
    (root / "configs").mkdir()
    (root / "configs" / f"rrd-arm-{side}.json").write_text("{}\n")
    template = root / "bench" / "target-template"
    template.mkdir(parents=True)
    (template / "README.md").write_text("demo\n")
    (root / "RRD-demo-prompt.txt").write_text("website prompt\n")
    runs = root / "runs" / "rrd-demo"
    runs.mkdir(parents=True)
    (runs / "round").write_text("rrd-test\n")
    (runs / "rrd-test").mkdir()
    (runs / "rrd-test/.combined-multiagent-v1").touch()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("curl", "codex", "uv"):
        executable = fake_bin / name
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    marker = tmp_path / "opencode-env"
    opencode = fake_bin / "opencode"
    opencode.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "--version" ]; then echo 1.18.15; exit 0; fi\n'
        'printf "%s|%s|%s|%s\\n" "$RRC_DEMO_MODE" "$OPENCODE_CONFIG" '
        '"$RRC_PLUGIN_PATH" "$CONTEXTMESH_PLUGIN_PATH" > "$RRC_TUI_MARKER"\n'
        "exit 73\n"
    )
    opencode.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "CONTEXTMESH_OPENCODE_BIN": str(opencode),
            "RRC_STRONG_MODEL": "test-inner-model",
            "RRC_TUI_MARKER": str(marker),
        }
    )

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
    assert f"side {side} ({mode.upper()}, session: rrd-demo-rrd-test-{side})" in result.stdout
    recorded_mode, config, plugin, contextmesh_plugin = marker.read_text().strip().split("|")
    assert recorded_mode == mode
    assert config == str(root / "configs" / f"rrd-arm-{side}.json")
    assert plugin == f"file://{root}/plugin/reasonrendercoding.ts"
    assert contextmesh_plugin == f"file://{root}/plugin/contextmesh.ts"
